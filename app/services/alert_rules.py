"""Alertes PERSONNELLES : chaque compte choisit ce qui doit le prevenir.

Une regle appartient au compte qui l'a creee, et ses emails partent A SON
ADRESSE, par le compte SMTP de Settings. Trois sortes :

- ``events`` : les problemes detectes par la surveillance (routeur muet, site
  tombe, mesure en panne, limite non tenue, UISP, urgence en cours), d'une ou
  plusieurs categories, eventuellement sur des sites choisis ;
- ``client_offline`` : des clients choisis, hors ligne ;
- ``router_cpu`` : un coeur de routeur au-dessus d'un seuil choisi.

Chaque regle a son DELAI : un probleme ne part que s'il dure depuis ce delai
(« plus de 5 minutes »), et sa resolution est annoncee (si demande).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.vlan_sites import site_key

logger = logging.getLogger(__name__)

KIND_EVENTS = "events"
KIND_CLIENT_OFFLINE = "client_offline"
KIND_ROUTER_CPU = "router_cpu"
KINDS = (KIND_EVENTS, KIND_CLIENT_OFFLINE, KIND_ROUTER_CPU)
#: Les categories de problemes qu'une regle « events » peut suivre.
CATEGORIES = ("router", "site", "measurement", "limit", "uisp", "emergency")
#: Un client est hors ligne quand il n'a plus de mesure depuis 2 minutes.
OFFLINE_AFTER = timedelta(minutes=2)


class InvalidAlertRuleError(ValueError):
    pass


def check(payload: Mapping[str, Any]) -> dict[str, Any]:
    nom = str(payload.get("name") or "").strip()
    if not nom:
        raise InvalidAlertRuleError("an alert needs a name")
    genre = payload.get("kind") or KIND_EVENTS
    if genre not in KINDS:
        raise InvalidAlertRuleError("kind: 'events', 'client_offline' or 'router_cpu'")
    categories = sorted({str(c) for c in payload.get("categories") or [] if str(c) in CATEGORIES})
    cibles = sorted({str(t).strip() for t in payload.get("targets") or [] if str(t).strip()})
    sites = sorted({str(t).strip() for t in payload.get("sites") or [] if str(t).strip()})
    seuil: Any = payload.get("threshold")
    if genre == KIND_EVENTS and not categories:
        raise InvalidAlertRuleError("tick at least one kind of problem")
    if genre == KIND_CLIENT_OFFLINE and not cibles:
        raise InvalidAlertRuleError("choose the clients to watch")
    if genre == KIND_ROUTER_CPU:
        try:
            seuil = float(seuil)
        except (TypeError, ValueError) as exc:
            raise InvalidAlertRuleError("give the CPU threshold, in %") from exc
        if not 10 <= seuil <= 100:
            raise InvalidAlertRuleError("CPU threshold: between 10 and 100 %")
    else:
        seuil = None
    try:
        delai = int(payload.get("delay_minutes", 3))
    except (TypeError, ValueError) as exc:
        raise InvalidAlertRuleError("delay: a number of minutes") from exc
    if not 0 <= delai <= 1440:
        raise InvalidAlertRuleError("delay: between 0 and 1440 minutes")
    return {
        "name": nom[:80],
        "kind": genre,
        "categories": categories if genre == KIND_EVENTS else [],
        "sites": sites if genre == KIND_EVENTS else [],
        "targets": cibles if genre != KIND_EVENTS else [],
        "threshold": seuil,
        "delay_minutes": delai,
        "notify_resolved": bool(payload.get("notify_resolved", True)),
        "enabled": bool(payload.get("enabled", True)),
    }


def _ts(valeur: Any) -> datetime | None:
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    return None


def matches(
    rule: Mapping[str, Any],
    *,
    problems: Iterable[Mapping[str, Any]],
    clients: Iterable[Mapping[str, Any]],
    cpu: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Ce que cette regle voit en ce moment : {key, level, title, detail}."""
    genre = rule.get("kind")
    if genre == KIND_EVENTS:
        categories = set(rule.get("categories") or [])
        sites = {site_key(s) for s in rule.get("sites") or []}
        return [
            {k: p.get(k) for k in ("key", "level", "title", "detail")}
            for p in problems
            if p.get("category") in categories
            and (not sites or (p.get("site") and site_key(str(p["site"])) in sites))
        ]
    if genre == KIND_CLIENT_OFFLINE:
        cibles = set(rule.get("targets") or [])
        sortie = []
        for c in clients:
            login = str(c.get("login") or "")
            vu = _ts(c.get("ts"))
            if login not in cibles or vu is None or now - vu <= OFFLINE_AFTER:
                continue
            nom = c.get("display_name") or login
            sortie.append(
                {
                    "key": f"offline:{login}",
                    "level": "warn",
                    "title": f"Client {nom} offline",
                    "detail": f"Last seen {vu:%Y-%m-%d %H:%M} UTC"
                    + (f" on {c['pop_name']}" if c.get("pop_name") else "")
                    + ".",
                }
            )
        return sortie
    if genre == KIND_ROUTER_CPU:
        seuil = float(rule.get("threshold") or 100)
        cibles = set(rule.get("targets") or [])
        sortie = []
        for routeur, releve in cpu.items():
            if cibles and routeur not in cibles:
                continue
            coeur = releve.get("max_core")
            if coeur is None or float(coeur) < seuil:
                continue
            sortie.append(
                {
                    "key": f"cpu:{routeur}",
                    "level": "crit" if float(coeur) >= 95 else "warn",
                    "title": f"Router {routeur}: CPU at {coeur} %",
                    "detail": f"Its busiest core is above your threshold of {seuil:g} %"
                    + (
                        f" (average of the cores {releve['avg']} %)"
                        if releve.get("avg") is not None
                        else ""
                    )
                    + ". See Router health.",
                }
            )
        return sortie
    return []


async def smtp_ready(container: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Le compte SMTP, ou pourquoi on ne peut pas envoyer."""
    repo = getattr(container, "notifications_repo", None)
    if repo is None:
        return None, "email is unavailable (database not initialised)"
    config = await repo.config()
    if not config or not config.get("host") or not config.get("from_addr"):
        return None, "no SMTP account yet: an Administrator sets it in Settings › Email alerts"
    if not config.get("enabled"):
        return None, "email alerts are switched off in Settings › Email alerts"
    return config, None


async def send_to(
    container: Any,
    recipient: str,
    *,
    rule_name: str,
    nouveaux: list[dict[str, Any]],
    resolus: list[dict[str, Any]],
) -> None:
    """Un email a UNE adresse, avec le compte SMTP de Settings."""
    from app.services import notifier
    from app.services.health_watch import public_url

    config, motif = await smtp_ready(container)
    if config is None:
        raise notifier.MailError(motif or "email unavailable")
    domaine = None
    acces = getattr(container, "public_access_repo", None)
    if acces is not None:
        try:
            fiche = await acces.get()
            domaine = fiche.get("domain") if fiche else None
        except Exception:  # noqa: BLE001
            domaine = None
    sujet, texte, corps = notifier.render_alerts(
        nouveaux=nouveaux, resolus=resolus, url=public_url(domaine)
    )
    sujet = sujet.replace("[freeQoS]", f"[freeQoS · {rule_name}]", 1)
    message = notifier.build_message(
        from_addr=config["from_addr"],
        recipients=[recipient],
        subject=sujet,
        text=texte,
        html_body=corps,
    )
    await notifier.send(config, await container.notifications_repo.password(), message)


async def run_rules(
    container: Any,
    *,
    problems: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Un tour : chaque regle active voit, retient, et previent son proprietaire."""
    from app.services import notifier

    repo = getattr(container, "alert_rules_repo", None)
    if repo is None:
        return {"rules": 0, "sent": 0}
    cpu_service = getattr(container, "router_cpu", None)
    cpu = cpu_service.latest() if cpu_service is not None else {}
    _config, motif = await smtp_ready(container)
    envoyes = 0
    regles = [r for r in await repo.list() if r.get("enabled")]
    for regle in regles:
        ident = int(regle["id"])
        vus = matches(regle, problems=problems, clients=clients, cpu=cpu, now=now)
        avant = await repo.events(ident)
        for p in vus:
            await repo.upsert_event(ident, p, now)
        actuels = {str(p["key"]) for p in vus}
        delai = timedelta(minutes=int(regle.get("delay_minutes") or 0))
        nouveaux = [
            p
            for p in vus
            if (avant.get(str(p["key"])) or {}).get("notified_at") is None
            and now - ((avant.get(str(p["key"])) or {}).get("first_seen") or now) >= delai
        ]
        partis = [e for k, e in avant.items() if k not in actuels]
        resolus = (
            [e for e in partis if e.get("notified_at")] if regle.get("notify_resolved") else []
        )
        await repo.forget(ident, [str(e["key"]) for e in partis])
        if not nouveaux and not resolus:
            continue
        if motif is not None:
            await repo.mark(ident, ok=False, error=motif)
            continue
        try:
            await send_to(
                container,
                str(regle["owner_email"]),
                rule_name=str(regle["name"]),
                nouveaux=nouveaux,
                resolus=resolus,
            )
        except notifier.MailError as exc:
            await repo.mark(ident, ok=False, error=str(exc))
            logger.warning("Alerte '%s' non envoyee : %s", regle["name"], exc)
            continue
        await repo.mark(ident, ok=True)
        await repo.mark_notified(ident, [str(p["key"]) for p in nouveaux], now)
        envoyes += 1
    return {"rules": len(regles), "sent": envoyes}

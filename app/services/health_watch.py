"""Surveillance cote serveur : les defaillances, calculees chaque minute.

Les alertes de l'interface ne vivent que dans un navigateur ouvert. Pour
prevenir par email, le serveur doit savoir seul qu'un routeur ne repond plus
ou qu'un site est tombe. ``evaluate`` est pur (teste sans base) ; ``run_cycle``
tient l'etat des problemes et envoie UN email par cycle : ceux qui durent
depuis le delai de grace, et ceux qui viennent d'etre resolus.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

JOB_WATCH = "watch_alerts"

#: Un client hors ligne : sans mesure depuis 2 minutes (un cycle dure 10 s).
OFFLINE_AFTER = timedelta(minutes=2)
#: Ne compte, pour un site, que les clients vus dans la journee.
RECENT = timedelta(hours=24)


def _ts(valeur: Any) -> datetime | None:
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    return None


def evaluate(
    *,
    now: datetime,
    router_reads: Mapping[str, Mapping[str, Any]],
    router_sites: Mapping[str, str],
    clients: Iterable[Mapping[str, Any]],
    jobs: Iterable[Mapping[str, Any]],
    extra: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Les problemes du moment : {key, category, level, title, detail}."""
    problemes: list[dict[str, Any]] = []

    # 1. Routeurs que freeQoS ne lit plus (depuis plus d'une minute).
    sites_muets: set[str] = set()
    for routeur, lecture in router_reads.items():
        depuis = _ts(lecture.get("failing_since"))
        if depuis is None or now - depuis < timedelta(minutes=1):
            continue
        sites_muets.add(str(router_sites.get(routeur, "")))
        problemes.append(
            {
                "key": f"router:{routeur}",
                "category": "router",
                "level": "crit",
                "title": f"Router {routeur} not answering freeQoS",
                "detail": f"{lecture.get('error') or 'no answer'} · since {depuis:%H:%M} UTC. "
                "Its clients are not measured meanwhile.",
            }
        )

    # 2. Sites dont les clients sont tombes ENSEMBLE (la moitie, et au moins 2).
    par_site: dict[str, list[datetime]] = {}
    for client in clients:
        vu = _ts(client.get("ts"))
        site = str(client.get("pop_name") or "")
        if vu is None or not site or now - vu > RECENT:
            continue
        par_site.setdefault(site, []).append(vu)
    for site, vus in par_site.items():
        if site in sites_muets:
            continue  # deja dit : c'est la lecture qui manque, pas les clients
        tombes = [v for v in vus if now - v > OFFLINE_AFTER]
        if len(tombes) >= 2 and len(tombes) * 2 >= len(vus):
            problemes.append(
                {
                    "key": f"site:{site}",
                    "category": "site",
                    "level": "crit",
                    "title": f"Site {site} down",
                    "detail": f"{len(tombes)} of {len(vus)} clients went offline together: "
                    "check the site link (backhaul radio, power, site router).",
                }
            )

    # 2 bis. Des limites que le routeur ne tient pas : des clients mesures a
    # l'instant bien au-dessus (125 %) de la limite qu'ils devraient avoir.
    au_dessus: list[str] = []
    for client in clients:
        vu = _ts(client.get("ts"))
        limite = float(client.get("effective_down_mbps") or 0)
        if vu is None or now - vu > timedelta(seconds=90) or limite <= 0:
            continue
        if float(client.get("tx_bps") or 0) / 1e6 >= limite * 1.25:
            au_dessus.append(str(client.get("login")))
    if au_dessus:
        problemes.append(
            {
                "key": "limit:over",
                "category": "limit",
                "level": "warn",
                "title": f"{len(au_dessus)} client(s) over their limit",
                "detail": ", ".join(sorted(au_dessus)[:8])
                + (" ..." if len(au_dessus) > 8 else "")
                + ": the queue is missing or bypassed (FastTrack). Reset queues on Devices.",
            }
        )

    # 3. La mesure elle-meme : un cycle qui echoue depuis trois tours.
    for job in jobs:
        intervalle = float(job.get("interval_s") or 0)
        sans_succes = job.get("seconds_since_last_success")
        if job.get("last_ok") is False and intervalle and sans_succes is not None:
            if float(sans_succes) >= max(3 * intervalle, 120):
                problemes.append(
                    {
                        "key": f"job:{job['job']}",
                        "category": "measurement",
                        "level": "warn",
                        "title": f"Collection '{job['job']}' failing",
                        "detail": str(job.get("last_error") or "repeated failures")[:300],
                    }
                )

    problemes.extend(extra)
    return problemes


def public_url(domaine: str | None) -> str | None:
    return f"https://{domaine}/" if domaine else None


async def run_cycle(container: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Un tour de surveillance. Rend ce qui a ete detecte et envoye."""
    from app.services import notifier
    from app.services.vlan_sites import present_site, router_pops, vlan_parents

    repo = getattr(container, "notifications_repo", None)
    if repo is None:
        return {"problems": 0, "sent": False}
    now = now or datetime.now(tz=UTC)
    collection = getattr(container, "collection", None)
    lectures = dict(getattr(collection, "router_reads", {}) or {})
    sites_routeurs = router_pops(container)
    clients: list[dict[str, Any]] = []
    metrics = getattr(container, "repository", None)
    if metrics is not None:
        clients = list(
            await metrics.subscriber_latest(limit=5000, order_by="login", include_unmeasured=True)
        )
        parents = vlan_parents(list(await metrics.pop_sites()), sites_routeurs)
        for ligne in clients:
            present_site(ligne, parents)
    planificateur = getattr(container, "scheduler", None)
    jobs = planificateur.status() if planificateur is not None else []
    extra: list[dict[str, Any]] = []
    for source in getattr(container, "watch_sources", []) or []:
        try:
            extra.extend(await source())
        except Exception:  # noqa: BLE001 - une source muette n'empeche pas les autres
            logger.exception("Source de surveillance en echec")

    problemes = evaluate(
        now=now,
        router_reads=lectures,
        router_sites=sites_routeurs,
        clients=clients,
        jobs=[j for j in jobs if j.get("job") != JOB_WATCH],
        extra=extra,
    )
    ouverts = await repo.open_events()
    for p in problemes:
        await repo.upsert_event(p, now)
    actuels = {p["key"] for p in problemes}
    await repo.resolve([k for k in ouverts if k not in actuels], now)

    config = await repo.config()
    rapport: dict[str, Any] = {"problems": len(problemes), "sent": False}
    if not config or not config.get("enabled") or not config.get("recipients"):
        return rapport
    categories = set(config.get("events") or [])
    grace = timedelta(minutes=int(config.get("grace_minutes") or 0))
    etat = await repo.open_events()
    nouveaux = [
        e
        for e in etat.values()
        if e["notified_at"] is None
        and e["category"] in categories
        and now - e["first_seen"] >= grace
    ]
    resolus = [e for e in await repo.to_announce_resolved() if e["category"] in categories]
    if not nouveaux and not resolus:
        return rapport
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
    message = notifier.build_message(
        from_addr=config["from_addr"],
        recipients=config["recipients"],
        subject=sujet,
        text=texte,
        html_body=corps,
    )
    try:
        await notifier.send(config, await repo.password(), message)
    except notifier.MailError as exc:
        await repo.mark(ok=False, error=str(exc))
        logger.warning("Email d'alerte non envoye : %s", exc)
        rapport["error"] = str(exc)
        return rapport
    await repo.mark(ok=True)
    await repo.mark_notified([e["key"] for e in nouveaux], now)
    await repo.mark_resolved_notified([e["key"] for e in resolus])
    logger.info("Email d'alerte envoye : %s", sujet)
    rapport.update(sent=True, new=len(nouveaux), resolved=len(resolus))
    return rapport

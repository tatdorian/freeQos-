"""Mise en service automatique d'un equipement, des son ajout.

DEMANDE EXPLICITE : une fois le routeur connecte par son API, tout doit se
faire seul -- types de file CAKE, premieres files, export NetFlow, arbre --
sans attendre les cycles periodiques ni cliquer ailleurs.

Les cycles le faisaient deja, mais a leur rythme : files sous 2 min, export
NetFlow sous 10 min, arbre sous 15 min. Ici on les deroule TOUT DE SUITE, dans
l'ordre ou chacun a besoin du precedent, puis on relit le routeur pour dire ce
qui y est reellement pose -- pas ce qu'on a cru envoyer.

Rien n'est contourne : l'ecriture reste soumise a ENFORCEMENT_ENABLED et aux
droits reels du compte. S'il manque l'un ou l'autre, le rapport le dit en
clair, avec le geste qui debloque.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.enforcement.models import MANAGED_COMMENT
from app.services.collection import (
    JOB_LINKS,
    JOB_PLANS,
    JOB_RECONCILE,
    JOB_RTT,
    JOB_SUBSCRIBERS,
    JOB_TOPOLOGY,
    JOB_VLAN_CLIENTS,
)
from app.services.netflow_export import JOB_NETFLOW_EXPORT
from app.services.restrictions import JOB_RESTRICTIONS

logger = logging.getLogger(__name__)

#: Les cycles a derouler, dans l'ordre : chacun s'appuie sur le precedent (les
#: files ont besoin des sessions et des plans, l'export du loopback lu a la
#: decouverte).
ETAPES: tuple[tuple[str, str], ...] = (
    (JOB_SUBSCRIBERS, "Read the PPPoE sessions"),
    (JOB_PLANS, "Load the subscriber plans"),
    (JOB_LINKS, "Read the ports"),
    (JOB_TOPOLOGY, "Discover the topology (tree, uplink, loopback)"),
    (JOB_RECONCILE, "Create the CAKE queue types and the subscriber queues"),
    (JOB_NETFLOW_EXPORT, "Configure the NetFlow export"),
    (JOB_RESTRICTIONS, "Apply the traffic restrictions"),
    (JOB_VLAN_CLIENTS, "Detect static-IP clients on the VLANs"),
    (JOB_RTT, "Measure latency"),
)
ETAPE_TIMEOUT_S = 90.0


@dataclass
class Provisioning:
    router: str
    state: str = "running"  # running | done | blocked | failed
    started_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    finished_at: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    blocker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "router": self.router,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": list(self.steps),
            "result": dict(self.result),
            "blocker": self.blocker,
        }


#: Dernier deroulement par routeur, lu par l'interface pendant qu'il avance.
ETATS: dict[str, Provisioning] = {}
_TACHES: set[asyncio.Task[None]] = set()


def status(router: str) -> dict[str, Any] | None:
    etat = ETATS.get(router)
    return etat.to_dict() if etat else None


def start(container: Any, router: str) -> dict[str, Any]:
    """Lance la mise en service en tache de fond ; rend l'etat initial."""
    etat = Provisioning(router=router)
    ETATS[router] = etat
    tache = asyncio.create_task(_run(container, etat), name=f"provision:{router}")
    _TACHES.add(tache)
    tache.add_done_callback(_TACHES.discard)
    return etat.to_dict()


async def run(container: Any, router: str) -> dict[str, Any]:
    """Version attendue (tests, API) : rend le rapport complet."""
    etat = Provisioning(router=router)
    ETATS[router] = etat
    await _run(container, etat)
    return etat.to_dict()


async def _run(container: Any, etat: Provisioning) -> None:
    try:
        await _derouler(container, etat)
    except Exception as exc:  # noqa: BLE001 - le rapport doit toujours se terminer
        logger.exception("Mise en service de %s interrompue", etat.router)
        etat.state = "failed"
        etat.blocker = f"{type(exc).__name__}: {exc}"
    finally:
        etat.finished_at = datetime.now(tz=UTC).isoformat()


async def _derouler(container: Any, etat: Provisioning) -> None:
    shaping = container.shaping
    if not any(c.name == etat.router for c in container.registry.collectors):
        etat.state = "failed"
        etat.blocker = (
            f"'{etat.router}' is not collected: check the address, the account and the "
            "password (Devices > Test the connection)."
        )
        return

    # 1. Peut-on ecrire ? Sans cela, tout le reste se calculerait pour rien.
    ecriture_ok = bool(shaping.enforcement_enabled)
    droits: Any = None
    try:
        droits = await asyncio.wait_for(shaping.write_capability(etat.router), 20.0)
    except Exception as exc:  # noqa: BLE001
        etat.steps.append(
            {"step": "Check the write rights", "ok": None, "detail": f"{type(exc).__name__}: {exc}"}
        )
    else:
        etat.steps.append(
            {
                "step": "Check the write rights",
                "ok": droits.can_write,
                "detail": droits.detail
                or (f"account '{droits.username}', group '{droits.group}'" if droits.group else ""),
            }
        )
    if not ecriture_ok:
        etat.blocker = (
            "Writing to the routers is off: nothing was created. Turn it on in "
            "Settings > Shaping and writing (or ENFORCEMENT_ENABLED=true), and every "
            "queue, CAKE type and NetFlow export is set up on its own."
        )
    elif droits is not None and droits.can_write is False:
        etat.blocker = droits.detail or "This account cannot write on the router."

    # 2. Les cycles, tout de suite et dans l'ordre.
    for job, libelle in ETAPES:
        if etat.blocker and job in (JOB_RECONCILE, JOB_NETFLOW_EXPORT, JOB_RESTRICTIONS):
            etat.steps.append({"step": libelle, "ok": None, "detail": "skipped: see above"})
            continue
        try:
            resultat = await asyncio.wait_for(
                container.scheduler.run_once(job), timeout=ETAPE_TIMEOUT_S
            )
        except KeyError:
            continue  # job absent de cette installation
        except TimeoutError:
            etat.steps.append(
                {
                    "step": libelle,
                    "ok": False,
                    "detail": f"no answer within {ETAPE_TIMEOUT_S:.0f} s",
                }
            )
            continue
        ok = None if resultat is None else bool(resultat.ok)
        detail = ""
        if resultat is None:
            # Job sans compte rendu detaille : le planificateur a quand meme
            # retenu s'il a abouti, et son erreur le cas echeant.
            suivi = next((j for j in container.scheduler.status() if j.get("job") == job), None)
            if suivi is not None:
                ok = suivi.get("last_ok")
                detail = str(suivi.get("last_error") or "")
        if resultat is not None and not resultat.ok and resultat.errors:
            detail = "; ".join(str(e) for e in list(resultat.errors)[:3])
        elif resultat is not None and resultat.items:
            detail = f"{resultat.items} item(s)"
        etat.steps.append({"step": libelle, "ok": ok, "detail": detail})

    # 3. Ce qui est REELLEMENT sur le routeur, relu.
    etat.result = await _constat(container, etat.router)
    if etat.blocker:
        etat.state = "blocked"
    elif all(s.get("ok") is not False for s in etat.steps):
        etat.state = "done"
    else:
        etat.state = "failed"
    logger.info("Mise en service de %s : %s %s", etat.router, etat.state, etat.result)


async def _constat(container: Any, router: str) -> dict[str, Any]:
    constat: dict[str, Any] = {}
    try:
        [lu] = await container.shaping.inspect(router)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not lu.reachable:
        return {"error": lu.error}
    types = [
        str(t.get("name")) for t in lu.queue_types if "cake" in str(t.get("kind") or t.get("name"))
    ]
    constat["cake_types"] = sorted(t for t in types if t.startswith("freeqos"))
    constat["managed_queues"] = len(lu.managed_queues)
    constat["other_queues"] = len(lu.foreign_queues)
    export = getattr(container, "netflow_export", None)
    collecteur = next((c for c in container.registry.collectors if c.name == router), None)
    if export is not None and collecteur is not None:
        try:
            etat_export = await asyncio.wait_for(export.state_of(collecteur), 20.0)
            constat["netflow_export"] = {
                "enabled": etat_export.enabled,
                "collector": etat_export.collector,
                "state": etat_export.state,
            }
        except Exception as exc:  # noqa: BLE001
            constat["netflow_export"] = {"error": f"{type(exc).__name__}: {exc}"}
    constat["managed_comment"] = MANAGED_COMMENT
    avertissement = _docker_sans_adresse(container, constat.get("netflow_export") or {})
    if avertissement:
        constat["warnings"] = [avertissement]
    return constat


def _docker_sans_adresse(container: Any, export: dict[str, Any]) -> str | None:
    """Sous Docker, l'adresse locale vue du conteneur est celle du pont (172.x) :
    les routeurs ne la joignent pas, le port est publie sur l'hote. Sans
    NETFLOW_COLLECTOR_ADDRESS, les flux partiraient dans le vide."""
    import ipaddress
    from pathlib import Path

    reglage = getattr(getattr(container, "settings", None), "netflow_collector_address", None)
    collecteur = str(export.get("collector") or "").split(":")[0]
    if reglage or not collecteur or not Path("/.dockerenv").exists():
        return None
    try:
        dans_le_pont = ipaddress.ip_address(collecteur) in ipaddress.ip_network("172.16.0.0/12")
    except ValueError:
        return None
    if not dans_le_pont:
        return None
    return (
        f"NetFlow is sent to {collecteur}, the container's address: routers cannot reach it. "
        "Set NETFLOW_COLLECTOR_ADDRESS in .env to this server's IP as the routers see it."
    )

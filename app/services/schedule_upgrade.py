"""Mise a niveau des plannings vers les SCHEDULERS, au demarrage.

UN SCHEDULER = UN DEBIT. Un planning d'avant portait plusieurs debits (soir
70 %, nuit 200 %) : il est separe en un scheduler par debit, memes clients et
memes heures (« Day and night » garde le premier, « Day and night · night »
porte le second). Ses plages ne se chevauchaient pas : les schedulers qui en
sortent non plus. Rien ne change sur les routeurs.

Les modes crees automatiquement par la version precedente pour chaque creneau
(Evening, Night, Constant), qu'aucun client et aucun scheduler n'utilisent,
sont retires : le scheduler lance a la main les remplace.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Les notes que la version precedente posait sur les modes qu'elle creait.
NOTES_AUTOMATIQUES = (
    "Follows the time slot of a schedule: change its % in Schedules.",
    "Keeps the plan at all hours, whatever the schedule.",
)


def _cle_debit(w: dict[str, Any]) -> tuple[Any, ...]:
    return (w.get("percent"), w.get("down_mbps"), w.get("up_mbps"), w.get("mode_id"))


async def split_mixed_schedules(schedules_repo: Any) -> list[str]:
    """Un planning a plusieurs debits -> un scheduler par debit. Rend les noms crees."""
    crees: list[str] = []
    lignes = await schedules_repo.list_all()
    pris = {str(r["name"]).lower() for r in lignes}
    for ligne in lignes:
        plages = list(ligne.get("windows") or [])
        groupes: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for w in plages:
            groupes.setdefault(_cle_debit(w), []).append(w)
        if len(groupes) < 2:
            continue
        premier, *autres = list(groupes.values())
        for groupe in autres:
            suffixe = str(groupe[0].get("label") or "").strip() or (
                f"{groupe[0].get('percent'):g} %"
                if groupe[0].get("percent") is not None
                else "part 2"
            )
            nom = f"{ligne['name']} · {suffixe}"[:128]
            n = 2
            while nom.lower() in pris:
                nom = f"{ligne['name']} · {suffixe} {n}"[:128]
                n += 1
            pris.add(nom.lower())
            await schedules_repo.create(
                {
                    "name": nom,
                    "enabled": ligne.get("enabled", True),
                    "timezone": ligne.get("timezone"),
                    "scope": ligne.get("scope"),
                    "targets": ligne.get("targets") or [],
                    "windows": groupe,
                    "note": ligne.get("note"),
                }
            )
            crees.append(nom)
        await schedules_repo.update(int(ligne["id"]), {"windows": premier})
        logger.info("Planning '%s' separe en schedulers : %s", ligne["name"], crees)
    return crees


async def drop_unused_auto_modes(container: Any) -> list[str]:
    """Retire les modes crees automatiquement que personne n'utilise."""
    modes_repo = getattr(container, "modes_repo", None)
    topo = getattr(container, "topology_repo", None)
    if modes_repo is None or topo is None:
        return []
    retires: list[str] = []
    for mode in await modes_repo.list():
        if mode.get("note") not in NOTES_AUTOMATIQUES:
            continue
        ident = int(mode["id"])
        if await topo.clients_in_mode(ident) or await modes_repo.schedules_using(ident):
            continue
        await modes_repo.delete(ident)
        retires.append(str(mode["name"]))
    if retires:
        logger.info("Modes automatiques inutilises retires : %s", retires)
    return retires


async def upgrade(container: Any) -> None:
    depot = getattr(container, "schedules_repo", None)
    if depot is not None:
        await split_mixed_schedules(depot)
    await drop_unused_auto_modes(container)

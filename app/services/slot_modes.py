"""Chaque creneau d'un planning est aussi un MODE, sans rien faire.

LE BESOIN (retour terrain). Le planning applique Evening 70 % et Night 200 %
tout seul. Mais :

- mettre UN client en Night a la main etait impossible : la liste des modes
  d'un client n'offrait qu'Automatic, les creneaux n'etant pas des modes ;
- garder un client a son forfait a toute heure, planning ou pas, demandait de
  savoir creer un mode a 100 %.

CE QUI SE PASSE. A chaque enregistrement d'un planning, chaque creneau NOMME
en pourcentage du forfait devient un mode du meme nom et du meme pourcentage
(Evening, Night). Le pourcentage se modifie dans le planning, comme avant : le
mode suit, et les clients mis dans ce mode a la main aussi. Un mode
**Constant** (100 % du forfait) existe d'office : un client dans ce mode garde
son forfait a toute heure, un mode passant avant le planning.

Un mode cree a la main sous le meme nom n'est jamais modifie : seuls les modes
que freeQoS a crees pour un creneau suivent le planning (leur note le dit).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

NOTE_CRENEAU = "Follows the time slot of a schedule: change its % in Schedules."
NOTE_CONSTANT = "Keeps the plan at all hours, whatever the schedule."
CONSTANT = "Constant"


def _nom(label: Any) -> str:
    brut = str(label or "").strip()
    return brut[:1].upper() + brut[1:64] if brut else ""


async def sync_slot_modes(
    container: Any, windows: list[dict[str, Any]], *, author: str
) -> list[str]:
    """Cree ou aligne les modes des creneaux, et le mode Constant. Rend leurs noms."""
    depot = getattr(container, "modes_repo", None)
    if depot is None:
        return []
    existants = {str(m["name"]).lower(): m for m in await depot.list()}
    touches: list[str] = []
    modifies: list[dict[str, Any]] = []

    voulus: list[tuple[str, float, float, str]] = []
    for w in windows:
        if w.get("mode_id") is not None or w.get("percent") is None:
            continue
        nom = _nom(w.get("label"))
        if not nom:
            continue
        bas = float(w["percent"])
        haut = float(w.get("percent_up") or bas)
        voulus.append((nom, bas, haut, NOTE_CRENEAU))
    if CONSTANT.lower() not in existants:
        voulus.append((CONSTANT, 100.0, 100.0, NOTE_CONSTANT))

    for nom, bas, haut, note in voulus:
        actuel = existants.get(nom.lower())
        if actuel is None:
            cree = await depot.create(
                {
                    "name": nom,
                    "rate_kind": "percent",
                    "down_value": bas,
                    "up_value": haut,
                    "note": note,
                }
            )
            existants[nom.lower()] = cree
            touches.append(nom)
            continue
        if actuel.get("note") != NOTE_CRENEAU:
            continue  # un mode de l'exploitant : jamais touche
        if (
            actuel.get("rate_kind") == "percent"
            and float(actuel["down_value"]) == bas
            and float(actuel["up_value"]) == haut
        ):
            continue
        modifie = await depot.update(
            int(actuel["id"]), {"rate_kind": "percent", "down_value": bas, "up_value": haut}
        )
        existants[nom.lower()] = modifie
        modifies.append(modifie)
        touches.append(nom)

    # Les clients mis dans un mode qui vient de changer suivent tout de suite.
    topo = getattr(container, "topology_repo", None)
    if modifies and topo is not None:
        from app.api.modes import _donner_mode

        for mode in modifies:
            for login in await topo.clients_in_mode(int(mode["id"])):
                try:
                    await _donner_mode(container, container.repository, login, mode, author=author)
                except Exception:  # noqa: BLE001 - la reconciliation rattrapera
                    logger.exception("Mode '%s' non reapplique a %s", mode["name"], login)
    if touches:
        logger.info("Modes des creneaux alignes : %s", touches)
    return touches

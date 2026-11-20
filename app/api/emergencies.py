"""Page Emergency : des urgences preparees a l'avance, lancees d'un bouton.

Une urgence = un nom, un debit (% du forfait, limite fixe, ou la bande
passante d'urgence propre a chaque client) et des clients (tous, des sites, des
clients choisis). « Activate » l'applique tout de suite ; « Stop » rend a
chacun son forfait. Plusieurs urgences peuvent exister et tourner ensemble :
sur un client commun, c'est la derniere activee qui vaut.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur, noter
from app.db.emergencies_repo import (
    EmergenciesRepository,
    EmergencyConflictError,
    EmergencyNotFoundError,
)
from app.enforcement.emergency import InvalidEmergencyError, to_model

logger = logging.getLogger(__name__)

router = APIRouter(tags=["emergencies"])


class EmergencyInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, examples=["Backhaul down"])
    kind: Literal["percent", "fixed", "client"] = Field(
        default="percent",
        description=(
            "percent: a % of each client's plan; fixed: a limit in Mbps (never above a "
            "client's plan); client: each client's own emergency bandwidth"
        ),
    )
    percent: float | None = Field(default=None, gt=0, le=100)
    down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    scope: Literal["all", "clients", "pops"] = "all"
    targets: list[str] = Field(default_factory=list, max_length=5000)
    note: str | None = Field(default=None, max_length=300)


class EmergencyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal["percent", "fixed", "client"] | None = None
    percent: float | None = Field(default=None, gt=0, le=100)
    down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    scope: Literal["all", "clients", "pops"] | None = None
    targets: list[str] | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=300)


def _repo(container: Any) -> EmergenciesRepository:
    depot = getattr(container, "emergencies_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Emergencies unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


def _erreur(exc: Exception) -> HTTPException:
    if isinstance(exc, EmergencyNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, EmergencyConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _clients(container: Any) -> list[dict[str, Any]]:
    metriques = getattr(container, "repository", None)
    if metriques is None:
        return []
    lignes: list[dict[str, Any]] = await metriques.subscriber_latest(
        limit=5000, order_by="login", include_unmeasured=True
    )
    return lignes


def _vises(ligne: dict[str, Any], clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Les clients que vise cette urgence, et ce que chacun recevrait."""
    modele = to_model({**ligne, "active": True})
    sortie = []
    for c in clients:
        login = str(c.get("login") or "")
        if not modele.governs(login, c.get("pop_name")):
            continue
        propre = (
            (float(c["emergency_down_mbps"]), float(c["emergency_up_mbps"]))
            if c.get("emergency_down_mbps") and c.get("emergency_up_mbps")
            else None
        )
        debit = modele.rate(c.get("plan_down_mbps"), c.get("plan_up_mbps"), propre)
        sortie.append(
            {
                "login": login,
                "down_mbps": debit[0] if debit else None,
                "up_mbps": debit[1] if debit else None,
            }
        )
    return sortie


def _sortie(ligne: dict[str, Any], clients: list[dict[str, Any]]) -> dict[str, Any]:
    vises = _vises(ligne, clients)
    modele = to_model(ligne)
    return {
        **{k: v for k, v in ligne.items() if k not in ("active_since", "created_at", "updated_at")},
        "active_since": ligne["active_since"].isoformat() if ligne.get("active_since") else None,
        "updated_at": ligne["updated_at"].isoformat() if ligne.get("updated_at") else None,
        "describe": modele.describe(),
        "clients": len(vises),
        # Ceux qui ne changeraient pas : pas de forfait (pour un %), ou pas de
        # bande passante d'urgence propre (pour « each client's own »).
        "unchanged": sum(1 for v in vises if v["down_mbps"] is None),
    }


async def _appliquer(container: Any, raison: str) -> dict[str, Any]:
    """Relit les urgences, puis ecrit tout de suite sur les routeurs."""
    shaping = getattr(container, "shaping", None)
    if shaping is None:
        return {"applied": 0, "errors": ["shaping unavailable"]}
    await shaping.load_emergencies()
    try:
        return await shaping.apply_schedules(force=True, reason=raison)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 - l'urgence est enregistree quoi qu'il arrive
        logger.exception("Urgence non appliquee")
        return {"applied": 0, "errors": [f"{type(exc).__name__}: {exc}"]}


@router.get("/emergencies", summary="Emergencies, ready to start with one button")
async def list_emergencies(container: ContainerDep) -> dict[str, Any]:
    lignes = await _repo(container).list_all()
    clients = await _clients(container)
    urgences = [_sortie(ligne, clients) for ligne in lignes]
    return {"emergencies": urgences, "active": sum(1 for u in urgences if u["active"])}


@router.post("/emergencies", status_code=status.HTTP_201_CREATED, summary="Prepare an emergency")
async def create_emergency(
    payload: EmergencyInput, request: Request, container: ContainerDep
) -> dict[str, Any]:
    try:
        ligne = await _repo(container).create(payload.model_dump(), acteur(request))
    except (InvalidEmergencyError, EmergencyConflictError) as exc:
        raise _erreur(exc) from exc
    return _sortie(ligne, await _clients(container))


@router.patch("/emergencies/{ident}", summary="Edit an emergency")
async def update_emergency(
    payload: EmergencyUpdate,
    request: Request,
    container: ContainerDep,
    ident: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    try:
        ligne = await _repo(container).update(
            ident, payload.model_dump(exclude_unset=True), acteur(request)
        )
    except (InvalidEmergencyError, EmergencyConflictError, EmergencyNotFoundError) as exc:
        raise _erreur(exc) from exc
    sortie = _sortie(ligne, await _clients(container))
    if ligne.get("active"):
        # Elle tourne : ses clients suivent la modification tout de suite.
        sortie["enforcement"] = await _appliquer(container, f"emergency '{ligne['name']}' edited")
    return sortie


@router.delete("/emergencies/{ident}", summary="Delete an emergency")
async def delete_emergency(
    container: ContainerDep, ident: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    depot = _repo(container)
    try:
        ligne = await depot.get(ident)
        await depot.delete(ident)
    except EmergencyNotFoundError as exc:
        raise _erreur(exc) from exc
    sortie: dict[str, Any] = {"deleted": ident}
    if ligne.get("active"):
        sortie["enforcement"] = await _appliquer(container, f"emergency '{ligne['name']}' deleted")
    return sortie


async def _basculer(container: Any, request: Request, ident: int, active: bool) -> dict[str, Any]:
    depot = _repo(container)
    try:
        ligne = await depot.set_active(ident, active, acteur(request))
    except EmergencyNotFoundError as exc:
        raise _erreur(exc) from exc
    clients = await _clients(container)
    verbe = "started" if active else "stopped"
    sortie = _sortie(ligne, clients)
    sortie["enforcement"] = await _appliquer(container, f"emergency '{ligne['name']}' {verbe}")
    # Le journal de chaque client concerne le dit.
    for vise in _vises(ligne, clients):
        if vise["down_mbps"] is None:
            continue
        detail = (
            f"Emergency '{ligne['name']}' on: {vise['down_mbps']:g} / {vise['up_mbps']:g} Mbps"
            if active
            else f"Emergency '{ligne['name']}' stopped: back to its plan"
        )
        await noter(container, request, login=vise["login"], action="emergency", detail=detail)
    logger.info("Urgence '%s' %s par %s", ligne["name"], verbe, acteur(request))
    return sortie


@router.post("/emergencies/{ident}/activate", summary="Start an emergency now")
async def activate_emergency(
    request: Request, container: ContainerDep, ident: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    return await _basculer(container, request, ident, True)


@router.post("/emergencies/{ident}/stop", summary="Stop an emergency: back to the plans")
async def stop_emergency(
    request: Request, container: ContainerDep, ident: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    return await _basculer(container, request, ident, False)

"""Modes de debit et gestes sur les clients : ce que fait l'exploitant au quotidien.

DEUX NIVEAUX DE GESTE
---------------------
- UN client (niveau User) : lui donner un mode (Backup, Night...), le remettre
  en Automatic, lui donner ou lui retirer un boost ;
- PLUSIEURS clients d'un coup (niveau Supervisor) : les memes gestes sur une
  selection. Le resultat est rendu CLIENT PAR CLIENT : un echec partiel ne doit
  jamais ressembler a une reussite complete.

Definir les modes eux-memes (leur nom, leur debit) est un geste technique,
reserve au niveau Technician par la garde d'acces.

"AUTOMATIC" N'EST PAS UN MODE STOCKE : c'est l'absence de mode. Le client suit
son forfait et, s'il en a un, son profil horaire (jour / soiree / nuit) -- son
debit change alors tout seul selon l'heure.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, RepositoryDep, acteur
from app.api.shaping import _apply_for_subscriber, _require_topology, client_connu, poser_boost
from app.db.modes_repo import (
    InvalidModeError,
    ModeConflictError,
    ModeNotFoundError,
    ModesRepository,
    mode_rates,
)
from app.services.accounts import ROLE_SUPERVISOR, role_at_least

logger = logging.getLogger(__name__)

router = APIRouter(tags=["modes"])

AUTOMATIC = {
    "id": None,
    "name": "Automatic",
    "description": "Follows the client's plan and, when it has one, its time-of-day schedule",
}


class ModeInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    rate_kind: Literal["fixed", "percent"] = Field(
        default="fixed", description="'fixed': Mbps. 'percent': a share of the client's plan."
    )
    down_value: float = Field(gt=0, le=100_000)
    up_value: float = Field(gt=0, le=100_000)
    disables_boost: bool = Field(
        default=False, description="Stops any running boost and refuses new ones in this mode"
    )
    position: int = Field(default=0, ge=0, le=10_000)
    note: str | None = Field(default=None, max_length=300)


class ModeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    rate_kind: Literal["fixed", "percent"] | None = None
    down_value: float | None = Field(default=None, gt=0, le=100_000)
    up_value: float | None = Field(default=None, gt=0, le=100_000)
    disables_boost: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)
    note: str | None = Field(default=None, max_length=300)


class ModeChoice(BaseModel):
    mode_id: int | None = Field(default=None, description="null = Automatic")
    apply_now: bool = True


class ClientAction(BaseModel):
    logins: list[str] = Field(min_length=1, max_length=500)
    action: Literal["mode", "boost", "stop_boost"]
    mode_id: int | None = Field(default=None, description="For action 'mode'; null = Automatic")
    boost_multiplier: float | None = Field(default=None, gt=1, le=50)
    boost_down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    boost_up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    boost_minutes: int = Field(default=60, ge=1, le=10_080)
    reason: str | None = Field(default=None, max_length=300)


def _modes(container: ContainerDep) -> ModesRepository:
    if container.modes_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bandwidth modes unavailable (database not initialised)",
        )
    return container.modes_repo


def _erreur(exc: Exception) -> HTTPException:
    if isinstance(exc, ModeNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ModeConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ------------------------------------------------------------------ les modes
@router.get("/bandwidth-modes", summary="Bandwidth modes a client can be put in")
async def list_modes(container: ContainerDep) -> dict[str, Any]:
    modes = await _modes(container).list()
    topo = container.topology_repo
    for mode in modes:
        mode["clients"] = len(await topo.clients_in_mode(int(mode["id"]))) if topo else 0
    return {"automatic": AUTOMATIC, "modes": modes}


@router.post(
    "/bandwidth-modes", status_code=status.HTTP_201_CREATED, summary="Create a bandwidth mode"
)
async def create_mode(payload: ModeInput, container: ContainerDep) -> dict[str, Any]:
    try:
        return await _modes(container).create(payload.model_dump())
    except (InvalidModeError, ModeConflictError) as exc:
        raise _erreur(exc) from exc


@router.patch("/bandwidth-modes/{mode_id}", summary="Edit a bandwidth mode")
async def update_mode(
    payload: ModeUpdate,
    request: Request,
    container: ContainerDep,
    metrics: RepositoryDep,
    mode_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """Modifie le mode, puis REAPPLIQUE son debit a chaque client qui l'a.

    Un mode "Backup" passe de 6 a 8 Mbps : les clients en Backup doivent
    passer a 8 tout de suite, pas au prochain geste sur chacun d'eux.
    """
    try:
        mode = await _modes(container).update(mode_id, payload.model_dump(exclude_unset=True))
    except (InvalidModeError, ModeConflictError, ModeNotFoundError) as exc:
        raise _erreur(exc) from exc
    resultats = []
    topo = _require_topology(container)
    for login in await topo.clients_in_mode(mode_id):
        resultats.append(
            await _donner_mode(container, metrics, login, mode, author=acteur(request))
        )
    return {"mode": mode, "clients": resultats}


@router.delete("/bandwidth-modes/{mode_id}", summary="Delete a bandwidth mode")
async def delete_mode(
    container: ContainerDep, mode_id: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    try:
        await _modes(container).delete(mode_id)
    except (ModeConflictError, ModeNotFoundError) as exc:
        raise _erreur(exc) from exc
    return {"deleted": mode_id}


# -------------------------------------------------------------- les clients
async def _donner_mode(
    container: Any,
    metrics: Any,
    login: str,
    mode: dict[str, Any] | None,
    *,
    author: str,
    apply_now: bool = True,
) -> dict[str, Any]:
    """Met UN client dans ce mode (None = Automatic) et pose sa file. Ne leve
    pas : le resultat dit ce qui s'est passe pour CE client."""
    topo = _require_topology(container)
    abonne = await client_connu(container, metrics, login)
    if abonne is None:
        return {"login": login, "ok": False, "detail": "unknown client"}
    if mode is None:
        await topo.clear_mode(login, updated_by=author)
        rapport: dict[str, Any] = {"login": login, "ok": True, "mode": None}
    else:
        try:
            bas, haut = mode_rates(mode, abonne.get("plan_down_mbps"), abonne.get("plan_up_mbps"))
        except InvalidModeError as exc:
            return {"login": login, "ok": False, "detail": str(exc)}
        await topo.set_mode(
            target_key=login,
            mode_id=int(mode["id"]),
            mode_name=str(mode["name"]),
            down_mbps=bas,
            up_mbps=haut,
            clear_boost=bool(mode.get("disables_boost")),
            updated_by=author,
        )
        rapport = {
            "login": login,
            "ok": True,
            "mode": {"id": mode["id"], "name": mode["name"]},
            "down_mbps": bas,
            "up_mbps": haut,
            "boost_stopped": bool(mode.get("disables_boost")),
        }
    logger.info("Mode de %s -> %s (par %s)", login, mode["name"] if mode else "Automatic", author)
    if apply_now:
        rapport["applied"] = await _apply_for_subscriber(container, login, author=author)
    return rapport


async def _mode_choisi(container: ContainerDep, mode_id: int | None) -> dict[str, Any] | None:
    if mode_id is None:
        return None
    try:
        return await _modes(container).get(mode_id)
    except ModeNotFoundError as exc:
        raise _erreur(exc) from exc


@router.put("/clients/{login}/mode", summary="Put one client in a bandwidth mode")
async def set_client_mode(
    login: str,
    payload: ModeChoice,
    request: Request,
    container: ContainerDep,
    metrics: RepositoryDep,
) -> dict[str, Any]:
    mode = await _mode_choisi(container, payload.mode_id)
    rapport = await _donner_mode(
        container, metrics, login, mode, author=acteur(request), apply_now=payload.apply_now
    )
    if not rapport["ok"]:
        code = (
            status.HTTP_404_NOT_FOUND
            if rapport.get("detail") == "unknown client"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=rapport.get("detail"))
    return rapport


@router.post("/clients/actions", summary="Change mode or boost of one or several clients")
async def client_actions(
    payload: ClientAction,
    request: Request,
    container: ContainerDep,
    metrics: RepositoryDep,
) -> dict[str, Any]:
    logins = list(dict.fromkeys(x.strip() for x in payload.logins if x.strip()))
    if not logins:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No client selected")
    user = getattr(request.state, "user", None)
    if (
        len(logins) > 1
        and user is not None
        and not role_at_least(user.get("role"), ROLE_SUPERVISOR)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Group actions are available to Supervisors and above: act on one client",
        )
    auteur = acteur(request)
    mode = await _mode_choisi(container, payload.mode_id) if payload.action == "mode" else None
    if payload.action == "boost" and not (
        payload.boost_multiplier or payload.boost_down_mbps or payload.boost_up_mbps
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A boost needs a multiplier or a rate",
        )
    resultats: list[dict[str, Any]] = []
    for login in logins:
        try:
            if payload.action == "mode":
                resultats.append(await _donner_mode(container, metrics, login, mode, author=auteur))
            elif payload.action == "boost":
                pose = await poser_boost(
                    container,
                    metrics,
                    login=login,
                    down_mbps=payload.boost_down_mbps,
                    up_mbps=payload.boost_up_mbps,
                    multiplier=payload.boost_multiplier,
                    duration_minutes=payload.boost_minutes,
                    reason=payload.reason,
                    apply_now=True,
                    author=auteur,
                )
                resultats.append({"login": login, "ok": True, **pose})
            else:
                retire = await _require_topology(container).clear_boost("subscriber", login)
                resultats.append(
                    {
                        "login": login,
                        "ok": True,
                        "stopped": retire,
                        "applied": (
                            await _apply_for_subscriber(container, login, author=auteur)
                            if retire
                            else None
                        ),
                    }
                )
        except HTTPException as exc:
            resultats.append({"login": login, "ok": False, "detail": exc.detail})
    reussis = sum(1 for r in resultats if r.get("ok"))
    return {
        "action": payload.action,
        "requested": len(logins),
        "succeeded": reussis,
        "failed": len(logins) - reussis,
        "results": resultats,
    }

"""Profils de debit horaires : jour, soiree, nuit.

Un profil dit, plage par plage, quel debit s'applique : un debit absolu, ou un
pourcentage du forfait de chaque abonne. Hors de toute plage, c'est le
forfait normal. Le changement se fait TOUT SEUL a l'heure dite.

Enregistrer un profil l'APPLIQUE aussitot (s'il y a une plage en cours), comme
un plafond saisi a la main : attendre la prochaine bascule laisserait
l'exploitant devant un profil qui dit une chose et un routeur qui en fait une
autre. ``ENFORCEMENT_ENABLED`` reste le dernier mot.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.schedules_repo import (
    ScheduleConflictError,
    ScheduleNotFoundError,
    SchedulesRepository,
)
from app.enforcement.planner import effective_rate
from app.enforcement.schedules import (
    MAX_WINDOWS,
    BandwidthSchedule,
    InvalidScheduleError,
    ScheduleBook,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bandwidth schedules"])

Scope = Literal["all", "pops", "plans", "subscribers"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class WindowInput(BaseModel):
    """Une plage horaire : de ``start`` a ``end`` (heure locale du profil).

    ``end`` avant ``start`` : la plage passe minuit (22:00 -> 06:00). ``days``
    sont les jours ou elle COMMENCE ; vide = tous les jours. Un debit absolu
    (``down_mbps`` / ``up_mbps``, un seul sens possible) OU un pourcentage du
    forfait (``percent``), pas les deux.
    """

    label: str | None = Field(default=None, max_length=64, examples=["evening"])
    start: str = Field(max_length=5, examples=["18:00"])
    end: str = Field(max_length=5, examples=["23:00"])
    days: list[Day] = Field(default_factory=list, max_length=7)
    down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    percent: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        description="Percentage of each subscriber's plan (200 = twice the plan).",
    )


class ScheduleInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    timezone: str = Field(
        default="UTC",
        max_length=64,
        description="IANA time zone of the subscribers (Europe/Paris, Africa/Abidjan...)",
    )
    scope: Scope = "all"
    targets: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description=(
            "pops: site names; plans: plan download rates in Mbps (e.g. 20); "
            "subscribers: logins or static-client references. Ignored for 'all'."
        ),
    )
    windows: list[WindowInput] = Field(min_length=1, max_length=MAX_WINDOWS)
    note: str | None = Field(default=None, max_length=1000)


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)
    scope: Scope | None = None
    targets: list[str] | None = Field(default=None, max_length=1000)
    windows: list[WindowInput] | None = Field(default=None, min_length=1, max_length=MAX_WINDOWS)
    note: str | None = Field(default=None, max_length=1000)


def _repo(container: ContainerDep) -> SchedulesRepository:
    depot = getattr(container, "schedules_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bandwidth schedules unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


def _valider(champs: dict[str, Any], *, ident: int = 0) -> dict[str, Any]:
    """Valide un profil complet et le rend sous sa forme canonique, ou 400."""
    try:
        profil = BandwidthSchedule.build(
            id=ident,
            name=str(champs["name"]),
            windows=list(champs.get("windows") or []),
            scope=str(champs.get("scope") or "all"),
            targets=list(champs.get("targets") or []),
            timezone=str(champs.get("timezone") or "UTC"),
            enabled=bool(champs.get("enabled", True)),
        )
    except InvalidScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    canonique = profil.to_dict()
    canonique.pop("id")
    canonique["note"] = champs.get("note")
    return canonique


def _etat(ligne: dict[str, Any], maintenant: datetime) -> dict[str, Any]:
    """Une ligne de profil, avec ce qui court maintenant et la prochaine bascule."""
    sortie = dict(ligne)
    try:
        profil = BandwidthSchedule.build(
            id=int(ligne["id"]),
            name=str(ligne["name"]),
            windows=list(ligne.get("windows") or []),
            scope=str(ligne.get("scope") or "all"),
            targets=list(ligne.get("targets") or []),
            timezone=str(ligne.get("timezone") or "UTC"),
            enabled=bool(ligne.get("enabled", True)),
        )
    except InvalidScheduleError as exc:
        sortie["error"] = str(exc)
        return sortie
    plage = profil.window_at(maintenant)
    prochaine = profil.next_change(maintenant)
    sortie["local_time"] = profil.local(maintenant).strftime("%a %H:%M")
    sortie["active_window"] = plage.to_dict() if plage else None
    sortie["next_change_at"] = prochaine.isoformat() if prochaine else None
    sortie["next_change_local"] = (
        profil.local(prochaine).strftime("%a %H:%M") if prochaine else None
    )
    return sortie


async def _appliquer(container: ContainerDep, motif: str) -> dict[str, Any]:
    """Applique tout de suite. Ne fait JAMAIS echouer la saisie : le profil est
    deja enregistre, un routeur muet se raconte dans la reponse."""
    try:
        return await container.shaping.apply_schedules(force=True, reason=motif)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Application des profils horaires impossible")
        return {"applied": 0, "errors": [f"{type(exc).__name__}: {exc}"]}


@router.get("/bandwidth-schedules", summary="Time-of-day bandwidth schedules")
async def list_schedules(container: ContainerDep) -> dict[str, Any]:
    """Les profils, la plage en cours de chacun et sa prochaine bascule."""
    lignes = await _repo(container).list_all()
    maintenant = datetime.now(tz=UTC)
    return {
        "enforcement_enabled": container.shaping.enforcement_enabled,
        "schedules": [_etat(ligne, maintenant) for ligne in lignes],
        "last_switch": getattr(container.shaping, "last_schedule_switch", None),
    }


@router.post(
    "/bandwidth-schedules",
    status_code=status.HTTP_201_CREATED,
    summary="Create a bandwidth schedule (applied right away)",
)
async def create_schedule(payload: ScheduleInput, container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    canonique = _valider(payload.model_dump())
    try:
        cree = await depot.create(canonique)
    except ScheduleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("Profil horaire '%s' cree", cree["name"])
    sortie = _etat(cree, datetime.now(tz=UTC))
    sortie["enforcement"] = await _appliquer(container, f"schedule '{cree['name']}' created")
    return sortie


@router.patch("/bandwidth-schedules/{schedule_id}", summary="Edit a bandwidth schedule")
async def update_schedule(
    payload: ScheduleUpdate,
    container: ContainerDep,
    schedule_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    depot = _repo(container)
    try:
        existant = await depot.get(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    modifs = payload.model_dump(exclude_unset=True)
    canonique = _valider({**existant, **modifs}, ident=schedule_id)
    try:
        modifie = await depot.update(schedule_id, canonique)
    except ScheduleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    sortie = _etat(modifie, datetime.now(tz=UTC))
    sortie["enforcement"] = await _appliquer(container, f"schedule '{modifie['name']}' edited")
    return sortie


@router.delete("/bandwidth-schedules/{schedule_id}", summary="Delete a bandwidth schedule")
async def delete_schedule(
    container: ContainerDep,
    schedule_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """Supprime le profil ; ses abonnes retrouvent leur forfait tout de suite."""
    depot = _repo(container)
    try:
        existant = await depot.get(schedule_id)
        await depot.delete(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "deleted": schedule_id,
        "enforcement": await _appliquer(container, f"schedule '{existant['name']}' deleted"),
    }


@router.get(
    "/bandwidth-schedules/{schedule_id}/preview",
    summary="Which subscribers this schedule governs, and their rate right now",
)
async def preview_schedule(
    container: ContainerDep,
    schedule_id: Annotated[int, Path(ge=1)],
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    """Repond a « qui est concerne, et a quel debit en ce moment ? ».

    Le calcul est celui du planificateur : meme carnet de profils, meme regle
    de priorite (boost > surcharge > horaire > forfait). Un abonne vise par un
    profil PLUS PRECIS est rendu a part : ce profil-ci ne le gouverne pas.
    """
    depot = _repo(container)
    try:
        ligne = await depot.get(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    maintenant = datetime.now(tz=UTC)
    etat = _etat(ligne, maintenant)
    if "error" in etat:
        return {"schedule": etat, "subscribers": [], "overridden": [], "counts": {}}
    profil = BandwidthSchedule.build(
        id=int(ligne["id"]),
        name=str(ligne["name"]),
        windows=list(ligne.get("windows") or []),
        scope=str(ligne.get("scope") or "all"),
        targets=list(ligne.get("targets") or []),
        timezone=str(ligne.get("timezone") or "UTC"),
        enabled=True,
    )
    # Le carnet REEL (les autres profils comptent pour la priorite), ce profil
    # pris tel qu'enregistre meme s'il est suspendu : c'est un apercu.
    carnet = ScheduleBook(
        [p for p in container.shaping.schedule_book.schedules if p.id != profil.id] + [profil]
    )
    abonnes = await container.repository.subscriber_latest(
        limit=5000, order_by="login", include_unmeasured=True
    )
    gouvernes: list[dict[str, Any]] = []
    ailleurs: list[dict[str, Any]] = []
    for abonne in abonnes:
        login = str(abonne.get("login") or "")
        sites = (abonne.get("pop_name"),)
        plan_bas, plan_haut = abonne.get("plan_down_mbps"), abonne.get("plan_up_mbps")
        if not profil.governs(login=login, sites=sites, plan_down=plan_bas):
            continue
        maitre = carnet.governing(login=login, sites=sites, plan_down=plan_bas)
        if maitre is not None and maitre.id != profil.id:
            ailleurs.append({"login": login, "governed_by": maitre.name})
            continue
        horaire = carnet.rate_for(
            login=login, sites=sites, plan_down=plan_bas, plan_up=plan_haut, now=maintenant
        )
        bas, source = effective_rate(
            plan_mbps=plan_bas,
            override_mbps=abonne.get("override_down_mbps"),
            boost_mbps=abonne.get("boost_down_mbps"),
            boost_expires_at=abonne.get("boost_expires_at"),
            schedule_mbps=horaire.down_mbps if horaire else None,
        )
        haut, _ = effective_rate(
            plan_mbps=plan_haut,
            override_mbps=abonne.get("override_up_mbps"),
            boost_mbps=abonne.get("boost_up_mbps"),
            boost_expires_at=abonne.get("boost_expires_at"),
            schedule_mbps=horaire.up_mbps if horaire else None,
        )
        gouvernes.append(
            {
                "login": login,
                "kind": abonne.get("kind"),
                "pop_name": abonne.get("pop_name"),
                "plan_down_mbps": plan_bas,
                "plan_up_mbps": plan_haut,
                "now_down_mbps": bas,
                "now_up_mbps": haut,
                "source": source,
            }
        )
    return {
        "schedule": etat,
        "counts": {
            "governed": len(gouvernes),
            "overridden": len(ailleurs),
            "on_schedule_now": sum(1 for g in gouvernes if g["source"] == "schedule"),
        },
        "subscribers": gouvernes[:limit],
        "overridden": ailleurs[:limit],
    }

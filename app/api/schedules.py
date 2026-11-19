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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.schedules_repo import (
    ScheduleConflictError,
    ScheduleNotFoundError,
    SchedulesRepository,
    to_model,
)
from app.enforcement.planner import effective_rate
from app.enforcement.schedules import (
    MAX_WINDOWS,
    BandwidthSchedule,
    InvalidScheduleError,
    Modes,
    ScheduleBook,
    _memes_cibles_possibles,
    overlap_conflict,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bandwidth schedules"])

Scope = Literal["all", "pops", "plans", "subscribers", "modes", "on_plan"]
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
    mode_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "A bandwidth mode (GET /bandwidth-modes) instead of a rate: the slot gives "
            "the mode's rate, and follows the mode when it is edited."
        ),
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
            "subscribers: logins or static-client references; modes: mode ids (the "
            "clients in these modes); pops: site names; plans: plan download rates in "
            "Mbps (e.g. 20). Ignored for 'all' and 'on_plan' (the clients without a mode)."
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


def _valider(
    champs: dict[str, Any], modes: Modes | None = None, *, ident: int = 0
) -> dict[str, Any]:
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
            modes=modes,
        )
    except InvalidScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    canonique = profil.to_dict()
    canonique.pop("id")
    # Lance / arrete a la main : seulement par /start et /stop.
    canonique.pop("manual_on", None)
    canonique.pop("manual_until", None)
    canonique["note"] = champs.get("note")
    return canonique


async def _verifier_chevauchements(
    container: Any, canonique: dict[str, Any], modes: Modes, *, ident: int = 0
) -> None:
    """409 si ce scheduler touche les memes clients qu'un autre aux memes heures."""
    if not canonique.get("enabled", True):
        return
    profil = BandwidthSchedule.build(
        id=ident,
        name=str(canonique["name"]),
        windows=list(canonique.get("windows") or []),
        scope=str(canonique.get("scope") or "all"),
        targets=list(canonique.get("targets") or []),
        timezone=str(canonique.get("timezone") or "UTC"),
        modes=modes,
    )
    autres = []
    for ligne in await _repo(container).list_all():
        if int(ligne["id"]) == ident or not ligne.get("enabled", True):
            continue
        try:
            autres.append(to_model(ligne, modes))
        except (InvalidScheduleError, KeyError, TypeError, ValueError):
            continue
    if not autres:
        return
    clients: list[dict[str, Any]] = []
    depot = getattr(container, "repository", None)
    if depot is not None:
        clients = await depot.subscriber_latest(
            limit=5000, order_by="login", include_unmeasured=True
        )
    noms: dict[str, str] = {}
    profils = getattr(container, "client_profiles_repo", None)
    if profils is not None:
        try:
            noms = {
                str(p["login"]): str(p.get("display_name"))
                for p in await profils.list_all()
                if p.get("display_name")
            }
        except Exception:  # noqa: BLE001 - les noms ne servent qu'au message
            noms = {}
    message = overlap_conflict(
        profil,
        autres,
        clients,
        describe_client=lambda c: noms.get(str(c.get("login"))) or c.get("login"),
    )
    if message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _etat(
    ligne: dict[str, Any], maintenant: datetime, modes: Modes | None = None
) -> dict[str, Any]:
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
            modes=modes,
            manual_on=bool(ligne.get("manual_on", False)),
            manual_until=ligne.get("manual_until"),
        )
    except InvalidScheduleError as exc:
        sortie["error"] = str(exc)
        return sortie
    sortie["personal"] = profil.personal
    sortie["manual_active"] = profil.manual_active(maintenant)
    sortie["manual_until"] = (
        profil.manual_until.isoformat()
        if profil.manual_active(maintenant) and profil.manual_until
        else None
    )
    # Les plages telles qu'elles s'appliquent : le nom d'un mode renomme depuis
    # est celui d'aujourd'hui.
    sortie["windows"] = [w.to_dict() for w in profil.windows]
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
    depot = _repo(container)
    lignes = await depot.list_all()
    modes = await depot.modes()
    maintenant = datetime.now(tz=UTC)
    return {
        "enforcement_enabled": container.shaping.enforcement_enabled,
        "schedules": [_etat(ligne, maintenant, modes) for ligne in lignes],
        "last_switch": getattr(container.shaping, "last_schedule_switch", None),
    }


@router.post(
    "/bandwidth-schedules",
    status_code=status.HTTP_201_CREATED,
    summary="Create a bandwidth schedule (applied right away)",
)
async def create_schedule(payload: ScheduleInput, container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    modes = await depot.modes()
    canonique = _valider(payload.model_dump(), modes)
    await _verifier_chevauchements(container, canonique, modes)
    try:
        cree = await depot.create(canonique)
    except ScheduleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("Profil horaire '%s' cree", cree["name"])
    sortie = _etat(cree, datetime.now(tz=UTC), modes)
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
    modes = await depot.modes()
    canonique = _valider({**existant, **modifs}, modes, ident=schedule_id)
    await _verifier_chevauchements(container, canonique, modes, ident=schedule_id)
    try:
        modifie = await depot.update(schedule_id, canonique)
    except ScheduleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    sortie = _etat(modifie, datetime.now(tz=UTC), modes)
    sortie["enforcement"] = await _appliquer(container, f"schedule '{modifie['name']}' edited")
    return sortie


class StartInput(BaseModel):
    minutes: int | None = Field(
        default=None,
        ge=1,
        le=60 * 24 * 31,
        description="How long it runs. null: until it is stopped.",
    )
    until_scheduled_end: bool = Field(
        default=False,
        description=(
            "Start now, before its time, and stop at the end time it was scheduled "
            "for (the window running now, or else the next one). Then back to normal."
        ),
    )


@router.post(
    "/bandwidth-schedules/{schedule_id}/start",
    summary="Start a scheduler now, by hand (whatever the time)",
)
async def start_schedule(
    payload: StartInput,
    container: ContainerDep,
    schedule_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """Applique ce scheduler TOUT DE SUITE, quelle que soit l'heure : pour
    ``minutes``, ou jusqu'a ``/stop``. Il passe devant les autres schedulers de
    ses clients tant qu'il tourne. Un scheduler suspendu est repris."""
    depot = _repo(container)
    try:
        existant = await depot.get(schedule_id)
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    fin = datetime.now(tz=UTC) + timedelta(minutes=payload.minutes) if payload.minutes else None
    # UN SEUL LANCE A LA MAIN PAR CLIENT : ceux qui touchent les memes clients
    # s'arretent (le dernier geste l'emporte, et la page le dit).
    modes = await depot.modes()
    if payload.until_scheduled_end:
        try:
            profil = to_model({**existant, "enabled": True, "manual_on": False}, modes)
        except (InvalidScheduleError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        fin = profil.scheduled_end(datetime.now(tz=UTC))
        if fin is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"'{existant['name']}' has no end time in the coming week: "
                    "choose a duration instead."
                ),
            )
    arretes = await _arreter_les_autres_lances(container, existant, modes)
    modifie = await depot.update(
        schedule_id, {"manual_on": True, "manual_until": fin, "enabled": True}
    )
    sortie = _etat(modifie, datetime.now(tz=UTC), modes)
    sortie["stopped"] = arretes
    sortie["enforcement"] = await _appliquer(
        container, f"schedule '{existant['name']}' started by hand"
    )
    return sortie


async def _arreter_les_autres_lances(
    container: Any, ligne: dict[str, Any], modes: Modes
) -> list[str]:
    """Arrete les schedulers lances a la main qui touchent les memes clients."""
    depot = _repo(container)
    maintenant = datetime.now(tz=UTC)
    try:
        profil = to_model({**ligne, "enabled": True}, modes)
    except (InvalidScheduleError, KeyError, TypeError, ValueError):
        return []
    clients: list[dict[str, Any]] = []
    metriques = getattr(container, "repository", None)
    if metriques is not None:
        clients = await metriques.subscriber_latest(
            limit=5000, order_by="login", include_unmeasured=True
        )

    def vise(p: BandwidthSchedule, c: dict[str, Any]) -> bool:
        return p.governs(
            login=str(c.get("login")),
            sites=(c.get("pop_name"),),
            plan_down=c.get("plan_down_mbps"),
            mode_id=c.get("mode_id"),
        )

    arretes: list[str] = []
    for autre in await depot.list_all():
        if int(autre["id"]) == int(ligne["id"]) or not autre.get("manual_on"):
            continue
        try:
            modele = to_model({**autre, "enabled": True}, modes)
        except (InvalidScheduleError, KeyError, TypeError, ValueError):
            continue
        if not modele.manual_active(maintenant):
            continue
        if profil.personal != modele.personal:
            continue  # le propre au client et le groupe ne s'arretent pas l'un l'autre
        if _memes_cibles_possibles(profil, modele) or any(
            vise(profil, c) and vise(modele, c) for c in clients
        ):
            await depot.update(int(autre["id"]), {"manual_on": False, "manual_until": None})
            arretes.append(str(autre["name"]))
    return arretes


@router.post("/bandwidth-schedules/{schedule_id}/stop", summary="Stop a scheduler started by hand")
async def stop_schedule(
    container: ContainerDep, schedule_id: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    """Il reprend ses horaires normaux."""
    depot = _repo(container)
    try:
        modifie = await depot.update(schedule_id, {"manual_on": False, "manual_until": None})
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    sortie = _etat(modifie, datetime.now(tz=UTC), await depot.modes())
    sortie["enforcement"] = await _appliquer(
        container, f"schedule '{modifie['name']}' stopped by hand"
    )
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
    modes = await depot.modes()
    etat = _etat(ligne, maintenant, modes)
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
        modes=modes,
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
        mode = abonne.get("mode_id")
        if not profil.governs(login=login, sites=sites, plan_down=plan_bas, mode_id=mode):
            continue
        maitre = carnet.governing(
            login=login, sites=sites, plan_down=plan_bas, mode_id=mode, now=maintenant
        )
        if maitre is not None and maitre.id != profil.id:
            ailleurs.append({"login": login, "governed_by": maitre.name})
            continue
        horaire = carnet.rate_for(
            login=login,
            sites=sites,
            plan_down=plan_bas,
            plan_up=plan_haut,
            now=maintenant,
            mode_id=mode,
            mode_down=abonne.get("override_down_mbps"),
            mode_up=abonne.get("override_up_mbps"),
        )
        devant = bool(horaire and horaire.over_mode)
        bas, source = effective_rate(
            plan_mbps=plan_bas,
            override_mbps=abonne.get("override_down_mbps"),
            boost_mbps=abonne.get("boost_down_mbps"),
            boost_expires_at=abonne.get("boost_expires_at"),
            schedule_mbps=horaire.down_mbps if horaire else None,
            schedule_over_override=devant,
        )
        haut, _ = effective_rate(
            plan_mbps=plan_haut,
            override_mbps=abonne.get("override_up_mbps"),
            boost_mbps=abonne.get("boost_up_mbps"),
            boost_expires_at=abonne.get("boost_expires_at"),
            schedule_mbps=horaire.up_mbps if horaire else None,
            schedule_over_override=devant,
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

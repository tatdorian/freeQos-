"""Page Plans : le plan de CHAQUE client, d'ou il vient, et le changer.

Le plan appartient au client -- un PoP n'est qu'un point de connexion. Il est
pousse par la facturation (API Preseem, plusieurs fois par jour s'il le faut)
ou saisi ici ; la derniere ecriture gagne. Sans plan, un client (y compris un
nouveau client detecte) recoit le plan par defaut.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.accounts import UserDep
from app.api.deps import ContainerDep
from app.models import KIND_STATIC
from app.services.plans import apply_now, default_plan

router = APIRouter(tags=["plans"])

ORIGINE_API = "api"
ORIGINE_MANUELLE = "ui"
ORIGINE_DEFAUT = "default"


class PlanInput(BaseModel):
    down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    #: Un forfait pousse par l'API (``/model/v1/packages``) : ses debits font foi.
    package_id: str | None = Field(default=None, max_length=128)


def _repos(container: ContainerDep) -> tuple[Any, Any]:
    if container.repository is None or container.client_plans_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plans unavailable (database not initialised)",
        )
    return container.repository, container.client_plans_repo


async def _packages(container: ContainerDep) -> list[dict[str, Any]]:
    modele = getattr(container, "model_repo", None)
    if modele is None:
        return []
    try:
        lignes: list[dict[str, Any]] = await modele.list_objects("packages")
    except Exception:  # noqa: BLE001 - la page reste utile sans forfaits
        return []
    return [
        {
            "id": p["id"],
            "name": p.get("name"),
            "down_mbps": (p["down_speed"] / 1000) if p.get("down_speed") else None,
            "up_mbps": (p["up_speed"] / 1000) if p.get("up_speed") else None,
        }
        for p in lignes
    ]


def _last(*dates: Any) -> Any:
    valides = [d for d in dates if isinstance(d, datetime)]
    return max(valides) if valides else None


async def _roster(container: ContainerDep) -> list[dict[str, Any]]:
    depot, plans = _repos(container)
    lignes = await depot.plan_roster()
    ecrits = await plans.list_all()
    statiques: dict[str, dict[str, Any]] = {}
    if container.static_clients_repo is not None:
        for fs in await container.static_clients_repo.list_all():
            statiques[str(fs["reference"])] = fs
    defaut = default_plan(container.settings)
    forcees = await _limites_forcees(container)
    sortie: list[dict[str, Any]] = []
    for ligne in lignes:
        login = str(ligne["login"])
        client: dict[str, Any] = {
            "subscriber_id": ligne["subscriber_id"],
            "login": login,
            "kind": ligne["kind"],
            "pop_name": ligne["pop_name"],
            "address": ligne["address"],
            "last_seen": ligne["last_seen"],
            "down_mbps": ligne["plan_down_mbps"],
            "up_mbps": ligne["plan_up_mbps"],
            "origin": ORIGINE_DEFAUT,
            "service_id": None,
            "package_id": None,
            "updated_at": None,
            "updated_by": None,
        }
        fiche: dict[str, Any] | None = (
            statiques.get(login) if ligne["kind"] == KIND_STATIC else None
        )
        ecrit = ecrits.get(login)
        if fiche is not None:
            a_un_plan = fiche.get("plan_down_mbps") is not None or fiche.get("plan_up_mbps")
            client["down_mbps"] = fiche.get("plan_down_mbps")
            client["up_mbps"] = fiche.get("plan_up_mbps")
            if a_un_plan:
                client["origin"] = ORIGINE_API if fiche.get("source") == "api" else ORIGINE_MANUELLE
                client["service_id"] = login if fiche.get("source") == "api" else None
                client["package_id"] = fiche.get("package_ref")
                client["updated_at"] = fiche.get("updated_at")
        elif ecrit is not None:
            client["origin"] = str(ecrit["source"])
            client["service_id"] = ecrit.get("service_id")
            client["package_id"] = ecrit.get("package_id")
            client["updated_at"] = ecrit.get("updated_at")
            client["updated_by"] = ecrit.get("updated_by")
            if ecrit.get("down_mbps") is not None or ecrit.get("up_mbps") is not None:
                client["down_mbps"] = ecrit.get("down_mbps")
                client["up_mbps"] = ecrit.get("up_mbps")
        if client["origin"] == ORIGINE_DEFAUT and defaut is not None:
            # Le plan reellement applique : celui par defaut.
            client["down_mbps"] = defaut.down_mbps
            client["up_mbps"] = defaut.up_mbps
        client["updated_at"] = _last(client["updated_at"])
        # LA LIMITE FORCEE l'emporte sur le plan (bouton Rate de Subscribers) :
        # la page Plans affichait 100/20 « Default » pour un client bride a la
        # main a 300k/750k. Elle dit maintenant ce qui est REELLEMENT applique.
        force = forcees.get(login)
        client["forced_down_mbps"] = force[0] if force else None
        client["forced_up_mbps"] = force[1] if force else None
        sortie.append(client)
    return sortie


async def _limites_forcees(container: ContainerDep) -> dict[str, tuple[Any, Any]]:
    """login -> (down, up) des limites posees a la main (Subscribers > Rate)."""
    topo = getattr(container, "topology_repo", None)
    if topo is None:
        return {}
    try:
        lignes = await topo.policies("subscriber")
    except Exception:  # noqa: BLE001 - la liste des plans ne doit pas tomber pour ca
        return {}
    return {
        str(p["target_key"]): (p.get("max_down_mbps"), p.get("max_up_mbps"))
        for p in lignes
        if p.get("enabled", True)
        and (p.get("max_down_mbps") is not None or p.get("max_up_mbps") is not None)
    }


async def _lever_limite_forcee(container: ContainerDep, login: str) -> bool:
    """Retire la limite forcee d'un client : son PLAN redevient la regle.

    Changer un plan sans cela n'avait aucun effet visible -- la limite posee
    dans Subscribers continuait de primer, et l'on croyait le plan ignore."""
    topo = getattr(container, "topology_repo", None)
    if topo is None:
        return False
    try:
        return bool(await topo.delete_policy("subscriber", login))
    except Exception:  # noqa: BLE001
        return False


@router.get("/plans", summary="The plan of every client, and where it comes from")
async def list_plans(container: ContainerDep) -> dict[str, Any]:
    clients = await _roster(container)
    defaut = default_plan(container.settings)
    return {
        "default": {
            "down_mbps": defaut.down_mbps if defaut else None,
            "up_mbps": defaut.up_mbps if defaut else None,
        },
        "provider": container.settings.plan_provider,
        "packages": await _packages(container),
        "summary": {
            "clients": len(clients),
            "api": sum(1 for c in clients if c["origin"] == ORIGINE_API),
            "manual": sum(1 for c in clients if c["origin"] == ORIGINE_MANUELLE),
            "default": sum(1 for c in clients if c["origin"] == ORIGINE_DEFAUT),
        },
        "clients": clients,
    }


async def _client(container: ContainerDep, login: str) -> dict[str, Any]:
    for client in await _roster(container):
        if client["login"] == login:
            return client
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown client: {login}")


@router.put("/plans/{login}", summary="Set the plan of a client")
async def set_plan(
    login: str, payload: PlanInput, container: ContainerDep, user: UserDep
) -> dict[str, Any]:
    client = await _client(container, login)
    _depot, plans = _repos(container)
    bas, haut, forfait = payload.down_mbps, payload.up_mbps, payload.package_id
    if forfait:
        paquet = next((p for p in await _packages(container) if p["id"] == forfait), None)
        if paquet is None:
            raise HTTPException(status_code=404, detail=f"unknown package: {forfait}")
        bas, haut = paquet["down_mbps"], paquet["up_mbps"]
    if bas is None and haut is None:
        raise HTTPException(status_code=422, detail="a rate or a package is required")
    auteur = f"ui:{user.get('email')}"
    # Un plan choisi ici est LA regle : la limite forcee ailleurs est levee.
    leve = await _lever_limite_forcee(container, login)
    if client["kind"] == KIND_STATIC and container.static_clients_repo is not None:
        await container.static_clients_repo.set_plan(login, bas, haut)
        pose: dict[str, Any] | None = await _pose_statique(container, login, auteur)
    else:
        await plans.set(
            login,
            down_mbps=bas,
            up_mbps=haut,
            source=ORIGINE_MANUELLE,
            package_id=forfait,
            updated_by=str(user.get("email")),
        )
        pose = await apply_now(container, login, author=auteur)
    return {
        "client": await _client(container, login),
        "enforcement": pose,
        "forced_limit_lifted": leve,
    }


@router.delete("/plans/{login}", summary="Put a client back on the default plan")
async def reset_plan(login: str, container: ContainerDep, user: UserDep) -> dict[str, Any]:
    client = await _client(container, login)
    _depot, plans = _repos(container)
    auteur = f"ui:{user.get('email')}"
    leve = await _lever_limite_forcee(container, login)
    pose: dict[str, Any] | None
    if client["kind"] == KIND_STATIC and container.static_clients_repo is not None:
        await container.static_clients_repo.set_plan(login, None, None)
        pose = await _pose_statique(container, login, auteur)
    else:
        await plans.delete(login)
        pose = await apply_now(container, login, author=auteur)
    return {
        "client": await _client(container, login),
        "enforcement": pose,
        "forced_limit_lifted": leve,
    }


async def _pose_statique(container: ContainerDep, reference: str, auteur: str) -> dict[str, Any]:
    depot = container.static_clients_repo
    fiches = await depot.list_all() if depot is not None else []
    fiche = next((f for f in fiches if f["reference"] == reference), {})
    try:
        rapport: dict[str, Any] = await container.shaping.enforce_static_client(
            reference=reference,
            pop_name=str(fiche.get("pop_name") or ""),
            author=auteur,
            address=str(fiche.get("address") or "") or None,
        )
        return rapport
    except Exception as exc:  # noqa: BLE001 - le plan est enregistre quoi qu'il arrive
        return {"state": "erreur", "reason": f"{type(exc).__name__}: {exc}"}


@router.post("/plans/refresh", summary="Apply the plans now (after a default-plan change)")
async def refresh_plans(container: ContainerDep) -> dict[str, Any]:
    """Relit les plans de tous les clients puis repose les files, sans attendre
    les cycles (5 min pour les plans, 2 min pour les files)."""
    from app.services.collection import JOB_PLANS, JOB_RECONCILE

    for job in (JOB_PLANS, JOB_RECONCILE):
        try:
            await container.scheduler.run_once(job)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{job}: {type(exc).__name__}: {exc}"}
    return {"ok": True}

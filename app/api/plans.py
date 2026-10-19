"""Page Plans : le plan de CHAQUE client, d'ou il vient, et le changer.

Le plan appartient au client -- un PoP n'est qu'un point de connexion. Il est
pousse par la facturation (API Preseem, plusieurs fois par jour s'il le faut)
ou saisi ici ; la derniere ecriture gagne. Sans plan, un client (y compris un
nouveau client detecte) recoit le plan par defaut.

UN FORFAIT PEUT PRECEDER L'ABONNE. L'interface de gestion de l'operateur, celle
qui alimente le RADIUS, cree l'abonne et son forfait AVANT sa premiere
connexion PPPoE, et une synchronisation de depart pousse aussi les abonnes hors
ligne a ce moment-la. Refuser ces forfaits ("unknown client") obligeait
l'integration a reessayer plus tard, sans savoir quand. Le forfait d'un
identifiant encore jamais vu est donc GARDE, liste a part ("en attente de
premiere connexion"), et repris tout seul quand la session apparait : le
fournisseur de plans lit la meme table pour tout identifiant nouveau.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.api.accounts import UserDep
from app.api.deps import ContainerDep
from app.enforcement.schedules import BOOK as SCHEDULE_BOOK
from app.models import KIND_STATIC
from app.services.plans import apply_now, default_plan

router = APIRouter(tags=["plans"])

ORIGINE_API = "api"
ORIGINE_MANUELLE = "ui"
ORIGINE_DEFAUT = "default"

#: Le forfait est enregistre, l'abonne n'a encore jamais ete vu connecte.
ETAT_EN_ATTENTE = "en-attente-de-connexion"
MOTIF_EN_ATTENTE = (
    "no session seen yet for this login: the plan is kept and applied as soon as "
    "the subscriber connects (at the latest at the next reconciliation pass)"
)

#: L'identifiant tel que le RADIUS le connait (login PPPoE) ou la reference
#: d'un client declare. Borne et sans espace en bord : un identifiant mal
#: copie ne doit pas creer un forfait qui n'attendra jamais personne.
Login = Annotated[str, Path(min_length=1, max_length=128)]


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
        # LE PROFIL HORAIRE qui gouverne ce client, et son debit s'il est dans
        # une plage en ce moment (soiree, nuit...). Meme carnet que le
        # planificateur : la page ne peut pas dire autre chose que le routeur.
        sites = (client["pop_name"],)
        profil = SCHEDULE_BOOK.governing(login=login, sites=sites, plan_down=client["down_mbps"])
        horaire = SCHEDULE_BOOK.rate_for(
            login=login, sites=sites, plan_down=client["down_mbps"], plan_up=client["up_mbps"]
        )
        client["schedule"] = profil.name if profil else None
        client["schedule_down_mbps"] = horaire.down_mbps if horaire else None
        client["schedule_up_mbps"] = horaire.up_mbps if horaire else None
        client["schedule_window"] = horaire.window if horaire else None
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


async def _en_attente(
    container: ContainerDep, clients: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Les forfaits ecrits pour des identifiants encore jamais vus connectes.

    Montres a part : un identifiant mal saisi dans l'interface de gestion y
    attendrait pour toujours, et c'est ici qu'on le voit.
    """
    _depot, plans = _repos(container)
    if clients is None:
        clients = await _roster(container)
    connus = {str(c["login"]) for c in clients}
    statiques: set[str] = set()
    if container.static_clients_repo is not None:
        statiques = {str(f["reference"]) for f in await container.static_clients_repo.list_all()}
    sortie: list[dict[str, Any]] = []
    for login, ligne in sorted((await plans.list_all()).items()):
        if login in connus or login in statiques:
            continue
        sortie.append(
            {
                "login": login,
                "down_mbps": ligne.get("down_mbps"),
                "up_mbps": ligne.get("up_mbps"),
                "package_id": ligne.get("package_id"),
                "origin": str(ligne.get("source") or ORIGINE_MANUELLE),
                "service_id": ligne.get("service_id"),
                "updated_at": ligne.get("updated_at"),
                "updated_by": ligne.get("updated_by"),
            }
        )
    return sortie


@router.get("/plans", summary="The plan of every client, and where it comes from")
async def list_plans(container: ContainerDep) -> dict[str, Any]:
    clients = await _roster(container)
    attente = await _en_attente(container, clients)
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
            "pending": len(attente),
        },
        "clients": clients,
        # Forfaits deja recus pour des abonnes jamais vus connectes.
        "pending": attente,
    }


async def _client_connu(container: ContainerDep, login: str) -> dict[str, Any] | None:
    for client in await _roster(container):
        if client["login"] == login:
            return client
    return None


async def _client(container: ContainerDep, login: str) -> dict[str, Any]:
    client = await _client_connu(container, login)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown client: {login}"
        )
    return client


async def _fiche_statique(container: ContainerDep, reference: str) -> dict[str, Any] | None:
    """Un client declare dont la fiche existe deja, meme pas encore collecte."""
    depot = container.static_clients_repo
    if depot is None:
        return None
    return next((f for f in await depot.list_all() if str(f["reference"]) == reference), None)


#: Avec PLAN_PROVIDER=freeradius_sql, le forfait d'un abonne qui se connecte
#: pour la premiere fois vient du RADIUS, pas de la table des forfaits.
MOTIF_RADIUS = (
    "plans are read from RADIUS (PLAN_PROVIDER=freeradius_sql): the plan of a login never "
    "seen connected cannot be kept here, it would not be applied. Set it in RADIUS: it is "
    "read at the subscriber's first connection."
)


def _forfaits_lus_dans_radius(container: ContainerDep) -> bool:
    return str(getattr(container.settings, "plan_provider", "")) == "freeradius_sql"


def _valider_login(login: str) -> None:
    if login != login.strip() or any(ord(c) < 32 for c in login):
        raise HTTPException(
            status_code=422,
            detail="invalid login: no leading or trailing spaces, no control characters",
        )


@router.put("/plans/{login}", summary="Set the plan of a client (even before its first connection)")
async def set_plan(
    login: Login, payload: PlanInput, container: ContainerDep, user: UserDep
) -> dict[str, Any]:
    """Pose le forfait d'un client, et l'applique tout de suite s'il est connecte.

    Un identifiant encore jamais vu (abonne cree dans l'interface de gestion,
    pas encore connecte) n'est PAS refuse : son forfait est garde, et repris a
    sa premiere connexion. La reponse le dit (``pending``).
    """
    _valider_login(login)
    client = await _client_connu(container, login)
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
    statique = None if client is not None else await _fiche_statique(container, login)
    if client is None and statique is None and _forfaits_lus_dans_radius(container):
        # Le forfait d'un NOUVEL abonne est lu dans le RADIUS a sa premiere
        # connexion : garder celui-ci ici serait promettre ce qui n'arrivera
        # pas. Refuser, et dire ou le poser.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MOTIF_RADIUS)
    # Un plan choisi ici est LA regle : la limite forcee ailleurs est levee.
    leve = await _lever_limite_forcee(container, login)
    if client is None and statique is None:
        # JAMAIS VU CONNECTE : le forfait attend l'abonne. Rien a poser sur un
        # routeur tant qu'aucune session ne porte cet identifiant.
        ligne = await plans.set(
            login,
            down_mbps=bas,
            up_mbps=haut,
            source=ORIGINE_MANUELLE,
            package_id=forfait,
            updated_by=str(user.get("email")),
        )
        return {
            "client": {
                "login": login,
                "down_mbps": ligne.get("down_mbps"),
                "up_mbps": ligne.get("up_mbps"),
                "package_id": ligne.get("package_id"),
                "updated_at": ligne.get("updated_at"),
            },
            "pending": True,
            "enforcement": {"state": ETAT_EN_ATTENTE, "reason": MOTIF_EN_ATTENTE},
            "forced_limit_lifted": leve,
        }
    if (
        statique is not None or (client is not None and client["kind"] == KIND_STATIC)
    ) and container.static_clients_repo is not None:
        await container.static_clients_repo.set_plan(login, bas, haut)
        pose: dict[str, Any] | None = await _pose_statique(container, login, auteur)
        if client is None:
            # Fiche declaree, pas encore collectee : elle apparaitra au cycle
            # suivant, avec ce forfait.
            return {
                "client": {"login": login, "kind": KIND_STATIC, "down_mbps": bas, "up_mbps": haut},
                "pending": False,
                "enforcement": pose,
                "forced_limit_lifted": leve,
            }
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
        "pending": False,
        "enforcement": pose,
        "forced_limit_lifted": leve,
    }


@router.delete("/plans/{login}", summary="Remove a client's plan (observed, not throttled)")
async def reset_plan(login: Login, container: ContainerDep, user: UserDep) -> dict[str, Any]:
    """Retire le forfait. Celui d'un abonne jamais connecte est simplement oublie."""
    client = await _client_connu(container, login)
    _depot, plans = _repos(container)
    if client is None and await _fiche_statique(container, login) is None:
        if not await plans.delete(login):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown client: {login}"
            )
        return {
            "client": None,
            "pending": True,
            "enforcement": {"state": "retire", "reason": "pending plan forgotten"},
            "forced_limit_lifted": await _lever_limite_forcee(container, login),
        }
    if client is None:
        # Fiche statique pas encore collectee : son forfait vit dans la fiche.
        client = {"login": login, "kind": KIND_STATIC}
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
        "client": await _client_connu(container, login),
        "pending": False,
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

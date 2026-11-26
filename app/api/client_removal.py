"""Retirer des clients de freeQoS, et les retablir.

On garde dans freeQoS les clients que l'on veut ; les autres sont RETIRES DE
FREEQOS, pas du routeur. Sur le routeur, rien du client n'est touche : ni son
compte PPPoE, ni sa session, ni son adresse. freeQoS cesse simplement de s'en
occuper :

- sa fiche, son historique, son forfait, son mode / plafond et son boost
  disparaissent de freeQoS ;
- il est inscrit dans la liste des clients retires : meme connecte, le releve
  l'ignore, il ne revient pas ;
- la file que FREEQOS avait posee pour lui (``freeqos-<login>``, marquee
  ``freeqos:managed``) est retiree par freeQoS a sa prochaine ecriture, pour
  qu'aucun plafond oublie ne reste applique. Aucune autre ligne du routeur
  n'est concernee.

Le retablir le sort de la liste : il revient au releve suivant, comme un client
nouvellement detecte (sans forfait, donc sans file).

Ecrire ici demande le niveau Admin (regle par defaut des routes d'ecriture).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, RepositoryDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["clients"])

MAX_PAR_APPEL = 500


class RemoveClients(BaseModel):
    logins: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        min_length=1, max_length=MAX_PAR_APPEL
    )
    confirm: bool = Field(
        default=False,
        description="Required: the client's history in freeQoS is deleted for good.",
    )


def _liste(container: Any) -> Any:
    retires = getattr(container, "exclusions", None)
    if retires is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The list of removed clients is not available",
        )
    return retires


def _acteur(request: Request) -> str | None:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("email") or user.get("name") or "") or None


async def _oublier(repo: Any, container: Any, login: str) -> dict[str, Any]:
    """Efface ce que freeQoS sait du client. Aucune ecriture routeur ici."""
    rapport: dict[str, Any] = {"login": login}

    plans = getattr(container, "client_plans_repo", None)
    if plans is not None:
        rapport["plan_removed"] = bool(await plans.delete(login))

    topo = getattr(container, "topology_repo", None)
    if topo is not None:
        rapport["override_removed"] = bool(await topo.delete_policy("subscriber", login))
        rapport["boost_removed"] = bool(await topo.clear_boost("subscriber", login))

    base = getattr(container, "database", None)
    if base is not None:
        from app.db import emergency_repo

        rapport["emergency_removed"] = await emergency_repo.delete(base.pool, login)

    statiques = getattr(container, "static_clients_repo", None)
    if statiques is not None:
        for ligne in await statiques.list_all():
            if str(ligne.get("reference")) == login:
                await statiques.delete(int(ligne["id"]))
                rapport["static_client_removed"] = True
                break

    fiche = await repo.get_subscriber_by_login(login)
    if fiche is not None:
        try:
            supprime = await repo.delete_subscriber(int(fiche["id"]))
            rapport["samples_deleted"] = int(supprime.get("samples") or 0)
        except LookupError:
            pass

    collection = getattr(container, "collection", None)
    if collection is not None:
        collection.directory.forget_subscriber(login)
        getattr(collection, "_known_logins", set()).discard(login)
    return rapport


@router.get("/clients/removed", summary="Clients removed from freeQoS")
async def list_removed(container: ContainerDep) -> dict[str, Any]:
    """Les clients retires de freeQoS : ils ne reviennent pas, meme connectes.

    Ceux qui SONT un client declare (meme IP, MAC ou nom) sont rendus a part
    (``duplicates``) : toujours ignores, ils ne comptent plus comme retires.
    """
    lignes = await _liste(container).list()
    for ligne in lignes:
        quand = ligne.get("removed_at")
        if quand is not None and hasattr(quand, "isoformat"):
            ligne["removed_at"] = quand.isoformat()
    # CE QUE LE RELEVE VOIT D'EUX (memoire seule) : connecte, IP, MAC, routeur.
    # Sans lien, c'est ce qui dit pourquoi : hors ligne (IP inconnue), ou une IP
    # qui n'est celle d'aucun client declare.
    collection = getattr(container, "collection", None)
    vues = dict(getattr(collection, "removed_sessions", {}) or {})
    for ligne in lignes:
        vu = vues.get(str(ligne["login"]))
        ligne["seen"] = (
            {
                "address": vu.get("address"),
                "mac": vu.get("caller_id"),
                "router": vu.get("router"),
                "pop_name": vu.get("pop_name"),
            }
            if vu
            else None
        )
    retires = [ligne for ligne in lignes if not ligne.get("same_as")]
    doublons = [ligne for ligne in lignes if ligne.get("same_as")]
    return {"count": len(retires), "clients": retires, "duplicates": doublons}


@router.post(
    "/clients/removed/clean",
    summary="Take out of 'Removed' the clients that are a declared client (same IP, MAC or name)",
)
async def clean_removed(container: ContainerDep) -> dict[str, Any]:
    """Compare tout de suite les clients retires aux clients declares."""
    from app.services.removed_duplicates import resolve

    _liste(container)
    return await resolve(container)


@router.post("/clients/remove", summary="Remove clients from freeQoS (not from the router)")
async def remove_clients(
    body: RemoveClients, request: Request, repo: RepositoryDep, container: ContainerDep
) -> dict[str, Any]:
    """Retire ces clients de freeQoS. Le routeur garde le client tel quel ;
    seule la file posee par freeQoS pour lui est retiree a la prochaine
    ecriture de freeQoS."""
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Removing clients deletes their history in freeQoS: 'confirm' must be true.",
        )
    retires = _liste(container)
    logins = list(dict.fromkeys(login.strip() for login in body.logins if login.strip()))
    if not logins:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No client given")
    # Inscrits D'ABORD : un releve qui tombe pendant l'effacement ne les
    # recree pas.
    await retires.add(logins, _acteur(request))
    clients = []
    for login in logins:
        try:
            clients.append(await _oublier(repo, container, login))
        except Exception as exc:  # noqa: BLE001 - un client ne bloque pas les autres
            logger.exception("Effacement incomplet de %s", login)
            clients.append({"login": login, "error": f"{type(exc).__name__}: {exc}"})
    return {"removed": len(logins), "clients": clients}


@router.delete("/clients/removed/{login}", summary="Bring a removed client back into freeQoS")
async def restore_client(
    container: ContainerDep, login: Annotated[str, Path(min_length=1, max_length=255)]
) -> dict[str, Any]:
    if not await _liste(container).remove(login):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a removed client")
    return {"login": login, "restored": True}

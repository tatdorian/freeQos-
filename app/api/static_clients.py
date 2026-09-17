"""Inventaire des clients a IP fixe, gere depuis l'interface.

Ces endpoints ecrivent une DECLARATION en base, jamais sur un equipement. Ce
que l'operateur saisit ici devient, au cycle suivant, un abonne comme un autre :
meme table, meme planification, meme reconciliation que les abonnes PPPoE.

Il n'y a volontairement aucune decouverte automatique derriere. Un client a IP
fixe n'ouvre pas de session et n'a pas d'attribut RADIUS : rien sur le reseau ne
dit qu'une adresse appartient a tel client ni quel debit il a souscrit. La
saisie manuelle n'est pas un pis-aller, c'est la seule source qui existe.

EN REVANCHE, LA SUITE NE SE FAIT PLUS A LA MAIN. Declarer un client POSE sa file
dans la foulee, sur le routeur de son PoP, et la reponse dit ce qui a ete ecrit
-- ou ce qui l'en empeche. La reconciliation periodique continue de passer
derriere ; elle n'est plus le seul chemin, et surtout plus la seule facon
d'apprendre qu'un client ne sera jamais bride parce que son PoP ne correspond a
aucun routeur.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.static_clients_repo import (
    DuplicateStaticClientError,
    InvalidStaticClientError,
    StaticClientNotFoundError,
    StaticClientsRepository,
    VlanSightingsRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["clients statiques"])


class StaticClientInput(BaseModel):
    reference: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Identite stable du client, reprise telle quelle comme identifiant "
            "d'abonne. Choisir une reference qui ne changera pas : y encoder "
            "l'IP ou le VLAN ferait perdre ses surcharges et son historique au "
            "premier demenagement."
        ),
    )
    pop_name: str = Field(min_length=1, max_length=128)
    address: str = Field(
        min_length=1,
        max_length=64,
        description="IP fixe (10.0.0.5) ou sous-reseau attribue au client (10.0.0.0/29)",
    )
    label: str | None = Field(default=None, max_length=128)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    sector_key: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Cle du noeud de topologie sous lequel rattacher le client. "
            "Declaratif : aucun caller-id n'existe pour ces clients."
        ),
    )
    plan_down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    plan_up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=512)


class StaticClientUpdate(BaseModel):
    """Mise a jour partielle : les champs omis restent inchanges."""

    reference: str | None = Field(default=None, min_length=1, max_length=64)
    pop_name: str | None = Field(default=None, min_length=1, max_length=128)
    address: str | None = Field(default=None, min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    sector_key: str | None = Field(default=None, max_length=128)
    plan_down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    plan_up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=512)


def _require_repository(container: ContainerDep) -> StaticClientsRepository:
    if container.static_clients_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inventaire des clients statiques indisponible (base non initialisee)",
        )
    return container.static_clients_repo


def _sightings(container: ContainerDep) -> VlanSightingsRepository | None:
    return container.sightings_repo


async def _poser_la_file(
    container: ContainerDep,
    *,
    reference: str,
    pop_name: str,
    removing: bool = False,
) -> dict[str, Any]:
    """Applique la file de ce client, et rend ce qui s'est passe.

    NE FAIT JAMAIS ECHOUER LA SAISIE. La fiche est deja enregistree quand on
    arrive ici : un routeur injoignable doit se raconter, pas annuler une
    declaration que l'exploitant a validee. Le rapport part dans la reponse,
    l'interface l'affiche telle quelle.
    """
    try:
        return await container.shaping.enforce_static_client(
            reference=reference,
            pop_name=pop_name,
            author="ui:static-client",
            removing=removing,
        )
    except Exception as exc:  # noqa: BLE001 - la fiche reste valide quoi qu'il arrive
        logger.exception("Pose immediate de la file impossible pour '%s'", reference)
        return {
            "reference": reference,
            "pop_name": pop_name,
            "state": "erreur",
            "reason": f"{type(exc).__name__}: {exc}",
            "applied": 0,
            "routers": [],
        }


@router.get("/static-clients", summary="Inventaire des clients a IP fixe")
async def list_static_clients(
    container: ContainerDep,
    pop_name: Annotated[str | None, Query(max_length=128)] = None,
) -> list[dict[str, Any]]:
    """Fiches declarees, enrichies de leur derniere presence observee.

    La presence vient de la table ARP et ne modifie JAMAIS la fiche : c'est une
    lecture jointe a l'affichage. Une fiche sans presence n'est pas une erreur
    -- le client peut etre silencieux, ou joignable par un chemin que l'ARP du
    routeur ne montre pas.
    """
    fiches = await _require_repository(container).list_all(pop_name=pop_name)
    repo = _sightings(container)
    if repo is None:
        return fiches
    try:
        presence = await repo.presence()
    except Exception:  # noqa: BLE001 - l'inventaire doit rester lisible
        logger.exception("Presence des clients statiques illisible")
        return fiches
    for fiche in fiches:
        vu = presence.get(fiche["reference"])
        fiche["last_seen_at"] = vu["last_seen"] if vu else None
        fiche["seen_mac"] = vu["mac"] if vu else None
        fiche["seen_vlan_interface"] = vu["vlan_interface"] if vu else None
        fiche["seen_router"] = vu["router_name"] if vu else None
    return fiches


@router.get(
    "/static-clients/candidates",
    summary="Adresses detectees sur une VLAN routee, non declarees",
)
async def list_candidates(container: ContainerDep) -> dict[str, Any]:
    """Ce que le recensement montre et que l'inventaire ne connait pas.

    A LIRE COMME UNE PISTE, PAS COMME UNE LISTE DE CLIENTS. Une observation dit
    qu'une adresse vit dans un sous-reseau desservi par le PoP : rien de plus.
    Une imprimante, une camera ou l'equipement d'un autre operateur produisent
    le meme signal. Aucun de ces candidats n'est faconne, aucun n'a de plan, et
    aucun ne le sera tant qu'un humain n'aura pas saisi sa fiche.

    Le recensement complet, avec ses sources et ses trous, est sur
    ``GET /pops/census``.
    """
    repo = _sightings(container)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Detection indisponible (base non initialisee)",
        )
    if not container.settings.vlan_detect_enabled:
        return {"enabled": False, "candidates": [], "count": 0}
    lignes = await repo.candidates(
        limit=container.settings.vlan_candidate_limit,
        max_age_s=container.settings.vlan_sighting_retention_s,
    )
    return {"enabled": True, "candidates": lignes, "count": len(lignes)}


@router.get(
    "/static-clients/candidates/diagnostic",
    summary="Pourquoi un client sur VLAN est vu, ou ne l'est pas",
)
async def diagnose_candidates(
    container: ContainerDep,
    router_name: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """Lit le routeur en direct et rend le motif de chaque ligne ARP ecartee.

    A LIRE D'ABORD QUAND UN CLIENT MANQUE. Une adresse est retenue par deux
    chemins : son interface est une VLAN declaree sans serveur PPPoE, OU son
    adresse tombe dans un sous-reseau que le routeur dessert (``reseaux_clients``,
    tire de ``/ip/address``). Le second chemin est celui qui rend visibles les
    clients derriere un pont en filtrage VLAN.

    Deux champs repondent presque toujours : ``reseaux_clients`` s'il est vide --
    sans ``/ip/address``, seul le nom des interfaces sert de critere -- et
    ``interfaces_hors_vlan``, les interfaces dont les adresses ne tombent nulle
    part. Le recensement complet du PoP est joint sous ``recensement``.

    Lecture seule : une quinzaine de commandes ``print``, rien n'est configure.
    """
    collecteurs = [c for c in container.registry.collectors if router_name in (None, c.name)]
    if not collecteurs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Aucun routeur collecte ne correspond a '{router_name}'. "
                f"Un routeur ecarte de la collecte n'est lu nulle part : "
                f"verifiez l'onglet Equipements."
            )
            if router_name
            else "Aucun routeur n'est collecte.",
        )

    rapports: list[dict[str, Any]] = []
    for collecteur in collecteurs:
        try:
            rapports.append(await collecteur.explain_vlan_clients())
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas le reste
            rapports.append(
                {
                    "router": collecteur.name,
                    "pop_name": collecteur.config.effective_pop_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"enabled": container.settings.vlan_detect_enabled, "routers": rapports}


@router.get(
    "/static-clients/enforcement",
    summary="Etat de la file de chaque client declare, et ce qui manque",
)
async def enforcement_state(container: ContainerDep) -> dict[str, Any]:
    """Repond a "j'ai declare ce client, pourquoi ne remonte-t-il pas ?".

    Un client declare peut rester sans file pour des raisons qui n'ont rien
    d'une panne, et qu'aucun ecran ne montrait jusqu'ici :

    - son PoP ne correspond a aucun routeur collecte (faute de frappe, routeur
      ecarte) -- il n'entre alors dans l'etat desire de personne ;
    - aucun debit souscrit n'a ete saisi : il n'y a rien a appliquer ;
    - une file tierce occupe deja son adresse ;
    - l'enforcement est desactive.

    Chaque cas a son motif, rendu tel que le planificateur l'a ecrit. Lecture
    seule : un plan par routeur concerne, aucune ecriture.
    """
    _require_repository(container)
    return {
        "enforcement_enabled": container.shaping.enforcement_enabled,
        "clients": await container.shaping.static_clients_enforcement(),
    }


@router.post(
    "/static-clients",
    status_code=status.HTTP_201_CREATED,
    summary="Declarer un client a IP fixe",
)
async def create_static_client(
    payload: StaticClientInput, container: ContainerDep
) -> dict[str, Any]:
    repository = _require_repository(container)
    try:
        created = await repository.create(payload.model_dump())
    except DuplicateStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info("Client statique '%s' declare depuis l'interface", created["reference"])
    created["enforcement"] = await _poser_la_file(
        container, reference=created["reference"], pop_name=str(created["pop_name"])
    )
    return created


@router.patch("/static-clients/{client_id}", summary="Modifier un client a IP fixe")
async def update_static_client(
    payload: StaticClientUpdate,
    container: ContainerDep,
    client_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """Modifie la fiche, puis REAPPLIQUE la file dans la foulee.

    Un debit change ou une adresse corrigee n'a aucun effet tant que la file ne
    l'a pas suivi. Attendre la reconciliation laisserait l'exploitant devant une
    fiche qui dit une chose et un routeur qui en fait une autre.
    """
    repository = _require_repository(container)
    try:
        modifie = await repository.update(client_id, payload.model_dump(exclude_unset=True))
    except StaticClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DuplicateStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    modifie["enforcement"] = await _poser_la_file(
        container, reference=modifie["reference"], pop_name=str(modifie["pop_name"])
    )
    return modifie


@router.delete(
    "/static-clients/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un client de l'inventaire",
)
async def delete_static_client(
    container: ContainerDep,
    client_id: Annotated[int, Path(ge=1)],
) -> None:
    """Retire la fiche ET sa file, tout de suite. L'historique est CONSERVE.

    La file de CE client est retiree, et elle seule : le plan est calcule avec
    ``prune`` puis restreint a son nom, si bien qu'un ``/ppp/active`` vide au
    mauvais moment ne peut pas emporter les files du PoP avec elle.
    """
    repository = _require_repository(container)
    try:
        fiche = await repository.get(client_id)
    except StaticClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        await repository.delete(client_id)
    except StaticClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # Apres la suppression : la fiche ne doit plus etre dans l'etat desire au
    # moment ou le plan est calcule, sinon la file serait aussitot reposee.
    await _poser_la_file(
        container,
        reference=str(fiche["reference"]),
        pop_name=str(fiche["pop_name"]),
        removing=True,
    )

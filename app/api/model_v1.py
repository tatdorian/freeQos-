"""API publique du modele de reseau -- contrat compatible Preseem.

POURQUOI CE CONTRAT-LA, ET PAS UN AUTRE
---------------------------------------
Le but n'est pas d'avoir "une API". Le but est de REMPLACER Preseem dans une
chaine d'exploitation qui tourne deja. Or ce qui coute cher dans une bascule,
ce n'est pas le controleur : c'est tout ce qui parle au controleur. Splynx,
UISP/UCRM, Powercode, Visp, et les developpements maison poussent deja leur
inventaire vers l'API "model" de Preseem.

On reprend donc sa forme telle quelle :

    GET    /model/v1/<collection>        liste
    GET    /model/v1/<collection>/<id>   fiche
    PUT    /model/v1/<collection>/<id>   cree ou remplace (idempotent)
    DELETE /model/v1/<collection>/<id>   retire

    Authorization: Basic base64(<cle>:)   -- la cle tient lieu d'utilisateur

Cinq collections : ``accounts`` (clients), ``packages`` (forfaits), ``sites``
(points hauts), ``access_points`` (secteurs radio), ``services`` (lignes
vendues). Un integrateur change l'URL de base et la cle. Rien d'autre.

POURQUOI PUT ET PAS POST. La facturation est la source de verite, et elle
resynchronise : elle doit pouvoir rejouer son inventaire entier sans se
demander ce qui existe deja. PUT sur un identifiant choisi par l'appelant est
la seule forme qui rende ce rejeu inoffensif.

CE QUI EST REFUSE, ET POURQUOI. L'identifiant dans l'URL fait foi ; un corps
qui en porte un autre est un 400, parce que deviner lequel des deux est le bon
reviendrait a ecrire au hasard sur le reseau d'un operateur. Et un service qui
reprend l'identifiant d'une fiche SAISIE A LA MAIN est un 409 : l'API ne prend
jamais la main sur un geste humain.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path, Response, status

from app.api.auth import ReadDep, WriteDep
from app.api.deps import ContainerDep
from app.db.model_repo import (
    TABLES,
    ModelConflictError,
    ModelNotFoundError,
    ModelRepository,
    ModelValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/model/v1", tags=["public api (model)"])

COLLECTIONS = (*TABLES.keys(), "services")

CollectionPath = Annotated[
    str,
    Path(
        description="accounts, packages, sites, access_points ou services",
        pattern="^(accounts|packages|sites|access_points|services)$",
    ),
]
IdPath = Annotated[str, Path(min_length=1, max_length=128, description="External identifier")]


def _repository(container: ContainerDep) -> ModelRepository:
    if container.model_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public API unavailable (database not initialised)",
        )
    return container.model_repo


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, ModelNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ModelConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ModelValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


@router.get("", summary="Available collections")
async def index(caller: ReadDep) -> dict[str, Any]:
    """Point d'entree lisible : un integrateur doit pouvoir verifier sa cle
    d'un seul appel, sans deviner un nom de collection."""
    return {
        "api": "freeqos-model",
        "version": "v1",
        "compatible_with": "preseem-model-v1",
        "collections": list(COLLECTIONS),
        "key": caller.prefix,
        "scopes": list(caller.scopes),
    }


@router.get("/{collection}", summary="List a collection")
async def list_collection(
    collection: CollectionPath, container: ContainerDep, caller: ReadDep
) -> list[dict[str, Any]]:
    repo = _repository(container)
    try:
        if collection == "services":
            return await repo.list_services()
        return await repo.list_objects(collection)
    except (ModelNotFoundError, ModelConflictError, ModelValidationError) as exc:
        raise _translate(exc) from exc


@router.get("/{collection}/{object_id}", summary="Read a record")
async def get_object(
    collection: CollectionPath, object_id: IdPath, container: ContainerDep, caller: ReadDep
) -> dict[str, Any]:
    repo = _repository(container)
    try:
        if collection == "services":
            return await repo.get_service(object_id)
        return await repo.get_object(collection, object_id)
    except (ModelNotFoundError, ModelConflictError, ModelValidationError) as exc:
        raise _translate(exc) from exc


@router.put("/{collection}/{object_id}", summary="Create or replace a record")
async def put_object(
    collection: CollectionPath,
    object_id: IdPath,
    container: ContainerDep,
    caller: WriteDep,
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> dict[str, Any]:
    corps_id = payload.get("id")
    if corps_id is not None and str(corps_id) != object_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"the body identifier ('{corps_id}') does not match the one "
                f"in the URI ('{object_id}')"
            ),
        )
    repo = _repository(container)
    try:
        if collection == "services":
            fiche = await repo.put_service(object_id, payload)
        elif collection == "accounts":
            fiche = await repo.put_account(object_id, payload)
        elif collection == "packages":
            fiche = await repo.put_package(object_id, payload)
        elif collection == "sites":
            fiche = await repo.put_site(object_id, payload)
        else:
            fiche = await repo.put_access_point(object_id, payload)
    except (ModelNotFoundError, ModelConflictError, ModelValidationError) as exc:
        raise _translate(exc) from exc

    logger.info("%s a ecrit %s/%s", caller.label, collection, object_id)
    # Un service qui vient d'etre ecrit doit etre SHAPE, pas attendre le
    # prochain cycle de reconciliation : une integration de facturation qui
    # active une ligne s'attend a ce que le client ait son debit tout de suite.
    if collection == "services":
        fiche["enforcement"] = await _apply_service(container, fiche)
    return fiche


@router.delete(
    "/{collection}/{object_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a record",
)
async def delete_object(
    collection: CollectionPath,
    object_id: IdPath,
    container: ContainerDep,
    caller: WriteDep,
) -> Response:
    repo = _repository(container)
    try:
        if collection == "services":
            await repo.delete_service(object_id)
        else:
            await repo.delete_object(collection, object_id)
    except (ModelNotFoundError, ModelConflictError, ModelValidationError) as exc:
        raise _translate(exc) from exc
    logger.info("%s a supprime %s/%s", caller.label, collection, object_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _apply_service(container: ContainerDep, fiche: dict[str, Any]) -> dict[str, Any]:
    """Pose la file du service qui vient d'etre ecrit.

    N'ECHOUE JAMAIS L'ECRITURE. La fiche est deja enregistree quand on arrive
    ici : un routeur injoignable doit se raconter dans la reponse, pas annuler
    une synchronisation que la facturation considere comme faite. C'est
    exactement la regle appliquee a la saisie manuelle.
    """
    try:
        return await container.shaping.enforce_static_client(
            reference=str(fiche["id"]),
            pop_name=str(fiche.get("pop_name") or ""),
            author="api:model",
        )
    except Exception as exc:  # noqa: BLE001 - la synchronisation prime
        logger.warning("File non posee pour le service %s : %s", fiche.get("id"), exc)
        return {"applied": False, "reason": str(exc)}

"""Gestion des cles d'API, depuis l'interface d'exploitation.

LE SECRET N'EST MONTRE QU'UNE FOIS. C'est la reponse de creation qui le porte,
et plus rien ensuite : la base n'en garde que l'empreinte. Une cle perdue se
revoque et se remplace -- elle ne se retrouve pas, y compris par celui qui l'a
creee. C'est la seule facon de rendre inoffensive une fuite de sauvegarde.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.api_keys_repo import ApiKeyNotFoundError, ApiKeysRepository
from app.services.api_keys import SCOPES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cles d'api"])


class ApiKeyInput(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="A quoi sert cette cle")
    scopes: list[str] = Field(
        default_factory=lambda: ["read"],
        description=f"Portees accordees parmi {', '.join(SCOPES)}",
    )
    note: str | None = Field(default=None, max_length=512)
    expires_at: datetime | None = Field(
        default=None, description="Echeance facultative (la cle cesse d'etre acceptee apres)"
    )


class ApiKeyToggle(BaseModel):
    enabled: bool


def _repo(container: ContainerDep) -> ApiKeysRepository:
    if container.api_keys_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cles d'API indisponibles (base non initialisee)",
        )
    return container.api_keys_repo


@router.get("/api-keys", summary="Cles d'API enregistrees (jamais les secrets)")
async def list_keys(container: ContainerDep) -> list[dict[str, Any]]:
    return await _repo(container).list_all()


@router.post(
    "/api-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Creer une cle (le secret n'est rendu qu'ici)",
)
async def create_key(payload: ApiKeyInput, container: ContainerDep) -> dict[str, Any]:
    fiche, secret = await _repo(container).create(
        name=payload.name,
        scopes=payload.scopes,
        note=payload.note,
        created_by="ui",
        expires_at=payload.expires_at,
    )
    logger.info("Cle d'API '%s' creee (%s)", payload.name, fiche["prefix"])
    return {
        **fiche,
        "secret": secret,
        "warning": (
            "Ce secret ne sera plus jamais affiche. Copiez-le maintenant dans "
            "le systeme qui doit appeler l'API."
        ),
    }


@router.patch("/api-keys/{key_id}", summary="Activer ou desactiver une cle")
async def toggle_key(
    key_id: Annotated[int, Path(ge=1)], payload: ApiKeyToggle, container: ContainerDep
) -> dict[str, Any]:
    try:
        return await _repo(container).set_enabled(key_id, enabled=payload.enabled)
    except ApiKeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoquer une cle definitivement",
)
async def delete_key(key_id: Annotated[int, Path(ge=1)], container: ContainerDep) -> None:
    try:
        await _repo(container).delete(key_id)
    except ApiKeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

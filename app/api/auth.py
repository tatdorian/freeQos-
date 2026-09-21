"""Authentification de l'API publique par cle.

TROIS FACONS DE PRESENTER LA MEME CLE, ET POURQUOI.

``Authorization: Basic base64(cle:)`` est la forme que Preseem impose a ses
integrateurs : la cle tient lieu de nom d'utilisateur, le mot de passe est vide.
La supporter telle quelle est ce qui permet de remplacer Preseem sans toucher au
code du systeme de facturation -- on change l'URL de base et la cle, rien
d'autre. C'est le point entier de ce module.

``Authorization: Bearer <cle>`` et ``X-API-Key: <cle>`` sont ajoutes parce que
tout le reste du monde ecrit comme ca, et qu'un integrateur neuf ne devrait pas
avoir a decouvrir une convention de 2010 pour appeler une API de 2026.

LE FRONT D'ADMIN N'EST PAS CONCERNE. Il vit sur la meme origine, derriere le
meme reseau de management, et parle aux routes ``/api/v1``. Ces cles-ci
protegent l'API EXTERNE, celle qu'un systeme tiers appelle depuis ailleurs.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import ContainerDep
from app.db.api_keys_repo import ApiKeysRepository

logger = logging.getLogger(__name__)

#: Renvoye sur chaque refus : sans lui, `curl -u cle:` ne sait pas qu'il doit
#: presenter une authentification Basic et abandonne au premier 401.
_CHALLENGE = {"WWW-Authenticate": 'Basic realm="freeQoS", charset="UTF-8"'}


@dataclass(frozen=True)
class ApiCaller:
    """L'appelant authentifie, tel qu'il apparait dans les journaux."""

    key_id: int
    name: str
    prefix: str
    scopes: tuple[str, ...]

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def label(self) -> str:
        return f"api:{self.name} ({self.prefix})"


def extract_secret(request: Request) -> str | None:
    """Sort la cle de la requete, quelle que soit la forme employee."""
    entete = request.headers.get("authorization", "").strip()
    if entete:
        schema, _, valeur = entete.partition(" ")
        schema = schema.lower()
        valeur = valeur.strip()
        if schema == "bearer" and valeur:
            return valeur
        if schema == "basic" and valeur:
            try:
                decode = base64.b64decode(valeur, validate=True).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                return None
            # La cle est le NOM D'UTILISATEUR, mot de passe vide (convention
            # Preseem). On accepte aussi l'inverse : un client qui met la cle
            # dans le mot de passe ne doit pas se heurter a un 401 muet.
            utilisateur, _, mot_de_passe = decode.partition(":")
            return utilisateur.strip() or mot_de_passe.strip() or None
    depuis_entete = request.headers.get("x-api-key", "").strip()
    return depuis_entete or None


def _repository(container: ContainerDep) -> ApiKeysRepository:
    if container.api_keys_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public API unavailable (database not initialised)",
        )
    return container.api_keys_repo


async def authenticate(request: Request, container: ContainerDep) -> ApiCaller:
    secret = extract_secret(request)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "API key missing. Present it as Basic (key as the username, "
                "empty password), as Bearer, or in the X-API-Key header."
            ),
            headers=_CHALLENGE,
        )
    fiche = await _repository(container).authenticate(secret)
    if fiche is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalid, disabled or expired",
            headers=_CHALLENGE,
        )
    return ApiCaller(
        key_id=int(fiche["id"]),
        name=str(fiche["name"]),
        prefix=str(fiche["prefix"]),
        scopes=tuple(fiche["scopes"]),
    )


class RequireScope:
    """Dependance FastAPI : authentifie, puis exige une portee.

    Le 403 est distinct du 401 a dessein : "je ne sais pas qui tu es" et "je
    sais qui tu es, mais cette cle ne peut que lire" appellent deux gestes
    differents cote integrateur.
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def __call__(self, caller: Annotated[ApiCaller, Depends(authenticate)]) -> ApiCaller:
        if not caller.has(self.scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This key does not have the '{self.scope}' scope "
                    f"(granted scopes: {', '.join(caller.scopes)})"
                ),
            )
        return caller


ReadDep = Annotated[ApiCaller, Depends(RequireScope("read"))]
WriteDep = Annotated[ApiCaller, Depends(RequireScope("write"))]

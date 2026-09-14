"""Authentification : connexion, session, cles d'API, comptes.

La connexion (``POST /auth/login``) est le SEUL endpoint anonyme de l'API :
tout le reste passe par ``require_identity``. Les endpoints de gestion (comptes,
cles) exigent en plus un compte administrateur.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.services.auth import (
    SESSION_COOKIE,
    AuthError,
    AuthService,
    Identity,
    InvalidCredentialsError,
    RateLimitedError,
    cookie_secure_default,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _auth(container: ContainerDep) -> AuthService:
    auth = container.auth
    if auth is None:
        # Refus SUR plutot que laisser passer : sans service d'auth, aucun
        # endpoint protege ne doit devenir accessible par accident.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentification non initialisee",
        )
    return auth


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


async def require_identity(request: Request, container: ContainerDep) -> Identity:
    """Exige une identite : cle d'API (appels machine) ou session (interface).

    C'est cette dependance, posee sur TOUS les routeurs de l'API, qui garantit
    qu'aucun endpoint n'est joignable anonymement.
    """
    auth = _auth(container)

    # Cle d'API d'abord : c'est le chemin des appels machine, sans cookie.
    secret = request.headers.get("x-api-key") or _bearer_token(request)
    if secret:
        identity = await auth.resolve_api_key(secret)
        if identity is not None:
            return identity
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cle d'API invalide",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        identity = await auth.resolve_session(token)
        if identity is not None:
            return identity

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentification requise",
        headers={"WWW-Authenticate": "Bearer"},
    )


IdentityDep = Annotated[Identity, Depends(require_identity)]


async def require_admin(identity: IdentityDep) -> Identity:
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reserve aux administrateurs",
        )
    return identity


AdminDep = Annotated[Identity, Depends(require_admin)]


# --------------------------------------------------------------- connexion
class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _set_session_cookie(
    response: Response, container: ContainerDep, token: str, max_age_s: int
) -> None:
    secure = container.settings.auth_cookie_secure
    if secure is None:
        secure = cookie_secure_default(container.settings.app_env)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age_s,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


@router.post("/auth/login", summary="Ouvrir une session")
async def login(payload: LoginInput, container: ContainerDep, response: Response) -> dict[str, Any]:
    """Verifie les identifiants et pose le cookie de session.

    Seul endpoint anonyme : c'est par lui qu'on obtient une identite. Limite en
    debit pour qu'un mot de passe ne se devine pas par force brute.
    """
    auth = _auth(container)
    try:
        identity = await auth.authenticate(payload.username, payload.password)
    except RateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_s))},
        ) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    token, expires_at = await auth.create_session(identity)
    _set_session_cookie(response, container, token, int(auth.session_ttl.total_seconds()))
    return {"identity": identity.to_dict(), "expires_at": expires_at}


@router.post("/auth/logout", summary="Fermer la session")
async def logout(
    request: Request, container: ContainerDep, response: Response, identity: IdentityDep
) -> dict[str, Any]:
    auth = _auth(container)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await auth.revoke_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/auth/me", summary="Identite courante")
async def me(identity: IdentityDep) -> dict[str, Any]:
    return identity.to_dict()


# ------------------------------------------------------------- comptes
class UserInput(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str | None = Field(default=None, max_length=256)
    is_admin: bool = False


class PasswordInput(BaseModel):
    password: str = Field(min_length=8, max_length=1024)


@router.get("/auth/users", summary="Comptes locaux")
async def list_users(container: ContainerDep, _: AdminDep) -> list[dict[str, Any]]:
    return await _auth(container).list_users()


@router.post("/auth/users", status_code=status.HTTP_201_CREATED, summary="Creer un compte")
async def create_user(
    payload: UserInput, container: ContainerDep, admin: AdminDep
) -> dict[str, Any]:
    auth = _auth(container)
    try:
        await auth.create_user(
            payload.username,
            payload.password,
            is_admin=payload.is_admin,
            display=payload.display_name,
        )
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("Compte '%s' cree par %s", payload.username, admin.name)
    return {"username": payload.username, "is_admin": payload.is_admin}


@router.put("/auth/users/{username}/password", summary="Changer un mot de passe")
async def set_password(
    username: str, payload: PasswordInput, container: ContainerDep, identity: IdentityDep
) -> dict[str, Any]:
    """Un administrateur change n'importe quel mot de passe ; un utilisateur ne
    peut changer que le sien."""
    if not identity.is_admin and identity.name != username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous ne pouvez changer que votre propre mot de passe",
        )
    changed = await _auth(container).set_password(username, payload.password)
    if not changed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compte inconnu")
    return {"username": username, "password_changed": True}


# ------------------------------------------------------------- cles d'API
class ApiKeyInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    is_admin: bool = False


@router.get("/auth/api-keys", summary="Cles d'API existantes")
async def list_api_keys(container: ContainerDep, _: AdminDep) -> list[dict[str, Any]]:
    return await _auth(container).list_api_keys()


@router.post("/auth/api-keys", status_code=status.HTTP_201_CREATED, summary="Creer une cle d'API")
async def create_api_key(
    payload: ApiKeyInput, container: ContainerDep, admin: AdminDep
) -> dict[str, Any]:
    """Cree une cle machine. Le secret n'est renvoye QU'ICI, une seule fois : il
    n'est jamais stocke en clair et ne pourra pas etre relu."""
    record, secret = await _auth(container).create_api_key(
        payload.name, is_admin=payload.is_admin, created_by=admin.name
    )
    logger.info("Cle d'API '%s' creee par %s", payload.name, admin.name)
    return {"api_key": record.to_public(), "secret": secret}


@router.delete("/auth/api-keys/{key_id}", summary="Revoquer une cle d'API")
async def delete_api_key(key_id: str, container: ContainerDep, _: AdminDep) -> dict[str, Any]:
    deleted = await _auth(container).delete_api_key(key_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cle inconnue")
    return {"deleted": True}

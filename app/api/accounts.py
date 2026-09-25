"""Connexion a l'interface d'exploitation, et gestion des comptes.

LE CONTRAT
----------
- Aucun compte en base : l'interface propose d'en creer un. Ce PREMIER compte
  est en edition -- sans lui, personne ne pourrait en creer d'autres.
- Ensuite, toute route ``/api/v1`` exige une session, sauf celles de ce module
  qui servent a en ouvrir une.
- Un compte ``read`` n'obtient que les methodes de lecture ; toute ecriture lui
  est refusee par le SERVEUR (403), quelle que soit la route.
- Seul un compte ``edit`` gere les comptes : il fixe l'email, le mot de passe
  et le grade de chacun.

La session est un cookie ``HttpOnly`` (illisible par le JavaScript de la page)
et ``SameSite=Strict`` (jamais envoye par un autre site) ; les ecritures
verifient en plus l'en-tete ``Origin`` quand le navigateur le fournit.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.users_repo import DuplicateUserError, UserNotFoundError, UsersStore
from app.services.accounts import (
    ROLE_EDIT,
    SAFE_METHODES,
    InvalidAccountError,
    LoginThrottle,
    check_password,
    check_role,
    hash_password,
    new_session_token,
    normalise_email,
    token_digest,
    verify_or_decoy,
    verify_password,
)

logger = logging.getLogger(__name__)

COOKIE = "freeqos_session"

router = APIRouter(tags=["accounts"])

#: Compte fictif rendu quand l'authentification est coupee (AUTH_ENABLED=false) :
#: tout est permis, et l'interface le dit.
ANONYME: dict[str, Any] = {"id": 0, "email": "(authentication disabled)", "role": ROLE_EDIT}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class NewUser(Credentials):
    role: Literal["read", "edit"] = "read"


class UserUpdate(BaseModel):
    role: Literal["read", "edit"] | None = None
    password: str | None = Field(default=None, max_length=256)
    disabled: bool | None = None


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=1, max_length=256)


def _store(container: ContainerDep) -> UsersStore:
    if container.users_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable (database not initialised)",
        )
    return container.users_repo


def _ttl(container: ContainerDep) -> timedelta:
    return timedelta(hours=max(1, int(container.settings.session_ttl_hours)))


def _throttle(container: ContainerDep) -> LoginThrottle:
    if container.login_throttle is None:
        container.login_throttle = LoginThrottle()
    return container.login_throttle


def _refus(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


#: Sessions verifiees recemment : empreinte -> (compte, heure monotone).
#:
#: Une page envoie une dizaine de requetes en parallele. Verifier la session en
#: base a CHACUNE, c'etait une ecriture (prolongation) par requete, toutes sur
#: la MEME ligne -- donc servies l'une apres l'autre, et autant de connexions
#: prises au reservoir que la collecte partage. Une verification toutes les
#: trente secondes suffit ; toute modification de compte vide ce cache, pour
#: qu'un grade retire ou une session fermee s'applique aussitot.
_SESSIONS_VUES: dict[str, tuple[dict[str, Any], float]] = {}
SESSION_CACHE_S = 30.0


def forget_cached_sessions() -> None:
    _SESSIONS_VUES.clear()


async def current_user(request: Request, container: ContainerDep) -> dict[str, Any]:
    """Le compte de la session, ou 401. Ne juge pas encore du grade."""
    if not container.settings.auth_enabled:
        return ANONYME
    jeton = request.cookies.get(COOKIE)
    if not jeton:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    empreinte = token_digest(jeton)
    vu = _SESSIONS_VUES.get(empreinte)
    if vu is not None and time.monotonic() - vu[1] < SESSION_CACHE_S:
        return vu[0]
    fiche = await _store(container).session_user(empreinte, ttl=_ttl(container))
    if fiche is not None:
        if len(_SESSIONS_VUES) > 1000:
            _SESSIONS_VUES.clear()
        _SESSIONS_VUES[empreinte] = (fiche, time.monotonic())
    if fiche is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired, log in again"
        )
    return fiche


UserDep = Annotated[dict[str, Any], Depends(current_user)]


def _meme_origine(request: Request) -> bool:
    """Une ecriture venue d'une autre page que la notre est refusee.

    ``SameSite=Strict`` suffit sur un navigateur recent ; l'en-tete ``Origin``
    couvre le reste. Absent (outil en ligne de commande), il ne bloque rien :
    la session reste exigee.
    """
    origine = request.headers.get("origin")
    if not origine:
        return True
    hote = request.headers.get("host", "")
    return urlsplit(origine).netloc == hote


async def require_access(request: Request, user: UserDep) -> dict[str, Any]:
    """Dependance posee sur TOUTES les routes d'exploitation."""
    if request.method.upper() in SAFE_METHODES:
        return user
    if not _meme_origine(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")
    if user.get("role") != ROLE_EDIT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only account: this change needs an account with edit rights",
        )
    return user


async def require_editor(request: Request, user: UserDep) -> dict[str, Any]:
    if user.get("role") != ROLE_EDIT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account management needs edit rights"
        )
    if request.method.upper() not in SAFE_METHODES and not _meme_origine(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")
    return user


EditorDep = Annotated[dict[str, Any], Depends(require_editor)]


def _pose_cookie(request: Request, response: Response, jeton: str, ttl: timedelta) -> None:
    response.set_cookie(
        COOKIE,
        jeton,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        samesite="strict",
        # En HTTPS le cookie ne part jamais en clair. En HTTP (lab), il faut
        # bien qu'il parte : le poser 'secure' rendrait la connexion impossible.
        secure=request.url.scheme == "https",
        path="/",
    )


async def _ouvre_session(
    request: Request, response: Response, container: ContainerDep, fiche: dict[str, Any]
) -> None:
    jeton = new_session_token()
    ttl = _ttl(container)
    await _store(container).open_session(
        token_hash=token_digest(jeton),
        user_id=int(fiche["id"]),
        ttl=ttl,
        user_agent=request.headers.get("user-agent"),
        address=_client_ip(request),
    )
    await _store(container).update(int(fiche["id"]), last_login_at=datetime.now(tz=UTC))
    _pose_cookie(request, response, jeton, ttl)


def _public(fiche: dict[str, Any]) -> dict[str, Any]:
    return {k: fiche.get(k) for k in ("id", "email", "role")}


# ------------------------------------------------------------ session


@router.get("/auth/status", summary="Who am I, and is a first account needed?")
async def auth_status(request: Request, container: ContainerDep) -> dict[str, Any]:
    if not container.settings.auth_enabled:
        return {"auth_enabled": False, "setup_required": False, "user": _public(ANONYME)}
    store = _store(container)
    if await store.count() == 0:
        return {"auth_enabled": True, "setup_required": True, "user": None}
    utilisateur = None
    jeton = request.cookies.get(COOKIE)
    if jeton:
        fiche = await store.session_user(token_digest(jeton), ttl=_ttl(container))
        utilisateur = _public(fiche) if fiche else None
    return {"auth_enabled": True, "setup_required": False, "user": utilisateur}


@router.post("/auth/setup", summary="Create the FIRST account (edit rights)")
async def auth_setup(
    payload: Credentials, request: Request, response: Response, container: ContainerDep
) -> dict[str, Any]:
    """Possible une seule fois : tant qu'AUCUN compte n'existe."""
    try:
        email = normalise_email(payload.email)
        mot_de_passe = check_password(payload.password)
    except InvalidAccountError as exc:
        raise _refus(exc) from exc
    if not _meme_origine(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")
    fiche = await _store(container).create_first(
        email=email, password_hash=hash_password(mot_de_passe)
    )
    if fiche is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An account already exists: log in"
        )
    logger.info("Premier compte cree : %s (edition)", email)
    await _ouvre_session(request, response, container, fiche)
    return {"user": _public(fiche)}


@router.post("/auth/login", summary="Log in with email and password")
async def auth_login(
    payload: Credentials, request: Request, response: Response, container: ContainerDep
) -> dict[str, Any]:
    frein = _throttle(container)
    email = (payload.email or "").strip().lower()
    cles = (f"email:{email}", f"ip:{_client_ip(request)}")
    attente = frein.locked_for(*cles)
    if attente:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts: try again in {int(attente) + 1} s",
        )
    fiche = await _store(container).credentials(email)
    stocke = fiche.get("password_hash") if fiche else None
    # Meme message, meme duree, que l'email existe ou non : la page de
    # connexion ne doit pas servir a savoir qui a un compte.
    if not verify_or_decoy(payload.password, stocke) or fiche is None or fiche["disabled"]:
        frein.failure(*cles)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong email or password"
        )
    frein.success(*cles)
    await _ouvre_session(request, response, container, fiche)
    return {"user": _public(fiche)}


@router.post("/auth/logout", summary="Close the current session")
async def auth_logout(request: Request, response: Response, container: ContainerDep) -> None:
    jeton = request.cookies.get(COOKIE)
    forget_cached_sessions()
    if jeton and container.users_repo is not None:
        await container.users_repo.close_session(token_digest(jeton))
    response.delete_cookie(COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT


@router.post("/auth/password", summary="Change my own password")
async def change_my_password(
    payload: PasswordChange, request: Request, user: UserDep, container: ContainerDep
) -> dict[str, Any]:
    """Ouvert a TOUS les grades : un compte en lecture seule gere son propre mot
    de passe. Les autres sessions de ce compte sont fermees."""
    if not container.settings.auth_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Authentication is off")
    if not _meme_origine(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")
    store = _store(container)
    fiche = await store.credentials(str(user["email"]))
    if fiche is None or not verify_password(payload.current, fiche.get("password_hash")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is wrong"
        )
    try:
        nouveau = check_password(payload.new)
    except InvalidAccountError as exc:
        raise _refus(exc) from exc
    await store.update(int(user["id"]), password_hash=hash_password(nouveau))
    forget_cached_sessions()
    jeton = request.cookies.get(COOKIE)
    await store.close_sessions_of(int(user["id"]), keep=token_digest(jeton) if jeton else None)
    return {"ok": True}


# ------------------------------------------------------------ comptes


@router.get("/users", summary="List the accounts (edit rights)")
async def list_users(editor: EditorDep, container: ContainerDep) -> list[dict[str, Any]]:
    return await _store(container).list_all()


@router.post("/users", summary="Create an account (edit rights)", status_code=201)
async def create_user(
    payload: NewUser, editor: EditorDep, container: ContainerDep
) -> dict[str, Any]:
    try:
        email = normalise_email(payload.email)
        mot_de_passe = check_password(payload.password)
        role = check_role(payload.role)
    except InvalidAccountError as exc:
        raise _refus(exc) from exc
    try:
        fiche = await _store(container).create(
            email=email,
            password_hash=hash_password(mot_de_passe),
            role=role,
            created_by=str(editor.get("email")),
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("Compte %s (%s) cree par %s", email, role, editor.get("email"))
    return fiche


async def _garde_un_editeur(store: UsersStore, cible: dict[str, Any], *, perd: bool) -> None:
    """Il reste TOUJOURS au moins un compte d'edition actif : sans lui, plus
    personne ne pourrait creer, reparer ou deverrouiller un compte."""
    if perd and cible["role"] == ROLE_EDIT and not cible["disabled"]:
        if await store.count_editors() <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This is the last account with edit rights: keep at least one",
            )


@router.patch("/users/{user_id}", summary="Change role, password or state (edit rights)")
async def update_user(
    user_id: Annotated[int, Path(ge=1)],
    payload: UserUpdate,
    editor: EditorDep,
    container: ContainerDep,
) -> dict[str, Any]:
    store = _store(container)
    try:
        cible = await store.get(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    champs: dict[str, Any] = {}
    perd = False
    if payload.role is not None:
        champs["role"] = payload.role
        perd = perd or payload.role != ROLE_EDIT
    if payload.disabled is not None:
        champs["disabled"] = payload.disabled
        perd = perd or payload.disabled
    if payload.password:
        try:
            champs["password_hash"] = hash_password(check_password(payload.password))
        except InvalidAccountError as exc:
            raise _refus(exc) from exc
    await _garde_un_editeur(store, cible, perd=perd)
    fiche = await store.update(user_id, **champs)
    forget_cached_sessions()
    # Un grade retire, un compte coupe ou un mot de passe change ferment ses
    # sessions : l'effet est immediat, pas au prochain expirement.
    if "password_hash" in champs or payload.disabled or perd:
        await store.close_sessions_of(user_id)
    return fiche


@router.delete("/users/{user_id}", summary="Delete an account (edit rights)", status_code=204)
async def delete_user(
    user_id: Annotated[int, Path(ge=1)], editor: EditorDep, container: ContainerDep
) -> None:
    store = _store(container)
    try:
        cible = await store.get(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _garde_un_editeur(store, cible, perd=True)
    await store.delete(user_id)
    forget_cached_sessions()

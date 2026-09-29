"""Connexion unique (SSO) par OpenID Connect.

L'EQUIPE SE CONNECTE AVEC LE COMPTE QU'ELLE A DEJA : Google Workspace,
Microsoft Entra ID, Keycloak, Authentik -- tout fournisseur OpenID Connect.

CE QUI EST VERIFIE
------------------
- ``state`` (aleatoire, lie au navigateur par un cookie) : la reponse revient
  bien de la demande que CE navigateur a faite -- sinon un tiers pourrait
  connecter la victime a son propre compte.
- PKCE : le code d'autorisation ne s'echange qu'avec le secret que seul ce
  serveur connait pour cette demande.
- L'email vient du point ``userinfo`` du fournisseur, interroge en TLS avec le
  jeton qu'il vient de delivrer : c'est lui qui fait foi, pas un parametre
  d'URL. Un email que le fournisseur n'a pas verifie est refuse.

QUI PEUT ENTRER
---------------
Par defaut, seul un email qui a DEJA un compte freeQoS (cree par un editeur) :
le SSO prouve l'identite, l'editeur decide des droits. ``OIDC_AUTO_CREATE_ROLE``
peut ouvrir la creation automatique, en lecture ou en edition.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.api.accounts import _ouvre_session, _store
from app.api.deps import ContainerDep
from app.services.accounts import hash_password, normalise_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["accounts"])

COOKIE_ETAT = "freeqos_sso_state"
ETAT_TTL_S = 600.0

#: Demandes en cours : state -> (verificateur PKCE, heure). En memoire : une
#: connexion SSO tient en quelques secondes, un redemarrage la fait recommencer.
_EN_COURS: dict[str, tuple[str, float]] = {}
#: Documents de decouverte des fournisseurs, relus au plus toutes les heures.
_DECOUVERTE: dict[str, tuple[dict[str, Any], float]] = {}


def sso_enabled(settings: Any) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def _retour(request: Request, settings: Any) -> str:
    if settings.oidc_redirect_url:
        return str(settings.oidc_redirect_url)
    return str(request.url_for("oidc_callback"))


async def _decouverte(issuer: str, client: httpx.AsyncClient) -> dict[str, Any]:
    connu = _DECOUVERTE.get(issuer)
    if connu is not None and time.monotonic() - connu[1] < 3600:
        return connu[0]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    reponse = await client.get(url)
    reponse.raise_for_status()
    doc: dict[str, Any] = reponse.json()
    _DECOUVERTE[issuer] = (doc, time.monotonic())
    return doc


def _client_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


def _echec(message: str) -> RedirectResponse:
    logger.warning("Connexion SSO refusee : %s", message)
    return RedirectResponse("/?sso_error=" + urlencode({"m": message})[2:], status_code=303)


@router.get("/auth/oidc/start", summary="Start a Single Sign-On login")
async def oidc_start(request: Request, container: ContainerDep) -> RedirectResponse:
    settings = container.settings
    if not sso_enabled(settings):
        return _echec("Single Sign-On is not configured")
    maintenant = time.monotonic()
    for cle, (_v, t) in list(_EN_COURS.items()):
        if maintenant - t > ETAT_TTL_S:
            del _EN_COURS[cle]
    try:
        async with _client_http() as client:
            doc = await _decouverte(str(settings.oidc_issuer), client)
    except Exception as exc:  # noqa: BLE001
        return _echec(f"identity provider unreachable ({type(exc).__name__})")
    etat = secrets.token_urlsafe(24)
    verificateur = secrets.token_urlsafe(48)
    defi = (
        base64.urlsafe_b64encode(hashlib.sha256(verificateur.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    _EN_COURS[etat] = (verificateur, maintenant)
    parametres = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": _retour(request, settings),
        "scope": "openid email profile",
        "state": etat,
        "code_challenge": defi,
        "code_challenge_method": "S256",
    }
    reponse = RedirectResponse(
        str(doc["authorization_endpoint"]) + "?" + urlencode(parametres), status_code=303
    )
    # SameSite=Lax : le retour du fournisseur est une navigation venue d'un
    # autre site ; un cookie Strict n'y serait pas joint.
    reponse.set_cookie(
        COOKIE_ETAT,
        etat,
        max_age=int(ETAT_TTL_S),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return reponse


@router.get("/auth/oidc/callback", summary="Single Sign-On return", name="oidc_callback")
async def oidc_callback(
    request: Request,
    container: ContainerDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = container.settings
    if error:
        return _echec(f"the identity provider refused: {error}")
    if not sso_enabled(settings) or not code or not state:
        return _echec("incomplete Single Sign-On answer")
    attendu = request.cookies.get(COOKIE_ETAT)
    demande = _EN_COURS.pop(state, None)
    if not attendu or attendu != state or demande is None:
        return _echec("this login was not started from this browser, or it expired")
    verificateur, depart = demande
    if time.monotonic() - depart > ETAT_TTL_S:
        return _echec("the login took too long, start again")

    secret = settings.oidc_client_secret.get_secret_value() if settings.oidc_client_secret else None
    try:
        async with _client_http() as client:
            doc = await _decouverte(str(settings.oidc_issuer), client)
            corps = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _retour(request, settings),
                "client_id": settings.oidc_client_id,
                "code_verifier": verificateur,
            }
            if secret:
                corps["client_secret"] = secret
            jeton = await client.post(str(doc["token_endpoint"]), data=corps)
            jeton.raise_for_status()
            acces = jeton.json().get("access_token")
            if not acces:
                return _echec("no access token from the identity provider")
            profil = await client.get(
                str(doc["userinfo_endpoint"]), headers={"Authorization": f"Bearer {acces}"}
            )
            profil.raise_for_status()
            infos = profil.json()
    except Exception as exc:  # noqa: BLE001
        return _echec(f"exchange with the identity provider failed ({type(exc).__name__})")

    if infos.get("email_verified") is False:
        return _echec("the identity provider has not verified this email")
    try:
        email = normalise_email(str(infos.get("email") or infos.get("preferred_username") or ""))
    except ValueError:
        return _echec("the identity provider gave no usable email")

    store = _store(container)
    fiche = await store.credentials(email)
    if fiche is None:
        role = settings.oidc_auto_create_role
        if role == "none":
            return _echec(f"{email} has no freeQoS account: ask an editor to create it")
        fiche = await store.create(
            email=email,
            # Un mot de passe que personne ne connait : ce compte entre en SSO.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role=role,
            created_by="sso",
        )
    if fiche.get("disabled"):
        return _echec(f"the account {email} is disabled")
    reponse = RedirectResponse("/", status_code=303)
    await _ouvre_session(request, reponse, container, fiche)
    reponse.delete_cookie(COOKIE_ETAT, path="/")
    logger.info("Connexion SSO : %s", email)
    return reponse

"""Connexion unique OpenID Connect, contre un faux fournisseur complet."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sso
from app.api.deps import get_container
from app.config import Settings
from app.db.users_repo import InMemoryUsersRepository
from app.main import register_routes
from app.services.accounts import hash_password
from tests.test_api import build_container

ISSUER = "https://idp.example"


def fournisseur(profil: dict[str, Any], vu: dict[str, Any]) -> httpx.MockTransport:
    def repondre(requete: httpx.Request) -> httpx.Response:
        chemin = requete.url.path
        if chemin == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                },
            )
        if chemin == "/token":
            vu["token"] = parse_qs(requete.content.decode())
            return httpx.Response(200, json={"access_token": "jeton-idp"})
        if chemin == "/userinfo":
            vu["bearer"] = requete.headers.get("authorization")
            return httpx.Response(200, json=profil)
        return httpx.Response(404)

    return httpx.MockTransport(repondre)


@pytest.fixture
def monter(settings: Settings, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def fabrique(profil: dict[str, Any], **reglages: Any) -> tuple[TestClient, Any, dict[str, Any]]:
        sso._DECOUVERTE.clear()
        vu: dict[str, Any] = {}
        transport = fournisseur(profil, vu)
        monkeypatch.setattr(sso, "_client_http", lambda: httpx.AsyncClient(transport=transport))
        cfg = settings.model_copy(
            update={
                "auth_enabled": True,
                "oidc_issuer": ISSUER,
                "oidc_client_id": "freeqos",
                **reglages,
            }
        )
        container = build_container(cfg)
        users = InMemoryUsersRepository()
        container.users_repo = users
        app = FastAPI()
        app.state.settings = cfg
        register_routes(app, cfg)
        app.dependency_overrides[get_container] = lambda: container
        return TestClient(app, base_url="https://testserver", follow_redirects=False), users, vu

    return fabrique


def aller_retour(client: TestClient) -> httpx.Response:
    depart = client.get("/api/v1/auth/oidc/start")
    assert depart.status_code == 303
    cible = urlsplit(depart.headers["location"])
    params = parse_qs(cible.query)
    assert cible.netloc == "idp.example" and params["code_challenge_method"] == ["S256"]
    return client.get(f"/api/v1/auth/oidc/callback?code=abc&state={params['state'][0]}")


def test_un_compte_existant_entre_par_le_sso(monter) -> None:  # type: ignore[no-untyped-def]
    client, users, vu = monter({"email": "Tech@Exemple.fr", "email_verified": True})
    users.users[1] = {
        "id": 1,
        "email": "tech@exemple.fr",
        "password_hash": hash_password("x" * 9),
        "role": "read",
        "disabled": False,
        "created_by": "admin",
        "created_at": None,
        "updated_at": None,
        "last_login_at": None,
    }
    retour = aller_retour(client)
    assert retour.status_code == 303 and retour.headers["location"] == "/"
    assert "freeqos_session" in retour.headers.get("set-cookie", "")
    assert vu["bearer"] == "Bearer jeton-idp"
    assert vu["token"]["code_verifier"]  # PKCE
    assert client.get("/api/v1/auth/status").json()["user"]["email"] == "tech@exemple.fr"


def test_un_email_inconnu_est_refuse_par_defaut(monter) -> None:  # type: ignore[no-untyped-def]
    client, users, _ = monter({"email": "inconnu@exemple.fr", "email_verified": True})
    retour = aller_retour(client)
    assert "sso_error" in retour.headers["location"]
    assert users.users == {}


def test_la_creation_automatique_se_choisit(monter) -> None:  # type: ignore[no-untyped-def]
    client, users, _ = monter({"email": "nouveau@exemple.fr"}, oidc_auto_create_role="read")
    assert aller_retour(client).headers["location"] == "/"
    (fiche,) = users.users.values()
    assert fiche["role"] == "read" and fiche["created_by"] == "sso"


def test_un_email_non_verifie_est_refuse(monter) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = monter(
        {"email": "x@exemple.fr", "email_verified": False}, oidc_auto_create_role="edit"
    )
    assert "sso_error" in aller_retour(client).headers["location"]


def test_un_retour_sans_la_demande_de_ce_navigateur_est_refuse(monter) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = monter({"email": "x@exemple.fr"}, oidc_auto_create_role="edit")
    client.get("/api/v1/auth/oidc/start")
    retour = client.get("/api/v1/auth/oidc/callback?code=abc&state=forge")
    assert "sso_error" in retour.headers["location"]


def test_le_statut_annonce_le_sso(monter) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = monter({}, oidc_label="Sign in with Google")
    assert client.get("/api/v1/auth/status").json()["sso"] == {
        "enabled": True,
        "label": "Sign in with Google",
    }

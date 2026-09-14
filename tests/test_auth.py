"""Authentification de l'API (P0-1).

Le test central verifie le critere de sortie : AUCUN endpoint de l'API n'est
joignable sans identite. Les autres couvrent la connexion, les cles d'API, la
limitation de debit, les droits admin, ainsi que les en-tetes de securite et le
CORS jamais ouvert a tout vent.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.main import create_app, register_routes
from app.services.auth import (
    AuthService,
    InMemoryAuthStore,
    InvalidCredentialsError,
    RateLimitedError,
)
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, make_test_auth
from tests.test_api import build_container

LOGIN_PATH = "/api/v1/auth/login"


def _mount(settings: Settings):
    container = build_container(settings)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return app, container


def _protected_operations(app: FastAPI, prefix: str) -> list[tuple[str, str]]:
    """Toutes les operations (methode, url) de l'API, hors la connexion anonyme.

    On lit le schema OpenAPI : il enumere toutes les operations documentees,
    quelle que soit la maniere dont la version de FastAPI structure ``app.routes``.
    """
    operations: list[tuple[str, str]] = []
    for path, methods in app.openapi().get("paths", {}).items():
        if not path.startswith(prefix) or path == LOGIN_PATH:
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            verb = method.upper()
            if verb in {"HEAD", "OPTIONS", "TRACE"}:
                continue
            operations.append((verb, url))
    return operations


def test_aucun_endpoint_api_accessible_sans_identite(settings: Settings) -> None:
    """Critere de sortie P0-1 : 0 endpoint sur les ~55 joignable anonymement."""
    app, _ = _mount(settings)
    anon = TestClient(app)

    operations = _protected_operations(app, settings.api_prefix)
    # L'inventaire des endpoints proteges doit couvrir toute la surface d'ecriture
    # et de lecture (les ~55 chemins v1) : un garde pose au mauvais endroit se
    # verrait ici.
    assert len(operations) >= 50, f"trop peu d'endpoints inspectes : {len(operations)}"

    for method, url in operations:
        response = anon.request(method, url)
        assert response.status_code == 401, f"{method} {url} devrait exiger une identite"


def test_login_anonyme_mais_le_reste_non(settings: Settings) -> None:
    """La connexion est le seul point d'entree anonyme ; tout le reste est ferme."""
    app, container = _mount(settings)
    asyncio.run(container.auth.create_user("alice", "motdepasse-solide", is_admin=True))
    client = TestClient(app)

    # Sans identite : refus.
    assert client.get("/api/v1/pops").status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401

    # Connexion : pose un cookie de session SameSite=Strict / HttpOnly.
    login = client.post(LOGIN_PATH, json={"username": "alice", "password": "motdepasse-solide"})
    assert login.status_code == 200
    assert login.json()["identity"]["name"] == "alice"
    set_cookie = login.headers.get("set-cookie", "")
    assert "freeqos_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()

    # Le cookie donne acces au reste de l'API.
    assert client.get("/api/v1/pops").status_code == 200
    assert client.get("/api/v1/auth/me").json()["name"] == "alice"

    # Deconnexion : la session est revoquee, l'acces retombe a 401.
    assert client.post("/api/v1/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/pops").status_code == 401


def test_mauvais_mot_de_passe_refuse(settings: Settings) -> None:
    app, container = _mount(settings)
    asyncio.run(container.auth.create_user("alice", "le-bon-mot-de-passe", is_admin=True))
    client = TestClient(app)

    reponse = client.post(LOGIN_PATH, json={"username": "alice", "password": "faux"})
    assert reponse.status_code == 401
    # Le message ne trahit pas l'existence du compte.
    assert "incorrect" in reponse.json()["detail"].lower()


def test_cle_d_api_pour_les_appels_machine(settings: Settings) -> None:
    """Une cle d'API donne acces sans cookie ; une cle inconnue est refusee."""
    app, _ = _mount(settings)
    client = TestClient(app)

    assert client.get("/api/v1/pops", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/api/v1/pops", headers={"X-API-Key": "fq_pas-la-bonne"}).status_code == 401
    # Egalement accepte en Bearer.
    bearer = {"Authorization": f"Bearer {TEST_API_KEY}"}
    assert client.get("/api/v1/pops", headers=bearer).status_code == 200


def test_endpoints_admin_refuses_a_un_non_admin(settings: Settings) -> None:
    """La gestion des comptes et des cles est reservee aux administrateurs."""
    store = InMemoryAuthStore()
    auth = AuthService(store)
    # Une cle d'API NON admin.
    _, secret = asyncio.run(auth.create_api_key("lecture", is_admin=False, created_by="tests"))

    container = build_container(settings)
    container.auth = auth
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    client = TestClient(app, headers={"X-API-Key": secret})

    # Acces en lecture : autorise (identite valide).
    assert client.get("/api/v1/pops").status_code == 200
    # Gestion des comptes : reservee aux admins -> 403.
    assert client.get("/api/v1/auth/users").status_code == 403
    assert client.get("/api/v1/auth/api-keys").status_code == 403


def test_connexion_limitee_en_debit() -> None:
    """Anti-bourrage : au-dela du seuil, la connexion est refusee un temps."""
    auth = AuthService(
        InMemoryAuthStore(), login_max_attempts=3, login_window_s=300, rate_clock=lambda: 0.0
    )
    asyncio.run(auth.create_user("bob", "un-mot-de-passe", is_admin=False))

    async def scenario() -> None:
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                await auth.authenticate("bob", "faux")
        # La 4e tentative est bloquee AVANT meme de verifier le mot de passe.
        with pytest.raises(RateLimitedError):
            await auth.authenticate("bob", "un-mot-de-passe")

    asyncio.run(scenario())


def test_cle_d_api_secret_montre_une_seule_fois() -> None:
    """Le secret n'est renvoye qu'a la creation ; il n'est jamais relu."""
    auth = make_test_auth()

    async def scenario() -> None:
        record, secret = await auth.create_api_key("ci", is_admin=True, created_by="tests")
        assert secret.startswith("fq_")
        listed = await auth.list_api_keys()
        # La liste expose le prefixe, jamais le secret ni son hash.
        entry = next(e for e in listed if e["id"] == record.id)
        assert "secret" not in entry
        assert "key_hash" not in entry
        assert entry["prefix"] == secret[:12]

    asyncio.run(scenario())


# ---------------------------------------------------- en-tetes & CORS
def test_entetes_de_securite_sur_chaque_reponse(settings: Settings) -> None:
    app = create_app(settings)
    client = TestClient(app)  # sans 'with' : pas de cycle de vie, /health suffit

    response = client.get("/health")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "strict-transport-security" in response.headers


def test_cors_ne_reflete_jamais_une_origine_inconnue(settings: Settings) -> None:
    settings.cors_allow_origins = ["https://ops.example"]
    app = create_app(settings)
    client = TestClient(app)

    autorise = client.get("/health", headers={"Origin": "https://ops.example"})
    assert autorise.headers.get("access-control-allow-origin") == "https://ops.example"

    inconnu = client.get("/health", headers={"Origin": "https://pirate.example"})
    origin = inconnu.headers.get("access-control-allow-origin")
    assert origin != "*"
    assert origin != "https://pirate.example"


def test_cors_refuse_le_joker_en_configuration() -> None:
    """'*' est refuse explicitement : cookies + ecriture ne se marient pas avec
    une origine joker."""
    with pytest.raises(ValueError, match="jamais valoir"):
        Settings(_env_file=None, cors_allow_origins="*")

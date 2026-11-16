"""Audit de securite : ce qu'un inconnu qui atteint le port peut apprendre.

CE QUE CES TESTS FIXENT
-----------------------
1. AUCUNE ROUTE NE REPOND SANS CONNEXION, hormis une liste fermee : la sonde,
   l'etat de connexion, la connexion elle-meme. Une route ajoutee demain sans
   garde fait echouer ce test.
2. LA CARTE DE L'API EST PRIVEE : ``/openapi.json`` et ``/docs`` exigent une
   session ; ``/redoc`` n'existe plus.
3. LA SONDE DE DISPONIBILITE NE RACONTE RIEN A UN INCONNU : son code (200 /
   503) reste lisible par un superviseur, mais le detail (jobs, erreurs des
   collecteurs, routeurs) est reserve a une session ou a une cle.
4. PAS DE CORS JOKER : aucun autre site ne lit les reponses de l'API, quel que
   soit APP_ENV.
5. LE CODE SERVI AU NAVIGATEUR NE CONTIENT AUCUN SECRET : ni cle d'API, ni
   jeton, ni mot de passe ecrit en dur.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.db.users_repo import InMemoryUsersRepository
from app.main import create_app, register_routes
from app.web.ui import STATIC_DIR, TEMPLATES_DIR
from tests.test_api import build_container

MDP = "correct-horse-1"
ADMIN = "admin@exemple.fr"

#: Les SEULES routes qu'un inconnu peut appeler. Toute autre doit refuser.
OUVERTES = {
    ("GET", "/health"),
    ("GET", "/health/ready"),
    ("GET", "/api/v1/auth/status"),
    ("POST", "/api/v1/auth/setup"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
}


@pytest.fixture
def client(settings: Settings) -> TestClient:
    reglages = settings.model_copy(update={"auth_enabled": True})
    container = build_container(reglages)
    container.users_repo = InMemoryUsersRepository()
    # Comme create_app : les routes de documentation integrees sont coupees.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = reglages
    register_routes(app, reglages)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app, base_url="https://testserver")


def connecter(client: TestClient) -> None:
    r = client.post("/api/v1/auth/setup", json={"email": ADMIN, "password": MDP})
    assert r.status_code == 200, r.text


# ================================================ 1. tout exige une connexion


def test_aucune_route_ne_repond_sans_connexion(client: TestClient) -> None:
    chemins = client.app.openapi()["paths"]
    assert sum(len(ops) for ops in chemins.values()) > 150
    ouvertes = []
    for chemin, operations in chemins.items():
        url = re.sub(r"\{[^}]+\}", "1", chemin)
        for methode in operations:
            if (methode.upper(), chemin) in OUVERTES:
                continue
            r = client.request(methode.upper(), url, json={})
            if r.status_code not in (401, 403):
                ouvertes.append(f"{methode.upper()} {chemin} -> {r.status_code}")
    assert not ouvertes, ouvertes


# ===================================================== 2. la carte de l'API


def test_le_schema_de_l_api_exige_une_connexion(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 401
    r = client.get("/docs", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.get("/redoc").status_code == 404


def test_connecte_le_schema_et_swagger_s_ouvrent(client: TestClient) -> None:
    connecter(client)
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/auth/status" in schema.json()["paths"]
    page = client.get("/docs")
    assert page.status_code == 200
    assert "swagger-ui" in page.text
    assert "/openapi.json" in page.text


def test_create_app_ne_publie_pas_la_documentation_integree() -> None:
    app = create_app(Settings(_env_file=None, auth_enabled=True))
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    # Le schema reste genere (le guide et les tests s'en servent).
    assert "/health" in app.openapi()["paths"]


# ============================================== 3. la sonde ne dit que l'etat


def test_la_sonde_ne_detaille_rien_a_un_inconnu(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "database": "ok"}


def test_la_sonde_detaille_tout_a_une_session(client: TestClient) -> None:
    connecter(client)
    corps = client.get("/health/ready").json()
    assert corps["status"] == "ready"
    assert "jobs" in corps
    assert "routers_in_file" in corps


def test_la_sonde_garde_son_code_503_sans_connexion(settings: Settings) -> None:
    reglages = settings.model_copy(update={"auth_enabled": True})
    container = build_container(reglages, db_reachable=False)
    container.users_repo = InMemoryUsersRepository()
    app = FastAPI()
    app.state.settings = reglages
    register_routes(app, reglages)
    app.dependency_overrides[get_container] = lambda: container
    r = TestClient(app).get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "database": "unreachable"}


# =========================================================== 4. pas de joker


@pytest.mark.parametrize("env", ["lab", "prod"])
def test_aucun_autre_site_ne_lit_l_api(env: str) -> None:
    app = create_app(Settings(_env_file=None, app_env=env, auth_enabled=True))
    r = TestClient(app).get("/health", headers={"Origin": "https://attaquant.example"})
    assert "access-control-allow-origin" not in r.headers


def test_une_origine_nommee_est_acceptee() -> None:
    app = create_app(
        Settings(_env_file=None, cors_allow_origins=["https://crm.example.com"], auth_enabled=True)
    )
    r = TestClient(app).get("/health", headers={"Origin": "https://crm.example.com"})
    assert r.headers.get("access-control-allow-origin") == "https://crm.example.com"


# ================================================ 5. aucun secret au navigateur

#: Ce qui ressemble a un secret ecrit en dur dans du code servi au navigateur.
SECRETS_EN_DUR = (
    re.compile(r"fqos_[0-9a-f]{8}_[A-Za-z0-9]{32}"),  # cle d'API freeQoS
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"""(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['"][^'"\s]{12,}['"]"""),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def _fichiers_servis() -> list[Path]:
    return [
        f
        for dossier in (STATIC_DIR, TEMPLATES_DIR)
        for f in dossier.rglob("*")
        if f.is_file() and f.suffix in {".js", ".html", ".css", ".json", ".svg"}
    ]


def test_le_code_servi_ne_contient_aucun_secret() -> None:
    fichiers = _fichiers_servis()
    assert any(f.name == "app.js" for f in fichiers)
    trouves = []
    for fichier in fichiers:
        texte = fichier.read_text(encoding="utf-8", errors="replace")
        for motif in SECRETS_EN_DUR:
            for m in motif.finditer(texte):
                trouves.append(f"{fichier.name}: {m.group(0)[:40]}")
    assert not trouves, trouves


def test_le_navigateur_ne_garde_aucun_secret_en_local() -> None:
    """localStorage / sessionStorage ne recoivent ni session, ni cle, ni mot de
    passe : la session vit dans un cookie HttpOnly que le script ne lit pas."""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    ecritures = re.findall(r"(?:localStorage|sessionStorage)\.setItem\(([^,]+),", js)
    assert ecritures
    for cle in ecritures:
        assert not re.search(r"(?i)token|secret|password|mdp|session|api.?key|cle", cle), cle
    assert "document.cookie" not in js

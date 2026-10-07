"""Le guide de l'API (/api-guide) : servi, autonome, et JUSTE.

DEMANDE : « une doc complete sur l'utilisation de l'API, pas brute ». Un guide
qui cite une route qui n'existe plus est pire que pas de guide : chaque chemin
qu'il mentionne doit exister dans l'API reellement exposee.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import register_routes


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    return TestClient(app)


def test_le_guide_est_servi_et_autonome(client: TestClient) -> None:
    for chemin in ("/api-guide", "/static/api-guide.js", "/static/api-guide.css"):
        reponse = client.get(chemin)
        assert reponse.status_code == 200, chemin
        # Une VM coupee d'internet doit afficher le guide en entier.
        assert "https://" not in reponse.text
        assert "//cdn" not in reponse.text and "//unpkg" not in reponse.text
    page = client.get("/api-guide").text
    for section in (
        "Authentication",
        "Add a router",
        "Client plans",
        "Forced limits",
        "Billing integration",
        "Consumption",
        "Every endpoint",
    ):
        assert section in page
    # Jamais l'adresse du serveur : un nom generique.
    assert "&lt;your-freeqos-url&gt;" in page


def _motif(chemin: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^/]*?\\\}", "[^/]+", re.escape(chemin)) + "$")


def test_chaque_route_citee_existe(client: TestClient) -> None:
    page = client.get("/api-guide").text
    cites = {
        c.rstrip(".,:;)") for c in re.findall(r"/(?:api|model|usage)/v1[A-Za-z0-9_\-./{}]*", page)
    }
    # Le prefixe seul (« l'API /api/v1 ») n'est pas une route.
    cites.discard("/api/v1")
    assert len(cites) > 30
    routes = [_motif(p) for p in client.get("/openapi.json").json()["paths"]]
    # Un chemin cite avec un exemple ({id} remplace par svc-42) doit aussi passer.
    manquants = [
        c for c in sorted(cites) if not any(r.match(re.sub(r"\{[^/]*\}", "x", c)) for r in routes)
    ]
    assert not manquants, manquants


def test_l_onglet_api_renvoie_au_guide(client: TestClient) -> None:
    assert 'href="/api-guide"' in client.get("/").text

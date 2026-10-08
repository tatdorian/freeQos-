"""La documentation technique (/documentation et son PDF) : servie, autonome,
a jour de sa source, et JUSTE sur les routes qu'elle cite.

DEMANDE : « une documentation tres pointue sur freeQoS, en anglais, integree a
l'app, et en PDF ». La page est generee depuis docs/ par scripts/build_docs.py ;
une page qui ne correspond plus a sa source serait une documentation fausse.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import register_routes

RACINE = Path(__file__).resolve().parent.parent
GABARIT = RACINE / "app" / "web" / "templates" / "documentation.html"

CHAPITRES = (
    "1. Overview",
    "2. Architecture",
    "3. Installation and updates",
    "4. Routers and inventory",
    "5. Measurements: throughput, latency, QoE",
    "6. NetFlow traffic",
    "7. Plans and shaping",
    "8. Interface",
    "9. API",
    "10. Security",
    "11. Operations",
    "12. Troubleshooting",
    "13. Settings reference",
)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    return TestClient(app)


def test_la_documentation_est_servie_et_complete(client: TestClient) -> None:
    reponse = client.get("/documentation")
    assert reponse.status_code == 200
    page = reponse.text
    for chapitre in CHAPITRES:
        assert f">{chapitre}</h2>" in page, chapitre
    # Les quatre schemas sont dans la page elle-meme, pas en images distantes.
    for schema in ("architecture", "network-flows", "netflow-pipeline", "shaping"):
        assert f'id="fig-{schema}"' in page, schema
    assert page.count("<svg") == 4
    # Jamais l'adresse d'un serveur reel : un nom generique.
    assert "&lt;your-freeqos-url&gt;" in page
    assert 'href="/documentation.pdf"' in page


def test_la_page_ne_charge_rien_de_l_exterieur(client: TestClient) -> None:
    """Une VM de management coupee d'internet doit l'afficher en entier."""
    page = client.get("/documentation").text
    assert not re.search(r"<script[^>]+src=\"(?:https?:)?//", page)
    assert not re.search(r"<link[^>]+href=\"(?:https?:)?//", page)
    for statique in ("/static/docs.css", "/static/docs.js", "/static/api-guide.css"):
        assert client.get(statique).status_code == 200, statique


def test_le_pdf_est_servi(client: TestClient) -> None:
    reponse = client.get("/documentation.pdf")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "application/pdf"
    assert reponse.content.startswith(b"%PDF")
    assert "freeqos-documentation.pdf" in reponse.headers.get("content-disposition", "")


def _motif(chemin: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^/]*?\\\}", "[^/]+", re.escape(chemin)) + "$")


def test_chaque_route_citee_existe(client: TestClient) -> None:
    page = client.get("/documentation").text
    cites = {
        c.rstrip(".,:;)`") for c in re.findall(r"/(?:api|model|usage)/v1[A-Za-z0-9_\-./{}]*", page)
    }
    cites -= {"/api/v1", "/model/v1", "/usage/v1"}
    assert len(cites) > 20
    routes = [_motif(p) for p in client.get("/openapi.json").json()["paths"]]
    # /model/v1 est lui-meme une route (verification d'une cle).
    routes.append(re.compile("^/model/v1$"))
    manquants = [
        c for c in sorted(cites) if not any(r.match(re.sub(r"\{[^/]*\}", "x", c)) for r in routes)
    ]
    assert not manquants, manquants


def test_la_page_correspond_a_sa_source() -> None:
    """La page versionnee est exactement ce que le script produit aujourd'hui.

    Si ce test echoue : ``python scripts/build_docs.py --pdf``, puis versionner
    la page ET le PDF.
    """
    pytest.importorskip("markdown")
    sys.path.insert(0, str(RACINE / "scripts"))
    try:
        import build_docs
    finally:
        sys.path.pop(0)
    assert GABARIT.read_text(encoding="utf-8") == build_docs.rendre_gabarit()


def test_l_interface_renvoie_a_la_documentation(client: TestClient) -> None:
    accueil = client.get("/").text
    assert 'href="/documentation"' in accueil
    assert 'href="/documentation.pdf"' in accueil
    assert 'href="/documentation"' in client.get("/api-guide").text

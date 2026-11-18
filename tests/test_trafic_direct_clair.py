"""Dashboard › Live traffic per router : lisible d'un coup d'oeil.

Trois tuiles titrees en clair (Internet link, Your clients, Other traffic),
chacune avec une phrase qui dit ce que c'est ; les sens ecrits en toutes
lettres (Download / Upload), pas une fleche seule ; le detail des ports
replie ; l'etat de la mesure dit QUAND les routeurs ont ete lus.
"""

from __future__ import annotations

from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")


def _rendu() -> str:
    debut = JS.index("async function loadPortsLive()")
    return JS[debut : JS.index("/* Filtres du graphe de debit", debut)]


def test_titre_et_phrase_d_introduction() -> None:
    assert "<h2>Live traffic per router</h2>" in HTML
    assert "Routers &mdash; live traffic" not in HTML


def test_trois_tuiles_en_clair() -> None:
    corps = _rendu()
    for titre in ("'Internet link'", "'Your clients'", "'Other traffic'"):
        assert titre in corps, titre
    assert "Download <b>" in corps and "Upload <b>" in corps
    assert "Not from clients" not in corps and "Measurement up to date" not in corps
    assert "Read live from the routers" in corps


def test_le_detail_des_ports_est_replie() -> None:
    corps = _rendu()
    assert '<details class="pl-ports-d"' in corps
    assert "Connected to" in corps and "Port use" in corps

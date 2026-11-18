"""Traffic › Destinations : une carte qui se parcourt simplement.

CE QUI EST FIXE ICI
-------------------
1. LA CARTE REMPLIT SON CADRE. Elle partageait la classe ``.map-wrap`` avec la
   page Map, dont la regle lui imposait une colonne vide de 320 px a droite
   (et des boutons de zoom qui flottaient dans le vide).
2. ELLE S'OUVRE SUR LE TRAFIC, pas sur le monde entier -- et le cadrage attend
   que la carte ait une taille (elle est dessinee dans un onglet cache).
3. ON S'Y DEPLACE SANS CHERCHER : regions en un clic (seulement celles ou il
   y a du trafic), glisser a la souris ou au doigt, molette, double-clic,
   pincement, clavier ; les deplacements sont animes.
4. UN CLIC SUR UN CERCLE RESTE UN CLIC : le pointeur n'est capture qu'une
   fois le glissement commence.
"""

from __future__ import annotations

from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def _carte() -> str:
    debut = JS.index("async function renderTrafficMap(host, data)")
    return JS[debut : JS.index("function showMapTip(event, p)")]


def test_la_carte_a_son_propre_cadre() -> None:
    corps = _carte()
    assert "'<div class=\"tmap-wrap\">'" in corps
    assert 'class="map-wrap"' not in corps
    assert ".tmap-wrap .worldmap { width: 100%; height: 100%;" in CSS


def test_elle_s_ouvre_sur_le_trafic_quand_elle_est_visible() -> None:
    corps = _carte()
    assert "if (svg.getBoundingClientRect().width < 2) return;" in corps
    assert "if (cadrageAuto) { cadrageAuto = false; vue = cadreDe(points); }" in corps
    assert "new ResizeObserver(() => applique())" in corps


def test_les_gestes_de_navigation() -> None:
    corps = _carte()
    for geste in ("'wheel'", "'dblclick'", "'pointerdown'", "'pointermove'", "'keydown'"):
        assert geste in corps, geste
    # Pincement a deux doigts.
    assert "apres.length >= 2" in corps
    # Regions : seulement celles ou il y a du trafic.
    assert ".filter((z) => z.n > 0)" in corps
    assert "const MAP_ZONES = [" in JS


def test_un_clic_sur_un_cercle_reste_un_clic() -> None:
    corps = _carte()
    appui = corps[corps.index("svg.addEventListener('pointerdown'") :]
    appui = appui[: appui.index("});")]
    assert "setPointerCapture" not in appui
    assert "if (aBouge) return;" in corps

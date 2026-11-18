"""Network health (ex-Executive) : une page claire.

Ce qui est fixe ici : des noms de page qui disent ce qu'on y trouve, aucun
jargon visible (node, RTT probe, QoO, subscriber, PPP profile), une case
« Measure latency » plutot qu'une case technique, et, cote Business insights,
ce qui manque replie en une ligne qui dit quoi faire -- plus de grand bandeau.
"""

from __future__ import annotations

from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")


def _section(vue: str) -> str:
    debut = HTML.index(f'<section id="view-{vue}">')
    return HTML[debut : HTML.index("</section>", debut)]


def test_les_noms_de_page() -> None:
    assert "<span>Network health</span></a>" in HTML
    assert "<span>Executive</span>" not in HTML
    assert "<span>Insights</span>" not in HTML
    # Business insights a ete retiree (jugee inutile).
    assert "Business insights" not in HTML


def test_pas_de_jargon_dans_network_health() -> None:
    page = _section("exec")
    anciens = (
        "RTT probe",
        "Network at a glance",
        "Load by node",
        "Queues by node",
        "Saturation risks",
    )
    for mot in anciens:
        assert mot not in page, mot
    assert "Measure latency" in page
    assert ">Load per site<" in page and ">Over time<" in page
    # Les textes affiches par le code.
    jargon = (
        "RTT probe off",
        "QoO and bufferbloat",
        "Busiest node",
        "No saturated node",
        "' node(s)",
    )
    for mot in jargon:
        assert mot not in JS, mot

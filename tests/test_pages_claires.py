"""Network health (ex-Executive) et Business insights (ex-Insights) : des pages claires.

Ce qui est fixe ici : des noms de page qui disent ce qu'on y trouve, aucun
jargon visible (node, RTT probe, QoO, subscriber, PPP profile), une case
« Measure latency » plutot qu'une case technique, et, cote Business insights,
ce qui manque replie en une ligne qui dit quoi faire -- plus de grand bandeau.
"""

from __future__ import annotations

import re

from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
HTML = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")


def _section(vue: str) -> str:
    debut = HTML.index(f'<section id="view-{vue}">')
    return HTML[debut : HTML.index("</section>", debut)]


def test_les_noms_de_page() -> None:
    assert "<span>Network health</span></a>" in HTML
    assert "<span>Business insights</span></a>" in HTML
    assert "<span>Executive</span>" not in HTML
    assert "<span>Insights</span>" not in HTML
    # Network health d'abord dans le menu.
    assert HTML.index("<span>Network health</span>") < HTML.index("<span>Business insights</span>")


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


def test_business_insights_dit_quoi_faire() -> None:
    assert "<b>To do:</b> call them first." in JS
    assert "<b>To do:</b> offer them more." in JS
    corps = JS[JS.index("function renderInsightsNotice(r, jours)") :]
    corps = corps[: corps.index("\n}\n")]
    assert "Some lists are not complete yet" in corps
    assert "<details" in corps
    assert "PPP profile" not in corps
    assert "notice warn" not in corps
    assert not re.search(r"subscriber\(s\)", corps)

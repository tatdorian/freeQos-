"""Genere la documentation technique : la page /documentation et son PDF.

UNE SEULE SOURCE
----------------
Le texte vit dans ``docs/freeqos-documentation.md`` et les schemas dans
``docs/diagrams/*.svg``. Ce script en tire :

  - ``app/web/templates/documentation.html``, servi par /documentation ;
  - ``app/web/static/freeqos-documentation.pdf`` (option ``--pdf``), servi par
    /documentation.pdf.

La page est GENEREE A L'AVANCE et versionnee : le serveur n'a besoin ni de
``markdown`` ni d'un navigateur pour la servir, et une VM coupee d'internet
l'affiche en entier. Un test verifie qu'elle correspond toujours a la source.

Usage :
    python scripts/build_docs.py          # la page seule
    python scripts/build_docs.py --pdf    # la page et le PDF (Playwright + Chromium)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
from pathlib import Path
from typing import Any

import markdown

RACINE = Path(__file__).resolve().parent.parent
SOURCE = RACINE / "docs" / "freeqos-documentation.md"
SCHEMAS = RACINE / "docs" / "diagrams"
GABARIT = RACINE / "app" / "web" / "templates" / "documentation.html"
STATIQUE = RACINE / "app" / "web" / "static"
PDF = STATIQUE / "freeqos-documentation.pdf"

TITRE = "freeQoS technical documentation"
CHAPEAU = (
    "How freeQoS works, from the router to the screen: architecture, installation and "
    "sizing, network flows, measurements, NetFlow, shaping, API, security, operations "
    "and troubleshooting."
)

#: Legende de chaque schema, dans l'ordre ou le texte les appelle.
LEGENDES = {
    "architecture": "Architecture: sources, cycles, database, and the single write path",
    "network-flows": "Network flows: who opens which connection, on which port",
    "netflow-pipeline": "NetFlow: from the router's cache to a subscriber's account",
    "shaping": "Shaping: where the queue forms, without and with freeQoS",
}

#: L'icone d'onglet, la meme que l'interface et le guide de l'API.
ICONE = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect width='16' height='16' rx='4' fill='%230a84ff'/%3E%3Cpath d='M3 11 L6 7 "
    "L9 9.5 L13 4.5' stroke='%23fff' stroke-width='1.8' stroke-linecap='round' "
    "stroke-linejoin='round' fill='none'/%3E%3C/svg%3E"
)

_FIGURE = re.compile(r"^<!-- figure: ([a-z0-9-]+) -->$", re.MULTILINE)


def empreinte_source() -> str:
    """Condensat du texte et des schemas : il change des qu'une source change."""
    condensat = hashlib.sha256(SOURCE.read_bytes())
    for schema in sorted(SCHEMAS.glob("*.svg")):
        condensat.update(schema.name.encode())
        condensat.update(schema.read_bytes())
    return condensat.hexdigest()[:16]


def _figure(nom: str) -> str:
    svg = (SCHEMAS / f"{nom}.svg").read_text(encoding="utf-8").strip()
    legende = html.escape(LEGENDES[nom])
    return (
        f'<figure class="fig" id="fig-{nom}">\n{svg}\n<figcaption>{legende}</figcaption>\n</figure>'
    )


def rendre_corps() -> tuple[str, list[dict[str, Any]]]:
    """Le corps HTML de la documentation et sa table des matieres."""
    texte = SOURCE.read_text(encoding="utf-8")
    # Le titre est porte par l'en-tete de la page, pas par le corps.
    texte = re.sub(r"\A# .*\n+", "", texte)
    # Un jeton neutre traverse la conversion ; le SVG est pose apres.
    texte = _FIGURE.sub(lambda m: f"FIGURE::{m.group(1)}", texte)

    convertisseur = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc"],
        extension_configs={"toc": {"toc_depth": "2-3"}},
        output_format="html",
    )
    corps = convertisseur.convert(texte)
    corps = re.sub(r"<p>FIGURE::([a-z0-9-]+)</p>", lambda m: _figure(m.group(1)), corps)
    # Un tableau large defile dans son cadre au lieu d'elargir la page.
    corps = corps.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )
    sommaire: list[dict[str, Any]] = convertisseur.toc_tokens
    return corps, sommaire


def _liens_sommaire(sommaire: list[dict[str, Any]]) -> str:
    lignes = []
    for chapitre in sommaire:
        lignes.append(f'    <a href="#{chapitre["id"]}">{chapitre["name"]}</a>')
        for partie in chapitre.get("children", []):
            lignes.append(f'    <a class="sub" href="#{partie["id"]}">{partie["name"]}</a>')
    return "\n".join(lignes)


def _sommaire_imprime(sommaire: list[dict[str, Any]]) -> str:
    """Le sommaire du PDF : les chapitres seuls, la page n'a pas de colonne."""
    items = "\n".join(f"<li>{c['name']}</li>" for c in sommaire)
    # Les titres portent deja leur numero : une liste non numerotee.
    return f'<nav class="print-toc"><h2>Contents</h2>\n<ul>\n{items}\n</ul></nav>'


def rendre_gabarit() -> str:
    corps, sommaire = rendre_corps()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="freeqos-docs-source" content="{empreinte_source()}">
  <title>{TITRE}</title>
  <link rel="icon" href="{ICONE}">
  <script src="/static/theme.js?v={{{{ theme_version }}}}"></script>
  <link rel="stylesheet" href="/static/api-guide.css?v={{{{ guide_css_version }}}}">
  <link rel="stylesheet" href="/static/docs.css?v={{{{ css_version }}}}">
</head>
<body>
<!-- GENERE par scripts/build_docs.py depuis docs/ : ne pas modifier a la main. -->
<div class="layout">
  <nav class="toc" aria-label="Contents">
    <a class="brand" href="/">freeQoS</a>
    <div class="toc-title">Documentation</div>
{_liens_sommaire(sommaire)}
  </nav>

  <main>
    <header class="hero">
      <h1>{TITRE}</h1>
      <p>{CHAPEAU}</p>
      <p class="doc-actions">
        <a class="doc-button" href="/documentation.pdf">Download the PDF</a>
        <a href="/api-guide">API guide</a>
      </p>
    </header>
{{% raw %}}
{corps}
{{% endraw %}}
  </main>
</div>
<script src="/static/docs.js?v={{{{ js_version }}}}"></script>
</body>
</html>
"""


def rendre_page_pdf() -> str:
    """Une page autonome pour l'impression : styles en ligne, aucun script."""
    corps, sommaire = rendre_corps()
    styles = (STATIQUE / "api-guide.css").read_text(encoding="utf-8") + (
        STATIQUE / "docs.css"
    ).read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head><meta charset="utf-8"><title>{TITRE}</title><style>{styles}</style></head>
<body class="print">
<main>
  <header class="hero cover">
    <h1>{TITRE}</h1>
    <p>{CHAPEAU}</p>
  </header>
  {_sommaire_imprime(sommaire)}
{corps}
</main>
</body>
</html>
"""


def ecrire_pdf(destination: Path) -> None:
    from playwright.sync_api import sync_playwright

    pied = (
        '<div style="width:100%;font-size:8px;color:#6e6e73;padding:0 14mm;'
        'display:flex;justify-content:space-between;font-family:sans-serif">'
        f"<span>{TITRE}</span>"
        '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
    )
    with sync_playwright() as p:
        chemin = os.environ.get("FREEQOS_CHROMIUM")
        navigateur = p.chromium.launch(executable_path=chemin) if chemin else p.chromium.launch()
        page = navigateur.new_page()
        page.emulate_media(media="print", color_scheme="light")
        page.set_content(rendre_page_pdf(), wait_until="load")
        page.pdf(
            path=str(destination),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=pied,
            margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
        )
        navigateur.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pdf", action="store_true", help="also write the PDF")
    args = parser.parse_args()
    GABARIT.write_text(rendre_gabarit(), encoding="utf-8")
    print(f"page : {GABARIT.relative_to(RACINE)}")
    if args.pdf:
        ecrire_pdf(PDF)
        print(f"PDF  : {PDF.relative_to(RACINE)} ({PDF.stat().st_size // 1024} Ko)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

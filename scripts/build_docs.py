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
import json
import os
import re
import subprocess
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
#: Les explications que l'interface affiche dans ses bulles (dictionnaire AIDE
#: d'app.js), extraites une fois par ``--ui-help`` : le guide de l'interface
#: reprend MOT POUR MOT ce que l'exploitant lit a l'ecran.
AIDE_JSON = RACINE / "docs" / "ui-help.json"
APP_JS = STATIQUE / "app.js"

TITRE = "freeQoS technical documentation"
CHAPEAU = (
    "How freeQoS works, from the router to the screen: architecture, installation and "
    "sizing, network flows, measurements, NetFlow, shaping, capacity and insights, API, "
    "security, operations, troubleshooting, and a reference of every screen and endpoint."
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
_GENERE = re.compile(r"^<!-- generated: ([a-z0-9-]+) -->$", re.MULTILINE)

# ----------------------------------------------------- guide de l'interface
#
# Chaque page de l'interface, dans l'ordre de la barre laterale, avec les
# sections d'aide (cles du dictionnaire AIDE) qui lui appartiennent. Ce qui
# n'est range nulle part part dans le glossaire des colonnes et des chiffres :
# aucune explication de l'interface ne peut manquer a la documentation.
PAGES_INTERFACE: list[tuple[str, str, list[str]]] = [
    (
        "Dashboard",
        "What is happening on the network right now.",
        ["network throughput", "routers — live traffic", "top consumers", "radio backhauls"],
    ),
    (
        "Executive",
        "Where the risk is: saturation, experience, load and queues per node, health over "
        "time. Clicking a node or a client opens the Selection panel.",
        [
            "saturation risks",
            "latency by client",
            "load by node",
            "queues by node",
            "selection",
            "sec|node",
            "sec|client",
            "health over time",
        ],
    ),
    (
        "Traffic",
        "Who consumes what, where the traffic goes, and the traffic restrictions.",
        [
            "who consumes",
            "which services the traffic comes from",
            "where the traffic goes",
            "who talks to whom, client by client",
            "destinations reached",
            "find an ip",
            "traffic restrictions",
        ],
    ),
    (
        "Network tree",
        "The discovered tree, editable with the mouse: drag a box onto another to re-parent "
        "it, correct a role, create or remove a link, merge two boxes that are the same "
        "device, hide a box. Each link carries its measured throughput. *Forget vanished "
        "devices* removes boxes not seen for a chosen time; declared routers and hand-made "
        "links are never removed.",
        [],
    ),
    (
        "Subscribers",
        "Every subscriber of every PoP, PPPoE and static-IP, filterable by PoP, kind and "
        "login: rate against plan, latency, bufferbloat grade, running boost. The *Add a "
        "client* form declares a static-IP client; *Declared static-IP clients* and "
        "*Clients by VLAN* list the inventory.",
        [],
    ),
    (
        "Plans",
        "Which rate for whom: the default plan, every client's plan and its source, and the "
        "packages pushed through the API.",
        ["default plan", "clients", "packages pushed by the api"],
    ),
    (
        "Insights",
        "The commercial reading of the measurements, comparing the chosen period with the "
        "one before.",
        [
            "at risk of leaving",
            "ready for a bigger plan",
            "sites and access points room for more subscribers",
            "every subscriber plan, usage, experience",
        ],
    ),
    (
        "Devices",
        "Routers and antennas: their health, adding them, and the inventory.",
        [
            "router health",
            "polled routers",
            "connect a router",
            "ubiquiti antennas",
            "radio health access points and their cpes",
            "add an antenna",
            "inventory",
            "known sites",
        ],
    ),
    ("API", "API keys, and the way to the API guide.", ["create a key", "endpoints", "example"]),
    (
        "Settings",
        "Accounts, operating settings, and everything about writing to the routers.",
        [
            "accounts",
            "who can log in",
            "my password",
            "my sessions",
            "login journal",
            "operational settings",
            "sec|services and ip location",
            "sec|shaping",
            "sec|cake",
            "sec|write safeguards",
            "sec|traffic",
            "sec|collection cadences",
            "what stays out of reach of the interface",
            "shaping and writing to the routers",
            "are the caps actually held",
            "log of commands sent",
        ],
    ),
    (
        "Detail panels",
        "What opens when you click a link, a node, a client or an address.",
        [
            "right now",
            "clients on this link",
            "link",
            "plan",
            "plan usage",
            "limit usage",
            "who reaches this address",
        ],
    ),
]

#: Ou se lit une cle a prefixe du glossaire.
CONTEXTES = {
    "acc": "Accounts",
    "col": "Table column",
    "heat": "Health over time",
    "kpi": "Routers — live traffic",
    "vantage": "Traffic",
    "hot": "Saturation risks",
    "sec": "Section",
}
TONS = {"ok": "Green", "warn": "Amber", "crit": "Red", "none": "Grey"}
_SIGLES = {
    "ip": "IP",
    "api": "API",
    "cpe": "CPE",
    "cpes": "CPEs",
    "pop": "PoP",
    "pops": "PoPs",
    "cake": "CAKE",
    "qoe": "QoE",
    "rtt": "RTT",
    "ccq": "CCQ",
    "snr": "SNR",
    "cpu": "CPU",
    "tx/rx": "TX/RX",
    "vlan": "VLAN",
    "vlans": "VLANs",
    "uisp": "UISP",
    "netflow": "NetFlow",
}


def empreinte_source() -> str:
    """Condensat du texte et des schemas : il change des qu'une source change."""
    condensat = hashlib.sha256(SOURCE.read_bytes())
    condensat.update(AIDE_JSON.read_bytes())
    for schema in sorted(SCHEMAS.glob("*.svg")):
        condensat.update(schema.name.encode())
        condensat.update(schema.read_bytes())
    return condensat.hexdigest()[:16]


def _titre(cle: str) -> str:
    """Le libelle d'une cle d'aide, tel qu'il se lit a l'ecran."""
    mots = cle.split("|", 1)[-1].split(" ")
    mots = [_SIGLES.get(m, m) for m in mots]
    if mots and mots[0][:1].islower():
        mots[0] = mots[0][:1].upper() + mots[0][1:]
    return " ".join(mots)


def _texte(valeur: str) -> str:
    """Un texte d'aide sans balise : il est affiche tel quel."""
    return html.escape(valeur, quote=False).replace("*", "\\*")


def _entree(entree: Any) -> str:
    if isinstance(entree, str):
        return _texte(entree)
    morceaux = []
    if entree.get("t"):
        morceaux.append(f"**What it shows.** {_texte(entree['t'])}")
    if entree.get("m"):
        morceaux.append(f"**How it is measured.** {_texte(entree['m'])}")
    if entree.get("s"):
        echelle = "\n".join(f"- {TONS.get(ton, ton)}: {_texte(txt)}" for ton, txt in entree["s"])
        morceaux.append("**Scale.**\n\n" + echelle)
    if entree.get("r"):
        morceaux.append(f"**How to read it.** {_texte(entree['r'])}")
    if entree.get("a"):
        morceaux.append(f"**What to do.** {_texte(entree['a'])}")
    return "\n\n".join(morceaux)


def _cellule(entree: Any) -> str:
    """Une entree d'aide en une cellule de tableau."""
    if isinstance(entree, str):
        texte = entree
    else:
        parties = [entree.get(k) for k in ("t", "m", "r") if entree.get(k)]
        if entree.get("s"):
            parties.append(
                "Scale: " + "; ".join(f"{TONS.get(t, t)}, {txt}" for t, txt in entree["s"]) + "."
            )
        texte = " ".join(parties)
    return _texte(texte).replace("|", "\\|").replace("\n", " ")


def lire_aide() -> dict[str, Any]:
    donnees: dict[str, Any] = json.loads(AIDE_JSON.read_text(encoding="utf-8"))
    return donnees


def extraire_aide() -> dict[str, Any]:
    """Le dictionnaire AIDE tel qu'app.js le construit (Node.js requis)."""
    script = (
        "const src = require('fs').readFileSync(process.argv[1], 'utf8');"
        "const a = src.indexOf(\"const OK = 'ok'\");"
        "const b = src.indexOf('\\n/** Libelles variables');"
        "if (a < 0 || b < 0) throw new Error('AIDE introuvable dans app.js');"
        "const AIDE = new Function(src.slice(a, b) + '\\nreturn AIDE;')();"
        "process.stdout.write(JSON.stringify(AIDE));"
    )
    sortie = subprocess.run(
        ["node", "-e", script, str(APP_JS)], check=True, capture_output=True, text=True
    )
    donnees: dict[str, Any] = json.loads(sortie.stdout)
    return donnees


def guide_interface() -> str:
    """Le guide de l'interface, page par page, puis le glossaire des chiffres."""
    aide = lire_aide()
    ranges = {cle for _, _, cles in PAGES_INTERFACE for cle in cles}
    inconnues = sorted(ranges - set(aide))
    if inconnues:
        raise SystemExit(f"cles d'aide absentes d'app.js : {inconnues}")
    blocs = []
    for page, intro, cles in PAGES_INTERFACE:
        blocs.append(f"### {page}\n\n{intro}")
        for cle in cles:
            blocs.append(f"#### {_titre(cle)}\n\n{_entree(aide[cle])}")
    lignes = []
    for cle in sorted(set(aide) - ranges, key=lambda c: (c.split("|", 1)[-1], c)):
        ou = CONTEXTES.get(cle.split("|", 1)[0], "") if "|" in cle else ""
        lignes.append(f"| {_titre(cle)} | {ou} | {_cellule(aide[cle])} |")
    blocs.append(
        "### Columns and figures\n\nEvery other label that carries an (i) in the interface, "
        "in alphabetical order. *Where* names the place when the same word means something "
        "else elsewhere.\n\n| Label | Where | Meaning |\n| --- | --- | --- |\n" + "\n".join(lignes)
    )
    return "\n\n".join(blocs)


#: Ordre des groupes de l'index de l'API : ce qui sert a un integrateur d'abord.
ORDRE_API = [
    "public api (model)",
    "public api (usage)",
    "pops",
    "plans",
    "shaping",
    "static clients",
    "metrics",
    "capacity",
    "traffic (netflow)",
    "traffic restrictions",
    "antennas",
    "operations",
    "settings",
    "health",
    "accounts",
    "api keys",
]


def index_api() -> str:
    """Chaque route exposee, tiree de l'OpenAPI du serveur lui-meme."""
    sys.path.insert(0, str(RACINE))
    try:
        from fastapi import FastAPI

        from app.config import Settings
        from app.main import register_routes
    finally:
        sys.path.pop(0)
    application = FastAPI()
    reglages = Settings(_env_file=None)
    application.state.settings = reglages
    register_routes(application, reglages)
    chemins = application.openapi()["paths"]
    groupes: dict[str, list[tuple[str, str, str]]] = {}
    for chemin in sorted(chemins):
        for methode in ("get", "post", "put", "patch", "delete"):
            op = chemins[chemin].get(methode)
            if not op:
                continue
            tag = (op.get("tags") or ["other"])[0]
            groupes.setdefault(tag, []).append(
                (methode.upper(), chemin, str(op.get("summary") or ""))
            )
    total = sum(len(v) for v in groupes.values())
    rang = {t: i for i, t in enumerate(ORDRE_API)}
    blocs = [
        f"The server exposes {total} operations. This index is generated from its own "
        "OpenAPI description, so it cannot drift from the code; the *API guide* "
        "(`/api-guide`) gives the parameters and bodies of each one."
    ]
    for tag in sorted(groupes, key=lambda t: (rang.get(t, 50), t)):
        lignes = "\n".join(f"| `{m}` | `{c}` | {_cellule(s)} |" for m, c, s in groupes[tag])
        blocs.append(
            f"### {_titre(tag)}\n\n| Method | Path | What it does |\n| --- | --- | --- |\n{lignes}"
        )
    return "\n\n".join(blocs)


GENERATEURS = {"interface-reference": guide_interface, "api-endpoints": index_api}


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
    # Les parties generees depuis le code : guide de l'interface, index de l'API.
    texte = _GENERE.sub(lambda m: GENERATEURS[m.group(1)](), texte)
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
    parser.add_argument(
        "--ui-help", action="store_true", help="re-extract the interface help from app.js (Node)"
    )
    args = parser.parse_args()
    if args.ui_help:
        AIDE_JSON.write_text(
            json.dumps(extraire_aide(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"aide : {AIDE_JSON.relative_to(RACINE)}")
    GABARIT.write_text(rendre_gabarit(), encoding="utf-8")
    print(f"page : {GABARIT.relative_to(RACINE)}")
    if args.pdf:
        ecrire_pdf(PDF)
        print(f"PDF  : {PDF.relative_to(RACINE)} ({PDF.stat().st_size // 1024} Ko)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

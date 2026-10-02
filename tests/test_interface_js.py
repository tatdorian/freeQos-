"""Garde-fous sur le fichier d'interface (app.js), verifiables sans navigateur.

POURQUOI CE FICHIER EXISTE
--------------------------
Une page ajoutee a l'interface a redefini ``pct()`` -- le nom qu'utilisaient
DEJA toutes les jauges du produit. En JavaScript, la derniere declaration gagne
pour tout le fichier : la nouvelle page marchait parfaitement, et le tableau de
bord, l'arbre reseau et la liste des abonnes tombaient sur un
``p.toFixed is not a function``. Rien dans les tests Python ne pouvait le voir,
et rien dans le fichier ne le signalait.

Ces deux tests coutent quelques millisecondes et ferment cette classe de
defauts : une collision de nom est desormais une erreur de suite, pas une
decouverte en production.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node absent : app.js non verifiable"
)

# Une declaration de fonction en tete de fichier, colonne zero. Les fonctions
# imbriquees (indentees) sont locales a leur portee et n'entrent pas en
# collision : elles ne sont volontairement pas comptees.
DECLARATION = re.compile(r"^(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(", re.MULTILINE)


def test_app_js_est_syntaxiquement_valide() -> None:
    """Une erreur de syntaxe rend l'interface entierement blanche."""
    proc = subprocess.run(
        ["node", "--check", str(APP_JS)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"app.js ne compile pas :\n{proc.stderr}"


def test_aucune_fonction_n_est_declaree_deux_fois() -> None:
    """DEUX FONCTIONS DE MEME NOM NE COHABITENT PAS, ELLES SE REMPLACENT.

    Et le remplacement est silencieux : c'est la page la plus ancienne qui casse,
    pas celle qu'on vient d'ecrire. Le message nomme la fonction fautive, parce
    que la trouver a la main dans 4 500 lignes est exactement le travail qu'on
    veut eviter.
    """
    source = APP_JS.read_text(encoding="utf-8")
    comptes = Counter(DECLARATION.findall(source))
    doublons = {nom: n for nom, n in comptes.items() if n > 1}

    assert not doublons, (
        "fonction(s) declaree(s) plusieurs fois dans app.js : "
        + ", ".join(f"{nom} ({n} fois)" for nom, n in sorted(doublons.items()))
        + ". La derniere declaration ecrase les precedentes pour tout le fichier."
    )


def test_le_plan_ne_part_jamais_avec_un_routeur_vide() -> None:
    """LE DEFAUT VU EN PRODUCTION. Avec "Tous les PoP", le selecteur vaut la
    chaine vide ; on envoyait ``router: ""``, l'API refusait, et son erreur de
    validation brute s'affichait en pleine page :

        [{"type":"string_too_short","loc":["body","router"],...}]

    Ce que l'exploitant demande dans ce cas n'a pourtant rien d'ambigu : le plan
    de chaque routeur. On les calcule donc tous.
    """
    source = APP_JS.read_text()
    debut = source.index("async function computePlan()")
    corps = source[debut : source.index("\n}\n", debut)]

    # La liste des routeurs est batie depuis les options, et les valeurs vides
    # sont ecartees avant tout appel.
    assert "filter(Boolean)" in corps
    # Et l'appel se fait sur un routeur nomme, jamais sur le choix brut.
    assert "JSON.stringify({ router: routeur })" in corps
    assert "JSON.stringify({ router: choisi })" not in corps


def test_les_erreurs_de_validation_sont_rendues_lisibles() -> None:
    """Un message d'erreur qu'il faut dechiffrer ne vaut guere mieux que pas de
    message : FastAPI rend un tableau d'objets, on en fait une phrase."""
    source = APP_JS.read_text()

    assert "function validationText(" in source
    assert "validationText(detail)" in source

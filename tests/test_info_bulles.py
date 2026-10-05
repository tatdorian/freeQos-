"""Les info-bulles : chaque fiche est complete et lisible par l'interface.

Le dictionnaire vit dans ``app.js`` ; une faute de frappe (une cle en
majuscules, un ton de couleur inconnu, une fiche sans texte) ne casse rien de
visible -- la bulle reste simplement vide ou absente. Ce test l'evalue avec
Node et verifie chaque entree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parents[1] / "app" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")


def _dictionnaire() -> dict:
    source = APP_JS.read_text(encoding="utf-8")
    debut = source.index("const OK = 'ok'")
    fin = source.index("\n};\n", source.index("const AIDE = {")) + 4
    motifs_debut = source.index("const AIDE_MOTIFS = [")
    motifs_fin = source.index("\n];\n", motifs_debut) + 4
    script = (
        source[debut:fin]
        + source[motifs_debut:motifs_fin]
        + "const sortie = {aide: AIDE, motifs: AIDE_MOTIFS.map((m) => m[1])};"
        + "process.stdout.write(JSON.stringify(sortie));"
    )
    sortie = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(sortie.stdout)


def test_chaque_fiche_est_complete() -> None:
    donnees = _dictionnaire()
    aide = donnees["aide"]
    assert len(aide) > 200
    fiches = list(aide.items()) + [(f"motif{i}", m) for i, m in enumerate(donnees["motifs"])]
    for cle, fiche in fiches:
        # Les libelles sont compares en minuscules : une cle en majuscules ne
        # serait jamais trouvee.
        assert cle == cle.lower(), cle
        if isinstance(fiche, str):
            assert fiche.strip(), cle
            continue
        assert set(fiche) <= {"t", "m", "s", "r", "a"}, cle
        assert fiche.get("t"), f"{cle} : il faut au moins dire ce que c'est"
        for ton, texte in fiche.get("s", []):
            assert ton in {"ok", "warn", "crit", "none"}, cle
            assert texte, cle


def test_les_valeurs_cles_disent_comment_elles_sont_mesurees_et_lues() -> None:
    aide = _dictionnaire()["aide"]
    for cle in ("latency", "experience", "saturation risks", "latency by client", "under load"):
        fiche = aide[cle]
        assert fiche.get("m") and fiche.get("s"), cle

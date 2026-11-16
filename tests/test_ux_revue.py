"""Revue d'usage : ce qu'une personne voit quand elle clique.

CE QUE CES TESTS FIXENT
-----------------------
1. UN ELEMENT MASQUE LE RESTE : une seule regle ``[hidden] { display: none !important }``.
   Sans elle, la duree libre du boost restait affichee sous « 1 h » (une classe
   ``display: flex`` l'emportait sur l'attribut ``hidden``).
2. LE RESULTAT D'UN GESTE SE VOIT : un message apparu apres un clic est ramene
   dans la vue ; les rechargements automatiques ne font jamais defiler.
3. CHAQUE GESTE DU JOURNAL A UN LIBELLE : toute action ecrite au journal des
   clients figure dans ``ACTION_LABEL`` (plus de « details » / « declared » bruts).
4. LA LEGENDE DES NIVEAUX DIT VRAI : l'Operator ajoute des clients, comme le
   serveur le permet.
5. LE COMPTEUR DES CLES D'API SUIT LA LISTE (creation, revocation).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api.accounts import niveau_requis
from app.web.ui import STATIC_DIR

APP_DIR = Path(__file__).resolve().parent.parent / "app"
JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def test_hidden_masque_toujours() -> None:
    assert re.search(r"^\[hidden\]\s*\{\s*display:\s*none\s*!important", CSS, re.M)


def test_le_resultat_d_un_geste_est_ramene_dans_la_vue() -> None:
    assert "function voir(el)" in JS
    assert "new MutationObserver" in JS
    # Seulement apres une action de la personne (clic / envoi), jamais au rechargement.
    assert "Date.now() - derniereAction > 12000" in JS
    assert "['click', 'submit']" in JS


def test_chaque_action_du_journal_a_un_libelle() -> None:
    bloc = re.search(r"const ACTION_LABEL = \{(.*?)\};", JS, re.S)
    assert bloc
    libelles = set(re.findall(r"\b([a-z_]+):\s*'", bloc.group(1)))
    ecrites: set[str] = set()
    for source in (APP_DIR / "api").glob("*.py"):
        ecrites |= set(re.findall(r'\baction="([a-z_]+)"', source.read_text(encoding="utf-8")))
    ecrites.discard("store_true")
    assert ecrites, "aucune action trouvee : le motif de recherche est a revoir"
    assert not (ecrites - libelles), f"actions sans libelle : {sorted(ecrites - libelles)}"


def test_la_legende_des_niveaux_dit_ce_que_le_serveur_permet() -> None:
    # Le serveur laisse l'Operator ajouter des clients...
    assert niveau_requis("POST", "/api/v1/clients", "/api/v1") == "operator"
    assert niveau_requis("POST", "/api/v1/static-clients", "/api/v1") == "operator"
    # ... et l'ecran ne le reserve plus a l'Administrator.
    aide = re.search(r"const ROLE_HELP = \{(.*?)\};", JS, re.S)
    assert aide
    texte = aide.group(1)
    operator = re.search(r"operator:\s*'([^']*)'", texte)
    edit = re.search(r"edit:\s*'([^']*)'", texte)
    assert operator and edit
    assert "adds them" in operator.group(1)
    assert "adds clients" not in edit.group(1)


def test_le_conseil_d_un_conflit_ne_renvoie_pas_a_un_message_en_dessous() -> None:
    assert "Read the reason below" not in JS


def test_le_compteur_des_cles_suit_la_liste() -> None:
    corps = JS[JS.index("async function loadApiKeys()") :]
    corps = corps[: corps.index("\n}\n")]
    assert "api-count" in corps


def test_les_actions_d_un_routeur_sont_rangees_en_deux_groupes() -> None:
    """Usage courant (Set up, Config, Test, Disable) puis ce qui efface
    (Reset queues, Remove freeQoS, Remove) : jamais melanges, toujours au meme endroit."""
    bloc = JS[JS.index('class="router-actions"') :]
    bloc = bloc[: bloc.index("</td></tr>")]
    courant, efface = bloc.split("data-clean-queues", 1)
    for geste in ("data-provision", "data-config", "data-probe", "data-toggle"):
        assert geste in courant
    for geste in ("data-uninstall", "data-del"):
        assert geste in efface
    assert "data-uninstall" not in courant

"""La navigation : ce que l'interface propose, et ce qu'elle promet de trouver.

POURQUOI CES TESTS EXISTENT
---------------------------
Retirer un onglet est une operation a trois endroits -- la barre, la section
HTML, le routeur de vues -- et en oublier un ne casse pas bruyamment. Pire :
``document.getElementById('x').addEventListener(...)`` ecrit hors de toute
fonction leve sur un id disparu, et une exception a ce moment-la interrompt le
script ENTIER. La page reste blanche, sans erreur visible ailleurs que dans la
console du navigateur.

Ces verifications sont purement textuelles : ni navigateur, ni serveur.
"""

from __future__ import annotations

import re
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1] / "app" / "web"
HTML = (RACINE / "templates" / "index.html").read_text(encoding="utf-8")
JS = (RACINE / "static" / "app.js").read_text(encoding="utf-8")

ONGLETS = re.findall(r'data-view="([a-z-]+)"', HTML)
IDS_HTML = set(re.findall(r'\sid="([A-Za-z0-9_-]+)"', HTML))
#: Les appels EN COLONNE ZERO : ils s'executent au chargement du script, hors de
#: toute fonction. Ce sont les seuls qui puissent blanchir la page entiere -- un
#: getElementById indente vise souvent un element cree par le script lui-meme
#: (tiroirs, editeurs), qui n'a rien a faire dans index.html.
IDS_UTILISES = set(
    re.findall(
        r"^document\.getElementById\('([A-Za-z0-9_-]+)'\)\.addEventListener",
        JS,
        re.MULTILINE,
    )
)


def loaders() -> set[str]:
    bloc = JS[JS.index("const LOADERS = {") : JS.index("async function show(view)")]
    return set(re.findall(r"^\s*([a-z]+):\s*load", bloc, re.MULTILINE))


def test_les_onglets_topologie_et_shaping_ont_disparu() -> None:
    """DEMANDE EXPLICITE : moins d'onglets.

    Rien n'est perdu pour autant -- le tableau des liens vit replie sous l'arbre
    reseau, l'interrupteur d'ecriture et le journal des commandes sous les
    reglages. Ce qui disparait, c'est la place qu'ils prenaient dans la
    navigation de tous les jours.
    """
    assert "topology" not in ONGLETS
    assert "shaping" not in ONGLETS
    assert 'id="view-topology"' not in HTML
    assert 'id="view-shaping"' not in HTML


def test_ce_qui_vivait_dans_ces_onglets_est_toujours_la() -> None:
    """Retirer un onglet n'est pas supprimer une capacite."""
    assert 'id="net-links-block"' in HTML  # le tableau des liens
    assert 'id="topo-links"' in HTML
    assert 'id="settings-shaping"' in HTML  # l'interrupteur et le journal
    assert 'id="enforcement-toggle"' in HTML
    assert 'id="shaping-audit"' in HTML
    assert 'id="shaping-points"' in HTML


def test_l_onglet_trafic_existe_et_est_cable() -> None:
    assert "traffic" in ONGLETS
    assert 'id="view-traffic"' in HTML
    assert "traffic: loadTraffic" in JS


def test_chaque_onglet_a_sa_section_et_son_chargeur() -> None:
    """LES TROIS ENDROITS DOIVENT S'ACCORDER. Un onglet sans section renvoie
    ``show()`` sur le tableau de bord sans rien dire ; un onglet sans chargeur
    affiche une section qui ne se remplit jamais."""
    chargeurs = loaders()
    for vue in ONGLETS:
        assert f'id="view-{vue}"' in HTML, f"l'onglet '{vue}' n'a pas de section"
        assert vue in chargeurs, f"l'onglet '{vue}' n'a pas de chargeur dans LOADERS"
    assert chargeurs == set(ONGLETS), "un chargeur ne correspond a aucun onglet"


def test_chaque_lien_de_la_barre_pointe_sur_sa_propre_vue() -> None:
    """Un href et un data-view qui divergent envoient l'exploitant ailleurs que
    la ou l'onglet s'allume."""
    for href, vue in re.findall(r'href="#/([a-z-]+)"\s+data-view="([a-z-]+)"', HTML):
        assert href == vue, f"le lien #/{href} ouvre la vue '{vue}'"


def test_aucun_ecouteur_ne_vise_un_element_disparu() -> None:
    """CELUI-CI RATTRAPE LA PAGE BLANCHE.

    Ces appels sont hors fonction : ils s'executent au chargement. Sur un id
    disparu, l'exception interrompt le script entier et TOUTE l'interface est
    morte, pas seulement l'onglet concerne.
    """
    manquants = sorted(IDS_UTILISES - IDS_HTML)
    assert not manquants, (
        "app.js pose un ecouteur sur des elements absents de index.html : " + ", ".join(manquants)
    )


def test_les_blocs_replies_ne_chargent_rien_tant_qu_ils_sont_fermes() -> None:
    """C'est ce qui rend leur deplacement gratuit : aucune lecture de plus tant
    que personne ne les regarde."""
    for bloc in ("net-links-block", "settings-shaping", "sc-candidates-block"):
        assert f"getElementById('{bloc}')" in JS


def test_le_formulaire_de_client_porte_bien_le_champ_vlan() -> None:
    """Les clients par VLAN se declarent a la main : le champ doit exister, et
    le tableau qui les range aussi."""
    assert 'id="sc-vlan"' in HTML
    assert 'id="sc-vlans"' in HTML
    assert "loadVlanClients" in JS

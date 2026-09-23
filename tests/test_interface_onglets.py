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


def test_l_onglet_capacite_a_disparu() -> None:
    """DEMANDE EXPLICITE : l'onglet Capacite est retire de la navigation.

    Comme pour Topologie et Shaping, c'est une operation a trois endroits. La
    lecture, elle, reste servie par l'API (``/capacity``) : retirer un onglet
    n'est pas supprimer une capacite, et un systeme tiers qui la consommait
    continue de la lire.
    """
    assert "capacity" not in ONGLETS
    assert 'id="view-capacity"' not in HTML
    assert "loadCapacity" not in JS


def test_l_onglet_services_existe_et_est_cable() -> None:
    """QUI SE CONNECTE A QUOI. L'onglet qui remplace Capacite doit exister aux
    trois endroits, sans quoi il s'ouvre sur une section qui ne se remplit
    jamais -- ou pire, blanchit la page."""
    assert "services" in ONGLETS
    assert 'id="view-services"' in HTML
    assert "services: loadServices" in JS


def test_l_onglet_api_existe_et_porte_la_creation_de_cles() -> None:
    """LA SURFACE D'INTEGRATION A SON PROPRE ONGLET.

    Les donnees sont poussees dans cette application depuis une autre interface :
    la cle, les points d'entree et l'exemple d'appel sont ce qu'un integrateur
    vient chercher. Les enterrer au fond des Reglages obligeait a savoir ou
    regarder.
    """
    assert "api" in ONGLETS
    assert 'id="view-api"' in HTML
    assert "api: loadApi" in JS
    assert 'id="key-form"' in HTML
    assert 'id="api-endpoints"' in HTML


def test_les_cles_ne_sont_plus_dans_les_reglages() -> None:
    """Un formulaire a deux endroits, c'est un formulaire qu'on modifie a un
    seul -- et l'autre se met a diverger sans que personne ne le voie."""
    reglages = HTML[HTML.index('id="view-settings"') :]
    assert 'id="key-form"' not in reglages


def test_l_export_netflow_n_a_plus_de_bloc_manuel() -> None:
    """DEMANDE EXPLICITE : l'export se pose tout seul (job periodique), le bloc
    "Export on the routers" et ses boutons n'avaient plus rien a faire."""
    assert 'id="flow-export"' not in HTML
    assert "applyFlowExport" not in JS


def test_l_interface_ne_fait_plus_la_lecon() -> None:
    """DEMANDE EXPLICITE : plus de pages de prose au milieu de l'outil.

    Les paragraphes d'explication et les blurbs sous les champs servaient de
    documentation a l'endroit ou l'exploitant travaille. Ce qui reste nomme des
    faits -- un etat, une erreur, une valeur -- et s'arrete la.
    """
    assert 'class="empty"' not in HTML
    assert 'class="help"' not in HTML


def test_l_onglet_services_porte_ses_trois_promesses() -> None:
    """Voir les connexions en cours, voir ce qui est atteint, et pouvoir
    restreindre. Les trois blocs doivent etre la : un formulaire de restriction
    sans tableau de connexions obligerait a deviner ce qu'on bride."""
    for identifiant in ("svc-live", "svc-destinations", "svc-services", "svc-rules"):
        assert f'id="{identifiant}"' in HTML, identifiant
    assert 'id="svc-rule-form"' in HTML
    assert "loadServices" in JS


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


def test_le_diagnostic_netflow_mene_au_geste_suivant() -> None:
    """« Configurez l'export ci-dessous » laisse chercher OU et COMMENT.

    Chaque cause a son geste : aucun routeur declare renvoie aux Equipements,
    aucun export pose donne le bouton qui y mene, et un export pose mais muet
    designe le chemin reseau plutot que la configuration -- ce n'est pas le
    meme probleme, et surtout pas la meme personne qui le corrige.
    """
    assert "No router is declared" in JS
    assert "automatically on each router" in JS
    # Le cas "pose mais rien n'arrive" doit nommer le port UDP : c'est le
    # coupable le plus frequent, et il n'a rien a voir avec le routeur.
    assert "udp" in JS


def test_le_pop_se_choisit_dans_un_menu() -> None:
    """Une saisie libre rattachait a un PoP ne d'une faute de frappe : le PoP
    d'un client ou d'une antenne se choisit parmi ceux des routeurs."""
    for ident in ("sc-pop", "a-pop"):
        debut = HTML.index(f'id="{ident}"')
        assert HTML[HTML.rindex("<", 0, debut) : debut].startswith("<select")
    assert "remplirMenusPop" in JS


# -------------------------------------------------------------------------
# L'arbre reseau : la toile, l'echelle, le repli
# -------------------------------------------------------------------------

CSS = (RACINE / "static" / "app.css").read_text(encoding="utf-8")


def test_la_toile_est_peinte_dans_le_dessin_pas_sur_le_cadre() -> None:
    """Le quadrillage etait peint sur le conteneur QUI DEFILE : il s'arretait a
    la partie visible, et l'arbre finissait sur du vide des qu'il depassait.
    Peint dans le SVG a la taille du contenu, il s'etend aussi loin que les
    cases."""
    assert "topo-grille" in JS
    assert 'patternUnits="userSpaceOnUse"' in JS
    assert 'fill="url(#topo-grille)"' in JS
    # Et le cadre ne doit plus porter de quadrillage a lui, sinon les deux se
    # superposeraient en decale des que l'on defile.
    bloc = CSS[CSS.index(".topo-canvas {") : CSS.index(".topo-canvas svg")]
    assert "linear-gradient" not in bloc


def test_la_toile_suit_la_place_disponible() -> None:
    """Une hauteur figee gachait un grand ecran et noyait un portable. Le cadre
    est desormais cale sur l'etendue des cases, bornee par la fenetre."""
    bloc = CSS[CSS.index(".topo-canvas {") : CSS.index(".topo-canvas svg")]
    assert "560px" not in bloc
    # Plus aucune hauteur imposee par la feuille de style : seul un plancher,
    # pour l'etat vide.
    assert "height:" not in bloc.replace("min-height:", "")
    assert "min-height: 360px" in bloc
    assert "TOPO_CADRE_MIN" in JS
    assert "window.innerHeight - 200" in JS


def test_l_arbre_offre_une_echelle_et_un_cadrage() -> None:
    """Un reseau d'operateur deborde toujours de l'ecran : sans echelle, on
    defile a l'aveugle sans jamais voir la forme d'ensemble."""
    for ident in ("btn-topo-zoom-in", "btn-topo-zoom-out", "btn-topo-fit", "topo-zoom-level"):
        assert f'id="{ident}"' in HTML
    assert "function setTopoZoom(" in JS
    assert "function topoFit(" in JS


def test_le_glisser_deposer_tient_compte_de_l_echelle() -> None:
    """A 50 %, un deplacement de 100 px a l'ecran vaut 200 px dans le dessin :
    sans la division, la case fuirait le pointeur."""
    bloc = JS[JS.index("function bindTopoDrag(") : JS.index("function topoDescendants(")]
    assert "const z = topo.zoom || 1;" in bloc
    assert "(e.clientX - start.x) / z" in bloc
    assert "(e.clientX - rect.left) / z" in bloc


def test_une_branche_se_replie_depuis_l_arbre() -> None:
    """Replier est ce qui rend le reste lisible. La pastille porte le COMPTE de
    ce qu'elle cache : fermer une branche ne doit pas faire disparaitre du
    reseau en silence."""
    assert "data-fold=" in JS
    assert "function bindTopoFolds(" in JS
    assert "topoDescendants(model, n.key).size" in JS


def test_la_legende_ne_nomme_que_les_roles_presents() -> None:
    """Une legende qui annonce des roles absents fait chercher des cases qui
    n'existent pas."""
    assert 'id="topo-legend"' in HTML
    bloc = JS[JS.index("function renderTopoLegend(") : JS.index("function bindTopoFolds(")]
    assert "n.replie" in bloc
    assert "KIND_LABEL" in bloc


# -------------------------------------------------------------------------
# Les clients a IP fixe : on vient en AJOUTER un
# -------------------------------------------------------------------------


def test_le_bouton_annonce_l_ajout_pas_un_inventaire() -> None:
    """« Inventaire des clients a IP fixe » decrivait un meuble, pas un geste.
    Ce qu'on vient faire ici, c'est ajouter un client."""
    assert "Add a client" in HTML
    assert "Inventaire des clients a IP fixe" not in HTML
    assert "Static-IP client inventory" not in HTML


def test_la_saisie_est_en_haut_du_panneau() -> None:
    """Le panneau s'ouvrait sur deux tableaux : le bouton promettait d'ajouter
    un client, et il fallait defiler pour trouver ou le faire."""
    panneau = HTML[HTML.index('<div id="sc-panel"') : HTML.index('id="limits-alert"')]
    form = panneau.index('id="sc-form"')
    assert form < panneau.index('id="sc-table"')
    assert form < panneau.index('id="sc-vlans"')
    assert form < panneau.index('id="sc-candidates-block"')


def test_le_pop_et_l_adresse_sont_exiges_et_nommes() -> None:
    """Un client a IP fixe sans PoP ne se rattache a rien, et sans adresse il ne
    se reconnait dans aucun flux : les deux sont obligatoires, et leur intitule
    doit dire lequel est lequel."""
    champ = lambda i: HTML[HTML.index(f'id="{i}"') : HTML.index(f'id="{i}"') + 200]  # noqa: E731
    assert "required" in champ("sc-pop")
    assert "required" in champ("sc-address")
    assert "Attached PoP" in HTML
    assert "IP address or subnet" in HTML


def test_le_formulaire_dit_s_il_ajoute_ou_s_il_modifie() -> None:
    """Le meme formulaire sert aux deux. Sans titre qui change, on croyait
    ajouter un client alors qu'on en ecrasait un autre."""
    assert 'id="sc-form-title"' in HTML
    assert "'Edit '" in JS
    assert "'Add a client'" in JS


def test_ouvrir_la_saisie_place_le_curseur_dedans() -> None:
    """Un bouton qui dit « Ajouter un client » doit laisser le curseur dans le
    premier champ, pas au-dessus d'un panneau a parcourir."""
    bloc = JS[JS.index("document.getElementById('sc-toggle')") :][:900]
    assert "reference.focus()" in bloc
    assert "scrollIntoView" in bloc

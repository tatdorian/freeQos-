"""Arbre reseau de l'interface (app.js) : la logique qui derive l'ARBRE du graphe.

La decouverte ne dit que "A et B sont voisins" : elle ne dit jamais lequel est
au-dessus. Deriver un arbre de ce graphe est donc une vraie logique, et c'est
elle qui se trompait -- deux PoPs relies par un lien de secours finissaient
l'un SOUS l'autre (une chaine) au lieu d'etre freres sous le coeur, et le
resultat dependait de l'ordre des liens renvoyes par la base.

Ces tests executent les fonctions reelles extraites de app.js avec Node : pas de
copie de la logique ici, sinon on testerait un double et non le code livre.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app.js"

# Bornes du bloc a extraire : de la table des rangs jusqu'a la fonction qui suit
# la disposition. Si ces reperes bougent, le test echoue franchement plutot que
# de tester du vide.
DEBUT = "const TOPO_RANG = {"
FIN = "function topoEdgeRates("

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node absent : logique d'arbre JS non verifiable"
)


@pytest.fixture(scope="module")
def harnais(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Extrait les fonctions d'arbre de app.js en un module Node importable."""
    source = APP_JS.read_text(encoding="utf-8")
    debut = source.find(DEBUT)
    fin = source.find(FIN)
    assert debut != -1, f"repere introuvable dans app.js : {DEBUT}"
    assert fin != -1, f"repere introuvable dans app.js : {FIN}"
    assert debut < fin, "reperes dans le desordre : app.js a ete reorganise"

    module = tmp_path_factory.mktemp("arbre") / "arbre.js"
    module.write_text(
        # ``topo`` est l'etat global de l'editeur ; seuls les abonnes, l'etat
        # des agregats deplies et les branches repliees servent ici.
        "const topo = { subs: [], abosOuverts: new Set(), replies: new Set() };\n"
        "const TOPO_ABOS_MAX = 25;\n"
        + source[debut:fin]
        + "\nmodule.exports = { topo, topoBuildModel, topoAutoLayout };\n",
        encoding="utf-8",
    )
    return module


def executer(harnais: Path, script: str) -> dict:
    """Lance un script Node qui doit afficher un JSON sur sa derniere ligne."""
    complet = f"const A = require({str(harnais)!r});\n{script}"
    proc = subprocess.run(
        ["node", "-e", complet], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, f"node a echoue :\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- jeux de donnees -------------------------------------------------------
# Le lien de secours PoP<->PoP est place AVANT les liens vers le coeur : c'est
# exactement ce que la base peut renvoyer, et c'est ce qui cassait l'arbre.
GRAPHE = """
const n = (k, nm, kd) => ({ key: k, name: nm, kind: kd, hidden: false, addresses: [] });
const l = (key, s, t, sur) => ({ key, source_key: s, target_key: t, interface: 'e',
  interface_links: sur ? 1 : 5, tx_bps: 1e6, rx_bps: 2e5,
  capacity_mbps: 1000, port_capacity_mbps: 1000, attributes: {} });
const nodes = [
  n('router:gw', 'GW', 'gateway'), n('router:core', 'CORE', 'core'),
  n('router:nord', 'PoP Nord', 'pop'), n('router:sud', 'PoP Sud', 'pop'),
  n('mac:AA', 'BH-Nord', 'radio'),
];
const links = [
  l('k1', 'router:nord', 'router:sud', true),
  l('k2', 'router:core', 'router:nord', true),
  l('k3', 'router:core', 'router:sud', true),
  l('k4', 'router:gw', 'router:core', true),
  l('k5', 'router:nord', 'mac:AA', true),
];
const data = { nodes, links, counts: {} };
const parents = (m) => { const o = {};
  m.nodesByKey.forEach((x) => { if (!x.synthetic) o[x.key] = x.parentKey; }); return o; };
"""


def test_les_pops_pendent_du_coeur_pas_l_un_de_l_autre(harnais: Path) -> None:
    """Regression : un lien de secours entre deux PoPs ne doit pas en faire une
    chaine. Chacun est relie au coeur : chacun doit pendre du coeur."""
    res = executer(
        harnais,
        GRAPHE
        + """
const m = A.topoBuildModel(data);
A.topoAutoLayout(m);
const p = parents(m);
console.log(JSON.stringify({
  parents: p,
  profondeurs: { sud: m.nodesByKey.get('router:sud').depth,
                 nord: m.nodesByKey.get('router:nord').depth },
  racines: m.roots.map((r) => r.key),
}));
""",
    )
    assert res["parents"]["router:nord"] == "router:core"
    # Le coeur, pas PoP Nord : c'est tout l'objet du correctif.
    assert res["parents"]["router:sud"] == "router:core"
    # Donc les deux PoPs sont au MEME etage.
    assert res["profondeurs"]["sud"] == res["profondeurs"]["nord"]
    # Et l'arbre a une seule racine : le sommet de la hierarchie.
    assert res["racines"] == ["router:gw"]


def test_l_arbre_ne_depend_pas_de_l_ordre_des_liens(harnais: Path) -> None:
    """L'ordre des lignes renvoyees par la base ne doit rien changer a l'arbre."""
    res = executer(
        harnais,
        GRAPHE
        + """
const ref = JSON.stringify(parents(A.topoBuildModel(data)));
let stable = true;
for (let i = 0; i < 200; i++) {
  const melange = links.slice().sort(() => Math.random() - 0.5);
  const m = A.topoBuildModel({ nodes, links: melange, counts: {} });
  if (JSON.stringify(parents(m)) !== ref) stable = false;
}
console.log(JSON.stringify({ stable }));
""",
    )
    assert res["stable"] is True


def test_un_lien_sur_prime_sur_un_raccourci_incertain(harnais: Path) -> None:
    """Un segment partage (switch, VLAN de gestion) ne prouve pas l'adjacence :
    un chemin SUR plus long doit lui etre prefere, meme s'il fait un saut de plus."""
    res = executer(
        harnais,
        GRAPHE
        + """
const noeuds = [n('c', 'CORE', 'core'), n('a', 'A', 'pop'), n('x', 'X', 'pop')];
const liens = [l('u', 'c', 'x', false), l('s1', 'c', 'a', true), l('s2', 'a', 'x', true)];
const m = A.topoBuildModel({ nodes: noeuds, links: liens, counts: {} });
const x = m.nodesByKey.get('x');
console.log(JSON.stringify({ parent: x.parentKey, incertain: !!(x.edge || {}).uncertain }));
""",
    )
    assert res["parent"] == "a"
    assert res["incertain"] is False


def test_faute_de_lien_sur_le_rattachement_probable_est_marque(harnais: Path) -> None:
    """Quand il n'existe QUE des segments partages, on rattache quand meme --
    mieux vaut un arbre probable qu'un tas d'orphelins -- mais c'est signale."""
    res = executer(
        harnais,
        GRAPHE
        + """
const noeuds = [n('c', 'CORE', 'core'), n('p1', 'P1', 'pop'), n('p2', 'P2', 'pop'),
                n('orph', 'Radio seule', 'radio')];
const liens = [l('s1', 'c', 'p1', false), l('s2', 'c', 'p2', false)];
const m = A.topoBuildModel({ nodes: noeuds, links: liens, counts: {} });
console.log(JSON.stringify({
  p1: m.nodesByKey.get('p1').parentKey,
  p1_incertain: !!(m.nodesByKey.get('p1').edge || {}).uncertain,
  orphelin: m.nodesByKey.get('orph').parentKey,
  racines: m.roots.map((r) => r.key).sort(),
}));
""",
    )
    assert res["p1"] == "c"
    assert res["p1_incertain"] is True
    # Une case qu'aucun lien ne relie reste une racine a part : on n'invente pas.
    assert res["orphelin"] is None
    assert res["racines"] == ["c", "orph"]


def test_le_parent_force_a_la_main_prime(harnais: Path) -> None:
    res = executer(
        harnais,
        GRAPHE
        + """
const noeuds = [n('c', 'CORE', 'core'), n('p1', 'P1', 'pop'), n('p2', 'P2', 'pop')];
noeuds[2].parent_override = 'p1';
const liens = [l('a', 'c', 'p1', true), l('b', 'c', 'p2', true)];
const m = A.topoBuildModel({ nodes: noeuds, links: liens, counts: {} });
console.log(JSON.stringify({ p2: m.nodesByKey.get('p2').parentKey }));
""",
    )
    assert res["p2"] == "p1"


def test_un_parent_force_circulaire_ne_boucle_pas(harnais: Path) -> None:
    """Deux cases qui se declarent parentes l'une de l'autre : l'arbre doit
    rester un arbre (sinon l'affichage part en boucle infinie)."""
    res = executer(
        harnais,
        GRAPHE
        + """
const noeuds = [n('a', 'A', 'pop'), n('b', 'B', 'pop')];
noeuds[0].parent_override = 'b';
noeuds[1].parent_override = 'a';
const m = A.topoBuildModel({ nodes: noeuds, links: [l('l', 'a', 'b', true)], counts: {} });
A.topoAutoLayout(m);
console.log(JSON.stringify({ racines: m.roots.length, parents: parents(m) }));
""",
    )
    assert res["racines"] == 1
    # Un seul des deux garde son parent force : la boucle est coupee.
    assert list(res["parents"].values()).count(None) == 1


def test_aucune_case_ne_se_superpose(harnais: Path) -> None:
    """Deux cases a la meme position seraient illisibles (l'une cache l'autre)."""
    res = executer(
        harnais,
        GRAPHE
        + """
const m = A.topoBuildModel(data);
A.topoAutoLayout(m);
const vus = new Set();
let collisions = 0;
m.nodesByKey.forEach((x) => {
  const k = Math.round(x.x) + ':' + Math.round(x.y);
  if (vus.has(k)) collisions++;
  vus.add(k);
});
console.log(JSON.stringify({ collisions, cases: m.nodesByKey.size }));
""",
    )
    assert res["collisions"] == 0
    assert res["cases"] == 5


def test_replier_une_branche_retire_tout_ce_qui_pend_dessous(harnais: Path) -> None:
    """Replier PoP Nord doit retirer le backhaul qui pend dessous, pas seulement
    le PoP lui-meme : une branche a moitie repliee laisserait un trait vers une
    case orpheline, et personne ne saurait de quoi elle depend."""
    res = executer(
        harnais,
        GRAPHE
        + """
A.topo.replies.add('router:nord');
const m = A.topoBuildModel(data);
A.topoAutoLayout(m);
const replies = [];
m.nodesByKey.forEach((x) => { if (x.replie) replies.push(x.key); });
console.log(JSON.stringify({
  replies: replies.sort(),
  nordVisible: m.nodesByKey.get('router:nord').replie === false,
  nordPlace: Number.isFinite(m.nodesByKey.get('router:nord').x),
}));
""",
    )
    # Le backhaul pend sous PoP Nord : il disparait avec lui.
    assert res["replies"] == ["mac:AA"]
    # La case repliee, elle, reste dessinee : c'est elle qui porte la pastille
    # permettant de rouvrir la branche.
    assert res["nordVisible"] is True
    assert res["nordPlace"] is True


def test_deplier_rend_la_branche_a_l_arbre(harnais: Path) -> None:
    """Le repli ne doit rien perdre : une branche rouverte retrouve ses cases
    et leurs positions, sans avoir a recharger la page."""
    res = executer(
        harnais,
        GRAPHE
        + """
A.topo.replies.add('router:nord');
A.topoAutoLayout(A.topoBuildModel(data));
A.topo.replies.delete('router:nord');
const m = A.topoBuildModel(data);
A.topoAutoLayout(m);
const bh = m.nodesByKey.get('mac:AA');
console.log(JSON.stringify({ replie: bh.replie, place: Number.isFinite(bh.x) }));
""",
    )
    assert res["replie"] is False
    assert res["place"] is True


def test_deux_sous_arbres_voisins_ne_se_touchent_pas(harnais: Path) -> None:
    """Colles, les feuilles d'une branche touchent celles de la suivante et
    l'oeil ne voit plus ou l'une finit. PoP Nord porte un backhaul, PoP Sud non :
    l'ecart entre les deux doit donc depasser une simple ligne."""
    res = executer(
        harnais,
        GRAPHE
        + """
const m = A.topoBuildModel(data);
A.topoAutoLayout(m);
const ys = {};
m.nodesByKey.forEach((x) => { ys[x.key] = x.y; });
console.log(JSON.stringify({ ys }));
""",
    )
    ecart = abs(res["ys"]["router:sud"] - res["ys"]["mac:AA"])
    # ROWH vaut 74 : un ecart strictement superieur prouve l'air ajoute entre
    # les deux sous-arbres.
    assert ecart > 74


def test_les_positions_enregistrees_sont_respectees(harnais: Path) -> None:
    """Une case deplacee a la main garde sa place : la disposition automatique
    ne doit pas ecraser le geste de l'exploitant."""
    res = executer(
        harnais,
        GRAPHE
        + """
const noeuds = nodes.map((x) => ({ ...x }));
noeuds[2].pos_x = 999; noeuds[2].pos_y = 640;
const m = A.topoBuildModel({ nodes: noeuds, links, counts: {} });
A.topoAutoLayout(m);
const nd = m.nodesByKey.get('router:nord');
console.log(JSON.stringify({ x: nd.x, y: nd.y }));
""",
    )
    assert res["x"] == 999
    assert res["y"] == 640


# -------------------------------------------------------------------------
# Le parent PROUVE par la configuration
# -------------------------------------------------------------------------

ANNEAU = """
// Anneau : les deux PoPs sont relies au coeur ET entre eux. Rien dans le
// graphe ne dit lequel des deux chemins est le bon -- ils ont la meme
// longueur. C'est exactement la ou l'arbre devine peut pendre un PoP sous
// son frere.
const nodes = [
  { key: 'router:coeur', name: 'Coeur', kind: 'core' },
  { key: 'router:nord',  name: 'Nord',  kind: 'pop' },
  { key: 'router:sud',   name: 'Sud',   kind: 'pop' },
];
const links = [
  { key: 'l1', source_key: 'router:coeur', target_key: 'router:nord', interface: 'ether1' },
  { key: 'l2', source_key: 'router:nord',  target_key: 'router:sud',  interface: 'ether2' },
];
"""


def test_le_parent_de_la_config_bat_le_plus_court_chemin(harnais: Path) -> None:
    """Sud n'est relie qu'a Nord dans le graphe : le calcul le pend donc sous
    Nord. Sa table de routage dit qu'il sort par le coeur -- et c'est elle qui
    doit gagner, parce qu'elle SAIT la ou le graphe suppose."""
    sans = executer(
        harnais,
        ANNEAU
        + """
const m = A.topoBuildModel({ nodes, links, counts: {} });
console.log(JSON.stringify({ parent: m.nodesByKey.get('router:sud').parentKey }));
""",
    )
    assert sans["parent"] == "router:nord", "sans la config, l'arbre pend Sud sous Nord"

    avec = executer(
        harnais,
        ANNEAU
        + """
const noeuds = nodes.map((x) => ({ ...x }));
noeuds[2].config_parent = 'router:coeur';
const m = A.topoBuildModel({ nodes: noeuds, links, counts: {} });
console.log(JSON.stringify({ parent: m.nodesByKey.get('router:sud').parentKey }));
""",
    )
    assert avec["parent"] == "router:coeur"


def test_le_parent_pose_a_la_main_bat_celui_de_la_config(harnais: Path) -> None:
    """L'operateur garde le dernier mot sur le controleur, ici comme partout."""
    res = executer(
        harnais,
        ANNEAU
        + """
const noeuds = nodes.map((x) => ({ ...x }));
noeuds[2].config_parent = 'router:coeur';
noeuds[2].parent_override = 'router:nord';
const m = A.topoBuildModel({ nodes: noeuds, links, counts: {} });
console.log(JSON.stringify({ parent: m.nodesByKey.get('router:sud').parentKey }));
""",
    )
    assert res["parent"] == "router:nord"


def test_un_parent_de_config_disparu_ne_bloque_pas_l_arbre(harnais: Path) -> None:
    """Le noeud designe peut avoir ete masque ou retire : l'arbre doit
    retomber sur son calcul, pas laisser la case orpheline."""
    res = executer(
        harnais,
        ANNEAU
        + """
const noeuds = nodes.map((x) => ({ ...x }));
noeuds[2].config_parent = 'router:fantome';
const m = A.topoBuildModel({ nodes: noeuds, links, counts: {} });
console.log(JSON.stringify({ parent: m.nodesByKey.get('router:sud').parentKey }));
""",
    )
    assert res["parent"] == "router:nord"


def test_une_case_sans_aucun_lien_reste_affichee(harnais: Path) -> None:
    """HYPOTHESE INFIRMEE, gardee comme telle.

    On a soupconne qu'un candidat ARP prive de lien vers son PoP disparaissait
    de l'arbre -- ce qui aurait explique qu'un client VLAN soit invisible. C'est
    faux : une case non rattachee devient une RACINE et garde sa ligne a elle.

    Le test existe pour que cette piste ne soit pas re-supposee : si un jour une
    case orpheline devient invisible, c'est ici qu'on le verra.
    """
    res = executer(
        harnais,
        """
const nodes = [
  { key: 'router:pop', name: 'PoP Nord', kind: 'pop' },
  { key: 'candidate:pop:10.20.0.77', name: '10.20.0.77', kind: 'candidate' },
];
const m = A.topoBuildModel({ nodes, links: [], counts: {} });
A.topoAutoLayout(m);
const c = m.nodesByKey.get('candidate:pop:10.20.0.77');
console.log(JSON.stringify({
  present: !!c, parent: c ? c.parentKey : 'absent', x: c ? c.x : null, cases: m.nodesByKey.size,
}));
""",
    )
    assert res["present"] is True
    assert res["parent"] is None, "orpheline, donc racine -- pas disparue"
    assert res["x"] is not None, "elle recoit bien une position"
    assert res["cases"] == 2


# ---------------------------------------------------------------------------
# RECONNAITRE UN ROUTEUR INTERROGE, OU QU'IL SOIT DANS LE TABLEAU
#
# Un cable entre deux routeurs interroges est vu de ses DEUX bouts, et ne
# compte qu'une ligne : celle du bout canonique. Dans un reseau EN ETOILE --
# un coeur, des PoPs autour, aucun voisin en aval -- tous les PoPs se
# retrouvaient donc du cote replie, absents de la colonne "Depuis". Le tableau
# ne nommait plus que le coeur, et l'exploitant en concluait qu'un seul routeur
# etait detecte alors que les quatre etaient lus.
#
# ``topoInterroges`` est ce qui permet de marquer l'autre bout. Si elle se
# trompe, le badge disparait et le malentendu revient.
# ---------------------------------------------------------------------------
MARQUEURS = ("function topoInterroges(", "function topoFusions(", "function topoAttrs(")


@pytest.fixture(scope="module")
def harnais_interroges(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = APP_JS.read_text(encoding="utf-8")
    morceaux = []
    for marqueur in MARQUEURS:
        debut = source.find(marqueur)
        assert debut != -1, f"repere introuvable dans app.js : {marqueur}"
        fin = source.find("\n}\n", debut)
        assert fin != -1, f"fin de fonction introuvable pour {marqueur}"
        morceaux.append(source[debut : fin + 3])

    module = tmp_path_factory.mktemp("interroges") / "interroges.js"
    module.write_text(
        "\n".join(morceaux) + "\nmodule.exports = { topoInterroges, topoFusions };\n",
        encoding="utf-8",
    )
    return module


def test_un_routeur_gere_est_reconnu_comme_interroge(harnais_interroges: Path) -> None:
    res = executer(
        harnais_interroges,
        """
const nodes = [
  { key: 'router:DS-CCR', attributes: { managed: true } },
  { key: 'router:NAS-BASSORA', attributes: { managed: true } },
  { key: 'mac:AA:00:00:00:00:FE', attributes: {} },
];
console.log(JSON.stringify({ cles: [...A.topoInterroges(nodes)].sort() }));
""",
    )
    assert res["cles"] == ["router:DS-CCR", "router:NAS-BASSORA"]


def test_un_voisin_simplement_vu_n_est_pas_marque(harnais_interroges: Path) -> None:
    """C'est TOUTE la distinction : un equipement vu en face n'apporte ni ses
    liens, ni ses abonnes, ni ses files. Le confondre avec un routeur lu
    laisserait croire que le controleur le pilote."""
    res = executer(
        harnais_interroges,
        """
const nodes = [
  { key: 'mac:AA:00:00:00:00:FE', name: 'MAIN GATEWAY', kind: 'pop', attributes: {} },
  { key: 'mac:DC:9F:DB:11:22:33', name: 'BH-Nord', kind: 'radio' },
];
console.log(JSON.stringify({ cles: [...A.topoInterroges(nodes)] }));
""",
    )
    assert res["cles"] == []


def test_les_attributs_en_json_brut_repondent_aussi(harnais_interroges: Path) -> None:
    """``attributes`` arrive en objet ou en chaine JSON selon le chemin de
    lecture. Les deux doivent marcher, sinon le badge saute une fois sur deux."""
    res = executer(
        harnais_interroges,
        """
const nodes = [{ key: 'router:DS-CCR', attributes: '{"managed": true}' }];
console.log(JSON.stringify({ cles: [...A.topoInterroges(nodes)] }));
""",
    )
    assert res["cles"] == ["router:DS-CCR"]


def test_une_liste_absente_ne_casse_rien(harnais_interroges: Path) -> None:
    res = executer(
        harnais_interroges,
        "console.log(JSON.stringify({ cles: [...A.topoInterroges(undefined)] }));",
    )
    assert res["cles"] == []


# ---------------------------------------------------------------------------
# VOIR CE QU'UNE CASE A ABSORBE
#
# La reconciliation replie en UNE case plusieurs observations du meme
# equipement. Quand elle se trompe, elle replie deux equipements DIFFERENTS et
# la case absorbe des liens qui ne lui appartiennent pas. Le compte etait
# calcule a chaque decouverte et n'apparaissait que dans le panneau d'une case
# de l'arbre, qu'il fallait penser a ouvrir -- jamais dans le tableau, la ou
# plusieurs lignes pointant vers un meme nom posent la question.
# ---------------------------------------------------------------------------
def test_une_case_qui_regroupe_plusieurs_vues_est_signalee(harnais_interroges: Path) -> None:
    res = executer(
        harnais_interroges,
        """
const nodes = [
  { key: 'mac:AA:50', merged_count: 2, members: ['mac:AA:50', 'mac:AA:51'] },
  { key: 'router:DS-CCR', merged_count: 1, members: ['router:DS-CCR'] },
];
const f = A.topoFusions(nodes);
console.log(JSON.stringify({
  cles: [...f.keys()], compte: f.get('mac:AA:50').compte,
  membres: f.get('mac:AA:50').membres,
}));
""",
    )
    assert res["cles"] == ["mac:AA:50"], "une case non fusionnee ne doit pas etre signalee"
    assert res["compte"] == 2
    assert res["membres"] == ["mac:AA:50", "mac:AA:51"]


def test_les_membres_sont_nommes_pas_seulement_comptes(harnais_interroges: Path) -> None:
    """Un compte seul ne permet pas de juger : "4 vues" est normal pour un
    equipement vu par quatre ports, et faux pour quatre equipements confondus.
    Sans la liste, l'operateur ne peut pas trancher."""
    res = executer(
        harnais_interroges,
        """
const nodes = [{ key: 'k', merged_count: 4,
                 members: ['mac:A', 'mac:B', 'mac:C', 'mac:D'] }];
console.log(JSON.stringify({ membres: A.topoFusions(nodes).get('k').membres }));
""",
    )
    assert res["membres"] == ["mac:A", "mac:B", "mac:C", "mac:D"]


def test_une_case_sans_membres_ne_casse_pas(harnais_interroges: Path) -> None:
    """Un depot d'une generation anterieure peut ne pas porter ``members``."""
    res = executer(
        harnais_interroges,
        """
const f = A.topoFusions([{ key: 'k', merged_count: 3 }]);
console.log(JSON.stringify({ compte: f.get('k').compte, membres: f.get('k').membres }));
""",
    )
    assert res["compte"] == 3
    assert res["membres"] == []


# ---------------------------------------------------------------------------
# LES ABONNES DANS L'ARBRE
#
# Un PoP porte une case "N abonne(s)". Elle s'annoncait "repliable" dans le
# code, mais rien ne la depliait : on lisait un COMPTE, jamais QUI. Or c'est
# exactement la question qu'on se pose devant un PoP qui sature.
# ---------------------------------------------------------------------------
ARBRE_ABOS = """
const nodes = [{ key: 'router:pop', name: 'PoP Nord', kind: 'pop', hidden: false }];
const data = { nodes, links: [], counts: {} };
const abonne = (login, pop, tx) => ({
  login, pop_name: pop, kind: 'pppoe', tx_bps: tx, rx_bps: 1000,
  last_ip: '10.20.0.' + login.length,
});
const cles = (m) => [...m.nodesByKey.keys()];
"""


def test_les_abonnes_sont_replies_par_defaut(harnais: Path) -> None:
    """Un PoP d'operateur en porte des centaines : aucun arbre ne se lit avec
    des centaines de cases."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = [abonne('alice', 'PoP Nord', 5e6), abonne('bob', 'PoP Nord', 2e6)];
const m = A.topoBuildModel(data);
const agregat = m.nodesByKey.get('abos:router:pop');
console.log(JSON.stringify({
  cles: cles(m), nom: agregat.name, deplie: agregat.expanded,
  enfants: agregat.children.length,
}));
""",
    )
    assert res["cles"] == ["router:pop", "abos:router:pop"]
    assert res["nom"] == "2 abonne(s)"
    assert res["deplie"] is False
    assert res["enfants"] == 0


def test_l_agregat_deplie_montre_chaque_abonne(harnais: Path) -> None:
    """LE manque : le compte ne disait pas qui."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = [abonne('alice', 'PoP Nord', 5e6), abonne('bob', 'PoP Nord', 2e6)];
A.topo.abosOuverts.add('abos:router:pop');
const m = A.topoBuildModel(data);
const agregat = m.nodesByKey.get('abos:router:pop');
console.log(JSON.stringify({
  deplie: agregat.expanded,
  enfants: agregat.children.map((c) => c.name),
  logins: agregat.children.map((c) => c.subscriber && c.subscriber.login),
}));
""",
    )
    assert res["deplie"] is True
    assert res["enfants"] == ["alice", "bob"]
    assert res["logins"] == ["alice", "bob"]


def test_un_abonne_porte_son_adresse_et_son_debit(harnais: Path) -> None:
    """C'est ce qu'on cherche en ouvrant la liste d'un PoP qui sature."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = [abonne('alice', 'PoP Nord', 5e6)];
A.topo.abosOuverts.add('abos:router:pop');
const m = A.topoBuildModel(data);
const feuille = m.nodesByKey.get('abos:router:pop|alice');
console.log(JSON.stringify({
  adresses: feuille.addresses, debit: feuille.synthRates, kind: feuille.kind,
}));
""",
    )
    assert res["adresses"] == ["10.20.0.5"]
    assert res["debit"]["down"] == 5e6
    assert res["kind"] == "cpe"


def test_un_client_a_ip_fixe_garde_sa_nature(harnais: Path) -> None:
    """Un client statique est une DECLARATION, pas un CPE observe : les
    confondre ferait croire a une decouverte la ou il n'y a qu'une saisie."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = [{ login: 'mairie', pop_name: 'PoP Nord', kind: 'static' }];
A.topo.abosOuverts.add('abos:router:pop');
const m = A.topoBuildModel(data);
console.log(JSON.stringify({ kind: m.nodesByKey.get('abos:router:pop|mairie').kind }));
""",
    )
    assert res["kind"] == "static"


def test_une_liste_trop_longue_est_bornee(harnais: Path) -> None:
    """Sinon un PoP de 800 abonnes rend l'arbre inutilisable -- et le detail se
    lit dans l'onglet Abonnes, qui est fait pour ca."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = Array.from({ length: 40 }, (_, i) => abonne('cli' + i, 'PoP Nord', 1e6));
A.topo.abosOuverts.add('abos:router:pop');
const m = A.topoBuildModel(data);
const agregat = m.nodesByKey.get('abos:router:pop');
console.log(JSON.stringify({
  enfants: agregat.children.length,
  dernier: agregat.children[agregat.children.length - 1].name,
}));
""",
    )
    assert res["enfants"] == 26, "25 abonnes + la case 'et les autres'"
    assert res["dernier"] == "+ 15 autres"


def test_un_abonne_sans_pop_n_est_rattache_nulle_part(harnais: Path) -> None:
    """On ne devine pas : le poser sous un PoP au hasard serait faux."""
    res = executer(
        harnais,
        ARBRE_ABOS
        + """
A.topo.subs = [abonne('orphelin', '', 1e6)];
const m = A.topoBuildModel(data);
console.log(JSON.stringify({ cles: cles(m) }));
""",
    )
    assert res["cles"] == ["router:pop"]


# -------------------------------------------------------------------------
# LE DESSIN LUI-MEME : la toile, l'echelle, le repli
#
# Les tests ci-dessus verifient la DISPOSITION (qui pend de qui, a quelle
# place). Ceux-ci executent ``renderTopoCanvas`` et lisent le SVG produit :
# c'est la seule facon de prouver que la toile de fond couvre vraiment le
# dessin, et pas seulement la partie visible du cadre.
# -------------------------------------------------------------------------

DEBUT_DESSIN = "const TOPO_RANG = {"
FIN_DESSIN = "function topoSelect("


@pytest.fixture(scope="module")
def harnais_dessin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Extrait le rendu de l'arbre, avec juste ce qu'il faut de DOM pour tourner."""
    source = APP_JS.read_text(encoding="utf-8")
    debut = source.find(DEBUT_DESSIN)
    fin = source.find(FIN_DESSIN)
    assert debut != -1, f"repere introuvable dans app.js : {DEBUT_DESSIN}"
    assert fin != -1, f"repere introuvable dans app.js : {FIN_DESSIN}"
    assert debut < fin, "reperes dans le desordre : app.js a ete reorganise"

    module = tmp_path_factory.mktemp("dessin") / "dessin.js"
    module.write_text(
        # Un DOM minuscule : le rendu n'a besoin que d'ecrire du HTML quelque
        # part et de connaitre la taille du cadre. ``querySelector`` rend null,
        # ce qui court-circuite proprement les branchements d'evenements.
        """
const boites = {};
const elem = (id) => (boites[id] = boites[id] || {
  id, innerHTML: '', textContent: '', clientWidth: 900, clientHeight: 500, style: {},
  querySelector: () => null, querySelectorAll: () => [],
});
const document = { getElementById: elem };
const window = { innerHeight: 900 };
const TOPO_CADRE_MIN = 360;
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const bpsShort = () => '1G';
const bpsText = () => '1 Gbps';
const pct = (v, m) => (m ? (v / m) * 100 : 0);
const severity = () => 'ok';
const KIND_COLOR = { pop: 'var(--down)', core: 'var(--accent)', gateway: 'var(--accent)',
  radio: 'var(--up)', unknown: 'var(--faint)' };
const KIND_LABEL = { pop: 'PoP', core: 'Coeur', gateway: 'Gateway', radio: 'Radio' };
const ICONE = { pop: 'POP', core: 'CORE', gateway: 'GW', radio: 'RF' };
const NODE_W = 176;
const NODE_H = 48;
const TOPO_ABOS_MAX = 25;
const topo = { data: null, subs: [], model: null, selected: null, rateOnly: false,
  linkMode: false, linkSource: null, abosOuverts: new Set(), replies: new Set(), zoom: 1 };
"""
        + source[debut:fin]
        + "\nmodule.exports = { topo, boites, renderTopoCanvas, setTopoZoom, topoFit };\n",
        encoding="utf-8",
    )
    return module


def dessiner(harnais_dessin: Path, script: str) -> dict:
    return executer(harnais_dessin, script)


def test_la_toile_couvre_tout_le_dessin_pas_seulement_le_cadre(harnais_dessin: Path) -> None:
    """LA DEMANDE : la toile de fond doit s'etendre selon la place utilisee.

    Un arbre plus grand que le cadre finissait sur du vide, parce que le
    quadrillage etait peint sur le conteneur qui defile. On force ici une case
    tres loin, puis on verifie que le rectangle de fond va bien jusque-la."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
const noeuds = nodes.map((x) => ({ ...x }));
noeuds[4].pos_x = 2400; noeuds[4].pos_y = 1700;
A.topo.data = { nodes: noeuds, links, counts: {} };
A.renderTopoCanvas();
const svg = A.boites['topo-canvas'].innerHTML;
const fond = svg.match(/<rect class="topo-fond" width="([0-9.]+)" height="([0-9.]+)"/);
const racine = svg.match(/<svg width="([0-9.]+)" height="([0-9.]+)"/);
console.log(JSON.stringify({
  fond: fond && { w: Number(fond[1]), h: Number(fond[2]) },
  racine: racine && { w: Number(racine[1]), h: Number(racine[2]) },
  motif: svg.indexOf('patternUnits="userSpaceOnUse"') !== -1,
}));
""",
    )
    assert res["motif"] is True, "le quadrillage n'est pas peint dans le dessin"
    # La case la plus lointaine finit a 2400+176 et 1700+48 : le fond doit aller
    # au-dela, sinon l'arbre deborde de sa propre toile.
    assert res["fond"]["w"] >= 2400 + 176
    assert res["fond"]["h"] >= 1700 + 48
    # Et il couvre exactement le SVG, donc toute la zone parcourue en defilant.
    assert res["fond"] == res["racine"]


def test_un_petit_arbre_remplit_quand_meme_le_cadre(harnais_dessin: Path) -> None:
    """L'inverse compte autant : trois cases ne doivent pas flotter sur un fond
    minuscule entoure de vide. La toile fait au moins la taille du cadre, dont
    le plancher est TOPO_CADRE_MIN (360 px)."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.replies.clear();
A.topo.data = { nodes, links, counts: {} };
A.renderTopoCanvas();
const svg = A.boites['topo-canvas'].innerHTML;
const fond = svg.match(/<rect class="topo-fond" width="([0-9.]+)" height="([0-9.]+)"/);
console.log(JSON.stringify({ w: Number(fond[1]), h: Number(fond[2]) }));
""",
    )
    # clientWidth du faux cadre : 900 px. En hauteur, c'est le plancher du
    # cadre qui commande, puisqu'il depasse ce petit arbre.
    assert res["w"] >= 898
    assert res["h"] >= 358


def test_l_echelle_agrandit_le_dessin_sans_bouger_les_coordonnees(
    harnais_dessin: Path,
) -> None:
    """Zoomer change la taille AFFICHEE, pas le repere du dessin : les positions
    enregistrees, les cibles de depot et les aretes restent dans le meme
    systeme de coordonnees."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.replies.clear();
A.topo.data = { nodes, links, counts: {} };
A.setTopoZoom(2);
const svg = A.boites['topo-canvas'].innerHTML;
const m = svg.match(/<svg width="([0-9.]+)" height="([0-9.]+)" viewBox="0 0 ([0-9.]+) ([0-9.]+)"/);
console.log(JSON.stringify({
  zoom: A.topo.zoom,
  affiche: { w: Number(m[1]), h: Number(m[2]) },
  repere: { w: Number(m[3]), h: Number(m[4]) },
  etiquette: A.boites['topo-zoom-level'].textContent,
}));
""",
    )
    assert res["zoom"] == 2
    assert res["affiche"]["w"] == res["repere"]["w"] * 2
    assert res["affiche"]["h"] == res["repere"]["h"] * 2
    assert res["etiquette"] == "200 %"


def test_l_echelle_reste_dans_des_bornes_utiles(harnais_dessin: Path) -> None:
    """Une echelle libre finit a 5 % (illisible) ou a 12 (une seule case a
    l'ecran) : dans les deux cas on a perdu l'arbre."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.data = { nodes, links, counts: {} };
A.setTopoZoom(50); const haut = A.topo.zoom;
A.setTopoZoom(0.01); const bas = A.topo.zoom;
A.setTopoZoom(1);
console.log(JSON.stringify({ haut, bas }));
""",
    )
    assert res["haut"] == 2
    assert res["bas"] == 0.4


def test_une_branche_repliee_n_est_plus_dessinee(harnais_dessin: Path) -> None:
    """Replier doit retirer les cases ET leurs traits : un trait vers une case
    absente serait pire que la case elle-meme."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.zoom = 1;
A.topo.replies.clear();
A.topo.data = { nodes, links, counts: {} };
A.renderTopoCanvas();
const ouvert = A.boites['topo-canvas'].innerHTML;
A.topo.replies.add('router:nord');
A.renderTopoCanvas();
const ferme = A.boites['topo-canvas'].innerHTML;
const cases = (s) => (s.match(/class="topo-node[^"]*" data-node=/g) || []).length;
const traits = (s) => (s.match(/class="topo-edge[ "]/g) || []).length;
console.log(JSON.stringify({
  casesOuvert: cases(ouvert), casesFerme: cases(ferme),
  traitsOuvert: traits(ouvert), traitsFerme: traits(ferme),
  backhaulOuvert: ouvert.indexOf('mac:AA') !== -1,
  backhaulFerme: ferme.indexOf('mac:AA') !== -1,
  pastille: ferme.indexOf('data-fold="router:nord"') !== -1,
  compte: ferme.indexOf('>+1<') !== -1,
}));
""",
    )
    assert res["casesOuvert"] == 5
    assert res["casesFerme"] == 4
    assert res["traitsFerme"] < res["traitsOuvert"]
    assert res["backhaulOuvert"] is True
    assert res["backhaulFerme"] is False
    # La case repliee garde sa pastille, et la pastille dit COMBIEN elle cache.
    assert res["pastille"] is True
    assert res["compte"] is True, "le repli doit annoncer le nombre de cases cachees"


def test_la_legende_ne_liste_que_les_roles_dessines(harnais_dessin: Path) -> None:
    """Replier PoP Nord retire le seul equipement radio : la legende ne doit
    plus proposer « Radio », sinon on cherche une case qui n'est plus la."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.replies.clear();
A.topo.data = { nodes, links, counts: {} };
A.renderTopoCanvas();
const avant = A.boites['topo-legend'].innerHTML;
A.topo.replies.add('router:nord');
A.renderTopoCanvas();
const apres = A.boites['topo-legend'].innerHTML;
console.log(JSON.stringify({
  radioAvant: avant.indexOf('Radio') !== -1,
  radioApres: apres.indexOf('Radio') !== -1,
  popApres: apres.indexOf('PoP') !== -1,
}));
""",
    )
    assert res["radioAvant"] is True
    assert res["radioApres"] is False
    assert res["popApres"] is True


def test_tout_voir_ramene_l_arbre_dans_le_cadre(harnais_dessin: Path) -> None:
    """« Tout voir » sert quand l'arbre deborde. Il ne doit jamais GROSSIR un
    petit arbre : l'agrandir ne dirait rien de plus et rendrait tout flou."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.replies.clear();
const loin = nodes.map((x) => ({ ...x }));
loin[4].pos_x = 3000; loin[4].pos_y = 40;
A.topo.data = { nodes: loin, links, counts: {} };
A.topo.zoom = 1;
A.renderTopoCanvas();
A.topoFit();
const large = A.topo.zoom;

// Deux etages seulement : 26 + 268 + 176 + 40 = 510 px, soit bien moins que
// les 900 px du cadre. Le graphe complet, lui, fait 1046 px de large et
// n'entre PAS -- s'en servir ici testerait l'inverse de ce qu'on veut.
A.topo.data = { nodes: nodes.slice(0, 2), links: [links[3]], counts: {} };
A.topo.zoom = 1;
A.renderTopoCanvas();
A.topoFit();
const petit = A.topo.zoom;
console.log(JSON.stringify({ large, petit }));
""",
    )
    assert res["large"] < 1, "un arbre trop large doit etre reduit"
    assert res["petit"] == 1, "un arbre qui tient deja ne doit pas etre grossi"


def test_le_cadre_se_cale_sur_l_arbre_et_se_retracte(harnais_dessin: Path) -> None:
    """LA DEMANDE, dans les deux sens : le fond s'etend selon la place utilisee.

    Un grand arbre remplit le cadre jusqu'a la fenetre ; un arbre replie le rend
    aussitot. Une hauteur deduite du cadre COURANT serait collante : une fois
    grande, elle le resterait apres le repli, et on retrouverait le grand vide
    qu'on cherche justement a supprimer."""
    res = dessiner(
        harnais_dessin,
        GRAPHE
        + """
A.topo.zoom = 1;
A.topo.replies.clear();
const grand = nodes.map((x) => ({ ...x }));
grand[4].pos_y = 1600;
A.topo.data = { nodes: grand, links, counts: {} };
A.renderTopoCanvas();
const haut = A.boites['topo-canvas'].style.height;

// Assez haut pour depasser le plancher, assez bas pour rester sous le
// plafond : c'est la seule plage ou le cadre epouse vraiment le dessin.
const moyen = nodes.map((x) => ({ ...x }));
moyen[4].pos_y = 500;
A.topo.data = { nodes: moyen, links, counts: {} };
A.renderTopoCanvas();
const normal = A.boites['topo-canvas'].style.height;

A.topo.data = { nodes, links, counts: {} };
A.topo.replies.add('router:core');
A.renderTopoCanvas();
const replie = A.boites['topo-canvas'].style.height;
console.log(JSON.stringify({ haut, normal, replie }));
""",
    )
    px = lambda v: float(str(v).removesuffix("px"))  # noqa: E731
    # innerHeight vaut 900 dans le faux DOM : le cadre plafonne a 700.
    assert px(res["haut"]) == 700
    # L'arbre normal tient sous le plafond : le cadre epouse son etendue.
    assert 360 < px(res["normal"]) < 700
    # Et il SE RETRACTE au repli, jusqu'au plancher.
    assert px(res["replie"]) == 360
    assert px(res["replie"]) < px(res["normal"])

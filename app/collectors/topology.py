"""Decouverte de topologie : quel lien va ou.

SIX SOURCES, RECONCILIEES
-------------------------
Aucune ne suffit seule ; ensemble elles donnent le graphe complet.

1. ``/ip/neighbor``    MNDP / LLDP / CDP. Source maitresse : pour chaque interface
                       locale, elle nomme l'equipement d'en face (identite,
                       plateforme, MAC, IP). C'est l'adjacence physique.
2. ``/interface/ethernet``  debit negocie = plafond physique du lien.
3. ``/ip/address``     rattache chaque interface a un segment L3.
4. UISP                liens radio PtP/PtMP et capacite reelle du moment.
5. ``/ppp/active``     et surtout son champ ``caller-id``, qui porte la MAC du
                       CPE de l'abonne.
6. ``/ip/arp``         presence sur les VLAN routees sans PPPoE. La seule source
                       des trois natures qui ne dit PAS qui est en face : elle
                       produit des candidats a declarer, jamais des abonnes
                       (cf. app/collectors/vlan_clients.py).
7. LA CONFIGURATION    ``/ip/route``, OSPF/BGP, empilement VLAN/bridge/bonding.
                       Les six sources ci-dessus decrivent ce que les routeurs
                       VOIENT ; celle-ci decrit ce qu'ils FONT, et c'est elle
                       qui donne sa hierarchie a l'arbre
                       (cf. app/collectors/config_graph.py).

L'IDENTITE D'UN ROUTEUR : SON LOOPBACK
--------------------------------------
Un routeur gere est identifie par son adresse de LOOPBACK, et par elle seule
quand elle est connue. C'est le seul identifiant qui tienne :

  - son NOM peut changer, et n'est unique que par convention ;
  - sa MAC depend du port par lequel on le regarde, et suit le materiel ;
  - une adresse d'INTERFACE ne l'identifie pas : un /30 de liaison appartient
    aux deux bouts, et les configurations modeles donnent souvent le meme /30 a
    tous les sites. Deux PoPs deployes au meme modele deviennent alors
    indiscernables, et les liens du coeur aboutissent sur le mauvais.

Le loopback, lui, est unique par construction dans un reseau d'operateur et ne
depend d'aucune interface. Il est declare dans l'inventaire, ou deduit (adresse
d'hote sur une interface ``lo*``, ou ``router-id`` de l'export), et le graphe
dit toujours d'ou il vient pour que l'operateur puisse le corriger.

LA NATURE D'UN ROUTEUR : SON ROLE DECLARE
-----------------------------------------
Passerelle, coeur ou PoP viennent de l'inventaire, pas d'une heuristique sur la
plateforme : c'est ce qui donne sa hierarchie a l'arbre.

LA JOINTURE QUI COMPTE
----------------------
``caller-id`` (MAC du CPE, cote RouterOS) contre la MAC des stations connues
d'UISP : c'est le seul moyen de savoir par QUEL secteur radio passe un abonne,
donc quelle est sa vraie chaine de goulots. Sans elle on sait seulement qu'il
est sur un PoP.

Ce module est en LECTURE SEULE. Il produit un graphe ; ce qu'on en fait
(shaping) vit dans app/enforcement/.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.collectors.parsing import parse_bitrate, parse_flag

logger = logging.getLogger(__name__)

# Types de noeuds, du plus haut au plus bas dans l'arbre de shaping.
KIND_GATEWAY = "gateway"
KIND_CORE = "core"
KIND_POP = "pop"
KIND_RADIO = "radio"
KIND_SECTOR = "sector"
KIND_CPE = "cpe"
# Client a IP fixe. Sa propre nature, et surtout PAS KIND_CPE : un CPE est un
# equipement observe (il a une MAC, il apparait dans UISP, sa presence se
# verifie). Un client statique est une DECLARATION -- il n'y a rien a voir sur
# le reseau qui le distingue de son voisin. Les confondre ferait croire a une
# decouverte la ou il n'y a qu'une saisie.
KIND_STATIC = "static"
# TROISIEME NATURE, distincte des deux sources historiques.
#
# Un noeud de ce graphe vient jusqu'ici soit d'une adjacence /ip/neighbor (un
# equipement reseau), soit d'un caller-id PPPoE (un abonne identifie). Une
# entree ARP sur une VLAN routee n'est ni l'un ni l'autre : on sait qu'une
# adresse parle, on ne sait pas QUI. La confondre avec un voisin ferait croire
# a de l'infrastructure, la confondre avec un CPE ou un abonne ferait croire a
# un client identifie -- les deux seraient faux, et le second serait dangereux
# (il n'a ni plan, ni contrat, ni file).
KIND_CANDIDATE = "candidate"
KIND_UNKNOWN = "unknown"

# Role DECLARE dans l'inventaire -> nature dans le graphe.
#
# Ces trois natures portent la hierarchie de l'arbre (cf. TOPO_RANG cote
# interface : gateway 0, core 1, pop 2). Les ignorer -- ce que faisait ce module
# en posant KIND_POP pour tout routeur gere -- met la passerelle, le coeur et
# les PoPs au meme niveau : l'arbre s'aplatit et sa racine devient arbitraire.
# L'operateur a declare ce role a la saisie ; c'est lui qui fait foi, pas une
# heuristique sur la plateforme.
ROLE_KINDS: dict[str, str] = {
    "gateway": KIND_GATEWAY,
    "core": KIND_CORE,
    "pop": KIND_POP,
}


def kind_for_role(role: Any) -> str:
    """Nature d'un routeur GERE d'apres son role declare. Defaut : PoP."""
    return ROLE_KINDS.get(str(role or "").strip().lower(), KIND_POP)


# Un lien est physique (cable/radio) ou logique (session PPPoE).
LINK_ETHERNET = "ethernet"
LINK_RADIO = "radio"
LINK_PPPOE = "pppoe"
# Rattachement declare d'un client a IP fixe. Le nommer a part evite de le
# confondre avec une adjacence observee : personne ne l'a mesure.
LINK_STATIC = "static"
# Rattachement DEDUIT d'une observation : ni mesure, ni declaration.
LINK_DETECTED = "detected"
# Adjacence PROUVEE par une session de routage etablie (OSPF, BGP).
LINK_ROUTING = "routing"

# Identites trop generiques pour prouver que deux noeuds sont le meme equipement :
# beaucoup de MikroTik gardent l'identite par defaut "MikroTik". On ne fusionne
# jamais deux equipements sur un nom pareil -- seul un MAC commun le peut alors.
GENERIC_NAMES = {"", "?", "mikrotik", "routeros", "routerboard"}


def _as_attributes(value: Any) -> dict[str, Any]:
    """``attributes`` revient tantot en dict, tantot en JSON brut (asyncpg)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _merge_signatures(node: dict[str, Any]) -> set[str]:
    """Signaux qui prouvent une IDENTITE d'equipement (pas une simple adjacence).

    Le MAC est le plus sur. Un routeur gere porte PLUSIEURS MAC (une par
    interface) : quand un autre routeur le voit en voisin, il ne connait que la
    MAC de l'interface en face. On expose donc TOUTES ses MAC (``attributes.macs``)
    pour que la fusion aboutisse quelle que soit l'interface observee. Le nom
    (identite RouterOS) sert aussi, une fois insensibilise a la casse --
    ``NAS-FRANCOPHONIE`` et ``NAS-francophonie`` sont le meme routeur -- SAUF s'il
    est generique. On NE fusionne PAS sur l'adresse IP : un meme equipement porte
    plusieurs IP, et deux equipements distincts peuvent partager une IP de segment.
    C'est justement pour ca qu'on RASSEMBLE les adresses sur un seul noeud au lieu
    de dedoubler.
    """
    signatures: set[str] = set()
    attrs = _as_attributes(node.get("attributes"))
    # Numero de serie : l'identifiant qui ne bouge JAMAIS. Deux cases qui le
    # partagent sont le meme routeur, quelles que soient ses adresses ou son nom.
    serial = str(attrs.get("serial") or "").strip().lower()
    if serial:
        signatures.add("serial:" + serial)
    # Toutes les MAC connues de l'equipement : le champ principal + celles de ses
    # interfaces (routeur gere) + une eventuelle MAC de gestion.
    macs = [node.get("mac"), attrs.get("mgmt_mac")]
    macs.extend(attrs.get("macs") or [])
    for brut in macs:
        mac = normalize_mac(brut)
        if mac:
            signatures.add("mac:" + mac)
    # Nom affiche ET identite RouterOS reelle (le nom d'un PoP gere est souvent
    # un libelle "PoP Nord", alors que ses voisins le voient sous son identite).
    for source in (node.get("name"), attrs.get("identity")):
        nom = str(source or "").strip().lower()
        if nom and nom not in GENERIC_NAMES:
            signatures.add("name:" + nom)
    return signatures


def _pick_canonical(
    members: list[dict[str, Any]], preferred: set[str] | None = None
) -> dict[str, Any]:
    """Choisit la case qui represente le groupe.

    On respecte d'abord le canonique DECLARE par l'operateur (``preferred``),
    puis le noeud du routeur GERE (cle ``router:``), stable et porteur de la
    disposition sauvegardee ; a defaut celui qui a une position ou un parent pose
    a la main, puis un MAC, puis le premier venu.
    """
    prefs = preferred or set()

    def rang(node: dict[str, Any]) -> tuple[Any, ...]:
        key = node.get("key", "")
        return (
            0 if key in prefs else 1,
            0 if key.startswith("router:") else 1,
            0 if (node.get("pos_x") is not None or node.get("parent_override")) else 1,
            0 if key.startswith("mac:") else 1,
        )

    return sorted(members, key=rang)[0]


def reconcile_topology(
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    forced_merges: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fusionne les doublons : un meme equipement, une seule case.

    La decouverte voit le meme routeur plusieurs fois -- comme PoP gere ET comme
    voisin du coeur, via plusieurs protocoles, sous des casses differentes. Sans
    reconciliation, l'arbre montre dix cases pour cinq routeurs. On regroupe donc
    par identite (MAC ou nom non generique), on garde une case canonique, on y
    RASSEMBLE toutes les adresses, et on recable les liens vers elle. Un lien
    devenu interne a un equipement fusionne (source == cible) disparait.

    ``forced_merges`` porte les fusions DECLAREES par l'operateur (alias_key ->
    canonical_key) : le dernier mot, quand l'automatique ne peut pas prouver
    l'identite (nom generique, pas de MAC commune). C'est le levier de precision
    maximale de l'arbre.

    Purement de lecture : ne touche ni la base ni les equipements. La cle des
    liens est conservee telle quelle, pour que la mesure de debit continue de la
    retrouver.
    """
    parent: dict[str, str] = {n["key"]: n["key"] for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    par_signature: dict[str, str] = {}
    for node in nodes:
        for signature in _merge_signatures(node):
            if signature in par_signature:
                union(node["key"], par_signature[signature])
            else:
                par_signature[signature] = node["key"]

    # Fusions manuelles : appliquees APRES les signatures, elles ne peuvent que
    # rapprocher davantage (jamais separer ce que la signature a uni). On ignore
    # une regle dont un cote a disparu du graphe.
    for alias_key, canonical_key in (forced_merges or {}).items():
        if alias_key in parent and canonical_key in parent:
            union(alias_key, canonical_key)

    groupes: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        groupes.setdefault(find(node["key"]), []).append(node)

    canoniques_forces = set((forced_merges or {}).values())
    canonique_de: dict[str, str] = {}
    fusionnes: list[dict[str, Any]] = []
    for membres in groupes.values():
        canon = dict(_pick_canonical(membres, canoniques_forces))
        # Toutes les adresses de l'equipement, rassemblees plutot que dedoublees.
        adresses = sorted({str(m.get("address")) for m in membres if m.get("address")})
        for champ in ("mac", "platform", "version", "uisp_device_id", "config_parent"):
            if not canon.get(champ):
                for m in membres:
                    if m.get(champ):
                        canon[champ] = m[champ]
                        break
        # Un role connu prime sur "unknown", meme si le canonique est indetermine.
        if canon.get("kind") in (None, KIND_UNKNOWN):
            for m in membres:
                if m.get("kind") and m["kind"] != KIND_UNKNOWN:
                    canon["kind"] = m["kind"]
                    break
        canon["addresses"] = adresses
        if adresses and not canon.get("address"):
            canon["address"] = adresses[0]
        canon["merged_count"] = len(membres)
        canon["members"] = [m["key"] for m in membres]
        canon["fresh"] = any(m.get("fresh") for m in membres)
        for m in membres:
            canonique_de[m["key"]] = canon["key"]
        fusionnes.append(canon)

    # Le parent prouve par la config designe une case qui vient peut-etre
    # d'etre fusionnee : sans ce recablage il pointerait dans le vide, et
    # l'arbre retomberait silencieusement sur son calcul de plus court chemin.
    for canon in fusionnes:
        parent_config = canon.get("config_parent")
        if parent_config:
            remplacant = canonique_de.get(parent_config, parent_config)
            canon["config_parent"] = None if remplacant == canon["key"] else remplacant

    par_cle = {n["key"]: n for n in fusionnes}
    liens_sortie: list[dict[str, Any]] = []
    for lien in links:
        source = canonique_de.get(lien["source_key"], lien["source_key"])
        cible = canonique_de.get(lien["target_key"], lien["target_key"])
        if source == cible:
            continue  # lien interne a un equipement fusionne : rien a montrer
        nouveau = {**lien, "source_key": source, "target_key": cible}
        if source in par_cle:
            nouveau["source_name"] = par_cle[source].get("name", nouveau.get("source_name"))
        if cible in par_cle:
            nouveau["target_name"] = par_cle[cible].get("name", nouveau.get("target_name"))
            nouveau["target_kind"] = par_cle[cible].get("kind", nouveau.get("target_kind"))
        liens_sortie.append(nouveau)

    return fusionnes, liens_sortie


def normalize_mac(value: Any) -> str | None:
    """Ramene une MAC a la forme canonique AA:BB:CC:DD:EE:FF.

    Indispensable : RouterOS ecrit ``AA:BB:CC:DD:EE:FF``, UISP parfois
    ``aa-bb-cc-dd-ee-ff`` ou sans separateur. Sans normalisation, la jointure
    entre les deux mondes echoue silencieusement.
    """
    if value is None:
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", str(value))
    if len(digits) != 12:
        return None
    digits = digits.upper()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


@dataclass(slots=True)
class TopologyNode:
    """Un equipement du reseau."""

    key: str  # identifiant stable, prefixe par sa source
    name: str
    kind: str = KIND_UNKNOWN
    mac: str | None = None
    address: str | None = None
    platform: str | None = None
    version: str | None = None
    router_name: str | None = None  # PoP qui l'a decouvert
    uisp_device_id: str | None = None
    # Parent PROUVE PAR LA CONFIGURATION : la route par defaut de cet
    # equipement sort vers ce noeud. Ce n'est pas une deduction de graphe, c'est
    # la relation hierarchique telle que le routeur l'applique. None = inconnue,
    # et l'arbre retombe alors sur son calcul de plus court chemin.
    config_parent: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopologyLink:
    """Une adjacence entre deux noeuds, avec sa capacite."""

    source_key: str
    target_key: str
    kind: str
    interface: str | None = None
    # Plafond physique (debit negocie du port, ou capacite radio mesuree).
    capacity_mbps: float | None = None
    discovered_by: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source_key}|{self.interface or ''}|{self.target_key}"


@dataclass(slots=True)
class TopologySnapshot:
    nodes: dict[str, TopologyNode] = field(default_factory=dict)
    links: dict[str, TopologyLink] = field(default_factory=dict)
    # Abonne -> secteur radio, quand la jointure caller-id a abouti.
    subscriber_sectors: dict[str, str] = field(default_factory=dict)
    # Routeur -> {interface logique: ses ports physiques}. Issu de la config,
    # c'est ce qui permet de dire par quel port sort le trafic d'un client.
    interface_paths: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_node(self, node: TopologyNode) -> TopologyNode:
        existing = self.nodes.get(node.key)
        if existing is None:
            self.nodes[node.key] = node
            return node
        # Fusion : on complete les champs manquants sans ecraser ce qu'on sait.
        for attribut in (
            "name",
            "mac",
            "address",
            "platform",
            "version",
            "uisp_device_id",
            "config_parent",
        ):
            if getattr(existing, attribut) is None and getattr(node, attribut) is not None:
                setattr(existing, attribut, getattr(node, attribut))
        if existing.kind == KIND_UNKNOWN and node.kind != KIND_UNKNOWN:
            existing.kind = node.kind
        existing.attributes.update(node.attributes)
        return existing

    def add_link(self, link: TopologyLink) -> None:
        existing = self.links.get(link.key)
        if existing is None:
            self.links[link.key] = link
            return
        if existing.capacity_mbps is None and link.capacity_mbps is not None:
            existing.capacity_mbps = link.capacity_mbps
        existing.attributes.update(link.attributes)


def classify_platform(platform: str | None, board: str | None = None) -> str:
    """Devine le role d'un equipement d'apres sa plateforme annoncee.

    Heuristique assumee : elle donne un point de depart lisible dans
    l'interface, que l'operateur peut corriger. Mieux vaut un graphe presque
    juste et modifiable qu'un graphe vide.
    """
    text = f"{platform or ''} {board or ''}".lower()
    if not text.strip():
        return KIND_UNKNOWN
    if "mikrotik" in text or "routeros" in text or "routerboard" in text or "chr" in text:
        return KIND_POP
    if any(
        marque in text
        for marque in (
            "ubiquiti",
            "ubnt",
            "airmax",
            "airfiber",
            "rocket",
            "powerbeam",
            "litebeam",
            "nanostation",
            "lhg",
        )
    ):
        return KIND_RADIO
    if "cambium" in text or "mimosa" in text:
        return KIND_RADIO
    return KIND_UNKNOWN


# Une interface de loopback se nomme presque toujours ainsi sur RouterOS : 'lo'
# (interface reelle en v7), ou un bridge sans port appele 'loopback' / 'lo0'.
#
# L'ancienne version de ce motif etait ancree des deux cotes sur une poignee de
# formes exactes, et ratait donc les conventions les plus repandues en
# production : 'dummy0' (le nom que prend l'interface de loopback sur beaucoup
# de deploiements), 'lo-bridge', 'br-loopback', 'Loopback-RID'. Un loopback
# manque n'est pas anodin : le routeur retombe alors sur sa MAC et ses adresses
# d'interface pour s'identifier, et deux PoPs deployes depuis la meme
# configuration modele (donc portant le meme /30) deviennent indiscernables.
#
# On reste STRICT sur une chose : le nom doit designer un loopback, pas
# simplement le contenir n'importe ou. 'bridge-local' ne doit pas passer pour un
# loopback parce qu'il commence par 'lo' au milieu du mot.
_LOOPBACK_INTERFACE = re.compile(
    r"""^(?:
        lo\d*                       # lo, lo0, lo1
      | loopback[\w.-]*             # loopback, loopback0, loopback-rid
      | dummy\d*                    # dummy, dummy0 : l'autre convention courante
      | (?:br|bridge)[\w.-]*[_.-]?(?:lo|loopback|dummy)\d*   # bridge-loopback, br_lo0
      | (?:lo|loopback|dummy)[_.-][\w.-]*                    # lo-bridge, loopback_rid
    )$""",
    re.I | re.X,
)

# Un commentaire d'adresse le dit souvent explicitement, la ou le nom de
# l'interface ne dit rien ('bridge1' portant le loopback, commente "loopback").
# C'est de la CONFIGURATION ecrite par l'exploitant : une intention, pas une
# deduction.
_LOOPBACK_COMMENT = re.compile(r"\b(loopback|router[- ]?id|lo0?)\b", re.I)


def _host_address(valeur: Any) -> tuple[str, int] | None:
    """``("10.255.0.1", 32)`` si la valeur est une adresse exploitable."""
    texte = str(valeur or "").strip()
    if not texte:
        return None
    try:
        interface = ipaddress.ip_interface(texte)
    except ValueError:
        return None
    ip = interface.ip
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return None
    return str(ip), interface.network.prefixlen


def loopback_from_addresses(rows: Sequence[dict[str, Any]]) -> tuple[str, str] | None:
    """Trouve le loopback d'un routeur dans ``/ip/address``.

    Renvoie ``(adresse, raison)``, la raison servant a l'afficher : l'operateur
    doit pouvoir voir POURQUOI le controleur a choisi cette adresse, et la
    corriger si la deduction est mauvaise.

    Trois niveaux, dans cet ordre :

    1. une adresse d'hote portee par une interface nommee ``lo``, ``lo0``,
       ``loopback``, ``dummy0``... C'est la convention, et elle est sans
       ambiguite.
    2. une adresse d'hote que son COMMENTAIRE designe comme le loopback. Beaucoup
       de configurations posent le loopback sur un ``bridge1`` quelconque et
       l'annotent ("loopback", "router-id") : le commentaire est alors la seule
       trace de l'intention, et c'est une declaration de l'exploitant, pas une
       devinette.
    3. a defaut, une adresse en /32 (ou /128) posee ailleurs. Un /32 sur un
       routeur ne sert a peu pres qu'a ca -- mais c'est une deduction, pas une
       certitude, et la raison le dit.

    Une adresse d'interface ordinaire n'est JAMAIS retenue : un /30 appartient
    aux deux bouts du lien, il ne peut identifier ni l'un ni l'autre.
    """
    candidats_nommes: list[str] = []
    candidats_commentes: list[str] = []
    candidats_hotes: list[str] = []
    for row in rows:
        if parse_flag(row.get("disabled")):
            continue
        analyse = _host_address(row.get("address"))
        if analyse is None:
            continue
        adresse, prefixe = analyse
        nom = str(row.get("interface") or "").strip()
        commentaire = str(row.get("comment") or "").strip()
        est_hote = prefixe == ipaddress.ip_address(adresse).max_prefixlen
        if not est_hote:
            continue
        if _LOOPBACK_INTERFACE.match(nom):
            candidats_nommes.append(adresse)
        elif commentaire and _LOOPBACK_COMMENT.search(commentaire):
            candidats_commentes.append(adresse)
        else:
            candidats_hotes.append(adresse)

    if candidats_nommes:
        return sorted(candidats_nommes)[0], "interface de loopback"
    if candidats_commentes:
        return sorted(candidats_commentes)[0], "commentaire d'adresse"
    if candidats_hotes:
        return sorted(candidats_hotes)[0], "adresse en /32"
    return None


def pick_loopback(
    *,
    declared: str | None,
    addresses: Sequence[dict[str, Any]],
    router_id: str | None = None,
    router_ids: Sequence[str] | None = None,
) -> tuple[str | None, str]:
    """Choisit le loopback qui fera foi, et dit d'ou il vient.

    L'ordre encode qui a le dernier mot :

    1. **la declaration de l'operateur**. Elle bat toute deduction -- c'est la
       convention de tout ce depot, et la seule facon de rattraper un reseau
       qui ne suit pas les usages ;
    2. une interface de loopback explicite ;
    3. le ``router-id`` de la configuration de routage : dans un reseau
       d'operateur, le router-id EST le loopback, c'est meme sa raison d'etre.
       Plusieurs valeurs peuvent remonter (``/routing/id``, instances OSPF et
       BGP, texte de l'export) : on retient la premiere qui est une VRAIE
       adresse, car une instance peut referencer une entree ``/routing/id`` par
       son NOM, et un ``router-id`` a 0.0.0.0 signifie "pas encore elu" ;
    4. le commentaire d'une adresse d'hote qui se declare loopback ;
    5. un /32 isole, faute de mieux.

    ``router_id`` (singulier) reste accepte pour les appelants existants.
    """
    if declared:
        analyse = _host_address(declared)
        if analyse is not None:
            return analyse[0], "declare"

    trouve = loopback_from_addresses(addresses)
    if trouve is not None and trouve[1] == "interface de loopback":
        return trouve

    candidats = [*(router_ids or []), *([router_id] if router_id else [])]
    for valeur in candidats:
        analyse = _host_address(valeur)
        if analyse is not None:
            return analyse[0], "router-id"

    if trouve is not None:
        return trouve
    return None, "introuvable"


def router_node_key(router_name: str) -> str:
    return f"router:{router_name}"


# MNDP annonce plus d'une adresse. RouterOS a change de nom de champ au fil
# des versions, et les listes sont separees par des virgules.
_NEIGHBOR_ADDRESS_FIELDS = (
    "address",
    "address4",
    "address6",
    "ipv4-addresses",
    "ipv6-addresses",
    "unicast-ipv4-addresses",
    "unicast-ipv6-addresses",
)


def neighbor_addresses(neighbor: dict[str, Any]) -> list[str]:
    """Toutes les adresses qu'un voisin annonce, sans doublon.

    POURQUOI TOUTES, ET PAS SEULEMENT LA PREMIERE. MNDP annonce l'adresse de
    l'interface par laquelle il parle -- une adresse de liaison, partagee avec
    nous, donc incapable de l'identifier. Mais il annonce aussi, selon la
    version, la liste de ses autres adresses, et c'est la que se trouve son
    LOOPBACK. N'en garder qu'une revient a jeter la seule qui prouve quelque
    chose, et a retomber sur la MAC ou le nom.
    """
    vues: list[str] = []
    for champ in _NEIGHBOR_ADDRESS_FIELDS:
        brut = neighbor.get(champ)
        if not brut:
            continue
        for morceau in str(brut).split(","):
            adresse = morceau.split("/")[0].strip()
            if adresse and adresse not in vues:
                vues.append(adresse)
    return vues


def neighbor_node_key(neighbor: dict[str, Any]) -> str:
    """Cle stable d'un voisin : MAC de preference, sinon identite, sinon IP.

    La MAC est le seul identifiant qui survive a un changement de nom ou
    d'adresse, et c'est aussi ce qui permet la jointure avec UISP.
    """
    mac = normalize_mac(neighbor.get("mac-address"))
    if mac:
        return f"mac:{mac}"
    identity = str(neighbor.get("identity") or "").strip()
    if identity:
        return f"identity:{identity}"
    address = str(neighbor.get("address") or neighbor.get("address4") or "").strip()
    return f"address:{address}" if address else "unknown:?"


def ethernet_capacity_mbps(row: dict[str, Any]) -> float | None:
    """Debit negocie d'un port ethernet, en Mbps.

    RouterOS ecrit ``1Gbps``, ``100Mbps``, parfois ``rate`` plutot que ``speed``.
    C'est le plafond physique : shaper au-dessus n'aurait aucun effet.
    """
    for champ in ("rate", "speed"):
        brut = row.get(champ)
        if not brut:
            continue
        texte = str(brut).strip().lower().replace("bps", "")
        bits = parse_bitrate(texte)
        if bits:
            return bits / 1_000_000.0
    return None


def _router_macs(interfaces: list[dict[str, Any]], ethernet: list[dict[str, Any]]) -> list[str]:
    """Toutes les MAC propres au routeur, normalisees et dedupliquees.

    Ce sont ces MAC qui permettent de reconnaitre le routeur quand un AUTRE
    routeur le voit en voisin : le voisinage ne revele que la MAC de l'interface
    en face, donc il faut les connaitre toutes pour garantir la fusion.
    """
    vues: list[str] = []
    for rangee in (*interfaces, *ethernet):
        for champ in ("mac-address", "orig-mac-address"):
            mac = normalize_mac(rangee.get(champ))
            if mac and mac not in vues:
                vues.append(mac)
    return vues


def build_from_router(
    snapshot: TopologySnapshot,
    *,
    router_name: str,
    pop_name: str,
    host: str,
    neighbors: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    ethernet: list[dict[str, Any]],
    addresses: list[dict[str, Any]],
    identity: str | None = None,
    serial: str | None = None,
    role: str | None = None,
    loopback: str | None = None,
    loopback_source: str | None = None,
) -> None:
    """Ajoute au graphe ce qu'un routeur voit autour de lui.

    Le noeud du routeur gere porte son LOOPBACK -- son identite dans le reseau,
    unique par construction et independante de toute interface -- ainsi que son
    numero de serie, son identite RouterOS et toutes ses MAC. Le loopback est ce
    qui permet de le reconnaitre a coup sur quand un autre routeur le voit en
    voisin, au lieu de le dedoubler.

    Sa nature vient du ROLE DECLARE (passerelle, coeur, PoP), pas d'une
    heuristique : c'est elle qui donne sa hierarchie a l'arbre.
    """
    router_key = router_node_key(router_name)
    macs = _router_macs(interfaces, ethernet)
    attributs: dict[str, Any] = {"managed": True}
    if identity:
        attributs["identity"] = str(identity)
    if serial:
        attributs["serial"] = str(serial)
    if macs:
        attributs["macs"] = macs
    if role:
        attributs["role"] = str(role)
    # Le loopback voyage dans les attributs : l'interface doit pouvoir le
    # montrer, et dire d'ou il vient, pour que l'operateur corrige une
    # deduction douteuse au lieu de subir un arbre faux sans savoir pourquoi.
    if loopback:
        attributs["loopback"] = loopback
        attributs["loopback_source"] = loopback_source or "inconnu"
    snapshot.add_node(
        TopologyNode(
            key=router_key,
            name=pop_name or router_name,
            kind=kind_for_role(role),
            mac=macs[0] if macs else None,
            address=host,
            router_name=router_name,
            attributes=attributs,
        )
    )

    capacites = {}
    for row in ethernet:
        nom = str(row.get("name") or "")
        if nom:
            capacites[nom] = ethernet_capacity_mbps(row)

    reseaux: dict[str, list[str]] = {}
    for row in addresses:
        interface = str(row.get("interface") or "")
        if interface:
            reseaux.setdefault(interface, []).append(str(row.get("address") or ""))

    types_interface = {str(row.get("name") or ""): str(row.get("type") or "") for row in interfaces}

    for neighbor in neighbors:
        interface = str(neighbor.get("interface") or "").strip()
        if not interface:
            continue
        cle = neighbor_node_key(neighbor)
        if cle.startswith("unknown"):
            continue

        platform = neighbor.get("platform")
        annonces = neighbor_addresses(neighbor)
        snapshot.add_node(
            TopologyNode(
                key=cle,
                name=str(
                    neighbor.get("identity")
                    or neighbor.get("mac-address")
                    or neighbor.get("address")
                    or "?"
                ),
                kind=classify_platform(platform, neighbor.get("board")),
                mac=normalize_mac(neighbor.get("mac-address")),
                address=str(neighbor.get("address") or neighbor.get("address4") or "") or None,
                platform=str(platform) if platform else None,
                version=str(neighbor.get("version")) if neighbor.get("version") else None,
                router_name=router_name,
                # Toutes les adresses annoncees : c'est parmi elles que la
                # reconciliation cherchera un loopback connu.
                attributes={"addresses": annonces} if annonces else {},
            )
        )
        snapshot.add_link(
            TopologyLink(
                source_key=router_key,
                target_key=cle,
                kind=LINK_ETHERNET,
                interface=interface,
                capacity_mbps=capacites.get(interface),
                discovered_by=router_name,
                attributes={
                    "interface_type": types_interface.get(interface),
                    "local_networks": reseaux.get(interface, []),
                    "discovery": str(
                        neighbor.get("discovered-by") or neighbor.get("protocol") or "mndp"
                    ),
                },
            )
        )


def _parse_export_kv(reste: str) -> dict[str, str]:
    """Découpe une ligne ``add …`` / ``set …`` d'un export en paires clé=valeur.

    Gère les valeurs entre guillemets (``comment="Lien vers PoP Nord"``) et les
    drapeaux nus (``disabled`` sans ``=``, ignorés)."""
    paires: dict[str, str] = {}
    for cle, val_q, val_nu in re.findall(r'([\w.-]+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))', reste):
        # Une seule alternative capture : entre guillemets -> val_q (branche prise
        # meme si vide, ex. comment=""), sinon la valeur nue val_nu.
        paires[cle] = val_q if val_nu == "" else val_nu
    return paires


# Menus de tunnel dont le champ remote-address nomme le routeur d'en face.
_TUNNEL_SECTIONS = {
    "/interface eoip": "eoip",
    "/interface gre": "gre",
    "/interface ipip": "ipip",
    "/interface vpls": "vpls",
    "/interface l2tp-client": "l2tp",
}


def parse_export(text: str) -> dict[str, Any]:
    """Analyse un ``/export`` RouterOS (texte) en structures exploitables.

    Renvoie ``{"addresses": [...], "tunnels": [...], "comments": {...},
    "router_ids": [...]}`` :
      - ``addresses``  : ``{address, interface, comment}`` de ``/ip address`` ;
      - ``tunnels``    : ``{type, name, remote_address, local_address}`` des tunnels
        (leur ``remote-address`` identifie le routeur pair) ;
      - ``comments``   : interface -> commentaire (souvent le nom du bout d'en face) ;
      - ``router_ids`` : les ``router-id`` declares dans les sections ``/routing``.
        Dans un reseau d'operateur, le router-id EST le loopback : c'est le
        meilleur indice quand aucune interface ne s'appelle ``lo``.

    Purement textuel et sans effet de bord : on peut donc l'appliquer aussi bien à
    l'export lu par l'API qu'à un export collé à la main. Tolérant : une ligne
    incomprise est ignorée, jamais une exception.
    """
    addresses: list[dict[str, str]] = []
    tunnels: list[dict[str, str]] = []
    comments: dict[str, str] = {}
    router_ids: list[str] = []
    section = ""
    for ligne_brute in (text or "").splitlines():
        ligne = ligne_brute.strip()
        if not ligne or ligne.startswith("#"):
            continue
        if ligne.startswith("/"):
            section = ligne
            continue
        if not (ligne.startswith("add ") or ligne.startswith("set ")):
            continue
        kv = _parse_export_kv(ligne)
        if section == "/ip address" and kv.get("address"):
            addresses.append(
                {
                    "address": kv["address"],
                    "interface": kv.get("interface", ""),
                    "comment": kv.get("comment", ""),
                }
            )
        elif section in _TUNNEL_SECTIONS and kv.get("remote-address"):
            tunnels.append(
                {
                    "type": _TUNNEL_SECTIONS[section],
                    "name": kv.get("name", ""),
                    "remote_address": kv["remote-address"],
                    "local_address": kv.get("local-address", ""),
                }
            )
        # Router-id : dans un reseau d'operateur, c'est le loopback. RouterOS
        # l'ecrit sous plusieurs formes selon la version et le protocole --
        # '/routing/id' en v7, 'router-id=' sur les instances OSPF et BGP.
        section_plate = section.replace("/", " ").strip().lower()
        if section_plate.startswith("routing"):
            if kv.get("router-id"):
                router_ids.append(kv["router-id"])
            elif section_plate.startswith("routing id") and kv.get("id"):
                router_ids.append(kv["id"])
        if kv.get("comment") and kv.get("name"):
            comments[kv["name"]] = kv["comment"]

    # Plusieurs instances peuvent porter le meme router-id : on ne garde que des
    # valeurs distinctes, dans l'ordre de lecture.
    uniques: list[str] = []
    for valeur in router_ids:
        propre = str(valeur).strip()
        if propre and propre not in uniques:
            uniques.append(propre)
    return {
        "addresses": addresses,
        "tunnels": tunnels,
        "comments": comments,
        "router_ids": uniques,
    }


def link_by_tunnels(
    snapshot: TopologySnapshot,
    ip_owner: dict[str, str],
    router_tunnels: list[tuple[str, str, list[dict[str, Any]]]],
) -> int:
    """Relie deux routeurs par un tunnel dont le ``remote-address`` appartient à
    l'autre.

    ``ip_owner`` : IP (sans préfixe) -> clé de nœud du routeur qui la porte.
    ``router_tunnels`` : ``(cle_source, nom_source, [tunnels parsés])``.
    On n'ajoute que les paires pas déjà reliées (jamais un doublon). Ces liens
    d'overlay sont ce que MNDP et les /30 physiques ne voient pas."""
    deja: set[frozenset[str]] = set()
    for lien in snapshot.links.values():
        deja.add(frozenset((lien.source_key, lien.target_key)))

    ajoutes = 0
    for cle_src, _nom_src, tunnels in router_tunnels:
        for tunnel in tunnels or []:
            distant = str(tunnel.get("remote_address") or "").strip()
            cible = ip_owner.get(distant)
            if not cible or cible == cle_src:
                continue
            if frozenset((cle_src, cible)) in deja:
                continue
            snapshot.add_link(
                TopologyLink(
                    source_key=cle_src,
                    target_key=cible,
                    kind=LINK_ETHERNET,
                    interface=str(tunnel.get("name") or "") or None,
                    discovered_by=_nom_src,
                    attributes={
                        "config_link": True,
                        "tunnel": str(tunnel.get("type") or "tunnel"),
                        "remote_address": distant,
                    },
                )
            )
            deja.add(frozenset((cle_src, cible)))
            ajoutes += 1
    return ajoutes


def _adresses_du_noeud(node: TopologyNode) -> list[str]:
    """Toutes les adresses connues d'un noeud, sans prefixe et sans doublon."""
    brutes = [node.address, *(node.attributes.get("addresses") or [])]
    vues: list[str] = []
    for valeur in brutes:
        ip = str(valeur or "").split("/")[0].strip()
        if ip and ip not in vues:
            vues.append(ip)
    return vues


def resolve_to_managed(
    snapshot: TopologySnapshot,
    ip_owner: dict[str, str],
    mac_owner: dict[str, str],
    name_owner: dict[str, str],
    loopback_owner: dict[str, str] | None = None,
) -> int:
    """Replie tout nœud DÉCOUVERT qui est en réalité un routeur GÉRÉ (ajouté par
    API) dans ce routeur — au lieu d'en créer un doublon à côté.

    Les routeurs, ce sont ceux que l'opérateur a connectés par API. Quand un PoP
    en voit un autre en voisin (``/ip/neighbor``), on ne crée pas une seconde case :
    on reconnaît le routeur géré et on fait pointer le lien vers SA case. Ce qui
    ne correspond à aucun routeur géré reste (ce sont les clients / équipements
    non gérés). Renvoie le nombre de nœuds repliés.

    L'ORDRE DES CRITÈRES N'EST PAS UNE COMMODITÉ
    --------------------------------------------
    1. **le loopback**, et il tranche seul. Unique par construction dans un
       réseau d'opérateur, il n'appartient qu'à un routeur et ne dépend d'aucune
       interface. Quand il correspond, il n'y a rien à interpréter.
    2. la MAC, qui dépend du port par lequel on regarde l'équipement.
    3. une adresse d'interface — le plus fragile des trois, parce qu'un ``/30``
       de liaison appartient AUX DEUX bouts : il dit qu'on partage un lien avec
       ce routeur, pas qu'on est ce routeur.
    4. l'identité RouterOS, un nom libre que rien n'empêche de dupliquer.

    Les trois derniers ne sont là que pour les équipements dont on ne connaît
    pas le loopback (voisins non gérés, routeur dont l'export est illisible).
    Dès qu'un loopback est connu des deux côtés, lui seul décide.
    """
    remap: dict[str, str] = {}
    for cle, node in list(snapshot.nodes.items()):
        if cle.startswith("router:"):
            continue  # deja un routeur gere
        cible: str | None = None
        # 1. Loopback : la seule preuve d'identite, elle passe avant tout.
        for adr in _adresses_du_noeud(node):
            if adr in (loopback_owner or {}):
                cible = (loopback_owner or {})[adr]
                break
        # 2. MAC.
        if cible is None:
            mac = normalize_mac(node.mac)
            if mac and mac in mac_owner:
                cible = mac_owner[mac]
        if cible is None:
            for ip in _adresses_du_noeud(node):
                if ip in ip_owner:
                    cible = ip_owner[ip]
                    break
        if cible is None:
            nom = str(node.name or "").strip().lower()
            if nom and nom not in GENERIC_NAMES and nom in name_owner:
                cible = name_owner[nom]
        if cible and cible != cle and cible in snapshot.nodes:
            remap[cle] = cible

    if not remap:
        return 0
    # Recable les liens vers la case gérée, jette les boucles internes, dedup par clé.
    nouveaux: dict[str, TopologyLink] = {}
    for lien in snapshot.links.values():
        lien.source_key = remap.get(lien.source_key, lien.source_key)
        lien.target_key = remap.get(lien.target_key, lien.target_key)
        if lien.source_key == lien.target_key:
            continue
        nouveaux[lien.key] = lien
    snapshot.links = nouveaux
    for cle in remap:
        snapshot.nodes.pop(cle, None)
    return len(remap)


def mark_reciprocal_links(snapshot: TopologySnapshot) -> int:
    """Marque le second exemplaire d'un cable vu par ses DEUX bouts.

    Deux routeurs geres relies par un cable se voient mutuellement en MNDP :
    ``pop-1`` annonce ``ether1 -> core-1`` et ``core-1`` annonce
    ``ether1 -> pop-1``. Comme la cle d'un lien vaut ``source|interface|cible``,
    ces deux observations produisent deux entrees distinctes pour UN SEUL cable.
    Le tableau des liens montrait donc chaque cable deux fois, et les compteurs
    annoncaient plus de liens que le reseau n'en porte.

    ON NE SUPPRIME PAS LE DOUBLON, ON LE MARQUE. Une cle de lien est referencee
    ailleurs -- surcharge de debit posee par l'exploitant (``shaping_policies``
    de portee ``link``), resserrage de la boucle QoE (``qoe_link_states``). La
    faire disparaitre effacerait silencieusement ces reglages. Le miroir garde
    donc sa cle et sa mesure ; il porte seulement ``mirror_of``, et l'interface
    s'en sert pour n'afficher qu'une ligne par cable. Le lien canonique, lui,
    apprend le nom du port d'en face (``peer_interface``) : l'information des
    deux bouts est conservee, pas perdue.

    PRUDENCE ASSUMEE : on ne marque que les paires ou l'on peut PROUVER la
    reciprocite, c'est-a-dire exactement deux liens entre les deux memes
    routeurs geres, un dans chaque sens. Deux cables paralleles entre les memes
    routeurs produisent quatre liens sans qu'on puisse dire lequel repond a
    lequel : on les laisse alors tous tels quels plutot que d'en effacer un vrai.
    """
    geres = {cle for cle, node in snapshot.nodes.items() if node.attributes.get("managed") is True}
    par_paire: dict[frozenset[str], list[TopologyLink]] = {}
    for lien in snapshot.links.values():
        if lien.discovered_by == "manual" or not lien.interface:
            continue
        if lien.source_key not in geres or lien.target_key not in geres:
            continue
        par_paire.setdefault(frozenset({lien.source_key, lien.target_key}), []).append(lien)

    marques = 0
    for liens in par_paire.values():
        if len(liens) != 2:
            continue
        premier, second = liens
        if premier.source_key != second.target_key:
            continue  # meme sens : ce ne sont pas deux vues du meme cable
        # Choix DETERMINISTE du canonique, pour que l'arbre et le tableau ne se
        # reorganisent pas d'une decouverte a l'autre.
        canonique, miroir = sorted(liens, key=lambda lien: (lien.discovered_by or "", lien.key))
        if canonique.attributes.get("mirror_of") or miroir.attributes.get("mirror_of"):
            continue
        miroir.attributes["mirror_of"] = canonique.key
        canonique.attributes["peer_interface"] = miroir.interface
        canonique.attributes["peer_router"] = miroir.discovered_by
        # La capacite honnete d'un cable est la PLUS BASSE des deux negociations :
        # avec un convertisseur de media ou un port bride, les deux bouts peuvent
        # annoncer des debits differents, et c'est le plus petit qui passe.
        capacites = [lien.capacity_mbps for lien in liens if lien.capacity_mbps]
        if capacites:
            canonique.capacity_mbps = min(capacites)
        marques += 1
    return marques


def link_by_shared_subnets(
    snapshot: TopologySnapshot,
    router_addresses: list[tuple[str, str, list[dict[str, Any]]]],
) -> int:
    """Déduit les liens routeur↔routeur de la CONFIG, par sous-réseau point-à-point.

    C'est la découverte la plus fiable entre PoP : deux routeurs gérés qui portent
    chacun une adresse sur le MÊME /30 ou /31 (v4) — /127, /126 (v6) — sont
    directement reliés. Leur ``/ip/address`` le prouve, là où ``/ip/neighbor``
    (MNDP/LLDP) peut manquer le lien : lien routé, tunnel, ou passage par un switch
    qui n'annonce rien.

    ``router_addresses`` : pour chaque PoP géré, ``(cle_noeud, nom_routeur,
    lignes /ip/address)``. On n'ajoute QUE les paires pas déjà reliées (jamais un
    doublon d'un lien MNDP), et seulement le point-à-point STRICT (exactement deux
    extrémités sur le sous-réseau), pour ne pas transformer un /29 partagé en
    maillage. ``discovered_by`` = nom du routeur source : le débit du port se
    rattache alors comme pour un lien MNDP. Renvoie le nombre de liens ajoutés.
    """
    deja: set[frozenset[str]] = set()
    for lien in snapshot.links.values():
        deja.add(frozenset((lien.source_key, lien.target_key)))

    par_reseau: dict[Any, dict[str, tuple[str, str | None]]] = {}
    for cle, nom, lignes in router_addresses:
        for ligne in lignes or []:
            brut = str(ligne.get("address") or "")
            interface = str(ligne.get("interface") or "") or None
            try:
                itf = ipaddress.ip_interface(brut)
            except ValueError:
                continue
            reseau = itf.network
            # Point-à-point STRICT seulement : un plus grand sous-réseau (un /24 de
            # LAN) relierait à tort tous ses hôtes entre eux.
            if reseau.version == 4 and reseau.prefixlen < 30:
                continue
            if reseau.version == 6 and reseau.prefixlen < 126:
                continue
            # Première interface vue par routeur sur ce réseau (le /30 n'en a qu'une).
            par_reseau.setdefault(reseau, {}).setdefault(cle, (nom, interface))

    ajoutes = 0
    for reseau, membres in par_reseau.items():
        if len(membres) != 2:
            continue  # exactement deux extrémités = vrai point-à-point
        (ka, (noma, ia)), (kb, (_nomb, _ib)) = sorted(membres.items())
        if ka == kb or frozenset((ka, kb)) in deja:
            continue
        snapshot.add_link(
            TopologyLink(
                source_key=ka,
                target_key=kb,
                kind=LINK_ETHERNET,
                interface=ia,
                discovered_by=noma,
                attributes={"config_link": True, "subnet": str(reseau)},
            )
        )
        deja.add(frozenset((ka, kb)))
        ajoutes += 1
    return ajoutes


def attach_uisp_devices(snapshot: TopologySnapshot, devices: list[dict[str, Any]]) -> int:
    """Enrichit le graphe avec ce que sait UISP.

    Le rattachement se fait par MAC : un voisin MikroTik de plateforme Ubiquiti
    et un device UISP sont le meme equipement physique.
    """
    rattaches = 0
    par_mac = {}
    for node in snapshot.nodes.values():
        if node.mac:
            par_mac[node.mac] = node

    for device in devices:
        identification = device.get("identification") or {}
        mac = normalize_mac(identification.get("mac"))
        device_id = identification.get("id")
        nom = identification.get("name") or identification.get("hostname") or "?"
        role = str(identification.get("role") or "").lower()

        kind = KIND_SECTOR if role in {"ap", "accesspoint"} else KIND_RADIO
        cle = f"mac:{mac}" if mac else f"uisp:{device_id}"

        existant = par_mac.get(mac) if mac else None
        if existant is not None:
            existant.uisp_device_id = str(device_id) if device_id else None
            existant.kind = kind
            existant.attributes["uisp_name"] = nom
            rattaches += 1
        else:
            snapshot.add_node(
                TopologyNode(
                    key=cle,
                    name=str(nom),
                    kind=kind,
                    mac=mac,
                    uisp_device_id=str(device_id) if device_id else None,
                    platform=str(identification.get("model") or "") or None,
                    attributes={"uisp_only": True},
                )
            )

        # Lien station -> AP, quand UISP le declare.
        parent = (device.get("attributes") or {}).get("apDevice") or {}
        parent_id = parent.get("id")
        if parent_id and device_id:
            snapshot.add_link(
                TopologyLink(
                    source_key=f"uisp:{parent_id}",
                    target_key=cle,
                    kind=LINK_RADIO,
                    discovered_by="uisp",
                    attributes={"ap_name": parent.get("name")},
                )
            )
    return rattaches


def static_client_node_key(reference: str) -> str:
    return f"static:{reference}"


def attach_static_clients(
    snapshot: TopologySnapshot,
    clients: Sequence[Any],
    *,
    pop_keys: dict[str, str] | None = None,
) -> int:
    """Pose les clients a IP fixe dans le graphe, sous leur secteur declare.

    POURQUOI ILS DOIVENT Y FIGURER. Le partage equitable d'un lien congestionne
    se calcule a partir de ce qui pend dessous. Un client statique absent du
    graphe est un client dont la consommation n'est comptee nulle part : le
    secteur parait moins charge qu'il ne l'est, et les abonnes PPPoE du meme
    secteur se font rogner a sa place. L'oubli n'est donc pas cosmetique.

    Le rattachement vient de la fiche, pas d'une observation : ces clients n'ont
    pas de caller-id a joindre a une station UISP. A defaut de secteur declare,
    le client est pose sous son PoP -- moins precis, mais jamais faux.
    """
    poses = 0
    for client in clients:
        reference = str(getattr(client, "reference", "") or "")
        if not reference:
            continue
        cle = static_client_node_key(reference)
        snapshot.add_node(
            TopologyNode(
                key=cle,
                name=str(getattr(client, "display_name", None) or reference),
                kind=KIND_STATIC,
                address=str(getattr(client, "address", "") or "") or None,
                attributes={
                    "declared": True,
                    "reference": reference,
                    "vlan": getattr(client, "vlan", None),
                    "pop_name": getattr(client, "pop_name", None),
                    "plan_down_mbps": getattr(client, "plan_down_mbps", None),
                    "plan_up_mbps": getattr(client, "plan_up_mbps", None),
                },
            )
        )
        poses += 1

        secteur = str(getattr(client, "sector_key", "") or "")
        parent = secteur or (pop_keys or {}).get(str(getattr(client, "pop_name", "") or ""), "")
        if not parent or parent not in snapshot.nodes:
            # Secteur declare mais inconnu du graphe : le noeud existe quand
            # meme (l'operateur doit VOIR son client), simplement detache.
            if secteur:
                snapshot.warnings.append(
                    f"Client statique '{reference}' rattache a un secteur inconnu "
                    f"'{secteur}' : verifiez la cle dans sa fiche."
                )
            continue
        snapshot.add_link(
            TopologyLink(
                source_key=parent,
                target_key=cle,
                kind=LINK_STATIC,
                discovered_by="inventory",
                attributes={"declared": True},
            )
        )
        if secteur:
            snapshot.subscriber_sectors[reference] = secteur
    return poses


def candidate_node_key(router_name: str, address: str) -> str:
    return f"candidate:{router_name}:{address}"


def attach_vlan_candidates(
    snapshot: TopologySnapshot,
    candidates: Sequence[dict[str, Any]],
    *,
    pop_keys: dict[str, str] | None = None,
    limit: int = 200,
) -> int:
    """Pose les adresses vues mais NON DECLAREES, sous leur PoP.

    Elles ne sont la que pour etre vues et traitees par un humain. Trois
    proprietes tiennent cette promesse :

    - leur nature est ``KIND_CANDIDATE``, jamais ``KIND_CPE`` ni ``KIND_POP`` :
      rien dans l'arbre ne les fait passer pour un abonne ou un equipement ;
    - elles portent ``declared: False``, que l'interface lit pour les traiter
      a part ;
    - elles ne sont posees qu'ICI, a la lecture du graphe, et ne sont jamais
      persistees dans ``topology_nodes``. Un candidat declare ou devenu muet
      disparait de lui-meme au chargement suivant.

    Le plafond n'est pas une precaution theorique : une VLAN de collecte un peu
    bavarde produirait des centaines d'entrees ARP, et un arbre illisible ne
    sert plus a decider.
    """
    poses = 0
    for candidat in candidates:
        if poses >= limit:
            break
        adresse = str(candidat.get("address") or "").strip()
        routeur = str(candidat.get("router_name") or "").strip()
        if not adresse or not routeur:
            continue
        cle = candidate_node_key(routeur, adresse)
        vlan = candidat.get("vlan_id")
        snapshot.add_node(
            TopologyNode(
                key=cle,
                name=adresse,
                kind=KIND_CANDIDATE,
                mac=normalize_mac(candidat.get("mac")),
                address=adresse,
                router_name=routeur,
                attributes={
                    "declared": False,
                    "detected": True,
                    "source": "arp",
                    "vlan_id": vlan,
                    "vlan_interface": candidat.get("vlan_interface"),
                    "pop_name": candidat.get("pop_name"),
                    "last_seen": candidat.get("last_seen"),
                    "first_seen": candidat.get("first_seen"),
                },
            )
        )
        poses += 1

        parent = (pop_keys or {}).get(str(candidat.get("pop_name") or ""), "")
        if not parent:
            parent = router_node_key(routeur)
        if parent in snapshot.nodes:
            snapshot.add_link(
                TopologyLink(
                    source_key=parent,
                    target_key=cle,
                    kind=LINK_DETECTED,
                    interface=str(candidat.get("vlan_interface") or "") or None,
                    discovered_by=routeur,
                    attributes={"detected": True, "declared": False},
                )
            )
    return poses


def orient_from_config(
    snapshot: TopologySnapshot,
    upstreams: dict[str, tuple[str | None, str]],
    address_owner: dict[str, str],
) -> tuple[int, list[str]]:
    """Pose le parent de chaque routeur d'apres SA TABLE DE ROUTAGE.

    C'est le passage de l'arbre devine a l'arbre reel. Jusqu'ici la hierarchie
    se calculait : une racine choisie au rang, des parents au plus court chemin.
    Ces heuristiques donnent souvent le bon resultat, mais elles ne SAVENT rien
    -- deux PoPs relies entre eux et au coeur peuvent se retrouver l'un sous
    l'autre sans que rien ne le contredise.

    La route par defaut, elle, dit exactement ou part le trafic que le routeur
    ne sait pas router. C'est la definition meme de "au-dessus".

    ``upstreams`` : nom de routeur -> (adresse de passerelle, raison).
    ``address_owner`` : adresse -> cle du noeud qui la porte.

    Renvoie ``(nombre d'orientations posees, avertissements)``.
    """
    poses = 0
    avertissements: list[str] = []
    for nom_routeur, (passerelle, raison) in sorted(upstreams.items()):
        cle = router_node_key(nom_routeur)
        noeud = snapshot.nodes.get(cle)
        if noeud is None:
            continue
        if passerelle is None:
            if raison == "ambigu":
                avertissements.append(
                    f"{nom_routeur} : plusieurs routes par defaut a egalite. Un arbre "
                    f"n'a qu'un parent et le controleur n'en inventera pas un ; "
                    f"son rattachement reste deduit du graphe."
                )
            continue
        parent = address_owner.get(passerelle)
        if parent is None:
            # Cas NORMAL pour la passerelle du reseau : son amont est le
            # transit, qui n'est pas dans l'inventaire. Rien a signaler.
            continue
        if parent == cle:
            avertissements.append(
                f"{nom_routeur} : sa route par defaut pointe vers lui-meme "
                f"({passerelle}). Rattachement ignore."
            )
            continue
        noeud.config_parent = parent
        noeud.attributes["config_parent_via"] = passerelle
        poses += 1
    return poses, avertissements


def link_by_routing_adjacency(
    snapshot: TopologySnapshot,
    peers: dict[str, list[str]],
    address_owner: dict[str, str],
) -> int:
    """Ajoute les liens PROUVES par une session de routage etablie.

    MNDP dit "je vois cet equipement" -- ce qui est vrai aussi de tout ce qui
    partage un switch. Une adjacence OSPF ou une session BGP etablie disent "je
    lui parle", et c'est ce qui fait un lien dans un reseau route.

    Les liens deja connus ne sont pas dupliques : ils sont seulement marques
    comme prouves, ce qui les rend surs au sens de l'arbre et donc preferes a
    un rattachement via un segment partage.
    """
    ajoutes = 0
    for nom_routeur, adresses in sorted(peers.items()):
        source = router_node_key(nom_routeur)
        if source not in snapshot.nodes:
            continue
        for adresse in adresses:
            cible = address_owner.get(adresse)
            if cible is None or cible == source:
                continue
            existant = _lien_entre(snapshot, source, cible)
            if existant is not None:
                existant.attributes["routing_adjacency"] = True
                continue
            snapshot.add_link(
                TopologyLink(
                    source_key=source,
                    target_key=cible,
                    kind=LINK_ROUTING,
                    discovered_by=nom_routeur,
                    attributes={"routing_adjacency": True, "peer_address": adresse},
                )
            )
            ajoutes += 1
    return ajoutes


def _lien_entre(snapshot: TopologySnapshot, a: str, b: str) -> TopologyLink | None:
    """Un lien deja connu entre ces deux noeuds, quel que soit son sens."""
    for lien in snapshot.links.values():
        if {lien.source_key, lien.target_key} == {a, b}:
            return lien
    return None


def map_subscribers_to_sectors(
    snapshot: TopologySnapshot,
    sessions: list[dict[str, Any]],
    uisp_stations: dict[str, str],
) -> int:
    """Rattache chaque abonne a son secteur radio.

    ``sessions`` porte le ``caller-id`` de RouterOS (MAC du CPE) ;
    ``uisp_stations`` associe une MAC de station a la cle de son AP.
    C'est cette jointure qui donne la vraie chaine de goulots d'un abonne.
    """
    rattaches = 0
    for session in sessions:
        login = session.get("login") or session.get("name")
        mac = normalize_mac(session.get("caller_id") or session.get("caller-id"))
        if not login or not mac:
            continue
        secteur = uisp_stations.get(mac)
        if secteur:
            snapshot.subscriber_sectors[str(login)] = secteur
            rattaches += 1
    if sessions and not rattaches:
        snapshot.warnings.append(
            "Aucun abonne rattache a un secteur radio : verifiez que les CPE "
            "sont visibles dans UISP (la jointure se fait sur la MAC du "
            "caller-id PPPoE)."
        )
    return rattaches

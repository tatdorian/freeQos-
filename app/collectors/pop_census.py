"""Recensement d'un PoP : localiser TOUS les clients, quel que soit leur acces.

LA QUESTION POSEE
-----------------
"Qui est client sur ce PoP ?" n'a pas de reponse dans RouterOS. Il n'existe
aucune table "clients", et la notion de "VLAN cliente" n'existe pas davantage.
Chaque population laisse une trace DIFFERENTE, et une seule lecture ne voit
jamais tout le monde :

  - l'abonne PPPoE s'annonce dans ``/ppp/active`` ;
  - le client a IP fixe ne s'annonce nulle part : il ne laisse qu'une entree
    ARP, qui s'efface apres quelques minutes de silence ;
  - le client en DHCP laisse un bail, qui lui survit a son silence ;
  - le client derriere un pont en filtrage VLAN ne laisse, cote L3, qu'une
    entree ARP rattachee au PONT -- le numero de VLAN n'y est pas ;
  - le client a qui on a route un /29 n'apparait JAMAIS en ARP pour son bloc :
    seule sa passerelle parle, et le bloc n'existe que dans ``/ip/route``.

Chercher une table parfaite est donc perdu d'avance. Ce module prend le
probleme dans l'autre sens.

LA METHODE, EN TROIS TEMPS
--------------------------
1. LE PERIMETRE. On part de ``/ip/address`` : les sous-reseaux que ce routeur
   dessert reellement. Chacun est classe, avec son motif -- lien point a point,
   transit de routage, interface a serveur PPPoE, ou **client**. On ne demande
   plus "cette interface s'appelle-t-elle vlanXXX ?" mais "cette adresse
   tombe-t-elle dans un sous-reseau client de ce PoP ?". C'est ce renversement
   qui rend le pont en filtrage VLAN inoffensif.

2. LA FUSION. Sept sources independantes sont croisees, chacune couvrant
   l'angle mort des autres : ARP, baux DHCP, sessions PPPoE, table de ponts,
   routes statiques, files deja posees, voisinage MNDP/LLDP. Un hote vu par UNE
   seule d'entre elles est localise. Les sources s'AJOUTENT : aucune ne peut
   faire disparaitre ce qu'une autre a vu.

3. L'AVEU. Un recensement qui se tait sur ses trous n'est pas fiable, il est
   seulement silencieux. Le resultat porte donc ce qu'il ne sait pas : source
   illisible, sous-reseau client sans aucune presence, adresse vue hors de tout
   sous-reseau connu, presence L2 sans adresse IP. C'est la troisieme partie
   qui rend la methode sure -- pas la promesse de ne rien rater, mais la
   garantie que rien ne se perd EN SILENCE.

CE QUE CE MODULE NE FAIT TOUJOURS PAS
-------------------------------------
Il ne cree aucune fiche, ne devine aucun plan, ne pose aucune file. Une
presence reste une presence : une imprimante, une camera ou l'equipement d'un
autre operateur produisent le meme signal qu'un client. Le recensement PROPOSE,
l'humain declare. Ce module ne fait aucune entree-sortie : il recoit des tables
deja lues et rend des structures, ce qui le rend testable sans routeur.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.collectors.config_graph import (
    InterfacePath,
    default_gateways,
    interface_stacks,
    routing_peers,
)
from app.collectors.parsing import parse_flag
from app.collectors.vlan_clients import (
    GARDE,
    REJET_ADRESSE,
    REJET_DESACTIVEE,
    REJET_INVALIDE,
    REJET_SANS_MAC,
    disabled_vlans,
    judge_arp_rows,
    normalise_mac,
    pppoe_interfaces,
    vlan_index,
)
from app.models import VlanSighting

logger = logging.getLogger(__name__)

# Verdicts ARP qui ne decrivent AUCUNE presence : l'entree est desactivee,
# invalide, illisible, ou c'est une adresse qu'on a cherchee sans obtenir de
# reponse. Tous les autres rejets decrivent bien une machine qui a parle -- ils
# disent seulement qu'elle n'est pas un client. Le recensement les garde donc,
# et les qualifie ; les faire disparaitre ici priverait l'exploitant du seul
# indice qui explique un PoP qui parait vide.
_ARP_SANS_PRESENCE = frozenset({REJET_DESACTIVEE, REJET_INVALIDE, REJET_SANS_MAC, REJET_ADRESSE})

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# -----------------------------------------------------------------------------
# Role d'un sous-reseau. C'est la premiere decision, et la plus lourde de
# consequences : elle dit ou un client PEUT se trouver. Chaque role porte un
# motif rendu tel quel a l'exploitant, parce qu'un perimetre qu'on ne peut pas
# verifier ne vaut pas mieux qu'un perimetre devine.
# -----------------------------------------------------------------------------
ROLE_CLIENT = "client"
ROLE_POINT_A_POINT = "point-a-point"
ROLE_TRANSIT = "transit"
ROLE_PPPOE = "pppoe"
ROLE_DESACTIVE = "desactive"

MOTIF_CLIENT = "sous-reseau desservi, aucun signe d'infrastructure"
MOTIF_POINT_A_POINT = "prefixe etroit (/30 ou plus) : lien entre deux equipements"
MOTIF_TRANSIT = "porte une adjacence de routage ou la passerelle par defaut"
MOTIF_PPPOE = "interface a serveur PPPoE : ses abonnes ont deja une identite"
MOTIF_DESACTIVE = "adresse desactivee dans la configuration"

# -----------------------------------------------------------------------------
# Les sept sources. Le nom est rendu a l'interface : savoir PAR QUOI un client
# est vu dit a l'exploitant ce qui se passerait s'il se taisait.
# -----------------------------------------------------------------------------
SOURCE_ARP = "arp"
SOURCE_BAIL = "bail-dhcp"
SOURCE_PPPOE = "session-pppoe"
SOURCE_PONT = "table-de-ponts"
SOURCE_ROUTE = "route-statique"
SOURCE_FILE = "file-existante"
SOURCE_VOISIN = "voisinage"

# Nature d'un hote recense. Un seul de ces mots decide si l'hote est propose a
# la declaration : NATURE_CLIENT.
NATURE_CLIENT = "client-possible"
NATURE_PPPOE = "abonne-pppoe"
NATURE_EQUIPEMENT = "equipement"
NATURE_HORS_PERIMETRE = "hors-perimetre"

# Comment le numero de VLAN a ete obtenu. Une localisation sans sa provenance
# ne se verifie pas : "vlan 120 d'apres la table de ponts" se recoupe sur le
# routeur, "vlan 120" tout court ne se recoupe pas.
VLAN_PAR_INTERFACE = "interface /interface/vlan"
VLAN_PAR_PONT = "table de ponts (/interface/bridge/host)"
VLAN_PAR_PVID = "pvid du port de pont"
VLAN_INCONNU = "inconnu"


# Tables dont l'ABSENCE est normale, et ne degrade rien : elles dependent des
# paquets installes et de la version de RouterOS. Un routeur purement L3 n'a pas
# de serveur PPPoE, un routeur sans protocole de routage n'a ni OSPF ni BGP.
# Toute autre lecture qui echoue est une degradation, et doit se voir.
SOURCES_FACULTATIVES = ("pppoe_servers", "ospf_neighbors", "bgp_sessions")


class PartialCensusError(RuntimeError):
    """Le recensement a abouti, mais une source lui a manque.

    L'exception PORTE ce qui a quand meme ete vu. Jeter ces observations parce
    qu'une table sur seize a expire punirait l'exploitant deux fois : il
    perdrait la moitie de sa liste en plus de son erreur. L'appelant enregistre
    donc ``sightings`` ET signale ``str(exc)``.
    """

    def __init__(self, message: str, sightings: Sequence[VlanSighting] = ()) -> None:
        super().__init__(message)
        self.sightings = list(sightings)


def _texte(valeur: Any) -> str:
    return str(valeur or "").strip()


def _entier(valeur: Any) -> int | None:
    brut = _texte(valeur)
    if not brut:
        return None
    try:
        return int(brut)
    except ValueError:
        return None


def _hote(valeur: Any) -> str | None:
    """Adresse d'hote exploitable, ou None.

    RouterOS ecrit selon les tables ``10.0.0.5``, ``10.0.0.5/29`` ou
    ``10.0.0.5%ether1``. Les trois designent le meme hote.
    """
    brut = _texte(valeur).split("%")[0].split("/")[0]
    if not brut:
        return None
    try:
        adresse = ipaddress.ip_address(brut)
    except ValueError:
        return None
    if adresse.is_unspecified or adresse.is_loopback or adresse.is_multicast:
        return None
    return str(adresse)


def _inclus(cible: IpNetwork, reseaux: Sequence[IpNetwork]) -> bool:
    """Vrai si ``cible`` tient entierement dans l'un des reseaux.

    Le controle de famille est fait ici, une fois : comparer une v4 a une v6
    leve dans ``ipaddress``, et ce serait une panne pour une question qui a une
    reponse evidente -- non.
    """
    for reseau in reseaux:
        if isinstance(cible, ipaddress.IPv4Network) and isinstance(reseau, ipaddress.IPv4Network):
            if cible.subnet_of(reseau):
                return True
        elif isinstance(cible, ipaddress.IPv6Network) and isinstance(reseau, ipaddress.IPv6Network):
            if cible.subnet_of(reseau):
                return True
    return False


def _dedans(adresse: str, reseaux: Sequence[IpNetwork]) -> bool:
    """Vrai si cette adresse d'hote tombe dans l'un des reseaux."""
    hote = ipaddress.ip_address(adresse)
    return any(hote in reseau for reseau in reseaux if hote.version == reseau.version)


def _reseau(valeur: Any) -> IpNetwork | None:
    brut = _texte(valeur).split("%")[0]
    if not brut:
        return None
    try:
        return ipaddress.ip_network(brut, strict=False)
    except ValueError:
        return None


# =========================================================================
# 1. LE PERIMETRE : ou un client peut se trouver
# =========================================================================


@dataclass(slots=True)
class Subnet:
    """Un sous-reseau porte par ce routeur, et ce qu'on en fait.

    ``role`` decide ; ``reason`` permet de CONTESTER la decision. Les deux sont
    rendus a l'interface : un perimetre qu'on ne peut pas relire est un
    perimetre qu'on subit.
    """

    network: str
    interface: str
    address: str
    role: str
    reason: str
    vlan_id: int | None = None
    ports: list[str] = field(default_factory=list)


def classify_subnets(
    *,
    addresses: Sequence[dict[str, Any]] = (),
    vlans: Sequence[dict[str, Any]] = (),
    pppoe_servers: Sequence[dict[str, Any]] = (),
    routes: Sequence[dict[str, Any]] = (),
    ospf_neighbors: Sequence[dict[str, Any]] = (),
    bgp_sessions: Sequence[dict[str, Any]] = (),
    stacks: dict[str, InterfacePath] | None = None,
) -> list[Subnet]:
    """Classe chaque adresse du routeur : infrastructure, ou desserte client.

    QUATRE EXCLUSIONS, ET RIEN D'AUTRE. Tout ce qui n'est pas exclu est declare
    client, volontairement : rater un sous-reseau client rend un client
    invisible, tandis qu'un sous-reseau d'infrastructure pris a tort pour de la
    desserte ne produit que des candidats qu'un humain ecartera d'un coup d'oeil.
    Entre les deux erreurs, la seconde se repare, la premiere se subit.

      - ``/30``, ``/31``, ``/32`` (``/126``+ en v6) : un lien entre deux
        equipements, ou un loopback. Aucun client n'y tient ;
      - le sous-reseau contient un pair OSPF/BGP etabli ou la passerelle par
        defaut : c'est du transit ;
      - l'interface heberge un serveur PPPoE : ses clients ont deja une
        identite dans ``/ppp/active``, les proposer serait les dedoubler ;
      - l'adresse est desactivee : elle ne dessert rien.
    """
    vlan_ids = vlan_index(vlans)
    exclues = pppoe_interfaces(pppoe_servers)
    eteintes = disabled_vlans(vlans)
    pairs = [
        *routing_peers(ospf_neighbors=ospf_neighbors, bgp_sessions=bgp_sessions),
        *(passerelle.gateway for passerelle in default_gateways(routes)),
    ]
    stacks = stacks or {}

    resultat: list[Subnet] = []
    for row in addresses:
        reseau = _reseau(row.get("address"))
        if reseau is None:
            continue
        interface = _texte(row.get("interface")) or _texte(row.get("actual-interface"))
        adresse = _hote(row.get("address")) or str(reseau.network_address)
        etroit = reseau.prefixlen >= (30 if reseau.version == 4 else 126)
        transit = any(_dedans(pair, [reseau]) for pair in pairs)

        if parse_flag(row.get("disabled")) or interface in eteintes:
            role, motif = ROLE_DESACTIVE, MOTIF_DESACTIVE
        elif etroit:
            role, motif = ROLE_POINT_A_POINT, MOTIF_POINT_A_POINT
        elif interface in exclues:
            role, motif = ROLE_PPPOE, MOTIF_PPPOE
        elif transit:
            role, motif = ROLE_TRANSIT, MOTIF_TRANSIT
        else:
            role, motif = ROLE_CLIENT, MOTIF_CLIENT

        chemin = stacks.get(interface)
        resultat.append(
            Subnet(
                network=str(reseau),
                interface=interface,
                address=adresse,
                role=role,
                reason=motif,
                vlan_id=vlan_ids.get(interface) if interface in vlan_ids else None,
                ports=list(chemin.ports) if chemin else [],
            )
        )
    resultat.sort(key=lambda s: (s.role != ROLE_CLIENT, s.network))
    return resultat


def client_networks(subnets: Sequence[Subnet]) -> list[str]:
    """Les seuls sous-reseaux ou un client est cherche."""
    return [s.network for s in subnets if s.role == ROLE_CLIENT]


# =========================================================================
# 2. LA FUSION : qui vit dans ce perimetre
# =========================================================================


@dataclass(slots=True)
class CensusHost:
    """Un hote localise sur le PoP, et tout ce qui permet de le retrouver.

    ``sources`` est la partie la plus utile a l'exploitation : un hote vu par
    la seule table ARP disparaitra s'il se tait quelques minutes, un hote qui
    porte aussi un bail DHCP restera. Ce n'est pas un detail d'implementation,
    c'est ce qui dit a l'exploitant quelle confiance accorder a une absence.
    """

    address: str
    mac: str | None = None
    interface: str = ""
    vlan_id: int | None = None
    vlan_source: str = VLAN_INCONNU
    ports: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    subnet: str | None = None
    nature: str = NATURE_CLIENT
    reason: str = MOTIF_CLIENT
    hostname: str | None = None
    identity: str | None = None
    login: str | None = None
    comment: str | None = None
    routed_prefixes: list[str] = field(default_factory=list)
    # Pourquoi cet hote est cote client alors qu'aucun sous-reseau ne le couvre.
    # Le perimetre d'adressage est la regle ; ceci en est le filet, pour les cas
    # ou /ip/address est illisible, ou ou la VLAN est adressee ailleurs.
    perimeter: str = ""


@dataclass(slots=True)
class ClientBlock:
    """Un BLOC route derriere un hote, ou deja vise par une file.

    Le cas que l'ARP ne peut structurellement pas voir : un ``/29`` vendu a une
    entreprise ne parle pas, seule sa passerelle parle. Sans cette lecture, le
    client apparaitrait comme une adresse unique et son bloc serait shape a
    cote.
    """

    prefix: str
    source: str
    via: str | None = None
    interface: str | None = None
    vlan_id: int | None = None


@dataclass(slots=True)
class L2Presence:
    """Une MAC vue sur un pont, sans adresse IP connue.

    Ce n'est pas un client : c'est un TROU, et il est rendu tel quel. Une MAC
    presente en L2 dont aucune source L3 ne donne l'adresse signifie que le
    client ne parle pas IP en ce moment, ou qu'il parle a travers un routeur.
    """

    mac: str
    bridge: str | None = None
    port: str | None = None
    vlan_id: int | None = None


@dataclass(slots=True)
class RouterTables:
    """Les tables RouterOS necessaires au recensement, deja lues.

    ``unreadable`` porte les lectures qui ont ECHOUE, avec leur motif. Une
    source muette doit se voir : un recensement ampute qui se presente comme
    complet est pire qu'une erreur franche.
    """

    addresses: Sequence[dict[str, Any]] = ()
    arp: Sequence[dict[str, Any]] = ()
    vlans: Sequence[dict[str, Any]] = ()
    pppoe_servers: Sequence[dict[str, Any]] = ()
    ppp_active: Sequence[dict[str, Any]] = ()
    dhcp_leases: Sequence[dict[str, Any]] = ()
    dhcp_servers: Sequence[dict[str, Any]] = ()
    bridge_hosts: Sequence[dict[str, Any]] = ()
    bridge_ports: Sequence[dict[str, Any]] = ()
    interfaces: Sequence[dict[str, Any]] = ()
    bondings: Sequence[dict[str, Any]] = ()
    routes: Sequence[dict[str, Any]] = ()
    queues: Sequence[dict[str, Any]] = ()
    neighbors: Sequence[dict[str, Any]] = ()
    ospf_neighbors: Sequence[dict[str, Any]] = ()
    bgp_sessions: Sequence[dict[str, Any]] = ()
    unreadable: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PopCensus:
    """Le recensement d'un routeur : le perimetre, les hotes, et les trous."""

    router_name: str
    pop_name: str
    subnets: list[Subnet] = field(default_factory=list)
    hosts: list[CensusHost] = field(default_factory=list)
    blocks: list[ClientBlock] = field(default_factory=list)
    l2_only: list[L2Presence] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    remarks: list[str] = field(default_factory=list)

    @property
    def clients(self) -> list[CensusHost]:
        """Les hotes qui peuvent etre des clients. Aucun n'est un client : ils
        le DEVIENNENT quand un humain saisit une fiche et un debit souscrit."""
        return [h for h in self.hosts if h.nature == NATURE_CLIENT]

    def as_dict(self) -> dict[str, Any]:
        return {
            "router": self.router_name,
            "pop_name": self.pop_name,
            "sources": self.sources,
            "remarks": self.remarks,
            "subnets": [
                {
                    "network": s.network,
                    "interface": s.interface,
                    "address": s.address,
                    "role": s.role,
                    "reason": s.reason,
                    "vlan_id": s.vlan_id,
                    "ports": s.ports,
                    "hosts": sum(1 for h in self.hosts if h.subnet == s.network),
                }
                for s in self.subnets
            ],
            "hosts": [
                {
                    "address": h.address,
                    "mac": h.mac,
                    "interface": h.interface,
                    "vlan_id": h.vlan_id,
                    "vlan_source": h.vlan_source,
                    "ports": h.ports,
                    "sources": h.sources,
                    "subnet": h.subnet,
                    "nature": h.nature,
                    "reason": h.reason,
                    "hostname": h.hostname,
                    "identity": h.identity,
                    "login": h.login,
                    "comment": h.comment,
                    "routed_prefixes": h.routed_prefixes,
                }
                for h in self.hosts
            ],
            "blocks": [
                {
                    "prefix": b.prefix,
                    "source": b.source,
                    "via": b.via,
                    "interface": b.interface,
                    "vlan_id": b.vlan_id,
                }
                for b in self.blocks
            ],
            "l2_only": [
                {"mac": p.mac, "bridge": p.bridge, "port": p.port, "vlan_id": p.vlan_id}
                for p in self.l2_only
            ],
            "counts": {
                "subnets_client": sum(1 for s in self.subnets if s.role == ROLE_CLIENT),
                "hosts": len(self.hosts),
                "clients": len(self.clients),
                "pppoe": sum(1 for h in self.hosts if h.nature == NATURE_PPPOE),
                "equipements": sum(1 for h in self.hosts if h.nature == NATURE_EQUIPEMENT),
                "hors_perimetre": sum(1 for h in self.hosts if h.nature == NATURE_HORS_PERIMETRE),
                "blocks": len(self.blocks),
                "l2_only": len(self.l2_only),
            },
        }


class _Accumulateur:
    """Fusionne les sources sur la cle qui les reunit toutes : l'adresse IP.

    Une classe plutot qu'un dictionnaire de dictionnaires parce que chaque
    source apporte des champs differents et qu'aucune ne doit ECRASER ce qu'une
    autre a trouve : une MAC connue ne redevient jamais inconnue.
    """

    def __init__(self) -> None:
        self.hotes: dict[str, CensusHost] = {}

    def voir(
        self,
        adresse: str,
        source: str,
        *,
        mac: str | None = None,
        interface: str | None = None,
        hostname: str | None = None,
        identity: str | None = None,
        login: str | None = None,
        comment: str | None = None,
        perimeter: str | None = None,
    ) -> CensusHost:
        hote = self.hotes.get(adresse)
        if hote is None:
            hote = CensusHost(address=adresse)
            self.hotes[adresse] = hote
        if source not in hote.sources:
            hote.sources.append(source)
        hote.mac = hote.mac or mac
        hote.interface = hote.interface or (interface or "")
        hote.hostname = hote.hostname or hostname
        hote.identity = hote.identity or identity
        hote.login = hote.login or login
        hote.comment = hote.comment or comment
        hote.perimeter = hote.perimeter or (perimeter or "")
        return hote


def build_census(
    tables: RouterTables,
    *,
    router_name: str,
    pop_name: str,
    known_equipment: Collection[str] = (),
) -> PopCensus:
    """Recense un routeur : perimetre, hotes, blocs, et ce qui manque.

    ``known_equipment`` recoit les adresses des equipements DECLARES ailleurs
    (inventaire des routeurs, antennes). Sans elle, le routeur voisin et
    l'antenne du secteur seraient proposes en clients a chaque cycle -- et une
    liste de candidats dont la moitie est du materiel reseau finit par ne plus
    etre lue du tout.
    """
    stacks = interface_stacks(
        interfaces=tables.interfaces,
        vlans=tables.vlans,
        bridge_ports=tables.bridge_ports,
        bondings=tables.bondings,
    )
    subnets = classify_subnets(
        addresses=tables.addresses,
        vlans=tables.vlans,
        pppoe_servers=tables.pppoe_servers,
        routes=tables.routes,
        ospf_neighbors=tables.ospf_neighbors,
        bgp_sessions=tables.bgp_sessions,
        stacks=stacks,
    )
    vlan_ids = vlan_index(tables.vlans)
    pvid_par_interface = _pvid_index(tables.bridge_ports)
    ponts_par_mac, l2_only_rows = _bridge_index(tables.bridge_hosts)

    acc = _Accumulateur()
    _voir_arp(acc, tables, subnets)
    _voir_baux(acc, tables)
    _voir_pppoe(acc, tables)
    _voir_voisins(acc, tables)
    blocks = _voir_routes(acc, tables, subnets)
    blocks += _voir_files(acc, tables, subnets)

    # Adresses du routeur lui-meme : elles ne sont jamais des clients, et les
    # laisser passer ferait proposer le routeur a sa propre declaration.
    siennes = {s.address for s in subnets}
    materiel = {a for a in (_hote(x) for x in known_equipment) if a} | set(
        routing_peers(ospf_neighbors=tables.ospf_neighbors, bgp_sessions=tables.bgp_sessions)
    )

    hosts: list[CensusHost] = []
    for adresse in sorted(acc.hotes, key=lambda a: ipaddress.ip_address(a)):
        hote = acc.hotes[adresse]
        sous_reseau = _subnet_de(adresse, subnets)
        hote.subnet = sous_reseau.network if sous_reseau else None
        if not hote.interface and sous_reseau is not None:
            hote.interface = sous_reseau.interface
        _localiser(
            hote,
            vlan_ids=vlan_ids,
            pvid=pvid_par_interface,
            ponts=ponts_par_mac,
            stacks=stacks,
            sous_reseau=sous_reseau,
        )
        _qualifier(hote, sous_reseau=sous_reseau, siennes=siennes, materiel=materiel)
        hosts.append(hote)

    # Une MAC vue en L2 dont aucune source L3 ne donne l'adresse reste un trou.
    connues = {h.mac for h in hosts if h.mac}
    l2_only = [p for p in l2_only_rows if p.mac not in connues]

    census = PopCensus(
        router_name=router_name,
        pop_name=pop_name,
        subnets=subnets,
        hosts=hosts,
        blocks=blocks,
        l2_only=sorted(l2_only, key=lambda p: p.mac),
        sources=_etat_des_sources(tables),
    )
    census.remarks = _remarques(census, tables)
    return census


# ------------------------------------------------------------------
# Les sept lectures. Chacune ne sait qu'une chose, et le dit.
# ------------------------------------------------------------------


def _voir_arp(acc: _Accumulateur, tables: RouterTables, subnets: Sequence[Subnet]) -> None:
    """``/ip/arp`` : qui a parle recemment, et sur quelle interface.

    Le verdict vient de ``vlan_clients.judge_arp_rows`` -- le meme chemin de
    decision que la detection historique, elargi au perimetre d'adressage. Deux
    logiques separees auraient fini par diverger.

    NUANCE QUI COMPTE : le recensement ne jette pas ce que le filtre ecarte. Une
    adresse vue sur du transit, ou hors de tout sous-reseau connu, est une
    machine REELLE ; elle est recensee puis qualifiee "hors perimetre", avec son
    motif. C'est souvent la seule chose qui explique un PoP qui parait vide.
    """
    for verdict in judge_arp_rows(
        tables.arp,
        tables.vlans,
        tables.pppoe_servers,
        client_networks=client_networks(subnets),
    ):
        if verdict.reason in _ARP_SANS_PRESENCE:
            continue
        acc.voir(
            verdict.address,
            SOURCE_ARP,
            mac=verdict.mac,
            interface=verdict.interface or None,
            # Retenu sur le NOM de l'interface : une VLAN declaree qui n'heberge
            # pas de PPPoE. C'est la regle historique, et elle reste vraie meme
            # quand /ip/address ne dit rien -- une VLAN peut etre adressee sur un
            # autre routeur, ou la table peut etre illisible.
            perimeter=(
                f"interface {verdict.interface} : VLAN declaree sans serveur PPPoE"
                if verdict.reason == GARDE
                else None
            ),
        )


def _voir_baux(acc: _Accumulateur, tables: RouterTables) -> None:
    """``/ip/dhcp-server/lease`` : la memoire que l'ARP n'a pas.

    Un bail survit au silence du client pendant toute sa duree, la ou une
    entree ARP s'efface en quelques minutes. C'est aussi la seule source qui
    porte souvent un NOM -- ``host-name`` ou le commentaire pose par
    l'exploitant.
    """
    # Un bail nomme son SERVEUR ("dhcp-clients"), pas son interface. Prendre ce
    # nom pour une interface ferait chercher un VLAN qui n'existe pas : c'est a
    # cela que sert /ip/dhcp-server, qui fait la correspondance.
    interfaces = {
        _texte(row.get("name")): _texte(row.get("interface"))
        for row in tables.dhcp_servers
        if _texte(row.get("name")) and _texte(row.get("interface"))
    }
    for row in tables.dhcp_leases:
        if parse_flag(row.get("disabled")):
            continue
        adresse = _hote(row.get("active-address")) or _hote(row.get("address"))
        if adresse is None:
            continue
        serveur = _texte(row.get("server"))
        acc.voir(
            adresse,
            SOURCE_BAIL,
            mac=normalise_mac(row.get("active-mac-address") or row.get("mac-address")),
            # A defaut de correspondance, l'interface reste vide : le
            # sous-reseau de rattachement la donnera, et c'est plus sur que de
            # poser un nom qui ne designe aucune interface.
            interface=interfaces.get(serveur) or None,
            hostname=_texte(row.get("host-name")) or None,
            comment=_texte(row.get("comment")) or None,
            # Un serveur DHCP sert des clients : le bail suffit a placer l'hote
            # cote desserte, meme si le perimetre d'adressage est muet.
            perimeter=f"bail DHCP du serveur {serveur}" if serveur else "bail DHCP",
        )


def _voir_pppoe(acc: _Accumulateur, tables: RouterTables) -> None:
    """``/ppp/active`` : les seuls clients qui portent deja un nom.

    Ils sont recenses pour etre ECARTES de la proposition : un abonne PPPoE a
    deja une identite et un plan, le proposer a la declaration serait le
    dedoubler. Les compter reste utile -- c'est la population du PoP.
    """
    for row in tables.ppp_active:
        adresse = _hote(row.get("address"))
        if adresse is None:
            continue
        acc.voir(
            adresse,
            SOURCE_PPPOE,
            mac=normalise_mac(row.get("caller-id")),
            login=_texte(row.get("name")) or None,
        )


def _voir_voisins(acc: _Accumulateur, tables: RouterTables) -> None:
    """``/ip/neighbor`` : ce que l'equipement d'en face DIT de lui-meme.

    N'ajoute presque jamais un hote que les autres sources n'ont pas ; en
    revanche il donne son identite et son modele, ce qui suffit le plus souvent
    a reconnaitre une antenne ou un CPE d'un coup d'oeil.
    """
    for row in tables.neighbors:
        adresse = _hote(row.get("address")) or _hote(row.get("address4"))
        if adresse is None:
            continue
        modele = _texte(row.get("board")) or _texte(row.get("platform"))
        identite = _texte(row.get("identity")) or None
        acc.voir(
            adresse,
            SOURCE_VOISIN,
            mac=normalise_mac(row.get("mac-address")),
            interface=_texte(row.get("interface")) or None,
            identity=f"{identite} ({modele})" if identite and modele else identite,
        )


def _voir_routes(
    acc: _Accumulateur, tables: RouterTables, subnets: Sequence[Subnet]
) -> list[ClientBlock]:
    """``/ip/route`` : les blocs qui ne parlent jamais.

    Un ``/29`` route derriere un CPE n'apparait dans AUCUNE table de presence :
    seule la passerelle parle. C'est la seule source qui dise que ce bloc
    existe, et donc la seule facon de ne pas shaper une adresse a la place d'un
    client entier.
    """
    reseaux = [n for n in (_reseau(s.network) for s in subnets if s.role == ROLE_CLIENT) if n]
    blocs: list[ClientBlock] = []
    for row in tables.routes:
        if parse_flag(row.get("disabled")) or parse_flag(row.get("dynamic")):
            continue
        destination = _reseau(row.get("dst-address"))
        passerelle = _hote(row.get("gateway")) or _hote(row.get("immediate-gw"))
        if destination is None or passerelle is None:
            continue
        if destination.prefixlen == 0:
            continue  # route par defaut : du transit, pas un client
        if not _dedans(passerelle, reseaux):
            continue
        hote = acc.voir(passerelle, SOURCE_ROUTE)
        prefixe = str(destination)
        if prefixe not in hote.routed_prefixes:
            hote.routed_prefixes.append(prefixe)
        blocs.append(ClientBlock(prefix=prefixe, source=SOURCE_ROUTE, via=passerelle))
    return blocs


def _voir_files(
    acc: _Accumulateur, tables: RouterTables, subnets: Sequence[Subnet]
) -> list[ClientBlock]:
    """``/queue/simple`` : ce que l'exploitant a DEJA declare, sur le routeur.

    Une file existante vise une adresse parce qu'un humain a decide qu'il y
    avait la un client. C'est la source la plus qualifiee de toutes -- et la
    seule qui survive a un client totalement muet.
    """
    reseaux = [n for n in (_reseau(s.network) for s in subnets if s.role == ROLE_CLIENT) if n]
    blocs: list[ClientBlock] = []
    for row in tables.queues:
        if parse_flag(row.get("disabled")):
            continue
        commentaire = _texte(row.get("comment")) or None
        for morceau in _texte(row.get("target")).split(","):
            cible = _reseau(morceau)
            if cible is None:
                continue
            if not _inclus(cible, reseaux):
                continue
            if cible.num_addresses == 1:
                acc.voir(str(cible.network_address), SOURCE_FILE, comment=commentaire)
            else:
                blocs.append(ClientBlock(prefix=str(cible), source=SOURCE_FILE))
    return blocs


# ------------------------------------------------------------------
# Localisation : dans quelle VLAN, sur quel port
# ------------------------------------------------------------------


def _pvid_index(bridge_ports: Sequence[dict[str, Any]]) -> dict[str, int]:
    """``interface -> pvid``. Le VLAN d'un port d'acces non tague."""
    index: dict[str, int] = {}
    for row in bridge_ports:
        if parse_flag(row.get("disabled")):
            continue
        nom = _texte(row.get("interface"))
        pvid = _entier(row.get("pvid"))
        if nom and pvid is not None:
            index[nom] = pvid
    return index


def _bridge_index(
    bridge_hosts: Sequence[dict[str, Any]],
) -> tuple[dict[str, L2Presence], list[L2Presence]]:
    """``MAC -> (port, vlan)`` d'apres la table de ponts.

    C'EST LA REPONSE AU PONT EN FILTRAGE VLAN. Quand l'adressage client est
    porte par un pont, ``/ip/arp`` ne nomme que ce pont et le numero de VLAN est
    perdu ; ``/interface/bridge/host`` le porte, avec le port physique en prime.
    La jointure se fait par la MAC, qui est la seule cle commune aux deux tables.
    """
    par_mac: dict[str, L2Presence] = {}
    toutes: list[L2Presence] = []
    for row in bridge_hosts:
        # Les entrees 'local' sont les MAC du pont lui-meme, pas des hotes.
        if parse_flag(row.get("local")):
            continue
        mac = normalise_mac(row.get("mac-address"))
        if mac is None:
            continue
        presence = L2Presence(
            mac=mac,
            bridge=_texte(row.get("bridge")) or None,
            port=_texte(row.get("on-interface")) or _texte(row.get("interface")) or None,
            vlan_id=_entier(row.get("vid")) or _entier(row.get("vlan-id")),
        )
        par_mac.setdefault(mac, presence)
        toutes.append(presence)
    return par_mac, toutes


def _localiser(
    hote: CensusHost,
    *,
    vlan_ids: dict[str, int | None],
    pvid: dict[str, int],
    ponts: dict[str, L2Presence],
    stacks: dict[str, InterfacePath],
    sous_reseau: Subnet | None,
) -> None:
    """Pose le VLAN et les ports physiques, du plus sur au moins sur.

    L'ordre n'est pas esthetique : une interface ``/interface/vlan`` PORTE son
    numero, la table de ponts l'OBSERVE, un pvid le SUPPOSE. Rendre la
    provenance permet a l'exploitant de savoir ce qu'il peut recouper.
    """
    if hote.interface in vlan_ids and vlan_ids[hote.interface] is not None:
        hote.vlan_id = vlan_ids[hote.interface]
        hote.vlan_source = VLAN_PAR_INTERFACE
    elif hote.mac and hote.mac in ponts and ponts[hote.mac].vlan_id is not None:
        presence = ponts[hote.mac]
        hote.vlan_id = presence.vlan_id
        hote.vlan_source = VLAN_PAR_PONT
        if presence.port:
            hote.ports = [presence.port]
    elif hote.interface in pvid:
        hote.vlan_id = pvid[hote.interface]
        hote.vlan_source = VLAN_PAR_PVID
    elif sous_reseau is not None and sous_reseau.vlan_id is not None:
        hote.vlan_id = sous_reseau.vlan_id
        hote.vlan_source = VLAN_PAR_INTERFACE

    if not hote.ports:
        if hote.mac and hote.mac in ponts and ponts[hote.mac].port:
            port = ponts[hote.mac].port
            hote.ports = [port] if port else []
        else:
            chemin = stacks.get(hote.interface)
            hote.ports = list(chemin.ports) if chemin else []


def _subnet_de(adresse: str, subnets: Sequence[Subnet]) -> Subnet | None:
    """Le sous-reseau qui contient cette adresse, le plus precis d'abord.

    Le plus precis, parce qu'un ``/24`` de desserte peut cohabiter avec un
    ``/30`` de transit : c'est le prefixe le plus long qui decrit reellement ou
    se trouve l'hote.
    """
    hote = ipaddress.ip_address(adresse)
    candidats: list[tuple[int, Subnet]] = []
    for sous_reseau in subnets:
        reseau = _reseau(sous_reseau.network)
        if reseau is None or reseau.version != hote.version:
            continue
        if hote in reseau:
            candidats.append((reseau.prefixlen, sous_reseau))
    if not candidats:
        return None
    candidats.sort(key=lambda paire: -paire[0])
    return candidats[0][1]


def _qualifier(
    hote: CensusHost,
    *,
    sous_reseau: Subnet | None,
    siennes: Collection[str],
    materiel: Collection[str],
) -> None:
    """Decide ce qu'est cet hote. Un seul verdict ouvre la declaration.

    L'ordre compte : l'adresse du routeur lui-meme passe avant tout le reste,
    une session PPPoE avant le perimetre (elle a deja une identite), et
    l'inventaire avant la desserte (une antenne posee dans une VLAN cliente
    reste une antenne).
    """
    if hote.address in siennes:
        hote.nature = NATURE_EQUIPEMENT
        hote.reason = "address carried by the router itself"
    elif hote.login:
        hote.nature = NATURE_PPPOE
        hote.reason = "PPPoE session open: identity and plan already known"
    elif hote.address in materiel:
        hote.nature = NATURE_EQUIPEMENT
        hote.reason = "declared in the inventory, or a routing peer"
    elif sous_reseau is None and hote.perimeter:
        hote.nature = NATURE_CLIENT
        hote.reason = hote.perimeter
    elif sous_reseau is None:
        hote.nature = NATURE_HORS_PERIMETRE
        hote.reason = "no subnet of this router covers this address"
    elif sous_reseau.role != ROLE_CLIENT:
        hote.nature = NATURE_HORS_PERIMETRE
        hote.reason = f"{sous_reseau.network} : {sous_reseau.reason}"
    else:
        hote.nature = NATURE_CLIENT
        hote.reason = f"{sous_reseau.network} : {sous_reseau.reason}"


# ------------------------------------------------------------------
# 3. L'AVEU : ce que le recensement ne sait pas
# ------------------------------------------------------------------


_LIBELLES = {
    "addresses": "/ip/address (le perimetre)",
    "arp": "/ip/arp",
    "dhcp_leases": "/ip/dhcp-server/lease",
    "ppp_active": "/ppp/active",
    "bridge_hosts": "/interface/bridge/host",
    "routes": "/ip/route",
    "queues": "/queue/simple",
    "neighbors": "/ip/neighbor",
    "vlans": "/interface/vlan",
    "bridge_ports": "/interface/bridge/port",
    "pppoe_servers": "/interface/pppoe-server/server",
}


def _etat_des_sources(tables: RouterTables) -> dict[str, str]:
    """Pour chaque table : lue (et combien de lignes), ou illisible (pourquoi)."""
    etat: dict[str, str] = {}
    for champ, libelle in _LIBELLES.items():
        motif = tables.unreadable.get(champ)
        if motif:
            etat[libelle] = f"illisible : {motif}"
        else:
            lignes = getattr(tables, champ, ())
            etat[libelle] = f"lue ({len(lignes)} ligne(s))"
    return etat


def _remarques(census: PopCensus, tables: RouterTables) -> list[str]:
    """Ce qui pourrait cacher un client, dit en clair.

    C'est la partie qui rend la methode sure. Elle ne promet pas de ne rien
    rater : elle garantit qu'un trou ne se referme pas en silence.
    """
    notes: list[str] = []

    for champ, motif in sorted(tables.unreadable.items()):
        notes.append(
            f"{_LIBELLES.get(champ, champ)} illisible ({motif}) : "
            f"les clients que cette source etait seule a voir manquent."
        )

    if not tables.addresses and "addresses" not in tables.unreadable:
        notes.append(
            "Aucune adresse lue dans /ip/address : le perimetre est vide, et la "
            "detection retombe sur le seul nom des interfaces (/interface/vlan). "
            "Un client sur un pont en filtrage VLAN reste invisible dans cet etat."
        )

    muets = [
        s.network
        for s in census.subnets
        if s.role == ROLE_CLIENT and not any(h.subnet == s.network for h in census.hosts)
    ]
    if muets:
        notes.append(
            "Sous-reseau(x) client sans aucune presence observee : "
            + ", ".join(muets)
            + ". Soit personne n'y parle, soit la desserte passe par un equipement "
            "qui masque ses clients (routeur intermediaire, NAT)."
        )

    dehors = [h for h in census.hosts if h.subnet is None]
    if dehors:
        interfaces = sorted({h.interface for h in dehors if h.interface})
        notes.append(
            f"{len(dehors)} adresse(s) vue(s) hors de tout sous-reseau de ce routeur"
            + (f" (interfaces : {', '.join(interfaces)})" if interfaces else "")
            + ". C'est le cas quand le PoP commute sans router : l'adressage est "
            "porte par un autre routeur, qui doit etre declare pour les recenser."
        )

    if census.l2_only:
        notes.append(
            f"{len(census.l2_only)} MAC vue(s) sur un pont sans adresse IP connue. "
            "Ces machines sont physiquement la mais aucune source L3 ne les nomme : "
            "client muet, ou trafic qui traverse un routeur intermediaire."
        )

    arp_seul = [h for h in census.hosts if h.nature == NATURE_CLIENT and h.sources == [SOURCE_ARP]]
    if arp_seul:
        notes.append(
            f"{len(arp_seul)} client(s) possible(s) connu(s) par la SEULE table ARP. "
            "Elle s'efface apres quelques minutes de silence : leur absence a un "
            "prochain cycle ne prouvera rien."
        )

    return notes


# ------------------------------------------------------------------
# Sortie vers la detection existante
# ------------------------------------------------------------------


def missing_sources(tables: RouterTables) -> dict[str, str]:
    """Les lectures ratees dont l'absence n'est PAS normale."""
    return {
        champ: motif
        for champ, motif in tables.unreadable.items()
        if champ not in SOURCES_FACULTATIVES
    }


def sightings_from_census(census: PopCensus) -> list[VlanSighting]:
    """Ce qui est enregistre comme presence, et rien d'autre.

    Seuls les hotes de nature ``client-possible`` sortent : ni le routeur, ni
    les antennes, ni les abonnes PPPoE qui ont deja une identite. Le contrat de
    ``vlan_sightings`` ne change pas d'un iota -- une observation reste une
    observation, et aucune fiche n'en sort sans qu'un humain la saisisse.
    """
    return [
        VlanSighting(
            router_name=census.router_name,
            pop_name=census.pop_name,
            address=hote.address,
            vlan_interface=hote.interface,
            mac=hote.mac,
            vlan_id=hote.vlan_id,
        )
        for hote in census.clients
    ]

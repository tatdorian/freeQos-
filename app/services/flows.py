"""Rattachement des flux aux abonnes, et agregation avant ecriture.

CE MODULE EST PUR. Pas de socket, pas de base, pas d'horloge implicite : on lui
donne des flux decodes, il rend des compteurs. C'est ce qui permet de tester le
comptage -- la partie ou une erreur se paie en factures fausses -- sans monter
un exporteur.

QUATRE DECISIONS STRUCTURANTES
------------------------------

1. LE SENS SE DEDUIT DE L'ABONNE, PAS DE L'INTERFACE. Un flux dont la
   DESTINATION tombe dans le bloc d'un client est du descendant pour lui ; dont
   la SOURCE y tombe, du montant. Lire le sens sur le numero d'interface
   obligerait a connaitre le cablage de chaque exporteur, et se tromperait
   silencieusement au premier recablage.

2. LE POINT DE MESURE FAIT PARTIE DE LA CLE. Le meme octet traverse le PoP puis
   la sortie internet, et les deux l'exportent. Additionner reviendrait a
   doubler la consommation de tout le monde. On range donc les compteurs par
   point de mesure, et la lecture en choisit un.

3. UNE ADRESSE INCONNUE N'EST PAS UN CLIENT. Ce qui parle sans correspondre a
   aucune fiche va dans une liste d'HOTES VUS, qui sert a la saisie et a rien
   d'autre. Une imprimante, une camera, un equipement d'un autre operateur
   laissent exactement la meme trace qu'un abonne : rien dans un flux ne dit
   quel debit a ete vendu, et c'est la seule chose qui compte pour brider.

4. L'AUTRE BOUT EST RETENU, LUI AUSSI. Pour chaque flux rattache a un abonne,
   l'adresse DISTANTE est notee : c'est elle qui, une fois nommee (cf.
   ``services/ipfinder``), repond a "qui regarde Netflix sur ce secteur". Seules
   les adresses reellement sur internet sont retenues -- deux abonnes qui se
   parlent ne sont une destination ni pour l'un ni pour l'autre.
"""

from __future__ import annotations

import ipaddress
import itertools
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from app.collectors.netflow import Flow
from app.services import ipfinder

logger = logging.getLogger(__name__)

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Espace d'adressage ou VIVENT les clients, par defaut. Sert a decider si une
#: adresse non rattachee merite d'etre proposee a la saisie : une adresse
#: publique quelconque sur internet n'est pas un candidat, elle est l'autre bout
#: de la conversation.
RESEAUX_CLIENTS_PAR_DEFAUT: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # CGNAT
    "fd00::/8",
)

#: Reseaux d'EXPLOITATION par defaut : aucun. Le controleur les apprend tout
#: seul (son adresse, celle des routeurs declares, celle des exporteurs) ; ce
#: reglage sert a en ajouter que l'inventaire ne connait pas -- un lien de
#: transit, un reseau de supervision.
RESEAUX_INFRASTRUCTURE_PAR_DEFAUT: tuple[str, ...] = ()

#: Familles d'usage, volontairement grossieres. L'inspection fine de protocole
#: n'a pas sa place dans un controleur de debit : la seule question a laquelle
#: ce tableau doit repondre est "de quoi est fait le trafic qui sature ce
#: secteur", et le numero de port y suffit.
#:
#: Le chiffrement generalise fait que la video en flux (Netflix, YouTube) se
#: presente en 443 comme le reste du web. On ne pretend donc pas les separer --
#: la famille s'appelle "web" et le dit.
PORTS: dict[int, str] = {
    20: "partage de fichiers",
    21: "partage de fichiers",
    22: "administration",
    23: "administration",
    25: "messagerie",
    53: "dns",
    80: "web",
    110: "messagerie",
    123: "horloge",
    143: "messagerie",
    139: "partage de fichiers",
    443: "web",
    445: "partage de fichiers",
    465: "messagerie",
    500: "vpn",
    587: "messagerie",
    993: "messagerie",
    995: "messagerie",
    1194: "vpn",
    1723: "vpn",
    2049: "partage de fichiers",
    3074: "jeux",
    3389: "administration",
    3478: "voix / visio",
    4500: "vpn",
    5004: "voix / visio",
    5060: "voix / visio",
    5061: "voix / visio",
    5900: "administration",
    6969: "p2p",
    8080: "web",
    8443: "web",
    27015: "jeux",
    51820: "vpn",
}

AUTRE = "autre"

#: Ports du PLAN DE GESTION. Un flux qui les porte n'est pas une conversation
#: de client : c'est le reseau qui s'administre lui-meme.
#:
#: SANS CE FILTRE, la liste "qui parle a qui" se remplit de ce que le
#: controleur fait lui-meme -- il interroge les routeurs en 8728, recoit leurs
#: flux en 2055 -- et de ce que les routeurs se disent entre eux (BFD, BGP).
#: Ce trafic est reel, il est meme le plus regulier du reseau, et il noie
#: exactement ce qu'on venait chercher : le ping d'un client vers un site.
#:
#: SSH et telnet n'y sont PAS. Un client a parfaitement le droit de s'en
#: servir, et les ecarter masquerait son trafic.
#: RESOLUTION DE NOMS (DNS, DNS sur TLS, mDNS). Demander a 8.8.8.8 l'adresse de
#: syit.fr n'est pas "aller sur 8.8.8.8" : la vraie destination est l'adresse
#: obtenue, que le client joint juste apres. Afficher le resolveur noyait la
#: liste des conversations d'une ligne par client. Les volumes, eux, restent
#: comptes.
PORTS_RESOLUTION: frozenset[int] = frozenset({53, 853, 5353})

PORTS_INFRASTRUCTURE: frozenset[int] = frozenset(
    {
        161,  # SNMP
        162,  # SNMP trap
        179,  # BGP
        514,  # syslog
        646,  # LDP
        1645,  # RADIUS (historique)
        1646,
        1812,  # RADIUS
        1813,
        2055,  # NetFlow
        3784,  # BFD
        3785,
        4739,  # IPFIX
        4784,  # BFD multihop
        8291,  # Winbox
        8728,  # API RouterOS
        8729,  # API RouterOS (TLS)
        9995,  # NetFlow (autres collecteurs)
        9996,
    }
)


def classify(flow: Flow) -> str:
    """Nomme la famille d'usage d'un flux.

    On regarde le plus PETIT des deux ports en priorite : le port de service est
    presque toujours celui-la, le port ephemere du client etant tire au-dessus
    de 32768. Prendre le port source au hasard classerait la moitie du web en
    "autre".
    """
    ports = sorted({flow.src_port, flow.dst_port} - {0})
    for port in ports:
        famille = PORTS.get(port)
        if famille is not None:
            return famille
    if any(6881 <= port <= 6999 for port in ports):
        return "p2p"
    if flow.protocol == 1:
        return "diagnostic"  # ICMP
    if flow.protocol in (50, 51):
        return "vpn"  # ESP / AH
    return AUTRE


def service_port(flow: Flow) -> int:
    """Le port qui designe le SERVICE, pas le port ephemere du client.

    Meme regle que ``classify`` -- le plus petit des deux -- et pour la meme
    raison : le client tire son port au-dessus de 32768, le serveur ecoute en
    dessous. Afficher le port ephemere ne dirait rien a personne.
    """
    ports = sorted({flow.src_port, flow.dst_port} - {0})
    return ports[0] if ports else 0


def _retenir(table: dict[Any, Any], cle: Any, valeur: Any, limite: int) -> None:
    """Range ``cle`` en DERNIER (la plus recemment vue), et evince les plus
    anciennes au-dela de ``limite`` -- un dixieme d'un coup, pour ne pas payer
    l'eviction a chaque insertion."""
    table.pop(cle, None)
    table[cle] = valeur
    if len(table) > limite:
        for vieille in list(itertools.islice(table, len(table) - limite + limite // 10)):
            del table[vieille]


@dataclass
class PrefixIndex:
    """Rattache une adresse au bloc declare qui la contient, le plus precis.

    POURQUOI PAS UNE SIMPLE BOUCLE. Un exporteur de sortie internet envoie
    facilement des dizaines de milliers de flux par minute. Parcourir la liste
    des blocs pour chacun d'eux ferait du rattachement le goulot d'etranglement
    du collecteur, et un collecteur en retard jette des datagrammes -- donc des
    octets qui manqueront a des factures.

    On range donc les blocs par longueur de prefixe, et on interroge par
    masquage : une recherche coute autant que le nombre de longueurs DISTINCTES
    declarees (une poignee), pas le nombre de clients.
    """

    v4: dict[int, dict[int, int]] = field(default_factory=dict)
    v6: dict[int, dict[int, int]] = field(default_factory=dict)
    _v4_lengths: tuple[int, ...] = ()
    _v6_lengths: tuple[int, ...] = ()

    @classmethod
    def build(cls, entries: Iterable[tuple[str, int]]) -> PrefixIndex:
        index = cls()
        for prefixe, subscriber_id in entries:
            try:
                reseau = ipaddress.ip_network(str(prefixe), strict=False)
            except ValueError:
                logger.debug("Prefixe ignore dans l'index : %r", prefixe)
                continue
            table = index.v4 if reseau.version == 4 else index.v6
            table.setdefault(reseau.prefixlen, {})[int(reseau.network_address)] = subscriber_id
        index._v4_lengths = tuple(sorted(index.v4, reverse=True))
        index._v6_lengths = tuple(sorted(index.v6, reverse=True))
        return index

    def __len__(self) -> int:
        return sum(len(t) for t in self.v4.values()) + sum(len(t) for t in self.v6.values())

    def lookup(self, address: str) -> int | None:
        try:
            adresse = ipaddress.ip_address(address)
        except ValueError:
            return None
        if adresse.version == 4:
            table, longueurs, bits = self.v4, self._v4_lengths, 32
        else:
            table, longueurs, bits = self.v6, self._v6_lengths, 128
        if not longueurs:
            return None
        valeur = int(adresse)
        for longueur in longueurs:
            masque = (1 << bits) - (1 << (bits - longueur))
            trouve = table[longueur].get(valeur & masque)
            if trouve is not None:
                return trouve
        return None


@dataclass
class SubscriberCounters:
    subscriber_id: int
    vantage: str
    down_bytes: int = 0
    up_bytes: int = 0
    down_packets: int = 0
    up_packets: int = 0
    flows: int = 0

    @property
    def total_bytes(self) -> int:
        return self.down_bytes + self.up_bytes


@dataclass
class AppCounters:
    subscriber_id: int
    app: str
    down_bytes: int = 0
    up_bytes: int = 0


@dataclass
class HostCounters:
    address: str
    vlan_id: int | None
    exporter: str | None = None
    pop_name: str | None = None
    down_bytes: int = 0
    up_bytes: int = 0


@dataclass
class DestinationCounters:
    """Ce qu'une machine du reseau a atteint sur internet, et combien.

    LA CLE EST L'ADRESSE DU CLIENT, PAS SON IDENTIFIANT D'ABONNE. C'est la
    correction d'un angle mort qui se voyait tout de suite a l'usage : un ping
    lance depuis un poste de supervision, un routeur, une camera -- n'importe
    quoi qui n'est pas une fiche d'abonne declaree -- ne laissait AUCUNE trace,
    alors que le flux traversait bien le reseau. L'observation est "cette
    adresse a joint celle-la" ; le rattachement a un abonne est une
    interpretation, qui peut manquer (machine non declaree) ou changer (une
    session PPPoE qui se reconnecte sur une autre adresse).

    ``port`` et ``protocol`` sont ceux du DERNIER flux vu vers cette adresse.
    Ils expliquent la ligne (443, 53, 3478...) ; ils ne la decoupent pas -- un
    meme serveur atteint sur deux ports reste une seule destination.
    """

    client: str
    address: str
    subscriber_id: int | None = None
    port: int = 0
    protocol: int = 0
    app: str = AUTRE
    down_bytes: int = 0
    up_bytes: int = 0
    flows: int = 0
    #: Secondes pendant lesquelles la conversation a reellement echange dans la
    #: fenetre (duree portee par les enregistrements). Diviser un volume par la
    #: PERIODE entiere donnait un debit dilue ; par ce temps actif, on retrouve
    #: le debit tel que le client l'a vecu.
    active_s: float = 0.0

    @property
    def total_bytes(self) -> int:
        return self.down_bytes + self.up_bytes


@dataclass
class FlushBatch:
    ts: datetime
    subscribers: list[SubscriberCounters]
    apps: list[AppCounters]
    hosts: list[HostCounters]
    destinations: list[DestinationCounters] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.subscribers or self.apps or self.hosts or self.destinations)


@dataclass
class FlowAggregator:
    """Accumule une fenetre de flux, puis la rend d'un bloc.

    On n'ecrit pas un flux a la fois. Un export de sortie internet porte des
    milliers de conversations par seconde ; une ligne par flux remplirait la
    base sans rien apprendre de plus. La question a laquelle le controleur doit
    repondre est "combien cet abonne a-t-il consomme, et de quoi" -- elle se
    repond avec une ligne par abonne et par fenetre.
    """

    index: PrefixIndex = field(default_factory=PrefixIndex)
    customer_networks: tuple[IpNetwork, ...] = ()
    #: Plafond de lignes d'hotes retenues par fenetre. Une VLAN bavarde ne doit
    #: pas noyer l'aide a la saisie sous des milliers d'adresses de passage.
    host_limit: int = 500
    track_hosts: bool = True
    #: Retenir l'adresse DISTANTE atteinte par chaque abonne. C'est ce qui
    #: alimente "qui se connecte a quoi" et, de la, les restrictions.
    track_destinations: bool = True
    #: Adresses du reseau d'exploitation : le collecteur, les routeurs declares,
    #: tout ce qui n'est pas un client. Renseigne par le service a chaque
    #: fenetre, depuis l'inventaire et les exporteurs declares.
    infrastructure_networks: tuple[IpNetwork, ...] = ()
    #: Ports du plan de gestion, et la resolution de noms. Vide = ne rien ecarter.
    infrastructure_ports: frozenset[int] = PORTS_INFRASTRUCTURE | PORTS_RESOLUTION
    #: Plafond de destinations retenues par fenetre. Un seul abonne qui fait du
    #: p2p peut toucher des milliers d'adresses en une minute : sans plafond,
    #: une fenetre de collecte deviendrait une fenetre d'ecriture en base.
    destination_limit: int = 2_000
    #: Expiration active posee sur les routeurs (1 min) : un enregistrement porte
    #: au plus ce temps de trafic, et un flux vivant en reemet un a ce rythme.
    active_timeout_s: float = 60.0
    clock: Any = time.monotonic

    #: Par (abonne, point de vue, EXPORTEUR) : un client vu par trois NAS
    #: etait compte trois fois. On garde chaque exporteur a part, et la
    #: fenetre ne retient que celui qui le voit le mieux (cf. flush).
    _subs: dict[tuple[int, str, str], SubscriberCounters] = field(default_factory=dict)
    _apps: dict[tuple[int, str, str, str], AppCounters] = field(default_factory=dict)
    _exporteur_courant: str = ""
    _hosts: dict[tuple[str, int | None], HostCounters] = field(default_factory=dict)
    _dests: dict[tuple[str, str], DestinationCounters] = field(default_factory=dict)
    #: Debit EN COURS : par conversation, le dernier enregistrement de chaque flux
    #: (5-uplet) -> (arrivee, descendant, bit/s). Survit aux fenetres.
    _live: dict[tuple[str, str], dict[tuple[Any, ...], tuple[float, bool, float]]] = field(
        default_factory=dict
    )
    #: Point de mesure retenu pour chaque conversation : le meme paquet est
    #: exporte par le PoP PUIS par la sortie internet, il ne compte qu'une fois.
    #: (point de vue, exporteur) retenu pour chaque conversation : UNE seule
    #: source par conversation, sinon deux exporteurs du meme point de vue la
    #: comptaient deux fois (volume au double, debit gonfle).
    _pair_vantage: dict[tuple[str, str, bool], tuple[str, str]] = field(default_factory=dict)
    #: Ce qui decrit une conversation en cours (abonne, port, protocole, usage),
    #: garde tant qu'elle est vivante : la fenetre, elle, se vide a chaque flush.
    _live_meta: dict[tuple[str, str], tuple[int | None, int, int, str]] = field(
        default_factory=dict
    )
    _vantage_courant: str = ""
    #: APPARIEMENT NAT. Une sortie internet qui masque ses clients voit le
    #: retour arriver sur SON adresse publique : rien dans ce flux ne nomme le
    #: client. Le montant, lui, le nomme. On retient donc, pour chaque
    #: conversation montante, (adresse distante, port distant, protocole, port
    #: client) -> (abonne, adresse du client, vu a) ; le flux descendant qui
    #: revient de ce meme bout, vers ce meme port, est le sien.
    _nat_exact: dict[tuple[str, int, int, int], tuple[int, str, float]] = field(
        default_factory=dict
    )
    #: Meme chose sans le port client, quand le routeur l'a traduit et ne
    #: l'exporte pas. N'est retenu que si UN SEUL abonne parle a ce bout :
    #: deux clients sur le meme serveur, et l'on ne devine pas.
    _nat_large: dict[tuple[str, int, int], dict[int, tuple[str, float]]] = field(
        default_factory=dict
    )
    #: Adresses PUBLIQUES de NAT reconnues (-> vu a) : celles qu'un flux
    #: montant annonce comme adresse traduite, ou qu'un retour apparie
    #: exactement a vise. L'appariement sans port client n'est tente que vers
    #: elles : une machine publique non declaree qui parle au meme serveur ne
    #: doit pas voir son trafic credite a un abonne.
    _nat_publiques: dict[str, float] = field(default_factory=dict)
    #: Flux descendants arrives AVANT leur montant (meme fenetre) : rejoues au
    #: flush, une fois tous les montants de la fenetre connus.
    _nat_attente: list[tuple[Flow, int, int, str, str]] = field(default_factory=list)
    #: Plafonds : une sortie internet porte des centaines de milliers de
    #: conversations ; la table ne doit pas devenir la memoire du collecteur.
    #: Au-dela, les correspondances les plus anciennement vues partent les
    #: premieres : une table pleine qui n'apprendrait plus rien serait pire.
    nat_table_limit: int = 100_000
    nat_pending_limit: int = 50_000
    #: Duree de vie d'une correspondance sans nouveau montant. Un flux vivant
    #: reemet son montant a chaque expiration active (1 min, posee par
    #: freeQoS) ; avec le defaut RouterOS (30 min), montant et retour arrivent
    #: ensemble, dans la meme fenetre.
    nat_ttl_s: float = 600.0
    flows_seen: int = 0
    flows_matched: int = 0
    #: Flux descendants rattaches par l'adresse traduite (champ NAT exporte).
    nat_translated: int = 0
    #: Flux descendants rattaches par appariement avec leur montant.
    nat_matched: int = 0
    #: Flux qui ressemblaient a un retour NAT, sans montant pour les nommer.
    nat_unmatched: int = 0
    #: Octets RATTACHES par point de vue et par sens, depuis le demarrage.
    #: C'est ce qui dit, sans lire la base, qu'un point ne voit qu'un sens.
    direction_bytes: dict[str, list[int]] = field(default_factory=dict)
    #: Destinations ecartees faute de place dans la fenetre. Un compteur qui
    #: monte dit que destination_limit est trop bas -- sinon on croirait que ces
    #: abonnes n'atteignent rien.
    destinations_dropped: int = 0
    #: Flux ecartes de "qui parle a qui" parce qu'ils relevent de
    #: l'exploitation. Compte, et non tu : un chiffre enorme ici veut dire que
    #: le filtre est trop large, et il faut pouvoir s'en apercevoir.
    destinations_infra: int = 0

    @staticmethod
    def parse_networks(values: Sequence[str]) -> tuple[IpNetwork, ...]:
        reseaux: list[IpNetwork] = []
        for valeur in values:
            texte = str(valeur).strip()
            if not texte:
                continue
            try:
                reseaux.append(ipaddress.ip_network(texte, strict=False))
            except ValueError:
                logger.warning("Reseau client ignore (invalide) : %s", texte)
        return tuple(reseaux)

    def set_index(self, index: PrefixIndex) -> None:
        self.index = index

    def add(
        self,
        flow: Flow,
        *,
        vantage: str,
        sampling_rate: int = 1,
        exporter: str | None = None,
        pop_name: str | None = None,
    ) -> None:
        octets = flow.octets * max(sampling_rate, 1)
        paquets = flow.packets * max(sampling_rate, 1)
        self.flows_seen += 1
        self._vantage_courant = vantage
        self._exporteur_courant = exporter or ""

        # L'adresse telle qu'exportee d'abord, l'adresse TRADUITE a defaut :
        # derriere un NAT, le retour vise l'adresse publique et seul le champ
        # NAT nomme le client.
        source, client_src = self._rattacher(flow.src, flow.post_src)
        destination, client_dst = self._rattacher(flow.dst, flow.post_dst)
        if destination is not None and client_dst != flow.dst:
            self.nat_translated += 1

        if destination is not None:
            self._credit(destination, vantage, down_bytes=octets, down_packets=paquets)
            self._credit_app(destination, classify(flow), vantage, down_bytes=octets)
        if source is not None:
            self._credit(source, vantage, up_bytes=octets, up_packets=paquets)
            self._credit_app(source, classify(flow), vantage, up_bytes=octets)
            if destination is None:
                self._apprendre_nat(flow, source, client_src)

        rattache = source is not None or destination is not None
        if rattache:
            self.flows_matched += 1
        elif self._retour_nat_possible(flow):
            if not self._rejouer(flow, octets, paquets):
                if len(self._nat_attente) < self.nat_pending_limit:
                    self._nat_attente.append((flow, octets, paquets, vantage, exporter or ""))
                else:
                    self.nat_unmatched += 1
            return

        if self.track_destinations:
            # LE SENS EST CELUI DU CLIENT. Un flux qui ARRIVE chez lui vient de
            # l'adresse distante (descendant) ; un flux qui en PART y va
            # (montant). Le meme flux peut faire les deux quand deux machines du
            # reseau se parlent -- et dans ce cas l'autre bout n'est pas une
            # destination internet, il est ecarte par is_routable.
            #
            # UNE MACHINE NON DECLAREE COMPTE AUSSI. Elle n'a pas d'abonne, mais
            # elle a une adresse, et c'est tout ce qu'il faut pour dire ce
            # qu'elle joint. Exiger une fiche d'abonne rendait invisible tout ce
            # qui n'en a pas : un poste de supervision, un routeur, une camera --
            # et le ping qu'on vient de lancer pour verifier que ca marche.
            if destination is not None or (not rattache and self._is_customer(flow.dst)):
                self._note_destination(
                    client_dst, flow.src, flow, octets, descendant=True, subscriber_id=destination
                )
            if source is not None or (not rattache and self._is_customer(flow.src)):
                self._note_destination(
                    client_src, flow.dst, flow, octets, descendant=False, subscriber_id=source
                )

        if not rattache and self.track_hosts:
            self._note_host(flow, octets, exporter=exporter, pop_name=pop_name)

    def _rattacher(self, adresse: str, traduite: str | None) -> tuple[int | None, str]:
        """L'abonne d'une adresse, et l'adresse qui l'a nomme."""
        trouve = self.index.lookup(adresse)
        if trouve is None and traduite and traduite != adresse:
            trouve = self.index.lookup(traduite)
            if trouve is not None:
                return trouve, traduite
        return trouve, adresse

    # ---------------------------------------------------------------- NAT
    def _apprendre_nat(self, flow: Flow, subscriber_id: int, client: str) -> None:
        """Un montant vers internet : retenir de quoi reconnaitre son retour."""
        if self._is_customer(flow.dst) or not ipfinder.is_routable(flow.dst):
            return
        maintenant = self.clock()
        if flow.post_src and flow.post_src != flow.src:
            self._noter_publique(flow.post_src, maintenant)
        valeur = (subscriber_id, client, maintenant)
        for port_client in {flow.src_port, flow.post_src_port} - {0}:
            _retenir(
                self._nat_exact,
                (flow.dst, flow.dst_port, flow.protocol, port_client),
                valeur,
                self.nat_table_limit,
            )
        large = (flow.dst, flow.dst_port, flow.protocol)
        abonnes = self._nat_large.pop(large, {})
        abonnes[subscriber_id] = (client, maintenant)
        _retenir(self._nat_large, large, abonnes, self.nat_table_limit)

    def _retour_nat_possible(self, flow: Flow) -> bool:
        """Un flux d'internet vers une adresse qui n'est pas un client.

        C'est la forme exacte d'un retour vers l'adresse publique d'un NAT. Un
        flux entre deux machines du reseau, ou vers un client non declare, n'en
        est pas un : il garde son chemin habituel (hotes vus).
        """
        return (
            ipfinder.is_routable(flow.src)
            and not self._is_customer(flow.src)
            and not self._is_customer(flow.dst)
        )

    def _noter_publique(self, adresse: str, vu: float) -> None:
        if adresse in self._nat_publiques or len(self._nat_publiques) < 4_096:
            self._nat_publiques[adresse] = vu

    def _apparier(self, flow: Flow) -> tuple[int, str] | None:
        exact = self._nat_exact.get((flow.src, flow.src_port, flow.protocol, flow.dst_port))
        if exact is not None:
            self._noter_publique(flow.dst, self.clock())
            return exact[0], exact[1]
        if flow.dst not in self._nat_publiques:
            return None
        abonnes = self._nat_large.get((flow.src, flow.src_port, flow.protocol))
        if abonnes is not None and len(abonnes) == 1:
            ((sid, (client, _vu)),) = abonnes.items()
            return sid, client
        return None

    def _rejouer(self, flow: Flow, octets: int, paquets: int) -> bool:
        """Rattache un retour NAT a son abonne, s'il est reconnu. Vrai si oui."""
        trouve = self._apparier(flow)
        if trouve is None:
            return False
        sid, client = trouve
        vantage = self._vantage_courant
        self._credit(sid, vantage, down_bytes=octets, down_packets=paquets)
        self._credit_app(sid, classify(flow), vantage, down_bytes=octets)
        self.flows_matched += 1
        self.nat_matched += 1
        if self.track_destinations:
            self._note_destination(
                client, flow.src, flow, octets, descendant=True, subscriber_id=sid
            )
        return True

    def _vider_attente_nat(self) -> None:
        """Rejoue les retours arrives avant leur montant, puis oublie le vieux."""
        attente, self._nat_attente = self._nat_attente, []
        for flow, octets, paquets, vantage, exporteur in attente:
            self._vantage_courant = vantage
            self._exporteur_courant = exporteur
            if not self._rejouer(flow, octets, paquets):
                self.nat_unmatched += 1
        limite = self.clock() - self.nat_ttl_s
        self._nat_exact = {k: v for k, v in self._nat_exact.items() if v[2] >= limite}
        self._nat_publiques = {k: v for k, v in self._nat_publiques.items() if v >= limite}
        for cle in list(self._nat_large):
            vivants = {s: v for s, v in self._nat_large[cle].items() if v[1] >= limite}
            if vivants:
                self._nat_large[cle] = vivants
            else:
                del self._nat_large[cle]

    def _credit(
        self,
        subscriber_id: int,
        vantage: str,
        *,
        down_bytes: int = 0,
        up_bytes: int = 0,
        down_packets: int = 0,
        up_packets: int = 0,
    ) -> None:
        cle = (subscriber_id, vantage, self._exporteur_courant)
        compteurs = self._subs.get(cle)
        if compteurs is None:
            compteurs = SubscriberCounters(subscriber_id=subscriber_id, vantage=vantage)
            self._subs[cle] = compteurs
        compteurs.down_bytes += down_bytes
        compteurs.up_bytes += up_bytes
        compteurs.down_packets += down_packets
        compteurs.up_packets += up_packets
        compteurs.flows += 1
        sens = self.direction_bytes.setdefault(vantage, [0, 0])
        sens[0] += down_bytes
        sens[1] += up_bytes

    def _credit_app(
        self,
        subscriber_id: int,
        app: str,
        vantage: str = "",
        *,
        down_bytes: int = 0,
        up_bytes: int = 0,
    ) -> None:
        cle = (subscriber_id, vantage, self._exporteur_courant, app)
        compteurs = self._apps.get(cle)
        if compteurs is None:
            compteurs = AppCounters(subscriber_id=subscriber_id, app=app)
            self._apps[cle] = compteurs
        compteurs.down_bytes += down_bytes
        compteurs.up_bytes += up_bytes

    def _note_destination(
        self,
        client: str,
        remote: str,
        flow: Flow,
        octets: int,
        *,
        descendant: bool,
        subscriber_id: int | None = None,
    ) -> None:
        """Retient l'autre bout de la conversation, s'il est sur internet.

        ``is_routable`` ecarte tout ce qui est prive, lien-local, multicast ou
        reserve : deux machines du reseau qui se parlent, un DNS interne, la
        supervision du PoP. Sans ce filtre, la liste des "services atteints" se
        remplirait de l'infrastructure de l'exploitant, et chaque adresse interne
        declencherait une requete de nom inverse pour rien.
        """
        # UN DE NOS ROUTEURS N'EST PAS UNE DESTINATION. Sonde de latence (le
        # routeur pingue chaque abonne toutes les 10 s), ping ou test vers un
        # loopback : du trafic d'exploitation, sans interet dans le trafic des
        # abonnes. Il reste compte dans les volumes, pas dans "qui parle a qui".
        # Import tardif : config -> flows -> mikrotik -> config serait circulaire.
        from app.collectors.mikrotik import own_address

        if own_address(remote) is not None or own_address(client) is not None:
            self.destinations_infra += 1
            return
        if not ipfinder.is_routable(remote) or self._is_customer(remote):
            # Ni internet, ni une destination : deux machines du reseau qui se
            # parlent. Ce n'est pas un rejet, c'est une absence de sujet -- et
            # ca ne se compte donc pas comme du trafic ecarte.
            return
        # EXPLOITATION. Soit l'un des bouts appartient au reseau lui-meme, soit
        # le port de service releve du plan de gestion : un routeur peut
        # parfaitement joindre une adresse publique pour s'administrer.
        if (self._is_infrastructure(remote) or self._is_infrastructure(client)) or service_port(
            flow
        ) in self.infrastructure_ports:
            self.destinations_infra += 1
            return
        cle = (client, remote)
        if not self._vantage_retenu((client, remote, descendant), self._vantage_courant):
            return
        compteurs = self._dests.get(cle)
        if compteurs is None:
            if len(self._dests) >= self.destination_limit:
                self.destinations_dropped += 1
                return
            compteurs = DestinationCounters(client=client, address=remote)
            self._dests[cle] = compteurs
        # Le rattachement peut apparaitre APRES la premiere vue : une session
        # PPPoE qui s'ouvre, une fiche saisie dans la minute. On le prend des
        # qu'il existe, sans jamais l'effacer sur un flux ou il manquait.
        if subscriber_id is not None:
            compteurs.subscriber_id = subscriber_id
        compteurs.port = service_port(flow) or compteurs.port
        compteurs.protocol = flow.protocol or compteurs.protocol
        compteurs.app = classify(flow)
        compteurs.flows += 1
        if descendant:
            compteurs.down_bytes += octets
        else:
            compteurs.up_bytes += octets
        duree = self._duree_s(flow)
        # Le temps actif de la fenetre = la plus longue duree couverte par un
        # de ses enregistrements (des flux paralleles couvrent le meme temps).
        compteurs.active_s = max(compteurs.active_s, duree)
        # Debit de CE flux = son volume sur la duree qu'il couvre. Le dernier
        # enregistrement de chaque flux remplace le precedent.
        self._live_meta[cle] = (
            compteurs.subscriber_id,
            compteurs.port,
            compteurs.protocol,
            compteurs.app,
        )
        cle_flux = (flow.src, flow.dst, flow.src_port, flow.dst_port, flow.protocol)
        self._live.setdefault(cle, {})[cle_flux] = (
            self.clock(),
            descendant,
            octets * 8 / duree,
        )

    def _duree_s(self, flow: Flow) -> float:
        """Duree REELLEMENT couverte par l'enregistrement.

        Elle n'est plus bornee a l'expiration active SUPPOSEE (1 min) : un
        routeur laisse a son defaut RouterOS exporte un flux long toutes les
        30 minutes, et diviser 30 minutes de volume par une minute affichait un
        debit trente fois trop haut (4,2 Mbps pour 270 kbps reels). Sans
        horodatage seulement, on suppose l'enregistrement plein.
        """
        if flow.duration_ms is None:
            return self.active_timeout_s
        return max(1.0, flow.duration_ms / 1000)

    _RANG_VANTAGE: ClassVar[dict[str, int]] = {"pop": 0, "unknown": 2, "": 2}

    def _vantage_retenu(self, cle: tuple[str, str, bool], vantage: str) -> bool:
        """Une seule SOURCE par conversation ET PAR SENS : le point de vue le
        plus proche du client, et dans ce point de vue un seul exporteur (le
        premier vu).

        PAR SENS, parce que les deux sens ne passent pas forcement par le meme
        routeur : routage asymetrique, deux sorties internet, un PoP qui
        n'exporte que l'entree de ses interfaces. Une source unique pour la
        conversation entiere jetait le sens que l'autre routeur etait seul a
        voir."""
        source = (vantage, self._exporteur_courant)
        actuel = self._pair_vantage.get(cle)
        if actuel is None or actuel == source:
            self._pair_vantage[cle] = source
            return True
        rang = self._RANG_VANTAGE
        if rang.get(vantage, 1) < rang.get(actuel[0], 1):
            self._pair_vantage[cle] = source
            return True
        return False

    def live_rates(self) -> dict[tuple[str, str], tuple[float, float]]:
        """Debit EN COURS de chaque conversation, (descendant, montant) en bit/s.

        Un flux vivant reemet un enregistrement a chaque expiration active : il
        reste compte tant que son dernier enregistrement date de moins que ce
        delai (plus une marge). Plus de division d'un volume d'une minute par
        l'age de la fenetre -- c'est ce qui triplait le chiffre affiche.
        """
        limite = self.clock() - (self.active_timeout_s + 15.0)
        sortie: dict[tuple[str, str], tuple[float, float]] = {}
        for cle in list(self._live):
            flux = {k: v for k, v in self._live[cle].items() if v[0] >= limite}
            if not flux:
                del self._live[cle]
                self._pair_vantage.pop((*cle, True), None)
                self._pair_vantage.pop((*cle, False), None)
                self._live_meta.pop(cle, None)
                continue
            self._live[cle] = flux
            bas = sum(bps for _t, desc, bps in flux.values() if desc)
            haut = sum(bps for _t, desc, bps in flux.values() if not desc)
            sortie[cle] = (bas, haut)
        return sortie

    def live_meta(self, cle: tuple[str, str]) -> tuple[int | None, int, int, str] | None:
        return self._live_meta.get(cle)

    @property
    def destinations_in_window(self) -> int:
        """Combien de couples (abonne, destination) la fenetre porte deja.

        Compte sans trier ni copier : l'etat du collecteur est relu a chaque
        affichage de l'interface, et il n'a pas a payer un tri pour rendre un
        nombre.
        """
        return len(self._dests)

    def live_destinations(self, limit: int = 100) -> list[DestinationCounters]:
        """Ce qui est en cours DANS LA FENETRE COURANTE, sans rien ecrire.

        C'est la seule vue reellement "en direct" du controleur : la base, elle,
        ne connait que les fenetres deja ecrites, donc au mieux la minute
        precedente. Un exploitant qui demande "qu'est-ce que ce client fait la,
        maintenant" ne veut pas une reponse vieille d'une minute.
        """
        lignes = sorted(self._dests.values(), key=lambda d: d.total_bytes, reverse=True)
        return lignes[:limit]

    def _note_host(
        self, flow: Flow, octets: int, *, exporter: str | None, pop_name: str | None
    ) -> None:
        """Retient une adresse du cote CLIENT qui n'est rattachee a rien.

        Le filtre sur l'espace d'adressage client est ce qui empeche cette liste
        de devenir un annuaire d'internet : sans lui, chaque serveur contacte
        par un abonne y apparaitrait.
        """
        for adresse, descendant in ((flow.dst, True), (flow.src, False)):
            if not self._is_customer(adresse):
                continue
            cle = (adresse, flow.vlan)
            compteurs = self._hosts.get(cle)
            if compteurs is None:
                if len(self._hosts) >= self.host_limit:
                    continue
                compteurs = HostCounters(
                    address=adresse, vlan_id=flow.vlan, exporter=exporter, pop_name=pop_name
                )
                self._hosts[cle] = compteurs
            if descendant:
                compteurs.down_bytes += octets
            else:
                compteurs.up_bytes += octets

    def _is_internet(self, address: str) -> bool:
        """L'adresse designe-t-elle vraiment l'autre bout, sur internet ?

        TROIS EXCLUSIONS, ET CHACUNE A SA RAISON :

        - le NON-ROUTABLE (prive, lien-local, multicast). La CGNAT
          (100.64.0.0/10) y echappe pourtant : la bibliotheque standard la dit
          privee, et un operateur y met ses clients. C'est precisement le cas ou
          il faut trancher nous-memes, avec l'espace client declare ;
        - l'ESPACE CLIENT. Deux clients qui se parlent ne sont une destination
          ni pour l'un ni pour l'autre, et la conversation apparaissait DEUX
          FOIS, une par sens ;
        - l'INFRASTRUCTURE. Le collecteur et les routeurs declares s'adressent
          en permanence : c'est le trafic le plus regulier du reseau, et il
          noyait ce qu'on venait chercher.
        """
        if not ipfinder.is_routable(address):
            return False
        return not self._is_customer(address) and not self._is_infrastructure(address)

    def _is_infrastructure(self, address: str) -> bool:
        if not self.infrastructure_networks:
            return False
        try:
            adresse = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(adresse in reseau for reseau in self.infrastructure_networks)

    def _is_customer(self, address: str) -> bool:
        if not self.customer_networks:
            return False
        try:
            adresse = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(adresse in reseau for reseau in self.customer_networks)

    def flush(self, ts: datetime, *, vantage: str | None = None) -> FlushBatch:
        """Vide la fenetre. UN CLIENT EST COMPTE A UN SEUL ENDROIT PAR SENS.

        Par point de vue, on retient l'exporteur qui voit le mieux CHAQUE SENS :
        le descendant de celui qui en voit le plus, le montant de celui qui en
        voit le plus -- souvent le meme, pas toujours. Choisir un seul exporteur
        sur le total jetait le sens qu'un autre routeur etait seul a porter
        (routage asymetrique, deux sorties internet). Rien n'est additionne : un
        flux qui traverse plusieurs PoP ne compte qu'une fois.

        Les applications suivent le meme choix, sens par sens, au point de vue
        ``vantage`` (celui qui compte). En ``auto``, chaque sens est lu la ou il
        est le mieux vu -- la meme regle que la lecture en base.
        """
        self._vider_attente_nat()
        par_point: dict[tuple[int, str], dict[str, SubscriberCounters]] = {}
        for (sid, point, exporteur), c in self._subs.items():
            par_point.setdefault((sid, point), {})[exporteur] = c
        meilleurs: dict[tuple[int, str], SubscriberCounters] = {}
        #: (exporteur du descendant, exporteur du montant) retenus.
        retenu: dict[tuple[int, str], tuple[str, str]] = {}
        for (sid, point), vus in par_point.items():
            bas = max(vus, key=lambda e: (vus[e].down_bytes, vus[e].total_bytes))
            haut = max(vus, key=lambda e: (vus[e].up_bytes, vus[e].total_bytes))
            cb, ch = vus[bas], vus[haut]
            meilleurs[(sid, point)] = SubscriberCounters(
                subscriber_id=sid,
                vantage=point,
                down_bytes=cb.down_bytes,
                up_bytes=ch.up_bytes,
                down_packets=cb.down_packets,
                up_packets=ch.up_packets,
                flows=cb.flows if bas == haut else max(cb.flows, ch.flows),
            )
            retenu[(sid, point)] = (bas, haut)
        points_par_client: dict[int, dict[str, SubscriberCounters]] = {}
        for (sid, point), c in meilleurs.items():
            points_par_client.setdefault(sid, {})[point] = c

        def point_lu(sid: int, descendant: bool) -> str | None:
            vus = points_par_client.get(sid, {})
            if not vus:
                return None
            if vantage in vus:
                return vantage
            if vantage == "auto":
                return max(
                    sorted(vus),
                    key=lambda p: vus[p].down_bytes if descendant else vus[p].up_bytes,
                )
            return sorted(vus)[0]

        apps: dict[tuple[int, str], AppCounters] = {}
        for (sid, point, exporteur, app), a in self._apps.items():
            bas, haut = retenu.get((sid, point), ("", ""))
            prend_bas = bool(a.down_bytes) and point == point_lu(sid, True) and exporteur == bas
            prend_haut = bool(a.up_bytes) and point == point_lu(sid, False) and exporteur == haut
            if not (prend_bas or prend_haut):
                continue
            cumul = apps.setdefault((sid, app), AppCounters(subscriber_id=sid, app=app))
            if prend_bas:
                cumul.down_bytes += a.down_bytes
            if prend_haut:
                cumul.up_bytes += a.up_bytes
        lot = FlushBatch(
            ts=ts,
            subscribers=list(meilleurs.values()),
            apps=list(apps.values()),
            hosts=list(self._hosts.values()),
            destinations=list(self._dests.values()),
        )
        self._subs = {}
        self._apps = {}
        self._hosts = {}
        self._dests = {}
        # Purge des conversations terminees (et de leur point de mesure).
        self.live_rates()
        return lot

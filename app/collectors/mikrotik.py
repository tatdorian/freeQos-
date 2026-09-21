"""Collecteur RouterOS : sessions PPPoE actives et compteurs d'octets.

POINT TECHNIQUE IMPORTANT
-------------------------
``/ppp/active/print`` ne porte PAS les compteurs d'octets d'une session. Sur
RouterOS, chaque session PPPoE cree une interface dynamique nommee
``<pppoe-LOGIN>`` et ce sont les compteurs de cette interface qui contiennent
``rx-byte`` / ``tx-byte``. Le collecteur fait donc deux lectures et les correle :

    /ppp/active/print  -> login, adresse, uptime, session-id
    /interface/print   -> rx-byte, tx-byte de <pppoe-LOGIN>

Le motif de nommage est configurable par routeur (``pppoe_interface_pattern``)
car il depend du profil PPP.

HORS-BANDE
----------
Ce module n'expose aucune methode d'ecriture. Les identifiants utilises sont ceux
du compte lecture seule ``qos-ro``. Le push des files CAKE est la phase 2 et
vivra dans un module distinct.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Any, Protocol

from app.collectors.parsing import (
    parse_bitrate,
    parse_counter,
    parse_flag,
    parse_routeros_duration_ms,
    parse_routeros_uptime,
    pppoe_interface_name,
)
from app.collectors.pop_census import (
    PartialCensusError,
    PopCensus,
    RouterTables,
    build_census,
    client_networks,
    missing_sources,
    sightings_from_census,
)
from app.collectors.topology import ethernet_capacity_mbps
from app.collectors.vlan_clients import explain_arp
from app.config import RouterConfig
from app.models import InterfaceSample, PppoeSession, VlanSighting

logger = logging.getLogger(__name__)


# Interfaces DYNAMIQUES : RouterOS en cree une par session PPPoE, nommee
# <pppoe-LOGIN>. Elles sont deja suivies abonne par abonne dans
# subscriber_metrics ; les reprendre ici dupliquerait chaque serie dans une
# table censee ne porter que les liens physiques, et la ferait grossir au
# rythme du parc plutot qu'au rythme des ports.
DYNAMIC_INTERFACE_TYPES = ("pppoe-in", "pppoe-out", "ppp-in", "ppp-out")

# Ou RouterOS range le router-id du routeur, selon sa version et les protocoles
# actives. Aucun n'est garanti present : on les essaie dans cet ordre.
_ROUTER_ID_PATHS = (
    "/routing/id",
    "/routing/ospf/instance",
    "/routing/bgp/instance",
    "/routing/bgp/connection",
)


def is_physical_interface(row: dict[str, Any]) -> bool:
    """Vrai pour un port qui porte un lien, faux pour une interface de session."""
    name = str(row.get("name") or "")
    if not name or name.startswith("<"):
        return False
    return not str(row.get("type") or "").startswith(DYNAMIC_INTERFACE_TYPES)


class RouterOsReadClient(Protocol):
    """Interface de lecture minimale d'un routeur : rend le collecteur testable."""

    def ppp_active(self) -> list[dict[str, Any]]: ...

    def interfaces(self) -> list[dict[str, Any]]: ...

    def identity(self) -> str | None: ...

    def system_resource(self) -> dict[str, Any]: ...

    def routerboard(self) -> dict[str, Any]: ...

    def license_id(self) -> str | None: ...

    def export_config(self) -> str: ...

    def ping(self, address: str, count: int = 1) -> list[dict[str, Any]]: ...

    # --- Topologie et etat du shaping (lecture seule) ---
    def neighbors(self) -> list[dict[str, Any]]: ...

    def ethernet(self) -> list[dict[str, Any]]: ...

    def monitor_traffic(self, interface: str) -> dict[str, Any]: ...

    def addresses(self) -> list[dict[str, Any]]: ...

    # --- Presence sur les VLAN routees (clients sans session PPPoE) ---
    def arp(self) -> list[dict[str, Any]]: ...

    def vlans(self) -> list[dict[str, Any]]: ...

    def pppoe_servers(self) -> list[dict[str, Any]]: ...

    # --- Recensement : les sources que l'ARP seule ne remplace pas ---
    def dhcp_leases(self) -> list[dict[str, Any]]: ...

    def dhcp_servers(self) -> list[dict[str, Any]]: ...

    def bridge_hosts(self) -> list[dict[str, Any]]: ...

    # --- Configuration : la verite terrain de la topologie ---
    def routes(self) -> list[dict[str, Any]]: ...

    def bridge_ports(self) -> list[dict[str, Any]]: ...

    def bondings(self) -> list[dict[str, Any]]: ...

    def ospf_neighbors(self) -> list[dict[str, Any]]: ...

    def bgp_sessions(self) -> list[dict[str, Any]]: ...

    def routing_ids(self) -> list[str]: ...

    def simple_queues(self) -> list[dict[str, Any]]: ...

    def firewall_filters(self) -> list[dict[str, Any]]: ...

    def firewall_address_list(self) -> list[dict[str, Any]]: ...

    def firewall_mangle(self) -> list[dict[str, Any]]: ...

    def traffic_flow(self) -> dict[str, Any]: ...

    def traffic_flow_targets(self) -> list[dict[str, Any]]: ...

    def queue_types(self) -> list[dict[str, Any]]: ...

    def queue_trees(self) -> list[dict[str, Any]]: ...

    def users(self) -> list[dict[str, Any]]: ...

    def user_groups(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class LibrouterosReadClient:
    """Client synchrone base sur librouteros (API binaire, port 8728).

    La connexion est persistante et reconstruite paresseusement : ouvrir une
    session API toutes les 10 secondes sur chaque PoP couterait cher en CPU
    routeur et polluerait les logs d'authentification. En cas d'erreur la
    connexion est fermee, le cycle suivant se reconnecte.

    Un verrou protege la connexion : l'appelant l'execute dans un thread pool et
    deux cycles ne doivent jamais se chevaucher sur la meme socket.
    """

    def __init__(self, config: RouterConfig) -> None:
        self._config = config
        self._api: Any = None
        self._lock = threading.Lock()
        # Chemins que CE routeur ne connait pas (version, protocole absent).
        # Les resonder a chaque cycle rouvrirait la session pour rien.
        self._chemins_absents: set[str] = set()

    # -- connexion ---------------------------------------------------------
    def _connect(self) -> Any:
        from librouteros import connect
        from librouteros.login import plain

        kwargs: dict[str, Any] = {
            "host": self._config.host,
            "port": self._config.port,
            "username": self._config.username,
            "password": self._config.resolve_password(),
            "timeout": self._config.timeout_s,
            "login_method": plain,
        }
        if self._config.use_ssl:
            # Posture TLS configurable PAR ROUTEUR (strict / empreinte / assumee
            # insecure), partagee avec le canal d'ecriture : plus de desactivation
            # codee en dur.
            from app.services.tls import ssl_wrapper_for

            kwargs["ssl_wrapper"] = ssl_wrapper_for(self._config)
        return connect(**kwargs)

    def _ensure(self) -> Any:
        if self._api is None:
            self._api = self._connect()
            logger.info(
                "Connecte a %s (%s:%s) en lecture seule",
                self._config.name,
                self._config.host,
                self._config.port,
            )
        return self._api

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:  # noqa: BLE001 - la socket est peut-etre deja morte
                pass
            self._api = None

    # -- lectures ----------------------------------------------------------
    def _query(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            try:
                api = self._ensure()
                return [dict(row) for row in api.path(path)]
            except Exception:
                # Toute erreur invalide la connexion : le prochain cycle rouvrira.
                self._drop()
                raise

    def ppp_active(self) -> list[dict[str, Any]]:
        return self._query("/ppp/active")

    def interfaces(self) -> list[dict[str, Any]]:
        return self._query("/interface")

    def identity(self) -> str | None:
        try:
            rows = self._query("/system/identity")
        except Exception:  # noqa: BLE001 - purement informatif
            return None
        return rows[0].get("name") if rows else None

    def system_resource(self) -> dict[str, Any]:
        rows = self._query("/system/resource")
        return dict(rows[0]) if rows else {}

    def export_config(self) -> str:
        """Config complete du routeur, facon ``/export`` (texte).

        C'est la vue la plus complete de ce que fait le routeur : adresses,
        tunnels, commentaires d'interface, routage. On l'analyse pour deduire des
        liens que ni MNDP ni les /30 ne revelent (tunnels EoIP/GRE, backhauls
        commentes). Best-effort : selon la version, l'API peut refuser ``/export``
        -- on renvoie alors une chaine vide et la decouverte se rabat sur le
        structure. LECTURE SEULE (``/export`` n'ecrit rien)."""
        with self._lock:
            try:
                api = self._ensure()
                lignes: list[str] = []
                for reponse in api("/export"):
                    if isinstance(reponse, dict):
                        section = reponse.get("section")
                        if section:
                            lignes.append(str(section))
                    else:
                        lignes.append(str(reponse))
                return "\n".join(lignes)
            except Exception:  # noqa: BLE001 - best-effort, on degrade proprement
                self._drop()
                return ""

    def license_id(self) -> str | None:
        """``/system/license`` : l'identite d'une instance CHR (machine virtuelle).

        Une CHR n'a pas de RouterBOARD, donc pas de numero de serie materiel :
        ``/system/routerboard`` ne rend rien. Son identifiant stable est le
        ``system-id`` de sa licence, propre a chaque instance -- y compris entre
        deux CHR deployees depuis la MEME image, qui partagent alors tout le
        reste, MAC d'interface comprises.

        C'est exactement le cas d'un laboratoire EVE-NG ou d'un parc virtualise :
        sans cette lecture, plusieurs routeurs bien distincts n'ont aucun
        identifiant qui les separe.
        """
        for chemin in ("/system/license", "/system/hardware"):
            if chemin in self._chemins_absents:
                continue
            try:
                rows = self._query(chemin)
            except Exception:  # noqa: BLE001 - absent selon la version et l'edition
                logger.debug("%s indisponible sur %s", chemin, self._config.name)
                self._chemins_absents.add(chemin)
                continue
            for row in rows:
                valeur = str(row.get("system-id") or row.get("software-id") or "").strip()
                if valeur:
                    return valeur
        return None

    def routerboard(self) -> dict[str, Any]:
        """``/system/routerboard`` : numero de serie et modele materiel.

        Le numero de serie est le SEUL identifiant qui ne change jamais, quel que
        soit le nom, l'adresse de gestion ou l'interface par laquelle on joint le
        routeur. C'est la meilleure cle anti-doublon quand un meme routeur est
        joignable sous plusieurs IP. Purement informatif : muet si absent (CHR,
        machine virtuelle sans RouterBOARD)."""
        try:
            rows = self._query("/system/routerboard")
        except Exception:  # noqa: BLE001 - purement informatif
            return {}
        return dict(rows[0]) if rows else {}

    # --- Topologie et etat du shaping (lecture seule) ---
    def neighbors(self) -> list[dict[str, Any]]:
        """Voisins MNDP / LLDP / CDP.

        Source maitresse de la topologie : pour chaque interface locale, elle
        nomme l'equipement d'en face. Aucun autre appel ne donne l'adjacence
        physique de facon aussi directe.
        """
        return self._query("/ip/neighbor")

    def ethernet(self) -> list[dict[str, Any]]:
        """Ports ethernet : le debit negocie est le plafond physique du lien."""
        return self._query("/interface/ethernet")

    def monitor_traffic(self, interface: str) -> dict[str, Any]:
        """Debit INSTANTANE d'une interface, mesure par le routeur lui-meme.

        Les compteurs cumulatifs ne donnent un debit qu'apres deux lectures :
        pour repondre "combien passe MAINTENANT sur ce lien", il faut demander
        au routeur, qui tient deja la mesure. ``once`` rend la commande
        ponctuelle au lieu de streamer.

        C'est une commande de LECTURE : elle ne modifie aucune configuration.
        """
        with self._lock:
            try:
                api = self._ensure()
                rows = [
                    dict(row)
                    for row in api(
                        "/interface/monitor-traffic",
                        **{"interface": interface, "once": True},
                    )
                ]
            except Exception:
                self._drop()
                raise
        return rows[0] if rows else {}

    def addresses(self) -> list[dict[str, Any]]:
        return self._query("/ip/address")

    def arp(self) -> list[dict[str, Any]]:
        """Table ARP. Le seul signal de presence d'un client sans session.

        Sur une VLAN routee, un client qui parle laisse une entree ARP (IP +
        MAC) rattachee a son interface. C'est une LECTURE, elle ne configure
        rien -- et elle ne dit rien de plus que "cette adresse a parle".
        """
        return self._query("/ip/arp")

    def vlans(self) -> list[dict[str, Any]]:
        return self._query("/interface/vlan")

    def dhcp_leases(self) -> list[dict[str, Any]]:
        """Baux DHCP. La memoire que la table ARP n'a pas.

        Une entree ARP s'efface apres quelques minutes de silence ; un bail
        survit a son client pendant toute sa duree. C'est aussi la seule table
        qui porte souvent un NOM -- le ``host-name`` annonce par la machine, ou
        le commentaire pose par l'exploitant.
        """
        return self._query("/ip/dhcp-server/lease")

    def dhcp_servers(self) -> list[dict[str, Any]]:
        """Serveurs DHCP declares : sur quelles interfaces un bail peut exister."""
        return self._query("/ip/dhcp-server")

    def bridge_hosts(self) -> list[dict[str, Any]]:
        """Table de ponts : MAC, port physique et VLAN, vus en L2.

        C'est la reponse au pont en filtrage VLAN. Quand l'adressage client est
        porte par un pont, ``/ip/arp`` ne nomme que ce pont et le numero de VLAN
        est perdu : cette table le porte, avec le port par lequel la machine
        est branchee. La jointure avec l'ARP se fait par la MAC.
        """
        return self._query("/interface/bridge/host")

    def routes(self) -> list[dict[str, Any]]:
        """Table de routage.

        C'est la seule source qui dise QUI EST AU-DESSUS. Une adjacence MNDP
        dit "ces deux equipements se voient" ; une route par defaut dit "tout
        ce que je ne sais pas router, je l'envoie la-bas" -- c'est-a-dire la
        relation hierarchique elle-meme, celle qu'un arbre doit representer.
        """
        return self._query("/ip/route")

    def bridge_ports(self) -> list[dict[str, Any]]:
        return self._query("/interface/bridge/port")

    def bondings(self) -> list[dict[str, Any]]:
        return self._query("/interface/bonding")

    def ospf_neighbors(self) -> list[dict[str, Any]]:
        """Voisins OSPF etablis : une adjacence PROUVEE, pas devinee.

        MNDP est un protocole de decouverte L2 : il voit ce qui est sur le meme
        segment, switch compris. Une adjacence OSPF en etat Full signifie que
        les deux routeurs echangent reellement des routes.
        """
        return self._query("/routing/ospf/neighbor")

    def routing_ids(self) -> list[str]:
        """Le ``router-id`` DU ROUTEUR, lu dans sa configuration de routage.

        Dans un reseau d'operateur le router-id EST le loopback : c'est sa raison
        d'etre. C'est donc le meilleur indice quand aucune interface ne s'appelle
        ``lo`` -- et sans loopback, un routeur retombe sur sa MAC et ses adresses
        d'interface pour s'identifier, ce qui rend indiscernables deux PoPs
        deployes depuis la meme configuration modele.

        Cette valeur n'etait jusqu'ici lue que dans le TEXTE de ``/export``, dont
        l'API peut refuser l'execution selon la version : la troisieme source de
        loopback disparaissait alors sans bruit. On la lit ici par les chemins
        STRUCTURES, qui repondent toujours.

        Les chemins different d'une version et d'un protocole a l'autre ; on les
        essaie tous et on ignore ceux qui n'existent pas. Ordre volontaire :
        ``/routing/id`` d'abord (v7, la declaration explicite), puis les
        instances OSPF et BGP.

        Un chemin absent est RETENU comme tel : ``_query`` ferme la connexion a
        la moindre erreur, et resonder quatre chemins inexistants a chaque
        decouverte rouvrirait la session API autant de fois -- exactement ce que
        la connexion persistante de cette classe existe pour eviter.
        """
        trouves: list[str] = []
        for chemin in _ROUTER_ID_PATHS:
            if chemin in self._chemins_absents:
                continue
            try:
                rows = self._query(chemin)
            except Exception:  # noqa: BLE001 - chemin absent selon la version
                logger.debug("%s indisponible sur %s", chemin, self._config.name)
                self._chemins_absents.add(chemin)
                continue
            for row in rows:
                if parse_flag(row.get("disabled")):
                    continue
                # '/routing/id' porte la valeur dans 'id', les instances dans
                # 'router-id'. Une instance peut aussi referencer une entree de
                # '/routing/id' par son nom : ce n'est alors pas une adresse, et
                # l'appelant l'ecartera.
                valeur = str(row.get("router-id") or row.get("id") or "").strip()
                if valeur and valeur not in trouves:
                    trouves.append(valeur)
        return trouves

    def bgp_sessions(self) -> list[dict[str, Any]]:
        return self._query("/routing/bgp/session")

    def pppoe_servers(self) -> list[dict[str, Any]]:
        """Serveurs PPPoE declares : sert a EXCLURE leurs interfaces.

        Une VLAN qui porte un serveur PPPoE a deja sa source d'identite dans
        /ppp/active. Y chercher des clients par ARP ferait doublonner chaque
        abonne, et pire, le presenterait comme un client statique a declarer.
        """
        return self._query("/interface/pppoe-server/server")

    def simple_queues(self) -> list[dict[str, Any]]:
        return self._query("/queue/simple")

    def firewall_filters(self) -> list[dict[str, Any]]:
        """Regles de filtrage, DANS L'ORDRE. Sert a detecter le fasttrack.

        Une regle ``action=fasttrack-connection`` fait sauter aux connexions
        etablies le reste du chemin, FILES SIMPLES COMPRISES. C'est la premiere
        cause d'un plafond qui ne plafonne pas, et elle est active par defaut
        dans le pare-feu d'usine. Lecture seule : le controleur ne touche pas au
        pare-feu, il se contente de dire pourquoi ses files ne servent a rien.
        """
        return self._query("/ip/firewall/filter")

    def firewall_address_list(self) -> list[dict[str, Any]]:
        """Listes d'adresses nommees. Support des restrictions de trafic.

        C'est le seul objet du pare-feu que le controleur ECRIT, et seulement
        les entrees qui portent la marque ``freeqos:managed``. Une liste tenue
        par l'exploitant (routage par politique, acces d'administration) est lue
        mais jamais modifiee.
        """
        return self._query("/ip/firewall/address-list")

    def firewall_mangle(self) -> list[dict[str, Any]]:
        """Regles de marquage. Sert a plafonner UN trafic sans toucher au reste.

        Un plafond par service se pose en deux temps : marquer les paquets
        concernes ici, puis accrocher une file d'arbre a cette marque. C'est la
        seule facon de plafonner cinquante blocs d'adresses sans poser cinquante
        files.
        """
        return self._query("/ip/firewall/mangle")

    def traffic_flow(self) -> dict[str, Any]:
        """Reglage d'export NetFlow du routeur : actif, et sur quelles interfaces.

        Une seule ligne : c'est un reglage global, pas une collection.
        """
        rows = self._query("/ip/traffic-flow")
        return dict(rows[0]) if rows else {}

    def traffic_flow_targets(self) -> list[dict[str, Any]]:
        """Collecteurs vers lesquels ce routeur exporte deja ses flux."""
        return self._query("/ip/traffic-flow/target")

    def queue_types(self) -> list[dict[str, Any]]:
        return self._query("/queue/type")

    def queue_trees(self) -> list[dict[str, Any]]:
        return self._query("/queue/tree")

    def users(self) -> list[dict[str, Any]]:
        """Comptes declares. Sert a savoir de quoi le notre est capable."""
        return self._query("/user")

    def user_groups(self) -> list[dict[str, Any]]:
        """Groupes et leurs politiques (read, write, api, test...)."""
        return self._query("/user/group")

    def ping(self, address: str, count: int = 1) -> list[dict[str, Any]]:
        """Sonde active depuis le routeur vers l'abonne.

        C'est une COMMANDE, pas une ecriture de configuration : elle ne modifie
        rien sur l'equipement. Elle exige la politique 'test' sur le compte, que
        le groupe qos-ro possede deja.
        """
        with self._lock:
            try:
                api = self._ensure()
                return [dict(row) for row in api("/ping", address=address, count=count)]
            except Exception:
                self._drop()
                raise


def _split_pair(value: Any) -> tuple[int | None, int | None] | None:
    """Decoupe un couple RouterOS ``"<upload>/<download>"``.

    Renvoie None si la valeur est absente ou illisible : une file qui n'a pas
    encore de compteur ne doit pas produire un faux zero, qui se lirait comme
    "ce client ne consomme rien".
    """
    texte = str(value or "").strip()
    if "/" not in texte:
        return None
    gauche, _, droite = texte.partition("/")
    return parse_counter(gauche), parse_counter(droite)


class MikrotikCollector:
    """Lit les sessions PPPoE d'un routeur et les normalise en PppoeSession."""

    def __init__(
        self,
        config: RouterConfig,
        client: RouterOsReadClient | None = None,
    ) -> None:
        self.config = config
        self._client = client or LibrouterosReadClient(config)

    @property
    def name(self) -> str:
        return self.config.name

    async def collect(self) -> list[PppoeSession]:
        """Execute la lecture bloquante dans un thread, avec garde-fou de duree.

        librouteros est synchrone. Sans ce passage par un thread, un routeur qui
        ne repond plus figerait la boucle asyncio et donc tous les autres PoPs.
        """
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.collect_sync), timeout=timeout)

    def collect_sync(self) -> list[PppoeSession]:
        active = self._client.ppp_active()
        interfaces = self._client.interfaces()
        return self._correlate(active, interfaces)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Debit des liens
    # ------------------------------------------------------------------
    async def collect_interfaces(self) -> list[InterfaceSample]:
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self.collect_interfaces_sync), timeout=timeout
        )

    def collect_interfaces_sync(self) -> list[InterfaceSample]:
        """Compteurs de chaque port physique, SANS les debits.

        Le debit se derive de deux lectures successives, exactement comme pour
        les abonnes : c'est le service de collecte qui le calcule, parce que
        c'est lui qui garde l'etat entre deux cycles.
        """
        rows = self._client.interfaces()
        try:
            capacites = {
                str(row.get("name") or ""): ethernet_capacity_mbps(row)
                for row in self._client.ethernet()
            }
        except Exception:  # noqa: BLE001 - la capacite n'est qu'un plafond d'affichage
            # Un CHR sans port physique refuse cet appel. Les compteurs restent
            # exploitables ; seule la jauge de charge perd sa reference.
            logger.debug("Capacites ethernet indisponibles sur %s", self.name)
            capacites = {}

        ts = utcnow()
        echantillons: list[InterfaceSample] = []
        for row in rows:
            if not is_physical_interface(row):
                continue
            nom = str(row.get("name"))
            echantillons.append(
                InterfaceSample(
                    ts=ts,
                    router_name=self.name,
                    interface=nom,
                    kind=_as_str(row.get("type")),
                    running=parse_flag(row.get("running")),
                    capacity_mbps=capacites.get(nom),
                    rx_bytes=parse_counter(row.get("rx-byte")),
                    tx_bytes=parse_counter(row.get("tx-byte")),
                )
            )
        return echantillons

    # ------------------------------------------------------------------
    # Presence sur les VLAN routees
    # ------------------------------------------------------------------
    async def collect_vlan_clients(
        self, *, known_equipment: Collection[str] = ()
    ) -> list[VlanSighting]:
        timeout = max(self.config.timeout_s * 6, 10.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self.collect_vlan_clients_sync, known_equipment=known_equipment),
            timeout=timeout,
        )

    async def explain_vlan_clients(
        self, *, known_equipment: Collection[str] = ()
    ) -> dict[str, Any]:
        """Pourquoi telle adresse est vue, ou ne l'est pas, sur CE routeur.

        Meme lecture que la detection, meme chemin de decision : ce n'est pas
        une simulation, c'est le filtre reel qui rend ses motifs. Un diagnostic
        qui raconterait autre chose que ce que fait le code serait pire que pas
        de diagnostic.
        """
        timeout = max(self.config.timeout_s * 6, 10.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self.explain_vlan_clients_sync, known_equipment=known_equipment),
            timeout=timeout,
        )

    def explain_vlan_clients_sync(self, *, known_equipment: Collection[str] = ()) -> dict[str, Any]:
        """Le rapport ARP ligne a ligne, ET le recensement complet du PoP.

        Les deux repondent a des questions differentes et sont tous les deux
        necessaires : "pourquoi cette ligne d'ARP a-t-elle ete ecartee ?" se lit
        dans ``by_reason``, "qui vit sur ce PoP ?" se lit dans ``recensement``.
        Ils sortent de la MEME lecture, faite une fois : un diagnostic calcule a
        part finirait par decrire un autre routeur que celui qu'on regarde.
        """
        tables, census = self._recenser_sync(known_equipment=known_equipment)
        rapport = explain_arp(
            tables.arp,
            tables.vlans,
            tables.pppoe_servers,
            client_networks=client_networks(census.subnets),
        )
        rapport["router"] = self.name
        rapport["pop_name"] = self.config.effective_pop_name
        rapport["recensement"] = census.as_dict()
        return rapport

    # ------------------------------------------------------------------
    # Recensement du PoP
    # ------------------------------------------------------------------
    async def census(self, *, known_equipment: Collection[str] = ()) -> PopCensus:
        """Recensement complet : plus long que la detection, et fait pour ca."""
        timeout = max(self.config.timeout_s * 6, 10.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self.census_sync, known_equipment=known_equipment),
            timeout=timeout,
        )

    def census_sync(self, *, known_equipment: Collection[str] = ()) -> PopCensus:
        return self._recenser_sync(known_equipment=known_equipment)[1]

    def _recenser_sync(
        self, *, known_equipment: Collection[str] = ()
    ) -> tuple[RouterTables, PopCensus]:
        """Lit les tables du recensement, puis les croise.

        CHAQUE LECTURE EST TOLEREE SEPAREMENT, et son echec est CONSERVE. Un
        routeur sans serveur DHCP, sans pont ou sans OSPF n'a pas ces tables --
        ce n'est pas une panne. Mais une source muette doit se voir dans le
        resultat : un recensement ampute qui se presenterait comme complet
        serait exactement le piege que ce module existe pour eviter.

        Une quinzaine de ``print`` en lecture seule, a la cadence lache du job
        de detection (5 minutes par defaut) : c'est le prix d'un recensement qui
        ne depend pas du nom des interfaces.
        """
        tables = RouterTables()

        def lire(champ: str, lecture: Callable[[], list[dict[str, Any]]]) -> None:
            try:
                setattr(tables, champ, lecture())
            except Exception as exc:  # noqa: BLE001 - une table absente n'annule pas le reste
                tables.unreadable[champ] = f"{type(exc).__name__}: {exc}"
                logger.debug("Table %s illisible sur %s : %s", champ, self.name, exc)

        lire("addresses", self._client.addresses)
        lire("arp", self._client.arp)
        lire("vlans", self._client.vlans)
        lire("pppoe_servers", self._client.pppoe_servers)
        lire("ppp_active", self._client.ppp_active)
        lire("dhcp_leases", self._client.dhcp_leases)
        lire("dhcp_servers", self._client.dhcp_servers)
        lire("bridge_hosts", self._client.bridge_hosts)
        lire("bridge_ports", self._client.bridge_ports)
        lire("interfaces", self._client.interfaces)
        lire("bondings", self._client.bondings)
        lire("routes", self._client.routes)
        lire("queues", self._client.simple_queues)
        lire("neighbors", self._client.neighbors)
        lire("ospf_neighbors", self._client.ospf_neighbors)
        lire("bgp_sessions", self._client.bgp_sessions)

        census = build_census(
            tables,
            router_name=self.name,
            pop_name=self.config.effective_pop_name,
            known_equipment=known_equipment,
        )
        return tables, census

    def collect_vlan_clients_sync(
        self, *, known_equipment: Collection[str] = ()
    ) -> list[VlanSighting]:
        """Qui vit sur ce routeur, hors abonnes PPPoE deja identifies.

        La detection ne part plus du NOM des interfaces mais du PLAN
        D'ADRESSAGE du PoP, croise avec sept sources de presence. C'est ce qui
        fait apparaitre les clients qu'un pont en filtrage VLAN cachait : leur
        entree ARP nomme le pont, mais leur adresse tombe dans un sous-reseau
        que ce routeur dessert, et cela suffit.

        Une source manquante ne fait pas perdre les autres : les observations
        obtenues remontent avec l'erreur, portees par ``PartialCensusError``.
        """
        tables, census = self._recenser_sync(known_equipment=known_equipment)
        vues = sightings_from_census(census)
        manquantes = missing_sources(tables)
        if manquantes:
            detail = ", ".join(f"{champ} ({motif})" for champ, motif in sorted(manquantes.items()))
            raise PartialCensusError(f"recensement incomplet : {detail}", vues)
        return vues

    # ------------------------------------------------------------------
    # Compteurs des files simples
    # ------------------------------------------------------------------
    async def queue_counters(self) -> dict[str, tuple[int | None, int | None]]:
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.queue_counters_sync), timeout=timeout)

    def queue_counters_sync(self) -> dict[str, tuple[int | None, int | None]]:
        """Octets cumules par cible de file simple : ``cible -> (rx, tx)``.

        POURQUOI CETTE LECTURE EXISTE
        -----------------------------
        Un abonne PPPoE a une interface dynamique a son nom, et c'est elle qui
        compte son trafic. Un client a IP fixe n'a rien de tel : son trafic se
        fond dans celui d'un VLAN ou d'un port partage. La seule chose qui
        compte SES octets a lui est la file qui le vise.

        Ce n'est donc pas une source inventee, c'est le compteur que RouterOS
        tient deja sur une file qui existe. Corollaire assume : sans file posee,
        il n'y a pas de mesure -- un client statique n'est mesurable qu'a partir
        du moment ou l'enforcement lui a construit sa file.

        Les files etrangeres sont lues aussi : si l'operateur brid(e) deja le
        client a la main, autant s'en servir plutot que de n'afficher rien.
        """
        compteurs: dict[str, tuple[int | None, int | None]] = {}
        for row in self._client.simple_queues():
            octets = _split_pair(row.get("bytes"))
            if octets is None:
                continue
            # RouterOS exprime la paire DU POINT DE VUE DE LA CIBLE :
            # <upload>/<download>. L'upload de la cible est ce que le routeur
            # RECOIT d'elle, donc rx ; son download est ce qu'il lui EMET, tx.
            # Meme convention que pour les sessions PPPoE (cf. app/models.py).
            rx, tx = octets
            # 'target' peut lister plusieurs adresses separees par des virgules.
            for cible in str(row.get("target") or "").split(","):
                cible = cible.strip()
                if cible:
                    compteurs[cible] = (rx, tx)
        return compteurs

    async def measure_interface(self, interface: str) -> dict[str, Any]:
        """Debit instantane d'une interface, a la demande.

        Sert le "je veux voir le debit de ce lien MAINTENANT" : au lieu
        d'attendre le prochain cycle de compteurs, on demande au routeur la
        mesure qu'il tient deja.
        """
        timeout = max(self.config.timeout_s * 2, 4.0)
        row = await asyncio.wait_for(
            asyncio.to_thread(self._client.monitor_traffic, interface), timeout=timeout
        )
        return {
            "interface": interface,
            "rx_bps": parse_bitrate(row.get("rx-bits-per-second")),
            "tx_bps": parse_bitrate(row.get("tx-bits-per-second")),
            "rx_pps": parse_counter(row.get("rx-packets-per-second")),
            "tx_pps": parse_counter(row.get("tx-packets-per-second")),
        }

    async def ping(self, address: str, count: int = 1) -> float | None:
        """RTT du routeur vers l'abonne, en millisecondes.

        Retourne None si l'abonne ne repond pas : une absence de mesure vaut
        mieux qu'une valeur inventee.
        """
        timeout = max(self.config.timeout_s * 2, 4.0) + count
        rows = await asyncio.wait_for(
            asyncio.to_thread(self._client.ping, address, count), timeout=timeout
        )
        times = [
            parse_routeros_duration_ms(row.get("time"))
            for row in rows
            if row.get("time") is not None
        ]
        valides = [t for t in times if t is not None]
        return min(valides) if valides else None

    async def health(self) -> dict[str, Any]:
        """Etat de sante du routeur : charge CPU, memoire, uptime, version.

        POURQUOI CETTE LECTURE EST SEPAREE DE ``probe``. Une sonde complete
        verifie aussi les droits d'ecriture et correle les sessions : c'est un
        diagnostic qu'on declenche a la main. La sante, elle, se regarde en
        continu -- un routeur a 95 % de CPU n'appliquera pas les files qu'on lui
        envoie, et le savoir AVANT de chercher pourquoi un abonne n'est pas
        bride epargne une demi-heure.

        Une seule commande de lecture, ``/system/resource``.
        """
        timeout = max(self.config.timeout_s, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.health_sync), timeout=timeout)

    def health_sync(self) -> dict[str, Any]:
        resource = self._client.system_resource()
        libre = parse_counter(resource.get("free-memory"))
        total = parse_counter(resource.get("total-memory"))
        return {
            "router": self.name,
            "pop_name": self.config.effective_pop_name,
            "host": self.config.host,
            "reachable": True,
            "identity": _as_str(resource.get("identity")) or self._client.identity(),
            "version": _as_str(resource.get("version")),
            "board_name": _as_str(resource.get("board-name")),
            "uptime_s": parse_routeros_uptime(resource.get("uptime")),
            "cpu_load_pct": parse_counter(resource.get("cpu-load")),
            "cpu_count": parse_counter(resource.get("cpu-count")),
            "free_memory": libre,
            "total_memory": total,
            # La part UTILISEE, calculee ici et pas dans l'interface : deux
            # implementations du meme rapport divergent toujours. None quand
            # RouterOS ne donne pas le total -- un pourcentage sans denominateur
            # serait invente.
            "memory_used_pct": (
                round((total - libre) / total * 100, 1)
                if libre is not None and total not in (None, 0)
                else None
            ),
            "free_hdd": parse_counter(resource.get("free-hdd-space")),
        }

    async def probe(self) -> dict[str, Any]:
        """Teste la connexion et renvoie de quoi identifier le routeur.

        Utilise par le bouton "Tester la connexion" de l'interface : un
        administrateur doit pouvoir verifier ses identifiants avant d'enregistrer
        un PoP, plutot que de decouvrir l'erreur dans les logs dix minutes apres.
        """
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.probe_sync), timeout=timeout)

    def probe_sync(self) -> dict[str, Any]:
        resource = self._client.system_resource()
        sessions = self._client.ppp_active()
        interfaces = self._client.interfaces()

        # Droits reels du compte : c'est ce qui determine si l'enforcement
        # pourra fonctionner, pas ce qui est declare dans l'inventaire.
        from app.enforcement.capability import inspect_write_capability

        try:
            capacite = inspect_write_capability(
                self.config.rw_username or self.config.username,
                self._client.users(),
                self._client.user_groups(),
            ).to_dict()
        except Exception as exc:  # noqa: BLE001 - purement informatif
            capacite = {
                "username": self.config.rw_username or self.config.username,
                "can_write": None,
                "detail": f"droits non verifiables ({type(exc).__name__})",
            }

        return {
            "write_capability": capacite,
            "reachable": True,
            "identity": self._client.identity(),
            "version": _as_str(resource.get("version")),
            "board_name": _as_str(resource.get("board-name")),
            "uptime": _as_str(resource.get("uptime")),
            "cpu_load": resource.get("cpu-load"),
            "free_memory": resource.get("free-memory"),
            "ppp_active_sessions": len(sessions),
            "interfaces": len(interfaces),
            # Le point qui compte vraiment : sans correlation, pas de debit.
            "correlated_sessions": sum(
                1
                for session in self._correlate(sessions, interfaces)
                if session.rx_bytes is not None
            ),
        }

    # ------------------------------------------------------------------
    def _correlate(
        self, active: list[dict[str, Any]], interfaces: list[dict[str, Any]]
    ) -> list[PppoeSession]:
        by_name = {str(row.get("name", "")): row for row in interfaces}
        pop_name = self.config.effective_pop_name
        sessions: list[PppoeSession] = []

        for row in active:
            login = row.get("name")
            if not login:
                continue
            login = str(login)
            iface_name = pppoe_interface_name(self.config.pppoe_interface_pattern, login)
            iface = by_name.get(iface_name)
            if iface is None:
                iface, iface_name = self._fuzzy_interface(by_name, login, iface_name)

            sessions.append(
                PppoeSession(
                    login=login,
                    router_name=self.config.name,
                    pop_name=pop_name,
                    address=_as_str(row.get("address")),
                    caller_id=_as_str(row.get("caller-id")),
                    service=_as_str(row.get("service")),
                    session_id=_as_str(row.get("session-id")),
                    uptime_s=parse_routeros_uptime(row.get("uptime")),
                    interface=iface_name if iface is not None else None,
                    rx_bytes=parse_counter(iface.get("rx-byte")) if iface else None,
                    tx_bytes=parse_counter(iface.get("tx-byte")) if iface else None,
                )
            )
        return sessions

    def _fuzzy_interface(
        self, by_name: dict[str, dict[str, Any]], login: str, expected: str
    ) -> tuple[dict[str, Any] | None, str]:
        """Repli si le motif de nommage ne correspond pas.

        Certains profils PPP renomment l'interface dynamique. On cherche alors une
        interface dont le nom contient le login, ce qui reste sans ambiguite tant
        que les logins ne sont pas prefixes les uns des autres. Si plusieurs
        candidats sortent, on n'en choisit aucun plutot que d'attribuer un debit
        au mauvais abonne.
        """
        candidates = [(name, row) for name, row in by_name.items() if login in name]
        if len(candidates) == 1:
            name, row = candidates[0]
            logger.debug(
                "%s: interface '%s' introuvable, correlation par nom sur '%s'",
                self.config.name,
                expected,
                name,
            )
            return row, name
        if len(candidates) > 1:
            logger.warning(
                "%s: correlation ambigue pour le login '%s' (%d interfaces)",
                self.config.name,
                login,
                len(candidates),
            )
        return None, expected


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def utcnow() -> datetime:
    return datetime.now(tz=UTC)

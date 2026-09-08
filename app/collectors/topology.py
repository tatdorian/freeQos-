"""Decouverte de topologie : quel lien va ou.

CINQ SOURCES, RECONCILIEES
--------------------------
Aucune ne suffit seule ; ensemble elles donnent le graphe complet.

1. ``/ip/neighbor``    MNDP / LLDP / CDP. Source maitresse : pour chaque interface
                       locale, elle nomme l'equipement d'en face (identite,
                       plateforme, MAC, IP). C'est l'adjacence physique.
2. ``/interface/ethernet``  debit negocie = plafond physique du lien.
3. ``/ip/address``     rattache chaque interface a un segment L3.
4. UISP                liens radio PtP/PtMP et capacite reelle du moment.
5. ``/ppp/active``     et surtout son champ ``caller-id``, qui porte la MAC du
                       CPE de l'abonne.

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

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.collectors.parsing import parse_bitrate

logger = logging.getLogger(__name__)

# Types de noeuds, du plus haut au plus bas dans l'arbre de shaping.
KIND_GATEWAY = "gateway"
KIND_CORE = "core"
KIND_POP = "pop"
KIND_RADIO = "radio"
KIND_SECTOR = "sector"
KIND_CPE = "cpe"
KIND_UNKNOWN = "unknown"

# Un lien est physique (cable/radio) ou logique (session PPPoE).
LINK_ETHERNET = "ethernet"
LINK_RADIO = "radio"
LINK_PPPOE = "pppoe"


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
    warnings: list[str] = field(default_factory=list)

    def add_node(self, node: TopologyNode) -> TopologyNode:
        existing = self.nodes.get(node.key)
        if existing is None:
            self.nodes[node.key] = node
            return node
        # Fusion : on complete les champs manquants sans ecraser ce qu'on sait.
        for attribut in ("name", "mac", "address", "platform", "version", "uisp_device_id"):
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


def router_node_key(router_name: str) -> str:
    return f"router:{router_name}"


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
) -> None:
    """Ajoute au graphe ce qu'un routeur voit autour de lui."""
    router_key = router_node_key(router_name)
    snapshot.add_node(
        TopologyNode(
            key=router_key,
            name=pop_name or router_name,
            kind=KIND_POP,
            address=host,
            router_name=router_name,
            attributes={"managed": True},
        )
    )

    capacites = {}
    for row in ethernet:
        nom = str(row.get("name") or "")
        if nom:
            capacites[nom] = ethernet_capacity_mbps(row)

    reseaux = {}
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

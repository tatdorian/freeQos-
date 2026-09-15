"""Detection des clients presents sur une VLAN routee, sans session PPPoE.

LE PROBLEME
-----------
Un abonne PPPoE s'annonce : ``/ppp/active`` le nomme. Un client a IP fixe sur
VLAN routee n'annonce rien. RouterOS n'a aucune table qui dise "voici les VLAN
clientes" -- la notion n'existe pas dans sa configuration.

LE SIGNAL RETENU
----------------
Il existe malgre tout une trace fiable de PRESENCE : la table ARP. Un client qui
parle sur une VLAN routee y laisse son couple IP/MAC, rattache a l'interface
VLAN. C'est tout ce dont on a besoin pour dire "quelque chose vit ici".

CE QUE CE SIGNAL NE DIT PAS, ET C'EST L'ESSENTIEL
-------------------------------------------------
Une entree ARP ne dit ni a QUI appartient l'adresse, ni quel debit a ete vendu.
Une imprimante, une camera, un routeur de passage ou l'equipement d'un autre
operateur produisent exactement la meme trace qu'un client. La detection ne
peut donc que PROPOSER : c'est un humain qui transforme un candidat en client
declare, en saisissant le plan que lui seul connait.

Aucune fiche n'est creee ici, aucun plan n'est devine, aucune file n'est posee.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence
from typing import Any

from app.collectors.parsing import parse_flag
from app.models import VlanSighting

logger = logging.getLogger(__name__)


def _texte(valeur: Any) -> str:
    return str(valeur or "").strip()


def _normalise_mac(valeur: Any) -> str | None:
    """MAC en majuscules separees par ':', ou None si illisible."""
    brut = _texte(valeur)
    chiffres = "".join(c for c in brut if c.isalnum()).upper()
    if len(chiffres) != 12:
        return None
    return ":".join(chiffres[i : i + 2] for i in range(0, 12, 2))


def _adresse_exploitable(valeur: Any) -> str | None:
    brut = _texte(valeur)
    if not brut:
        return None
    try:
        adresse = ipaddress.ip_address(brut)
    except ValueError:
        return None
    if adresse.is_unspecified or adresse.is_loopback or adresse.is_multicast:
        return None
    return str(adresse)


def vlan_index(rows: Sequence[dict[str, Any]]) -> dict[str, int | None]:
    """``nom d'interface VLAN -> identifiant 802.1Q``, VLAN desactivees exclues."""
    index: dict[str, int | None] = {}
    for row in rows:
        if parse_flag(row.get("disabled")):
            continue
        nom = _texte(row.get("name"))
        if not nom:
            continue
        brut = _texte(row.get("vlan-id")) or _texte(row.get("vlan_id"))
        try:
            index[nom] = int(brut) if brut else None
        except ValueError:
            index[nom] = None
    return index


def pppoe_interfaces(rows: Sequence[dict[str, Any]]) -> set[str]:
    """Interfaces qui hebergent un serveur PPPoE actif.

    Elles sont exclues de la detection : leurs clients ont deja une identite
    dans ``/ppp/active``. Sans cette exclusion, chaque abonne PPPoE
    apparaitrait aussi en candidat "client a IP fixe a declarer", ce qui est
    faux et serait la pire des confusions a mettre sous les yeux d'un operateur.
    """
    interfaces: set[str] = set()
    for row in rows:
        if parse_flag(row.get("disabled")):
            continue
        nom = _texte(row.get("interface"))
        if nom:
            interfaces.add(nom)
    return interfaces


def sightings_from_arp(
    arp_rows: Sequence[dict[str, Any]],
    vlan_rows: Sequence[dict[str, Any]],
    pppoe_rows: Sequence[dict[str, Any]],
    *,
    router_name: str,
    pop_name: str,
) -> list[VlanSighting]:
    """Ne garde que les entrees ARP qui parlent VRAIMENT d'un client potentiel.

    Quatre filtres, chacun pour une raison precise :

    - l'interface doit etre une VLAN de ``/interface/vlan``. Le reste de la
      table ARP est du transit, de l'infrastructure ou du management ;
    - cette VLAN ne doit pas heberger de serveur PPPoE, sinon on redecouvrirait
      des abonnes qui ont deja une identite ;
    - une MAC doit etre presente. Une entree sans MAC signifie qu'on a CHERCHE
      cette adresse, pas qu'elle a repondu : ce n'est pas une presence ;
    - l'entree ne doit etre ni desactivee ni invalide.

    Les entrees statiques sont conservees volontairement : beaucoup d'operateurs
    figent le couple IP/MAC de leurs clients a IP fixe, et les ecarter
    reviendrait a rater exactement la population qu'on cherche.
    """
    vlans = vlan_index(vlan_rows)
    exclues = pppoe_interfaces(pppoe_rows)

    vues: dict[str, VlanSighting] = {}
    for row in arp_rows:
        if parse_flag(row.get("disabled")) or parse_flag(row.get("invalid")):
            continue
        interface = _texte(row.get("interface"))
        if interface not in vlans or interface in exclues:
            continue
        adresse = _adresse_exploitable(row.get("address"))
        if adresse is None:
            continue
        mac = _normalise_mac(row.get("mac-address") or row.get("mac_address"))
        if mac is None:
            continue
        # Une meme adresse peut apparaitre deux fois (entree statique doublee
        # d'une dynamique) : la derniere lue suffit, elles designent le meme hote.
        vues[adresse] = VlanSighting(
            router_name=router_name,
            pop_name=pop_name,
            address=adresse,
            vlan_interface=interface,
            mac=mac,
            vlan_id=vlans.get(interface),
        )
    return sorted(vues.values(), key=lambda v: ipaddress.ip_address(v.address))

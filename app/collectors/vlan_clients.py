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
from dataclasses import dataclass
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


def disabled_vlans(rows: Sequence[dict[str, Any]]) -> set[str]:
    """Noms des VLAN declarees mais desactivees.

    Les distinguer des interfaces inconnues change tout pour l'exploitant : une
    VLAN absente demande de chercher ou est l'adressage, une VLAN desactivee
    demande juste de la reactiver.
    """
    return {
        _texte(row.get("name"))
        for row in rows
        if parse_flag(row.get("disabled")) and _texte(row.get("name"))
    }


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


# Motifs de rejet. Ils sont rendus tels quels a l'interface : chacun doit dire
# a l'exploitant ce qu'il peut VERIFIER sur son routeur, pas seulement que la
# ligne a ete ecartee.
GARDE = "retenu"
REJET_DESACTIVEE = "entree ARP desactivee"
REJET_INVALIDE = "entree ARP invalide"
REJET_HORS_VLAN = "interface absente de /interface/vlan"
REJET_VLAN_DESACTIVEE = "VLAN declaree mais desactivee dans la configuration"
REJET_PPPOE = "cette interface heberge un serveur PPPoE"
REJET_ADRESSE = "adresse inexploitable"
REJET_SANS_MAC = "aucune MAC : l'adresse a ete cherchee, elle n'a pas repondu"


@dataclass(slots=True)
class ArpVerdict:
    """Le sort d'une entree ARP, et POURQUOI.

    Exister separement du resultat permet de repondre a la seule question qui
    compte quand un client manque : "qu'est-ce qui l'a ecarte ?". Sans cela,
    l'exploitant n'a qu'une liste vide et aucune prise dessus.
    """

    address: str
    mac: str | None
    interface: str
    kept: bool
    reason: str
    vlan_id: int | None = None


def judge_arp_rows(
    arp_rows: Sequence[dict[str, Any]],
    vlan_rows: Sequence[dict[str, Any]],
    pppoe_rows: Sequence[dict[str, Any]],
) -> list[ArpVerdict]:
    """Rend un verdict motive pour CHAQUE entree ARP.

    C'est l'unique chemin de decision : ``sightings_from_arp`` se contente de
    filtrer ce que cette fonction a retenu. Deux logiques separees auraient
    fini par diverger, et un diagnostic qui ment sur ce que fait le code est
    pire que pas de diagnostic du tout.
    """
    vlans = vlan_index(vlan_rows)
    exclues = pppoe_interfaces(pppoe_rows)
    eteintes = disabled_vlans(vlan_rows)

    verdicts: list[ArpVerdict] = []
    for row in arp_rows:
        interface = _texte(row.get("interface"))
        adresse = _adresse_exploitable(row.get("address"))
        mac = _normalise_mac(row.get("mac-address") or row.get("mac_address"))

        if parse_flag(row.get("disabled")):
            motif, garde = REJET_DESACTIVEE, False
        elif parse_flag(row.get("invalid")):
            motif, garde = REJET_INVALIDE, False
        elif interface in exclues:
            motif, garde = REJET_PPPOE, False
        elif interface in eteintes:
            motif, garde = REJET_VLAN_DESACTIVEE, False
        elif interface not in vlans:
            motif, garde = REJET_HORS_VLAN, False
        elif adresse is None:
            motif, garde = REJET_ADRESSE, False
        elif mac is None:
            motif, garde = REJET_SANS_MAC, False
        else:
            motif, garde = GARDE, True

        verdicts.append(
            ArpVerdict(
                # L'adresse brute est conservee quand elle est illisible : c'est
                # elle que l'exploitant retrouvera dans son /ip/arp print.
                address=adresse or (_texte(row.get("address")) or "?"),
                mac=mac,
                interface=interface,
                kept=garde,
                reason=motif,
                vlan_id=vlans.get(interface),
            )
        )
    return verdicts


def explain_arp(
    arp_rows: Sequence[dict[str, Any]],
    vlan_rows: Sequence[dict[str, Any]],
    pppoe_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Synthese lisible : ce qui a ete retenu, et ce qui a ecarte le reste.

    ``interfaces_hors_vlan`` est le champ qui repond le plus souvent a la
    question. Si une interface y apparait avec beaucoup d'adresses -- un pont,
    typiquement -- c'est que l'adressage client est pose sur elle et non sur une
    interface de ``/interface/vlan`` : la detection, telle qu'elle est ecrite,
    ne peut pas la voir.
    """
    verdicts = judge_arp_rows(arp_rows, vlan_rows, pppoe_rows)
    par_motif: dict[str, int] = {}
    hors_vlan: dict[str, int] = {}
    for v in verdicts:
        par_motif[v.reason] = par_motif.get(v.reason, 0) + 1
        if v.reason == REJET_HORS_VLAN and v.interface:
            hors_vlan[v.interface] = hors_vlan.get(v.interface, 0) + 1
    return {
        "arp_rows": len(verdicts),
        "kept": sum(1 for v in verdicts if v.kept),
        "by_reason": dict(sorted(par_motif.items(), key=lambda kv: -kv[1])),
        # Interfaces vues dans /ip/arp mais absentes de /interface/vlan, les
        # plus bavardes d'abord.
        "interfaces_hors_vlan": dict(sorted(hors_vlan.items(), key=lambda kv: -kv[1])),
        "vlans_declares": sorted(vlan_index(vlan_rows)),
        "interfaces_pppoe": sorted(pppoe_interfaces(pppoe_rows)),
        "verdicts": [
            {
                "address": v.address,
                "mac": v.mac,
                "interface": v.interface,
                "vlan_id": v.vlan_id,
                "kept": v.kept,
                "reason": v.reason,
            }
            for v in verdicts
        ],
    }


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

    LIMITE CONNUE. Le premier filtre suppose que l'adressage du client est pose
    sur une interface de ``/interface/vlan``. Si le routeur porte l'adresse sur
    un PONT en filtrage VLAN, la table ARP nomme ce pont et non une VLAN : le
    client est alors invisible ici. ``explain_arp`` le dit explicitement.
    """
    vues: dict[str, VlanSighting] = {}
    vlans = vlan_index(vlan_rows)
    for verdict in judge_arp_rows(arp_rows, vlan_rows, pppoe_rows):
        if not verdict.kept:
            continue
        # Une meme adresse peut apparaitre deux fois (entree statique doublee
        # d'une dynamique) : la derniere lue suffit, elles designent le meme hote.
        vues[verdict.address] = VlanSighting(
            router_name=router_name,
            pop_name=pop_name,
            address=verdict.address,
            vlan_interface=verdict.interface,
            mac=verdict.mac,
            vlan_id=vlans.get(verdict.interface),
        )
    return sorted(vues.values(), key=lambda v: ipaddress.ip_address(v.address))

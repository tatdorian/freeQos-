"""Les radios Ubiquiti DECOUVERTES deviennent des antennes interrogees, seules.

POURQUOI
--------
La capacite d'un backhaul radio varie (pluie, interference, alignement) : le
controleur ne la connait que s'il INTERROGE l'antenne. Demander a l'exploitant
de declarer chaque radio a la main, c'est garantir qu'une partie du parc ne
l'est jamais. Or la decouverte les voit deja : voisins MNDP/LLDP de plateforme
Ubiquiti, avec leur adresse et leur MAC.

Des que des identifiants airOS communs au parc sont fournis (``AIROS_USERNAME``
/ ``AIROS_PASSWORD``, poses par l'installation), chaque radio decouverte avec
une adresse est ajoutee aux antennes, rattachee au PoP du routeur qui la voit.
Rien n'est jamais retire ni modifie : une antenne declaree ou corrigee a la main
reste telle quelle, et une radio deja connue (meme adresse, meme MAC ou meme
nom) n'est pas dupliquee.

Fonction PURE pour le choix ; l'ecriture est faite par l'appelant.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from typing import Any

from app.collectors.topology import normalize_mac

#: Natures de cases qui designent une radio.
NATURES_RADIO = {"radio", "sector"}


def _attributs(noeud: dict[str, Any]) -> dict[str, Any]:
    brut = noeud.get("attributes")
    if isinstance(brut, dict):
        return brut
    if isinstance(brut, str) and brut:
        try:
            valeur = json.loads(brut)
        except ValueError:
            return {}
        return valeur if isinstance(valeur, dict) else {}
    return {}


def _adresse(noeud: dict[str, Any]) -> str | None:
    for brut in (noeud.get("address"), *(_attributs(noeud).get("addresses") or [])):
        texte = str(brut or "").split("/")[0].strip()
        if not texte:
            continue
        try:
            ip = ipaddress.ip_address(texte)
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_loopback and not ip.is_link_local:
            return texte
    return None


def antennas_to_enroll(
    nodes: Iterable[dict[str, Any]],
    existing: Iterable[dict[str, Any]],
    *,
    pop_of_router: dict[str, str],
    username: str,
) -> list[dict[str, Any]]:
    """Les radios decouvertes a ajouter aux antennes (charges utiles de creation)."""
    connues_hotes: set[str] = set()
    connues_macs: set[str] = set()
    connus_noms: set[str] = set()
    for a in existing:
        connues_hotes.add(str(a.get("host") or "").strip())
        mac = normalize_mac(a.get("device_key"))
        if mac:
            connues_macs.add(mac)
        connus_noms.add(str(a.get("name") or "").strip().lower())

    nouvelles: list[dict[str, Any]] = []
    for noeud in nodes:
        if str(noeud.get("kind") or "").lower() not in NATURES_RADIO:
            continue
        if _attributs(noeud).get("managed"):
            continue  # un routeur gere n'est pas une antenne a interroger
        hote = _adresse(noeud)
        if hote is None:
            continue
        mac = normalize_mac(noeud.get("mac"))
        nom = str(noeud.get("name") or hote).strip() or hote
        if hote in connues_hotes or (mac and mac in connues_macs):
            continue
        if nom.lower() in connus_noms:
            nom = f"{nom} ({hote})"
            if nom.lower() in connus_noms:
                continue
        routeur = str(noeud.get("router_name") or "")
        pop = pop_of_router.get(routeur)
        if not pop:
            continue  # sans PoP, sa capacite ne se rattacherait a rien
        nouvelles.append(
            {
                "name": nom[:120],
                "pop_name": pop,
                "host": hote,
                "username": username,
                "device_key": mac,
                "verify_tls": False,
                "enabled": True,
            }
        )
        connues_hotes.add(hote)
        if mac:
            connues_macs.add(mac)
        connus_noms.add(nom.lower())
    return nouvelles

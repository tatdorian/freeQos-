"""Objets de domaine echanges entre collecteurs, service de collecte et writer.

Ce sont des dataclasses volontairement simples : elles ne dependent ni de la base
ni de FastAPI, ce qui permet de tester la logique metier sans infrastructure.

CONVENTION DE SENS (source d'erreur numero un sur ce type de systeme) :
tous les compteurs et debits abonnes sont exprimes DU POINT DE VUE DU ROUTEUR.
  - rx = le routeur recoit depuis l'abonne  -> c'est l'UPLOAD de l'abonne
  - tx = le routeur emet vers l'abonne      -> c'est le DOWNLOAD de l'abonne

Meme convention sur une interface physique, l'"abonne" devenant l'equipement
d'en face :
  - rx = le routeur recoit DEPUIS le voisin
  - tx = le routeur emet VERS le voisin
Selon que le voisin soit en amont (passerelle) ou en aval (secteur radio), le
meme tx est le trafic montant ou descendant du reseau. On ne devine donc pas :
on stocke le sens brut et l'interface nomme l'equipement d'en face.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# -----------------------------------------------------------------------------
# Nature d'un abonne
#
# Les deux types coexistent dans la meme table et suivent le meme chemin de
# planification : seule la maniere de connaitre leur adresse change.
#
#   pppoe  : decouvert dans /ppp/active. L'adresse vient de la session en cours
#            et peut changer a chaque reconnexion ; la file la suit.
#   static : declare a la main dans l'inventaire static_clients. L'adresse est
#            fixe, et peut etre un sous-reseau entier (client pro en /29).
#
# Le discriminant voyage jusqu'a l'enforcement parce qu'il a une consequence
# CONCRETE : une session PPPoE est toujours ramenee a un /32, un client statique
# garde le prefixe qu'on lui a declare.
# -----------------------------------------------------------------------------
KIND_PPPOE = "pppoe"
KIND_STATIC = "static"
SUBSCRIBER_KINDS = (KIND_PPPOE, KIND_STATIC)


@dataclass(slots=True)
class StaticClient:
    """Un client a IP fixe, tel que l'operateur l'a DECLARE.

    Il n'y a pas de source automatique derriere cet objet, et c'est assume :
    aucune session a observer, aucun attribut RADIUS a lire. L'inventaire est
    la verite, au meme titre que l'inventaire de routeurs.
    """

    reference: str
    pop_name: str
    address: str
    label: str | None = None
    vlan: int | None = None
    sector_key: str | None = None
    plan_down_mbps: float | None = None
    plan_up_mbps: float | None = None
    enabled: bool = True
    note: str | None = None

    @property
    def display_name(self) -> str:
        return self.label or self.reference


@dataclass(slots=True)
class PppoeSession:
    """Une session PPPoE active, apres correlation /ppp/active + /interface."""

    login: str
    router_name: str
    pop_name: str
    address: str | None = None
    caller_id: str | None = None
    service: str | None = None
    session_id: str | None = None
    uptime_s: int | None = None
    interface: str | None = None
    # Compteurs cumulatifs de l'interface PPPoE dynamique (None si non correlee).
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclass(slots=True)
class SubscriberSample:
    """Echantillon abonne pret a etre ecrit dans ``subscriber_metrics``."""

    ts: datetime
    login: str
    router_name: str
    pop_name: str
    address: str | None = None
    uptime_s: int | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    # None tant qu'on n'a pas deux mesures exploitables (premier passage, reconnexion).
    rx_bps: float | None = None
    tx_bps: float | None = None
    rtt_ms: float | None = None  # phase 3 (latence sous charge)


@dataclass(slots=True)
class InterfaceSample:
    """Compteurs et debit d'UNE interface physique de routeur.

    C'est le seul endroit ou le debit d'un lien peut reellement etre mesure :
    RouterOS compte les octets par interface, pas par adjacence. Un lien de la
    topologie herite donc du debit de l'interface qui le porte -- et quand
    plusieurs voisins sont vus sur le meme port (un switch entre les deux), le
    chiffre est celui du port, partage entre eux. L'interface le dit plutot que
    de faire croire a une mesure par voisin.
    """

    ts: datetime
    router_name: str
    interface: str
    kind: str | None = None  # type RouterOS : ether, vlan, bridge, wlan...
    running: bool | None = None
    # Debit negocie du port, quand c'est un ethernet. Sert de plafond aux jauges.
    capacity_mbps: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    # None tant qu'on n'a pas deux mesures exploitables, comme pour les abonnes.
    rx_bps: float | None = None
    tx_bps: float | None = None


@dataclass(slots=True)
class BackhaulSample:
    """Capacite instantanee d'un lien radio, lue chez le fournisseur (jamais pilotee)."""

    ts: datetime
    device_id: str
    capacity_mbps: float | None = None
    capacity_down_mbps: float | None = None
    capacity_up_mbps: float | None = None
    signal_dbm: float | None = None
    airtime_pct: float | None = None
    mcs_down: str | None = None
    mcs_up: str | None = None
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    """Plan commercial d'un abonne, en Mbps.

    Les deux sens sont optionnels : beaucoup d'offres ne plafonnent que la
    descente, et un client a IP fixe peut n'avoir qu'un debit declare. Un sens
    a None veut dire "pas de plafond contractuel connu", ce qui n'est pas la
    meme chose que zero -- zero serait une coupure.
    """

    down_mbps: float | None
    up_mbps: float | None
    source: str = "unknown"


@dataclass(slots=True)
class RunResult:
    """Compte rendu d'une execution de job, expose par /health/ready et /status."""

    job: str
    started_at: datetime
    duration_s: float
    ok: bool
    items: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def error_text(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None

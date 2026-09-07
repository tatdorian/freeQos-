"""Objets de domaine echanges entre collecteurs, service de collecte et writer.

Ce sont des dataclasses volontairement simples : elles ne dependent ni de la base
ni de FastAPI, ce qui permet de tester la logique metier sans infrastructure.

CONVENTION DE SENS (source d'erreur numero un sur ce type de systeme) :
tous les compteurs et debits abonnes sont exprimes DU POINT DE VUE DU ROUTEUR.
  - rx = le routeur recoit depuis l'abonne  -> c'est l'UPLOAD de l'abonne
  - tx = le routeur emet vers l'abonne      -> c'est le DOWNLOAD de l'abonne
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
    """Plan commercial d'un abonne, en Mbps."""

    down_mbps: float
    up_mbps: float
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

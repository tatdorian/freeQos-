"""Les URGENCES : des debits de crise, prepares a l'avance, lances d'un bouton.

Une urgence a un NOM (« Backhaul down », « Power cut »), un DEBIT et des
CLIENTS. Le debit est, au choix :

- un POURCENTAGE du forfait de chaque client (50 % : la moitie de son plan) ;
- une LIMITE FIXE (8 / 3 Mbps), qui ne releve jamais un client au-dessus de
  son plan : c'est un plafond ;
- la bande passante d'urgence PROPRE a chaque client (saisie sur sa fiche) ;
  un client qui n'en a pas garde sa vitesse.

Ses clients : tout le monde, des sites, ou des clients choisis.

Tant qu'elle est ACTIVE (bouton « Activate », jusqu'a « Stop »), ce debit
passe devant le forfait, le mode et tout scheduler ; seul un boost reste
au-dessus. Plusieurs urgences peuvent exister, et plusieurs tourner : sur un
client vise par deux urgences actives, c'est la DERNIERE activee qui vaut.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.vlan_sites import site_key

KIND_PERCENT = "percent"
KIND_FIXED = "fixed"
KIND_CLIENT = "client"
KINDS = (KIND_PERCENT, KIND_FIXED, KIND_CLIENT)

SCOPE_ALL = "all"
SCOPE_CLIENTS = "clients"
SCOPE_POPS = "pops"
SCOPES = (SCOPE_ALL, SCOPE_CLIENTS, SCOPE_POPS)

#: Sous 64 kbps, une connexion ne s'ouvre meme plus : refuse a la saisie.
MIN_RATE_MBPS = 0.064


class InvalidEmergencyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Emergency:
    id: int
    name: str
    kind: str
    percent: float | None = None
    down_mbps: float | None = None
    up_mbps: float | None = None
    scope: str = SCOPE_ALL
    targets: frozenset[str] = field(default_factory=frozenset)
    active: bool = False
    active_since: datetime | None = None

    def governs(self, login: str, site: str | None) -> bool:
        if self.scope == SCOPE_ALL:
            return True
        if self.scope == SCOPE_CLIENTS:
            return login in self.targets
        return bool(site) and site_key(str(site)) in {site_key(t) for t in self.targets}

    def rate(
        self,
        plan_down: float | None,
        plan_up: float | None,
        own: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        """Le debit d'urgence de CE client, ou None (il garde sa vitesse)."""
        if self.kind == KIND_PERCENT:
            if not plan_down or not plan_up or not self.percent:
                return None
            facteur = self.percent / 100.0
            return round(plan_down * facteur, 3), round(plan_up * facteur, 3)
        if self.kind == KIND_FIXED:
            if not self.down_mbps or not self.up_mbps:
                return None
            # Un plafond : jamais au-dessus du plan du client.
            bas = min(self.down_mbps, plan_down) if plan_down else self.down_mbps
            haut = min(self.up_mbps, plan_up) if plan_up else self.up_mbps
            return bas, haut
        return own

    def describe(self) -> str:
        if self.kind == KIND_PERCENT:
            return f"{self.percent:g} % of the plan"
        if self.kind == KIND_FIXED:
            return f"limit {self.down_mbps:g} / {self.up_mbps:g} Mbps"
        return "each client's own emergency bandwidth"


@dataclass(frozen=True, slots=True)
class EmergencyRate:
    down_mbps: float
    up_mbps: float
    emergency_id: int
    name: str


def _nombre(valeur: Any, nom: str, *, minimum: float, maximum: float) -> float:
    try:
        n = float(valeur)
    except (TypeError, ValueError) as exc:
        raise InvalidEmergencyError(f"{nom}: a number is needed") from exc
    if not minimum <= n <= maximum:
        raise InvalidEmergencyError(f"{nom}: between {minimum:g} and {maximum:g}")
    return n


def check(payload: dict[str, Any]) -> dict[str, Any]:
    """Valide une urgence saisie ; rend sa forme canonique."""
    nom = str(payload.get("name") or "").strip()
    if not nom:
        raise InvalidEmergencyError("an emergency needs a name")
    genre = payload.get("kind") or KIND_PERCENT
    if genre not in KINDS:
        raise InvalidEmergencyError("kind: 'percent', 'fixed' or 'client'")
    sortie: dict[str, Any] = {
        "name": nom[:64],
        "kind": genre,
        "percent": None,
        "down_mbps": None,
        "up_mbps": None,
    }
    if genre == KIND_PERCENT:
        sortie["percent"] = _nombre(payload.get("percent"), "percent", minimum=1, maximum=100)
    elif genre == KIND_FIXED:
        for cle in ("down_mbps", "up_mbps"):
            sortie[cle] = _nombre(payload.get(cle), cle, minimum=MIN_RATE_MBPS, maximum=100_000)
    portee = payload.get("scope") or SCOPE_ALL
    if portee not in SCOPES:
        raise InvalidEmergencyError("scope: 'all', 'clients' or 'pops'")
    cibles = sorted({str(t).strip() for t in (payload.get("targets") or []) if str(t).strip()})
    if portee != SCOPE_ALL and not cibles:
        raise InvalidEmergencyError(
            "choose the clients or the sites of this emergency, or 'Everyone'"
        )
    sortie["scope"] = portee
    sortie["targets"] = [] if portee == SCOPE_ALL else cibles
    sortie["note"] = (str(payload.get("note") or "").strip() or None) if "note" in payload else None
    return sortie


def to_model(row: dict[str, Any]) -> Emergency:
    return Emergency(
        id=int(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        percent=row.get("percent"),
        down_mbps=row.get("down_mbps"),
        up_mbps=row.get("up_mbps"),
        scope=str(row.get("scope") or SCOPE_ALL),
        targets=frozenset(str(t) for t in (row.get("targets") or [])),
        active=bool(row.get("active")),
        active_since=row.get("active_since"),
    )


class EmergencyBook:
    """Les urgences ACTIVES, partagees par le planificateur et l'affichage."""

    def __init__(self) -> None:
        self.active: list[Emergency] = []

    def replace(self, emergencies: Iterable[Emergency]) -> None:
        actives = [e for e in emergencies if e.active]
        # La derniere activee d'abord : c'est elle qui vaut sur un client commun.
        actives.sort(key=lambda e: (e.active_since is not None, e.active_since), reverse=True)
        self.active = actives

    def for_client(
        self,
        *,
        login: str,
        site: str | None,
        plan_down: float | None,
        plan_up: float | None,
        own: tuple[float, float] | None = None,
    ) -> EmergencyRate | None:
        for urgence in self.active:
            if not urgence.governs(login, site):
                continue
            debit = urgence.rate(plan_down, plan_up, own)
            if debit is not None:
                return EmergencyRate(debit[0], debit[1], urgence.id, urgence.name)
        return None


#: Le carnet partage (comme celui des schedulers).
BOOK = EmergencyBook()

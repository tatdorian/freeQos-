"""Objets de l'enforcement : etat desire, actions, plan.

Aucune dependance a RouterOS ni au reseau : ce sont des donnees. C'est ce qui
permet d'afficher un plan a l'operateur avant d'envoyer quoi que ce soit.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Literal

# Marque de propriete. Toute ligne qui ne la porte pas appartient a quelqu'un
# d'autre (operateur, RADIUS, script tiers) et ne doit JAMAIS etre modifiee.
MANAGED_COMMENT = "freeqos:managed"

PREFIX = "freeqos-"

ActionVerb = Literal["add", "set", "remove"]


def slugify(value: str) -> str:
    """Nom RouterOS sur : lettres, chiffres, tirets, point et deux-points exclus.

    Les noms de files servent de cles de reconciliation ; un caractere exotique
    dans un login PPPoE ne doit pas casser le rapprochement.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:48] or "sans-nom"


def address_target(value: Any) -> str | None:
    """Adresse d'abonne au format attendu par ``/queue/simple target``.

    Sortie CANONIQUE (``10.20.0.10/32``, ``2001:db8::1/128``) et non l'adresse
    nue, pour une raison de reconciliation : RouterOS reecrit toujours la cible
    avec son prefixe. Ecrire ``10.20.0.10`` puis relire ``10.20.0.10/32``
    produirait un ecart a chaque cycle, donc un ``set`` inutile a chaque plan.

    Accepte ce que renvoient les deux sources : une chaine issue de
    ``/ppp/active`` et un objet ``ipaddress`` issu de la colonne INET.
    """
    if value is None:
        return None
    texte = str(value).strip()
    if not texte:
        return None
    # Une session PPPoE porte une seule adresse : un prefixe plus large serait
    # une erreur de saisie, et shaperait les voisins de l'abonne.
    try:
        interface = ipaddress.ip_interface(texte)
    except ValueError:
        return None
    adresse = interface.ip
    if adresse.is_unspecified or adresse.is_loopback:
        return None
    return f"{adresse}/{adresse.max_prefixlen}"


def format_rate(mbps: float | None) -> str:
    """Debit au format RouterOS.

    On sort des bits par seconde entiers plutot que ``12.5M`` : RouterOS
    n'accepte pas toujours les decimales, et un entier est sans ambiguite.
    """
    if mbps is None or mbps <= 0:
        return "0"
    return str(int(round(mbps * 1_000_000)))


_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def quote(value: Any) -> str:
    """Echappe une valeur pour la ligne de commande RouterOS.

    Genereux volontairement : la commande affichee dans l'interface doit pouvoir
    etre collee telle quelle dans un terminal. Un nom d'interface PPPoE dynamique
    (``<pppoe-dupont>``) doit donc etre entre guillemets.
    """
    text = str(value)
    if _SAFE_UNQUOTED.match(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(slots=True)
class QueueTypeSpec:
    """Un type de file CAKE (``/queue/type``)."""

    name: str
    kind: str = "cake"
    # Cadrage PPPoE : l'entete ajoute par l'encapsulation doit etre compte, sinon
    # le shaper laisse passer plus que le lien ne peut porter.
    overhead: int | None = None
    mpu: int | None = None
    rtt_ms: int | None = None
    diffserv: str | None = None
    flowmode: str | None = None
    nat: bool | None = None
    ack_filter: str | None = None
    wash: bool | None = None

    def routeros_fields(self) -> dict[str, str]:
        champs: dict[str, str] = {"name": self.name, "kind": self.kind}
        optionnels = {
            "cake-overhead": self.overhead,
            "cake-mpu": self.mpu,
            "cake-rtt": f"{self.rtt_ms}ms" if self.rtt_ms is not None else None,
            "cake-diffserv": self.diffserv,
            "cake-flowmode": self.flowmode,
            "cake-nat": _bool(self.nat),
            "cake-ack-filter": self.ack_filter,
            "cake-wash": _bool(self.wash),
        }
        champs.update({k: str(v) for k, v in optionnels.items() if v is not None})
        return champs


@dataclass(slots=True)
class QueueSpec:
    """Une file simple (``/queue/simple``)."""

    name: str
    target: str
    max_up_mbps: float | None
    max_down_mbps: float | None
    parent: str | None = None
    queue_up: str | None = None
    queue_down: str | None = None
    comment: str = MANAGED_COMMENT
    disabled: bool = False
    # Ordre d'evaluation : les parents doivent preceder leurs enfants.
    order: int = 0

    @property
    def max_limit(self) -> str:
        return f"{format_rate(self.max_up_mbps)}/{format_rate(self.max_down_mbps)}"

    @property
    def queue(self) -> str | None:
        if self.queue_up and self.queue_down:
            return f"{self.queue_up}/{self.queue_down}"
        return None

    def routeros_fields(self) -> dict[str, str]:
        champs = {
            "name": self.name,
            "target": self.target,
            "max-limit": self.max_limit,
            "comment": self.comment,
        }
        if self.parent:
            champs["parent"] = self.parent
        if self.queue:
            champs["queue"] = self.queue
        if self.disabled:
            champs["disabled"] = "yes"
        return champs


@dataclass(slots=True)
class PlanAction:
    """Une commande a envoyer. Portee par le plan, jamais executee ici."""

    verb: ActionVerb
    path: str  # "/queue/simple", "/queue/type"
    fields: dict[str, str] = field(default_factory=dict)
    target_id: str | None = None  # .id RouterOS, pour set et remove
    # Nom lisible de la cible. Un 'set' ne renvoie pas le champ name (on ne le
    # reecrit pas), il faut donc le porter a part pour l'affichage.
    name: str = ""
    reason: str = ""
    # Ce qui change reellement, pour l'affichage : {champ: (avant, apres)}
    changes: dict[str, tuple[str | None, str]] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """Rendu CLI RouterOS, tel qu'il sera envoye. C'est ce que voit
        l'operateur avant d'appliquer."""
        morceaux = [f"{self.path}/{self.verb}"]
        if self.target_id:
            morceaux.append(f".id={self.target_id}")
        for cle, valeur in self.fields.items():
            morceaux.append(f"{cle}={quote(valeur)}")
        return " ".join(morceaux)

    def summary(self) -> str:
        nom = self.name or self.fields.get("name") or self.target_id or "?"
        if self.verb == "add":
            return f"creer {nom}"
        if self.verb == "remove":
            return f"supprimer {nom}"
        details = ", ".join(
            f"{k} {avant or '-'} -> {apres}" for k, (avant, apres) in self.changes.items()
        )
        return f"modifier {nom} ({details})" if details else f"modifier {nom}"


@dataclass(slots=True)
class PlanSkip:
    """Un abonne volontairement laisse de cote, et pourquoi.

    Sans cette trace, un abonne absent du plan est indiscernable d'un abonne
    correctement shape : l'exploitant chercherait la panne au mauvais endroit.
    """

    login: str
    reason: str


@dataclass(slots=True)
class PlanConflict:
    """Un nom desire est deja pris par une ligne qui ne nous appartient pas."""

    name: str
    path: str
    detail: str


@dataclass(slots=True)
class Plan:
    """Ce qui serait fait sur un routeur. Sans effet tant qu'il n'est pas applique."""

    router_name: str
    actions: list[PlanAction] = field(default_factory=list)
    conflicts: list[PlanConflict] = field(default_factory=list)
    skipped: list[PlanSkip] = field(default_factory=list)
    unchanged: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def counts(self) -> dict[str, int]:
        resultat = {"add": 0, "set": 0, "remove": 0}
        for action in self.actions:
            resultat[action.verb] += 1
        return resultat

    def to_dict(self) -> dict[str, Any]:
        return {
            "router": self.router_name,
            "counts": self.counts(),
            "unchanged": self.unchanged,
            "actions": [
                {
                    "verb": action.verb,
                    "path": action.path,
                    "name": action.name or action.fields.get("name", ""),
                    "command": action.command,
                    "summary": action.summary(),
                    "reason": action.reason,
                    "changes": {k: list(v) for k, v in action.changes.items()},
                }
                for action in self.actions
            ],
            "conflicts": [
                {"name": c.name, "path": c.path, "detail": c.detail} for c in self.conflicts
            ],
            "skipped": [{"login": s.login, "reason": s.reason} for s in self.skipped],
        }


def _bool(value: bool | None) -> str | None:
    if value is None:
        return None
    return "yes" if value else "no"

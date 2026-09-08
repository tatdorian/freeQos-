"""Etat desire, comparaison avec l'existant, plan d'action.

Entierement PUR : rien ici n'ouvre de connexion. On donne l'inventaire lu sur le
routeur et la politique voulue, on obtient une liste de commandes. C'est ce qui
rend l'enforcement testable a 100 % et affichable avant execution.

PRINCIPE DE SHAPING
-------------------
Preseem et LibreQoS reposent tous deux sur la meme idee : pour qu'une gestion de
file fonctionne, il faut que le goulot soit CHEZ NOUS, pas dans la radio. On
shape donc legerement SOUS la capacite reelle du lien (facteur de securite), afin
que la file se forme dans CAKE, ou on la controle, plutot que dans un buffer
d'equipement qu'on ne maitrise pas.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.enforcement.models import (
    MANAGED_COMMENT,
    PREFIX,
    Plan,
    PlanAction,
    PlanConflict,
    QueueSpec,
    QueueTypeSpec,
    slugify,
)

logger = logging.getLogger(__name__)

QUEUE_TYPE_UP = f"{PREFIX}cake-up"
QUEUE_TYPE_DOWN = f"{PREFIX}cake-down"


@dataclass(slots=True)
class SubscriberTarget:
    """Un abonne a shaper.

    Trois niveaux de debit, du plus fort au plus faible :
      1. le BOOST, tant qu'il n'a pas expire ;
      2. la surcharge manuelle permanente ;
      3. le plan commercial venu de RADIUS.
    """

    login: str
    interface: str
    plan_down_mbps: float | None
    plan_up_mbps: float | None
    parent: str | None = None
    override_down_mbps: float | None = None
    override_up_mbps: float | None = None
    boost_down_mbps: float | None = None
    boost_up_mbps: float | None = None
    boost_expires_at: datetime | None = None
    enabled: bool = True

    @property
    def queue_name(self) -> str:
        return f"{PREFIX}{slugify(self.login)}"

    def boost_active(self, now: datetime | None = None) -> bool:
        """Un boost sans echeance n'existe pas : ce serait une surcharge."""
        if self.boost_expires_at is None:
            return False
        if self.boost_down_mbps is None and self.boost_up_mbps is None:
            return False
        return self.boost_expires_at > (now or datetime.now(tz=UTC))

    def effective_down_at(self, now: datetime | None = None) -> float | None:
        if self.boost_active(now) and self.boost_down_mbps:
            return self.boost_down_mbps
        return self.override_down_mbps or self.plan_down_mbps

    def effective_up_at(self, now: datetime | None = None) -> float | None:
        if self.boost_active(now) and self.boost_up_mbps:
            return self.boost_up_mbps
        return self.override_up_mbps or self.plan_up_mbps

    @property
    def effective_down(self) -> float | None:
        return self.effective_down_at()

    @property
    def effective_up(self) -> float | None:
        return self.effective_up_at()


@dataclass(slots=True)
class LinkTarget:
    """Un lien parent : backhaul radio, uplink, ou le PoP entier."""

    name: str
    interface: str
    # Capacite mesuree du moment (radio) ou negociee (ethernet).
    measured_capacity_mbps: float | None = None
    # Surcharge manuelle : l'operateur fixe lui-meme le plafond.
    override_down_mbps: float | None = None
    override_up_mbps: float | None = None
    enabled: bool = True

    @property
    def queue_name(self) -> str:
        return f"{PREFIX}parent-{slugify(self.name)}"


def shaped_capacity(
    measured_mbps: float | None,
    *,
    safety_factor: float,
    floor_mbps: float,
    override_mbps: float | None = None,
) -> float | None:
    """Debit a appliquer sur un lien parent.

    Une surcharge manuelle est prise telle quelle : l'operateur sait ce qu'il
    fait. Sinon on applique le facteur de securite a la capacite mesuree, avec
    un plancher pour qu'un fade profond ne coupe pas le lien a zero.
    """
    if override_mbps is not None and override_mbps > 0:
        return override_mbps
    if measured_mbps is None or measured_mbps <= 0:
        return None
    return max(floor_mbps, measured_mbps * safety_factor)


def desired_queue_types(
    *, overhead: int | None = None, rtt_ms: int | None = None, **extra: Any
) -> list[QueueTypeSpec]:
    """Les deux types CAKE utilises par toutes les files.

    ``overhead`` doit refleter l'encapsulation reelle : PPPoE ajoute 8 octets a
    l'ethernet, davantage avec du VLAN ou du MPLS. Le sous-estimer fait shaper
    au-dessus de la capacite du lien, ce qui annule le benefice de l'AQM.
    """
    commun = {"overhead": overhead, "rtt_ms": rtt_ms, **extra}
    return [
        QueueTypeSpec(name=QUEUE_TYPE_UP, **commun),
        QueueTypeSpec(name=QUEUE_TYPE_DOWN, **commun),
    ]


def desired_state(
    *,
    links: Sequence[LinkTarget],
    subscribers: Sequence[SubscriberTarget],
    safety_factor: float = 0.90,
    floor_mbps: float = 5.0,
    queue_types: Sequence[QueueTypeSpec] | None = None,
    now: datetime | None = None,
) -> tuple[list[QueueTypeSpec], list[QueueSpec]]:
    """Construit l'etat desire complet pour un routeur."""
    types = list(queue_types) if queue_types is not None else desired_queue_types()

    files: list[QueueSpec] = []
    for index, link in enumerate(links):
        if not link.enabled:
            continue
        down = shaped_capacity(
            link.measured_capacity_mbps,
            safety_factor=safety_factor,
            floor_mbps=floor_mbps,
            override_mbps=link.override_down_mbps,
        )
        up = shaped_capacity(
            link.measured_capacity_mbps,
            safety_factor=safety_factor,
            floor_mbps=floor_mbps,
            override_mbps=link.override_up_mbps,
        )
        if down is None and up is None:
            # Sans capacite connue, un parent poserait un plafond arbitraire :
            # mieux vaut ne pas en creer.
            continue
        files.append(
            QueueSpec(
                name=link.queue_name,
                target=link.interface,
                max_up_mbps=up,
                max_down_mbps=down,
                queue_up=QUEUE_TYPE_UP,
                queue_down=QUEUE_TYPE_DOWN,
                order=index,
            )
        )

    parents_connus = {file.name for file in files}
    for subscriber in subscribers:
        if not subscriber.enabled:
            continue
        down = subscriber.effective_down_at(now)
        up = subscriber.effective_up_at(now)
        if down is None and up is None:
            continue
        parent = subscriber.parent if subscriber.parent in parents_connus else None
        files.append(
            QueueSpec(
                name=subscriber.queue_name,
                target=subscriber.interface,
                max_up_mbps=up,
                max_down_mbps=down,
                parent=parent,
                queue_up=QUEUE_TYPE_UP,
                queue_down=QUEUE_TYPE_DOWN,
                order=1000,
            )
        )
    return types, files


def _is_managed(row: dict[str, Any]) -> bool:
    return MANAGED_COMMENT in str(row.get("comment") or "")


def _index_by_name(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("name") or ""): row for row in rows if row.get("name")}


def build_plan(
    router_name: str,
    *,
    desired_types: Sequence[QueueTypeSpec],
    desired_queues: Sequence[QueueSpec],
    actual_types: Sequence[dict[str, Any]],
    actual_queues: Sequence[dict[str, Any]],
    prune: bool = True,
) -> Plan:
    """Compare l'etat desire a l'etat lu sur le routeur.

    Regle absolue : une ligne qui ne porte pas notre commentaire de propriete
    n'est ni modifiee ni supprimee. Si son nom entre en collision avec un nom
    desire, on signale un conflit et on ne touche a rien.
    """
    plan = Plan(router_name=router_name)

    types_existants = _index_by_name(actual_types)
    for spec in desired_types:
        existant = types_existants.get(spec.name)
        champs = spec.routeros_fields()
        if existant is None:
            plan.actions.append(
                PlanAction(
                    verb="add",
                    path="/queue/type",
                    fields=champs,
                    name=spec.name,
                    reason="type CAKE absent",
                )
            )
            continue
        changements = _diff_fields(existant, champs, ignore={"name"})
        if changements:
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path="/queue/type",
                    fields={k: v for k, v in champs.items() if k != "name"},
                    target_id=str(existant.get(".id") or existant.get("id") or ""),
                    name=spec.name,
                    reason="parametres CAKE differents",
                    changes=changements,
                )
            )
        else:
            plan.unchanged += 1

    files_existantes = _index_by_name(actual_queues)
    noms_desires: set[str] = set()

    # Les parents d'abord : RouterOS refuse un enfant dont le parent n'existe pas.
    for spec in sorted(desired_queues, key=lambda q: q.order):
        noms_desires.add(spec.name)
        existante = files_existantes.get(spec.name)
        champs = spec.routeros_fields()

        if existante is None:
            plan.actions.append(
                PlanAction(
                    verb="add",
                    path="/queue/simple",
                    fields=champs,
                    name=spec.name,
                    reason="file absente",
                )
            )
            continue

        if not _is_managed(existante):
            plan.conflicts.append(
                PlanConflict(
                    name=spec.name,
                    path="/queue/simple",
                    detail=(
                        "une file de ce nom existe deja sans le marqueur "
                        f"'{MANAGED_COMMENT}' : elle n'appartient pas au controleur "
                        "et ne sera pas modifiee"
                    ),
                )
            )
            continue

        changements = _diff_fields(existante, champs, ignore={"name", "comment"})
        if changements:
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path="/queue/simple",
                    fields={k: v for k, v in champs.items() if k != "name"},
                    target_id=str(existante.get(".id") or existante.get("id") or ""),
                    name=spec.name,
                    reason="debit ou parent different",
                    changes=changements,
                )
            )
        else:
            plan.unchanged += 1

    if prune:
        # Nos files devenues inutiles (abonne parti, lien retire). On ne touche
        # qu'a ce qui porte notre marque.
        for nom, row in files_existantes.items():
            if nom in noms_desires or not _is_managed(row):
                continue
            if not nom.startswith(PREFIX):
                continue
            plan.actions.append(
                PlanAction(
                    verb="remove",
                    path="/queue/simple",
                    fields={"name": nom},
                    target_id=str(row.get(".id") or row.get("id") or ""),
                    name=nom,
                    reason="plus dans l'etat desire",
                )
            )

    return plan


def _diff_fields(
    actual: dict[str, Any], desired: dict[str, str], *, ignore: set[str]
) -> dict[str, tuple[str | None, str]]:
    """Champs dont la valeur lue differe de la valeur voulue."""
    changements: dict[str, tuple[str | None, str]] = {}
    for cle, voulu in desired.items():
        if cle in ignore:
            continue
        brut = actual.get(cle)
        lu = None if brut is None else str(brut)
        if _normalise(lu) != _normalise(voulu):
            changements[cle] = (lu, voulu)
    return changements


def _normalise(value: str | None) -> str | None:
    """Compare des valeurs RouterOS sans se laisser piéger par la mise en forme.

    RouterOS renvoie volontiers ``20000000/100000000`` la ou on a ecrit ``20M/100M``,
    et ``true``/``yes`` de facon interchangeable selon les versions.
    """
    if value is None:
        return None
    texte = str(value).strip().lower()
    if texte in {"true", "yes"}:
        return "yes"
    if texte in {"false", "no"}:
        return "no"
    # Debits composes : on compare les entiers, pas leur ecriture.
    if "/" in texte:
        morceaux = [_normalise_rate(m) for m in texte.split("/")]
        if all(m is not None for m in morceaux):
            return "/".join(morceaux)  # type: ignore[arg-type]
    seul = _normalise_rate(texte)
    return seul if seul is not None else texte


def _normalise_rate(value: str) -> str | None:
    texte = value.strip().lower()
    multiplicateurs = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
    if texte and texte[-1] in multiplicateurs and texte[:-1].replace(".", "", 1).isdigit():
        return str(int(float(texte[:-1]) * multiplicateurs[texte[-1]]))
    if texte.isdigit():
        return str(int(texte))
    return None

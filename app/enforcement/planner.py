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
    PlanSkip,
    QueueSpec,
    QueueTypeSpec,
    address_target,
    slugify,
)

logger = logging.getLogger(__name__)

QUEUE_TYPE_UP = f"{PREFIX}cake-up"
QUEUE_TYPE_DOWN = f"{PREFIX}cake-down"

# Sur quoi une file abonne est accrochee.
#
# ADRESSE (defaut). ``target=10.20.0.10/32`` designe l'abonne sans ambiguite, et
# surtout le sens y est celui du client : ``max-limit=montant/descendant`` ou le
# montant est ce qui VIENT de la cible. C'est ce que fait tout le monde.
#
# INTERFACE. ``target=<pppoe-alice>`` semble plus direct, mais trois choses le
# rendent inutilisable en pratique :
#   1. l'interface dynamique est recreee a chaque reconnexion, la file reste
#      accrochee a un objet disparu et devient inactive ;
#   2. le sens s'INVERSE -- RouterOS raisonne alors du point de vue de
#      l'interface, donc ``100M/500M`` bride le descendant a 100 et le montant a
#      500, exactement l'inverse du plan vendu ;
#   3. les chevrons du nom dynamique ne passent pas l'API sur RouterOS 7.
# Le mode reste disponible pour un parc qui l'utilise deja, mais ce n'est pas le
# defaut, et ce n'est pas conseille.
TARGET_ADDRESS = "address"
TARGET_INTERFACE = "interface"


# Source du debit finalement applique, exposee a l'interface.
SOURCE_BOOST = "boost"
SOURCE_OVERRIDE = "override"
SOURCE_PLAN = "plan"
SOURCE_NONE = "none"


def effective_rate(
    *,
    plan_mbps: float | None,
    override_mbps: float | None,
    boost_mbps: float | None,
    boost_expires_at: datetime | None,
    now: datetime | None = None,
) -> tuple[float | None, str]:
    """Debit reellement applique, et d'ou il vient.

    UNE SEULE implementation de la regle de priorite. L'interface doit afficher
    exactement ce que le planificateur va ecrire : recoder la regle cote client
    garantirait qu'elles divergent un jour.

    Priorite : boost non expire, puis surcharge permanente, puis plan RADIUS.
    """
    if boost_mbps and boost_expires_at is not None:
        if boost_expires_at > (now or datetime.now(tz=UTC)):
            return boost_mbps, SOURCE_BOOST
    if override_mbps:
        return override_mbps, SOURCE_OVERRIDE
    if plan_mbps:
        return plan_mbps, SOURCE_PLAN
    return None, SOURCE_NONE


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
    # Adresse de la session en cours. C'est elle qui porte la file : sans elle,
    # l'abonne est hors ligne et il n'y a rien a brider.
    address: str | None = None
    parent: str | None = None
    override_down_mbps: float | None = None
    override_up_mbps: float | None = None
    boost_down_mbps: float | None = None
    boost_up_mbps: float | None = None
    boost_expires_at: datetime | None = None
    enabled: bool = True

    @property
    def queue_name(self) -> str:
        """Cle de reconciliation, STABLE : elle ne depend pas de l'adresse.

        C'est ce qui fait qu'un changement d'IP produit un ``set target=...`` sur
        la file existante, et non une suppression suivie d'une creation."""
        return f"{PREFIX}{slugify(self.login)}"

    def queue_target(self, mode: str = TARGET_ADDRESS) -> str | None:
        """Ce que la file doit viser, ou None si l'abonne n'est pas shapable."""
        if mode == TARGET_INTERFACE:
            return self.interface or None
        return address_target(self.address)

    def boost_active(self, now: datetime | None = None) -> bool:
        """Un boost sans echeance n'existe pas : ce serait une surcharge."""
        if self.boost_expires_at is None:
            return False
        if self.boost_down_mbps is None and self.boost_up_mbps is None:
            return False
        return self.boost_expires_at > (now or datetime.now(tz=UTC))

    def effective_down_at(self, now: datetime | None = None) -> float | None:
        return effective_rate(
            plan_mbps=self.plan_down_mbps,
            override_mbps=self.override_down_mbps,
            boost_mbps=self.boost_down_mbps,
            boost_expires_at=self.boost_expires_at,
            now=now,
        )[0]

    def effective_up_at(self, now: datetime | None = None) -> float | None:
        return effective_rate(
            plan_mbps=self.plan_up_mbps,
            override_mbps=self.override_up_mbps,
            boost_mbps=self.boost_up_mbps,
            boost_expires_at=self.boost_expires_at,
            now=now,
        )[0]

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
    target_mode: str = TARGET_ADDRESS,
) -> tuple[list[QueueTypeSpec], list[QueueSpec], list[PlanSkip]]:
    """Construit l'etat desire complet pour un routeur.

    Renvoie aussi les abonnes ECARTES et pourquoi : un abonne qui disparait
    silencieusement du plan est indiscernable d'un abonne correctement shape.
    """
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
    ecartes: list[PlanSkip] = []

    # Deux abonnes qui reclament la MEME adresse : l'un des deux est perime
    # (session fermee dont l'IP a ete reattribuee, doublon de collecte). On ne
    # peut pas savoir lequel, et RouterOS n'appliquerait de toute facon que la
    # premiere file, en silence. On n'ecrit donc ni l'une ni l'autre.
    occurrences: dict[str, list[str]] = {}
    for subscriber in subscribers:
        # Seuls comptent ceux qui produiraient VRAIMENT une file : un abonne
        # sans debit a appliquer ne prend la place de personne.
        if not subscriber.enabled:
            continue
        if subscriber.effective_down_at(now) is None and subscriber.effective_up_at(now) is None:
            continue
        cible = subscriber.queue_target(target_mode)
        if cible is not None:
            occurrences.setdefault(cible, []).append(subscriber.login)
    ambigues = {cible: logins for cible, logins in occurrences.items() if len(logins) > 1}

    for subscriber in subscribers:
        if not subscriber.enabled:
            ecartes.append(PlanSkip(subscriber.login, "shaping desactive pour cet abonne"))
            continue
        down = subscriber.effective_down_at(now)
        up = subscriber.effective_up_at(now)
        if down is None and up is None:
            ecartes.append(
                PlanSkip(subscriber.login, "aucun debit a appliquer (ni plan, ni surcharge)")
            )
            continue

        cible = subscriber.queue_target(target_mode)
        if cible is None:
            # Cas courant et normal : l'abonne n'a pas de session ouverte. Ecrire
            # une file sur sa DERNIERE adresse connue serait dangereux -- entre
            # temps le pool a pu la reattribuer, et on briderait un autre client.
            ecartes.append(
                PlanSkip(
                    subscriber.login,
                    "aucune adresse en cours : abonne hors ligne, rien a brider",
                )
            )
            continue

        if cible in ambigues:
            autres = [x for x in ambigues[cible] if x != subscriber.login]
            ecartes.append(
                PlanSkip(
                    subscriber.login,
                    f"adresse {cible} revendiquee aussi par {', '.join(autres)} : "
                    "impossible de savoir qui est a jour, aucune file ecrite",
                )
            )
            continue

        parent = subscriber.parent if subscriber.parent in parents_connus else None
        files.append(
            QueueSpec(
                name=subscriber.queue_name,
                target=cible,
                max_up_mbps=up,
                max_down_mbps=down,
                parent=parent,
                queue_up=QUEUE_TYPE_UP,
                queue_down=QUEUE_TYPE_DOWN,
                order=1000,
            )
        )
    return types, files, ecartes


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

    # Files tierces indexees par cible. RouterOS n'evalue les files ``simple``
    # qu'en liste : quand deux files visent la meme cible, seule la PREMIERE
    # s'applique, l'autre est ignoree sans le moindre avertissement. Ajouter
    # notre file a la suite d'une file tierce deja postee sur cette adresse
    # produirait donc un debit purement decoratif, qu'on croirait applique.
    etrangeres_par_cible: dict[str, dict[str, Any]] = {}
    for row in actual_queues:
        if _is_managed(row):
            continue
        cible = str(row.get("target") or "").strip()
        if cible:
            etrangeres_par_cible.setdefault(cible, row)

    # Les parents d'abord : RouterOS refuse un enfant dont le parent n'existe pas.
    for spec in sorted(desired_queues, key=lambda q: q.order):
        noms_desires.add(spec.name)
        existante = files_existantes.get(spec.name)
        champs = spec.routeros_fields()

        if existante is None:
            etrangere = etrangeres_par_cible.get(spec.target)
            if etrangere is not None:
                plan.conflicts.append(
                    PlanConflict(
                        name=spec.name,
                        path="/queue/simple",
                        detail=(
                            f"la cible {spec.target} est deja visee par la file tierce "
                            f"'{etrangere.get('name')}' (sans le marqueur "
                            f"'{MANAGED_COMMENT}') : RouterOS n'appliquerait que la "
                            "premiere des deux en silence, donc aucune file n'est ecrite "
                            "tant que le conflit n'est pas resolu a la main"
                        ),
                    )
                )
                continue
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

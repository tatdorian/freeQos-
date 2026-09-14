"""Boucle fermee QoE (phase 4) : decision pure, puis cycle de bout en bout.

Ce que ces tests prouvent, dans l'ordre :

  1. la DECISION seule, sans base ni routeur (``decide_sector``) ;
  2. le CYCLE complet : un score degrade produit un plan avec le resserrage
     attendu, un score normal n'en produit aucun ;
  3. les garde-fous : pas de purge, rien d'ecrit en lecture seule, le plan
     souscrit de l'abonne jamais touche.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.enforcement.models import MANAGED_COMMENT
from app.services.qoe_loop import (
    ACTION_FLOOR,
    ACTION_HOLD,
    ACTION_RELAX,
    ACTION_TIGHTEN,
    ACTION_UNKNOWN,
    SectorState,
    decide_sector,
)
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture

SECTEUR = "mac:DC:9F:DB:11:22:33"
LIEN = "pop-test/ether2/sector"


# =========================================================================
# 1. La decision, isolee
# =========================================================================
def decider(
    scores: list[float],
    *,
    trim: float = 1.0,
    healthy: int = 0,
    min_degraded: int = 2,
    recovery: int = 3,
) -> Any:
    return decide_sector(
        sector_key=SECTEUR,
        link_key=LIEN,
        scores=scores,
        state=SectorState(trim_factor=trim, healthy_cycles=healthy),
        threshold=55.0,
        min_degraded=min_degraded,
        step=0.10,
        floor=0.50,
        recovery_cycles=recovery,
    )


def test_un_secteur_degrade_est_resserre_d_un_cran() -> None:
    verdict = decider([20.0, 30.0, 95.0])

    assert verdict.action == ACTION_TIGHTEN
    assert verdict.trim_after == pytest.approx(0.90)
    assert verdict.changed is True
    assert verdict.degraded == 2 and verdict.scored == 3
    assert "2/3" in verdict.reason


def test_un_seul_abonne_degrade_n_accuse_pas_le_secteur() -> None:
    """Un abonne qui gonfle tout seul, c'est SON dernier km (CPE, wifi
    domestique) : resserrer le secteur punirait ses voisins pour rien. C'est la
    correlation entre plusieurs abonnes qui designe le partage."""
    verdict = decider([20.0, 92.0, 95.0, 97.0])

    assert verdict.action == ACTION_HOLD
    assert verdict.trim_after == 1.0
    assert verdict.changed is False


def test_un_abonne_suffit_si_l_exploitant_le_demande() -> None:
    """Le seuil est configurable : a 1, un abonne isole declenche."""
    verdict = decider([20.0, 92.0], min_degraded=1)

    assert verdict.action == ACTION_TIGHTEN


def test_un_secteur_sain_sans_resserrage_ne_produit_rien() -> None:
    verdict = decider([88.0, 95.0])

    assert verdict.action == ACTION_HOLD
    assert verdict.changed is False


def test_on_resserre_vite_on_relache_lentement() -> None:
    """Sans ce delai de garde la boucle oscillerait : chaque desserrage ramenant
    la saturation qui l'avait provoque."""
    # Deux premiers cycles sains : le resserrage tient.
    premier = decider([90.0, 92.0], trim=0.80, healthy=0)
    assert premier.action == ACTION_HOLD
    assert premier.trim_after == pytest.approx(0.80)
    assert premier.healthy_cycles == 1

    deuxieme = decider([90.0, 92.0], trim=0.80, healthy=1)
    assert deuxieme.action == ACTION_HOLD
    assert deuxieme.healthy_cycles == 2

    # Troisieme : le delai de garde est tenu, on rend UN cran (pas tout).
    troisieme = decider([90.0, 92.0], trim=0.80, healthy=2)
    assert troisieme.action == ACTION_RELAX
    assert troisieme.trim_after == pytest.approx(0.90)
    assert troisieme.healthy_cycles == 0


def test_une_rechute_remet_le_compteur_de_retablissement_a_zero() -> None:
    verdict = decider([20.0, 25.0], trim=0.80, healthy=2)

    assert verdict.action == ACTION_TIGHTEN
    assert verdict.healthy_cycles == 0


def test_la_boucle_ne_descend_jamais_sous_le_plancher() -> None:
    """Au plancher, le goulot n'est plus le buffer radio mais la capacite :
    resserrer encore ne ferait que brider un secteur deja a genoux."""
    verdict = decider([10.0, 12.0], trim=0.50)

    assert verdict.action == ACTION_FLOOR
    assert verdict.trim_after == pytest.approx(0.50)
    assert verdict.changed is False
    assert "capacite" in verdict.reason


def test_sans_mesure_on_ne_decide_rien() -> None:
    """Un secteur dont la sonde s'est tue n'est pas un secteur qui va bien : on
    ne resserre pas, mais on ne fait pas non plus avancer le retablissement."""
    verdict = decider([], trim=0.80, healthy=2)

    assert verdict.action == ACTION_UNKNOWN
    assert verdict.trim_after == pytest.approx(0.80)
    assert verdict.healthy_cycles == 2
    assert verdict.changed is False


# =========================================================================
# 2. Le cycle complet
# =========================================================================
class DepotQoe:
    """Depot de topologie minimal : un lien de secteur et l'etat de la boucle."""

    def __init__(self, *, capacity_mbps: float = 200.0) -> None:
        self.capacity_mbps = capacity_mbps
        self.etats: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[Any] = []
        self.ecritures = 0

    # --- topologie ---
    async def links(self) -> list[dict[str, Any]]:
        return [
            {
                "key": LIEN,
                "source_key": "router:pop-test",
                "target_key": SECTEUR,
                "target_name": "BH-Nord",
                "interface": "ether2",
                "capacity_mbps": self.capacity_mbps,
                "discovered_by": "pop-test",
                "attributes": json.dumps({"local_networks": ["10.20.0.0/24"]}),
            }
        ]

    async def attachments(self) -> dict[str, str]:
        return {"dupont": SECTEUR, "durand": SECTEUR, "martin": SECTEUR}

    async def policy_map(self, scope: str) -> dict[str, dict[str, Any]]:
        return {}

    # --- etat de la boucle ---
    async def qoe_link_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.etats)

    async def qoe_trims(self) -> dict[str, float]:
        return {
            cle: float(etat["trim_factor"])
            for cle, etat in self.etats.items()
            if float(etat["trim_factor"]) < 1.0
        }

    async def save_qoe_link_state(self, **kwargs: Any) -> None:
        self.ecritures += 1
        self.etats[kwargs["link_key"]] = dict(kwargs)

    # --- audit ---
    async def record_audit(
        self, router_name: str, *, dry_run: bool, outcomes: Any, author: str | None = None
    ) -> int:
        self.audit_rows.extend((outcome, author) for outcome in outcomes)
        return len(outcomes)


class MetriquesQoe:
    """Depot de metriques minimal : les abonnes, leur plan et leur note de QoE."""

    def __init__(self, notes: dict[str, float]) -> None:
        self.notes = notes

    async def subscriber_latest(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "pppoe_login": login,
                "pop_name": "PoP Test",
                "plan_down_mbps": 100.0,
                "plan_up_mbps": 20.0,
            }
            for login in self.notes
        ]

    async def backhaul_latest(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def qoe_subscribers(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "subscriber_id": index,
                "pppoe_login": login,
                "pop_name": "PoP Test",
                "score": score,
                "severity": "crit" if score < 50 else "ok",
                "basis": "composite",
                "grade": "F" if score < 50 else "A",
                "bloat_ms": 300.0 if score < 50 else 3.0,
                "rtt_ms": 12.0,
            }
            for index, (login, score) in enumerate(self.notes.items(), start=1)
        ]


@pytest.fixture
def secteur_routeur() -> FakeRouterOsClient:
    """Un PoP avec trois abonnes derriere le meme secteur radio."""
    client = FakeRouterOsClient()
    for index, login in enumerate(("dupont", "durand", "martin"), start=10):
        client.add_session(login, address=f"10.20.0.{index}")
    client.address_rows = [{"address": "10.20.0.1/24", "interface": "ether2"}]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


def make_service(
    settings: Settings,
    client: FakeRouterOsClient,
    *,
    depot: DepotQoe,
    metriques: MetriquesQoe,
    ecriture: FauxClientEcriture | None = None,
) -> ShapingService:
    registry = RouterRegistry(settings, client_factory=lambda config: client)
    return ShapingService(
        settings,
        registry=registry,
        repository=depot,  # type: ignore[arg-type]
        metrics=metriques,
        write_client_factory=(lambda config: ecriture) if ecriture else None,
    )


def file_du_secteur(commandes: list[str]) -> str | None:
    """La commande qui porte la file du LIEN de secteur (le partage), pas celle
    d'un abonne."""
    return next((c for c in commandes if "freeqos-parent-BH-Nord" in c), None)


async def test_un_secteur_degrade_produit_un_plan_puis_le_calme_n_en_produit_aucun(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """LE critere de sortie de la phase 4.

    Cycle 1 : deux abonnes du secteur sont notes sous le seuil -> la file du
    lien est resserree d'un cran (200 Mbps * 0.90 de securite = 180, puis 0.90
    de resserrage = 162 Mbps) et le plan le montre.

    Cycle 2 : plus personne n'est degrade -> aucune action. Le resserrage est
    MAINTENU (delai de garde), il n'est pas rendu tout de suite : sans cette
    asymetrie la boucle oscillerait.
    """
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    depot = DepotQoe(capacity_mbps=200.0)
    ecriture = FauxClientEcriture()

    # --- cycle 1 : deux abonnes decroches ---
    service = make_service(
        settings,
        secteur_routeur,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0, "martin": 93.0}),
        ecriture=ecriture,
    )
    await service.registry.reload()

    premier = await service.adjust_for_qoe()

    assert premier["scored"] == 3
    secteur = premier["sectors"][0]
    assert secteur["action"] == ACTION_TIGHTEN
    assert secteur["trim_after"] == pytest.approx(0.90)
    assert premier["routers"] == ["pop-test"]

    # Le plan est diffable et porte le resserrage attendu, sur la file du LIEN.
    plan = premier["plans"][0]
    assert plan["counts"]["remove"] == 0  # jamais de purge
    commandes = [a.command for a in ecriture.executed]
    parent = file_du_secteur(commandes)
    assert parent is not None
    # 200 * 0.90 (securite) * 0.90 (resserrage) = 162 Mbps.
    assert "max-limit=162000000/162000000" in parent

    # Le plan SOUSCRIT des abonnes n'a pas bouge d'un bit.
    abonnes = [c for c in commandes if "freeqos-dupont" in c or "freeqos-durand" in c]
    assert abonnes and all("20000000/100000000" in c for c in abonnes)

    # --- cycle 2 : tout le monde est revenu dans les clous ---
    ecriture.executed.clear()
    service.metrics = MetriquesQoe({"dupont": 91.0, "durand": 93.0, "martin": 95.0})

    second = await service.adjust_for_qoe()

    assert second["sectors"][0]["action"] == ACTION_HOLD
    assert second["sectors"][0]["trim_after"] == pytest.approx(0.90)
    assert second["routers"] == []
    assert second["plans"] == []
    assert not ecriture.executed


async def test_le_resserrage_est_rendu_apres_le_delai_de_garde(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """La boucle n'est pas un cliquet : ce qu'elle a pris, elle le rend.

    Sans ce test, rien ne distinguerait une boucle fermee d'un resserrage
    definitif applique au premier pic de latence.
    """
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    settings.qoe_recovery_cycles = 2
    depot = DepotQoe(capacity_mbps=200.0)
    ecriture = FauxClientEcriture()
    service = make_service(
        settings,
        secteur_routeur,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0}),
        ecriture=ecriture,
    )
    await service.registry.reload()

    await service.adjust_for_qoe()
    assert (await depot.qoe_trims())[LIEN] == pytest.approx(0.90)

    service.metrics = MetriquesQoe({"dupont": 91.0, "durand": 93.0})
    await service.adjust_for_qoe()  # 1er cycle sain : on attend
    assert (await depot.qoe_trims())[LIEN] == pytest.approx(0.90)

    ecriture.executed.clear()
    troisieme = await service.adjust_for_qoe()  # 2e cycle sain : on rend un cran

    assert troisieme["sectors"][0]["action"] == ACTION_RELAX
    assert not await depot.qoe_trims()  # plus aucun resserrage en cours
    parent = file_du_secteur([a.command for a in ecriture.executed])
    assert parent is not None and "max-limit=180000000/180000000" in parent


async def test_rien_n_est_ecrit_en_lecture_seule_mais_la_decision_est_lisible(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """ENFORCEMENT_ENABLED reste le dernier rempart, boucle fermee ou pas.

    La decision est quand meme prise, enregistree et le plan calcule : c'est
    ainsi qu'on LIT ce que la boucle ferait avant de lui donner la main. Mais
    aucune commande ne part.
    """
    settings.enforcement_enabled = False
    depot = DepotQoe()
    ecriture = FauxClientEcriture()
    service = make_service(
        settings,
        secteur_routeur,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0}),
        ecriture=ecriture,
    )
    await service.registry.reload()

    resultat = await service.adjust_for_qoe()

    assert resultat["enabled"] is False
    assert resultat["sectors"][0]["action"] == ACTION_TIGHTEN
    assert resultat["plans"]  # le plan est calcule et lisible
    assert resultat["routers"] == [] and not ecriture.executed
    assert any("enforcement desactive" in e for e in resultat["errors"])


async def test_la_boucle_ne_purge_jamais(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """Meme garde-fou que reconcile() et expire_boosts() : ce job ecrit sans
    revue humaine, un /ppp/active momentanement vide ne doit pas lui faire
    supprimer toutes les files du PoP."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    settings.shaping_prune = True  # meme avec la purge activee globalement
    secteur_routeur.simple_queue_rows = [
        {
            ".id": "*1",
            "name": "freeqos-parti",
            "target": "10.20.0.99/32",
            "max-limit": "20M/100M",
            "comment": MANAGED_COMMENT,
        }
    ]
    ecriture = FauxClientEcriture()
    service = make_service(
        settings,
        secteur_routeur,
        depot=DepotQoe(),
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0}),
        ecriture=ecriture,
    )
    await service.registry.reload()

    await service.adjust_for_qoe()

    assert not [a for a in ecriture.executed if a.verb == "remove"]


async def test_l_ecriture_est_tracee_sous_son_propre_auteur(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """Le journal doit dire QUEL automate a change le debit : la boucle QoE n'est
    ni la reconciliation ni une action d'interface."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    depot = DepotQoe()
    service = make_service(
        settings,
        secteur_routeur,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0}),
        ecriture=FauxClientEcriture(),
    )
    await service.registry.reload()

    await service.adjust_for_qoe()

    assert depot.audit_rows
    assert all(author == "system:qoe-loop" for _, author in depot.audit_rows)


async def test_un_abonne_sans_secteur_connu_n_accuse_personne(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """On ne devine pas un rattachement : sans secteur, l'abonne ne pese sur
    aucune decision."""

    class SansRattachement(DepotQoe):
        async def attachments(self) -> dict[str, str]:
            return {}

    depot = SansRattachement()
    service = make_service(
        settings,
        secteur_routeur,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 5.0, "durand": 8.0}),
    )
    await service.registry.reload()

    resultat = await service.adjust_for_qoe()

    assert resultat["scored"] == 2
    assert resultat["sectors"] == []
    assert depot.ecritures == 0


async def test_un_secteur_sans_lien_connu_est_signale(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    """Rattachement connu mais aucun lien pour le desservir : il n'y a rien a
    resserrer, et le taire rendrait le secteur introuvable."""

    class SansLien(DepotQoe):
        async def links(self) -> list[dict[str, Any]]:
            return []

    service = make_service(
        settings,
        secteur_routeur,
        depot=SansLien(),
        metriques=MetriquesQoe({"dupont": 5.0, "durand": 8.0}),
    )
    await service.registry.reload()

    resultat = await service.adjust_for_qoe()

    assert resultat["sectors"] == []
    assert any("aucun lien connu" in e for e in resultat["errors"])


async def test_un_routeur_injoignable_n_arrete_pas_la_boucle(
    settings: Settings, secteur_routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    service = make_service(
        settings,
        secteur_routeur,
        depot=DepotQoe(),
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0}),
    )
    await service.registry.reload()
    secteur_routeur.raise_on_ppp = TimeoutError("routeur muet")

    resultat = await service.adjust_for_qoe()

    assert resultat["routers"] == []
    assert resultat["errors"] and "pop-test" in resultat["errors"][0]

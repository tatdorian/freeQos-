"""Service de shaping : verrous, analyse de l'existant, plan de bout en bout."""

from __future__ import annotations

import pytest

from app.config import RouterConfig, Settings
from app.enforcement.models import MANAGED_COMMENT, Plan, PlanAction
from app.enforcement.planner import LinkTarget, SubscriberTarget
from app.enforcement.routeros import MissingWriteCredentialsError
from app.services.registry import RouterRegistry
from app.services.shaping import EnforcementDisabledError, ShapingService
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture


def make_service(settings: Settings, client: FakeRouterOsClient, **kwargs) -> ShapingService:
    registry = RouterRegistry(settings, client_factory=lambda config: client)
    service = ShapingService(settings, registry=registry, **kwargs)
    return service


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    client.neighbor_rows = [
        {
            "interface": "ether2",
            "identity": "BH-Nord",
            "mac-address": "DC:9F:DB:11:22:33",
            "platform": "Ubiquiti Networks Inc.",
        }
    ]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


# ------------------------------------------------------- VERROU PRINCIPAL
async def test_application_reelle_refusee_si_enforcement_desactive(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Le drapeau global est le dernier rempart : meme avec un plan valide et
    une confirmation, rien ne part si l'enforcement est desactive."""
    settings.enforcement_enabled = False
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    with pytest.raises(EnforcementDisabledError, match="ENFORCEMENT_ENABLED"):
        await service.apply(plan, dry_run=False)


async def test_dry_run_autorise_meme_enforcement_desactive(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Simuler doit rester possible : c'est ainsi qu'on prepare une bascule."""
    settings.enforcement_enabled = False
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    resultat = await service.apply(plan, dry_run=True)

    assert resultat.ok and resultat.dry_run is True


async def test_application_reelle_sans_compte_ecriture(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    settings.routers = [RouterConfig(name="pop-test", host="192.0.2.11", password="lecture")]
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    with pytest.raises(MissingWriteCredentialsError):
        await service.apply(plan, dry_run=False)


async def test_application_reelle_aboutit(settings: Settings, routeur: FakeRouterOsClient) -> None:
    settings.enforcement_enabled = True
    ecriture = FauxClientEcriture()
    service = make_service(settings, routeur, write_client_factory=lambda config: ecriture)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    resultat = await service.apply(plan, dry_run=False)

    assert resultat.ok
    assert [a.name for a in ecriture.executed] == ["x"]


# ------------------------------------------------ analyse de l'existant
async def test_inspection_distingue_nos_files_des_autres(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Avant d'ecrire, il faut savoir ce que l'operateur ou RADIUS ont deja pose."""
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-dupont", "comment": MANAGED_COMMENT},
        {".id": "*2", "name": "queue-radius-jean", "comment": ""},
        {".id": "*3", "name": "shaping-exploitant", "comment": "ne pas toucher"},
    ]
    service = make_service(settings, routeur)
    await service.registry.reload()

    etats = await service.inspect()

    etat = etats[0].to_dict()
    assert etat["counts"]["managed"] == 1
    assert etat["counts"]["foreign"] == 2
    assert {q["name"] for q in etat["foreign_queues"]} == {
        "queue-radius-jean",
        "shaping-exploitant",
    }


async def test_inspection_d_un_routeur_injoignable(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.raise_on_queues = ConnectionRefusedError("connexion refusee")
    service = make_service(settings, routeur)
    await service.registry.reload()

    etats = await service.inspect()

    assert etats[0].reachable is False
    assert "ConnectionRefusedError" in (etats[0].error or "")


# ----------------------------------------------------- plan de bout en bout
async def test_plan_complet_depuis_un_routeur_vierge(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()

    plan = await service.plan(
        "pop-test",
        links=[LinkTarget(name="bh-nord", interface="ether2", measured_capacity_mbps=500)],
        subscribers=[
            SubscriberTarget(
                login="dupont",
                interface="<pppoe-dupont>",
                plan_down_mbps=100,
                plan_up_mbps=20,
                parent="freeqos-parent-bh-nord",
            )
        ],
    )

    assert plan.counts() == {"add": 4, "set": 0, "remove": 0}
    commandes = [a.command for a in plan.actions]
    assert any("kind=cake" in c for c in commandes)
    assert any("cake-overhead=22" in c for c in commandes)
    # 500 x 0,9 = 450 Mbps sur le parent.
    assert any("450000000" in c for c in commandes)


async def test_plan_sur_routeur_inconnu(settings: Settings, routeur: FakeRouterOsClient) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()
    with pytest.raises(KeyError):
        await service.plan("pop-inexistant", links=[], subscribers=[])


# ------------------------------------------------------------- decouverte
async def test_decouverte_construit_le_graphe(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()

    snapshot = await service.discover()

    assert "router:pop-test" in snapshot.nodes
    assert "mac:DC:9F:DB:11:22:33" in snapshot.nodes
    lien = next(iter(snapshot.links.values()))
    assert lien.interface == "ether2"
    assert lien.capacity_mbps == 1000.0


async def test_un_pop_injoignable_n_annule_pas_la_decouverte(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.raise_on_neighbors = TimeoutError("pas de reponse")
    service = make_service(settings, routeur)
    await service.registry.reload()

    snapshot = await service.discover()

    assert snapshot.nodes == {}
    assert len(snapshot.warnings) == 1
    assert "TimeoutError" in snapshot.warnings[0]

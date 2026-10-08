"""La reconciliation n'oscille plus sans fin.

CONSTATE EN PRODUCTION : toutes les deux minutes, la cible de la file *5A
etait reecrite en alternance (ether3,lan-bridge / ether6,lan-bridge /
100.100.105.240/30), plus de 76 fois en quelques heures.
"""

from __future__ import annotations

from app.config import Settings
from app.enforcement.models import Plan, PlanAction
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture
from tests.test_shaping_service import make_service


def _cible(valeur: str) -> Plan:
    return Plan(
        router_name="pop-test",
        actions=[
            PlanAction(
                verb="set",
                path="/queue/simple",
                target_id="*5A",
                name="freeqos-parent-x",
                fields={"target": valeur},
            )
        ],
    )


async def test_une_cible_qui_va_et_vient_est_gelee(settings: Settings) -> None:
    settings.enforcement_enabled = True
    ecriture = FauxClientEcriture()
    service = make_service(settings, FakeRouterOsClient(), write_client_factory=lambda c: ecriture)
    await service.registry.reload()

    await service.apply(_cible("ether3,lan-bridge"), dry_run=False)
    await service.apply(_cible("100.100.105.240/30"), dry_run=False)
    # Retour a l'avant-derniere valeur : A -> B -> A. Rien ne part.
    resultat = await service.apply(_cible("lan-bridge, ether3"), dry_run=False)

    assert resultat.applied == 0
    assert len(ecriture.executed) == 2
    [gel] = service.frozen.values()
    assert gel["field"] == "target" and gel["line"] == "freeqos-parent-x"
    # Gelee : plus rien, meme vers une autre valeur.
    await service.apply(_cible("10.0.0.0/24"), dry_run=False)
    assert len(ecriture.executed) == 2

    # Geste explicite : le nettoyage du routeur libere la ligne.
    assert service.unfreeze("pop-test") == 1
    await service.apply(_cible("10.0.0.0/24"), dry_run=False)
    assert len(ecriture.executed) == 3


async def test_un_debit_qui_va_et_vient_n_est_pas_une_oscillation(settings: Settings) -> None:
    """Un boost qui expire ramene le debit d'avant : c'est normal."""
    settings.enforcement_enabled = True
    ecriture = FauxClientEcriture()
    service = make_service(settings, FakeRouterOsClient(), write_client_factory=lambda c: ecriture)
    await service.registry.reload()

    def debit(v: str) -> Plan:
        return Plan(
            router_name="pop-test",
            actions=[
                PlanAction(
                    verb="set",
                    path="/queue/simple",
                    target_id="*1",
                    name="freeqos-dupont",
                    fields={"max-limit": v},
                )
            ],
        )

    for valeur in ("20M/100M", "40M/200M", "20M/100M", "40M/200M"):
        await service.apply(debit(valeur), dry_run=False)
    assert len(ecriture.executed) == 4
    assert not service.frozen


def test_deux_passes_sans_changement_reel_n_ecrivent_rien() -> None:
    """Une adresse relue sous une autre forme n'est pas un changement :
    « 10.20.0.10 » = « 10.20.0.10/32 », « 10.0.0.1/24 » = « 10.0.0.0/24 »."""
    from app.enforcement.models import MANAGED_COMMENT, QueueSpec
    from app.enforcement.planner import build_plan

    for voulu, relu in (("10.20.0.10", "10.20.0.10/32"), ("10.0.0.1/24", "10.0.0.0/24")):
        spec = QueueSpec(name="freeqos-x", target=voulu, max_up_mbps=10, max_down_mbps=50)
        pose = {**spec.routeros_fields(), ".id": "*1", "target": relu, "comment": MANAGED_COMMENT}
        for _passe in range(2):
            plan = build_plan(
                "nas",
                desired_types=[],
                desired_queues=[spec],
                actual_types=[],
                actual_queues=[pose],
            )
            assert plan.is_empty, [a.command for a in plan.actions]

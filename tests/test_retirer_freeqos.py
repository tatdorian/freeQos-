"""« Remove freeQoS » : le routeur comme avant freeQoS, en un geste.

DEMANDE (plainte de clients) : retirer toutes les files posees par freeQoS sur
les routeurs, sans toucher a ce que l'exploitant ou RADIUS y ont mis, et sans
que rien ne soit repose ensuite.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.enforcement import models
from app.enforcement.models import MANAGED_COMMENT
from app.enforcement.restrictions import tag
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture
from tests.test_shaping_service import make_service


def _routeur() -> FakeRouterOsClient:
    routeur = FakeRouterOsClient()
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-parent", "comment": MANAGED_COMMENT, "parent": "none"},
        {
            ".id": "*2",
            "name": "freeqos-dupont",
            "comment": MANAGED_COMMENT,
            "parent": "freeqos-parent",
        },
        {".id": "*3", "name": "client-vip", "comment": "pose a la main", "parent": "none"},
        {".id": "*4", "name": "<pppoe-radius>", "comment": "", "parent": "none"},
    ]
    routeur.queue_type_rows = [
        {".id": "*T1", "name": "freeqos-cake-up", "kind": "cake"},
        {".id": "*T2", "name": "freeqos-cake-down", "kind": "cake"},
        {".id": "*T3", "name": "default", "kind": "pfifo"},
    ]
    routeur.firewall_filter_rows = [
        {".id": "*F1", "chain": "forward", "action": "drop", "comment": tag(1, "drop-dst")},
        {".id": "*F2", "chain": "forward", "action": "fasttrack-connection", "comment": "defconf"},
    ]
    routeur.firewall_mangle_rows = [{".id": "*M1", "comment": tag(2, "mark-d")}]
    routeur.queue_tree_rows = [{".id": "*Q1", "name": "freeqos-r2-down", "comment": tag(2, "q-d")}]
    routeur.firewall_address_list_rows = [
        {
            ".id": "*A1",
            "list": "freeqos-r1-dst",
            "address": "203.0.113.7",
            "comment": tag(1, "dst"),
        },
        {".id": "*A2", "list": "clients-bloques", "address": "198.51.100.1", "comment": ""},
    ]
    return routeur


@pytest.mark.parametrize("ecriture", [True, False])
async def test_tout_ce_qui_porte_la_marque_part_et_rien_d_autre(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, ecriture: bool
) -> None:
    """Meme un routeur deja passe en lecture seule est nettoye : retirer est le
    sens sur, et c'est justement l'ecriture coupee qui empeche de reposer."""
    monkeypatch.setattr(models, "INSTANCE_ID", "cette-instance")
    settings.enforcement_enabled = False
    settings.routers[0].write_enabled = ecriture
    ecrivain = FauxClientEcriture()
    service = make_service(settings, _routeur(), write_client_factory=lambda c: ecrivain)
    await service.registry.reload()

    rapport = await service.uninstall("pop-test", author="ui:test")

    assert all(a.verb == "remove" for a in ecrivain.executed)
    ids = [a.target_id for a in ecrivain.executed]
    assert set(ids) == {"*1", "*2", "*T1", "*T2", "*F1", "*M1", "*Q1", "*A1"}
    # Le blocage cesse d'abord, l'enfant part avant son parent, les types en dernier.
    assert ids.index("*F1") < ids.index("*A1")
    assert ids.index("*2") < ids.index("*1") < ids.index("*T1")
    assert rapport["removed"] == 8 and rapport["errors"] == []
    assert rapport["by_kind"]["simple queues"] == 2


async def test_un_plan_qui_pose_quelque_chose_reste_refuse_en_lecture_seule(
    settings: Settings,
) -> None:
    """Le passe-droit ne vaut que pour un plan fait UNIQUEMENT de retraits."""
    from app.enforcement.models import Plan, PlanAction
    from app.services.shaping import RouterWriteDisabledError

    settings.routers[0].write_enabled = False
    service = make_service(
        settings, _routeur(), write_client_factory=lambda c: FauxClientEcriture()
    )
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )
    with pytest.raises(RouterWriteDisabledError):
        await service.apply(plan, dry_run=False, author="ui:test", explicit=True, removal=True)

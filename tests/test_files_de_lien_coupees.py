"""Plus jamais de file sur un LIEN sans qu'on l'ait demande.

CONSTATE EN PRODUCTION : des clients se sont plaints. freeQoS avait pose, sur
le segment d'un PTP, une file parente plafonnee a la capacite du lien : tout ce
qui traversait le PTP -- transit compris, pas seulement les clients -- passait
dedans. Desormais, par defaut, seules les adresses de chaque client sont
bridees ; une file de lien se demande dans Reglages (shaping_link_queues).
"""

from __future__ import annotations

from app.config import Settings
from app.enforcement.planner import LINK_QUEUES_OFF, LinkTarget, SubscriberTarget
from app.services.runtime_config import REGLAGES
from tests.conftest import FakeRouterOsClient
from tests.test_shaping_service import make_service


def test_le_defaut_est_coupe_et_reglable_a_chaud() -> None:
    assert (
        Settings(_env_file=None, database_url="postgresql://x:x@localhost/x").shaping_link_queues
        is False
    )
    assert any(r.name == "shaping_link_queues" and r.kind == "bool" for r in REGLAGES)


async def test_par_defaut_aucune_file_de_lien_seulement_le_client(settings: Settings) -> None:
    settings.shaping_link_queues = False
    service = make_service(settings, FakeRouterOsClient())
    await service.registry.reload()

    plan = await service.plan(
        "pop-test",
        links=[
            LinkTarget(
                name="ptp-caisse",
                interface="sfp-sfpplus1",
                subnet="10.20.0.1/24",
                measured_capacity_mbps=500,
            )
        ],
        subscribers=[
            SubscriberTarget(
                login="dupont",
                interface="<pppoe-dupont>",
                address="10.20.0.10",
                plan_down_mbps=100,
                plan_up_mbps=20,
                parent="freeqos-parent-ptp-caisse",
            )
        ],
    )

    files = [a for a in plan.actions if a.path == "/queue/simple"]
    # UNE seule file : celle du client, sur SA seule adresse, sans parent.
    assert len(files) == 1
    cible = str(files[0].fields.get("target"))
    assert cible.startswith("10.20.0.10")
    assert str(files[0].fields.get("parent") or "none") == "none"
    assert not any("10.20.0.0/24" in a.command for a in plan.actions)
    assert any(s.reason == LINK_QUEUES_OFF for s in plan.skipped)

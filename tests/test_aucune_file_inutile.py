"""Une file qui ne limite rien n'est jamais ecrite.

CONSTATE EN PRODUCTION : des clients se sont plaints alors qu'AUCUNE limite
n'etait posee. Les files 0/0 (client a IP fixe sans forfait, lien decouvert
sans capacite) ne bridaient rien, mais tout le trafic qu'elles visaient passait
par le traitement des files du routeur. Desormais : pas de debit, pas de file.
"""

from __future__ import annotations

from app.config import Settings
from app.enforcement.planner import NO_RATE_SKIP, LinkTarget, SubscriberTarget, desired_state
from app.models import KIND_STATIC


def test_client_a_ip_fixe_sans_forfait_aucune_file() -> None:
    _types, files, ecartes = desired_state(
        links=[],
        subscribers=[
            SubscriberTarget(
                login="mairie",
                interface="vlan2060",
                plan_down_mbps=None,
                plan_up_mbps=None,
                kind=KIND_STATIC,
                address="10.9.0.0/30",
            )
        ],
    )
    assert files == []
    assert [(e.login, e.reason) for e in ecartes] == [("mairie", NO_RATE_SKIP)]


def test_un_client_avec_forfait_garde_sa_file() -> None:
    _types, files, _ = desired_state(
        links=[],
        subscribers=[
            SubscriberTarget(
                login="mairie",
                interface="vlan2060",
                plan_down_mbps=50,
                plan_up_mbps=10,
                kind=KIND_STATIC,
                address="10.9.0.0/30",
            )
        ],
    )
    assert len(files) == 1 and files[0].max_limit != "0/0"


def test_lien_sans_capacite_aucune_file_par_defaut() -> None:
    defaut = Settings(_env_file=None, database_url="postgresql://x:x@h/x")
    assert defaut.shaping_queue_for_detected_links is False
    _types, files, _ = desired_state(
        links=[LinkTarget(name="ptp", interface="sfp1", subnet="10.1.0.1/30")],
        subscribers=[],
        link_queues=True,
        queue_unmeasured_links=defaut.shaping_queue_for_detected_links,
    )
    assert files == []

"""freeQoS ne bride JAMAIS son propre reseau.

Constate en production : une file posee sur NAS-FRANCOPHONIE freinait le
trafic vers DS-CCR, et revenait a chaque cycle quand on la corrigeait.
"""

from __future__ import annotations

import pytest

from app.collectors import mikrotik
from app.enforcement.planner import LinkTarget, SubscriberTarget, desired_state
from app.services.shaping import protect_infrastructure


@pytest.fixture(autouse=True)
def reseau(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mikrotik, "_AMONTS", {"NAS": ("100.100.101.113", "ether1")})
    monkeypatch.setattr(
        mikrotik,
        "_PROPRIETAIRES",
        {"11.11.11.1": ("DS-CCR", "lo"), "10.20.0.1": ("NAS", "vlan2060")},
    )
    monkeypatch.setattr(mikrotik, "_LOOPBACKS_DETECTES", {"NAS": "11.11.11.75"})


def _client(login: str, adresse: str) -> SubscriberTarget:
    return SubscriberTarget(
        login=login,
        interface="",
        plan_down_mbps=100,
        plan_up_mbps=20,
        kind="static",
        address=adresse,
    )


def test_le_lien_montant_n_a_pas_de_file() -> None:
    montant = LinkTarget(name="DS-CCR", interface="ether1", measured_capacity_mbps=1000)
    par_segment = LinkTarget(
        name="core", interface="vlan9", subnet="100.100.101.112/30", measured_capacity_mbps=1000
    )
    secteur = LinkTarget(
        name="secteur", interface="ether3", subnet="10.30.0.0/24", measured_capacity_mbps=200
    )
    liens = [montant, par_segment, secteur]
    protect_infrastructure("NAS", liens, [])
    _types, files, ecartes = desired_state(links=liens, subscribers=[])
    assert [f.target for f in files] == ["10.30.0.0/24"]
    assert {e.login for e in ecartes} == {"DS-CCR", "core"}


def test_un_routeur_vu_comme_client_n_est_pas_bride() -> None:
    clients = [
        _client("ds-ccr", "11.11.11.1"),  # loopback d'un de nos routeurs
        _client("passerelle", "100.100.101.113"),
        _client("bloc", "11.11.11.0/24"),  # couvre des loopbacks
        _client("nestle", "10.20.0.0/29"),  # contient la passerelle du VLAN : normal
        _client("vrai", "10.20.0.10"),
    ]
    protect_infrastructure("NAS", [], clients)
    _types, files, ecartes = desired_state(links=[], subscribers=clients)
    assert sorted(f.target for f in files) == ["10.20.0.0/29", "10.20.0.10/32"]
    assert {e.login for e in ecartes} == {"ds-ccr", "passerelle", "bloc"}
    assert all("never shaped" in e.reason for e in ecartes)


def test_une_file_tierce_n_est_jamais_debridee() -> None:
    """Un client sans debit (compteur 0/0) ne doit pas aligner la file que
    l'exploitant avait posee sur sa cible : ce serait la debrider."""
    from app.enforcement.planner import build_plan

    client = _client("sans-forfait", "10.20.0.10")
    client.plan_down_mbps = None
    client.plan_up_mbps = None
    types, files, _ = desired_state(links=[], subscribers=[client])
    tierce = {
        ".id": "*5A",
        "name": "client-maison",
        "target": "10.20.0.10/32",
        "max-limit": "2M/10M",
    }
    plan = build_plan(
        "NAS",
        desired_types=types,
        desired_queues=files,
        actual_types=[],
        actual_queues=[tierce],
    )
    assert not [a for a in plan.actions if a.path == "/queue/simple"]


def test_le_segment_de_transit_commun_au_coeur_n_a_pas_de_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONSTATE EN PRODUCTION : files CAKE sur 100.100.101.112/29 (segment
    commun aux NAS et a DS-CCR) et sur 100.100.100.252/30 (uplink) : 4 a 7 s de
    latence et l'acces au coeur coupe."""
    monkeypatch.setattr(mikrotik, "_AMONTS", {"DS-CCR": ("100.100.100.254", "ether1")})
    monkeypatch.setattr(
        mikrotik,
        "_PROPRIETAIRES",
        {
            "100.100.101.113": ("DS-CCR", "ether2"),
            "100.100.101.114": ("NAS-TAILLADJE", "ether1"),
            "100.100.101.115": ("NAS-BASSORA", "ether1"),
        },
    )
    transit = LinkTarget(name="NAS-TAILLADJE", interface="ether2", subnet="100.100.101.113/29")
    uplink = LinkTarget(name="MAIN-GATEWAY", interface="ether1", subnet="100.100.100.253/30")
    clients = LinkTarget(name="clients", interface="ether3", subnet="172.16.38.1/23")
    liens = [transit, uplink, clients]
    protect_infrastructure("DS-CCR", liens, [])
    _types, files, ecartes = desired_state(links=liens, subscribers=[])
    assert [f.target for f in files] == ["172.16.38.0/23"]
    raisons = {e.login: e.reason for e in ecartes}
    assert "transit link" in raisons["NAS-TAILLADJE"]
    assert "uplink" in raisons["MAIN-GATEWAY"]


@pytest.mark.parametrize(
    ("ligne", "attendu"),
    [
        ({"target_key": "router:NAS-BASSORA", "target_name": "NAS-BASSORA"}, "managed router"),
        ({"target_key": "mac:aa", "attributes": {"routing_adjacency": True}}, "routing adjacency"),
        (
            {"target_key": "mac:bb", "attributes": '{"routing_adjacency": true}'},
            "routing adjacency",
        ),
        ({"target_key": "mac:cc", "target_kind": "gateway"}, "gateway"),
    ],
)
def test_un_lien_vers_un_routeur_est_un_lien_de_transit(ligne: dict, attendu: str) -> None:
    from app.services.shaping import transit_reason

    assert attendu in str(transit_reason(ligne))


def test_un_lien_vers_une_antenne_n_est_pas_du_transit() -> None:
    from app.services.shaping import transit_reason

    assert transit_reason({"target_key": "mac:dd", "target_kind": "radio"}) is None

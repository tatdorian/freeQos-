"""Collecteur MikroTik : correlation /ppp/active <-> /interface."""

from __future__ import annotations

import pytest

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig
from tests.conftest import FakeRouterOsClient


async def test_correlation_des_compteurs_via_interface_dynamique(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    """Les octets viennent de <pppoe-LOGIN>, pas de /ppp/active."""
    fake_client.add_session("dupont", rx_byte=1_000, tx_byte=2_000, uptime="02:00:00")

    sessions = await collector.collect()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.login == "dupont"
    assert session.interface == "<pppoe-dupont>"
    assert session.rx_bytes == 1_000
    assert session.tx_bytes == 2_000
    assert session.uptime_s == 7200
    assert session.pop_name == "PoP Test"
    assert session.router_name == "pop-test"
    assert session.address == "10.20.0.10"


async def test_session_sans_interface_correlee_reste_collectee(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    """Une session sans compteur doit quand meme etre vue : l'abonne est en ligne,
    seul son debit est inconnu."""
    fake_client.active.append({"name": "orphelin", "address": "10.20.0.99", "uptime": "00:10:00"})

    sessions = await collector.collect()

    assert len(sessions) == 1
    assert sessions[0].login == "orphelin"
    assert sessions[0].rx_bytes is None
    assert sessions[0].interface is None


async def test_repli_sur_nom_approchant(
    router_config: RouterConfig, fake_client: FakeRouterOsClient
) -> None:
    """Profil PPP avec un nommage non standard : on retrouve l'interface."""
    fake_client.add_session("dupont", rx_byte=42, tx_byte=43, interface_name="pppoe-dupont-in")
    collector = MikrotikCollector(router_config, client=fake_client)

    sessions = await collector.collect()

    assert sessions[0].interface == "pppoe-dupont-in"
    assert sessions[0].rx_bytes == 42


async def test_correlation_ambigue_refuse_d_attribuer_un_debit(
    router_config: RouterConfig,
) -> None:
    """Deux interfaces candidates : mieux vaut aucun debit qu'un debit attribue
    au mauvais abonne."""
    client = FakeRouterOsClient(
        active=[{"name": "jean", "uptime": "01:00:00"}],
        interfaces=[
            {"name": "vlan-jean-a", "rx-byte": "1", "tx-byte": "2"},
            {"name": "vlan-jean-b", "rx-byte": "3", "tx-byte": "4"},
        ],
    )
    collector = MikrotikCollector(router_config, client=client)

    sessions = await collector.collect()

    assert sessions[0].rx_bytes is None
    assert sessions[0].interface is None


async def test_plusieurs_sessions(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    for index in range(5):
        fake_client.add_session(f"abonne{index}", rx_byte=index * 100, tx_byte=index * 200)

    sessions = await collector.collect()

    assert [s.login for s in sessions] == [f"abonne{i}" for i in range(5)]
    assert [s.rx_bytes for s in sessions] == [0, 100, 200, 300, 400]


async def test_erreur_routeur_remontee(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    fake_client.raise_on_ppp = ConnectionResetError("connexion perdue")
    with pytest.raises(ConnectionResetError):
        await collector.collect()


async def test_session_sans_nom_ignoree(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    fake_client.active.append({"address": "10.0.0.1"})
    assert await collector.collect() == []


def test_le_collecteur_n_expose_aucune_ecriture() -> None:
    """Garde-fou hors-bande : aucune methode d'ecriture ne doit apparaitre."""
    interdits = {"set", "add", "remove", "write", "push", "apply", "queue", "enforce"}
    exposes = {name for name in dir(MikrotikCollector) if not name.startswith("_")}
    assert exposes & interdits == set()

"""Controle en direct d'un abonne : le routeur, NetFlow et la base, cote a cote.

DEMANDE EXPLICITE : un test de debit passait, l'onglet Subscribers ne le
montrait pas. Ce controle dit OU le debit se perd, au lieu de le deviner.
"""

from __future__ import annotations

from app.api.metrics import live_verdict
from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig
from tests.conftest import FakeRouterOsClient


class RouteurQuiDebite(FakeRouterOsClient):
    """Les compteurs de l'interface avancent a chaque lecture."""

    def interfaces(self):  # type: ignore[no-untyped-def]
        for row in self.interfaces_rows:
            if row["name"] == "<pppoe-test-ta>":
                row["tx-byte"] = str(int(row["tx-byte"]) + 250_000)  # vers l'abonne
                row["rx-byte"] = str(int(row["rx-byte"]) + 25_000)
        return super().interfaces()


async def test_le_routeur_est_lu_deux_fois_et_donne_le_debit_reel() -> None:
    client = RouteurQuiDebite()
    client.add_session("test-ta", address="10.20.0.7", rx_byte=0, tx_byte=0)
    client.simple_queue_rows = [
        {
            "name": "freeqos-test-ta",
            "target": "10.20.0.7/32",
            "max-limit": "50M/300M",
            "rate": "96000/880000",
            "bytes": "0/0",
        }
    ]
    collector = MikrotikCollector(
        RouterConfig(name="nas-ta", host="192.0.2.1", password="x"), client=client
    )

    lu = await collector.live_subscriber("test-ta", window_s=0.05)

    assert lu["session"]["address"] == "10.20.0.7"
    assert lu["interface"] == "<pppoe-test-ta>"
    # tx de l'interface = ce que le routeur envoie a l'abonne = son download.
    assert lu["interface_tx_bps"] > lu["interface_rx_bps"] > 0
    (file,) = lu["queues"]
    assert file["rate_down_bps"] == 880000 and file["rate_up_bps"] == 96000


async def test_sans_session_le_controle_le_dit() -> None:
    collector = MikrotikCollector(
        RouterConfig(name="nas-ta", host="192.0.2.1", password="x"), client=FakeRouterOsClient()
    )
    lu = await collector.live_subscriber("absent", window_s=0.01)
    verdict = live_verdict("pppoe", lu, None, None, [])
    assert "No open PPPoE session" in verdict["text"]


def live(tx: float | None, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "router": "nas-ta",
        "session": {"address": "10.20.0.7"},
        "interface": "<pppoe-x>",
        "interface_found": True,
        "interface_tx_bps": tx,
        "interface_rx_bps": 1000.0,
        "queues": [],
    }
    base.update(extra)
    return base


def test_le_routeur_compte_mais_rien_n_est_enregistre_designe_la_collecte() -> None:
    v = live_verdict("pppoe", live(500_000.0), None, {"age_s": 3600, "tx_bps": 493}, [])
    assert v["level"] == "crit" and "cycle is failing" in v["text"]


def test_netflow_voit_mais_pas_l_interface_designe_le_chemin() -> None:
    v = live_verdict("pppoe", live(400.0), {"tx_bps": 400_000.0, "rx_bps": 0.0}, {"age_s": 5}, [])
    assert v["level"] == "warn" and "does not go through its session" in v["text"]


def test_tout_concorde() -> None:
    v = live_verdict("pppoe", live(500_000.0), None, {"age_s": 5, "tx_bps": 500_000}, [])
    assert v["level"] == "ok"


def test_interface_introuvable() -> None:
    v = live_verdict("pppoe", live(None, interface_found=False, interface=None), None, None, [])
    assert "PPPOE_INTERFACE_PATTERN" in v["text"]

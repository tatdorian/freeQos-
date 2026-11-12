"""Le trafic des equipements UISP : debit des ports, stations des AP, place
dans l'arbre, et ce qui en sort pour la surveillance et le diagnostic radio."""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors.topology import TopologyNode, TopologySnapshot, attach_uisp_devices
from app.services import uisp_traffic as module
from app.services.client_health import radio
from app.services.uisp_traffic import UispTraffic, interface_rates, parse_station

AP = {
    "identification": {
        "id": "ap-1",
        "name": "AP-Nord",
        "role": "ap",
        "mac": "AA:00:00:00:00:01",
        "site": {"name": "Plateau"},
        "modelName": "Rocket 5AC",
    },
    "overview": {
        "status": "active",
        "downlinkCapacity": 200_000_000,
        "uplinkCapacity": 150_000_000,
    },
}
STATION = {
    "identification": {
        "id": "st-1",
        "name": "CPE-Dupont",
        "role": "station",
        "mac": "AA:00:00:00:00:02",
    },
    "overview": {"status": "active", "signal": -61},
    "attributes": {"apDevice": {"id": "ap-1", "name": "AP-Nord"}},
}


def test_le_debit_vient_du_port_radio() -> None:
    ports = [
        {
            "identification": {"name": "eth0"},
            "statistics": {"rxrate": 9_000_000, "txrate": 8_000_000},
        },
        {
            "identification": {"name": "ath0"},
            "statistics": {"rxrate": 2_000_000, "txrate": 30_000_000},
        },
    ]
    assert interface_rates(ports) == (2_000_000, 30_000_000, "ath0")


def test_sans_port_radio_le_plus_charge() -> None:
    ports = [
        {"identification": {"name": "eth0"}, "statistics": {"rxrate": 1, "txrate": 1}},
        {"identification": {"name": "eth1"}, "statistics": {"rxrate": 500, "txrate": 700}},
        {"identification": {"name": "lo"}},
    ]
    assert interface_rates(ports) == (500, 700, "eth1")
    assert interface_rates([]) == (None, None, None)


def test_une_station_se_lit_quelle_que_soit_la_version() -> None:
    st = parse_station(
        {
            "deviceIdentification": {"mac": "aa:00:00:00:00:02", "name": "CPE"},
            "ipAddress": "10.20.0.10",
            "rxSignal": -63,
            "rxRate": 4_000_000,
        }
    )
    assert st["mac"] == "aa:00:00:00:00:02" and st["ip"] == "10.20.0.10"
    assert st["signal_dbm"] == -63 and st["rx_bps"] == 4_000_000 and st["tx_bps"] is None


def test_le_lien_station_ap_vise_la_vraie_case_de_l_ap() -> None:
    """L'AP connue du routeur par sa MAC : le lien de sa station s'y accroche."""
    snap = TopologySnapshot()
    snap.add_node(
        TopologyNode(
            key="mac:AA:00:00:00:00:01", name="ap", kind="unknown", mac="AA:00:00:00:00:01"
        )
    )
    attach_uisp_devices(snap, [AP, STATION])
    [lien] = list(snap.links.values())
    assert lien.source_key == "mac:AA:00:00:00:00:01"
    assert lien.target_key == "mac:AA:00:00:00:00:02"


class FauxDepot:
    def __init__(self) -> None:
        self.marques: list[Any] = []

    async def credentials(self) -> tuple[str, str, bool]:
        return ("https://uisp.test", "jeton", True)

    async def mark(self, **k: Any) -> None:
        self.marques.append(k)

    async def links(self) -> list[dict[str, Any]]:
        return [{"device_id": "st-1", "name": "CPE", "pop_name": "Plateau"}]


class FauxUisp:
    hors_ligne = False

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    async def raw_devices(self) -> list[dict[str, Any]]:
        ap = {
            **AP,
            "overview": {
                **AP["overview"],
                "status": "disconnected" if FauxUisp.hors_ligne else "active",
            },
        }
        return [ap, STATION]

    async def device_interfaces(self, ident: str) -> list[dict[str, Any]]:
        return [
            {
                "identification": {"name": "ath0"},
                "statistics": {"rxrate": 1_000_000, "txrate": 12_000_000},
            }
        ]

    async def device_stations(self, ident: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "CPE-Dupont",
                "mac": "AA:00:00:00:00:02",
                "ipAddress": "10.20.0.10",
                "rxSignal": -61,
            }
        ]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def faux_uisp(monkeypatch):  # type: ignore[no-untyped-def]
    FauxUisp.hors_ligne = False
    monkeypatch.setattr(module, "UispProvider", FauxUisp)
    return FauxUisp


async def test_le_releve_garde_debit_stations_et_signale_l_ap_tombee(faux_uisp) -> None:  # type: ignore[no-untyped-def]
    trafic = UispTraffic(FauxDepot())
    assert await trafic.collect() == {"devices": 2}
    ap = trafic.devices["ap-1"]
    assert (ap["rx_bps"], ap["tx_bps"], ap["port"]) == (1_000_000, 12_000_000, "ath0")
    assert ap["stations_count"] == 1 and "st-1" not in trafic.stations
    assert await trafic.problems() == []
    # Le diagnostic radio d'un client retrouve sa station par la MAC.
    verdict = radio("aa-00-00-00-00-02", trafic.radio_antennas())
    assert "AP-Nord" in str(verdict) and "-61" in str(verdict)

    faux_uisp.hors_ligne = True
    await trafic.collect()
    [p] = await trafic.problems()
    assert p["key"] == "uisp:ap-1" and p["level"] == "crit" and p["category"] == "uisp"

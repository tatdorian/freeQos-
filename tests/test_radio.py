"""Sante radio : AP et CPE lus sur airOS, et ce qui ne va pas, en clair."""

from __future__ import annotations

from typing import Any

from app.collectors.uisp import (
    AirOsProvider,
    AirOsTarget,
    parse_airos_radio,
    parse_airos_stations,
)
from app.services.radio import radio_issues, station_issues

STATUT_AP = {
    "host": {
        "hostname": "AP-Nord",
        "devmodel": "LiteAP AC",
        "fwversion": "v8.7.11",
        "uptime": 86400,
    },
    "wireless": {
        "mode": "ap-ptmp",
        "essid": "WISP",
        "frequency": 5620,
        "chanbw": 40,
        "signal": -52,
        "noisef": -92,
        "ccq": 96,
        "polling": {"use": 81},
        "txrate": 650,
        "rxrate": 585,
        "txcapacity": 310000,
        "rxcapacity": 290000,
        "count": 2,
        "distance": 150,
    },
}

STATIONS = [
    {
        "mac": "AA:00:00:00:00:01",
        "name": "client-1",
        "lastip": "10.20.0.7",
        "signal": -58,
        "noisefloor": -93,
        "ccq": 98,
        "tx": 390,
        "rx": 351,
        "distance": 2100,
        "remote": {"hostname": "client-1", "signal": -60, "platform": "NanoStation 5AC"},
    },
    {
        "mac": "AA:00:00:00:00:02",
        "name": "client-2",
        "lastip": "10.20.0.9",
        "signal": -79,
        "noisefloor": -90,
        "ccq": 61,
        "tx": 65,
        "rx": 58,
        "distance": 8400,
    },
]


def test_la_radio_d_une_ap_est_lue_avec_son_bruit() -> None:
    r = parse_airos_radio(STATUT_AP)
    assert r["noise_dbm"] == -92 and r["snr_db"] == 40
    assert r["frequency_mhz"] == 5620 and r["channel_width_mhz"] == 40
    assert r["airtime_pct"] == 81 and r["capacity_down_mbps"] == 310
    assert r["model"] == "LiteAP AC"


def test_une_ap_saturee_le_dit() -> None:
    assert any("saturated" in p for p in radio_issues(parse_airos_radio(STATUT_AP)))


def test_les_cpe_sont_lus_et_juges() -> None:
    bon, faible = parse_airos_stations(STATIONS)
    assert bon["ip"] == "10.20.0.7" and bon["snr_db"] == 35 and bon["remote_signal_dbm"] == -60
    assert bon["model"] == "NanoStation 5AC"
    assert station_issues(bon) == []
    problemes = station_issues(faible)
    assert any("weak signal -79" in p for p in problemes) and any("CCQ 61" in p for p in problemes)


def test_une_reponse_illisible_ne_casse_rien() -> None:
    assert parse_airos_stations({"pas": "une liste"}) == []
    assert parse_airos_stations([None, "x"]) == []


class ClientAirOs:
    def __init__(self, statut: dict[str, Any], stations: list[dict[str, Any]]) -> None:
        self.statut, self.stations, self.appels = statut, stations, []

    async def fetch_status(self) -> dict[str, Any]:
        self.appels.append("status")
        return self.statut

    async def fetch_stations(self) -> list[dict[str, Any]]:
        self.appels.append("stations")
        return self.stations

    async def aclose(self) -> None:
        return None


async def test_le_fournisseur_lit_les_cpe_des_ap_seulement() -> None:
    ap = ClientAirOs(STATUT_AP, STATIONS)
    cpe = ClientAirOs({"wireless": {"mode": "sta-ptmp", "signal": -60}}, [])
    clients = {"ap": ap, "cpe": cpe}
    fournisseur = AirOsProvider(
        [AirOsTarget(key="ap", host="10.0.0.2"), AirOsTarget(key="cpe", host="10.0.0.3")],
        client_factory=lambda t: clients[t.key],  # type: ignore[arg-type, return-value]
    )
    await fournisseur.get_capacities(["ap", "cpe"])
    assert ap.appels == ["status", "stations"]
    assert cpe.appels == ["status"]  # une station n'a personne sous elle
    vue = {a["key"]: a for a in fournisseur.radio_snapshot()}
    assert len(vue["ap"]["stations"]) == 2 and vue["ap"]["radio"]["snr_db"] == 40
    assert vue["cpe"]["stations"] == []

"""Ubiquiti UISP branche depuis l'interface : lecture des antennes, et capacite
des antennes rattachees a un site."""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors.uisp import DbUispSource, uisp_device_summary
from app.db.uisp_repo import InvalidUispError, check_base_url

APPAREIL = {
    "identification": {
        "id": "dev-1",
        "name": "BH-Plateau",
        "modelName": "airFiber 5XHD",
        "role": "ap",
        "mac": "AA:BB:CC:00:11:22",
        "site": {"name": "Plateau"},
    },
    "ipAddress": "10.10.0.30/24",
    "overview": {
        "status": "active",
        "downlinkCapacity": 450_000_000,
        "uplinkCapacity": 380_000_000,
        "signal": -54,
        "frequency": 5800,
    },
}


def test_une_antenne_uisp_se_lit_en_clair() -> None:
    fiche = uisp_device_summary(APPAREIL)
    assert fiche["id"] == "dev-1"
    assert fiche["name"] == "BH-Plateau"
    assert fiche["model"] == "airFiber 5XHD"
    assert fiche["site"] == "Plateau"
    assert fiche["capacity_down_mbps"] == 450
    assert fiche["capacity_up_mbps"] == 380
    assert fiche["signal_dbm"] == -54
    assert fiche["online"] is True


@pytest.mark.parametrize(
    ("saisie", "attendu"),
    [
        ("https://uisp.example.net/", "https://uisp.example.net"),
        ("https://uisp.example.net/nms/api/v2.1", "https://uisp.example.net"),
        ("http://10.0.0.5:8443/nms", "http://10.0.0.5:8443"),
    ],
)
def test_l_adresse_uisp_est_normalisee(saisie: str, attendu: str) -> None:
    assert check_base_url(saisie) == attendu


def test_une_adresse_sans_schema_passe_en_https() -> None:
    assert check_base_url("uisp.example.net") == "https://uisp.example.net"
    with pytest.raises(InvalidUispError):
        check_base_url("ftp://uisp.example.net")
    with pytest.raises(InvalidUispError):
        check_base_url("")


class DepotFaux:
    def __init__(self, configure: bool = True) -> None:
        self.configure = configure
        self.marques: list[dict[str, Any]] = []

    async def config(self) -> dict[str, Any] | None:
        return {"base_url": "https://uisp.test"} if self.configure else None

    async def credentials(self) -> tuple[str, str, bool] | None:
        return ("https://uisp.test", "jeton", True) if self.configure else None

    async def links(self) -> list[dict[str, Any]]:
        return [{"device_id": "dev-1", "name": "BH-Plateau", "pop_name": "Plateau"}]

    async def mark(self, **k: Any) -> None:
        self.marques.append(k)


async def test_les_antennes_rattachees_deviennent_des_backhauls() -> None:
    source = DbUispSource(DepotFaux())
    [config] = await source.backhaul_configs()
    assert (config.name, config.pop_name, config.uisp_device_id) == (
        "BH-Plateau",
        "Plateau",
        "dev-1",
    )
    # Sans UISP branche : rien, et surtout pas d'erreur.
    vide = DbUispSource(DepotFaux(configure=False))
    assert await vide.backhaul_configs() == []
    assert await vide.get_capacities(["dev-1"]) == {}

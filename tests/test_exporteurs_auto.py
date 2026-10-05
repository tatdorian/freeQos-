"""Un exporteur NetFlow qui est l'un de NOS routeurs se declare tout seul."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.collectors import mikrotik
from app.services.netflow_service import NetflowService, identify_exporter


def _routeur(nom: str, host: str, *, role: str = "pop", pop: str | None = None) -> Any:
    return SimpleNamespace(
        name=nom,
        config=SimpleNamespace(host=host, role=role, effective_pop_name=pop or nom),
    )


@pytest.fixture
def reseau(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    # DS-CCR sort vers MAIN GATEWAY (pas un de nos routeurs) ; les NAS sortent
    # vers DS-CCR. Les exporteurs emettent depuis leur loopback 11.11.11.x.
    monkeypatch.setattr(
        mikrotik,
        "_AMONTS",
        {"DS-CCR": ("100.100.100.254", "ether1"), "NAS-FRANCOPHONIE": ("11.11.11.1", "ether1")},
    )
    monkeypatch.setattr(
        mikrotik,
        "_PROPRIETAIRES",
        {"11.11.11.1": ("DS-CCR", "lo"), "11.11.11.75": ("NAS-FRANCOPHONIE", "lo")},
    )
    return [
        _routeur("DS-CCR", "100.100.100.1"),
        _routeur("NAS-FRANCOPHONIE", "100.100.101.82", pop="NAS-FRANCOPHONIE"),
    ]


def test_le_coeur_est_en_bordure_et_le_nas_au_pop(reseau: list[Any]) -> None:
    assert identify_exporter("11.11.11.1", reseau) == ("DS-CCR", "edge", "DS-CCR")
    assert identify_exporter("11.11.11.75", reseau) == (
        "NAS-FRANCOPHONIE",
        "pop",
        "NAS-FRANCOPHONIE",
    )
    # Par son adresse de connexion aussi.
    assert identify_exporter("100.100.101.82", reseau)[1] == "pop"


def test_un_role_coeur_declare_suffit(reseau: list[Any]) -> None:
    reseau[1].config.role = "core"
    assert identify_exporter("11.11.11.75", reseau)[1] == "edge"


def test_une_adresse_inconnue_n_est_pas_devinee(reseau: list[Any]) -> None:
    assert identify_exporter("203.0.113.9", reseau) is None


class Depot:
    def __init__(self, lignes: list[dict[str, Any]]) -> None:
        self.lignes = lignes
        self.maj: list[tuple[int, dict[str, Any]]] = []

    async def list_all(self) -> list[dict[str, Any]]:
        return self.lignes

    async def update(self, exporter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.maj.append((exporter_id, payload))
        return {}


async def test_seuls_les_inconnus_sont_declares(reseau: list[Any]) -> None:
    depot = Depot(
        [
            {"id": 1, "address": "11.11.11.1", "vantage": "unknown", "name": None},
            {"id": 2, "address": "11.11.11.75", "vantage": "pop", "name": "Mon NAS"},
            {"id": 3, "address": "203.0.113.9", "vantage": "unknown", "name": None},
        ]
    )
    service = NetflowService(exporters_repo=depot)
    service.identify_exporter = lambda a: identify_exporter(a, reseau)

    assert await service.auto_declare_exporters() == ["11.11.11.1"]
    [(ident, champs)] = depot.maj
    assert ident == 1
    assert champs["vantage"] == "edge" and champs["name"] == "DS-CCR"
    assert champs["pop_name"] is None

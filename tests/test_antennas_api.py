"""Gestion des antennes Ubiquiti depuis l'interface.

Ces endpoints ecrivent en BASE (quelles radios interroger sur leur API airOS),
jamais sur l'equipement. Ajouter une antenne suffit a la collecter, sans variable
d'environnement.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.uisp import AirOsClient
from app.config import Settings
from app.db.antennas_repo import AntennaNotFoundError, DuplicateAntennaError
from app.main import register_routes
from app.services.crypto import SecretBox, generate_key
from tests.test_api import build_container

PUBLIC = {
    "id",
    "name",
    "pop_name",
    "host",
    "username",
    "verify_tls",
    "device_key",
    "nominal_capacity_mbps",
    "enabled",
    "timeout_s",
    "last_ok_at",
    "last_error",
    "last_capacity_mbps",
}


class InMemoryAntennasRepository:
    """Meme contrat que la version PostgreSQL, sans base."""

    def __init__(self, secrets: SecretBox) -> None:
        self._secrets = secrets
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: row.get(k) for k in PUBLIC}

    async def list_public(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self.rows.values()]

    async def get_public(self, antenna_id: int) -> dict[str, Any]:
        if antenna_id not in self.rows:
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")
        return self._public(self.rows[antenna_id])

    async def create(self, payload: dict[str, Any], password: str | None) -> dict[str, Any]:
        if any(r["name"] == payload["name"] for r in self.rows.values()):
            raise DuplicateAntennaError(f"une antenne nommee '{payload['name']}' existe deja")
        antenna_id = self._next_id
        self._next_id += 1
        self.rows[antenna_id] = {
            "id": antenna_id,
            "password_enc": self._secrets.encrypt(password) if password else None,
            "last_ok_at": None,
            "last_error": None,
            "last_capacity_mbps": None,
            **payload,
        }
        return self._public(self.rows[antenna_id])

    async def update(
        self, antenna_id: int, payload: dict[str, Any], password: str | None = None
    ) -> dict[str, Any]:
        if antenna_id not in self.rows:
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")
        self.rows[antenna_id].update({k: v for k, v in payload.items() if v is not None})
        if password:
            self.rows[antenna_id]["password_enc"] = self._secrets.encrypt(password)
        return self._public(self.rows[antenna_id])

    async def delete(self, antenna_id: int) -> None:
        if self.rows.pop(antenna_id, None) is None:
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")

    async def record_success(self, antenna_id: int, capacity_mbps: float | None) -> None:
        self.rows[antenna_id].update({"last_ok_at": "now", "last_capacity_mbps": capacity_mbps})

    async def record_failure(self, antenna_id: int, error: str) -> None:
        self.rows[antenna_id]["last_error"] = error

    async def load_targets(self, *, enabled_only: bool = True):
        from app.collectors.uisp import AirOsTarget

        targets = []
        for row in self.rows.values():
            if enabled_only and not row.get("enabled", True):
                continue
            targets.append(
                AirOsTarget(
                    key=row.get("device_key") or row["name"],
                    host=row["host"],
                    username=row.get("username", "ubnt"),
                    password=self._secrets.decrypt(row["password_enc"])
                    if row["password_enc"]
                    else "",
                    verify_tls=row.get("verify_tls", False),
                )
            )
        return targets


NOUVELLE = {
    "name": "bh-nord",
    "pop_name": "PoP Nord",
    "host": "10.10.0.30",
    "username": "ubnt",
    "password": "secret-antenne",
    "device_key": "DC:9F:DB:11:22:33",
    "nominal_capacity_mbps": 300,
}


@pytest.fixture(autouse=True)
def _fake_airos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toute lecture airOS tape un faux transport : aucune vraie radio."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "host": {"hwaddr": "DC:9F:DB:11:22:33"},
                "wireless": {"signal": -55, "txcapacity": 280000, "rxcapacity": 260000},
            },
        )

    original = AirOsClient.__init__

    def patched(self, target, *, timeout_s=10.0, client=None):  # type: ignore[no-untyped-def]
        original(
            self,
            target,
            timeout_s=timeout_s,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=f"https://{target.host}"
            ),
        )

    monkeypatch.setattr(AirOsClient, "__init__", patched)


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox(generate_key())


@pytest.fixture
def repo(secrets: SecretBox) -> InMemoryAntennasRepository:
    return InMemoryAntennasRepository(secrets)


@pytest.fixture
def client(settings: Settings, secrets: SecretBox, repo: InMemoryAntennasRepository) -> TestClient:
    container = build_container(settings, secrets=secrets, antennas_repo=repo)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    test_client = TestClient(app)
    test_client.container = container  # type: ignore[attr-defined]
    return test_client


def test_creation_puis_liste(client: TestClient) -> None:
    cree = client.post("/api/v1/pops/antennas", json=NOUVELLE)
    assert cree.status_code == 201
    assert cree.json()["name"] == "bh-nord"

    liste = client.get("/api/v1/pops/antennas").json()
    assert [a["name"] for a in liste["antennas"]] == ["bh-nord"]


def test_le_mot_de_passe_ne_ressort_jamais(client: TestClient) -> None:
    cree = client.post("/api/v1/pops/antennas", json=NOUVELLE)
    liste = client.get("/api/v1/pops/antennas")
    for corps in (cree.text, liste.text):
        assert "secret-antenne" not in corps
        assert "password" not in corps.lower()


def test_le_mot_de_passe_est_chiffre_en_base(
    client: TestClient, repo: InMemoryAntennasRepository
) -> None:
    client.post("/api/v1/pops/antennas", json=NOUVELLE)
    stocke = repo.rows[1]["password_enc"]
    assert stocke and "secret-antenne" not in stocke


def test_antenne_sans_mot_de_passe_acceptee(client: TestClient) -> None:
    """Beaucoup de radios laissent /status.cgi ouvert : pas de secret exige."""
    sans_secret = {k: v for k, v in NOUVELLE.items() if k != "password"}
    reponse = client.post("/api/v1/pops/antennas", json=sans_secret)
    assert reponse.status_code == 201


def test_test_de_connexion_lit_la_capacite(client: TestClient) -> None:
    reponse = client.post("/api/v1/pops/antennas/test", json=NOUVELLE)
    corps = reponse.json()
    assert corps["reachable"] is True
    # min(280, 260) : la capacite utile est bornee par le sens le plus faible.
    assert corps["capacity_mbps"] == 260.0
    assert corps["mac"] == "DC:9F:DB:11:22:33"


def test_probe_enregistre_le_diagnostic(
    client: TestClient, repo: InMemoryAntennasRepository
) -> None:
    client.post("/api/v1/pops/antennas", json=NOUVELLE)
    reponse = client.post("/api/v1/pops/antennas/1/probe")
    assert reponse.json()["reachable"] is True
    assert repo.rows[1]["last_capacity_mbps"] == 260.0


def test_suppression(client: TestClient) -> None:
    client.post("/api/v1/pops/antennas", json=NOUVELLE)
    assert client.delete("/api/v1/pops/antennas/1").status_code == 204
    assert client.get("/api/v1/pops/antennas").json()["antennas"] == []


def test_nom_en_double_refuse(client: TestClient) -> None:
    client.post("/api/v1/pops/antennas", json=NOUVELLE)
    doublon = client.post("/api/v1/pops/antennas", json=NOUVELLE)
    assert doublon.status_code == 409

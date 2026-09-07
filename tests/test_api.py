"""API REST montee sur un conteneur factice : ni PostgreSQL ni routeur."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.radius import MockPlanProvider
from app.collectors.uisp import MockBackhaulProvider
from app.config import Settings
from app.container import Container
from app.db.directory import InMemoryDirectory
from app.db.writer import InMemoryMetricsWriter
from app.main import register_routes
from app.scheduler import Scheduler
from app.services.collection import JOB_SUBSCRIBERS, CollectionService
from tests.conftest import FakeRouterOsClient

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class FakeDatabase:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.timescale_available = True

    async def ping(self) -> bool:
        return self.reachable


class FakeRepository:
    """Repond ce que repondrait TimescaleDB, sans TimescaleDB."""

    async def list_pops(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "name": "PoP Test",
                "router_host": "192.0.2.11",
                "created_at": NOW,
                "subscriber_count": 1,
                "backhaul_count": 1,
            }
        ]

    async def list_subscribers(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "pppoe_login": "dupont",
                "pop_id": 1,
                "pop_name": "PoP Test",
                "plan_down_mbps": 100.0,
                "plan_up_mbps": 20.0,
                "plan_source": "mock",
                "last_ip": "10.20.0.10",
                "last_seen": NOW,
            }
        ]

    async def get_subscriber(self, subscriber_id: int) -> dict[str, Any] | None:
        if subscriber_id != 1:
            return None
        return (await self.list_subscribers())[0]

    async def subscriber_metrics(self, subscriber_id: int, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "bucket": NOW,
                "rx_bps_avg": 5_000_000.0,
                "tx_bps_avg": 40_000_000.0,
                "rx_bps_max": 8_000_000.0,
                "tx_bps_max": 95_000_000.0,
                "rtt_ms_avg": None,
                "rtt_ms_max": None,
                "samples": 6,
            }
        ]

    async def subscriber_latest(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "subscriber_id": 1,
                "pppoe_login": "dupont",
                "pop_id": 1,
                "pop_name": "PoP Test",
                "plan_down_mbps": 100.0,
                "plan_up_mbps": 20.0,
                "ts": NOW,
                "rx_bps": 5_000_000.0,
                "tx_bps": 40_000_000.0,
                "rtt_ms": None,
                "session_uptime_s": 3600,
            }
        ]

    async def list_backhauls(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "name": "bh-test",
                "pop_id": 1,
                "pop_name": "PoP Test",
                "uisp_device_id": "device-1",
                "nominal_capacity_mbps": 500.0,
            }
        ]

    async def backhaul_latest(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "backhaul_id": 1,
                "name": "bh-test",
                "pop_id": 1,
                "pop_name": "PoP Test",
                "uisp_device_id": "device-1",
                "nominal_capacity_mbps": 500.0,
                "ts": NOW,
                "capacity_mbps": 420.0,
                "capacity_down_mbps": 315.0,
                "capacity_up_mbps": 105.0,
                "signal_dbm": -52.0,
                "airtime_pct": 33.0,
                "online": True,
            }
        ]

    async def backhaul_metrics(self, backhaul_id: int, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "bucket": NOW,
                "capacity_mbps_avg": 420.0,
                "capacity_mbps_min": 380.0,
                "capacity_mbps_max": 460.0,
                "signal_dbm_avg": -52.0,
                "signal_dbm_min": -58.0,
                "airtime_pct_avg": 33.0,
                "samples": 2,
            }
        ]

    async def recent_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "job": JOB_SUBSCRIBERS,
                "started_at": NOW,
                "duration_s": 0.12,
                "ok": True,
                "items": 1,
                "error": None,
            }
        ]

    async def counters(self) -> dict[str, Any]:
        return {
            "pops": 1,
            "subscribers": 1,
            "backhauls": 1,
            "last_subscriber_metric": NOW,
            "last_backhaul_metric": NOW,
            "active_subscribers": 1,
        }


def build_container(settings: Settings, *, db_reachable: bool = True) -> Container:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=1000, tx_byte=2000)
    collectors = [MikrotikCollector(cfg, client=client) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    directory = InMemoryDirectory()
    plan_provider = MockPlanProvider()
    backhaul_provider = MockBackhaulProvider()
    collection = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=backhaul_provider,
        plan_provider=plan_provider,
        directory=directory,
        writer=writer,
    )
    scheduler = Scheduler()
    scheduler.add_job(JOB_SUBSCRIBERS, 10, collection.collect_subscribers)
    return Container(
        settings=settings,
        database=FakeDatabase(reachable=db_reachable),  # type: ignore[arg-type]
        writer=writer,
        repository=FakeRepository(),  # type: ignore[arg-type]
        directory=directory,
        plan_provider=plan_provider,
        backhaul_provider=backhaul_provider,
        collection=collection,
        scheduler=scheduler,
    )


@pytest.fixture
def container(settings: Settings) -> Container:
    return build_container(settings)


@pytest.fixture
def client(settings: Settings, container: Container) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


# ------------------------------------------------------------------- sante
def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_ok(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert body["routers_configured"] == 1
    # Garde-fou hors-bande visible dans la sonde.
    assert body["enforcement_enabled"] is False


def test_readiness_degradee_si_base_injoignable(settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: build_container(settings, db_reachable=False)
    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


# ---------------------------------------------------------------- metriques
def test_liste_des_pops(client: TestClient) -> None:
    body = client.get("/api/v1/pops").json()
    assert body[0]["name"] == "PoP Test"


def test_liste_des_abonnes(client: TestClient) -> None:
    body = client.get("/api/v1/subscribers").json()
    assert body[0]["pppoe_login"] == "dupont"


def test_top_talkers(client: TestClient) -> None:
    """La route /subscribers/latest doit primer sur /subscribers/{id}."""
    response = client.get("/api/v1/subscribers/latest")
    assert response.status_code == 200
    assert response.json()[0]["pppoe_login"] == "dupont"


def test_fiche_abonne_inconnu(client: TestClient) -> None:
    assert client.get("/api/v1/subscribers/999").status_code == 404


def test_serie_d_un_abonne(client: TestClient) -> None:
    response = client.get("/api/v1/subscribers/1/metrics?minutes=30&bucket_seconds=60")
    assert response.status_code == 200
    body = response.json()
    assert body["bucket_seconds"] == 60
    assert body["points"][0]["tx_bps_avg"] == 40_000_000.0
    # La convention de sens est rappelee dans la reponse.
    assert "upload abonne" in body["orientation"]


def test_serie_fenetre_incoherente(client: TestClient) -> None:
    start = NOW.isoformat()
    end = (NOW - timedelta(hours=1)).isoformat()
    response = client.get(f"/api/v1/subscribers/1/metrics?start={start}&end={end}")
    assert response.status_code == 422


def test_backhauls(client: TestClient) -> None:
    assert client.get("/api/v1/backhauls").json()[0]["name"] == "bh-test"
    assert client.get("/api/v1/backhauls/latest").json()[0]["capacity_mbps"] == 420.0
    body = client.get("/api/v1/backhauls/1/metrics?minutes=60").json()
    assert body["points"][0]["capacity_mbps_min"] == 380.0


# ------------------------------------------------------------- exploitation
def test_status(client: TestClient) -> None:
    body = client.get("/api/v1/status").json()
    assert body["mode"] == "out-of-band (lecture seule)"
    assert body["enforcement_enabled"] is False
    assert body["routers"][0]["username"] == "qos-ro"
    assert body["providers"] == {"backhaul": "mock", "plans": "mock"}


def test_status_ne_divulgue_aucun_secret(client: TestClient) -> None:
    """Aucun mot de passe ne doit transiter par l'API."""
    body = client.get("/api/v1/status").text
    assert "secret-de-lab" not in body
    assert "password" not in body.lower()


def test_declenchement_manuel_d_un_job(client: TestClient) -> None:
    response = client.post(f"/api/v1/jobs/{JOB_SUBSCRIBERS}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["items"] == 1


def test_declenchement_d_un_job_inconnu(client: TestClient) -> None:
    assert client.post("/api/v1/jobs/inexistant/run").status_code == 404


def test_historique_et_compteurs(client: TestClient) -> None:
    assert client.get("/api/v1/status/runs").json()[0]["ok"] is True
    assert client.get("/api/v1/status/counters").json()["subscribers"] == 1


# ---------------------------------------------------------------------- UI
def test_dashboard_se_rend_sans_dependance_externe(client: TestClient) -> None:
    html = client.get("/").text
    assert "freeQoS" in html
    # Un controleur souverain ne doit pas dependre d'un CDN pour s'afficher.
    assert "http://" not in html and "https://" not in html


def test_fragments_ui(client: TestClient) -> None:
    assert "dupont" in client.get("/ui/fragments/subscribers").text
    assert "bh-test" in client.get("/ui/fragments/backhauls").text
    assert JOB_SUBSCRIBERS in client.get("/ui/fragments/overview").text


def test_api_non_initialisee_repond_503(settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    assert TestClient(app).get("/api/v1/pops").status_code == 503

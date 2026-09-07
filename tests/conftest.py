"""Doubles de test partages.

Objectif : toute la logique du controleur doit etre testable sans PostgreSQL,
sans routeur et sans radio. Seuls les modules d'acces (PgMetricsWriter,
PgDirectory, MetricsRepository) supposent une vraie base ; ils sont couverts par
les tests d'integration optionnels.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors.mikrotik import MikrotikCollector
from app.config import BackhaulConfig, RouterConfig, Settings


class FakeRouterOsClient:
    """Faux routeur RouterOS : renvoie les tables /ppp/active et /interface.

    Les valeurs imitent la reponse reelle de l'API binaire, y compris les cles a
    tirets et les compteurs sous forme de chaines.
    """

    def __init__(
        self,
        active: list[dict[str, Any]] | None = None,
        interfaces: list[dict[str, Any]] | None = None,
        *,
        identity: str = "fake-chr",
    ) -> None:
        self.active = active if active is not None else []
        self.interfaces_rows = interfaces if interfaces is not None else []
        self._identity = identity
        self.calls = 0
        self.closed = False
        self.raise_on_ppp: Exception | None = None

    def ppp_active(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.raise_on_ppp is not None:
            raise self.raise_on_ppp
        return [dict(row) for row in self.active]

    def interfaces(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.interfaces_rows]

    def identity(self) -> str | None:
        return self._identity

    def close(self) -> None:
        self.closed = True

    # --- aides de scenario -------------------------------------------------
    def add_session(
        self,
        login: str,
        *,
        address: str = "10.20.0.10",
        uptime: str = "01:00:00",
        rx_byte: int = 0,
        tx_byte: int = 0,
        interface_name: str | None = None,
    ) -> None:
        self.active.append(
            {
                ".id": f"*{len(self.active) + 1:X}",
                "name": login,
                "service": "pppoe",
                "caller-id": "AA:BB:CC:DD:EE:FF",
                "address": address,
                "uptime": uptime,
                "session-id": f"0x{len(self.active) + 1:08X}",
            }
        )
        self.interfaces_rows.append(
            {
                ".id": f"*{len(self.interfaces_rows) + 100:X}",
                "name": interface_name or f"<pppoe-{login}>",
                "type": "pppoe-in",
                "running": "true",
                "rx-byte": str(rx_byte),
                "tx-byte": str(tx_byte),
            }
        )

    def advance(
        self, login: str, *, rx_delta: int, tx_delta: int, uptime: str | None = None
    ) -> None:
        """Simule l'ecoulement du temps : les compteurs avancent."""
        for row in self.interfaces_rows:
            if login in str(row.get("name", "")):
                row["rx-byte"] = str(int(row["rx-byte"]) + rx_delta)
                row["tx-byte"] = str(int(row["tx-byte"]) + tx_delta)
        if uptime is not None:
            for row in self.active:
                if row.get("name") == login:
                    row["uptime"] = uptime

    def restart_session(self, login: str, *, uptime: str = "00:00:05") -> None:
        """Simule une reconnexion PPPoE : compteurs remis a zero."""
        for row in self.interfaces_rows:
            if login in str(row.get("name", "")):
                row["rx-byte"] = "0"
                row["tx-byte"] = "0"
        for row in self.active:
            if row.get("name") == login:
                row["uptime"] = uptime


@pytest.fixture
def router_config() -> RouterConfig:
    return RouterConfig(
        name="pop-test",
        host="192.0.2.11",
        username="qos-ro",
        password="secret-de-lab",
        pop_name="PoP Test",
    )


@pytest.fixture
def fake_client() -> FakeRouterOsClient:
    return FakeRouterOsClient()


@pytest.fixture
def collector(router_config: RouterConfig, fake_client: FakeRouterOsClient) -> MikrotikCollector:
    return MikrotikCollector(router_config, client=fake_client)


@pytest.fixture
def settings(router_config: RouterConfig) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        routers=[router_config],
        backhauls=[
            BackhaulConfig(
                name="bh-test",
                pop_name="PoP Test",
                uisp_device_id="device-1",
                nominal_capacity_mbps=500,
            )
        ],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=False,
        subscriber_interval_s=10,
        min_rate_interval_s=1.0,
    )

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
        self.pings: list[tuple[str, int]] = []
        # Topologie et files, pour la phase 2.
        self.neighbor_rows: list[dict[str, Any]] = []
        self.ethernet_rows: list[dict[str, Any]] = []
        self.address_rows: list[dict[str, Any]] = []
        self.simple_queue_rows: list[dict[str, Any]] = []
        self.queue_type_rows: list[dict[str, Any]] = []
        self.queue_tree_rows: list[dict[str, Any]] = []
        self.raise_on_neighbors: Exception | None = None
        self.raise_on_queues: Exception | None = None
        # Par defaut : compte de lecture seule, comme qos-ro.
        self.user_rows: list[dict[str, Any]] = [{"name": "qos-ro", "group": "qos-ro"}]
        self.group_rows: list[dict[str, Any]] = [{"name": "qos-ro", "policy": "read,api,test"}]
        self.raise_on_users: Exception | None = None
        # Debit instantane rendu par /interface/monitor-traffic, par interface.
        self.monitor_rates: dict[str, tuple[float, float]] = {}
        self.raise_on_monitor: Exception | None = None
        self.ping_reply: str | None = "12ms"
        self.ping_error: Exception | None = None
        # Config complete facon /export (texte). Vide par defaut.
        self.export_text: str = ""

    def ppp_active(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.raise_on_ppp is not None:
            raise self.raise_on_ppp
        return [dict(row) for row in self.active]

    def interfaces(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.interfaces_rows]

    def identity(self) -> str | None:
        return self._identity

    def system_resource(self) -> dict[str, Any]:
        return {
            "version": "7.21.5 (stable)",
            "board-name": "CHR",
            "uptime": "1w2d03:04:05",
            "cpu-load": "3",
            "free-memory": "201326592",
        }

    # --- Topologie et files (phase 2) ---
    def neighbors(self) -> list[dict[str, Any]]:
        if self.raise_on_neighbors is not None:
            raise self.raise_on_neighbors
        return [dict(row) for row in self.neighbor_rows]

    def ethernet(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ethernet_rows]

    def monitor_traffic(self, interface: str) -> dict[str, Any]:
        if self.raise_on_monitor is not None:
            raise self.raise_on_monitor
        rx, tx = self.monitor_rates.get(interface, (0.0, 0.0))
        return {
            "name": interface,
            "rx-bits-per-second": str(int(rx)),
            "tx-bits-per-second": str(int(tx)),
            "rx-packets-per-second": "1200",
            "tx-packets-per-second": "9800",
        }

    def addresses(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.address_rows]

    def export_config(self) -> str:
        return self.export_text

    def simple_queues(self) -> list[dict[str, Any]]:
        if self.raise_on_queues is not None:
            raise self.raise_on_queues
        return [dict(row) for row in self.simple_queue_rows]

    def queue_types(self) -> list[dict[str, Any]]:
        if self.raise_on_queues is not None:
            raise self.raise_on_queues
        return [dict(row) for row in self.queue_type_rows]

    def queue_trees(self) -> list[dict[str, Any]]:
        if self.raise_on_queues is not None:
            raise self.raise_on_queues
        return [dict(row) for row in self.queue_tree_rows]

    def users(self) -> list[dict[str, Any]]:
        if self.raise_on_users is not None:
            raise self.raise_on_users
        return [dict(row) for row in self.user_rows]

    def user_groups(self) -> list[dict[str, Any]]:
        if self.raise_on_users is not None:
            raise self.raise_on_users
        return [dict(row) for row in self.group_rows]

    def grant_write(self, username: str = "qos-ro") -> None:
        """Donne les droits d'ecriture a ce compte, comme le ferait un
        exploitant qui se connecte deja avec un compte complet."""
        self.user_rows = [{"name": username, "group": "full"}]
        self.group_rows = [{"name": "full", "policy": "read,write,api,test,policy"}]

    def ping(self, address: str, count: int = 1) -> list[dict[str, Any]]:
        self.pings.append((address, count))
        if self.ping_error is not None:
            raise self.ping_error
        if self.ping_reply is None:
            # Abonne silencieux : RouterOS renvoie des lignes sans champ 'time'.
            return [{"seq": str(i), "host": address} for i in range(count)]
        return [
            {"seq": str(i), "host": address, "time": self.ping_reply, "ttl": "64"}
            for i in range(count)
        ]

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

    def add_interface(
        self,
        name: str,
        *,
        kind: str = "ether",
        rx_byte: int = 0,
        tx_byte: int = 0,
        speed: str | None = "1Gbps",
        running: str = "true",
    ) -> None:
        """Ajoute un PORT physique : c'est lui qui porte le debit d'un lien."""
        self.interfaces_rows.append(
            {
                ".id": f"*{len(self.interfaces_rows) + 200:X}",
                "name": name,
                "type": kind,
                "running": running,
                "rx-byte": str(rx_byte),
                "tx-byte": str(tx_byte),
            }
        )
        if speed is not None:
            self.ethernet_rows.append({"name": name, "speed": speed})

    def advance_interface(self, name: str, *, rx_delta: int, tx_delta: int) -> None:
        for row in self.interfaces_rows:
            if row.get("name") == name:
                row["rx-byte"] = str(int(row["rx-byte"]) + rx_delta)
                row["tx-byte"] = str(int(row["tx-byte"]) + tx_delta)

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

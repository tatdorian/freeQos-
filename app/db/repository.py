"""Lectures pour l'API.

Les agregations utilisent ``date_bin`` (PostgreSQL >= 14) plutot que
``time_bucket`` : le resultat est identique sur les hypertables, mais les memes
requetes fonctionnent aussi sur un PostgreSQL sans Timescale (integration
continue, poste de dev).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import asyncpg


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(record) for record in records]


class MetricsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ PoPs
    async def list_pops(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT p.id, p.name, p.router_host, p.created_at,
                       (SELECT count(*) FROM subscribers s WHERE s.pop_id = p.id)
                           AS subscriber_count,
                       (SELECT count(*) FROM backhauls b WHERE b.pop_id = p.id)
                           AS backhaul_count
                  FROM pops p
                 ORDER BY p.name
                """
            )
        return _rows(records)

    # ------------------------------------------------------------- Abonnes
    async def list_subscribers(
        self,
        *,
        pop_id: int | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT s.id, s.pppoe_login, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE ($1::int IS NULL OR s.pop_id = $1)
                   AND ($2::text IS NULL OR s.pppoe_login ILIKE '%' || $2 || '%')
                 ORDER BY s.pppoe_login
                 LIMIT $3 OFFSET $4
                """,
                pop_id,
                search,
                limit,
                offset,
            )
        return _rows(records)

    async def get_subscriber(self, subscriber_id: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT s.id, s.pppoe_login, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen, s.created_at
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE s.id = $1
                """,
                subscriber_id,
            )
        return dict(record) if record else None

    async def get_subscriber_by_login(self, login: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow("SELECT id FROM subscribers WHERE pppoe_login = $1", login)
        return dict(record) if record else None

    async def subscriber_metrics(
        self,
        subscriber_id: int,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Serie agregee. On prend le max sur le bucket pour les debits :
        une moyenne masquerait les pics de saturation, qui sont justement le
        signal interessant pour la QoE."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(rx_bps) AS rx_bps_avg,
                       avg(tx_bps) AS tx_bps_avg,
                       max(rx_bps) AS rx_bps_max,
                       max(tx_bps) AS tx_bps_max,
                       avg(rtt_ms) AS rtt_ms_avg,
                       max(rtt_ms) AS rtt_ms_max,
                       count(*)    AS samples
                  FROM subscriber_metrics
                 WHERE subscriber_id = $1 AND ts >= $2 AND ts < $3
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                subscriber_id,
                start,
                end,
                timedelta(seconds=bucket_seconds),
            )
        return _rows(records)

    async def subscriber_latest(
        self, *, pop_id: int | None = None, limit: int = 50, order_by: str = "total"
    ) -> list[dict[str, Any]]:
        """Dernier echantillon par abonne, trie par debit (top talkers)."""
        order_sql = {
            "total": "COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0) DESC",
            "down": "COALESCE(tx_bps, 0) DESC",
            "up": "COALESCE(rx_bps, 0) DESC",
            "login": "pppoe_login ASC",
        }.get(order_by, "COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0) DESC")
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                f"""
                SELECT * FROM subscriber_latest
                 WHERE ($1::int IS NULL OR pop_id = $1)
                 ORDER BY {order_sql}
                 LIMIT $2
                """,  # noqa: S608 - order_sql provient d'une liste blanche
                pop_id,
                limit,
            )
        return _rows(records)

    # ------------------------------------------------------------ Backhauls
    async def list_backhauls(self, *, pop_id: int | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT b.id, b.name, b.pop_id, p.name AS pop_name, b.uisp_device_id,
                       b.nominal_capacity_mbps
                  FROM backhauls b
                  LEFT JOIN pops p ON p.id = b.pop_id
                 WHERE ($1::int IS NULL OR b.pop_id = $1)
                 ORDER BY b.name
                """,
                pop_id,
            )
        return _rows(records)

    async def backhaul_latest(self, *, pop_id: int | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT * FROM backhaul_latest
                 WHERE ($1::int IS NULL OR pop_id = $1)
                 ORDER BY name
                """,
                pop_id,
            )
        return _rows(records)

    async def backhaul_metrics(
        self,
        backhaul_id: int,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Pour la capacite radio on prend aussi le MIN : c'est le creux de capacite
        (fade) qui contraint le debit parent du shaping, pas la moyenne."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(capacity_mbps) AS capacity_mbps_avg,
                       min(capacity_mbps) AS capacity_mbps_min,
                       max(capacity_mbps) AS capacity_mbps_max,
                       avg(signal_dbm)    AS signal_dbm_avg,
                       min(signal_dbm)    AS signal_dbm_min,
                       avg(airtime_pct)   AS airtime_pct_avg,
                       count(*)           AS samples
                  FROM backhaul_metrics
                 WHERE backhaul_id = $1 AND ts >= $2 AND ts < $3
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                backhaul_id,
                start,
                end,
                timedelta(seconds=bucket_seconds),
            )
        return _rows(records)

    # ----------------------------------------------------------- Exploitation
    async def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT job, started_at, duration_s, ok, items, error
                  FROM collector_runs
                 ORDER BY started_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return _rows(records)

    async def counters(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM pops)        AS pops,
                       (SELECT count(*) FROM subscribers) AS subscribers,
                       (SELECT count(*) FROM backhauls)   AS backhauls,
                       (SELECT max(ts) FROM subscriber_metrics) AS last_subscriber_metric,
                       (SELECT max(ts) FROM backhaul_metrics)   AS last_backhaul_metric,
                       (SELECT count(*) FROM subscribers
                         WHERE last_seen > now() - INTERVAL '5 minutes') AS active_subscribers
                """
            )
        return dict(record) if record else {}

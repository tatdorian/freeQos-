"""Ecriture des series temporelles.

L'interface ``MetricsWriter`` permet de substituer un double memoire dans les
tests : toute la logique de collecte est ainsi testable sans PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import asyncpg

from app.models import BackhaulSample, RunResult, SubscriberSample

logger = logging.getLogger(__name__)


@runtime_checkable
class MetricsWriter(Protocol):
    async def write_subscriber_metrics(
        self, rows: Sequence[tuple[int, SubscriberSample]]
    ) -> int: ...

    async def write_backhaul_metrics(self, rows: Sequence[tuple[int, BackhaulSample]]) -> int: ...

    async def record_run(self, result: RunResult) -> None: ...


class PgMetricsWriter:
    """Ecriture par lots dans TimescaleDB.

    ``executemany`` + ``ON CONFLICT DO NOTHING`` plutot que COPY : a 10 s d'intervalle
    et quelques milliers d'abonnes le volume reste modeste, et l'idempotence protege
    d'un double passage (cycle rejoue, horloge qui recule). Passer a
    ``copy_records_to_table`` sera trivial si le volume l'impose.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write_subscriber_metrics(self, rows: Sequence[tuple[int, SubscriberSample]]) -> int:
        if not rows:
            return 0
        payload = [
            (
                sample.ts,
                subscriber_id,
                sample.rx_bps,
                sample.tx_bps,
                sample.rx_bytes,
                sample.tx_bytes,
                sample.rtt_ms,
                sample.uptime_s,
            )
            for subscriber_id, sample in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO subscriber_metrics
                       (ts, subscriber_id, rx_bps, tx_bps, rx_bytes, tx_bytes,
                        rtt_ms, session_uptime_s)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (subscriber_id, ts) DO NOTHING
                """,
                payload,
            )
        return len(payload)

    async def write_backhaul_metrics(self, rows: Sequence[tuple[int, BackhaulSample]]) -> int:
        if not rows:
            return 0
        payload = [
            (
                sample.ts,
                backhaul_id,
                sample.capacity_mbps,
                sample.capacity_down_mbps,
                sample.capacity_up_mbps,
                sample.signal_dbm,
                sample.airtime_pct,
                sample.mcs_down,
                sample.mcs_up,
                sample.online,
            )
            for backhaul_id, sample in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO backhaul_metrics
                       (ts, backhaul_id, capacity_mbps, capacity_down_mbps,
                        capacity_up_mbps, signal_dbm, airtime_pct, mcs_down, mcs_up, online)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (backhaul_id, ts) DO NOTHING
                """,
                payload,
            )
        return len(payload)

    async def record_run(self, result: RunResult) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector_runs (job, started_at, duration_s, ok, items, error)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                result.job,
                result.started_at,
                result.duration_s,
                result.ok,
                result.items,
                result.error_text,
            )


class InMemoryMetricsWriter:
    """Double memoire : utilise par les tests et par le mode sans base."""

    def __init__(self) -> None:
        self.subscriber_rows: list[tuple[int, SubscriberSample]] = []
        self.backhaul_rows: list[tuple[int, BackhaulSample]] = []
        self.runs: list[RunResult] = []

    async def write_subscriber_metrics(self, rows: Sequence[tuple[int, SubscriberSample]]) -> int:
        self.subscriber_rows.extend(rows)
        return len(rows)

    async def write_backhaul_metrics(self, rows: Sequence[tuple[int, BackhaulSample]]) -> int:
        self.backhaul_rows.extend(rows)
        return len(rows)

    async def record_run(self, result: RunResult) -> None:
        self.runs.append(result)

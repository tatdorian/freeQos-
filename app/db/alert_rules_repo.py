"""Les alertes personnelles (table ``alert_rules``) et leur etat."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any

import asyncpg

COLUMNS = (
    "id, owner_email, name, kind, categories, sites, targets, threshold, delay_minutes, "
    "notify_resolved, enabled, last_sent_at, last_error, created_at, updated_at"
)
CHAMPS = (
    "name",
    "kind",
    "categories",
    "sites",
    "targets",
    "threshold",
    "delay_minutes",
    "notify_resolved",
    "enabled",
)


class AlertRuleNotFoundError(LookupError):
    pass


class AlertRulesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list(self, owner: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM alert_rules "  # noqa: S608
                "WHERE ($1::text IS NULL OR lower(owner_email) = lower($1)) "
                "ORDER BY lower(owner_email), lower(name)",
                owner,
            )
        return [dict(r) for r in rows]

    async def get(self, ident: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM alert_rules WHERE id = $1",  # noqa: S608
                ident,
            )
        if row is None:
            raise AlertRuleNotFoundError(f"no alert {ident}")
        return dict(row)

    async def create(self, owner: str, valeurs: dict[str, Any]) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO alert_rules (owner_email, name, kind, categories, sites, targets,
                                         threshold, delay_minutes, notify_resolved, enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING {COLUMNS}
                """,  # noqa: S608
                owner,
                *(valeurs[c] for c in CHAMPS),
            )
        return dict(row)

    async def update(self, ident: int, valeurs: dict[str, Any]) -> dict[str, Any]:
        affectations = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(CHAMPS))
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE alert_rules SET {affectations}, updated_at = now() "  # noqa: S608
                f"WHERE id = $1 RETURNING {COLUMNS}",
                ident,
                *(valeurs[c] for c in CHAMPS),
            )
            # Ce qu'elle surveille a change : on repart de zero.
            await conn.execute("DELETE FROM alert_rule_events WHERE rule_id = $1", ident)
        if row is None:
            raise AlertRuleNotFoundError(f"no alert {ident}")
        return dict(row)

    async def delete(self, ident: int) -> None:
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM alert_rules WHERE id = $1", ident)
        if str(res).endswith(" 0"):
            raise AlertRuleNotFoundError(f"no alert {ident}")

    async def mark(self, ident: int, *, ok: bool, error: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE alert_rules SET last_sent_at = CASE WHEN $2 THEN now() "
                "ELSE last_sent_at END, last_error = $3 WHERE id = $1",
                ident,
                ok,
                None if ok else error,
            )

    # ------------------------------------------------------------ etat
    async def events(self, ident: int) -> dict[str, dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM alert_rule_events WHERE rule_id = $1", ident)
        return {str(r["key"]): dict(r) for r in rows}

    async def open_counts(self) -> dict[int, int]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT rule_id, count(*) AS n FROM alert_rule_events GROUP BY rule_id"
            )
        return {int(r["rule_id"]): int(r["n"]) for r in rows}

    async def upsert_event(self, ident: int, probleme: dict[str, Any], now: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert_rule_events (rule_id, key, level, title, detail,
                                               first_seen, last_seen)
                VALUES ($1, $2, $3, $4, $5, $6, $6)
                ON CONFLICT (rule_id, key) DO UPDATE SET level = EXCLUDED.level,
                    title = EXCLUDED.title, detail = EXCLUDED.detail, last_seen = EXCLUDED.last_seen
                """,
                ident,
                str(probleme["key"]),
                str(probleme.get("level") or "warn"),
                str(probleme["title"]),
                probleme.get("detail"),
                now,
            )

    async def mark_notified(self, ident: int, keys: builtins.list[str], now: datetime) -> None:
        if not keys:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE alert_rule_events SET notified_at = $3 "
                "WHERE rule_id = $1 AND key = ANY($2::text[])",
                ident,
                keys,
                now,
            )

    async def forget(self, ident: int, keys: builtins.list[str]) -> None:
        if not keys:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_rule_events WHERE rule_id = $1 AND key = ANY($2::text[])",
                ident,
                keys,
            )

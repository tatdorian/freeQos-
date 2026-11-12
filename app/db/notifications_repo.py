"""Compte SMTP et etat des problemes surveilles (cf. app.services.health_watch)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg

from app.services.crypto import SecretBox

logger = logging.getLogger(__name__)

CATEGORIES = ("router", "site", "measurement", "uisp", "limit")
_CHAMPS = (
    "host, port, security, username, from_addr, recipients, events, grace_minutes, enabled, "
    "updated_by, updated_at, last_sent_at, last_error, (password_enc IS NOT NULL) AS has_password"
)


class NotificationsRepository:
    def __init__(self, pool: asyncpg.Pool, secrets: SecretBox) -> None:
        self._pool = pool
        self._secrets = secrets

    # ------------------------------------------------------------- SMTP
    async def config(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_CHAMPS} FROM smtp_config WHERE id = 1")  # noqa: S608
        return dict(row) if row else None

    async def password(self) -> str | None:
        async with self._pool.acquire() as conn:
            chiffre = await conn.fetchval("SELECT password_enc FROM smtp_config WHERE id = 1")
        if not chiffre:
            return None
        try:
            return self._secrets.decrypt(chiffre)
        except Exception as exc:  # noqa: BLE001
            logger.error("Mot de passe SMTP illisible : %s", exc)
            return None

    @property
    def can_encrypt(self) -> tuple[bool, str | None]:
        return self._secrets.available, self._secrets.unavailable_reason

    async def save(self, valeurs: dict[str, Any], *, password: str | None, actor: str) -> None:
        """``password`` : None garde l'ancien, "" l'efface, sinon le remplace."""
        async with self._pool.acquire() as conn:
            ancien = await conn.fetchval("SELECT password_enc FROM smtp_config WHERE id = 1")
            if password is None:
                chiffre = ancien
            elif password == "":
                chiffre = None
            else:
                chiffre = self._secrets.encrypt(password)
            await conn.execute(
                """
                INSERT INTO smtp_config (id, host, port, security, username, password_enc,
                       from_addr, recipients, events, grace_minutes, enabled, updated_by)
                VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (id) DO UPDATE SET
                    host = EXCLUDED.host, port = EXCLUDED.port, security = EXCLUDED.security,
                    username = EXCLUDED.username, password_enc = EXCLUDED.password_enc,
                    from_addr = EXCLUDED.from_addr, recipients = EXCLUDED.recipients,
                    events = EXCLUDED.events, grace_minutes = EXCLUDED.grace_minutes,
                    enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                valeurs["host"],
                valeurs["port"],
                valeurs["security"],
                valeurs.get("username"),
                chiffre,
                valeurs["from_addr"],
                list(valeurs["recipients"]),
                list(valeurs["events"]),
                valeurs["grace_minutes"],
                valeurs["enabled"],
                actor,
            )

    async def delete(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM smtp_config WHERE id = 1")

    async def mark(self, *, ok: bool, error: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            if ok:
                await conn.execute(
                    "UPDATE smtp_config SET last_sent_at = now(), last_error = NULL WHERE id = 1"
                )
            else:
                await conn.execute(
                    "UPDATE smtp_config SET last_error = $1 WHERE id = 1", (error or "")[:500]
                )

    # --------------------------------------------------------- problemes
    async def open_events(self) -> dict[str, dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM alert_events WHERE resolved_at IS NULL")
        return {str(r["key"]): dict(r) for r in rows}

    async def upsert_event(self, probleme: dict[str, Any], now: datetime) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert_events (key, category, level, title, detail, first_seen,
                       last_seen)
                VALUES ($1, $2, $3, $4, $5, $6, $6)
                ON CONFLICT (key) DO UPDATE SET
                    category = EXCLUDED.category, level = EXCLUDED.level,
                    title = EXCLUDED.title, detail = EXCLUDED.detail, last_seen = $6,
                    -- Un probleme qui REVIENT apres sa resolution est un nouveau
                    -- probleme : son compteur et son signalement repartent.
                    first_seen = CASE WHEN alert_events.resolved_at IS NULL
                                      THEN alert_events.first_seen ELSE $6 END,
                    notified_at = CASE WHEN alert_events.resolved_at IS NULL
                                       THEN alert_events.notified_at ELSE NULL END,
                    resolved_notified = CASE WHEN alert_events.resolved_at IS NULL
                                       THEN alert_events.resolved_notified ELSE FALSE END,
                    resolved_at = NULL
                """,
                probleme["key"],
                probleme["category"],
                probleme["level"],
                probleme["title"],
                probleme.get("detail"),
                now,
            )

    async def resolve(self, keys: list[str], now: datetime) -> None:
        if not keys:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE alert_events SET resolved_at = $2 WHERE key = ANY($1::text[]) "
                "AND resolved_at IS NULL",
                keys,
                now,
            )

    async def to_announce_resolved(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM alert_events WHERE resolved_at IS NOT NULL "
                "AND notified_at IS NOT NULL AND NOT resolved_notified"
            )
        return [dict(r) for r in rows]

    async def mark_notified(self, keys: list[str], now: datetime) -> None:
        if keys:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE alert_events SET notified_at = $2 WHERE key = ANY($1::text[])",
                    keys,
                    now,
                )

    async def mark_resolved_notified(self, keys: list[str]) -> None:
        if keys:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE alert_events SET resolved_notified = TRUE WHERE key = ANY($1::text[])",
                    keys,
                )

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, category, level, title, detail, first_seen, last_seen, notified_at, "
                "resolved_at FROM alert_events ORDER BY last_seen DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    async def prune(self, days: int = 90) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_events WHERE resolved_at < now() - make_interval(days => $1)",
                days,
            )

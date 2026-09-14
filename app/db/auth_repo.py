"""Stockage PostgreSQL des comptes, cles d'API et sessions.

Implemente le contrat ``AuthStore`` sur asyncpg. Les secrets n'y figurent que
sous forme hachee (argon2 pour les mots de passe, SHA-256 pour les cles et les
jetons de session) : la base volee ne rend aucun secret utilisable directement.
"""

from __future__ import annotations

import asyncpg

from app.services.auth import ApiKeyRecord, SessionRecord, UserRecord


class PgAuthStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ comptes
    async def count_users(self) -> int:
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval("SELECT count(*) FROM auth_users"))

    async def get_user(self, username: str) -> UserRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM auth_users WHERE username = $1", username)
        return _user(row) if row else None

    async def create_user(self, record: UserRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_users
                       (username, password_hash, display_name, is_admin, disabled)
                VALUES ($1, $2, $3, $4, $5)
                """,
                record.username,
                record.password_hash,
                record.display_name,
                record.is_admin,
                record.disabled,
            )

    async def list_users(self) -> list[UserRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM auth_users ORDER BY username")
        return [_user(row) for row in rows]

    async def set_password(self, username: str, password_hash: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE auth_users SET password_hash = $2, updated_at = now() WHERE username = $1",
                username,
                password_hash,
            )
        return not result.endswith(" 0")

    async def set_user_disabled(self, username: str, disabled: bool) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE auth_users SET disabled = $2, updated_at = now() WHERE username = $1",
                username,
                disabled,
            )
        return not result.endswith(" 0")

    async def touch_login(self, username: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_users SET last_login_at = now() WHERE username = $1", username
            )

    # ------------------------------------------------------------ cles d'API
    async def create_api_key(self, record: ApiKeyRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_api_keys
                       (id, name, prefix, key_hash, is_admin, created_by, disabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                record.id,
                record.name,
                record.prefix,
                record.key_hash,
                record.is_admin,
                record.created_by,
                record.disabled,
            )

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM auth_api_keys WHERE key_hash = $1", key_hash)
        return _api_key(row) if row else None

    async def list_api_keys(self) -> list[ApiKeyRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM auth_api_keys ORDER BY created_at")
        return [_api_key(row) for row in rows]

    async def delete_api_key(self, key_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM auth_api_keys WHERE id = $1", key_id)
        return not result.endswith(" 0")

    async def touch_api_key(self, key_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_api_keys SET last_used_at = now() WHERE id = $1", key_id
            )

    # ------------------------------------------------------------ sessions
    async def create_session(self, record: SessionRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_sessions (token_hash, username, display, is_admin, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                record.token_hash,
                record.username,
                record.display,
                record.is_admin,
                record.expires_at,
            )

    async def get_session(self, token_hash: str) -> SessionRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM auth_sessions WHERE token_hash = $1", token_hash
            )
        if row is None:
            return None
        return SessionRecord(
            token_hash=row["token_hash"],
            username=row["username"],
            display=row["display"],
            is_admin=row["is_admin"],
            expires_at=row["expires_at"],
        )

    async def delete_session(self, token_hash: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM auth_sessions WHERE token_hash = $1", token_hash)

    async def purge_expired_sessions(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
        return int(result.rsplit(" ", 1)[-1] or 0)


def _user(row: asyncpg.Record) -> UserRecord:
    return UserRecord(
        username=row["username"],
        password_hash=row["password_hash"],
        display_name=row["display_name"],
        is_admin=row["is_admin"],
        disabled=row["disabled"],
        last_login_at=row["last_login_at"],
    )


def _api_key(row: asyncpg.Record) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        key_hash=row["key_hash"],
        is_admin=row["is_admin"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        disabled=row["disabled"],
        last_used_at=row["last_used_at"],
    )

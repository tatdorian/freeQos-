"""Comptes et sessions de l'interface d'exploitation.

Ce depot ne rend JAMAIS une empreinte de mot de passe hors de ``credentials``,
la seule lecture qui en a besoin (la verification a la connexion). Les listes
et les fiches ne portent que l'email, le grade et des dates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import asyncpg

COLUMNS = "id, email, role, disabled, created_by, created_at, updated_at, last_login_at"


class UserNotFoundError(LookupError):
    pass


class DuplicateUserError(ValueError):
    pass


class UsersStore(Protocol):
    async def count(self) -> int: ...
    async def count_editors(self) -> int: ...
    async def list_all(self) -> list[dict[str, Any]]: ...
    async def get(self, user_id: int) -> dict[str, Any]: ...
    async def credentials(self, email: str) -> dict[str, Any] | None: ...
    async def create(
        self, *, email: str, password_hash: str, role: str, created_by: str | None
    ) -> dict[str, Any]: ...
    async def create_first(self, *, email: str, password_hash: str) -> dict[str, Any] | None: ...
    async def update(self, user_id: int, **fields: Any) -> dict[str, Any]: ...
    async def delete(self, user_id: int) -> None: ...
    async def open_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        ttl: timedelta,
        user_agent: str | None,
        address: str | None,
    ) -> None: ...
    async def session_user(self, token_hash: str, *, ttl: timedelta) -> dict[str, Any] | None: ...
    async def close_session(self, token_hash: str) -> None: ...
    async def close_sessions_of(self, user_id: int, *, keep: str | None = None) -> None: ...


class UsersRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval("SELECT count(*) FROM app_users"))

    async def count_editors(self) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM app_users WHERE role = 'edit' AND NOT disabled"
                )
            )

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT {COLUMNS} FROM app_users ORDER BY email")  # noqa: S608
        return [dict(r) for r in rows]

    async def get(self, user_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM app_users WHERE id = $1",  # noqa: S608
                user_id,
            )
        if row is None:
            raise UserNotFoundError(f"unknown account {user_id}")
        return dict(row)

    async def credentials(self, email: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS}, password_hash FROM app_users WHERE email = $1",  # noqa: S608
                email,
            )
        return dict(row) if row is not None else None

    async def create(
        self, *, email: str, password_hash: str, role: str, created_by: str | None
    ) -> dict[str, Any]:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"INSERT INTO app_users (email, password_hash, role, created_by) "  # noqa: S608
                    f"VALUES ($1, $2, $3, $4) RETURNING {COLUMNS}",
                    email,
                    password_hash,
                    role,
                    created_by,
                )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateUserError(f"an account already exists for {email}") from exc
        return dict(row)

    async def create_first(self, *, email: str, password_hash: str) -> dict[str, Any] | None:
        """Le PREMIER compte, en edition -- ou None si un compte existe deja.

        Le test et l'insertion sont une seule transaction verrouillee : deux
        navigateurs ouverts sur l'ecran d'initialisation ne peuvent pas creer
        chacun leur "premier" compte.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE app_users IN EXCLUSIVE MODE")
            if await conn.fetchval("SELECT count(*) FROM app_users"):
                return None
            row = await conn.fetchrow(
                f"INSERT INTO app_users (email, password_hash, role, created_by) "  # noqa: S608
                f"VALUES ($1, $2, 'edit', 'setup') RETURNING {COLUMNS}",
                email,
                password_hash,
            )
        return dict(row)

    async def update(self, user_id: int, **fields: Any) -> dict[str, Any]:
        autorises = {"role", "password_hash", "disabled", "last_login_at"}
        champs = {k: v for k, v in fields.items() if k in autorises}
        if not champs:
            return await self.get(user_id)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(champs))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE app_users SET {sets}, updated_at = now() WHERE id = $1 "  # noqa: S608
                f"RETURNING {COLUMNS}",
                user_id,
                *champs.values(),
            )
        if row is None:
            raise UserNotFoundError(f"unknown account {user_id}")
        return dict(row)

    async def delete(self, user_id: int) -> None:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM app_users WHERE id = $1", user_id)
        if resultat.endswith(" 0"):
            raise UserNotFoundError(f"unknown account {user_id}")

    async def open_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        ttl: timedelta,
        user_agent: str | None,
        address: str | None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM app_sessions WHERE expires_at < now()")
            await conn.execute(
                "INSERT INTO app_sessions (token_hash, user_id, expires_at, user_agent, address) "
                "VALUES ($1, $2, now() + $3::interval, $4, $5)",
                token_hash,
                user_id,
                ttl,
                (user_agent or "")[:256] or None,
                address,
            )

    async def session_user(self, token_hash: str, *, ttl: timedelta) -> dict[str, Any] | None:
        """Le compte d'une session valide, et prolonge celle-ci (expiration glissante)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE app_sessions s
                   SET last_seen = now(), expires_at = now() + $2::interval
                  FROM app_users u
                 WHERE s.token_hash = $1 AND u.id = s.user_id
                   AND s.expires_at > now() AND NOT u.disabled
                RETURNING {", ".join("u." + c.strip() for c in COLUMNS.split(","))}
                """,  # noqa: S608
                token_hash,
                ttl,
            )
        return dict(row) if row is not None else None

    async def close_session(self, token_hash: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM app_sessions WHERE token_hash = $1", token_hash)

    async def close_sessions_of(self, user_id: int, *, keep: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM app_sessions WHERE user_id = $1 AND token_hash IS DISTINCT FROM $2",
                user_id,
                keep,
            )


class InMemoryUsersRepository:
    """Meme contrat, en memoire : tests et demonstration sans base."""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self._next = 1

    def _public(self, u: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in u.items() if k != "password_hash"}

    async def count(self) -> int:
        return len(self.users)

    async def count_editors(self) -> int:
        return sum(1 for u in self.users.values() if u["role"] == "edit" and not u["disabled"])

    async def list_all(self) -> list[dict[str, Any]]:
        return [self._public(u) for u in sorted(self.users.values(), key=lambda u: u["email"])]

    async def get(self, user_id: int) -> dict[str, Any]:
        if user_id not in self.users:
            raise UserNotFoundError(f"unknown account {user_id}")
        return self._public(self.users[user_id])

    async def credentials(self, email: str) -> dict[str, Any] | None:
        return next((dict(u) for u in self.users.values() if u["email"] == email), None)

    async def create(
        self, *, email: str, password_hash: str, role: str, created_by: str | None
    ) -> dict[str, Any]:
        if any(u["email"] == email for u in self.users.values()):
            raise DuplicateUserError(f"an account already exists for {email}")
        maintenant = datetime.now(tz=UTC)
        fiche = {
            "id": self._next,
            "email": email,
            "password_hash": password_hash,
            "role": role,
            "disabled": False,
            "created_by": created_by,
            "created_at": maintenant,
            "updated_at": maintenant,
            "last_login_at": None,
        }
        self.users[self._next] = fiche
        self._next += 1
        return self._public(fiche)

    async def create_first(self, *, email: str, password_hash: str) -> dict[str, Any] | None:
        if self.users:
            return None
        return await self.create(
            email=email, password_hash=password_hash, role="edit", created_by="setup"
        )

    async def update(self, user_id: int, **fields: Any) -> dict[str, Any]:
        if user_id not in self.users:
            raise UserNotFoundError(f"unknown account {user_id}")
        for cle in ("role", "password_hash", "disabled", "last_login_at"):
            if cle in fields:
                self.users[user_id][cle] = fields[cle]
        return self._public(self.users[user_id])

    async def delete(self, user_id: int) -> None:
        if self.users.pop(user_id, None) is None:
            raise UserNotFoundError(f"unknown account {user_id}")
        self.sessions = {k: v for k, v in self.sessions.items() if v["user_id"] != user_id}

    async def open_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        ttl: timedelta,
        user_agent: str | None,
        address: str | None,
    ) -> None:
        self.sessions[token_hash] = {
            "user_id": user_id,
            "expires_at": datetime.now(tz=UTC) + ttl,
        }

    async def session_user(self, token_hash: str, *, ttl: timedelta) -> dict[str, Any] | None:
        session = self.sessions.get(token_hash)
        if session is None or session["expires_at"] < datetime.now(tz=UTC):
            return None
        fiche = self.users.get(session["user_id"])
        if fiche is None or fiche["disabled"]:
            return None
        session["expires_at"] = datetime.now(tz=UTC) + ttl
        return self._public(fiche)

    async def close_session(self, token_hash: str) -> None:
        self.sessions.pop(token_hash, None)

    async def close_sessions_of(self, user_id: int, *, keep: str | None = None) -> None:
        self.sessions = {
            k: v for k, v in self.sessions.items() if v["user_id"] != user_id or k == keep
        }

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

#: Sessions ouvertes en meme temps par un compte. Au-dela, la plus ancienne est
#: fermee : un mot de passe qui fuit ne laisse pas s'accumuler des dizaines de
#: sessions oubliees, et la liste "mes sessions" reste lisible.
MAX_SESSIONS_PER_USER = 10

#: Le journal des connexions garde six mois : de quoi remonter une intrusion,
#: sans grossir sans fin.
EVENTS_RETENTION = timedelta(days=180)

#: Longueur de l'identifiant de session montre a l'interface : un PREFIXE de
#: l'empreinte stockee. Il designe une session sans permettre de l'ouvrir.
SESSION_ID_LEN = 16


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
    async def session_user(
        self, token_hash: str, *, ttl: timedelta, max_age: timedelta | None = None
    ) -> dict[str, Any] | None: ...
    async def close_session(self, token_hash: str) -> None: ...
    async def close_sessions_of(self, user_id: int, *, keep: str | None = None) -> None: ...
    async def sessions_of(self, user_id: int) -> list[dict[str, Any]]: ...
    async def close_session_of(self, user_id: int, prefix: str) -> bool: ...
    async def record_event(
        self,
        event: str,
        *,
        email: str | None = None,
        actor: str | None = None,
        address: str | None = None,
        user_agent: str | None = None,
        detail: str | None = None,
    ) -> None: ...
    async def events(
        self, *, limit: int = 200, email: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]: ...


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
            await conn.execute(
                """
                DELETE FROM app_sessions
                 WHERE user_id = $1 AND token_hash IN (
                       SELECT token_hash FROM app_sessions WHERE user_id = $1
                        ORDER BY created_at DESC, last_seen DESC OFFSET $2)
                """,
                user_id,
                MAX_SESSIONS_PER_USER,
            )

    async def session_user(
        self, token_hash: str, *, ttl: timedelta, max_age: timedelta | None = None
    ) -> dict[str, Any] | None:
        """Le compte d'une session valide, et prolonge celle-ci (expiration glissante).

        ``max_age`` borne la vie de la session QUELLE QUE SOIT l'activite : un
        onglet qui rafraichit sa page toutes les trente secondes ne garde pas
        une session ouverte pour toujours.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE app_sessions s
                   SET last_seen = now(), expires_at = now() + $2::interval
                  FROM app_users u
                 WHERE s.token_hash = $1 AND u.id = s.user_id
                   AND s.expires_at > now() AND NOT u.disabled
                   AND ($3::interval IS NULL OR s.created_at > now() - $3::interval)
                RETURNING {", ".join("u." + c.strip() for c in COLUMNS.split(","))}
                """,  # noqa: S608
                token_hash,
                ttl,
                max_age,
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

    async def sessions_of(self, user_id: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token_hash, created_at, last_seen, expires_at, user_agent, address "
                "FROM app_sessions WHERE user_id = $1 AND expires_at > now() "
                "ORDER BY last_seen DESC",
                user_id,
            )
        return [dict(r) for r in rows]

    async def close_session_of(self, user_id: int, prefix: str) -> bool:
        if len(prefix) != SESSION_ID_LEN or not all(c in "0123456789abcdef" for c in prefix):
            return False
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM app_sessions WHERE user_id = $1 AND left(token_hash, $2) = $3",
                user_id,
                SESSION_ID_LEN,
                prefix,
            )
        return not resultat.endswith(" 0")

    async def record_event(
        self,
        event: str,
        *,
        email: str | None = None,
        actor: str | None = None,
        address: str | None = None,
        user_agent: str | None = None,
        detail: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO auth_events (event, email, actor, address, user_agent, detail) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                event,
                (email or "")[:254] or None,
                (actor or "")[:254] or None,
                address,
                (user_agent or "")[:256] or None,
                (detail or "")[:500] or None,
            )
            if event == "login_ok":
                await conn.execute(
                    "DELETE FROM auth_events WHERE at < now() - $1::interval", EVENTS_RETENTION
                )

    async def events(
        self, *, limit: int = 200, email: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, at, event, email, actor, address, user_agent, detail "
                "FROM auth_events WHERE ($1::text IS NULL OR email = $1) "
                "AND ($2::text IS NULL OR event = $2) ORDER BY at DESC, id DESC LIMIT $3",
                email,
                event,
                limit,
            )
        return [dict(r) for r in rows]


class InMemoryUsersRepository:
    """Meme contrat, en memoire : tests et demonstration sans base."""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.journal: list[dict[str, Any]] = []
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
        maintenant = datetime.now(tz=UTC)
        self.sessions[token_hash] = {
            "user_id": user_id,
            "created_at": maintenant,
            "last_seen": maintenant,
            "expires_at": maintenant + ttl,
            "user_agent": user_agent,
            "address": address,
        }
        siennes = sorted(
            (k for k, v in self.sessions.items() if v["user_id"] == user_id),
            key=lambda k: self.sessions[k]["created_at"],
            reverse=True,
        )
        for cle in siennes[MAX_SESSIONS_PER_USER:]:
            self.sessions.pop(cle, None)

    async def session_user(
        self, token_hash: str, *, ttl: timedelta, max_age: timedelta | None = None
    ) -> dict[str, Any] | None:
        session = self.sessions.get(token_hash)
        maintenant = datetime.now(tz=UTC)
        if session is None or session["expires_at"] < maintenant:
            return None
        cree = session.get("created_at")
        if max_age is not None and cree is not None and cree <= maintenant - max_age:
            return None
        fiche = self.users.get(session["user_id"])
        if fiche is None or fiche["disabled"]:
            return None
        session["expires_at"] = maintenant + ttl
        session["last_seen"] = maintenant
        return self._public(fiche)

    async def close_session(self, token_hash: str) -> None:
        self.sessions.pop(token_hash, None)

    async def close_sessions_of(self, user_id: int, *, keep: str | None = None) -> None:
        self.sessions = {
            k: v for k, v in self.sessions.items() if v["user_id"] != user_id or k == keep
        }

    async def sessions_of(self, user_id: int) -> list[dict[str, Any]]:
        maintenant = datetime.now(tz=UTC)
        siennes = [
            {
                "token_hash": k,
                **{
                    c: v.get(c)
                    for c in ("created_at", "last_seen", "expires_at", "user_agent", "address")
                },
            }
            for k, v in self.sessions.items()
            if v["user_id"] == user_id and v["expires_at"] > maintenant
        ]
        return sorted(siennes, key=lambda x: x["last_seen"], reverse=True)

    async def close_session_of(self, user_id: int, prefix: str) -> bool:
        if len(prefix) != SESSION_ID_LEN:
            return False
        cibles = [
            k for k, v in self.sessions.items() if v["user_id"] == user_id and k.startswith(prefix)
        ]
        for cle in cibles:
            self.sessions.pop(cle, None)
        return bool(cibles)

    async def record_event(
        self,
        event: str,
        *,
        email: str | None = None,
        actor: str | None = None,
        address: str | None = None,
        user_agent: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.journal.append(
            {
                "id": len(self.journal) + 1,
                "at": datetime.now(tz=UTC),
                "event": event,
                "email": email,
                "actor": actor,
                "address": address,
                "user_agent": user_agent,
                "detail": detail,
            }
        )

    async def events(
        self, *, limit: int = 200, email: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        lignes = [
            e
            for e in reversed(self.journal)
            if (email is None or e["email"] == email) and (event is None or e["event"] == event)
        ]
        return lignes[:limit]

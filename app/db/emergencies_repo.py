"""Les urgences enregistrees (table ``emergencies``)."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.enforcement.emergency import Emergency, check, to_model

COLUMNS = (
    "id, name, kind, percent, down_mbps, up_mbps, scope, targets, active, active_since, "
    "active_by, note, created_at, updated_at"
)


class EmergencyNotFoundError(LookupError):
    pass


class EmergencyConflictError(ValueError):
    pass


def _ligne(row: Any) -> dict[str, Any]:
    d = dict(row)
    cibles = d.get("targets")
    d["targets"] = json.loads(cibles) if isinstance(cibles, str) else list(cibles or [])
    return d


class EmergenciesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM emergencies ORDER BY lower(name)"  # noqa: S608
            )
        return [_ligne(r) for r in rows]

    async def models(self) -> list[Emergency]:
        return [to_model(r) for r in await self.list_all()]

    async def get(self, ident: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM emergencies WHERE id = $1",  # noqa: S608
                ident,
            )
        if row is None:
            raise EmergencyNotFoundError(f"no emergency {ident}")
        return _ligne(row)

    async def create(self, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
        f = check(payload)
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO emergencies (name, kind, percent, down_mbps, up_mbps, scope,
                                             targets, note, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                    RETURNING {COLUMNS}
                    """,  # noqa: S608
                    f["name"],
                    f["kind"],
                    f["percent"],
                    f["down_mbps"],
                    f["up_mbps"],
                    f["scope"],
                    json.dumps(f["targets"]),
                    f["note"],
                    actor,
                )
            except asyncpg.UniqueViolationError as exc:
                raise EmergencyConflictError(
                    f"an emergency named '{f['name']}' already exists"
                ) from exc
        return _ligne(row)

    async def update(
        self, ident: int, payload: dict[str, Any], actor: str | None
    ) -> dict[str, Any]:
        actuel = await self.get(ident)
        f = check({**actuel, **{k: v for k, v in payload.items() if v is not None or k == "note"}})
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE emergencies SET name = $2, kind = $3, percent = $4, down_mbps = $5,
                           up_mbps = $6, scope = $7, targets = $8::jsonb, note = $9,
                           updated_by = $10, updated_at = now()
                     WHERE id = $1 RETURNING {COLUMNS}
                    """,  # noqa: S608
                    ident,
                    f["name"],
                    f["kind"],
                    f["percent"],
                    f["down_mbps"],
                    f["up_mbps"],
                    f["scope"],
                    json.dumps(f["targets"]),
                    f["note"],
                    actor,
                )
            except asyncpg.UniqueViolationError as exc:
                raise EmergencyConflictError(
                    f"an emergency named '{f['name']}' already exists"
                ) from exc
        if row is None:
            raise EmergencyNotFoundError(f"no emergency {ident}")
        return _ligne(row)

    async def set_active(self, ident: int, active: bool, actor: str | None) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE emergencies
                   SET active = $2,
                       active_since = CASE WHEN $2 THEN now() ELSE NULL END,
                       active_by = CASE WHEN $2 THEN $3 ELSE NULL END,
                       updated_at = now()
                 WHERE id = $1 RETURNING {COLUMNS}
                """,  # noqa: S608
                ident,
                active,
                actor,
            )
        if row is None:
            raise EmergencyNotFoundError(f"no emergency {ident}")
        return _ligne(row)

    async def delete(self, ident: int) -> None:
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM emergencies WHERE id = $1", ident)
        if str(res).endswith(" 0"):
            raise EmergencyNotFoundError(f"no emergency {ident}")

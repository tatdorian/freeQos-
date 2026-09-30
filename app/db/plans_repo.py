"""Le plan de chaque client : pousse par la facturation ou saisi a la main."""

from __future__ import annotations

from typing import Any

import asyncpg


class ClientPlansRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_many(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM client_plans WHERE login = ANY($1::text[])", list(logins)
            )
        return {r["login"]: dict(r) for r in rows}

    async def list_all(self) -> dict[str, dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM client_plans")
        return {r["login"]: dict(r) for r in rows}

    async def set(
        self,
        login: str,
        *,
        down_mbps: float | None,
        up_mbps: float | None,
        source: str,
        package_id: str | None = None,
        service_id: str | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO client_plans (login, down_mbps, up_mbps, package_id, source,
                                          service_id, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, now())
                ON CONFLICT (login) DO UPDATE
                   SET down_mbps = EXCLUDED.down_mbps, up_mbps = EXCLUDED.up_mbps,
                       package_id = EXCLUDED.package_id, source = EXCLUDED.source,
                       service_id = EXCLUDED.service_id, updated_by = EXCLUDED.updated_by,
                       updated_at = now()
                RETURNING *
                """,
                login,
                down_mbps,
                up_mbps,
                package_id,
                source,
                service_id,
                updated_by,
            )
        return dict(row)

    async def delete(self, login: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM client_plans WHERE login = $1", login)
        return not resultat.endswith(" 0")

    async def delete_by_service(self, service_id: str) -> list[str]:
        """Oublie les plans venus d'un service supprime ; rend les logins liberes."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "DELETE FROM client_plans WHERE source = 'api' AND service_id = $1 RETURNING login",
                service_id,
            )
        return [r["login"] for r in rows]

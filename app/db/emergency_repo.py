"""Bande passante d'URGENCE de chaque client.

Le forfait est la bande passante NORMALE : celle du client quand il n'a pas de
mode. Quand un mode marque « emergency » est active pour lui, c'est sa bande
passante d'urgence qui s'applique -- propre a chaque client, saisie a cote de
son forfait. Un client sans valeur d'urgence recoit le debit du mode.
"""

from __future__ import annotations

from typing import Any


async def get_rates(pool: Any, login: str) -> tuple[float, float] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT down_mbps, up_mbps FROM client_emergency_rates WHERE login = $1", login
        )
    return (float(row["down_mbps"]), float(row["up_mbps"])) if row else None


async def get(pool: Any, login: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT login, down_mbps, up_mbps, updated_by, updated_at "
            "FROM client_emergency_rates WHERE login = $1",
            login,
        )
    return dict(row) if row else None


async def all_rates(pool: Any) -> dict[str, tuple[float, float]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT login, down_mbps, up_mbps FROM client_emergency_rates")
    return {str(r["login"]): (float(r["down_mbps"]), float(r["up_mbps"])) for r in rows}


async def put(pool: Any, login: str, down: float, up: float, actor: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO client_emergency_rates (login, down_mbps, up_mbps, updated_by, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (login) DO UPDATE SET down_mbps = EXCLUDED.down_mbps,
                up_mbps = EXCLUDED.up_mbps, updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            login,
            down,
            up,
            actor,
        )


async def delete(pool: Any, login: str) -> bool:
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM client_emergency_rates WHERE login = $1", login)
    return not str(res).endswith(" 0")

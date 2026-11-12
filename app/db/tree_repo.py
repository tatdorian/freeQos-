"""Corrections de l'exploitant sur le placement des clients dans l'arbre."""

from __future__ import annotations

import asyncpg


class TreeRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def places(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT login, node_key FROM tree_client_places")
        return {str(r["login"]): str(r["node_key"]) for r in rows}

    async def links(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT node_key, login FROM tree_client_links")
        return {str(r["node_key"]): str(r["login"]) for r in rows}

    async def set_place(self, logins: list[str], node_key: str, *, updated_by: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            for login in logins:
                await conn.execute(
                    """
                    INSERT INTO tree_client_places (login, node_key, updated_by)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (login) DO UPDATE
                       SET node_key = EXCLUDED.node_key, updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                    """,
                    login,
                    node_key,
                    updated_by,
                )

    async def clear_place(self, login: str) -> bool:
        async with self._pool.acquire() as conn:
            r = await conn.execute("DELETE FROM tree_client_places WHERE login = $1", login)
        return str(r).endswith(" 1")

    async def set_link(self, node_key: str, login: str, *, updated_by: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            if login:
                # Un client n'est qu'UNE case : le lien precedent part.
                await conn.execute("DELETE FROM tree_client_links WHERE login = $1", login)
            await conn.execute(
                """
                INSERT INTO tree_client_links (node_key, login, updated_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (node_key) DO UPDATE
                   SET login = EXCLUDED.login, updated_by = EXCLUDED.updated_by,
                       updated_at = now()
                """,
                node_key,
                login,
                updated_by,
            )

    async def clear_link(self, node_key: str) -> bool:
        async with self._pool.acquire() as conn:
            r = await conn.execute("DELETE FROM tree_client_links WHERE node_key = $1", node_key)
        return str(r).endswith(" 1")

"""La memoire des files de clients posees par freeQoS (sans marque sur le routeur)."""

from __future__ import annotations

from typing import Any

from app.enforcement.models import OWNED_QUEUES


async def load(pool: Any) -> int:
    """Recharge la memoire depuis la base. Rend le nombre de files connues."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT router, name FROM owned_queues")
    OWNED_QUEUES.clear()
    for row in rows:
        OWNED_QUEUES.setdefault(str(row["router"]), set()).add(str(row["name"]))
    return len(rows)


async def remember(pool: Any, router: str, added: set[str], dropped: set[str]) -> None:
    """Retient les files posees ou renommees, oublie celles retirees."""
    if not added and not dropped:
        return
    async with pool.acquire() as conn, conn.transaction():
        if dropped:
            await conn.execute(
                "DELETE FROM owned_queues WHERE router = $1 AND name = ANY($2::text[])",
                router,
                sorted(dropped),
            )
        if added:
            await conn.executemany(
                "INSERT INTO owned_queues (router, name) VALUES ($1, $2) "
                "ON CONFLICT (router, name) DO NOTHING",
                [(router, nom) for nom in sorted(added)],
            )


async def rename_router(pool: Any, old: str, new: str) -> None:
    """Un routeur RENOMME dans freeQoS garde ses files et son type de file.

    La memoire est rangee par NOM de routeur : sans ce report, un routeur
    renomme perdait la trace de ses files de clients (sans marque sur le
    routeur) -- « Remove freeQoS » les y aurait laissees, et la reconciliation
    les aurait prises pour des files posees a la main.
    """
    if not old or not new or old == new:
        return
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM owned_queues WHERE router = $2 AND name IN "
            "(SELECT name FROM owned_queues WHERE router = $1)",
            old,
            new,
        )
        await conn.execute("UPDATE owned_queues SET router = $2 WHERE router = $1", old, new)
        await conn.execute(
            "UPDATE router_queue_kinds SET router = $2 WHERE router = $1 "
            "AND NOT EXISTS (SELECT 1 FROM router_queue_kinds WHERE router = $2)",
            old,
            new,
        )
    noms = OWNED_QUEUES.pop(old, set())
    if noms:
        OWNED_QUEUES.setdefault(new, set()).update(noms)

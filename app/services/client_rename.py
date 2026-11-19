"""Renommer un client a IP fixe sans le dedoubler.

La reference d'une fiche est l'identite du client partout dans freeQoS : son
historique (``subscribers``), son forfait, son mode, sa fiche, son journal, sa
place sur la carte, ses schedulers personnels, et le nom de sa file sur le
routeur. Changer la reference sans rien d'autre laissait l'ancien client en
place, hors ligne, a cote d'un nouveau sans historique : un doublon.

Ici, tout suit le nouveau nom, dans une seule transaction.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ClientNameTakenError(Exception):
    """Le nouveau nom est deja celui d'un autre client (une session PPPoE)."""


#: Tables cle = login (une ligne par client) : la ligne de l'ancien nom prend
#: le nouveau ; une ligne orpheline deja au nouveau nom cede la place.
_PAR_LOGIN: tuple[tuple[str, str], ...] = (
    ("client_plans", "login"),
    ("client_profiles", "login"),
    ("client_locations", "name"),
    ("tree_client_places", "login"),
    ("excluded_clients", "login"),
)


async def check_new_name_free(pool: Any, old: str, new: str) -> None:
    """Refuse AVANT toute ecriture un nom deja porte par une session PPPoE."""
    if old == new:
        return
    async with pool.acquire() as conn:
        kind = await conn.fetchval("SELECT kind FROM subscribers WHERE login = $1", new)
    if kind == "pppoe":
        raise ClientNameTakenError(
            f"A client called '{new}' already exists (PPPoE session). Choose another name."
        )


async def rename_client(container: Any, old: str, new: str, *, actor: str) -> None:
    """Fait passer tout ce qui appartient a ``old`` sous ``new``."""
    if old == new:
        return
    pool = container.database.pool
    async with pool.acquire() as conn, conn.transaction():
        # L'historique : la ligne de l'abonne change de nom, ses mesures (cle
        # numerique) la suivent. Une ligne orpheline deja au nouveau nom (un
        # doublon laisse par une version precedente) cede la place.
        if await conn.fetchval("SELECT 1 FROM subscribers WHERE login = $1", old):
            await conn.execute("DELETE FROM subscribers WHERE login = $1 AND kind = 'static'", new)
            await conn.execute(
                "UPDATE subscribers SET login = $2, updated_at = now() WHERE login = $1",
                old,
                new,
            )
        for table, colonne in _PAR_LOGIN:
            if await conn.fetchval(f"SELECT 1 FROM {table} WHERE {colonne} = $1", old):  # noqa: S608
                await conn.execute(f"DELETE FROM {table} WHERE {colonne} = $1", new)  # noqa: S608
                await conn.execute(
                    f"UPDATE {table} SET {colonne} = $2 WHERE {colonne} = $1",  # noqa: S608
                    old,
                    new,
                )
        # Mode, limite, boost.
        await conn.execute(
            """
            DELETE FROM shaping_policies
             WHERE scope = 'subscriber' AND target_key = $2
               AND EXISTS (SELECT 1 FROM shaping_policies
                            WHERE scope = 'subscriber' AND target_key = $1)
            """,
            old,
            new,
        )
        await conn.execute(
            """
            UPDATE shaping_policies SET target_key = $2, updated_at = now()
             WHERE scope = 'subscriber' AND target_key = $1
            """,
            old,
            new,
        )
        await conn.execute("UPDATE client_activity SET login = $2 WHERE login = $1", old, new)
        await conn.execute("UPDATE tree_client_links SET login = $2 WHERE login = $1", old, new)
        # Les schedulers qui le visent par son nom (le sien, ou un de groupe).
        await conn.execute(
            """
            UPDATE bandwidth_schedules
               SET targets = (
                     SELECT COALESCE(jsonb_agg(DISTINCT CASE WHEN t = to_jsonb($1::text)
                                                             THEN to_jsonb($2::text)
                                                             ELSE t END), '[]'::jsonb)
                       FROM jsonb_array_elements(targets) AS t),
                   updated_at = now()
             WHERE scope = 'subscribers' AND targets ? $1
            """,
            old,
            new,
        )

    collection = getattr(container, "collection", None)
    if collection is not None:
        try:
            collection.directory.forget_subscriber(old)
            getattr(collection, "_known_logins", set()).discard(old)
        except Exception:  # noqa: BLE001
            logger.debug("Oubli de '%s' par la collecte impossible", old)
    shaping = getattr(container, "shaping", None)
    if shaping is not None:
        try:
            await shaping.load_schedules()
        except Exception:  # noqa: BLE001 - le prochain chargement rattrapera
            logger.exception("Rechargement des schedulers apres renommage impossible")
    activite = getattr(container, "activity_repo", None)
    if activite is not None:
        await activite.record(
            actor=actor,
            login=new,
            action="Renamed",
            detail=f"{old} is now {new}: same client, history kept",
        )
    logger.info("Client '%s' renomme en '%s'", old, new)

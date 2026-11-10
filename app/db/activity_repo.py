"""Journal des gestes sur les clients (mode, boost, forfait).

L'ecriture ne leve JAMAIS : un journal indisponible ne doit pas faire echouer
le geste qu'il raconte. L'echec est trace dans les logs, le geste aboutit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

#: Garde 400 jours : de quoi repondre a "depuis quand ce client est en Backup"
#: sur plus d'un an de facturation.
RETENTION_DAYS = 400

ETATS_ECRITS = frozenset({"file-posee", "file-retiree"})


def etat_file(rapport: Any) -> tuple[str | None, str | None]:
    """(etat, motif) de la file d'apres le rapport d'ecriture d'un geste."""
    if not isinstance(rapport, dict):
        return None, None
    if rapport.get("ok") is False:
        return "erreur", str(rapport.get("detail") or "")
    etat = rapport.get("state")
    return (str(etat) if etat else None), (str(rapport.get("reason") or "") or None)


class ActivityRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(
        self,
        *,
        actor: str,
        login: str,
        action: str,
        detail: str | None = None,
        ok: bool = True,
        applied: Any = None,
    ) -> None:
        etat, motif = etat_file(applied)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO client_activity
                           (actor, login, action, detail, ok, queue_state, queue_reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    actor,
                    login,
                    action,
                    detail,
                    ok,
                    etat,
                    motif,
                )
        except Exception:  # noqa: BLE001 - le geste a eu lieu, le journal suit
            logger.exception("Journal des gestes indisponible (%s sur %s)", action, login)

    async def list(
        self,
        *,
        login: str | None = None,
        search: str | None = None,
        before: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        motif = f"%{search.strip()}%" if search and search.strip() else None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, actor, login, action, detail, ok, queue_state, queue_reason
                  FROM client_activity
                 WHERE ($1::text IS NULL OR login = $1)
                   AND ($2::text IS NULL OR login ILIKE $2 OR actor ILIKE $2
                        OR action ILIKE $2 OR coalesce(detail, '') ILIKE $2)
                   AND ($3::timestamptz IS NULL OR ts < $3)
                 ORDER BY ts DESC, id DESC
                 LIMIT $4
                """,
                login,
                motif,
                before,
                limit,
            )
        return [{**dict(r), "written": r["queue_state"] in ETATS_ECRITS} for r in rows]

    async def purge(self) -> int:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM client_activity WHERE ts < now() - make_interval(days => $1)",
                RETENTION_DAYS,
            )
        return int(resultat.split()[-1]) if resultat.split()[-1].isdigit() else 0

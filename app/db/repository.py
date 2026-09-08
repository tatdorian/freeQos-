"""Lectures pour l'API.

Les agregations utilisent ``date_bin`` (PostgreSQL >= 14) plutot que
``time_bucket`` : le resultat est identique sur les hypertables, mais les memes
requetes fonctionnent aussi sur un PostgreSQL sans Timescale (integration
continue, poste de dev).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import asyncpg


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(record) for record in records]


class MetricsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ PoPs
    async def list_pops(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT p.id, p.name, p.router_host, p.created_at,
                       (SELECT count(*) FROM subscribers s WHERE s.pop_id = p.id)
                           AS subscriber_count,
                       (SELECT count(*) FROM backhauls b WHERE b.pop_id = p.id)
                           AS backhaul_count
                  FROM pops p
                 ORDER BY p.name
                """
            )
        return _rows(records)

    async def delete_pop(self, pop_id: int) -> dict[str, int]:
        """Supprime un PoP et tout ce qui en depend.

        Retirer un routeur de l'inventaire ne fait pas disparaitre ses donnees :
        le PoP, ses abonnes et leurs metriques restent en base. C'est voulu — on
        ne perd pas un historique par accident — mais il faut donc un moyen
        explicite de faire le menage.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            compte = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM subscribers WHERE pop_id = $1) AS subscribers,
                       (SELECT count(*) FROM backhauls WHERE pop_id = $1)   AS backhauls
                """,
                pop_id,
            )
            # subscribers.pop_id est en ON DELETE SET NULL : c'est voulu, pour ne
            # pas perdre un historique lors d'une reorganisation. La suppression
            # explicite d'un site doit donc emporter ses abonnes elle-meme,
            # sinon ils resteraient orphelins.
            await conn.execute("DELETE FROM subscribers WHERE pop_id = $1", pop_id)
            supprime = await conn.execute("DELETE FROM pops WHERE id = $1", pop_id)
        if supprime.endswith(" 0"):
            raise LookupError(f"PoP {pop_id} inconnu")
        return {
            "subscribers": compte["subscribers"],
            "backhauls": compte["backhauls"],
        }

    # ------------------------------------------------------------- Abonnes
    async def list_subscribers(
        self,
        *,
        pop_id: int | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT s.id, s.pppoe_login, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE ($1::int IS NULL OR s.pop_id = $1)
                   AND ($2::text IS NULL OR s.pppoe_login ILIKE '%' || $2 || '%')
                 ORDER BY s.pppoe_login
                 LIMIT $3 OFFSET $4
                """,
                pop_id,
                search,
                limit,
                offset,
            )
        return _rows(records)

    async def get_subscriber(self, subscriber_id: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT s.id, s.pppoe_login, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen, s.created_at
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE s.id = $1
                """,
                subscriber_id,
            )
        return dict(record) if record else None

    async def get_subscriber_by_login(self, login: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow("SELECT id FROM subscribers WHERE pppoe_login = $1", login)
        return dict(record) if record else None

    async def subscriber_metrics(
        self,
        subscriber_id: int,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Serie agregee. On prend le max sur le bucket pour les debits :
        une moyenne masquerait les pics de saturation, qui sont justement le
        signal interessant pour la QoE."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(rx_bps) AS rx_bps_avg,
                       avg(tx_bps) AS tx_bps_avg,
                       max(rx_bps) AS rx_bps_max,
                       max(tx_bps) AS tx_bps_max,
                       avg(rtt_ms) AS rtt_ms_avg,
                       max(rtt_ms) AS rtt_ms_max,
                       count(*)    AS samples
                  FROM subscriber_metrics
                 WHERE subscriber_id = $1 AND ts >= $2 AND ts < $3
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                subscriber_id,
                start,
                end,
                timedelta(seconds=bucket_seconds),
            )
        return _rows(records)

    async def subscriber_latest(
        self,
        *,
        pop_id: int | None = None,
        search: str | None = None,
        limit: int = 50,
        order_by: str = "total",
    ) -> list[dict[str, Any]]:
        """Dernier echantillon par abonne, trie par debit (top talkers)."""
        order_sql = {
            "total": "COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0) DESC",
            "down": "COALESCE(tx_bps, 0) DESC",
            "up": "COALESCE(rx_bps, 0) DESC",
            "login": "pppoe_login ASC",
        }.get(order_by, "COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0) DESC")
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                f"""
                SELECT * FROM subscriber_latest
                 WHERE ($1::int IS NULL OR pop_id = $1)
                   AND ($2::text IS NULL OR pppoe_login ILIKE '%' || $2 || '%')
                 ORDER BY {order_sql}
                 LIMIT $3
                """,  # noqa: S608 - order_sql provient d'une liste blanche
                pop_id,
                search,
                limit,
            )
        return _rows(records)

    # ------------------------------------------------------------ Backhauls
    async def list_backhauls(self, *, pop_id: int | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT b.id, b.name, b.pop_id, p.name AS pop_name, b.uisp_device_id,
                       b.nominal_capacity_mbps
                  FROM backhauls b
                  LEFT JOIN pops p ON p.id = b.pop_id
                 WHERE ($1::int IS NULL OR b.pop_id = $1)
                 ORDER BY b.name
                """,
                pop_id,
            )
        return _rows(records)

    async def backhaul_latest(self, *, pop_id: int | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT * FROM backhaul_latest
                 WHERE ($1::int IS NULL OR pop_id = $1)
                 ORDER BY name
                """,
                pop_id,
            )
        return _rows(records)

    async def backhaul_metrics(
        self,
        backhaul_id: int,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Pour la capacite radio on prend aussi le MIN : c'est le creux de capacite
        (fade) qui contraint le debit parent du shaping, pas la moyenne."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(capacity_mbps) AS capacity_mbps_avg,
                       min(capacity_mbps) AS capacity_mbps_min,
                       max(capacity_mbps) AS capacity_mbps_max,
                       avg(signal_dbm)    AS signal_dbm_avg,
                       min(signal_dbm)    AS signal_dbm_min,
                       avg(airtime_pct)   AS airtime_pct_avg,
                       count(*)           AS samples
                  FROM backhaul_metrics
                 WHERE backhaul_id = $1 AND ts >= $2 AND ts < $3
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                backhaul_id,
                start,
                end,
                timedelta(seconds=bucket_seconds),
            )
        return _rows(records)

    # ------------------------------------------------- Vues d'ensemble (UI)
    async def throughput_series(
        self,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 10,
        pop_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Debit agrege de tout le reseau, par pas de temps.

        On somme d'abord par bucket ET par abonne, puis on additionne : sommer
        directement fausserait le total des qu'un abonne a plusieurs echantillons
        dans le meme bucket (ce qui arrive des que le bucket depasse la periode
        de collecte).
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                WITH par_abonne AS (
                    SELECT date_bin($3::interval, m.ts, TIMESTAMPTZ 'epoch') AS bucket,
                           m.subscriber_id,
                           avg(m.rx_bps) AS rx_bps,
                           avg(m.tx_bps) AS tx_bps
                      FROM subscriber_metrics m
                      JOIN subscribers s ON s.id = m.subscriber_id
                     WHERE m.ts >= $1 AND m.ts < $2
                       AND ($4::int IS NULL OR s.pop_id = $4)
                     GROUP BY bucket, m.subscriber_id
                )
                SELECT bucket,
                       sum(rx_bps)  AS rx_bps,
                       sum(tx_bps)  AS tx_bps,
                       count(*)     AS subscribers
                  FROM par_abonne
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                start,
                end,
                timedelta(seconds=bucket_seconds),
                pop_id,
            )
        return _rows(records)

    async def overview(self) -> dict[str, Any]:
        """Chiffres de tete du tableau de bord, en une seule requete."""
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                WITH recent AS (
                    SELECT * FROM subscriber_latest
                     WHERE ts > now() - INTERVAL '2 minutes'
                )
                SELECT
                    (SELECT count(*) FROM subscribers)                     AS subscribers,
                    (SELECT count(*) FROM recent)                          AS online,
                    (SELECT count(*) FROM recent WHERE plan_down_mbps IS NOT NULL)
                                                                           AS shaped,
                    (SELECT coalesce(sum(rx_bps), 0) FROM recent)          AS rx_bps,
                    (SELECT coalesce(sum(tx_bps), 0) FROM recent)          AS tx_bps,
                    (SELECT coalesce(sum(plan_down_mbps), 0) FROM recent)  AS sold_down_mbps,
                    (SELECT coalesce(sum(plan_up_mbps), 0) FROM recent)    AS sold_up_mbps,
                    (SELECT count(*) FROM pops)                            AS pops,
                    (SELECT count(*) FROM backhauls)                       AS backhauls,
                    (SELECT coalesce(sum(capacity_mbps), 0) FROM backhaul_latest
                      WHERE ts > now() - INTERVAL '5 minutes')             AS backhaul_capacity,
                    (SELECT max(ts) FROM subscriber_metrics)               AS last_metric_ts
                """
            )
        result = dict(record) if record else {}
        # Alias lisible cote API sans allonger la requete au-dela de la marge.
        if "backhaul_capacity" in result:
            result["backhaul_capacity_mbps"] = result.pop("backhaul_capacity")
        return result

    async def network_tree(self) -> list[dict[str, Any]]:
        """Arbre PoP -> backhauls + abonnes, avec capacite et charge courante.

        C'est la vue qui donne le rapport le plus utile du systeme : le debit
        reellement ecoule sur un PoP face a la capacite mesuree de sa radio.
        """
        async with self._pool.acquire() as conn:
            pops = await conn.fetch(
                """
                WITH recent AS (
                    SELECT * FROM subscriber_latest
                     WHERE ts > now() - INTERVAL '2 minutes'
                )
                SELECT p.id, p.name, p.router_host,
                       (SELECT count(*) FROM subscribers s WHERE s.pop_id = p.id)
                           AS subscribers,
                       (SELECT count(*) FROM recent r WHERE r.pop_id = p.id)
                           AS online,
                       (SELECT coalesce(sum(r.rx_bps), 0) FROM recent r WHERE r.pop_id = p.id)
                           AS rx_bps,
                       (SELECT coalesce(sum(r.tx_bps), 0) FROM recent r WHERE r.pop_id = p.id)
                           AS tx_bps,
                       (SELECT coalesce(sum(r.plan_down_mbps), 0) FROM recent r
                         WHERE r.pop_id = p.id) AS sold_down_mbps
                  FROM pops p
                 ORDER BY p.name
                """
            )
            backhauls = await conn.fetch("SELECT * FROM backhaul_latest ORDER BY pop_id, name")

        par_pop: dict[int | None, list[dict[str, Any]]] = {}
        for row in backhauls:
            par_pop.setdefault(row["pop_id"], []).append(dict(row))

        return [{**dict(pop), "backhauls": par_pop.get(pop["id"], [])} for pop in pops]

    # ----------------------------------------------------------- Exploitation
    async def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT job, started_at, duration_s, ok, items, error
                  FROM collector_runs
                 ORDER BY started_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return _rows(records)

    async def counters(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM pops)        AS pops,
                       (SELECT count(*) FROM subscribers) AS subscribers,
                       (SELECT count(*) FROM backhauls)   AS backhauls,
                       (SELECT max(ts) FROM subscriber_metrics) AS last_subscriber_metric,
                       (SELECT max(ts) FROM backhaul_metrics)   AS last_backhaul_metric,
                       (SELECT count(*) FROM subscribers
                         WHERE last_seen > now() - INTERVAL '5 minutes') AS active_subscribers
                """
            )
        return dict(record) if record else {}

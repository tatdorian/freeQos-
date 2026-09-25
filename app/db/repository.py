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

from app.services.capacity import SEUIL_PLAFOND


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(record) for record in records]


def _with_effective_limits(row: dict[str, Any]) -> dict[str, Any]:
    """Ajoute la limite REELLEMENT appliquee et d'ou elle vient.

    Calculee avec la meme fonction que le planificateur : l'interface doit
    afficher exactement ce qui sera ecrit sur le routeur. Sans cela, un abonne
    bride a 512 kbps continuerait de s'afficher avec son plan de 500 Mbps.
    """
    from app.enforcement.planner import effective_rate

    descendant, source = effective_rate(
        plan_mbps=row.get("plan_down_mbps"),
        override_mbps=row.get("override_down_mbps"),
        boost_mbps=row.get("boost_down_mbps"),
        boost_expires_at=row.get("boost_expires_at"),
    )
    montant, _ = effective_rate(
        plan_mbps=row.get("plan_up_mbps"),
        override_mbps=row.get("override_up_mbps"),
        boost_mbps=row.get("boost_up_mbps"),
        boost_expires_at=row.get("boost_expires_at"),
    )
    row["effective_down_mbps"] = descendant
    row["effective_up_mbps"] = montant
    row["limit_source"] = source
    return row


class MetricsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ PoPs
    async def list_pops(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT p.id, p.name, p.router_host, p.created_at,
                       p.kind, p.router_name, p.vlan_id, p.vlan_interface,
                       (SELECT count(*) FROM subscribers s WHERE s.pop_id = p.id)
                           AS subscriber_count,
                       (SELECT count(*) FROM backhauls b WHERE b.pop_id = p.id)
                           AS backhaul_count
                  FROM pops p
                 ORDER BY p.name
                """
            )
        return _rows(records)

    async def pop_sites(self) -> list[dict[str, Any]]:
        """Les sites et le ROUTEUR qui dessert chacun. Sans compteur, sans jointure.

        C'est la table de correspondance dont le shaping a besoin : un abonne
        range dans un site de VLAN doit retrouver le routeur qui le bride. La
        version complete (``list_pops``) compte les abonnes et les backhauls,
        ce qui n'a pas sa place dans un chemin appele a chaque plan.
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT name, kind, router_name, vlan_id, vlan_interface
                  FROM pops
                 ORDER BY name
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
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT s.id, s.login, s.kind, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE ($1::int IS NULL OR s.pop_id = $1)
                   AND ($2::text IS NULL OR s.login ILIKE '%' || $2 || '%')
                   AND ($3::text IS NULL OR s.kind = $3)
                 ORDER BY s.login
                 LIMIT $4 OFFSET $5
                """,
                pop_id,
                search,
                kind,
                limit,
                offset,
            )
        return _rows(records)

    async def get_subscriber(self, subscriber_id: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT s.id, s.login, s.kind, s.pop_id, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.plan_source,
                       host(s.last_ip) AS last_ip, s.last_seen, s.created_at,
                       pol.max_down_mbps AS override_down_mbps,
                       pol.max_up_mbps   AS override_up_mbps,
                       pol.boost_down_mbps, pol.boost_up_mbps, pol.boost_expires_at,
                       pol.boost_reason, pol.note AS policy_note
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                  LEFT JOIN shaping_policies pol
                         ON pol.scope = 'subscriber' AND pol.target_key = s.login
                 WHERE s.id = $1
                """,
                subscriber_id,
            )
        return _with_effective_limits(dict(record)) if record else None

    async def get_subscriber_by_login(self, login: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow("SELECT id, kind FROM subscribers WHERE login = $1", login)
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

    async def bufferbloat(
        self,
        *,
        minutes: int = 60,
        pop_id: int | None = None,
        subscriber_id: int | None = None,
    ) -> dict[str, Any]:
        """Note de bufferbloat par abonne : latence a vide vs sous charge.

        On lit les echantillons deja collectes — RTT (sonde ``/ping``) et debit
        du meme point — puis on delegue la correlation a un module pur
        (``app.services.bufferbloat``). Aucun test dedie n'est necessaire :
        c'est la correlation RTT/debit annoncee comme la phase 3 du README.

        Ne remontent que les abonnes pour lesquels on peut CONCLURE : ceux qui
        n'ont jamais chargé le lien restent absents, avec leur compte a part —
        afficher une note optimiste sur un abonne silencieux serait trompeur.
        """
        from app.services.bufferbloat import compute_bufferbloat, summarize
        from app.services.qoe import compute_qoe

        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT m.subscriber_id, s.login, s.kind, p.name AS pop_name,
                       m.rtt_ms,
                       COALESCE(m.rx_bps, 0) + COALESCE(m.tx_bps, 0) AS load_bps
                  FROM subscriber_metrics m
                  JOIN subscribers s ON s.id = m.subscriber_id
                  LEFT JOIN pops p   ON p.id = s.pop_id
                 WHERE m.ts > now() - $1::interval
                   AND m.rtt_ms IS NOT NULL
                   AND ($2::int IS NULL OR s.pop_id = $2)
                   AND ($3::bigint IS NULL OR m.subscriber_id = $3)
                 ORDER BY m.subscriber_id, m.ts
                """,
                timedelta(minutes=minutes),
                pop_id,
                subscriber_id,
            )

        par_abonne: dict[int, dict[str, Any]] = {}
        for record in records:
            entry = par_abonne.setdefault(
                record["subscriber_id"],
                {
                    "subscriber_id": record["subscriber_id"],
                    "login": record["login"],
                    "kind": record["kind"],
                    "pop_name": record["pop_name"],
                    "samples": [],
                },
            )
            entry["samples"].append((record["rtt_ms"], record["load_bps"]))

        notes: list[dict[str, Any]] = []
        verdicts = []
        indetermines = 0
        for entry in par_abonne.values():
            verdict = compute_bufferbloat(entry["samples"])
            if verdict is None:
                indetermines += 1
                continue
            verdicts.append(verdict)
            # Score de QoE composite du meme abonne. C'est la MEME fonction que
            # la heatmap Executif et que la boucle fermee de la phase 4 : un seul
            # bareme, sinon l'ecran et le declencheur finissent par diverger.
            note = compute_qoe(rtt_ms=verdict.idle_ms, bloat_ms=verdict.bloat_ms)
            notes.append(
                {
                    "subscriber_id": entry["subscriber_id"],
                    "login": entry["login"],
                    "kind": entry["kind"],
                    "pop_name": entry["pop_name"],
                    **verdict.as_dict(),
                    "qoe": note.as_dict() if note else None,
                }
            )

        # Le pire bufferbloat en tete : c'est l'abonne dont l'experience se
        # degrade le plus, donc celui a regarder d'abord.
        notes.sort(key=lambda r: r["bloat_ms"], reverse=True)
        synthese = summarize(verdicts)
        synthese["indeterminate"] = indetermines
        synthese["candidates"] = len(par_abonne)
        return {
            "window_minutes": minutes,
            "summary": synthese,
            "subscribers": notes,
        }

    async def qoe_subscribers(
        self, *, minutes: int = 15, pop_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Score de QoE composite par abonne, pour la boucle fermee (phase 4).

        Projection de ``bufferbloat()`` : meme fenetre, memes echantillons, meme
        fonction de score que la heatmap Executif. Il n'y a volontairement PAS de
        second chemin de calcul — un declencheur qui reagirait a un score
        different de celui affiche a l'ecran serait indefendable.

        Ne remontent que les abonnes pour lesquels on a pu CONCLURE : un abonne
        silencieux, ou jamais sonde, n'a pas de note et ne doit peser sur aucune
        decision.
        """
        detail = await self.bufferbloat(minutes=minutes, pop_id=pop_id)
        lignes: list[dict[str, Any]] = []
        for row in detail["subscribers"]:
            note = row.get("qoe")
            if not note:
                continue
            lignes.append(
                {
                    "subscriber_id": row["subscriber_id"],
                    "login": row["login"],
                    "pop_name": row["pop_name"],
                    "score": note["score"],
                    "severity": note["severity"],
                    "basis": note["basis"],
                    "grade": note["grade"],
                    "bloat_ms": note["bloat_ms"],
                    "rtt_ms": note["rtt_ms"],
                }
            )
        return lignes

    async def heatmap(self, *, minutes: int = 15, buckets: int = 15) -> dict[str, Any]:
        """Heatmap executif facon LibreQoS : QoE, RTT et utilisation dans le temps.

        Chaque ligne est une bande de cellules colorees (une par pas de temps).
        Tout vient des series deja collectees. La ligne des retransmissions TCP
        est presente mais marquee INDISPONIBLE : hors-bande, on ne voit pas les
        paquets, donc on ne l'invente pas.
        """
        bucket_s = max(60, (minutes * 60) // max(1, buckets))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH par_bucket AS (
                    SELECT date_bin($2::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                           subscriber_id,
                           avg(rtt_ms) AS rtt,
                           avg(COALESCE(tx_bps, 0)) AS tx,
                           avg(COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0)) AS charge
                      FROM subscriber_metrics
                     WHERE ts > now() - $1::interval
                     GROUP BY bucket, subscriber_id
                )
                SELECT bucket,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY rtt)
                           FILTER (WHERE rtt IS NOT NULL) AS rtt_p50,
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY rtt)
                           FILTER (WHERE rtt IS NOT NULL) AS rtt_p90,
                       sum(tx) AS tx_sum,
                       -- Deux tableaux PARALLELES (meme FILTER, meme ORDER BY) :
                       -- un abonne = un indice. C'est ce qui permet de calculer la
                       -- latence SOUS CHARGE du pas avec la meme fonction que la
                       -- note A+..F par abonne, plutot qu'un proxy sur le RTT seul.
                       array_agg(rtt ORDER BY subscriber_id)
                           FILTER (WHERE rtt IS NOT NULL) AS rtt_samples,
                       array_agg(charge ORDER BY subscriber_id)
                           FILTER (WHERE rtt IS NOT NULL) AS load_samples
                  FROM par_bucket
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                timedelta(minutes=minutes),
                timedelta(seconds=bucket_s),
            )
            sold_down = await conn.fetchval(
                "SELECT coalesce(sum(plan_down_mbps), 0) FROM subscribers"
            )
            if not sold_down:
                sold_down = await conn.fetchval(
                    "SELECT coalesce(sum(capacity_mbps), 0) FROM backhaul_latest "
                    "WHERE ts > now() - INTERVAL '5 minutes'"
                )

        from app.services.heatmap import build_heatmap

        return build_heatmap(
            [dict(r) for r in rows],
            minutes=minutes,
            buckets=buckets,
            bucket_seconds=bucket_s,
            reference_down_bps=(float(sold_down) * 1e6) if sold_down else 0.0,
        )

    async def subscriber_latest(
        self,
        *,
        pop_id: int | None = None,
        search: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        order_by: str = "total",
        include_unmeasured: bool = False,
    ) -> list[dict[str, Any]]:
        """Les abonnes d'un PoP, avec leur dernier echantillon quand il existe.

        LA REQUETE PART DE L'EFFECTIF, PAS DES MESURES. Elle lisait autrefois la
        vue ``subscriber_latest``, c'est-a-dire les abonnes qui ont AU MOINS UNE
        mesure : un abonne declare qui ne s'est jamais connecte, ou dont le PoP
        n'est plus collecte, n'apparaissait nulle part. Il etait indiscernable
        d'un abonne qui n'existe pas -- alors qu'il est facture.

        ``include_unmeasured`` decide lequel des deux ensembles on veut :

          - ``False`` (defaut) : seulement ceux qui ont une mesure. C'est ce que
            demande le classement par debit -- un abonne sans mesure n'a pas sa
            place dans un "top talkers" ;
          - ``True`` : TOUT l'effectif du PoP. Les abonnes sans mesure sortent
            avec des debits a NULL, jamais a zero : un trou se lit comme une
            absence d'information, un zero comme une absence de trafic.
        """
        order_sql = {
            "total": "COALESCE(l.rx_bps, 0) + COALESCE(l.tx_bps, 0) DESC, s.login ASC",
            "down": "COALESCE(l.tx_bps, 0) DESC, s.login ASC",
            "up": "COALESCE(l.rx_bps, 0) DESC, s.login ASC",
            "login": "s.login ASC",
        }.get(order_by, "COALESCE(l.rx_bps, 0) + COALESCE(l.tx_bps, 0) DESC, s.login ASC")
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                f"""
                SELECT s.id AS subscriber_id, s.login, s.kind, s.pop_id,
                       pop.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps, s.last_seen, s.last_ip,
                       l.ts, l.rx_bps, l.tx_bps, l.rtt_ms, l.session_uptime_s,
                       p.max_down_mbps  AS override_down_mbps,
                       p.max_up_mbps    AS override_up_mbps,
                       p.boost_down_mbps,
                       p.boost_up_mbps,
                       p.boost_expires_at,
                       p.boost_reason,
                       p.note           AS policy_note
                  FROM subscribers s
                  LEFT JOIN pops pop ON pop.id = s.pop_id
                  LEFT JOIN subscriber_latest l ON l.subscriber_id = s.id
                  LEFT JOIN shaping_policies p
                         ON p.scope = 'subscriber' AND p.target_key = s.login
                 WHERE ($1::int IS NULL OR s.pop_id = $1)
                   AND ($2::text IS NULL OR s.login ILIKE '%' || $2 || '%')
                   AND ($3::text IS NULL OR s.kind = $3)
                   AND ($5::bool OR l.subscriber_id IS NOT NULL)
                 ORDER BY {order_sql}
                 LIMIT $4
                """,  # noqa: S608 - order_sql provient d'une liste blanche
                pop_id,
                search,
                kind,
                limit,
                include_unmeasured,
            )
        return [_with_effective_limits(dict(record)) for record in records]

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
                ),
                -- LA POINTE, PAS SEULEMENT LA MOYENNE. Un cycle de collecte
                -- ecrit tous les abonnes sous le MEME horodatage : la somme
                -- par horodatage est le debit du reseau a cet instant. Sur un
                -- pas de quelques minutes, un test de debit de 20 s se noie
                -- dans la moyenne ; son maximum, lui, reste visible.
                par_cycle AS (
                    SELECT date_bin($3::interval, m.ts, TIMESTAMPTZ 'epoch') AS bucket,
                           sum(m.rx_bps) AS rx_bps,
                           sum(m.tx_bps) AS tx_bps
                      FROM subscriber_metrics m
                      JOIN subscribers s ON s.id = m.subscriber_id
                     WHERE m.ts >= $1 AND m.ts < $2
                       AND ($4::int IS NULL OR s.pop_id = $4)
                     GROUP BY m.ts
                ),
                pointes AS (
                    SELECT bucket, max(rx_bps) AS rx_peak_bps, max(tx_bps) AS tx_peak_bps
                      FROM par_cycle
                     GROUP BY bucket
                )
                SELECT a.bucket,
                       sum(a.rx_bps)  AS rx_bps,
                       sum(a.tx_bps)  AS tx_bps,
                       count(*)       AS subscribers,
                       max(p.rx_peak_bps) AS rx_peak_bps,
                       max(p.tx_peak_bps) AS tx_peak_bps
                  FROM par_abonne a
                  LEFT JOIN pointes p ON p.bucket = a.bucket
                 GROUP BY a.bucket
                 ORDER BY a.bucket
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
    # ------------------------------------------------------------------
    # Capacite vendue, capacite reelle, et ce qui se passe entre les deux
    # ------------------------------------------------------------------
    async def capacity_by_pop(self, *, hours: int = 24) -> list[dict[str, Any]]:
        """Par PoP : ce qui est vendu, ce qui porte, et la pointe reellement vue.

        TROIS CHIFFRES, ET C'EST LEUR RAPPROCHEMENT QUI COMPTE. Vendre vingt fois
        la capacite d'un site ne se voit pas tant que la pointe reste au tiers du
        lien ; cela se voit tres bien quand elle le frole. Rendre le taux de
        survente seul serait donc un chiffre a sensation ; il vient ici avec la
        pointe qui le rend lisible.

        La pointe est calculee par INSTANT DE MESURE : on additionne d'abord les
        abonnes d'un meme horodatage, puis on prend le maximum. Prendre le
        maximum de chaque abonne et les additionner donnerait une pointe que
        personne n'a jamais vue -- tous les abonnes ne saturent pas a la meme
        seconde.
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                WITH vendu AS (
                    SELECT p.id   AS pop_id,
                           p.name AS pop_name,
                           count(s.id) FILTER (
                               WHERE s.plan_down_mbps IS NOT NULL
                           )                                        AS subscribers,
                           coalesce(sum(s.plan_down_mbps), 0)       AS sold_down_mbps,
                           coalesce(sum(s.plan_up_mbps), 0)         AS sold_up_mbps
                      FROM pops p
                      LEFT JOIN subscribers s ON s.pop_id = p.id
                     GROUP BY p.id, p.name
                ),
                capacite AS (
                    SELECT pop_id, sum(capacity_mbps) AS capacity_mbps
                      FROM backhaul_latest
                     WHERE capacity_mbps IS NOT NULL
                     GROUP BY pop_id
                ),
                instants AS (
                    SELECT s.pop_id, m.ts, sum(m.tx_bps) AS total_bps
                      FROM subscriber_metrics m
                      JOIN subscribers s ON s.id = m.subscriber_id
                     WHERE m.ts > now() - make_interval(hours => $1)
                     GROUP BY s.pop_id, m.ts
                ),
                pointe AS (
                    SELECT pop_id, max(total_bps) AS peak_bps
                      FROM instants
                     GROUP BY pop_id
                )
                SELECT v.pop_name,
                       v.subscribers,
                       v.sold_down_mbps,
                       v.sold_up_mbps,
                       c.capacity_mbps,
                       pt.peak_bps
                  FROM vendu v
                  LEFT JOIN capacite c ON c.pop_id = v.pop_id
                  LEFT JOIN pointe   pt ON pt.pop_id = v.pop_id
                 ORDER BY v.sold_down_mbps DESC, v.pop_name
                """,
                hours,
            )
        return _rows(records)

    async def link_occupancy(self, *, hours: int = 24, limit: int = 30) -> list[dict[str, Any]]:
        """Par port : l'occupation atteinte, et L'HEURE a laquelle elle l'a ete.

        "Combien passe maintenant" se lit sur une courbe. "Quand ce lien
        sature-t-il, et a combien de sa capacite" ne s'en lit pas, et c'est
        pourtant ce chiffre qui decide d'un investissement.

        Le nom du lien vient de la topologie quand elle le connait : un
        exploitant raisonne sur "NAS-AGADEZ", pas sur "ether3".
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                WITH mesures AS (
                    SELECT router_name, interface,
                           max(capacity_mbps)                                   AS capacity_mbps,
                           max(rx_bps)                                          AS peak_rx_bps,
                           max(tx_bps)                                          AS peak_tx_bps,
                           avg(greatest(coalesce(rx_bps, 0), coalesce(tx_bps, 0))) AS avg_bps,
                           -- Le nombre de mesures : une moyenne sur trois points
                           -- ne justifie pas d'acheter un backhaul.
                           count(*)                                             AS samples,
                           (array_agg(ts ORDER BY rx_bps DESC NULLS LAST))[1]   AS peak_rx_at,
                           (array_agg(ts ORDER BY tx_bps DESC NULLS LAST))[1]   AS peak_tx_at
                      FROM interface_metrics
                     WHERE ts > now() - make_interval(hours => $1)
                     GROUP BY router_name, interface
                ),
                noms AS (
                    -- Le nom vient du NOEUD d'en face : topology_links ne porte
                    -- que des cles. Le lien le plus recemment vu gagne quand
                    -- plusieurs voisins partagent le meme port (un switch entre
                    -- les deux) -- et l'interface reste affichee a cote, parce
                    -- que le debit, lui, est bien celui du port.
                    SELECT DISTINCT ON (l.discovered_by, l.interface)
                           l.discovered_by, l.interface, n.name AS target_name
                      FROM topology_links l
                      LEFT JOIN topology_nodes n ON n.key = l.target_key
                     WHERE l.interface IS NOT NULL
                     ORDER BY l.discovered_by, l.interface, l.last_seen DESC
                )
                SELECT m.*, n.target_name AS link_name
                  FROM mesures m
                  LEFT JOIN noms n
                         ON n.discovered_by = m.router_name AND n.interface = m.interface
                 ORDER BY greatest(coalesce(m.peak_rx_bps, 0), coalesce(m.peak_tx_bps, 0)) DESC
                 LIMIT $2
                """,
                hours,
                limit,
            )
        return _rows(records)

    async def subscriber_usage(self, *, hours: int = 168, limit: int = 20) -> list[dict[str, Any]]:
        """Par abonne : le VOLUME consomme, et le temps passe a son plafond.

        Le top des debits instantanes designe celui qui telecharge a cet
        instant ; le volume sur une semaine designe celui qui pese sur le
        reseau. Ce ne sont presque jamais les memes abonnes, et c'est le second
        qui sert a dimensionner.

        LE VOLUME EST UNE INTEGRATION DU DEBIT MESURE, pas un releve de
        compteurs : les compteurs d'une session PPPoE repartent de zero a chaque
        reconnexion, et les additionner produirait des volumes fantaisistes.
        L'integration utilise la cadence REELLE de l'abonne (duree observee
        divisee par le nombre d'intervalles) : un abonne mesure deux fois moins
        souvent n'en sort pas deux fois plus leger. Chaque echantillon porte les
        secondes qui l'ont PRECEDE, puisque son debit vient d'un delta de
        compteurs sur cet intervalle.

        Un abonne qui n'a qu'un seul echantillon sort a zero : d'un point unique
        aucune duree ne se deduit, et inventer un intervalle par defaut serait
        inventer du volume.
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT m.subscriber_id,
                       s.login,
                       s.kind,
                       p.name AS pop_name,
                       s.plan_down_mbps,
                       count(*)                                        AS samples,
                       max(m.tx_bps)                                   AS peak_bps,
                       avg(m.tx_bps)                                   AS avg_bps,
                       count(*) FILTER (
                           WHERE s.plan_down_mbps IS NOT NULL
                             AND m.tx_bps >= $3 * s.plan_down_mbps * 1000000
                       )                                               AS capped_samples,
                       max(m.ts) FILTER (
                           WHERE coalesce(m.rx_bps, 0) + coalesce(m.tx_bps, 0) > 0
                       )                                               AS last_traffic_at,
                       -- Integration : somme des debits x cadence observee / 8.
                       coalesce(sum(coalesce(m.rx_bps, 0) + coalesce(m.tx_bps, 0)), 0) / 8.0
                         * coalesce(
                               extract(epoch FROM (max(m.ts) - min(m.ts)))
                                 / nullif(count(*) - 1, 0),
                               0
                           )                                           AS bytes
                  FROM subscriber_metrics m
                  JOIN subscribers s ON s.id = m.subscriber_id
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE m.ts > now() - make_interval(hours => $1)
                 GROUP BY m.subscriber_id, s.login, s.kind, p.name, s.plan_down_mbps
                 ORDER BY bytes DESC
                 LIMIT $2
                """,
                hours,
                limit,
                SEUIL_PLAFOND,
            )
        return _rows(records)

    async def silent_subscribers(self, *, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
        """Abonnes declares dont plus rien n'est passe depuis N jours.

        Une ligne qui ne consomme plus est soit un depart qu'on facture encore,
        soit une panne que personne n'a signalee. Les deux valent qu'on regarde,
        et aucun ecran ne les montrait : le tableau de bord ne parle que de ce
        qui est EN LIGNE.
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                """
                SELECT s.id AS subscriber_id, s.login, s.kind, p.name AS pop_name,
                       s.plan_down_mbps, s.last_seen,
                       (SELECT max(m.ts)
                          FROM subscriber_metrics m
                         WHERE m.subscriber_id = s.id
                           AND coalesce(m.rx_bps, 0) + coalesce(m.tx_bps, 0) > 0
                       ) AS last_traffic_at
                  FROM subscribers s
                  LEFT JOIN pops p ON p.id = s.pop_id
                 WHERE NOT EXISTS (
                           SELECT 1 FROM subscriber_metrics m
                            WHERE m.subscriber_id = s.id
                              AND m.ts > now() - make_interval(days => $1)
                              AND coalesce(m.rx_bps, 0) + coalesce(m.tx_bps, 0) > 0
                       )
                 ORDER BY s.last_seen DESC NULLS LAST, s.login
                 LIMIT $2
                """,
                days,
                limit,
            )
        return _rows(records)

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

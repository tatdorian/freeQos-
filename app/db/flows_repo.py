"""Persistance et lecture du trafic mesure par NetFlow.

DEUX DEPOTS, DEUX NATURES
-------------------------
``NetflowExportersRepository`` porte la DECLARATION : qui a le droit d'exporter,
d'ou il regarde le reseau, et a quel taux d'echantillonnage. C'est une saisie
d'exploitant, comme l'inventaire de routeurs.

``FlowsRepository`` porte la MESURE : des octets par abonne, par usage, et une
liste d'hotes vus qui ne sert qu'a aider la saisie.

La separation n'est pas cosmetique : la mesure s'efface (retention), la
declaration non.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from app.services.flows import FlushBatch

logger = logging.getLogger(__name__)

EXPORTER_COLUMNS = """
    id, host(address) AS address, name, vantage, pop_name, sampling_rate,
    enabled, note, last_version, last_seen, packets_seen, flows_seen,
    created_at, updated_at
"""

CHAMPS_EXPORTEUR = ("name", "vantage", "pop_name", "sampling_rate", "enabled", "note")


class ExporterNotFoundError(LookupError):
    pass


class NetflowExportersRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {EXPORTER_COLUMNS} FROM netflow_exporters "  # noqa: S608
                "ORDER BY vantage, address"
            )
        return [dict(row) for row in rows]

    async def declare(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Declare (ou corrige) un exporteur."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO netflow_exporters (address, name, vantage, pop_name,
                                               sampling_rate, enabled, note)
                VALUES ($1::inet, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (address) DO UPDATE
                   SET name = EXCLUDED.name,
                       vantage = EXCLUDED.vantage,
                       pop_name = EXCLUDED.pop_name,
                       sampling_rate = EXCLUDED.sampling_rate,
                       enabled = EXCLUDED.enabled,
                       note = EXCLUDED.note,
                       updated_at = now()
                RETURNING {EXPORTER_COLUMNS}
                """,  # noqa: S608
                str(payload["address"]),
                payload.get("name"),
                str(payload.get("vantage") or "pop"),
                payload.get("pop_name"),
                int(payload.get("sampling_rate") or 1),
                bool(payload.get("enabled", True)),
                payload.get("note"),
            )
        return dict(row)

    async def update(self, exporter_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        champs = {k: v for k, v in payload.items() if k in CHAMPS_EXPORTEUR}
        if not champs:
            return await self.get(exporter_id)
        colonnes = ", ".join(f"{nom} = ${i + 2}" for i, nom in enumerate(champs))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE netflow_exporters SET {colonnes}, updated_at = now()
                WHERE id = $1 RETURNING {EXPORTER_COLUMNS}
                """,  # noqa: S608
                exporter_id,
                *champs.values(),
            )
        if row is None:
            raise ExporterNotFoundError(f"exporteur {exporter_id} inconnu")
        return dict(row)

    async def get(self, exporter_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {EXPORTER_COLUMNS} FROM netflow_exporters WHERE id = $1",  # noqa: S608
                exporter_id,
            )
        if row is None:
            raise ExporterNotFoundError(f"exporteur {exporter_id} inconnu")
        return dict(row)

    async def delete(self, exporter_id: int) -> None:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM netflow_exporters WHERE id = $1", exporter_id
            )
        if resultat.endswith(" 0"):
            raise ExporterNotFoundError(f"exporteur {exporter_id} inconnu")

    async def record_activity(self, stats: dict[str, dict[str, Any]]) -> None:
        """Enregistre l'activite observee, et INSCRIT les exporteurs inconnus.

        Un exporteur qui n'a pas ete declare est cree en 'unknown' plutot
        qu'ignore. C'est deliberé : un PoP qui exporte vers un collecteur qui
        l'ignore en silence reste invisible pendant des semaines, et personne ne
        comprend pourquoi ses chiffres manquent. La ligne apparait dans
        l'interface, l'exploitant n'a plus qu'a dire d'ou elle regarde.
        """
        if not stats:
            return
        lignes = [
            (
                adresse,
                str(donnees.get("version") or ""),
                int(donnees.get("packets") or 0),
                int(donnees.get("flows") or 0),
            )
            for adresse, donnees in stats.items()
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO netflow_exporters (address, last_version, packets_seen,
                                               flows_seen, last_seen)
                VALUES ($1::inet, $2, $3, $4, now())
                ON CONFLICT (address) DO UPDATE
                   SET last_version = EXCLUDED.last_version,
                       packets_seen = netflow_exporters.packets_seen + EXCLUDED.packets_seen,
                       flows_seen  = netflow_exporters.flows_seen + EXCLUDED.flows_seen,
                       last_seen = now()
                """,
                lignes,
            )


class FlowsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write_batch(self, batch: FlushBatch) -> int:
        """Ecrit une fenetre agregee. Rend le nombre de lignes d'abonnes.

        Le ``WHERE EXISTS`` sur subscribers n'est pas une precaution de style :
        un abonne peut disparaitre entre la mesure et l'ecriture (fiche
        supprimee, PoP retire), et une violation de cle etrangere ferait perdre
        la fenetre ENTIERE -- donc le trafic de tous les autres.
        """
        if batch.empty:
            return 0
        async with self._pool.acquire() as conn, conn.transaction():
            if batch.subscribers:
                await conn.executemany(
                    """
                    INSERT INTO flow_metrics (ts, subscriber_id, vantage, down_bytes,
                                              up_bytes, down_packets, up_packets, flows)
                    SELECT $1, $2, $3, $4, $5, $6, $7, $8
                    WHERE EXISTS (SELECT 1 FROM subscribers WHERE id = $2)
                    ON CONFLICT (subscriber_id, vantage, ts) DO UPDATE
                       SET down_bytes   = flow_metrics.down_bytes + EXCLUDED.down_bytes,
                           up_bytes     = flow_metrics.up_bytes + EXCLUDED.up_bytes,
                           down_packets = flow_metrics.down_packets + EXCLUDED.down_packets,
                           up_packets   = flow_metrics.up_packets + EXCLUDED.up_packets,
                           flows        = flow_metrics.flows + EXCLUDED.flows
                    """,
                    [
                        (
                            batch.ts,
                            c.subscriber_id,
                            c.vantage,
                            c.down_bytes,
                            c.up_bytes,
                            c.down_packets,
                            c.up_packets,
                            c.flows,
                        )
                        for c in batch.subscribers
                    ],
                )
            if batch.apps:
                await conn.executemany(
                    """
                    INSERT INTO flow_app_metrics (ts, subscriber_id, app, down_bytes, up_bytes)
                    SELECT $1, $2, $3, $4, $5
                    WHERE EXISTS (SELECT 1 FROM subscribers WHERE id = $2)
                    ON CONFLICT (subscriber_id, app, ts) DO UPDATE
                       SET down_bytes = flow_app_metrics.down_bytes + EXCLUDED.down_bytes,
                           up_bytes   = flow_app_metrics.up_bytes + EXCLUDED.up_bytes
                    """,
                    [
                        (batch.ts, c.subscriber_id, c.app, c.down_bytes, c.up_bytes)
                        for c in batch.apps
                    ],
                )
            if batch.destinations:
                await conn.executemany(
                    """
                    INSERT INTO flow_destinations (subscriber_id, address, port, protocol,
                                                   app, down_bytes, up_bytes, flows,
                                                   first_seen, last_seen)
                    SELECT $1, $2::inet, $3, $4, $5, $6, $7, $8, $9, $9
                    WHERE EXISTS (SELECT 1 FROM subscribers WHERE id = $1)
                    ON CONFLICT (subscriber_id, address) DO UPDATE
                       SET down_bytes = flow_destinations.down_bytes + EXCLUDED.down_bytes,
                           up_bytes   = flow_destinations.up_bytes + EXCLUDED.up_bytes,
                           flows      = flow_destinations.flows + EXCLUDED.flows,
                           port       = EXCLUDED.port,
                           protocol   = EXCLUDED.protocol,
                           app        = EXCLUDED.app,
                           last_seen  = EXCLUDED.last_seen
                    """,
                    [
                        (
                            d.subscriber_id,
                            d.address,
                            d.port,
                            d.protocol,
                            d.app,
                            d.down_bytes,
                            d.up_bytes,
                            d.flows,
                            batch.ts,
                        )
                        for d in batch.destinations
                    ],
                )
                # LA FILE D'ATTENTE DE L'ENRICHISSEMENT.
                #
                # Une adresse jamais vue entre ici avec resolved_at a NULL, et
                # c'est exactement ce que le resolveur vient chercher. C'est ce
                # qui rend la decouverte dynamique : personne n'a a declarer
                # qu'une nouvelle adresse existe, le fait de l'avoir vue suffit.
                # Une adresse deja connue ne voit que sa date bougee -- son
                # verdict n'est pas recalcule pour rien.
                await conn.executemany(
                    """
                    INSERT INTO ip_intel (address, first_seen, last_seen)
                    VALUES ($1::inet, $2, $2)
                    ON CONFLICT (address) DO UPDATE SET last_seen = EXCLUDED.last_seen
                    """,
                    [(d.address, batch.ts) for d in _adresses_uniques(batch.destinations)],
                )
            if batch.hosts:
                await conn.executemany(
                    """
                    INSERT INTO flow_hosts (address, vlan_id, exporter, pop_name,
                                            down_bytes, up_bytes, first_seen, last_seen)
                    VALUES ($1::inet, $2, $3::inet, $4, $5, $6, $7, $7)
                    ON CONFLICT (address, vlan_id) DO UPDATE
                       SET down_bytes = flow_hosts.down_bytes + EXCLUDED.down_bytes,
                           up_bytes   = flow_hosts.up_bytes + EXCLUDED.up_bytes,
                           exporter   = COALESCE(EXCLUDED.exporter, flow_hosts.exporter),
                           pop_name   = COALESCE(EXCLUDED.pop_name, flow_hosts.pop_name),
                           last_seen  = EXCLUDED.last_seen
                    """,
                    [
                        (
                            c.address,
                            # 0 = aucune etiquette VLAN (cf. schema) : un
                            # exporteur purement L3 n'en voit jamais.
                            c.vlan_id or 0,
                            c.exporter,
                            c.pop_name,
                            c.down_bytes,
                            c.up_bytes,
                            batch.ts,
                        )
                        for c in batch.hosts
                    ],
                )
        return len(batch.subscribers)

    async def subscriber_prefixes(self) -> list[tuple[str, int]]:
        """Tous les blocs declares, avec l'abonne qu'ils designent.

        Reunit les deux natures d'abonnes : l'adresse de la session en cours
        pour un PPPoE, le bloc declare (et ses prefixes additionnels) pour un
        client a IP fixe. C'est cet index qui decide a qui appartient un octet.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id,
                       CASE WHEN c.address IS NOT NULL
                            THEN host(c.address) || '/' || masklen(c.address)
                            ELSE host(s.last_ip) || '/' ||
                                 CASE WHEN family(s.last_ip) = 4 THEN 32 ELSE 128 END
                       END AS prefix,
                       c.extra_prefixes
                FROM subscribers s
                LEFT JOIN static_clients c
                       ON c.reference = s.login AND c.enabled
                WHERE c.address IS NOT NULL OR s.last_ip IS NOT NULL
                """
            )
        sortie: list[tuple[str, int]] = []
        for row in rows:
            if row["prefix"]:
                sortie.append((str(row["prefix"]), int(row["id"])))
            for supplement in _as_list(row["extra_prefixes"]):
                sortie.append((str(supplement), int(row["id"])))
        return sortie

    async def subscribers_by_id(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Login, nature et PoP de ces abonnes, indexes par identifiant.

        La vue "en direct" vient de la memoire du collecteur, qui ne connait que
        des identifiants. Afficher '#42' a un exploitant ne l'aide pas : il
        raisonne en logins.
        """
        if not ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.login, s.kind, p.name AS pop_name,
                       s.plan_down_mbps, s.plan_up_mbps
                FROM subscribers s
                LEFT JOIN pops p ON p.id = s.pop_id
                WHERE s.id = ANY($1::bigint[])
                """,
                sorted(set(ids)),
            )
        return {int(row["id"]): dict(row) for row in rows}

    async def prefixes_for_logins(self, logins: list[str]) -> dict[str, list[str]]:
        """Les adresses de CES abonnes-la, par login.

        Sert a borner une restriction a quelques clients. Les deux natures sont
        reunies, comme partout ailleurs : l'adresse de la session en cours pour
        un PPPoE, le bloc declare (et ses prefixes additionnels) pour un client
        a IP fixe. Un login sans adresse connue rend une liste vide -- l'appelant
        doit pouvoir dire "ce client n'a pas d'adresse, sa regle ne vise rien"
        plutot que de poser une regle qui viserait tout le monde.
        """
        if not logins:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.login,
                       CASE WHEN c.address IS NOT NULL
                            THEN host(c.address) || '/' || masklen(c.address)
                            ELSE host(s.last_ip) || '/' ||
                                 CASE WHEN family(s.last_ip) = 4 THEN 32 ELSE 128 END
                       END AS prefix,
                       c.extra_prefixes
                FROM subscribers s
                LEFT JOIN static_clients c
                       ON c.reference = s.login AND c.enabled
                WHERE s.login = ANY($1::text[])
                  AND (c.address IS NOT NULL OR s.last_ip IS NOT NULL)
                """,
                logins,
            )
        sortie: dict[str, list[str]] = {login: [] for login in logins}
        for row in rows:
            login = str(row["login"])
            if row["prefix"]:
                sortie.setdefault(login, []).append(str(row["prefix"]))
            for supplement in _as_list(row["extra_prefixes"]):
                sortie.setdefault(login, []).append(str(supplement))
        return sortie

    async def top_subscribers(
        self, *, minutes: int, vantage: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT f.subscriber_id,
                       s.login,
                       s.kind,
                       p.name AS pop_name,
                       s.plan_down_mbps,
                       s.plan_up_mbps,
                       sum(f.down_bytes)::bigint AS down_bytes,
                       sum(f.up_bytes)::bigint   AS up_bytes,
                       sum(f.flows)::bigint      AS flows,
                       max(f.ts)                 AS last_ts
                FROM flow_metrics f
                JOIN subscribers s ON s.id = f.subscriber_id
                LEFT JOIN pops p   ON p.id = s.pop_id
                WHERE f.vantage = $1 AND f.ts >= now() - make_interval(mins => $2)
                GROUP BY f.subscriber_id, s.login, s.kind, p.name,
                         s.plan_down_mbps, s.plan_up_mbps
                ORDER BY (sum(f.down_bytes) + sum(f.up_bytes)) DESC
                LIMIT $3
                """,
                vantage,
                minutes,
                limit,
            )
        return [dict(row) for row in rows]

    async def totals(self, *, minutes: int, vantage: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT coalesce(sum(down_bytes), 0)::bigint AS down_bytes,
                       coalesce(sum(up_bytes), 0)::bigint   AS up_bytes,
                       coalesce(sum(flows), 0)::bigint      AS flows,
                       count(DISTINCT subscriber_id)        AS subscribers,
                       max(ts)                              AS last_ts
                FROM flow_metrics
                WHERE vantage = $1 AND ts >= now() - make_interval(mins => $2)
                """,
                vantage,
                minutes,
            )
        return dict(row) if row is not None else {}

    async def applications(
        self, *, minutes: int, subscriber_id: int | None = None, limit: int = 15
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT app,
                       sum(down_bytes)::bigint AS down_bytes,
                       sum(up_bytes)::bigint   AS up_bytes
                FROM flow_app_metrics
                WHERE ts >= now() - make_interval(mins => $1)
                  AND ($2::bigint IS NULL OR subscriber_id = $2)
                GROUP BY app
                ORDER BY (sum(down_bytes) + sum(up_bytes)) DESC
                LIMIT $3
                """,
                minutes,
                subscriber_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def subscriber_series(
        self,
        *,
        subscriber_id: int,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        vantage: str,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date_bin(make_interval(secs => $4), ts, $2) AS bucket,
                       sum(down_bytes)::bigint AS down_bytes,
                       sum(up_bytes)::bigint   AS up_bytes
                FROM flow_metrics
                WHERE subscriber_id = $1 AND vantage = $5
                  AND ts >= $2 AND ts < $3
                GROUP BY bucket
                ORDER BY bucket
                """,
                subscriber_id,
                start,
                end,
                bucket_seconds,
                vantage,
            )
        return [dict(row) for row in rows]

    async def usage(
        self,
        *,
        start: datetime,
        end: datetime,
        vantage: str,
        bucket: str | None = None,
        service_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Consommation par service, eventuellement decoupee par jour ou par mois.

        ``bucket`` a None rend UN total par service sur la fenetre : c'est la
        forme que lit un systeme de facturation qui veut un chiffre par periode
        de facturation, et c'est de loin la plus demandee.
        """
        intervalle = {"day": "1 day", "hour": "1 hour"}.get(bucket or "")
        if intervalle:
            colonne = "date_bin(INTERVAL '" + intervalle + "', f.ts, $1)"
        elif bucket == "month":
            # date_bin refuse un intervalle de mois (duree variable) ; date_trunc
            # le prend. C'est la seule raison de ce cas a part.
            colonne = "date_trunc('month', f.ts)"
        else:
            colonne = "NULL::timestamptz"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT s.login AS id,
                       c.account_ref AS account,
                       c.package_ref AS package,
                       {colonne} AS period_start,
                       sum(f.down_bytes)::bigint AS down_bytes,
                       sum(f.up_bytes)::bigint   AS up_bytes
                FROM flow_metrics f
                JOIN subscribers s ON s.id = f.subscriber_id
                LEFT JOIN static_clients c ON c.reference = s.login
                WHERE f.vantage = $3 AND f.ts >= $1 AND f.ts < $2
                  AND ($4::text IS NULL OR s.login = $4)
                GROUP BY s.login, c.account_ref, c.package_ref, period_start
                ORDER BY s.login, period_start
                """,  # noqa: S608 - 'colonne' vient d'une table blanche fermee
                start,
                end,
                vantage,
                service_id,
            )
        return [dict(row) for row in rows]

    async def hosts(
        self, *, vlan_id: int | None = None, max_age_s: float = 86_400.0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Hotes vus dans les flux et rattaches a aucune fiche.

        Le ``NOT EXISTS`` est relu a chaque appel plutot que grave a l'ecriture :
        declarer un client doit le faire disparaitre de cette liste TOUT DE
        SUITE, pas au prochain flush.

        ``<<=`` ET NON ``<<``. L'operateur strict exclut l'egalite : un client
        declare sur une adresse unique (``10.0.0.5``, donc un /32) ne serait
        jamais reconnu dans son propre /32, et resterait propose comme candidat
        pour toujours -- exactement ce que cette liste doit eviter.

        Les PREFIXES ADDITIONNELS comptent aussi. Un service pousse par l'API
        peut porter plusieurs blocs ; seul le premier vit dans ``address``, les
        autres dans ``extra_prefixes``. Les ignorer ferait reapparaitre comme
        "non declare" une adresse qui l'est parfaitement.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(h.address) AS address,
                       NULLIF(h.vlan_id, 0) AS vlan_id,
                       host(h.exporter) AS exporter,
                       h.pop_name, h.down_bytes, h.up_bytes, h.first_seen, h.last_seen
                FROM flow_hosts h
                WHERE h.last_seen >= now() - make_interval(secs => $1)
                  AND ($2::int IS NULL OR h.vlan_id = $2)
                  AND NOT EXISTS (
                        SELECT 1 FROM static_clients c
                         WHERE c.enabled AND h.address <<= c.address)
                  AND NOT EXISTS (
                        SELECT 1
                          FROM static_clients c,
                               jsonb_array_elements_text(c.extra_prefixes) AS p
                         WHERE c.enabled AND h.address <<= p::inet)
                  AND NOT EXISTS (
                        SELECT 1 FROM subscribers s
                         WHERE s.last_ip IS NOT NULL AND h.address <<= s.last_ip)
                ORDER BY (h.down_bytes + h.up_bytes) DESC
                LIMIT $3
                """,
                max_age_s,
                vlan_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def vlans(self, *, max_age_s: float = 86_400.0) -> list[dict[str, Any]]:
        """VLAN vues dans les flux, avec le nombre d'hotes et le volume."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT vlan_id,
                       count(*)                AS hosts,
                       sum(down_bytes)::bigint AS down_bytes,
                       sum(up_bytes)::bigint   AS up_bytes,
                       max(last_seen)          AS last_seen
                FROM flow_hosts
                WHERE vlan_id <> 0
                  AND last_seen >= now() - make_interval(secs => $1)
                GROUP BY vlan_id
                ORDER BY vlan_id
                """,
                max_age_s,
            )
        return [dict(row) for row in rows]

    async def prune_hosts(self, *, older_than_s: float) -> int:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM flow_hosts WHERE last_seen < now() - make_interval(secs => $1)",
                older_than_s,
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)


def _adresses_uniques(destinations: list[Any]) -> list[Any]:
    """Une ligne par ADRESSE, pas par couple (abonne, adresse).

    Dix abonnes qui regardent le meme serveur, c'est dix destinations et UNE
    adresse a enrichir. Envoyer dix fois la meme ligne ferait dix conflits a
    resoudre en base pour un seul nom a chercher.
    """
    vues: dict[str, Any] = {}
    for destination in destinations:
        vues.setdefault(destination.address, destination)
    return list(vues.values())


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            charge = json.loads(value)
        except ValueError:
            return []
        return charge if isinstance(charge, list) else []
    return []

"""Lecture de ce que les clients atteignent, et de ce qu'on en sait.

DEUX TABLES, DEUX DUREES DE VIE
-------------------------------
``flow_destinations`` est de la MESURE : qui a parle a quoi, combien. Elle
s'efface avec la retention, comme le reste du trafic.

``ip_intel`` est de la CONNAISSANCE : a qui appartient cette adresse. Elle ne
s'efface pas au meme rythme -- reapprendre a chaque purge que 45.57.12.34 est
Netflix serait une requete DNS pour rien, et un trou dans l'affichage pendant
qu'elle s'execute.

CE DEPOT NE DECIDE DE RIEN. Il rend des lignes. Le verdict (quel service, quelle
famille) est calcule dans ``services/ipfinder`` et ECRIT ici ; la base ne fait
que le conserver, ce qui permet de le recalculer entierement si le catalogue
change, sans perdre les noms inverses deja resolus.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

INTEL_COLUMNS = """
    host(address) AS address, hostname, service, category, source, org, asn,
    country, network, attempts, resolved_at, first_seen, last_seen
"""


class DestinationsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # --------------------------------------------------------------- lecture
    async def top(
        self,
        *,
        minutes: int = 60,
        subscriber_id: int | None = None,
        client: str | None = None,
        service: str | None = None,
        category: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Les adresses les plus atteintes sur la periode, deja nommees.

        UNE LIGNE PAR ADRESSE, pas par couple (abonne, adresse) : la question
        posee ici est "qu'est-ce qui est atteint sur mon reseau", et le nombre
        d'abonnes concernes en est la reponse la plus parlante. Le detail par
        abonne se demande adresse par adresse (``detail``).

        ``search`` porte sur l'adresse ET sur le nom inverse : un exploitant
        cherche aussi bien "45.57." que "nflxvideo".
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(d.address)             AS address,
                       i.hostname,
                       i.service,
                       i.category,
                       i.source,
                       i.org,
                       i.asn,
                       i.country,
                       i.resolved_at,
                       count(DISTINCT d.client)            AS clients,
                       count(DISTINCT d.subscriber_id)     AS subscribers,
                       sum(d.down_bytes)::bigint           AS down_bytes,
                       sum(d.up_bytes)::bigint             AS up_bytes,
                       sum(d.flows)::bigint                AS flows,
                       max(d.last_seen)                    AS last_seen,
                       min(d.first_seen)                   AS first_seen,
                       (array_agg(d.port ORDER BY d.last_seen DESC))[1]     AS port,
                       (array_agg(d.protocol ORDER BY d.last_seen DESC))[1] AS protocol,
                       (array_agg(d.app ORDER BY d.last_seen DESC))[1]      AS app
                FROM flow_destinations d
                LEFT JOIN ip_intel i ON i.address = d.address
                WHERE d.last_seen >= now() - make_interval(mins => $1)
                  AND ($2::bigint IS NULL OR d.subscriber_id = $2)
                  AND ($7::inet IS NULL OR d.client = $7)
                  AND ($3::text IS NULL OR i.service = $3)
                  AND ($4::text IS NULL OR i.category = $4)
                  AND ($5::text IS NULL
                       OR host(d.address) ILIKE '%' || $5 || '%'
                       OR coalesce(i.hostname, '') ILIKE '%' || $5 || '%'
                       OR coalesce(i.org, '') ILIKE '%' || $5 || '%')
                GROUP BY d.address, i.hostname, i.service, i.category, i.source,
                         i.org, i.asn, i.country, i.resolved_at
                ORDER BY (sum(d.down_bytes) + sum(d.up_bytes)) DESC
                LIMIT $6
                """,
                minutes,
                subscriber_id,
                service,
                category,
                search,
                limit,
                client,
            )
        return [dict(row) for row in rows]

    async def by_service(self, *, minutes: int = 60, limit: int = 30) -> list[dict[str, Any]]:
        """Volume par service reconnu. C'est la reponse a "qui fait du streaming".

        Les adresses non reconnues sont RENDUES elles aussi, sous un service nul
        plutot qu'ecartees. Une page qui ne montrerait que ce qu'elle sait
        nommer laisserait croire que tout est identifie, et la part reellement
        inconnue -- souvent la plus grosse -- disparaitrait de la discussion.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT i.service,
                       i.category,
                       count(DISTINCT d.address)       AS addresses,
                       count(DISTINCT d.client)        AS clients,
                       count(DISTINCT d.subscriber_id) AS subscribers,
                       sum(d.down_bytes)::bigint       AS down_bytes,
                       sum(d.up_bytes)::bigint         AS up_bytes,
                       max(d.last_seen)                AS last_seen
                FROM flow_destinations d
                LEFT JOIN ip_intel i ON i.address = d.address
                WHERE d.last_seen >= now() - make_interval(mins => $1)
                GROUP BY i.service, i.category
                ORDER BY (sum(d.down_bytes) + sum(d.up_bytes)) DESC
                LIMIT $2
                """,
                minutes,
                limit,
            )
        return [dict(row) for row in rows]

    async def detail(self, address: str, *, minutes: int = 1440) -> dict[str, Any]:
        """Tout ce qu'on sait d'UNE adresse, et qui l'a atteinte.

        C'est la fiche que l'exploitant ouvre avant de decider d'une
        restriction : le nom, l'organisation, l'AS, le pays, depuis quand elle
        est vue, et la liste nominative des abonnes qui lui parlent.
        """
        async with self._pool.acquire() as conn:
            intel = await conn.fetchrow(
                f"SELECT {INTEL_COLUMNS} FROM ip_intel WHERE address = $1::inet",  # noqa: S608
                address,
            )
            abonnes = await conn.fetch(
                """
                SELECT host(d.client) AS client,
                       d.subscriber_id,
                       s.login,
                       s.kind,
                       p.name AS pop_name,
                       s.plan_down_mbps,
                       d.port,
                       d.protocol,
                       d.app,
                       d.down_bytes,
                       d.up_bytes,
                       d.flows,
                       d.first_seen,
                       d.last_seen
                FROM flow_destinations d
                -- JOINTURE EXTERNE, et c'est tout l'interet : une machine sans
                -- fiche d'abonne doit apparaitre avec son adresse plutot que de
                -- disparaitre de la liste de ceux qui joignent cette adresse.
                LEFT JOIN subscribers s ON s.id = d.subscriber_id
                LEFT JOIN pops p        ON p.id = s.pop_id
                WHERE d.address = $1::inet
                  AND d.last_seen >= now() - make_interval(mins => $2)
                ORDER BY (d.down_bytes + d.up_bytes) DESC
                LIMIT 200
                """,
                address,
                minutes,
            )
            totaux = await conn.fetchrow(
                """
                SELECT coalesce(sum(down_bytes), 0)::bigint AS down_bytes,
                       coalesce(sum(up_bytes), 0)::bigint   AS up_bytes,
                       coalesce(sum(flows), 0)::bigint      AS flows,
                       count(DISTINCT client)               AS clients,
                       count(DISTINCT subscriber_id)        AS subscribers,
                       min(first_seen)                      AS first_seen,
                       max(last_seen)                       AS last_seen
                FROM flow_destinations
                WHERE address = $1::inet
                """,
                address,
            )
        return {
            "address": address,
            "intel": dict(intel) if intel is not None else None,
            "totals": dict(totaux) if totaux is not None else {},
            "clients": [dict(row) for row in abonnes],
        }

    async def get_intel(self, address: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {INTEL_COLUMNS} FROM ip_intel WHERE address = $1::inet",  # noqa: S608
                address,
            )
        return dict(row) if row is not None else None

    async def intel_for(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        """Ce qu'on sait de CES adresses-la, indexe par adresse.

        Une seule requete pour tout un tableau : la vue "en direct" affiche une
        centaine de lignes, et les enrichir une par une ferait une centaine
        d'allers-retours a la base a chaque rafraichissement.
        """
        if not addresses:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {INTEL_COLUMNS} FROM ip_intel "  # noqa: S608
                "WHERE address = ANY($1::inet[])",
                sorted(set(addresses)),
            )
        return {str(row["address"]): dict(row) for row in rows}

    async def pending(self, *, limit: int = 50, max_attempts: int = 3) -> list[str]:
        """Les adresses vues et pas encore nommees, les plus recentes d'abord.

        LES PLUS RECENTES, ET C'EST VOULU. Une adresse atteinte il y a dix
        secondes interesse l'exploitant qui regarde sa page ; une adresse vue
        hier et jamais resolue attendra un tour de plus sans que personne ne
        s'en apercoive.

        ``max_attempts`` protege du cas le plus courant : une adresse SANS nom
        inverse. La majorite d'internet n'en a pas, et la redemander a chaque
        passage ferait une requete DNS perpetuelle par adresse muette.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(address) AS address
                FROM ip_intel
                WHERE resolved_at IS NULL AND attempts < $2
                ORDER BY last_seen DESC
                LIMIT $1
                """,
                limit,
                max_attempts,
            )
        return [str(row["address"]) for row in rows]

    async def count_pending(self, *, max_attempts: int = 3) -> int:
        async with self._pool.acquire() as conn:
            valeur = await conn.fetchval(
                "SELECT count(*) FROM ip_intel WHERE resolved_at IS NULL AND attempts < $1",
                max_attempts,
            )
        return int(valeur or 0)

    # --------------------------------------------------------------- ecriture
    async def save_intel(self, verdicts: list[dict[str, Any]]) -> int:
        """Enregistre ce qui vient d'etre appris.

        ``attempts`` est incremente MEME quand on a trouve : il compte les
        tentatives, pas les echecs, et sert a reperer une adresse qui coute cher
        a resoudre. Ce qui distingue le succes de l'echec est ``resolved_at`` --
        pose des qu'on a une reponse, meme "cette adresse n'a pas de nom", parce
        que c'est une reponse aussi.
        """
        if not verdicts:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO ip_intel (address, hostname, service, category, source,
                                      org, asn, country, network, attempts, resolved_at)
                VALUES ($1::inet, $2, $3, $4, $5, $6, $7, $8, $9, 1, $10)
                ON CONFLICT (address) DO UPDATE
                   SET hostname    = COALESCE(EXCLUDED.hostname, ip_intel.hostname),
                       service     = COALESCE(EXCLUDED.service, ip_intel.service),
                       category    = COALESCE(EXCLUDED.category, ip_intel.category),
                       source      = EXCLUDED.source,
                       org         = COALESCE(EXCLUDED.org, ip_intel.org),
                       asn         = COALESCE(EXCLUDED.asn, ip_intel.asn),
                       country     = COALESCE(EXCLUDED.country, ip_intel.country),
                       network     = COALESCE(EXCLUDED.network, ip_intel.network),
                       attempts    = ip_intel.attempts + 1,
                       resolved_at = EXCLUDED.resolved_at,
                       last_seen   = now()
                """,
                [
                    (
                        v["address"],
                        v.get("hostname"),
                        v.get("service"),
                        v.get("category"),
                        str(v.get("source") or "inconnu"),
                        v.get("org"),
                        v.get("asn"),
                        v.get("country"),
                        v.get("network"),
                        v.get("resolved_at"),
                    )
                    for v in verdicts
                ],
            )
        return len(verdicts)

    async def forget_resolution(self, address: str) -> None:
        """Remet une adresse dans la file d'attente, tentatives remises a zero.

        Sert a la demande explicite "reanalyse cette adresse" : un service qui
        vient de changer de nom inverse, ou un catalogue mis a jour depuis.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ip_intel (address) VALUES ($1::inet)
                ON CONFLICT (address) DO UPDATE
                   SET resolved_at = NULL, attempts = 0, last_seen = now()
                """,
                address,
            )

    async def addresses_for(
        self, *, services: set[str], categories: set[str], limit: int = 5_000
    ) -> list[str]:
        """Les adresses DECOUVERTES qui relevent de ces services ou familles.

        C'est la moitie vivante d'une restriction : le catalogue fournit les
        blocs publies, cette requete fournit ce que NetFlow a trouve en plus --
        un cache Open Connect heberge chez vous, un serveur hors des blocs
        connus. Les deux se rejoignent dans la liste d'adresses du routeur.
        """
        if not services and not categories:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(address) AS address
                FROM ip_intel
                WHERE (service = ANY($1::text[]) OR category = ANY($2::text[]))
                ORDER BY last_seen DESC
                LIMIT $3
                """,
                sorted(services),
                sorted(categories),
                limit,
            )
        return [str(row["address"]) for row in rows]

    async def prune(self, *, older_than_s: float) -> int:
        """Oublie les destinations qui se sont tues.

        SEULE LA MESURE EST PURGEE. ``ip_intel`` reste : le jour ou cette
        adresse reapparait, elle est deja nommee, et l'exploitant ne voit pas un
        trou le temps qu'une resolution repasse.
        """
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM flow_destinations WHERE last_seen < now() - make_interval(secs => $1)",
                older_than_s,
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)

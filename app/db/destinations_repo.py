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
    country, city, region, latitude, longitude, network, attempts, resolved_at,
    first_seen, last_seen
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
                       i.city,
                       i.region,
                       i.latitude,
                       i.longitude,
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
                         i.org, i.asn, i.country, i.city, i.region, i.latitude,
                         i.longitude, i.resolved_at
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

    async def pairs(
        self,
        *,
        minutes: int = 60,
        app: str | None = None,
        client: str | None = None,
        service: str | None = None,
        category: str | None = None,
        pop: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """QUI PARLE A QUI : une ligne par couple (client, adresse atteinte).

        C'est la question que pose un exploitant devant une famille d'usage :
        "autre, 3 Kio -- mais AVEC QUI ?". Le tableau par usage ne peut pas y
        repondre, il agrege justement ce detail-la ; celui par adresse non plus,
        il fond tous les clients ensemble. Le couple est la seule forme qui
        montre la conversation.

        Le login est rendu quand il existe, l'adresse du client sinon : une
        machine non declaree parle autant que les autres.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(d.client)   AS client,
                       host(d.address)  AS address,
                       d.subscriber_id,
                       s.login,
                       s.kind,
                       p.name AS pop_name,
                       d.port,
                       d.protocol,
                       d.app,
                       i.hostname,
                       i.service,
                       i.category,
                       i.org,
                       i.country,
                       i.city,
                       i.latitude,
                       i.longitude,
                       d.down_bytes,
                       d.up_bytes,
                       d.flows,
                       d.first_seen,
                       d.last_seen
                FROM flow_destinations d
                LEFT JOIN ip_intel i    ON i.address = d.address
                LEFT JOIN subscribers s ON s.id = d.subscriber_id
                LEFT JOIN pops p        ON p.id = s.pop_id
                WHERE d.last_seen >= now() - make_interval(mins => $1)
                  AND ($2::text IS NULL OR d.app = $2)
                  AND ($3::inet IS NULL OR d.client = $3)
                  AND ($4::text IS NULL OR i.service = $4)
                  AND ($5::text IS NULL OR i.category = $5)
                  AND ($6::text IS NULL OR p.name = $6)
                  -- LA RECHERCHE PORTE SUR TOUT CE QUI IDENTIFIE UNE LIGNE :
                  -- l'adresse du client, son login, l'adresse jointe, son nom
                  -- inverse, son organisation. Un exploitant tape ce qu'il a
                  -- sous les yeux, pas le champ ou ca se trouve.
                  AND ($7::text IS NULL
                       OR host(d.client) ILIKE '%' || $7 || '%'
                       OR coalesce(s.login, '') ILIKE '%' || $7 || '%'
                       OR host(d.address) ILIKE '%' || $7 || '%'
                       OR coalesce(i.hostname, '') ILIKE '%' || $7 || '%'
                       OR coalesce(i.org, '') ILIKE '%' || $7 || '%'
                       OR coalesce(i.city, '') ILIKE '%' || $7 || '%'
                       OR coalesce(i.country, '') ILIKE '%' || $7 || '%'
                       OR coalesce(i.service, '') ILIKE '%' || $7 || '%')
                ORDER BY (d.down_bytes + d.up_bytes) DESC
                LIMIT $8
                """,
                minutes,
                app,
                client,
                service,
                category,
                pop,
                search,
                limit,
            )
        return [dict(row) for row in rows]

    async def pairs_facets(self, *, minutes: int = 60) -> dict[str, list[str]]:
        """Les valeurs REELLEMENT presentes sur la periode, pour les filtres.

        Proposer une liste figee -- tous les PoPs de l'inventaire, toutes les
        familles du catalogue -- ferait choisir des filtres qui ne rendent rien.
        On n'offre que ce qui existe dans les donnees affichees.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT p.name AS pop, i.category, i.service, d.app
                FROM flow_destinations d
                LEFT JOIN ip_intel i    ON i.address = d.address
                LEFT JOIN subscribers s ON s.id = d.subscriber_id
                LEFT JOIN pops p        ON p.id = s.pop_id
                WHERE d.last_seen >= now() - make_interval(mins => $1)
                """,
                minutes,
            )
        sortie: dict[str, set[str]] = {
            "pops": set(),
            "categories": set(),
            "services": set(),
            "apps": set(),
        }
        for row in rows:
            for cle, colonne in (
                ("pops", "pop"),
                ("categories", "category"),
                ("services", "service"),
                ("apps", "app"),
            ):
                valeur = row[colonne]
                if valeur:
                    sortie[cle].add(str(valeur))
        return {cle: sorted(valeurs) for cle, valeurs in sortie.items()}

    async def purge_infrastructure(
        self,
        *,
        customer_networks: list[str],
        infrastructure_networks: list[str],
        ports: list[int],
    ) -> int:
        """Efface les conversations qui n'en sont pas.

        LE FILTRE A L'ECRITURE NE SUFFIT PAS. Il empeche les nouvelles lignes,
        mais celles deja ecrites restent jusqu'a expiration de la retention --
        une semaine pendant laquelle la liste continue d'afficher le BFD entre
        routeurs et l'interrogation du parc. Changer le filtre doit nettoyer ce
        qu'il aurait refuse.

        Trois motifs, les memes qu'a l'ecriture : une destination dans l'espace
        client (deux machines du reseau qui se parlent), un bout dans
        l'infrastructure, ou un port du plan de gestion.
        """
        if not (customer_networks or infrastructure_networks or ports):
            return 0
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                """
                DELETE FROM flow_destinations
                 WHERE ($1::text[] IS NOT NULL
                        AND address <<= ANY($1::text[]::inet[]))
                    OR ($2::text[] IS NOT NULL
                        AND (address <<= ANY($2::text[]::inet[])
                             OR client <<= ANY($2::text[]::inet[])))
                    OR ($3::int[] IS NOT NULL AND port = ANY($3::int[]))
                """,
                customer_networks or None,
                infrastructure_networks or None,
                ports or None,
            )
        efface = int(resultat.rsplit(" ", 1)[-1] or 0)
        if efface:
            logger.info("%d conversation(s) d'exploitation effacee(s) de l'historique", efface)
        return efface

    async def by_location(
        self,
        *,
        minutes: int = 60,
        category: str | None = None,
        search: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        """OU VA LE TRAFIC : volume par lieu, et par pays.

        Les points sont regroupes au centieme de degre (environ un kilometre) :
        cent adresses d'un meme centre de donnees font UN point sur la carte,
        pas cent points superposes illisibles.

        Ce qui n'est pas localise est RENDU en total a part plutot qu'ecarte :
        une carte qui ne montrerait que ce qu'elle sait placer laisserait croire
        que tout le trafic y figure.
        """
        filtres = """
            d.last_seen >= now() - make_interval(mins => $1)
            AND ($2::text IS NULL OR i.category = $2)
            AND ($3::text IS NULL
                 OR host(d.address) ILIKE '%' || $3 || '%'
                 OR coalesce(i.hostname, '') ILIKE '%' || $3 || '%'
                 OR coalesce(i.org, '') ILIKE '%' || $3 || '%'
                 OR coalesce(i.city, '') ILIKE '%' || $3 || '%'
                 OR coalesce(i.country, '') ILIKE '%' || $3 || '%'
                 OR coalesce(i.service, '') ILIKE '%' || $3 || '%')
        """
        async with self._pool.acquire() as conn:
            points = await conn.fetch(
                f"""
                SELECT i.country,
                       i.city,
                       i.region,
                       round(i.latitude::numeric, 2)::float8  AS latitude,
                       round(i.longitude::numeric, 2)::float8 AS longitude,
                       count(DISTINCT d.address)              AS addresses,
                       count(DISTINCT d.client)               AS clients,
                       sum(d.down_bytes)::bigint              AS down_bytes,
                       sum(d.up_bytes)::bigint                AS up_bytes,
                       array_remove(array_agg(DISTINCT i.service), NULL) AS services,
                       array_remove(array_agg(DISTINCT i.org), NULL)     AS orgs,
                       (array_agg(host(d.address)
                                  ORDER BY d.down_bytes + d.up_bytes DESC))[1:40]
                                                              AS top_addresses
                FROM flow_destinations d
                JOIN ip_intel i ON i.address = d.address
                WHERE {filtres}
                  AND i.latitude IS NOT NULL AND i.longitude IS NOT NULL
                GROUP BY 1, 2, 3, 4, 5
                ORDER BY (sum(d.down_bytes) + sum(d.up_bytes)) DESC
                LIMIT $4
                """,  # noqa: S608 - filtres est une constante, pas une saisie
                minutes,
                category,
                search,
                limit,
            )
            pays = await conn.fetch(
                f"""
                SELECT i.country,
                       count(DISTINCT d.address)              AS addresses,
                       count(DISTINCT d.client)               AS clients,
                       count(DISTINCT i.city)                 AS cities,
                       sum(d.down_bytes)::bigint              AS down_bytes,
                       sum(d.up_bytes)::bigint                AS up_bytes
                FROM flow_destinations d
                LEFT JOIN ip_intel i ON i.address = d.address
                WHERE {filtres}
                GROUP BY i.country
                ORDER BY (sum(d.down_bytes) + sum(d.up_bytes)) DESC
                """,  # noqa: S608
                minutes,
                category,
                search,
            )
            hors_carte = await conn.fetchrow(
                f"""
                SELECT count(DISTINCT d.address)                    AS addresses,
                       coalesce(sum(d.down_bytes + d.up_bytes), 0)::bigint AS bytes
                FROM flow_destinations d
                LEFT JOIN ip_intel i ON i.address = d.address
                WHERE {filtres}
                  AND (i.latitude IS NULL OR i.longitude IS NULL)
                """,  # noqa: S608
                minutes,
                category,
                search,
            )
        sortie_points = []
        for row in points:
            point = dict(row)
            # Le tableau est trie par volume de CONVERSATION : une meme adresse
            # y revient autant de fois qu'elle a de clients. On garde l'ordre,
            # sans les doublons.
            vues: list[str] = []
            for adresse in point.pop("top_addresses") or []:
                if adresse not in vues:
                    vues.append(adresse)
            point["top_addresses"] = vues[:5]
            point["services"] = sorted(point["services"] or [])
            point["orgs"] = sorted(point["orgs"] or [])[:5]
            sortie_points.append(point)
        return {
            "points": sortie_points,
            "countries": [dict(row) for row in pays],
            "unlocated": dict(hors_carte) if hors_carte is not None else {},
        }

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

    async def pending_location(
        self, *, limit: int = 20, max_attempts: int = 5, retry_after_s: float = 3600.0
    ) -> list[str]:
        """Adresses deja analysees mais toujours SANS POSITION, a relocaliser.

        La localisation depend de services tiers qui limitent leur debit : une
        adresse analysee au mauvais moment restait sans position pour toujours,
        puisque la file principale ne redemande jamais une adresse resolue.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host(address) AS address
                FROM ip_intel
                WHERE resolved_at IS NOT NULL
                  AND latitude IS NULL
                  AND geo_attempts < $2
                  AND (geo_tried_at IS NULL
                       OR geo_tried_at < now() - make_interval(secs => $3))
                ORDER BY last_seen DESC
                LIMIT $1
                """,
                limit,
                max_attempts,
                float(retry_after_s),
            )
        return [str(row["address"]) for row in rows]

    async def save_location(self, rows: list[dict[str, Any]]) -> int:
        """Complete la position d'adresses deja analysees, sans rien ecraser."""
        if not rows:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                UPDATE ip_intel
                   SET country   = COALESCE($2, country),
                       city      = COALESCE($3, city),
                       region    = COALESCE($4, region),
                       latitude  = COALESCE($5, latitude),
                       longitude = COALESCE($6, longitude),
                       org       = COALESCE(org, $7),
                       asn       = COALESCE(asn, $8),
                       geo_attempts = geo_attempts + 1,
                       geo_tried_at = now()
                 WHERE address = $1::inet
                """,
                [
                    (
                        r["address"],
                        r.get("country"),
                        r.get("city"),
                        r.get("region"),
                        r.get("latitude"),
                        r.get("longitude"),
                        r.get("org"),
                        r.get("asn"),
                    )
                    for r in rows
                ],
            )
        return len(rows)

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
                                      org, asn, country, city, region, latitude,
                                      longitude, network, attempts, resolved_at)
                VALUES ($1::inet, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 1, $14)
                ON CONFLICT (address) DO UPDATE
                   SET hostname    = COALESCE(EXCLUDED.hostname, ip_intel.hostname),
                       service     = COALESCE(EXCLUDED.service, ip_intel.service),
                       category    = COALESCE(EXCLUDED.category, ip_intel.category),
                       source      = EXCLUDED.source,
                       org         = COALESCE(EXCLUDED.org, ip_intel.org),
                       asn         = COALESCE(EXCLUDED.asn, ip_intel.asn),
                       country     = COALESCE(EXCLUDED.country, ip_intel.country),
                       city        = COALESCE(EXCLUDED.city, ip_intel.city),
                       region      = COALESCE(EXCLUDED.region, ip_intel.region),
                       latitude    = COALESCE(EXCLUDED.latitude, ip_intel.latitude),
                       longitude   = COALESCE(EXCLUDED.longitude, ip_intel.longitude),
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
                        v.get("city"),
                        v.get("region"),
                        v.get("latitude"),
                        v.get("longitude"),
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

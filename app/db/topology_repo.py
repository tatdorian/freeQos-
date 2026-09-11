"""Persistance de la topologie et de la politique de shaping."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import asyncpg

from app.collectors.topology import TopologySnapshot

logger = logging.getLogger(__name__)


class TopologyRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------- ecriture
    async def save_snapshot(self, snapshot: TopologySnapshot) -> dict[str, int]:
        """Ecrit noeuds et liens en upsert.

        On ne supprime rien : un equipement momentanement invisible (redemarrage,
        fade) ne doit pas disparaitre du graphe. C'est ``last_seen`` qui dit ce
        qui est frais, et l'interface qui le signale.
        """
        if not snapshot.nodes and not snapshot.links:
            return {"nodes": 0, "links": 0}

        noeuds = [
            (
                n.key,
                n.name,
                n.kind,
                n.mac,
                n.address,
                n.platform,
                n.version,
                n.router_name,
                n.uisp_device_id,
                json.dumps(n.attributes),
            )
            for n in snapshot.nodes.values()
        ]
        liens = [
            (
                lk.key,
                lk.source_key,
                lk.target_key,
                lk.kind,
                lk.interface,
                lk.capacity_mbps,
                lk.discovered_by,
                json.dumps(lk.attributes),
            )
            for lk in snapshot.links.values()
        ]

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO topology_nodes
                       (key, name, kind, mac, address, platform, version,
                        router_name, uisp_device_id, attributes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    name           = EXCLUDED.name,
                    kind           = EXCLUDED.kind,
                    mac            = COALESCE(EXCLUDED.mac, topology_nodes.mac),
                    address        = COALESCE(EXCLUDED.address, topology_nodes.address),
                    platform       = COALESCE(EXCLUDED.platform, topology_nodes.platform),
                    version        = COALESCE(EXCLUDED.version, topology_nodes.version),
                    router_name    = COALESCE(EXCLUDED.router_name, topology_nodes.router_name),
                    uisp_device_id = COALESCE(EXCLUDED.uisp_device_id,
                                              topology_nodes.uisp_device_id),
                    attributes     = topology_nodes.attributes || EXCLUDED.attributes,
                    last_seen      = now()
                """,
                noeuds,
            )
            await conn.executemany(
                """
                INSERT INTO topology_links
                       (key, source_key, target_key, kind, interface,
                        capacity_mbps, discovered_by, attributes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    kind          = EXCLUDED.kind,
                    capacity_mbps = COALESCE(EXCLUDED.capacity_mbps,
                                             topology_links.capacity_mbps),
                    discovered_by = COALESCE(EXCLUDED.discovered_by,
                                             topology_links.discovered_by),
                    attributes    = topology_links.attributes || EXCLUDED.attributes,
                    last_seen     = now()
                """,
                liens,
            )
        return {"nodes": len(noeuds), "links": len(liens)}

    async def save_attachments(self, attachments: dict[int, tuple[str, str]]) -> int:
        """Rattachements abonne -> secteur radio (subscriber_id -> (secteur, mac))."""
        if not attachments:
            return 0
        lignes = [(sid, secteur, mac) for sid, (secteur, mac) in attachments.items()]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO subscriber_attachments (subscriber_id, sector_key, cpe_mac)
                VALUES ($1, $2, $3)
                ON CONFLICT (subscriber_id) DO UPDATE SET
                    sector_key = EXCLUDED.sector_key,
                    cpe_mac    = EXCLUDED.cpe_mac,
                    updated_at = now()
                """,
                lignes,
            )
        return len(lignes)

    # -------------------------------------------------------------- lecture
    async def nodes(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, name, COALESCE(kind_override, kind) AS kind, kind AS kind_detected,
                       kind_override, mac, address, platform, version, router_name,
                       uisp_device_id, attributes, first_seen, last_seen,
                       pos_x, pos_y, parent_override, hidden,
                       (last_seen > now() - INTERVAL '10 minutes') AS fresh
                  FROM topology_nodes
                 ORDER BY kind, name
                """
            )
        return [dict(row) for row in rows]

    async def links(self) -> list[dict[str, Any]]:
        """Liens du graphe, avec le DEBIT MESURE de leur port.

        La mesure vient de ``interface_latest``, donc de l'interface qui porte le
        lien. ``interface_links`` dit combien d'adjacences partagent ce port :
        a 1 le debit est bien celui du lien, au-dela c'est le debit cumule du
        port (un switch entre le routeur et plusieurs voisins). On expose le
        compte plutot que de laisser croire a une mesure par voisin.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT l.key, l.source_key, l.target_key, l.kind, l.interface,
                       l.capacity_mbps, l.discovered_by, l.attributes,
                       l.first_seen, l.last_seen,
                       (l.last_seen > now() - INTERVAL '10 minutes') AS fresh,
                       s.name AS source_name, t.name AS target_name,
                       COALESCE(t.kind_override, t.kind) AS target_kind,
                       p.max_down_mbps, p.max_up_mbps, p.enabled AS policy_enabled,
                       p.note AS policy_note,
                       im.rx_bps, im.tx_bps, im.running,
                       im.capacity_mbps AS port_capacity_mbps,
                       im.ts AS measured_at,
                       (im.ts > now() - INTERVAL '2 minutes') AS measure_fresh,
                       count(*) FILTER (WHERE l.interface IS NOT NULL)
                           OVER (PARTITION BY l.discovered_by, l.interface)
                           AS interface_links
                  FROM topology_links l
                  LEFT JOIN topology_nodes s ON s.key = l.source_key
                  LEFT JOIN topology_nodes t ON t.key = l.target_key
                  LEFT JOIN shaping_policies p
                         ON p.scope = 'link' AND p.target_key = l.key
                  LEFT JOIN interface_latest im
                         ON im.router_name = l.discovered_by AND im.interface = l.interface
                 WHERE NOT COALESCE(l.hidden, FALSE)
                 ORDER BY s.name NULLS LAST, l.interface
                """
            )
        return [dict(row) for row in rows]

    async def add_manual_link(self, source_key: str, target_key: str) -> str:
        """Cree (ou reaffiche) un lien pose a la main entre deux noeuds.

        La decouverte n'ecrase jamais ces liens : ils portent ``discovered_by
        = 'manual'`` et une cle prefixee ``manual:``. Aucune capacite : c'est une
        adjacence declaree par l'operateur, pas un port mesure.
        """
        if source_key == target_key:
            raise ValueError("un lien ne peut pas relier un noeud a lui-meme")
        key = f"manual:{source_key}|{target_key}"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO topology_links
                       (key, source_key, target_key, kind, discovered_by, hidden, attributes)
                VALUES ($1, $2, $3, 'manual', 'manual', FALSE, '{"manual": true}'::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    hidden = FALSE, last_seen = now()
                """,
                key,
                source_key,
                target_key,
            )
        return key

    async def hide_link(self, key: str) -> bool:
        """Ecarte un lien de l'affichage (adjacence erronee). Reversible."""
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_links SET hidden = TRUE WHERE key = $1", key
            )
        return not resultat.endswith(" 0")

    async def link(self, key: str) -> dict[str, Any] | None:
        """Un lien precis. Passe par ``links()`` : une seule definition de ce
        qu'est un lien enrichi, donc pas de divergence entre la liste et le
        detail."""
        for row in await self.links():
            if row["key"] == key:
                return row
        return None

    async def interface_series(
        self,
        *,
        router_name: str,
        interface: str,
        minutes: int = 60,
        bucket_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Historique de debit d'un port, agrege par pas de temps.

        ``date_bin`` plutot que ``time_bucket`` : le controleur doit tourner sur
        un PostgreSQL nu autant que sur TimescaleDB.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(rx_bps) AS rx_bps,
                       avg(tx_bps) AS tx_bps,
                       max(rx_bps) AS rx_peak_bps,
                       max(tx_bps) AS tx_peak_bps,
                       max(capacity_mbps) AS capacity_mbps
                  FROM interface_metrics
                 WHERE router_name = $1 AND interface = $2
                   AND ts > now() - $3::interval
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                router_name,
                interface,
                timedelta(minutes=minutes),
                timedelta(seconds=bucket_seconds),
            )
        return [dict(row) for row in rows]

    async def interface_latest(self) -> list[dict[str, Any]]:
        """Derniere mesure de chaque port, tous routeurs confondus."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT router_name, interface, ts, rx_bps, tx_bps,
                       running, capacity_mbps,
                       (ts > now() - INTERVAL '2 minutes') AS fresh
                  FROM interface_latest
                 ORDER BY router_name, interface
                """
            )
        return [dict(row) for row in rows]

    async def attachments(self) -> dict[str, str]:
        """login PPPoE -> cle du secteur radio, issu de la jointure caller-id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.pppoe_login, a.sector_key
                  FROM subscriber_attachments a
                  JOIN subscribers s ON s.id = a.subscriber_id
                 WHERE a.sector_key IS NOT NULL
                """
            )
        return {row["pppoe_login"]: row["sector_key"] for row in rows}

    async def set_node_kind(self, key: str, kind: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE topology_nodes SET kind_override = $2 WHERE key = $1", key, kind
            )

    async def set_node_position(self, key: str, x: float | None, y: float | None) -> bool:
        """Range une case a l'endroit ou l'operateur l'a laissee tomber.

        Purement cosmetique : deplacer une case ne touche aucun equipement.
        """
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET pos_x = $2, pos_y = $3 WHERE key = $1", key, x, y
            )
        return not resultat.endswith(" 0")

    async def set_node_parent(self, key: str, parent_key: str | None) -> bool:
        """Force le parent d'une case (glisser-deposer un lien).

        NULL retablit l'orientation automatique par role. On refuse qu'une case
        soit son propre parent : ce serait un cycle immediat. Les cycles plus
        longs sont evites cote lecture, en coupant la boucle a l'affichage.
        """
        if parent_key is not None and parent_key == key:
            raise ValueError("un equipement ne peut pas etre son propre parent")
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET parent_override = $2 WHERE key = $1", key, parent_key
            )
        return not resultat.endswith(" 0")

    async def set_node_hidden(self, key: str, hidden: bool) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET hidden = $2 WHERE key = $1", key, hidden
            )
        return not resultat.endswith(" 0")

    # ------------------------------------------------ fusions manuelles
    async def aliases(self) -> dict[str, str]:
        """Fusions declarees par l'operateur : alias_key -> canonical_key."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT alias_key, canonical_key FROM topology_aliases")
        return {row["alias_key"]: row["canonical_key"] for row in rows}

    async def merge_nodes(self, alias_key: str, canonical_key: str) -> None:
        """Declare que deux cases sont le meme equipement.

        On garde ``canonical_key`` et on replie ``alias_key`` dessus. Refuse une
        case sur elle-meme, et rechaine toute fusion qui pointait deja vers
        l'alias pour eviter une chaine alias -> alias -> canonique."""
        if alias_key == canonical_key:
            raise ValueError("un noeud ne peut pas etre fusionne avec lui-meme")
        async with self._pool.acquire() as conn, conn.transaction():
            # Si le canonique choisi etait lui-meme un alias, on remonte a SA cible.
            cible = await conn.fetchval(
                "SELECT canonical_key FROM topology_aliases WHERE alias_key = $1", canonical_key
            )
            canonical_key = cible or canonical_key
            if alias_key == canonical_key:
                raise ValueError("fusion circulaire refusee")
            await conn.execute(
                """
                INSERT INTO topology_aliases (alias_key, canonical_key)
                VALUES ($1, $2)
                ON CONFLICT (alias_key) DO UPDATE SET
                    canonical_key = EXCLUDED.canonical_key, updated_at = now()
                """,
                alias_key,
                canonical_key,
            )
            # Les cases repliees sur l'alias suivent desormais le meme canonique.
            await conn.execute(
                "UPDATE topology_aliases SET canonical_key = $2, updated_at = now() "
                "WHERE canonical_key = $1",
                alias_key,
                canonical_key,
            )

    async def unmerge_node(self, alias_key: str) -> bool:
        """Annule une fusion manuelle : la case redevient distincte."""
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM topology_aliases WHERE alias_key = $1", alias_key
            )
        return not resultat.endswith(" 0")

    # ------------------------------------------------------------ politique
    async def upsert_policy(
        self,
        *,
        scope: str,
        target_key: str,
        max_down_mbps: float | None,
        max_up_mbps: float | None,
        enabled: bool = True,
        note: str | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO shaping_policies
                       (scope, target_key, max_down_mbps, max_up_mbps, enabled, note, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope, target_key) DO UPDATE SET
                    max_down_mbps = EXCLUDED.max_down_mbps,
                    max_up_mbps   = EXCLUDED.max_up_mbps,
                    enabled       = EXCLUDED.enabled,
                    note          = EXCLUDED.note,
                    updated_by    = EXCLUDED.updated_by,
                    updated_at    = now()
                RETURNING *
                """,
                scope,
                target_key,
                max_down_mbps,
                max_up_mbps,
                enabled,
                note,
                updated_by,
            )
        return dict(row)

    async def delete_policy(self, scope: str, target_key: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM shaping_policies WHERE scope = $1 AND target_key = $2",
                scope,
                target_key,
            )
        return not resultat.endswith(" 0")

    async def policies(self, scope: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM shaping_policies
                 WHERE ($1::text IS NULL OR scope = $1)
                 ORDER BY scope, target_key
                """,
                scope,
            )
        return [dict(row) for row in rows]

    async def policy_map(self, scope: str) -> dict[str, dict[str, Any]]:
        return {row["target_key"]: row for row in await self.policies(scope)}

    # -------------------------------------------------------------- boost
    async def set_boost(
        self,
        *,
        scope: str,
        target_key: str,
        down_mbps: float | None,
        up_mbps: float | None,
        expires_at,
        reason: str | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Pose un boost temporaire, en creant la ligne de politique si besoin.

        Le boost n'ecrase pas la surcharge permanente : il vient par-dessus et
        s'efface a echeance, laissant l'abonne revenir a son plan.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO shaping_policies
                       (scope, target_key, boost_down_mbps, boost_up_mbps,
                        boost_expires_at, boost_reason, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope, target_key) DO UPDATE SET
                    boost_down_mbps  = EXCLUDED.boost_down_mbps,
                    boost_up_mbps    = EXCLUDED.boost_up_mbps,
                    boost_expires_at = EXCLUDED.boost_expires_at,
                    boost_reason     = EXCLUDED.boost_reason,
                    updated_by       = EXCLUDED.updated_by,
                    updated_at       = now()
                RETURNING *
                """,
                scope,
                target_key,
                down_mbps,
                up_mbps,
                expires_at,
                reason,
                updated_by,
            )
        return dict(row)

    async def clear_boost(self, scope: str, target_key: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                """
                UPDATE shaping_policies
                   SET boost_down_mbps = NULL, boost_up_mbps = NULL,
                       boost_expires_at = NULL, boost_reason = NULL, updated_at = now()
                 WHERE scope = $1 AND target_key = $2 AND boost_expires_at IS NOT NULL
                """,
                scope,
                target_key,
            )
        return not resultat.endswith(" 0")

    async def active_boosts(self, scope: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scope, target_key, boost_down_mbps, boost_up_mbps,
                       boost_expires_at, boost_reason,
                       EXTRACT(EPOCH FROM (boost_expires_at - now()))::double precision
                           AS seconds_left
                  FROM shaping_policies
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at > now()
                   AND ($1::text IS NULL OR scope = $1)
                 ORDER BY boost_expires_at
                """,
                scope,
            )
        return [dict(row) for row in rows]

    async def expired_boosts(self) -> list[dict[str, Any]]:
        """Boosts arrives a echeance mais dont les champs trainent encore.

        C'est ce que le job d'expiration doit nettoyer, et surtout : ce sont les
        abonnes dont la file doit etre ramenee a son debit normal.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scope, target_key, boost_expires_at
                  FROM shaping_policies
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at <= now()
                """
            )
        return [dict(row) for row in rows]

    async def purge_expired_boosts(self) -> int:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                """
                UPDATE shaping_policies
                   SET boost_down_mbps = NULL, boost_up_mbps = NULL,
                       boost_expires_at = NULL, boost_reason = NULL, updated_at = now()
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at <= now()
                """
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)

    # ------------------------------------------------------ drapeaux runtime
    async def get_flag(self, name: str) -> bool | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT value FROM runtime_flags WHERE name = $1", name)

    async def set_flag(
        self, name: str, value: bool, *, updated_by: str | None = None, reason: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_flags (name, value, updated_by, reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE SET
                    value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                    reason = EXCLUDED.reason, updated_at = now()
                """,
                name,
                value,
                updated_by,
                reason,
            )

    async def flag_detail(self, name: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM runtime_flags WHERE name = $1", name)
        return dict(row) if row else None

    # ---------------------------------------------------------------- audit
    async def record_audit(
        self, router_name: str, *, dry_run: bool, outcomes: list[tuple[Any, bool, str]]
    ) -> int:
        if not outcomes:
            return 0
        lignes = [
            (router_name, action.verb, action.path, action.command, dry_run, ok, detail[:2000])
            for action, ok, detail in outcomes
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO enforcement_audit
                       (router_name, verb, path, command, dry_run, ok, detail)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                lignes,
            )
        return len(lignes)

    async def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, router_name, verb, path, command, dry_run, ok, detail
                  FROM enforcement_audit
                 ORDER BY ts DESC
                 LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

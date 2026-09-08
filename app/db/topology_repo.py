"""Persistance de la topologie et de la politique de shaping."""

from __future__ import annotations

import json
import logging
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
                       (last_seen > now() - INTERVAL '10 minutes') AS fresh
                  FROM topology_nodes
                 ORDER BY kind, name
                """
            )
        return [dict(row) for row in rows]

    async def links(self) -> list[dict[str, Any]]:
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
                       p.note AS policy_note
                  FROM topology_links l
                  LEFT JOIN topology_nodes s ON s.key = l.source_key
                  LEFT JOIN topology_nodes t ON t.key = l.target_key
                  LEFT JOIN shaping_policies p
                         ON p.scope = 'link' AND p.target_key = l.key
                 ORDER BY s.name NULLS LAST, l.interface
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

"""Nature de chaque lien : filaire (capacite fixe) ou radio (capacite en direct).

Un reseau d'operateur radio melange les deux entre ses PoP et son coeur : une
fibre ou un cuivre porte toujours le meme debit, un backhaul radio varie avec
la meteo, l'alignement et le bruit. Le controleur ne doit pas DEVINER lequel
est lequel : l'exploitant le declare, lien par lien.

La cle est (routeur, interface) : c'est l'identite stable du port qui porte le
lien -- celle des mesures de debit -- et non une cle de topologie qui change
quand la decouverte renomme un voisin.

- ``wired`` : la capacite est un chiffre fixe (saisi), ou a defaut la vitesse
  negociee du port. Comme Preseem ou LibreQoS : rien a interroger.
- ``radio`` : la capacite est celle que l'antenne designee annonce EN DIRECT
  (airOS / UISP) ; quand elle chute, le controleur le dit.
"""

from __future__ import annotations

from typing import Any, Protocol

import asyncpg

MEDIUMS = ("wired", "radio")


class LinkMediaStore(Protocol):
    async def all(self) -> list[dict[str, Any]]: ...
    async def set(
        self,
        *,
        router_name: str,
        interface: str,
        medium: str,
        capacity_mbps: float | None,
        backhaul_name: str | None,
        updated_by: str | None,
    ) -> dict[str, Any]: ...
    async def delete(self, router_name: str, interface: str) -> bool: ...


class LinkMediaRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM link_media ORDER BY router_name, interface")
        return [dict(r) for r in rows]

    async def set(
        self,
        *,
        router_name: str,
        interface: str,
        medium: str,
        capacity_mbps: float | None,
        backhaul_name: str | None,
        updated_by: str | None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO link_media
                       (router_name, interface, medium, capacity_mbps, backhaul_name, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (router_name, interface) DO UPDATE SET
                    medium = EXCLUDED.medium,
                    capacity_mbps = EXCLUDED.capacity_mbps,
                    backhaul_name = EXCLUDED.backhaul_name,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                RETURNING *
                """,
                router_name,
                interface,
                medium,
                capacity_mbps,
                backhaul_name,
                updated_by,
            )
        return dict(row)

    async def delete(self, router_name: str, interface: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM link_media WHERE router_name = $1 AND interface = $2",
                router_name,
                interface,
            )
        return not resultat.endswith(" 0")


class InMemoryLinkMediaRepository:
    """Meme contrat, en memoire : tests et demonstration sans base."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def all(self) -> list[dict[str, Any]]:
        return [dict(v) for _k, v in sorted(self.rows.items())]

    async def set(
        self,
        *,
        router_name: str,
        interface: str,
        medium: str,
        capacity_mbps: float | None,
        backhaul_name: str | None,
        updated_by: str | None,
    ) -> dict[str, Any]:
        ligne = {
            "router_name": router_name,
            "interface": interface,
            "medium": medium,
            "capacity_mbps": capacity_mbps,
            "backhaul_name": backhaul_name,
            "updated_by": updated_by,
        }
        self.rows[(router_name, interface)] = ligne
        return dict(ligne)

    async def delete(self, router_name: str, interface: str) -> bool:
        return self.rows.pop((router_name, interface), None) is not None

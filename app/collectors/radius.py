"""Plans abonnes : RADIUS est la source de verite.

Abstraction ``PlanProvider`` avec deux implementations :
  - FreeradiusSqlPlanProvider : lit directement les tables FreeRADIUS ;
  - MockPlanProvider : catalogue simule, deterministe, pour le lab.

L'ecriture (CoA / Disconnect-Request pour changer un plan a chaud) n'est pas
implementee : seule l'interface ``CoaClient`` est posee, pour que la phase 2
n'ait pas a rouvrir ce module.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from typing import Protocol

from app.collectors.parsing import parse_bitrate, parse_mikrotik_rate_limit
from app.models import Plan

logger = logging.getLogger(__name__)


class PlanProvider(Protocol):
    async def get_plan(self, login: str) -> Plan | None: ...

    async def get_plans(self, logins: Sequence[str]) -> dict[str, Plan]: ...

    async def aclose(self) -> None: ...


class CoaClient(Protocol):
    """Changement de plan a chaud (phase 2+). Interface uniquement.

    Rien n'est implemente ici volontairement : modifier un plan a chaud est une
    action d'ecriture sur le reseau et sort du perimetre du collecteur.
    """

    async def change_rate_limit(self, login: str, plan: Plan) -> bool: ...

    async def disconnect(self, login: str) -> bool: ...


# ---------------------------------------------------------------------------
# Simulateur
# ---------------------------------------------------------------------------

DEFAULT_CATALOG: tuple[tuple[str, float, float], ...] = (
    ("Essentiel 30/10", 30.0, 10.0),
    ("Confort 100/20", 100.0, 20.0),
    ("Premium 300/50", 300.0, 50.0),
    ("Pro 500/100", 500.0, 100.0),
)


class MockPlanProvider:
    """Attribue un plan stable a chaque login, sans base RADIUS.

    Le plan derive d'un hachage du login : le meme abonne recoit toujours le meme
    plan d'une execution a l'autre, ce qui rend les tests et les demos
    reproductibles.
    """

    def __init__(
        self,
        catalog: Sequence[tuple[str, float, float]] = DEFAULT_CATALOG,
        *,
        overrides: dict[str, Plan] | None = None,
    ) -> None:
        if not catalog:
            raise ValueError("Le catalogue de plans ne peut pas etre vide")
        self._catalog = tuple(catalog)
        self._overrides: dict[str, Plan] = dict(overrides or {})

    def set_plan(self, login: str, plan: Plan) -> None:
        self._overrides[login] = plan

    async def get_plan(self, login: str) -> Plan | None:
        if login in self._overrides:
            return self._overrides[login]
        digest = hashlib.sha256(login.encode()).digest()
        name, down, up = self._catalog[digest[0] % len(self._catalog)]
        return Plan(down_mbps=down, up_mbps=up, source=f"mock:{name}")

    async def get_plans(self, logins: Sequence[str]) -> dict[str, Plan]:
        result: dict[str, Plan] = {}
        for login in logins:
            plan = await self.get_plan(login)
            if plan is not None:
                result[login] = plan
        return result

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# FreeRADIUS (SQL)
# ---------------------------------------------------------------------------


def plan_from_attributes(attributes: dict[str, str], *, source: str) -> Plan | None:
    """Construit un plan a partir des attributs RADIUS d'un abonne.

    Deux conventions couvrent la quasi-totalite des deploiements WISP :
      - ``Mikrotik-Rate-Limit`` : "up/down" cote routeur, ex. "10M/50M" ;
      - ``WISPr-Bandwidth-Max-Down`` / ``-Up`` : en bits par seconde.
    """
    rate_limit = attributes.get("Mikrotik-Rate-Limit")
    if rate_limit:
        parsed = parse_mikrotik_rate_limit(rate_limit)
        if parsed:
            down, up = parsed
            return Plan(down_mbps=down, up_mbps=up, source=source)

    down_bps = parse_bitrate(attributes.get("WISPr-Bandwidth-Max-Down"))
    up_bps = parse_bitrate(attributes.get("WISPr-Bandwidth-Max-Up"))
    if down_bps and up_bps:
        return Plan(
            down_mbps=down_bps / 1_000_000.0,
            up_mbps=up_bps / 1_000_000.0,
            source=source,
        )
    return None


class FreeradiusSqlPlanProvider:
    """Lecture des plans dans la base FreeRADIUS (PostgreSQL).

    Deux niveaux, dans l'ordre de priorite de FreeRADIUS lui-meme :
      1. ``radreply``      : attribut pose sur l'utilisateur ;
      2. ``radgroupreply`` : attribut pose sur son groupe (via ``radusergroup``).

    Note deploiement : beaucoup d'installations FreeRADIUS tournent sur MySQL.
    Le contrat ``PlanProvider`` etant deja pose, une variante aiomysql se
    limiterait a reimplementer ``get_plans``.
    """

    ATTRIBUTES = ("Mikrotik-Rate-Limit", "WISPr-Bandwidth-Max-Down", "WISPr-Bandwidth-Max-Up")

    def __init__(
        self,
        dsn: str,
        *,
        default_plan: Plan | None = None,
        pool: object | None = None,
    ) -> None:
        self._dsn = dsn
        self._pool = pool
        self._default_plan = default_plan

    async def _ensure_pool(self) -> object:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=4)
        return self._pool

    async def get_plans(self, logins: Sequence[str]) -> dict[str, Plan]:
        if not logins:
            return {}
        pool = await self._ensure_pool()
        logins = list(logins)

        async with pool.acquire() as conn:  # type: ignore[attr-defined]
            user_rows = await conn.fetch(
                """
                SELECT username, attribute, value
                  FROM radreply
                 WHERE username = ANY($1::text[]) AND attribute = ANY($2::text[])
                """,
                logins,
                list(self.ATTRIBUTES),
            )
            group_rows = await conn.fetch(
                """
                SELECT ug.username, gr.attribute, gr.value
                  FROM radusergroup ug
                  JOIN radgroupreply gr ON gr.groupname = ug.groupname
                 WHERE ug.username = ANY($1::text[]) AND gr.attribute = ANY($2::text[])
                 ORDER BY ug.priority ASC
                """,
                logins,
                list(self.ATTRIBUTES),
            )

        # Les attributs de groupe servent de socle, ceux de l'utilisateur priment.
        per_user: dict[str, dict[str, str]] = {login: {} for login in logins}
        sources: dict[str, str] = {}
        for row in group_rows:
            per_user.setdefault(row["username"], {}).setdefault(row["attribute"], row["value"])
            sources[row["username"]] = "radius:group"
        for row in user_rows:
            per_user.setdefault(row["username"], {})[row["attribute"]] = row["value"]
            sources[row["username"]] = "radius:user"

        result: dict[str, Plan] = {}
        for login, attributes in per_user.items():
            plan = plan_from_attributes(attributes, source=sources.get(login, "radius"))
            if plan is None and self._default_plan is not None:
                plan = self._default_plan
            if plan is not None:
                result[login] = plan
        return result

    async def get_plan(self, login: str) -> Plan | None:
        return (await self.get_plans([login])).get(login)

    async def aclose(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()  # type: ignore[misc]
            self._pool = None

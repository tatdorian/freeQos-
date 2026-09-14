"""Referentiel : resolution et creation a la volee des PoPs, abonnes et backhauls.

Le collecteur decouvre les abonnes par leur login PPPoE ; il faut donc convertir
un login en identifiant stable avant d'ecrire une metrique. Un cache memoire evite
un aller-retour SQL par abonne et par cycle (a 10 s et quelques milliers de
sessions, ce serait le goulot d'etranglement).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import asyncpg

from app.models import KIND_PPPOE, Plan

logger = logging.getLogger(__name__)


@runtime_checkable
class Directory(Protocol):
    """Contrat du referentiel, implemente par PostgreSQL et par un double memoire."""

    async def ensure_pop(self, name: str, router_host: str | None = None) -> int: ...

    async def ensure_subscriber(
        self,
        login: str,
        *,
        pop_id: int | None = None,
        plan: Plan | None = None,
        kind: str = KIND_PPPOE,
    ) -> int: ...

    async def ensure_backhaul(
        self,
        name: str,
        *,
        pop_id: int | None = None,
        uisp_device_id: str | None = None,
        nominal_capacity_mbps: float | None = None,
    ) -> int: ...

    async def touch_subscribers(self, seen: dict[int, tuple[str | None, object]]) -> None: ...

    async def update_plans(self, plans: dict[int, Plan]) -> int: ...

    async def list_subscriber_logins(self, *, kind: str | None = None) -> dict[str, int]: ...


class PgDirectory:
    """Implementation PostgreSQL du referentiel."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._pop_cache: dict[str, int] = {}
        self._subscriber_cache: dict[str, int] = {}
        # Ce que la ligne portait au dernier UPSERT : sert a n'ecrire que
        # lorsque quelque chose a reellement change.
        self._subscriber_state: dict[str, tuple[int | None, Plan | None, str]] = {}
        self._backhaul_cache: dict[str, int] = {}

    def clear_cache(self) -> None:
        self._pop_cache.clear()
        self._subscriber_cache.clear()
        self._backhaul_cache.clear()

    async def ensure_pop(self, name: str, router_host: str | None = None) -> int:
        cached = self._pop_cache.get(name)
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            pop_id = await conn.fetchval(
                """
                INSERT INTO pops (name, router_host)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE
                   SET router_host = COALESCE(EXCLUDED.router_host, pops.router_host),
                       updated_at  = now()
                RETURNING id
                """,
                name,
                router_host,
            )
        pop_id = int(pop_id)
        self._pop_cache[name] = pop_id
        return pop_id

    async def ensure_subscriber(
        self,
        login: str,
        *,
        pop_id: int | None = None,
        plan: Plan | None = None,
        kind: str = KIND_PPPOE,
    ) -> int:
        # Le cache evite un UPSERT par abonne et par cycle. Il ne peut servir
        # que si rien de ce que porte la ligne n'a change : pour un abonne PPPoE
        # le plan est rafraichi par un job dedie, mais pour un client statique
        # l'inventaire est la seule source, et une fiche modifiee doit descendre
        # en base des le cycle suivant.
        signature = (pop_id, plan, kind)
        cached = self._subscriber_cache.get(login)
        if cached is not None and self._subscriber_state.get(login) == signature:
            return cached
        async with self._pool.acquire() as conn:
            subscriber_id = await conn.fetchval(
                """
                INSERT INTO subscribers
                       (login, kind, pop_id, plan_down_mbps, plan_up_mbps, plan_source)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (login) DO UPDATE
                   SET kind           = EXCLUDED.kind,
                       pop_id         = COALESCE(EXCLUDED.pop_id, subscribers.pop_id),
                       -- Pour un client statique l'inventaire FAIT AUTORITE :
                       -- retirer un debit de sa fiche doit le retirer ici aussi,
                       -- la ou un plan PPPoE absent signifie juste "RADIUS n'a
                       -- rien dit ce cycle-ci" et ne doit rien ecraser.
                       plan_down_mbps = CASE WHEN EXCLUDED.kind = 'static'
                                             THEN EXCLUDED.plan_down_mbps
                                             ELSE COALESCE(EXCLUDED.plan_down_mbps,
                                                           subscribers.plan_down_mbps) END,
                       plan_up_mbps   = CASE WHEN EXCLUDED.kind = 'static'
                                             THEN EXCLUDED.plan_up_mbps
                                             ELSE COALESCE(EXCLUDED.plan_up_mbps,
                                                           subscribers.plan_up_mbps) END,
                       plan_source    = COALESCE(EXCLUDED.plan_source,
                                                 subscribers.plan_source),
                       updated_at     = now()
                RETURNING id
                """,
                login,
                kind,
                pop_id,
                plan.down_mbps if plan else None,
                plan.up_mbps if plan else None,
                plan.source if plan else None,
            )
        subscriber_id = int(subscriber_id)
        self._subscriber_cache[login] = subscriber_id
        self._subscriber_state[login] = signature
        return subscriber_id

    async def ensure_backhaul(
        self,
        name: str,
        *,
        pop_id: int | None = None,
        uisp_device_id: str | None = None,
        nominal_capacity_mbps: float | None = None,
    ) -> int:
        cache_key = f"{pop_id}:{name}"
        cached = self._backhaul_cache.get(cache_key)
        if cached is not None:
            return cached
        async with self._pool.acquire() as conn:
            backhaul_id = await conn.fetchval(
                """
                INSERT INTO backhauls (pop_id, name, uisp_device_id, nominal_capacity_mbps)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (pop_id, name) DO UPDATE
                   SET uisp_device_id        = COALESCE(EXCLUDED.uisp_device_id,
                                                        backhauls.uisp_device_id),
                       nominal_capacity_mbps = COALESCE(EXCLUDED.nominal_capacity_mbps,
                                                        backhauls.nominal_capacity_mbps),
                       updated_at            = now()
                RETURNING id
                """,
                pop_id,
                name,
                uisp_device_id,
                nominal_capacity_mbps,
            )
        backhaul_id = int(backhaul_id)
        self._backhaul_cache[cache_key] = backhaul_id
        return backhaul_id

    async def touch_subscribers(self, seen: dict[int, tuple[str | None, object]]) -> None:
        """Met a jour last_ip / last_seen pour les abonnes vus dans ce cycle."""
        if not seen:
            return
        rows = [(sid, ip, ts) for sid, (ip, ts) in seen.items()]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                UPDATE subscribers
                   SET last_ip   = COALESCE($2::inet, last_ip),
                       last_seen = $3,
                       updated_at = now()
                 WHERE id = $1
                """,
                rows,
            )

    async def update_plans(self, plans: dict[int, Plan]) -> int:
        if not plans:
            return 0
        rows = [(sid, p.down_mbps, p.up_mbps, p.source) for sid, p in plans.items()]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                UPDATE subscribers
                   SET plan_down_mbps = $2,
                       plan_up_mbps   = $3,
                       plan_source    = $4,
                       updated_at     = now()
                 WHERE id = $1
                """,
                rows,
            )
        return len(rows)

    async def list_subscriber_logins(self, *, kind: str | None = None) -> dict[str, int]:
        """Identite -> id. ``kind`` restreint a une nature d'abonne."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, login FROM subscribers WHERE ($1::text IS NULL OR kind = $1)",
                kind,
            )
        mapping = {r["login"]: r["id"] for r in rows}
        self._subscriber_cache.update(mapping)
        return mapping


class InMemoryDirectory:
    """Double memoire pour les tests et le mode 'dry-run' sans base."""

    def __init__(self) -> None:
        self.pops: dict[str, int] = {}
        self.subscribers: dict[str, int] = {}
        self.backhauls: dict[str, int] = {}
        self.plans: dict[int, Plan] = {}
        self.kinds: dict[int, str] = {}
        self.last_seen: dict[int, tuple[str | None, object]] = {}
        self._next_id = 1

    def _allocate(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    async def ensure_pop(self, name: str, router_host: str | None = None) -> int:
        return self.pops.setdefault(name, self._allocate())

    async def ensure_subscriber(
        self,
        login: str,
        *,
        pop_id: int | None = None,
        plan: Plan | None = None,
        kind: str = KIND_PPPOE,
    ) -> int:
        subscriber_id = self.subscribers.setdefault(login, self._allocate())
        self.kinds[subscriber_id] = kind
        if plan is not None:
            self.plans[subscriber_id] = plan
        return subscriber_id

    async def ensure_backhaul(
        self,
        name: str,
        *,
        pop_id: int | None = None,
        uisp_device_id: str | None = None,
        nominal_capacity_mbps: float | None = None,
    ) -> int:
        return self.backhauls.setdefault(f"{pop_id}:{name}", self._allocate())

    async def touch_subscribers(self, seen: dict[int, tuple[str | None, object]]) -> None:
        self.last_seen.update(seen)

    async def update_plans(self, plans: dict[int, Plan]) -> int:
        self.plans.update(plans)
        return len(plans)

    async def list_subscriber_logins(self, *, kind: str | None = None) -> dict[str, int]:
        if kind is None:
            return dict(self.subscribers)
        return {
            login: sid
            for login, sid in self.subscribers.items()
            if self.kinds.get(sid, KIND_PPPOE) == kind
        }

"""Orchestration de la phase 2 : decouverte, analyse et enforcement.

Trois responsabilites, volontairement separees :

  discover()   lit la topologie sur tous les PoPs et la persiste. Sans risque.
  inspect()    lit l'etat de shaping DEJA en place sur un routeur, sans rien
               modifier : c'est "analyser la connexion en cours".
  plan()       calcule ce qu'il faudrait faire. Ne touche a rien.
  apply()      execute un plan. Seul point qui ecrit, et seulement sur ordre.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.collectors.mikrotik import MikrotikCollector
from app.collectors.topology import (
    KIND_SECTOR,
    TopologySnapshot,
    attach_uisp_devices,
    build_from_router,
    map_subscribers_to_sectors,
    normalize_mac,
    router_node_key,
)
from app.config import Settings
from app.db.topology_repo import TopologyRepository
from app.enforcement.models import Plan
from app.enforcement.planner import (
    LinkTarget,
    SubscriberTarget,
    build_plan,
    desired_queue_types,
    desired_state,
)
from app.enforcement.routeros import (
    ApplyResult,
    LibrouterosWriteClient,
    MissingWriteCredentialsError,
    RouterOsWriteClient,
    apply_plan,
)
from app.services.registry import RouterRegistry

logger = logging.getLogger(__name__)


class EnforcementDisabledError(RuntimeError):
    """L'enforcement est desactive globalement."""


@dataclass
class RouterShapingState:
    """Ce qui est reellement configure sur un routeur, lu tel quel."""

    router_name: str
    reachable: bool = True
    error: str | None = None
    simple_queues: list[dict[str, Any]] = field(default_factory=list)
    queue_types: list[dict[str, Any]] = field(default_factory=list)
    queue_trees: list[dict[str, Any]] = field(default_factory=list)

    @property
    def managed_queues(self) -> list[dict[str, Any]]:
        from app.enforcement.models import MANAGED_COMMENT

        return [q for q in self.simple_queues if MANAGED_COMMENT in str(q.get("comment") or "")]

    @property
    def foreign_queues(self) -> list[dict[str, Any]]:
        from app.enforcement.models import MANAGED_COMMENT

        return [q for q in self.simple_queues if MANAGED_COMMENT not in str(q.get("comment") or "")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "router": self.router_name,
            "reachable": self.reachable,
            "error": self.error,
            "counts": {
                "simple_queues": len(self.simple_queues),
                "managed": len(self.managed_queues),
                "foreign": len(self.foreign_queues),
                "queue_types": len(self.queue_types),
                "queue_trees": len(self.queue_trees),
            },
            "managed_queues": self.managed_queues,
            "foreign_queues": self.foreign_queues,
            "queue_types": self.queue_types,
            "queue_trees": self.queue_trees,
        }


class ShapingService:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: RouterRegistry,
        repository: TopologyRepository | None = None,
        write_client_factory=None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.repository = repository
        self._write_clients: dict[str, RouterOsWriteClient] = {}
        self._write_client_factory = write_client_factory or LibrouterosWriteClient
        self.last_snapshot: TopologySnapshot | None = None

    # ------------------------------------------------------------ decouverte
    async def discover(self, uisp_devices: list[dict[str, Any]] | None = None) -> TopologySnapshot:
        """Interroge tous les PoPs et construit le graphe.

        Lecture seule et parallelisee : un PoP injoignable retire sa branche du
        resultat mais n'empeche pas les autres.
        """
        snapshot = TopologySnapshot()
        collectors = self.registry.collectors

        resultats = await asyncio.gather(
            *(self._read_router_topology(c) for c in collectors), return_exceptions=True
        )
        for collector, resultat in zip(collectors, resultats, strict=True):
            if isinstance(resultat, BaseException):
                message = f"{collector.name}: {type(resultat).__name__}: {resultat}"
                snapshot.warnings.append(message)
                logger.warning("Topologie non lue sur %s : %s", collector.name, resultat)
                continue
            build_from_router(
                snapshot,
                router_name=collector.config.name,
                pop_name=collector.config.effective_pop_name,
                host=collector.config.host,
                **resultat,
            )

        if uisp_devices:
            attach_uisp_devices(snapshot, uisp_devices)

        self.last_snapshot = snapshot
        if self.repository is not None:
            compte = await self.repository.save_snapshot(snapshot)
            logger.info("Topologie : %d noeud(s), %d lien(s)", compte["nodes"], compte["links"])
        return snapshot

    @staticmethod
    async def _read_router_topology(collector: MikrotikCollector) -> dict[str, Any]:
        client = collector._client  # noqa: SLF001 - lecture interne assumee
        timeout = max(collector.config.timeout_s * 4, 10.0)

        def lire() -> dict[str, Any]:
            return {
                "neighbors": client.neighbors(),
                "interfaces": client.interfaces(),
                "ethernet": client.ethernet(),
                "addresses": client.addresses(),
            }

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    async def map_sectors(
        self, sessions: list[dict[str, Any]], uisp_devices: list[dict[str, Any]]
    ) -> dict[str, str]:
        """Rattache les abonnes a leur secteur via la MAC du CPE.

        C'est la jointure qui donne la vraie chaine de goulots : sans elle, on
        sait qu'un abonne est sur un PoP mais pas par quelle antenne il passe.
        """
        if self.last_snapshot is None:
            return {}
        stations: dict[str, str] = {}
        for device in uisp_devices:
            identification = device.get("identification") or {}
            mac = normalize_mac(identification.get("mac"))
            if not mac:
                continue
            parent = (device.get("attributes") or {}).get("apDevice") or {}
            parent_id = parent.get("id")
            if parent_id:
                stations[mac] = f"uisp:{parent_id}"
        map_subscribers_to_sectors(self.last_snapshot, sessions, stations)
        return dict(self.last_snapshot.subscriber_sectors)

    # -------------------------------------------------------------- analyse
    async def inspect(self, router_name: str | None = None) -> list[RouterShapingState]:
        """Lit le shaping deja en place, sans rien modifier.

        A faire AVANT tout enforcement : on doit savoir ce que l'operateur ou
        RADIUS ont deja pose, pour ne pas entrer en collision avec.
        """
        collectors = [c for c in self.registry.collectors if router_name in (None, c.name)]
        etats = await asyncio.gather(
            *(self._inspect_one(c) for c in collectors), return_exceptions=True
        )
        resultat: list[RouterShapingState] = []
        for collector, etat in zip(collectors, etats, strict=True):
            if isinstance(etat, BaseException):
                resultat.append(
                    RouterShapingState(
                        router_name=collector.name,
                        reachable=False,
                        error=f"{type(etat).__name__}: {etat}",
                    )
                )
            else:
                resultat.append(etat)
        return resultat

    @staticmethod
    async def _inspect_one(collector: MikrotikCollector) -> RouterShapingState:
        client = collector._client  # noqa: SLF001
        timeout = max(collector.config.timeout_s * 4, 10.0)

        def lire() -> RouterShapingState:
            return RouterShapingState(
                router_name=collector.name,
                simple_queues=client.simple_queues(),
                queue_types=client.queue_types(),
                queue_trees=client.queue_trees(),
            )

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    # ----------------------------------------------------------------- plan
    async def plan(
        self,
        router_name: str,
        *,
        links: list[LinkTarget],
        subscribers: list[SubscriberTarget],
    ) -> Plan:
        """Calcule ce qu'il faudrait faire. N'ecrit rien."""
        collector = self._collector(router_name)
        etat = await self._inspect_one(collector)

        types, files = desired_state(
            links=links,
            subscribers=subscribers,
            safety_factor=self.settings.shaping_safety_factor,
            floor_mbps=self.settings.shaping_floor_mbps,
            queue_types=desired_queue_types(
                overhead=self.settings.cake_overhead,
                rtt_ms=self.settings.cake_rtt_ms,
            ),
        )
        return build_plan(
            router_name,
            desired_types=types,
            desired_queues=files,
            actual_types=etat.queue_types,
            actual_queues=etat.simple_queues,
            prune=self.settings.shaping_prune,
        )

    # ---------------------------------------------------------------- apply
    async def apply(self, plan: Plan, *, dry_run: bool = True) -> ApplyResult:
        """Execute un plan.

        Deux verrous : le drapeau global ``ENFORCEMENT_ENABLED``, et le fait que
        ``dry_run`` vaut vrai par defaut. Les deux doivent etre leves.
        """
        if not dry_run and not self.settings.enforcement_enabled:
            raise EnforcementDisabledError(
                "ENFORCEMENT_ENABLED=false : le controleur reste en lecture seule. "
                "Passez-le a true pour autoriser l'ecriture sur les routeurs."
            )

        client: RouterOsWriteClient | None = None
        if not dry_run:
            client = self._write_client(plan.router_name)

        resultat = await apply_plan(
            plan,
            client,  # type: ignore[arg-type]
            dry_run=dry_run,
            max_actions=self.settings.enforcement_max_actions,
        )

        if self.repository is not None:
            await self.repository.record_audit(
                plan.router_name,
                dry_run=dry_run,
                outcomes=[(o.action, o.ok, o.detail) for o in resultat.outcomes],
            )
        return resultat

    # ------------------------------------------------------------ interne
    def _collector(self, router_name: str) -> MikrotikCollector:
        for collector in self.registry.collectors:
            if collector.name == router_name:
                return collector
        raise KeyError(f"routeur '{router_name}' absent de l'inventaire actif")

    def _write_client(self, router_name: str) -> RouterOsWriteClient:
        existant = self._write_clients.get(router_name)
        if existant is not None:
            return existant
        config = self._collector(router_name).config
        try:
            client = self._write_client_factory(config)
        except MissingWriteCredentialsError:
            raise
        self._write_clients[router_name] = client
        return client

    def close(self) -> None:
        for client in self._write_clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._write_clients.clear()


def sector_key_for(snapshot: TopologySnapshot, login: str) -> str | None:
    """Secteur radio d'un abonne, si la jointure a abouti."""
    return snapshot.subscriber_sectors.get(login)


def sectors(snapshot: TopologySnapshot) -> list[str]:
    return [key for key, node in snapshot.nodes.items() if node.kind == KIND_SECTOR]


__all__ = [
    "EnforcementDisabledError",
    "RouterShapingState",
    "ShapingService",
    "router_node_key",
    "sector_key_for",
    "sectors",
]

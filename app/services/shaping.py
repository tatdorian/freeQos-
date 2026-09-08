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
from app.enforcement.capability import WriteCapability, inspect_write_capability
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
    RouterOsWriteClient,
    apply_plan,
)
from app.services.registry import RouterRegistry

logger = logging.getLogger(__name__)


FLAG_ENFORCEMENT = "enforcement_enabled"


class EnforcementDisabledError(RuntimeError):
    """L'enforcement est desactive globalement."""


class EnforcementLockedError(RuntimeError):
    """La bascule a chaud est interdite par la configuration."""


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
        metrics: Any = None,
        write_client_factory=None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.repository = repository
        # Depot de metriques : donne les abonnes actifs et leur plan. Optionnel
        # pour que le service reste testable sans base.
        self.metrics = metrics
        self._write_clients: dict[str, RouterOsWriteClient] = {}
        self._write_client_factory = write_client_factory or LibrouterosWriteClient
        self.last_snapshot: TopologySnapshot | None = None
        # Etat courant du drapeau. La base fait foi une fois amorcee ; la
        # variable d'environnement ne sert plus qu'a la valeur initiale.
        self._enforcement_enabled = settings.enforcement_enabled

    # ------------------------------------------------------------- drapeau
    @property
    def enforcement_enabled(self) -> bool:
        return self._enforcement_enabled

    @property
    def enforcement_locked(self) -> bool:
        return self.settings.enforcement_locked

    async def load_flags(self) -> None:
        """Amorce le drapeau depuis la base, ou l'y ecrit au premier demarrage."""
        if self.repository is None:
            return
        try:
            stocke = await self.repository.get_flag(FLAG_ENFORCEMENT)
        except Exception:  # noqa: BLE001 - table pas encore creee
            return
        if stocke is None:
            await self.repository.set_flag(
                FLAG_ENFORCEMENT,
                self.settings.enforcement_enabled,
                updated_by="bootstrap",
                reason="valeur initiale issue de ENFORCEMENT_ENABLED",
            )
            return
        self._enforcement_enabled = stocke
        if stocke != self.settings.enforcement_enabled:
            logger.warning(
                "Enforcement %s d'apres la base (ENFORCEMENT_ENABLED vaut %s dans "
                "l'environnement : c'est la base qui fait foi une fois amorcee).",
                "ACTIF" if stocke else "desactive",
                self.settings.enforcement_enabled,
            )

    async def set_enforcement(
        self, enabled: bool, *, reason: str | None = None, actor: str = "ui"
    ) -> dict[str, Any]:
        """Bascule l'enforcement sans redemarrage.

        Refusee si ENFORCEMENT_LOCKED est vrai : un exploitant qui tient a la
        friction du redemarrage doit pouvoir la garder.
        """
        if self.settings.enforcement_locked:
            raise EnforcementLockedError(
                "ENFORCEMENT_LOCKED=true : la bascule depuis l'interface est "
                "interdite. Modifiez ENFORCEMENT_ENABLED puis redemarrez."
            )
        self._enforcement_enabled = enabled
        if self.repository is not None:
            await self.repository.set_flag(
                FLAG_ENFORCEMENT, enabled, updated_by=actor, reason=reason
            )
        logger.warning(
            "Enforcement %s par %s%s",
            "ACTIVE" if enabled else "desactive",
            actor,
            f" ({reason})" if reason else "",
        )
        return {"enforcement_enabled": enabled, "locked": False, "reason": reason}

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

    # ------------------------------------------------------- etat desire
    async def build_targets(
        self, router_name: str
    ) -> tuple[list[LinkTarget], list[SubscriberTarget]]:
        """Assemble l'etat desire d'un routeur : liens et abonnes.

        Vit ici plutot que dans l'API : le job d'expiration des boosts en a
        besoin autant que l'operateur qui demande un plan.
        """
        if self.repository is None or self.metrics is None:
            return [], []

        surcharges_liens = await self.repository.policy_map("link")
        surcharges_abonnes = await self.repository.policy_map("subscriber")
        rattachements = await self.repository.attachments()

        collector = self._collector(router_name)
        pop_name = collector.config.effective_pop_name

        liens: list[LinkTarget] = []
        # cle du noeud d'en face -> file parent, pour rattacher chaque abonne au
        # lien qu'il traverse REELLEMENT et non a un lien pris au hasard.
        parent_par_noeud: dict[str, str] = {}
        for lien in await self.repository.links():
            if lien.get("discovered_by") != router_name or not lien.get("interface"):
                continue
            surcharge = surcharges_liens.get(lien["key"], {})
            if surcharge and not surcharge.get("enabled", True):
                continue
            cible = LinkTarget(
                name=str(lien.get("target_name") or lien["interface"]),
                interface=str(lien["interface"]),
                measured_capacity_mbps=lien.get("capacity_mbps"),
                override_down_mbps=surcharge.get("max_down_mbps"),
                override_up_mbps=surcharge.get("max_up_mbps"),
            )
            liens.append(cible)
            if lien.get("target_key"):
                parent_par_noeud[str(lien["target_key"])] = cible.queue_name

        # La capacite radio mesuree prime sur le debit negocie du port : c'est
        # elle le vrai goulot d'un backhaul sans fil.
        for backhaul in await self.metrics.backhaul_latest():
            if backhaul.get("pop_name") != pop_name or not backhaul.get("capacity_mbps"):
                continue
            for lien in liens:
                if lien.name == backhaul["name"]:
                    lien.measured_capacity_mbps = backhaul["capacity_mbps"]

        abonnes: list[SubscriberTarget] = []
        for ligne in await self.metrics.subscriber_latest(limit=5000, order_by="login"):
            if ligne.get("pop_name") != pop_name:
                continue
            login = str(ligne["pppoe_login"])
            surcharge = surcharges_abonnes.get(login, {})
            secteur = rattachements.get(login)
            abonnes.append(
                SubscriberTarget(
                    login=login,
                    interface=collector.config.pppoe_interface_pattern.format(
                        login=login, name=login, user=login
                    ),
                    plan_down_mbps=ligne.get("plan_down_mbps"),
                    plan_up_mbps=ligne.get("plan_up_mbps"),
                    override_down_mbps=surcharge.get("max_down_mbps"),
                    override_up_mbps=surcharge.get("max_up_mbps"),
                    boost_down_mbps=surcharge.get("boost_down_mbps"),
                    boost_up_mbps=surcharge.get("boost_up_mbps"),
                    boost_expires_at=surcharge.get("boost_expires_at"),
                    enabled=surcharge.get("enabled", True),
                    parent=parent_par_noeud.get(secteur) if secteur else None,
                )
            )

        return liens, abonnes

    async def plan_router(self, router_name: str) -> Plan:
        """Raccourci : assemble l'etat desire puis compare au routeur."""
        liens, abonnes = await self.build_targets(router_name)
        return await self.plan(router_name, links=liens, subscribers=abonnes)

    # ------------------------------------------------------------- boosts
    async def expire_boosts(self) -> dict[str, Any]:
        """Retire les boosts arrives a echeance et ramene les files concernees.

        Sans cette etape, un boost resterait en place indefiniment : la file
        RouterOS ne sait rien de l'echeance, c'est le controleur qui doit la
        faire respecter.
        """
        resultat: dict[str, Any] = {"expired": 0, "routers": [], "applied": 0, "errors": []}
        if self.repository is None:
            return resultat

        echus = await self.repository.expired_boosts()
        if not echus:
            return resultat

        logins = {row["target_key"] for row in echus if row["scope"] == "subscriber"}
        resultat["expired"] = await self.repository.purge_expired_boosts()
        logger.info("%d boost(s) arrive(s) a echeance", resultat["expired"])

        if not self._enforcement_enabled:
            resultat["errors"].append(
                "enforcement desactive : les files gardent leur debit boosté "
                "jusqu'a la prochaine application"
            )
            return resultat

        for router_name in await self._routers_for_logins(logins):
            try:
                plan = await self.plan_router(router_name)
                if plan.is_empty:
                    continue
                applique = await self.apply(plan, dry_run=False)
                resultat["routers"].append(router_name)
                resultat["applied"] += applique.applied
            except Exception as exc:  # noqa: BLE001
                resultat["errors"].append(f"{router_name}: {type(exc).__name__}: {exc}")
                logger.exception("Retrait de boost impossible sur %s", router_name)
        return resultat

    async def _routers_for_logins(self, logins: set[str]) -> list[str]:
        """Quels routeurs portent ces abonnes. Evite de replanifier tout le parc."""
        if not logins or self.metrics is None:
            return []
        pops = set()
        for ligne in await self.metrics.subscriber_latest(limit=5000, order_by="login"):
            if ligne["pppoe_login"] in logins and ligne.get("pop_name"):
                pops.add(ligne["pop_name"])
        return [c.name for c in self.registry.collectors if c.config.effective_pop_name in pops]

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
        if not dry_run and not self._enforcement_enabled:
            raise EnforcementDisabledError(
                "Le controleur est en lecture seule. Activez l'enforcement depuis "
                "l'onglet Shaping, ou passez ENFORCEMENT_ENABLED a true."
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
            client = self._write_client_factory(
                config, require_separate=self.settings.require_separate_write_account
            )
        except TypeError:
            # Fabrique de test qui n'accepte pas l'option.
            client = self._write_client_factory(config)
        self._write_clients[router_name] = client
        return client

    async def write_capability(self, router_name: str) -> WriteCapability:
        """Interroge le routeur sur les droits reels du compte utilise.

        On lit /user et /user/group plutot que de se fier a l'inventaire. En cas
        d'impossibilite de lecture, le verdict reste indetermine : on tentera la
        commande et RouterOS aura le dernier mot.
        """
        collector = self._collector(router_name)
        config = collector.config
        utilisateur = config.rw_username or config.username
        client = collector._client  # noqa: SLF001

        def lire() -> tuple[list, list]:
            return client.users(), client.user_groups()

        try:
            comptes, groupes = await asyncio.wait_for(
                asyncio.to_thread(lire), timeout=max(config.timeout_s * 3, 8.0)
            )
        except Exception as exc:  # noqa: BLE001
            return WriteCapability(
                username=utilisateur,
                detail=(
                    f"droits non verifiables ({type(exc).__name__}) : la commande "
                    "sera tentee et RouterOS tranchera"
                ),
            )
        return inspect_write_capability(utilisateur, comptes, groupes)

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
    "FLAG_ENFORCEMENT",
    "EnforcementDisabledError",
    "EnforcementLockedError",
    "RouterShapingState",
    "ShapingService",
    "router_node_key",
    "sector_key_for",
    "sectors",
]

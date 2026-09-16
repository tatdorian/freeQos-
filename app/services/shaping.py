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
import ipaddress
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.collectors.config_graph import (
    InterfacePath,
    best_upstream,
    interface_stacks,
    routing_peers,
)
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.topology import (
    KIND_SECTOR,
    TopologyNode,
    TopologySnapshot,
    attach_static_clients,
    attach_uisp_devices,
    build_from_router,
    kind_for_role,
    link_by_routing_adjacency,
    link_by_shared_subnets,
    link_by_tunnels,
    map_subscribers_to_sectors,
    normalize_mac,
    orient_from_config,
    parse_export,
    pick_loopback,
    resolve_to_managed,
    router_node_key,
)
from app.config import Settings
from app.db.topology_repo import TopologyRepository
from app.enforcement.capability import WriteCapability, inspect_write_capability
from app.enforcement.models import Plan, network_target
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
from app.models import KIND_STATIC, StaticClient
from app.services.qoe_loop import ACTION_UNKNOWN, SectorState, decide_sector
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


async def discover_with_devices(
    shaping: ShapingService, providers: Sequence[Any]
) -> TopologySnapshot:
    """Decouverte complete : radios rassemblees, puis lecture des PoPs.

    UN SEUL CHEMIN, partage par le job periodique et le bouton "Relancer la
    decouverte". Les dupliquer aurait garanti qu'ils divergent -- et un arbre
    qui change selon qu'il a ete construit par le planificateur ou par un clic
    serait impossible a diagnostiquer.

    Une source de radios muette n'empeche pas l'autre, ni la lecture des PoPs :
    la topologie des routeurs ne depend pas d'UISP.
    """
    devices: list[dict[str, Any]] = []
    for fournisseur in providers:
        if fournisseur is None or not hasattr(fournisseur, "raw_devices"):
            continue
        try:
            devices.extend(await fournisseur.raw_devices())
        except Exception:  # noqa: BLE001
            logger.warning("raw_devices indisponible pour %s", type(fournisseur).__name__)
    return await shaping.discover(uisp_devices=devices)


class ShapingService:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: RouterRegistry,
        repository: TopologyRepository | None = None,
        metrics: Any = None,
        write_client_factory: Callable[..., RouterOsWriteClient] | None = None,
        static_clients: Any = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.repository = repository
        # Depot de metriques : donne les abonnes actifs et leur plan. Optionnel
        # pour que le service reste testable sans base.
        self.metrics = metrics
        # Inventaire des clients a IP fixe. Il fait AUTORITE sur leur adresse,
        # a l'inverse des abonnes PPPoE dont l'adresse doit venir du routeur :
        # ici il n'y a pas de pool qui reattribue, il y a un contrat.
        self.static_clients = static_clients
        self._write_clients: dict[str, RouterOsWriteClient] = {}
        self._write_client_factory = write_client_factory or LibrouterosWriteClient
        self.last_snapshot: TopologySnapshot | None = None
        # Quand la derniere decouverte a tourne. Distingue "aucun equipement"
        # de "aucune decouverte n'a encore eu lieu" -- deux causes opposees
        # derriere le meme arbre vide.
        self.last_discovery_at: datetime | None = None
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
        # Adresses et tunnels par PoP gere, pour deduire les liens routeur<->routeur
        # de la CONFIG (sous-reseaux /30 partages ET tunnels), la ou MNDP peut
        # manquer le lien. Les trois index (IP / MAC / identite -> case du routeur
        # gere) servent aussi a RECONNAITRE un routeur gere vu en voisin, pour ne
        # pas le dedoubler.
        router_addresses: list[tuple[str, str, list[dict[str, Any]]]] = []
        router_tunnels: list[tuple[str, str, list[dict[str, Any]]]] = []
        ip_owner: dict[str, str] = {}
        mac_owner: dict[str, str] = {}
        name_owner: dict[str, str] = {}
        # Loopback -> case du routeur. C'est l'index qui tranche : il est le seul
        # dont une correspondance vaut preuve d'identite.
        loopback_owner: dict[str, str] = {}
        loopbacks_par_routeur: dict[str, str] = {}
        # Ce que la CONFIGURATION dit de la hierarchie et des chemins.
        amonts: dict[str, tuple[str | None, str]] = {}
        pairs_routage: dict[str, list[str]] = {}
        piles_interfaces: dict[str, dict[str, InterfacePath]] = {}
        for collector, resultat in zip(collectors, resultats, strict=True):
            if isinstance(resultat, BaseException):
                message = f"{collector.name}: {type(resultat).__name__}: {resultat}"
                snapshot.warnings.append(message)
                logger.warning("Topologie non lue sur %s : %s", collector.name, resultat)
                # Un PoP injoignable ne doit pas DISPARAITRE de l'arbre : on pose
                # quand meme sa case (marquee injoignable), sinon l'operateur croit
                # l'avoir perdu alors que c'est juste la lecture qui a echoue.
                # Meme injoignable, il garde son role declare et son loopback :
                # sinon un coeur en panne se retrouverait pose en PoP, et l'arbre
                # se reorganiserait autour d'une panne.
                attributs_hs: dict[str, Any] = {
                    "managed": True,
                    "unreachable": True,
                    "error": str(resultat),
                    "role": str(collector.config.role),
                }
                if collector.config.loopback:
                    attributs_hs["loopback"] = collector.config.loopback
                    attributs_hs["loopback_source"] = "declare"
                snapshot.add_node(
                    TopologyNode(
                        key=router_node_key(collector.config.name),
                        name=collector.config.effective_pop_name or collector.config.name,
                        kind=kind_for_role(collector.config.role),
                        address=collector.config.host,
                        router_name=collector.config.name,
                        attributes=attributs_hs,
                    )
                )
                if collector.config.loopback:
                    loopbacks_par_routeur[collector.config.name] = collector.config.loopback
                continue
            # L'export n'est pas un parametre de build_from_router : on le retire
            # avant de deballer, puis on l'analyse a part.
            export = resultat.pop("export", "") or ""
            # La configuration voyage a part : build_from_router decrit ce que
            # le routeur VOIT, l'analyse de config decrit ce qu'il FAIT.
            config = {
                cle: resultat.pop(cle, []) or []
                for cle in (
                    "routes",
                    "vlans",
                    "bridge_ports",
                    "bondings",
                    "ospf_neighbors",
                    "bgp_sessions",
                )
            }
            analyse_export = parse_export(export) if export else {}
            # Le loopback est choisi AVANT de poser la case : c'est une propriete
            # d'identite, pas une decoration ajoutee apres coup.
            router_ids = analyse_export.get("router_ids") or []
            loopback, origine_loopback = pick_loopback(
                declared=collector.config.loopback,
                addresses=(resultat.get("addresses") or [])
                + (analyse_export.get("addresses") or []),
                router_id=router_ids[0] if router_ids else None,
            )
            build_from_router(
                snapshot,
                router_name=collector.config.name,
                pop_name=collector.config.effective_pop_name,
                host=collector.config.host,
                role=str(collector.config.role),
                loopback=loopback,
                loopback_source=origine_loopback,
                **resultat,
            )
            cle = router_node_key(collector.config.name)
            amont, raison_amont = best_upstream(config["routes"])
            amonts[collector.config.name] = (amont.gateway if amont else None, raison_amont)
            pairs = routing_peers(
                ospf_neighbors=config["ospf_neighbors"],
                bgp_sessions=config["bgp_sessions"],
            )
            if pairs:
                pairs_routage[collector.config.name] = pairs
            piles_interfaces[collector.config.name] = interface_stacks(
                interfaces=resultat.get("interfaces") or [],
                vlans=config["vlans"],
                bridge_ports=config["bridge_ports"],
                bondings=config["bondings"],
            )
            if loopback:
                loopbacks_par_routeur[collector.config.name] = loopback
            else:
                snapshot.warnings.append(
                    f"{collector.config.name} : aucun loopback trouve. Son identite "
                    f"retombe sur la MAC et l'adresse d'interface, moins sures. "
                    f"Declarez-le dans la fiche du PoP."
                )
            router_addresses.append((cle, collector.config.name, resultat.get("addresses") or []))
            # Toute IP portee par ce routeur pointe vers sa case (pour resoudre le
            # remote-address d'un tunnel vers le bon routeur).
            for ligne in resultat.get("addresses") or []:
                brut = str(ligne.get("address") or "").split("/")[0].strip()
                if brut:
                    ip_owner.setdefault(brut, cle)
            if collector.config.host:
                ip_owner.setdefault(str(collector.config.host), cle)
            # MAC et identite du routeur gere -> sa case (pour le reconnaitre en voisin).
            noeud_gere = snapshot.nodes.get(cle)
            if noeud_gere is not None:
                for mac in noeud_gere.attributes.get("macs") or []:
                    normalisee = normalize_mac(mac)
                    if normalisee:
                        mac_owner.setdefault(normalisee, cle)
            identite = str(resultat.get("identity") or "").strip().lower()
            if identite:
                name_owner.setdefault(identite, cle)
            if export:
                analyse = analyse_export
                router_tunnels.append((cle, collector.config.name, analyse.get("tunnels") or []))
                # L'export peut reveler des adresses absentes du /ip/address structure.
                for ligne in analyse.get("addresses") or []:
                    brut = str(ligne.get("address") or "").split("/")[0].strip()
                    if brut:
                        ip_owner.setdefault(brut, cle)
                router_addresses.append(
                    (cle, collector.config.name, analyse.get("addresses") or [])
                )

        # Un routeur gere vu en voisin par un autre ne doit PAS faire un doublon :
        # on replie ces cases decouvertes dans le routeur gere correspondant, les
        # liens pointent alors vers la case API. Ce qui reste = clients / non geres.
        # UNICITE : c'est la promesse du modele, donc c'est ce qu'il faut verifier.
        # Deux routeurs qui partagent un loopback sont une erreur de configuration,
        # et les fusionner silencieusement donnerait un arbre FAUX plutot
        # qu'incomplet. On ecarte donc l'adresse en double de l'index plutot que
        # de choisir un gagnant au hasard, et on le dit.
        proprietaires: dict[str, list[str]] = {}
        for nom_routeur, adresse in loopbacks_par_routeur.items():
            proprietaires.setdefault(adresse, []).append(nom_routeur)
        for adresse, noms in sorted(proprietaires.items()):
            if len(noms) > 1:
                snapshot.warnings.append(
                    f"Loopback {adresse} declare par {len(noms)} routeurs "
                    f"({', '.join(sorted(noms))}) : il doit etre unique. Aucun "
                    f"n'est identifie par cette adresse tant que ce n'est pas corrige."
                )
                logger.error("Loopback %s partage par %s", adresse, sorted(noms))
                continue
            loopback_owner[adresse] = router_node_key(noms[0])

        replies = resolve_to_managed(snapshot, ip_owner, mac_owner, name_owner, loopback_owner)
        if replies:
            logger.info("Topologie : %d voisin(s) reconnus comme routeurs geres", replies)

        # Liens deduits de la config, ajoutes APRES (ils ne comblent que les
        # adjacences manquantes) : d'abord les /30 point-a-point, puis les tunnels.
        ajoutes = link_by_shared_subnets(snapshot, router_addresses)
        ajoutes += link_by_tunnels(snapshot, ip_owner, router_tunnels)
        if ajoutes:
            logger.info("Topologie : %d lien(s) routeur<->routeur deduits de la config", ajoutes)

        # ROUTEURS CONNUS MAIS NON COLLECTES.
        #
        # Un PoP ecarte (secret illisible, fiche invalide) n'a pas de
        # collecteur : la boucle ci-dessus ne peut donc pas lui poser de case,
        # pas meme celle marquee "injoignable". Il disparaissait purement et
        # simplement de l'arbre -- le pire des affichages, parce que rien ne
        # distingue un PoP efface d'un PoP qui n'a jamais existe.
        #
        # Un routeur MASQUE a la main, lui, ne reapparait pas : le registre
        # l'a deja retire des ecartes, et c'est exactement ce que "Retirer"
        # doit faire.
        for ecarte in self.registry.skipped:
            nom = str(ecarte.get("name") or "")
            if not nom:
                continue  # panne globale de l'inventaire : rien a poser
            cle_ecarte = router_node_key(nom)
            if cle_ecarte in snapshot.nodes:
                continue
            snapshot.add_node(
                TopologyNode(
                    key=cle_ecarte,
                    name=str(ecarte.get("pop_name") or "") or nom,
                    kind=kind_for_role(ecarte.get("role")),
                    address=str(ecarte.get("host") or "") or None,
                    router_name=nom,
                    attributes={
                        "managed": True,
                        "excluded": True,
                        "error": str(ecarte.get("reason") or ""),
                        "source": str(ecarte.get("source") or ""),
                    },
                )
            )
            snapshot.warnings.append(
                f"{nom} : ecarte de la collecte ({ecarte.get('reason')}). Sa case "
                f"reste dans l'arbre, mais rien n'est lu sur lui -- ni topologie, "
                f"ni abonnes, ni detection des clients a IP fixe."
            )

            # ANALYSE DE CONFIGURATION : la hierarchie reelle.
        #
        # Elle vient APRES la reconciliation (les cases doivent etre fusionnees
        # pour que les passerelles se resolvent vers la bonne) et APRES les
        # liens deduits (une adjacence de routage marque un lien existant
        # plutot que d'en creer un second).
        proprietaire_adresse = {**ip_owner, **loopback_owner}
        prouves = link_by_routing_adjacency(snapshot, pairs_routage, proprietaire_adresse)
        if prouves:
            logger.info("Topologie : %d lien(s) prouves par une session de routage", prouves)
        orientes, alertes = orient_from_config(snapshot, amonts, proprietaire_adresse)
        snapshot.warnings.extend(alertes)
        if orientes:
            logger.info("Topologie : %d routeur(s) rattaches par leur table de routage", orientes)
        # Les piles d'interfaces servent a rattacher les clients a leur VRAI
        # port de sortie ; elles restent sur le snapshot pour l'appelant.
        snapshot.interface_paths = piles_interfaces

        if uisp_devices:
            attach_uisp_devices(snapshot, uisp_devices)

        # En dernier : les clients a IP fixe se raccrochent a des secteurs qui
        # viennent d'etre poses, UISP compris.
        clients = await self._static_clients_all()
        if clients:
            pop_keys = {
                c.config.effective_pop_name: router_node_key(c.config.name) for c in collectors
            }
            poses = attach_static_clients(snapshot, clients, pop_keys=pop_keys)
            logger.info("Topologie : %d client(s) a IP fixe declares", poses)

        self.last_snapshot = snapshot
        self.last_discovery_at = datetime.now(tz=UTC)
        if self.repository is not None:
            compte = await self.repository.save_snapshot(snapshot)
            logger.info("Topologie : %d noeud(s), %d lien(s)", compte["nodes"], compte["links"])
        return snapshot

    @staticmethod
    async def _read_router_topology(collector: MikrotikCollector) -> dict[str, Any]:
        client = collector._client  # noqa: SLF001 - lecture interne assumee
        timeout = max(collector.config.timeout_s * 4, 10.0)

        def lire() -> dict[str, Any]:
            serial = None
            lire_rb = getattr(client, "routerboard", None)
            if callable(lire_rb):
                serial = (lire_rb() or {}).get("serial-number")
            export = ""
            lire_exp = getattr(client, "export_config", None)
            if callable(lire_exp):
                export = lire_exp() or ""

            def optionnel(nom: str) -> list[dict[str, Any]]:
                """Lecture dont l'ABSENCE est normale.

                Les chemins de routage et de pontage different d'une version a
                l'autre, et un routeur peut n'avoir ni OSPF ni bridge. Un
                echec ici ne doit pas priver la topologie du reste.
                """
                methode = getattr(client, nom, None)
                if not callable(methode):
                    return []
                try:
                    return list(methode())
                except Exception:  # noqa: BLE001
                    logger.debug("%s indisponible sur %s", nom, collector.name)
                    return []

            return {
                "neighbors": client.neighbors(),
                "interfaces": client.interfaces(),
                "ethernet": client.ethernet(),
                "addresses": client.addresses(),
                "identity": client.identity(),
                "serial": serial,
                "export": export,
                # --- Configuration : d'ou vient la hierarchie reelle ---
                "routes": optionnel("routes"),
                "vlans": optionnel("vlans"),
                "bridge_ports": optionnel("bridge_ports"),
                "bondings": optionnel("bondings"),
                "ospf_neighbors": optionnel("ospf_neighbors"),
                "bgp_sessions": optionnel("bgp_sessions"),
            }

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    async def export_router(self, router_name: str) -> dict[str, Any]:
        """Config complete d'un PoP (``/export``) + son analyse.

        Sert a VOIR ce que le controleur percoit du routeur : le texte brut et ce
        qu'on en tire (adresses, tunnels, commentaires). Lecture seule."""
        collector = next((c for c in self.registry.collectors if c.name == router_name), None)
        if collector is None:
            raise KeyError(router_name)
        client = collector._client  # noqa: SLF001 - lecture interne assumee
        timeout = max(collector.config.timeout_s * 4, 15.0)

        def lire() -> str:
            lire_exp = getattr(client, "export_config", None)
            return (lire_exp() or "") if callable(lire_exp) else ""

        texte = await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)
        return {"router_name": router_name, "export": texte, "parsed": parse_export(texte)}

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
        # Resserrages decides par la boucle fermee QoE (phase 4). Ils vivent dans
        # leur propre table, pas dans les surcharges : une surcharge est une
        # decision d'exploitant, un resserrage une decision de la boucle, et les
        # confondre ferait qu'un cycle automatique ecraserait un debit saisi a la
        # main. Les deux se composent dans ``shaped_capacity``.
        resserrages = await self.repository.qoe_trims()

        collector = self._collector(router_name)
        pop_name = collector.config.effective_pop_name

        liens: list[LinkTarget] = []
        # cle du noeud d'en face -> file parent, pour rattacher chaque abonne au
        # lien qu'il traverse REELLEMENT et non a un lien pris au hasard.
        parent_par_noeud: dict[str, str] = {}
        # interface physique -> file parente. Complete parent_par_noeud pour les
        # clients rattaches par leur VLAN plutot que par un secteur declare.
        parent_par_interface: dict[str, str] = {}
        for lien in await self.repository.links():
            if lien.get("discovered_by") != router_name or not lien.get("interface"):
                continue
            surcharge = surcharges_liens.get(lien["key"], {})
            if surcharge and not surcharge.get("enabled", True):
                continue
            cible = LinkTarget(
                name=str(lien.get("target_name") or lien["interface"]),
                interface=str(lien["interface"]),
                subnet=_segment_du_lien(lien),
                measured_capacity_mbps=lien.get("capacity_mbps"),
                override_down_mbps=surcharge.get("max_down_mbps"),
                override_up_mbps=surcharge.get("max_up_mbps"),
                trim_factor=resserrages.get(str(lien["key"]), 1.0),
            )
            liens.append(cible)
            if lien.get("target_key"):
                parent_par_noeud[str(lien["target_key"])] = cible.queue_name
            # Index par PORT : c'est par la que passent les clients dont on
            # connait le VLAN mais pas le secteur radio.
            parent_par_interface[str(lien["interface"])] = cible.queue_name

        # La capacite radio mesuree prime sur le debit negocie du port : c'est
        # elle le vrai goulot d'un backhaul sans fil.
        for backhaul in await self.metrics.backhaul_latest():
            if backhaul.get("pop_name") != pop_name or not backhaul.get("capacity_mbps"):
                continue
            for cible_lien in liens:
                if cible_lien.name == backhaul["name"]:
                    cible_lien.measured_capacity_mbps = backhaul["capacity_mbps"]

        # L'ADRESSE VIENT DU ROUTEUR, PAS DE LA BASE.
        #
        # C'est elle qui portera la file. Une adresse issue de la derniere
        # collecte peut avoir jusqu'a un cycle de retard : si l'abonne s'est
        # reconnecte entre-temps, le pool a pu reattribuer son IP a un voisin,
        # et on briderait le mauvais client. /ppp/active est la seule source qui
        # dit ce qui est vrai a l'instant ou l'on ecrit.
        sessions = {s.login: s for s in await collector.collect()}

        abonnes: list[SubscriberTarget] = []
        for ligne in await self.metrics.subscriber_latest(limit=5000, order_by="login"):
            if ligne.get("pop_name") != pop_name:
                continue
            # Les clients a IP fixe sont traites plus bas, a partir de leur
            # fiche : la ligne de metriques dirait 'pas de session' et les
            # rendrait non shapables, alors qu'ils sont joignables en permanence.
            if ligne.get("kind") == KIND_STATIC:
                continue
            login = str(ligne["login"])
            surcharge = surcharges_abonnes.get(login, {})
            secteur = rattachements.get(login)
            session = sessions.pop(login, None)
            abonnes.append(
                self._cible_abonne(
                    login,
                    collector=collector,
                    session=session,
                    plan_down=ligne.get("plan_down_mbps"),
                    plan_up=ligne.get("plan_up_mbps"),
                    surcharge=surcharge,
                    parent=parent_par_noeud.get(secteur) if secteur else None,
                )
            )

        # Sessions ouvertes que la base ne connait pas encore (abonne apparu
        # entre deux cycles de collecte). Une surcharge posee a la main doit
        # s'appliquer des maintenant, sans attendre le prochain tour.
        for login, session in sessions.items():
            surcharge = surcharges_abonnes.get(login, {})
            if not surcharge:
                continue
            secteur = rattachements.get(login)
            abonnes.append(
                self._cible_abonne(
                    login,
                    collector=collector,
                    session=session,
                    plan_down=None,
                    plan_up=None,
                    surcharge=surcharge,
                    parent=parent_par_noeud.get(secteur) if secteur else None,
                )
            )

        # Clients a IP fixe du meme PoP. Ils rejoignent la MEME liste : a partir
        # d'ici, plan(), le diff de reconciliation et apply() ne font plus
        # aucune difference entre les deux natures.
        ports_par_vlan = self._ports_par_vlan(router_name)
        for client in await self._static_clients_for(pop_name):
            surcharge = surcharges_abonnes.get(client.reference, {})
            secteur = client.sector_key or rattachements.get(client.reference)
            parent = parent_par_noeud.get(secteur) if secteur else None
            if parent is None and client.vlan is not None:
                # Aucun secteur declare : la CONFIGURATION sait quand meme par
                # ou ce client sort. Son VLAN est pose sur une interface, qui
                # descend jusqu'a un port physique, qui porte un lien connu --
                # et ce lien est sa vraie file parente. Sans cela, un client
                # sans secteur pendait a la racine et echappait au partage du
                # lien qu'il sature pourtant.
                for port in ports_par_vlan.get(client.vlan, []):
                    parent = parent_par_interface.get(port)
                    if parent is not None:
                        break
            abonnes.append(self._cible_statique(client, surcharge=surcharge, parent=parent))

        return liens, abonnes

    def _ports_par_vlan(self, router_name: str) -> dict[int, list[str]]:
        """``identifiant de VLAN -> ports physiques`` sur ce routeur.

        Vient de l'analyse de configuration faite a la derniere decouverte. Vide
        tant qu'aucune decouverte n'a tourne : le rattachement retombe alors sur
        le secteur declare, comme avant.
        """
        if self.last_snapshot is None:
            return {}
        piles = self.last_snapshot.interface_paths.get(router_name) or {}
        par_vlan: dict[int, list[str]] = {}
        for chemin in piles.values():
            if chemin.vlan_id is None or chemin.broken:
                continue
            par_vlan.setdefault(chemin.vlan_id, []).extend(chemin.ports)
        return par_vlan

    async def _static_clients_all(self) -> list[StaticClient]:
        """Toutes les fiches actives, tous PoPs confondus."""
        if self.static_clients is None:
            return []
        try:
            clients: list[StaticClient] = await self.static_clients.load_enabled()
        except Exception:  # noqa: BLE001
            logger.exception("Inventaire des clients statiques illisible, topologie sans eux")
            return []
        return clients

    async def _static_clients_for(self, pop_name: str) -> list[StaticClient]:
        """Fiches actives de ce PoP, ou liste vide si l'inventaire est absent.

        Un inventaire illisible ne doit pas faire echouer le plan des abonnes
        PPPoE : on journalise et on continue avec ce qu'on a.
        """
        return [c for c in await self._static_clients_all() if c.pop_name == pop_name]

    def _cible_statique(
        self,
        client: StaticClient,
        *,
        surcharge: dict[str, Any],
        parent: str | None,
    ) -> SubscriberTarget:
        """Traduit une fiche d'inventaire en cible de shaping.

        Meme objet et meme hierarchie de debits que pour un abonne PPPoE : le
        boost prime sur la surcharge, qui prime sur le plan. Seule l'origine du
        plan change -- la fiche au lieu de RADIUS -- et l'interface reste vide,
        parce que ce client n'en a pas a lui.
        """
        return SubscriberTarget(
            login=client.reference,
            interface="",
            kind=KIND_STATIC,
            address=client.address,
            plan_down_mbps=client.plan_down_mbps,
            plan_up_mbps=client.plan_up_mbps,
            override_down_mbps=surcharge.get("max_down_mbps"),
            override_up_mbps=surcharge.get("max_up_mbps"),
            boost_down_mbps=surcharge.get("boost_down_mbps"),
            boost_up_mbps=surcharge.get("boost_up_mbps"),
            boost_expires_at=surcharge.get("boost_expires_at"),
            enabled=surcharge.get("enabled", True),
            parent=parent,
        )

    def _cible_abonne(
        self,
        login: str,
        *,
        collector: MikrotikCollector,
        session: Any,
        plan_down: float | None,
        plan_up: float | None,
        surcharge: dict[str, Any],
        parent: str | None,
    ) -> SubscriberTarget:
        return SubscriberTarget(
            login=login,
            interface=collector.config.pppoe_interface_pattern.format(
                login=login, name=login, user=login
            ),
            address=getattr(session, "address", None),
            plan_down_mbps=plan_down,
            plan_up_mbps=plan_up,
            override_down_mbps=surcharge.get("max_down_mbps"),
            override_up_mbps=surcharge.get("max_up_mbps"),
            boost_down_mbps=surcharge.get("boost_down_mbps"),
            boost_up_mbps=surcharge.get("boost_up_mbps"),
            boost_expires_at=surcharge.get("boost_expires_at"),
            enabled=surcharge.get("enabled", True),
            parent=parent,
        )

    async def plan_router(self, router_name: str, *, prune: bool | None = None) -> Plan:
        """Raccourci : assemble l'etat desire puis compare au routeur."""
        liens, abonnes = await self.build_targets(router_name)
        return await self.plan(router_name, links=liens, subscribers=abonnes, prune=prune)

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
                # JAMAIS de purge dans une application automatique.
                #
                # Ce job ecrit sans revue humaine. Depuis que la file est
                # accrochee a l'adresse de la session, un /ppp/active vide --
                # coupure momentanee de l'API, PoP qui redemarre -- ferait
                # apparaitre tout le monde comme hors ligne, et la purge
                # supprimerait toutes les files du PoP sans que personne ne
                # l'ait vu passer. Ce job n'a besoin que de RAMENER les debits
                # boostes : il n'a aucune raison de supprimer quoi que ce soit.
                plan = await self.plan_router(router_name, prune=False)
                if plan.is_empty:
                    continue
                applique = await self.apply(plan, dry_run=False, author="system:boost-expiry")
                resultat["routers"].append(router_name)
                resultat["applied"] += applique.applied
            except Exception as exc:  # noqa: BLE001
                resultat["errors"].append(f"{router_name}: {type(exc).__name__}: {exc}")
                logger.exception("Retrait de boost impossible sur %s", router_name)
        return resultat

    # --------------------------------------------------------- reconciliation
    async def reconcile(self) -> dict[str, Any]:
        """Reapplique l'etat desire sur tous les routeurs, sans intervention.

        C'est ce qui fait qu'un debit saisi dans l'interface PLAFONNE vraiment :
        sans cette boucle, la surcharge reste une intention en base tant que
        personne n'a demande un plan puis ne l'a applique a la main. Elle
        rattrape aussi les reconnexions -- une file posee sur l'adresse d'hier
        ne bride plus rien apres un changement d'IP.

        Deux garde-fous, volontairement les memes que pour les boosts :

        - rien n'est ecrit tant que ``ENFORCEMENT_ENABLED`` est faux ;
        - JAMAIS de purge. Ce job ecrit sans revue humaine, et un
          ``/ppp/active`` vide -- API coupee, PoP qui redemarre -- ferait passer
          tout le monde pour hors ligne : la purge supprimerait alors toutes les
          files du PoP sans que personne ne l'ait vu passer.
        """
        resultat: dict[str, Any] = {
            "enabled": self._enforcement_enabled,
            "routers": [],
            "applied": 0,
            "errors": [],
        }
        if not self._enforcement_enabled:
            return resultat

        for collector in self.registry.collectors:
            nom = collector.name
            try:
                plan = await self.plan_router(nom, prune=False)
                if plan.is_empty:
                    continue
                applique = await self.apply(plan, dry_run=False, author="system:reconcile")
                resultat["routers"].append(nom)
                resultat["applied"] += applique.applied
                if plan.conflicts:
                    # Un conflit ne bloque pas les autres files, mais il laisse
                    # un abonne non bride : le taire le rendrait introuvable.
                    for conflit in plan.conflicts:
                        logger.warning(
                            "Reconciliation %s : %s non ecrite -- %s",
                            nom,
                            conflit.name,
                            conflit.detail,
                        )
            except Exception as exc:  # noqa: BLE001 - un routeur ne bloque pas les autres
                resultat["errors"].append(f"{nom}: {type(exc).__name__}: {exc}")
                logger.exception("Reconciliation impossible sur %s", nom)
        return resultat

    # ------------------------------------------------- boucle fermee QoE
    async def adjust_for_qoe(self) -> dict[str, Any]:
        """Ajuste le partage d'un SECTEUR selon la QoE de ses abonnes (phase 4).

        Le meme enchainement que ``reconcile()`` -- lecture, decision, plan,
        application -- mais declenche par un signal qui, jusqu'ici, n'alimentait
        qu'un tableau de bord : le score de QoE composite (bufferbloat + latence
        a vide, ``app.services.qoe``). C'est LA MEME fonction de score que la
        heatmap Executif : le declencheur et l'ecran ne peuvent pas diverger.

        Ce qui bouge quand un secteur decroche, c'est l'enveloppe PARTAGEE de ce
        secteur -- la file du lien qui le dessert -- et rien d'autre. Le plan
        souscrit d'un abonne n'est jamais touche : un abonne n'est pas
        responsable du bufferbloat de son secteur, et lui retirer le debit qu'il
        paie serait la mauvaise reponse. CAKE arbitre ensuite entre les circuits,
        comme d'habitude.

        Memes garde-fous que les autres boucles automatiques :

        - le resserrage est BORNE (``QOE_TRIM_FLOOR``) : la boucle ne coupe
          jamais un secteur, elle le ramene au plus bas sous son goulot ;
        - rien n'est ecrit tant que ``ENFORCEMENT_ENABLED`` est faux -- la
          decision est quand meme prise et journalisee, pour qu'on puisse LIRE ce
          que la boucle ferait avant de lui donner la main ;
        - JAMAIS de purge (``prune=False``), pour la meme raison que
          ``reconcile()`` : un ``/ppp/active`` momentanement vide ne doit pas
          faire disparaitre les files d'un PoP ;
        - le plan passe par ``build_plan`` comme tous les autres, donc il est
          diffable, journalise dans ``enforcement_audit``, et visible dans
          l'interface. Aucun chemin d'ecriture parallele.
        """
        resultat: dict[str, Any] = {
            "enabled": self._enforcement_enabled,
            "window_minutes": self.settings.qoe_window_minutes,
            "threshold": self.settings.qoe_score_threshold,
            "scored": 0,
            "sectors": [],
            "routers": [],
            "applied": 0,
            "plans": [],
            "errors": [],
        }
        if self.repository is None or self.metrics is None:
            return resultat

        notes = await self.metrics.qoe_subscribers(minutes=self.settings.qoe_window_minutes)
        resultat["scored"] = len(notes)
        rattachements = await self.repository.attachments()
        etats = await self.repository.qoe_link_states()

        # Le lien qui DESSERT un secteur est celui dont le secteur est la cible :
        # c'est deja la convention de ``build_targets`` pour rattacher un abonne
        # a sa file parente.
        lien_par_secteur: dict[str, dict[str, Any]] = {}
        for ligne in await self.repository.links():
            cle_secteur = ligne.get("target_key")
            if cle_secteur and ligne.get("interface"):
                lien_par_secteur.setdefault(str(cle_secteur), ligne)

        par_secteur: dict[str, list[dict[str, Any]]] = {}
        for note in notes:
            secteur = rattachements.get(str(note["login"]))
            if secteur is None:
                # Abonne dont on ignore le secteur : il ne peut incriminer
                # personne. On ne devine pas un rattachement.
                continue
            par_secteur.setdefault(secteur, []).append(note)

        routeurs: dict[str, list[str]] = {}
        for secteur in sorted(par_secteur):
            mesures = par_secteur[secteur]
            lien = lien_par_secteur.get(secteur)
            if lien is None:
                resultat["errors"].append(
                    f"secteur {secteur} : aucun lien connu ne le dessert, rien a resserrer"
                )
                continue

            verdict = decide_sector(
                sector_key=secteur,
                link_key=str(lien["key"]),
                scores=[float(m["score"]) for m in mesures],
                state=SectorState.from_row(etats.get(str(lien["key"]))),
                threshold=self.settings.qoe_score_threshold,
                min_degraded=self.settings.qoe_min_degraded_subscribers,
                step=self.settings.qoe_trim_step,
                floor=self.settings.qoe_trim_floor,
                recovery_cycles=self.settings.qoe_recovery_cycles,
                logins=[str(m["login"]) for m in mesures],
            )
            resultat["sectors"].append(verdict.as_dict())
            if verdict.action == ACTION_UNKNOWN:
                continue

            await self.repository.save_qoe_link_state(
                link_key=verdict.link_key,
                sector_key=verdict.sector_key,
                trim_factor=verdict.trim_after,
                healthy_cycles=verdict.healthy_cycles,
                scored_count=verdict.scored,
                degraded_count=verdict.degraded,
                worst_score=verdict.worst_score,
                last_action=verdict.action,
                last_reason=verdict.reason,
                triggered=verdict.changed,
            )
            if verdict.changed:
                logger.warning("Boucle QoE : %s -- %s", verdict.sector_key, verdict.reason)
                nom = str(lien.get("discovered_by") or "")
                if nom:
                    routeurs.setdefault(nom, []).append(verdict.sector_key)
                else:
                    resultat["errors"].append(
                        f"secteur {verdict.sector_key} : lien sans routeur d'origine, "
                        "resserrage enregistre mais non applicable"
                    )

        # Seuls les routeurs dont un secteur a REELLEMENT bouge sont replanifies :
        # replanifier tout le parc a chaque cycle serait le travail de
        # ``reconcile()``, pas celui-ci.
        for nom in sorted(routeurs):
            try:
                # JAMAIS de purge, meme raison que reconcile() et expire_boosts().
                plan = await self.plan_router(nom, prune=False)
                resultat["plans"].append(plan.to_dict())
                if plan.is_empty:
                    continue
                if not self._enforcement_enabled:
                    resultat["errors"].append(
                        f"{nom}: enforcement desactive, le resserrage est enregistre "
                        "et le plan calcule mais rien n'est ecrit sur le routeur"
                    )
                    continue
                applique = await self.apply(plan, dry_run=False, author="system:qoe-loop")
                resultat["routers"].append(nom)
                resultat["applied"] += applique.applied
            except Exception as exc:  # noqa: BLE001 - un routeur ne bloque pas les autres
                resultat["errors"].append(f"{nom}: {type(exc).__name__}: {exc}")
                logger.exception("Ajustement QoE impossible sur %s", nom)
        return resultat

    async def _routers_for_logins(self, logins: set[str]) -> list[str]:
        """Quels routeurs portent ces abonnes. Evite de replanifier tout le parc."""
        if not logins:
            return []
        pops = set()
        if self.metrics is not None:
            for ligne in await self.metrics.subscriber_latest(limit=5000, order_by="login"):
                if ligne["login"] in logins and ligne.get("pop_name"):
                    pops.add(ligne["pop_name"])
        # L'inventaire complete les metriques : un client statique boostee le
        # jour meme de sa saisie n'a pas encore d'echantillon, et son PoP ne
        # serait pas retrouve.
        if self.static_clients is not None:
            try:
                for client in await self.static_clients.load_enabled():
                    if client.reference in logins:
                        pops.add(client.pop_name)
            except Exception:  # noqa: BLE001
                logger.exception("Inventaire statique illisible, PoPs deduits des metriques seules")
        return [c.name for c in self.registry.collectors if c.config.effective_pop_name in pops]

    # ----------------------------------------------------------------- plan
    async def plan(
        self,
        router_name: str,
        *,
        links: list[LinkTarget],
        subscribers: list[SubscriberTarget],
        prune: bool | None = None,
    ) -> Plan:
        """Calcule ce qu'il faudrait faire. N'ecrit rien."""
        collector = self._collector(router_name)
        etat = await self._inspect_one(collector)

        types, files, ecartes = desired_state(
            links=links,
            subscribers=subscribers,
            safety_factor=self.settings.shaping_safety_factor,
            floor_mbps=self.settings.shaping_floor_mbps,
            queue_types=desired_queue_types(
                overhead=self.settings.cake_overhead,
                rtt_ms=self.settings.cake_rtt_ms,
                diffserv=self.settings.cake_diffserv,
                flowmode=self.settings.cake_flowmode,
                nat=self.settings.cake_nat,
                ack_filter=self.settings.cake_ack_filter,
                wash=self.settings.cake_wash,
                mpu=self.settings.cake_mpu,
            ),
            target_mode=self.settings.subscriber_queue_target,
            queue_unmeasured_links=self.settings.shaping_queue_for_detected_links,
        )
        plan = build_plan(
            router_name,
            desired_types=types,
            desired_queues=files,
            actual_types=etat.queue_types,
            actual_queues=etat.simple_queues,
            prune=self.settings.shaping_prune if prune is None else prune,
            adopt=self.settings.shaping_adopt_foreign_queues,
        )
        plan.skipped = ecartes
        return plan

    # ---------------------------------------------------------------- apply
    async def apply(
        self, plan: Plan, *, dry_run: bool = True, author: str | None = None
    ) -> ApplyResult:
        """Execute un plan.

        Deux verrous : le drapeau global ``ENFORCEMENT_ENABLED``, et le fait que
        ``dry_run`` vaut vrai par defaut. Les deux doivent etre leves.

        ``author`` identifie qui declenche l'action -- un compte connecte pour une
        demande venue de l'interface, ou "system:*" pour les boucles automatiques.
        Il est journalise dans ``enforcement_audit`` : une commande sans auteur
        est intracable.
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
                author=author,
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

        def lire() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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


def _segment_du_lien(lien: dict[str, Any]) -> str | None:
    """Segment L3 porte par le port du lien, lu dans ``/ip/address``.

    Une interface peut en porter plusieurs (un /30 de gestion et le /23 des
    abonnes) : on retient le PLUS LARGE, celui qui couvre le trafic qu'on
    cherche a shaper, pas le lien de service.
    """
    brut = lien.get("attributes")
    if isinstance(brut, str):
        try:
            brut = json.loads(brut)
        except ValueError:
            return None
    if not isinstance(brut, dict):
        return None

    reseaux: list[tuple[int, str]] = []
    for valeur in brut.get("local_networks") or []:
        normalise = network_target(valeur)
        if normalise is None:
            continue
        reseaux.append((ipaddress.ip_network(normalise).prefixlen, normalise))
    if not reseaux:
        return None
    return min(reseaux)[1]


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

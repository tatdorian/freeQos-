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
from app.collectors.parsing import parse_flag
from app.collectors.topology import (
    KIND_RADIO,
    KIND_SECTOR,
    TopologyNode,
    TopologySnapshot,
    ambiguous_neighbor_macs,
    attach_static_clients,
    attach_uisp_devices,
    build_from_router,
    kind_for_role,
    link_by_routing_adjacency,
    link_by_shared_subnets,
    link_by_tunnels,
    map_subscribers_to_sectors,
    mark_reciprocal_links,
    mark_subscriber_cpes,
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
from app.enforcement.models import PREFIX, Plan, QueueSpec, network_target, slugify
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
from app.services.limit_audit import audit_router
from app.services.pop_match import explain as explain_pop
from app.services.pop_match import resolve_pop
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
        # L'inventaire TEL QU'IL ETAIT a la derniere decouverte. C'est lui la
        # reference pour savoir si le graphe est perime : le comparer a
        # l'inventaire courant dit si un routeur a ete ajoute, retire ou
        # reconfigure depuis. Comparer deux rechargements successifs ne
        # marcherait pas -- n'importe quel autre appel (l'API qui liste les
        # routeurs, par exemple) recharge l'inventaire et effacerait l'ecart
        # avant que le job periodique ne l'ait vu.
        self.last_inventory_signature: tuple[tuple[Any, ...], ...] | None = None
        # Etat courant du drapeau. La base fait foi une fois amorcee ; la
        # variable d'environnement ne sert plus qu'a la valeur initiale.
        self._enforcement_enabled = settings.enforcement_enabled
        # Ce qu'a fait le DERNIER passage de la boucle de reconciliation. C'est
        # elle qui ecrit sur les routeurs ; sans cette trace, l'exploitant n'a
        # aucun moyen de voir qu'elle tourne, et croit que rien ne se passe.
        self.last_reconcile: dict[str, Any] | None = None

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
        # Candidats AVANT arbitrage : un indice peut etre revendique par
        # plusieurs routeurs, et c'est precisement ce qu'il faut savoir.
        ip_candidats: dict[str, set[str]] = {}
        mac_candidats: dict[str, set[str]] = {}
        name_candidats: dict[str, set[str]] = {}
        # Loopback -> case du routeur. C'est l'index qui tranche : il est le seul
        # dont une correspondance vaut preuve d'identite.
        loopback_owner: dict[str, str] = {}
        loopbacks_par_routeur: dict[str, str] = {}
        # Ce que la CONFIGURATION dit de la hierarchie et des chemins.
        amonts: dict[str, tuple[str | None, str]] = {}
        pairs_routage: dict[str, list[str]] = {}
        piles_interfaces: dict[str, dict[str, InterfacePath]] = {}
        # Sessions PPPoE de tous les PoPs, avec leur caller-id : c'est la matiere
        # de la jointure abonne -> secteur radio, faite en fin de decouverte.
        sessions_pppoe: list[dict[str, Any]] = []
        # MAC annoncees par PLUSIEURS voisins distincts, calculees sur TOUS les
        # routeurs avant de construire quoi que ce soit. Une cle de noeud doit
        # etre decidee avec la vue d'ensemble : prise routeur par routeur, la
        # premiere occurrence garderait la MAC et les suivantes s'ecraseraient
        # dessus, dans un ordre qui depend de la lecture.
        tous_voisins: list[dict[str, Any]] = []
        for resultat in resultats:
            if not isinstance(resultat, BaseException):
                tous_voisins.extend(resultat.get("neighbors") or [])
        macs_ambigues = ambiguous_neighbor_macs(tous_voisins)
        if macs_ambigues:
            snapshot.warnings.append(
                f"{len(macs_ambigues)} MAC annoncee(s) par plusieurs equipements distincts : "
                f"elles ne servent plus a les identifier. Des equipements clones depuis la "
                f"meme image donnent ce symptome."
            )
            logger.warning("MAC de voisins ambigues : %s", sorted(macs_ambigues))

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
                cle_hs = router_node_key(collector.config.name)
                snapshot.add_node(
                    TopologyNode(
                        key=cle_hs,
                        name=collector.config.effective_pop_name or collector.config.name,
                        kind=kind_for_role(collector.config.role),
                        address=collector.config.host,
                        router_name=collector.config.name,
                        attributes=attributs_hs,
                    )
                )
                if collector.config.loopback:
                    loopbacks_par_routeur[collector.config.name] = collector.config.loopback
                # SON ADRESSE DE MANAGEMENT RESTE UNE VERITE, meme quand la
                # lecture echoue : l'exploitant a declare "ce routeur est a cette
                # adresse". Sans cet index, un voisin qui l'annonce ne se
                # reconnaissait pas en lui et posait une SECONDE case pour le
                # meme equipement -- le routeur apparaissait deux fois dans
                # l'arbre, une fois injoignable et une fois en voisin anonyme,
                # exactement quand l'operateur cherche a comprendre pourquoi il
                # ne repond pas.
                if collector.config.host:
                    ip_candidats.setdefault(str(collector.config.host), set()).add(cle_hs)
                continue
            # L'export n'est pas un parametre de build_from_router : on le retire
            # avant de deballer, puis on l'analyse a part.
            export = resultat.pop("export", "") or ""
            # Les sessions non plus : elles ne decrivent pas le voisinage du
            # routeur, elles servent la jointure de fin de decouverte.
            sessions_pppoe.extend(resultat.pop("ppp", []) or [])
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
            #
            # DEUX sources de router-id, dans cet ordre : les chemins structures
            # (/routing/id, instances OSPF et BGP) puis le texte de /export. Les
            # structures d'abord parce qu'elles repondent toujours, la ou l'API
            # refuse '/export' selon la version -- le loopback disparaissait alors
            # sans bruit, et le routeur retombait sur sa MAC pour s'identifier.
            router_ids = [
                *(resultat.pop("router_ids", []) or []),
                *(analyse_export.get("router_ids") or []),
            ]
            loopback, origine_loopback = pick_loopback(
                declared=collector.config.loopback,
                addresses=(resultat.get("addresses") or [])
                + (analyse_export.get("addresses") or []),
                router_ids=router_ids,
            )
            build_from_router(
                snapshot,
                router_name=collector.config.name,
                pop_name=collector.config.effective_pop_name,
                host=collector.config.host,
                role=str(collector.config.role),
                loopback=loopback,
                loopback_source=origine_loopback,
                ambiguous_macs=macs_ambigues,
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
                    ip_candidats.setdefault(brut, set()).add(cle)
            if collector.config.host:
                ip_candidats.setdefault(str(collector.config.host), set()).add(cle)
            # MAC et identite du routeur gere -> sa case (pour le reconnaitre en voisin).
            noeud_gere = snapshot.nodes.get(cle)
            if noeud_gere is not None:
                for mac in noeud_gere.attributes.get("macs") or []:
                    normalisee = normalize_mac(mac)
                    if normalisee:
                        mac_candidats.setdefault(normalisee, set()).add(cle)
            identite = str(resultat.get("identity") or "").strip().lower()
            if identite:
                name_candidats.setdefault(identite, set()).add(cle)
            if export:
                analyse = analyse_export
                router_tunnels.append((cle, collector.config.name, analyse.get("tunnels") or []))
                # L'export peut reveler des adresses absentes du /ip/address structure.
                for ligne in analyse.get("addresses") or []:
                    brut = str(ligne.get("address") or "").split("/")[0].strip()
                    if brut:
                        ip_candidats.setdefault(brut, set()).add(cle)
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

        # UN INDICE PARTAGE PAR DEUX ROUTEURS N'IDENTIFIE PLUS PERSONNE.
        #
        # Ces index servent a reconnaitre un routeur gere quand un voisin
        # l'annonce. Ils etaient remplis avec ``setdefault`` : en cas de
        # collision, le PREMIER routeur rencontre gagnait, en silence, et tous
        # les voisins de l'autre se repliaient dans la mauvaise case.
        #
        # Les collisions ne sont pas theoriques : des CHR deployees depuis la
        # meme image partagent les MAC de leurs interfaces, et les
        # configurations modeles donnent le meme /30 de liaison a tous les
        # sites. Un indice ambigu est donc ECARTE -- ne pas savoir vaut mieux
        # que rattacher au hasard.
        ip_owner = _index_sans_ambiguite(ip_candidats, "adresse", snapshot)
        mac_owner = _index_sans_ambiguite(mac_candidats, "MAC", snapshot)
        name_owner = _index_sans_ambiguite(name_candidats, "identite", snapshot)

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

        # LA JOINTURE QUI DONNE SA CHAINE DE GOULOTS A UN ABONNE.
        #
        # caller-id (MAC du CPE, cote RouterOS) contre la MAC des stations
        # connues d'UISP : c'est le seul moyen de savoir par QUEL secteur radio
        # passe un abonne. Sans elle on sait seulement qu'il est sur un PoP, sa
        # file est posee a la racine, et la boucle fermee QoE n'a aucun secteur a
        # incriminer -- les deux fonctionnalites restaient donc inertes.
        #
        # Elle vient APRES attach_uisp_devices (les stations doivent etre dans le
        # graphe) et APRES attach_static_clients (qui inscrit, lui, les
        # rattachements DECLARES des clients a IP fixe dans le meme index).
        self._joindre_secteurs(snapshot, sessions_pppoe, uisp_devices or [])

        # LE CPE D'UN ABONNE N'EST PAS UN EQUIPEMENT DE PLUS.
        #
        # Le routeur d'un abonne se presente aux deux bouts du controleur : une
        # session PPPoE d'un cote -- c'est l'abonne -- et un voisin MNDP de
        # l'autre -- c'est un equipement decouvert. L'arbre montrait les deux, et
        # l'exploitant y comptait plus de clients qu'il n'en a. 'caller-id' porte
        # la MAC du CPE : elle recolle les deux vues sur une EGALITE.
        #
        # Apres '_joindre_secteurs', qui a besoin de ces memes noeuds tels qu'ils
        # ont ete decouverts pour calculer les rattachements.
        cpe_abonnes = {
            mac: login
            for mac, login in (
                (
                    normalize_mac(session.get("caller-id") or session.get("caller_id")),
                    str(session.get("name") or session.get("login") or ""),
                )
                for session in sessions_pppoe
            )
            if mac and login
        }
        # Sur une VLAN routee il n'y a pas de session, donc pas de 'caller-id' :
        # le client y est declare par son ADRESSE, et c'est elle qui fait la
        # jointure. Meme regle, autre preuve. Les deux index se completent -- un
        # PoP peut servir des abonnes PPPoE et des clients sur VLAN.
        adresses_abonnes: dict[str, str] = {}
        for session in sessions_pppoe:
            login = str(session.get("name") or session.get("login") or "")
            ip = str(session.get("address") or "").split("/")[0].strip()
            if login and ip:
                adresses_abonnes[ip] = login
        for client in clients:
            reference = str(getattr(client, "reference", "") or "")
            ip = str(getattr(client, "address", "") or "").split("/")[0].strip()
            if reference and ip:
                adresses_abonnes[ip] = reference

        reconnus = mark_subscriber_cpes(snapshot, cpe_abonnes, adresses_abonnes)
        if reconnus:
            logger.info(
                "Topologie : %d equipement(s) decouvert(s) reconnus comme CPE d'abonne", reconnus
            )

        # Un cable vu par ses deux bouts a produit deux liens : on marque le
        # second pour que l'interface n'en montre qu'un. En DERNIER, quand plus
        # aucune passe n'ajoute de lien.
        miroirs = mark_reciprocal_links(snapshot)
        if miroirs:
            logger.info("Topologie : %d lien(s) reciproques replies sur un seul cable", miroirs)

        self.last_snapshot = snapshot
        self.last_discovery_at = datetime.now(tz=UTC)
        self.last_inventory_signature = self.registry.inventory_signature()
        if self.repository is not None:
            compte = await self.repository.save_snapshot(snapshot)
            logger.info("Topologie : %d noeud(s), %d lien(s)", compte["nodes"], compte["links"])
            await self._oublier_routeurs_retires(collectors)
            await self._persister_rattachements(snapshot, sessions_pppoe)
        return snapshot

    async def _oublier_routeurs_retires(self, collectors: Sequence[MikrotikCollector]) -> None:
        """Retire du graphe les cases des routeurs sortis de l'inventaire.

        La persistance est volontairement additive : un PoP momentanement
        illisible ne doit pas disparaitre de l'arbre. Mais un routeur SUPPRIME de
        l'inventaire n'en sortait jamais non plus -- l'exploitant le retirait
        depuis l'interface et le voyait encore, sans rien pour l'expliquer.
        """
        oublier = getattr(self.repository, "forget_removed_routers", None)
        if not callable(oublier):
            return  # depot d'une generation anterieure, ou double de test
        try:
            await oublier([c.name for c in collectors])
        except Exception:  # noqa: BLE001 - un nettoyage rate ne casse rien
            logger.exception("Nettoyage des routeurs retires impossible")

    def _joindre_secteurs(
        self,
        snapshot: TopologySnapshot,
        sessions: list[dict[str, Any]],
        uisp_devices: list[dict[str, Any]],
    ) -> None:
        """Remplit ``snapshot.subscriber_sectors`` pour les abonnes PPPoE.

        Deux index de stations sont essayes, dans cet ordre :

        1. le rattachement declare par UISP (``attributes.apDevice``), qui donne
           l'AP exact d'une station -- la meilleure reponse quand elle existe ;
        2. a defaut, la MAC de la station contre les noeuds RADIO deja poses
           dans le graphe. Un exploitant sans UISP (mode ``airos``, ou radios lues
           en direct) n'a jamais d'``apDevice`` : sans ce second index, aucun
           abonne ne serait jamais rattache chez lui.

        Ne rattache JAMAIS de force : un abonne dont le CPE n'est reconnu nulle
        part reste sans secteur, et ``_persister_rattachements`` le dit.
        """
        # LE SECTEUR DOIT ETRE UNE CLE DE NOEUD DE CE GRAPHE, pas un identifiant
        # UISP brut. Quand l'AP est aussi vu en voisin MNDP par le PoP -- le cas
        # normal, puisqu'il est au bout d'un port -- ``attach_uisp_devices`` a
        # FUSIONNE la fiche UISP dans le noeud existant, qui garde sa cle
        # ``mac:...``. Poser ``uisp:<id>`` designerait alors un noeud qui n'existe
        # pas : le rattachement serait ecrit, et le planificateur comme la boucle
        # QoE ne trouveraient aucun lien desservant ce secteur.
        noeud_par_uisp_id: dict[str, str] = {
            str(node.uisp_device_id): node.key
            for node in snapshot.nodes.values()
            if node.uisp_device_id
        }

        stations: dict[str, str] = {}
        for device in uisp_devices:
            identification = device.get("identification") or {}
            mac = normalize_mac(identification.get("mac"))
            if not mac:
                continue
            parent_id = ((device.get("attributes") or {}).get("apDevice") or {}).get("id")
            if parent_id:
                cle_ap = noeud_par_uisp_id.get(str(parent_id), f"uisp:{parent_id}")
                if cle_ap in snapshot.nodes:
                    stations[mac] = cle_ap

        # Second index : les radios du graphe, par MAC. Il ne remplace pas le
        # premier -- une station rattachee par UISP garde son AP -- il comble le
        # cas ou la MAC du CPE est elle-meme celle d'un equipement connu.
        for node in snapshot.nodes.values():
            if node.kind in {KIND_RADIO, KIND_SECTOR} and node.mac:
                stations.setdefault(node.mac, node.key)

        map_subscribers_to_sectors(snapshot, sessions, stations)

    async def _persister_rattachements(
        self, snapshot: TopologySnapshot, sessions: list[dict[str, Any]]
    ) -> None:
        """Ecrit les rattachements du graphe dans ``subscriber_attachments``.

        C'est l'etape qui manquait : le graphe savait deja rattacher un abonne a
        son secteur, mais le resultat mourait avec le snapshot. Or le
        planificateur et la boucle QoE lisent la TABLE, pas le graphe -- ils
        travaillaient donc en permanence sur un index vide.

        ON N'EFFACE JAMAIS UN RATTACHEMENT DEVENU ABSENT, on ne fait qu'ajouter
        et mettre a jour. Meme raison que le ``prune=False`` de la
        reconciliation : une lecture UISP momentanement muette detacherait tout
        le parc d'un coup, ferait remonter chaque file d'abonne a la racine, et
        la decouverte suivante les redescendrait -- une oscillation qui reecrit
        les files de tous les PoPs a chaque hoquet. Un CPE qui change vraiment de
        secteur est corrige au cycle suivant, puisque la jointure le reconduit.

        La MAC du CPE accompagne le rattachement quand on la connait : elle
        permet de verifier apres coup d'ou vient un rattachement, et distingue un
        abonne PPPoE observe d'un client statique declare.
        """
        if not snapshot.subscriber_sectors:
            return
        ecrire = getattr(self.repository, "save_attachments", None)
        if not callable(ecrire):
            return  # depot d'une generation anterieure, ou double de test

        macs: dict[str, str] = {}
        for session in sessions:
            login = str(session.get("name") or session.get("login") or "")
            mac = normalize_mac(session.get("caller-id") or session.get("caller_id"))
            if login and mac:
                macs[login] = mac

        rattachements = {
            login: (secteur, macs.get(login))
            for login, secteur in snapshot.subscriber_sectors.items()
        }
        try:
            ecrits = await ecrire(rattachements)
        except Exception:  # noqa: BLE001 - un rattachement perdu ne casse pas la decouverte
            logger.exception("Rattachements abonne -> secteur non enregistres")
            return
        logger.info("Topologie : %d rattachement(s) abonne -> secteur enregistres", ecrits)

    @staticmethod
    async def _read_router_topology(collector: MikrotikCollector) -> dict[str, Any]:
        client = collector._client  # noqa: SLF001 - lecture interne assumee
        timeout = max(collector.config.timeout_s * 4, 10.0)

        def lire() -> dict[str, Any]:
            # LE NUMERO DE SERIE EST L'IDENTITE DE L'EQUIPEMENT.
            #
            # Deux sources, parce qu'un parc en melange les deux natures :
            # '/system/routerboard' donne le numero grave dans le materiel, et
            # '/system/license' le 'system-id' d'une CHR, qui n'a pas de
            # RouterBOARD. Sans la seconde, un parc virtualise n'a AUCUN
            # identifiant propre : des CHR deployees depuis la meme image
            # partagent jusqu'aux MAC de leurs interfaces, et rien ne les
            # distingue plus les unes des autres.
            serial = None
            lire_rb = getattr(client, "routerboard", None)
            if callable(lire_rb):
                serial = (lire_rb() or {}).get("serial-number")
            if not serial:
                lire_lic = getattr(client, "license_id", None)
                if callable(lire_lic):
                    try:
                        serial = lire_lic()
                    except Exception:  # noqa: BLE001 - jamais bloquant
                        logger.debug("license_id indisponible sur %s", collector.name)
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

            # Router-id lu par les chemins STRUCTURES. Dans un reseau
            # d'operateur c'est le loopback, et c'est la seule source qui reponde
            # quand aucune interface ne s'appelle 'lo' et que l'API refuse
            # '/export' (ce qui arrive selon la version).
            router_ids: list[str] = []
            lire_ids = getattr(client, "routing_ids", None)
            if callable(lire_ids):
                try:
                    router_ids = [str(v) for v in (lire_ids() or [])]
                except Exception:  # noqa: BLE001 - jamais bloquant
                    logger.debug("routing_ids indisponible sur %s", collector.name)

            return {
                "neighbors": client.neighbors(),
                "interfaces": client.interfaces(),
                "ethernet": client.ethernet(),
                "addresses": client.addresses(),
                "identity": client.identity(),
                "serial": serial,
                "export": export,
                "router_ids": router_ids,
                # Sessions PPPoE : lues ICI et pas dans un second passage, pour
                # leur champ 'caller-id' (la MAC du CPE de l'abonne). C'est la
                # seule cle qui relie un abonne a la station radio par laquelle
                # il passe. Optionnelle : un coeur ou une passerelle n'a pas de
                # serveur PPPoE, et son absence ne doit rien faire echouer.
                "ppp": optionnel("ppp_active"),
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

    # NOTE : la jointure abonne -> secteur vit dans ``_joindre_secteurs``, appele
    # par ``discover()``. Elle a longtemps eu ici un jumeau public (``map_sectors``)
    # que PERSONNE n'appelait : la fonctionnalite paraissait donc presente alors
    # qu'aucun abonne n'etait jamais rattache. Un seul chemin desormais, celui que
    # la decouverte emprunte vraiment.

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
        # Identites PHYSIQUES de l'equipement d'en face -> la file du lien qui le
        # dessert. C'est par elles que la capacite radio mesuree retrouve son
        # lien, plus bas.
        liens_par_identite: dict[str, LinkTarget] = {}
        for lien in await self.repository.links():
            if lien.get("discovered_by") != router_name or not lien.get("interface"):
                continue
            surcharge = surcharges_liens.get(lien["key"], {})
            if surcharge and not surcharge.get("enabled", True):
                # Le lien reste dans l'etat desire, DESACTIVE. Le retirer d'ici
                # le faisait disparaitre partout : ni file, ni motif, ni ligne
                # sur la carte du shaping -- alors qu'un lien sans file parente
                # ne partage plus rien, ce qui se voit sur le reseau. Il n'entre
                # en revanche dans aucun index : il ne doit ni servir de parent,
                # ni capter la capacite mesuree d'un backhaul.
                liens.append(
                    LinkTarget(
                        name=str(lien.get("target_name") or lien["interface"]),
                        interface=str(lien["interface"]),
                        subnet=_segment_du_lien(lien),
                        measured_capacity_mbps=lien.get("capacity_mbps"),
                        enabled=False,
                    )
                )
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
            for identite in _identites_du_bout_distant(lien):
                liens_par_identite.setdefault(identite, cible)
            if lien.get("target_key"):
                parent_par_noeud[str(lien["target_key"])] = cible.queue_name
            # Index par PORT : c'est par la que passent les clients dont on
            # connait le VLAN mais pas le secteur radio.
            parent_par_interface[str(lien["interface"])] = cible.queue_name

        # La capacite radio mesuree prime sur le debit negocie du port : c'est
        # elle le vrai goulot d'un backhaul sans fil.
        #
        # LE RAPPROCHEMENT SE FAIT SUR L'IDENTITE PHYSIQUE DE LA RADIO, PAS SUR
        # SON NOM. Le nom d'un lien est l'identite que l'equipement d'en face
        # annonce en MNDP/LLDP ('NanoBeam-Nord') ; le nom d'un backhaul est le
        # libelle saisi par l'exploitant dans l'inventaire ('bh-1'). Rien ne les
        # oblige a coincider, et en pratique ils different presque toujours : la
        # capacite mesuree n'atteignait alors jamais la file, qui restait posee
        # sur le debit negocie du PORT -- soit le plafond du cable ethernet, pas
        # celui de la parabole. C'est precisement le goulot que ce controleur
        # existe pour tenir.
        #
        # L'egalite des noms reste acceptee en DERNIER recours : elle sert les
        # inventaires ou l'exploitant a deliberement nomme le backhaul comme la
        # radio, et ne coute rien quand l'identite a deja tranche.
        for backhaul in await self.metrics.backhaul_latest():
            if backhaul.get("pop_name") != pop_name or not backhaul.get("capacity_mbps"):
                continue
            cible_lien = None
            for identite in _identites_du_backhaul(backhaul):
                cible_lien = liens_par_identite.get(identite)
                if cible_lien is not None:
                    break
            if cible_lien is None:
                cible_lien = next(
                    (lien for lien in liens if lien.name == backhaul["name"]),
                    None,
                )
            if cible_lien is None:
                logger.info(
                    "Backhaul '%s' (%s) sans lien correspondant sur %s : sa capacite "
                    "mesuree (%s Mbps) ne s'applique a aucune file parente.",
                    backhaul.get("name"),
                    backhaul.get("uisp_device_id") or "sans identifiant",
                    router_name,
                    backhaul.get("capacity_mbps"),
                )
                continue
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
        for client in await self._static_clients_for(collector):
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

    async def _static_clients_for(self, collector: MikrotikCollector) -> list[StaticClient]:
        """Fiches actives que CE routeur doit shaper.

        Le rapprochement passe par ``resolve_pop`` et non par une egalite de
        chaine : "francophonie" saisi a la main et "PoP Francophonie" porte par
        le routeur designent le meme site, et une egalite stricte laissait le
        client hors de l'etat desire -- donc sans file, sans erreur, et sans que
        rien ne le dise.

        Un inventaire illisible ne doit pas faire echouer le plan des abonnes
        PPPoE : on journalise et on continue avec ce qu'on a.
        """
        routeurs = self.registry.collectors
        retenus: list[StaticClient] = []
        for client in await self._static_clients_all():
            match = resolve_pop(client.pop_name, routeurs)
            if any(c.name == collector.name for c in match.collectors):
                retenus.append(client)
        return retenus

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

    # ------------------------------------------------ carte du shaping
    #
    # LA QUESTION DE L'EXPLOITANT N'EST PAS "quelles commandes as-tu envoyees",
    # c'est "OU est-ce que ca bride, et a combien". Les commandes sont un moyen ;
    # les montrer comme resultat oblige a relire du RouterOS pour reconstruire
    # mentalement une carte que le controleur possede deja.
    ETAT_MANUELLE = "posee-a-la-main"

    POINT_LIEN = "lien"
    POINT_ABONNE = "abonne"

    async def shaping_points(self, router_name: str | None = None) -> dict[str, Any]:
        """Ou le shaping s'applique sur le reseau, et ou il ne s'applique pas.

        Rend un ARBRE : chaque lien parent porte les abonnes qui passent par lui.
        C'est la hierarchie que RouterOS applique reellement, et c'est aussi
        celle qui explique un debit -- un abonne a 100 Mbps sous un backhaul
        plafonne a 80 partage ces 80 avec ses voisins.

        Les points ECARTES y figurent au meme titre que les autres : un abonne
        sans file n'est pas absent de la carte, il y est avec son motif. Une
        carte qui ne montrerait que ce qui marche laisserait chercher le reste
        dans le journal des commandes, c'est-a-dire nulle part.

        Lecture seule. Aucun plan n'est applique ici, meme quand l'enforcement
        est actif : cette page REGARDE, la boucle de reconciliation ECRIT.
        """
        noms = [c.name for c in self.registry.collectors if router_name in (None, "", c.name)]
        resultats = await asyncio.gather(
            *(self._points_un_routeur(nom) for nom in noms), return_exceptions=True
        )
        routeurs: list[dict[str, Any]] = []
        for nom, resultat in zip(noms, resultats, strict=True):
            if isinstance(resultat, BaseException):
                logger.exception("Carte du shaping impossible sur %s", nom)
                routeurs.append(
                    {
                        "router": nom,
                        "error": f"{type(resultat).__name__}: {resultat}",
                        "points": [],
                        "counts": {},
                    }
                )
                continue
            routeurs.append(resultat)
        return {
            "enforcement_enabled": self._enforcement_enabled,
            "last_reconcile": self.last_reconcile,
            "reconcile_interval_s": self.settings.shaping_reconcile_interval_s,
            "routers": routeurs,
        }

    async def _points_un_routeur(self, router_name: str) -> dict[str, Any]:
        """La carte d'un routeur, batie sur le MEME calcul que le plan.

        Rien n'est recalcule a cote : les memes ``build_targets`` et
        ``desired_state`` que l'ecriture, puis ``build_plan`` pour savoir ce qui
        est deja en place. Une carte qui raconterait autre chose que ce que fait
        le controleur serait pire qu'une absence de carte.
        """
        collector = self._collector(router_name)
        liens, abonnes = await self.build_targets(router_name)
        etat = await self._inspect_one(collector)
        types, files, ecartes = desired_state(
            links=liens,
            subscribers=abonnes,
            safety_factor=self.settings.shaping_safety_factor,
            floor_mbps=self.settings.shaping_floor_mbps,
            target_mode=self.settings.subscriber_queue_target,
            queue_unmeasured_links=self.settings.shaping_queue_for_detected_links,
        )
        plan = build_plan(
            router_name,
            desired_types=types,
            desired_queues=files,
            actual_types=etat.queue_types,
            actual_queues=etat.simple_queues,
            prune=False,
            adopt=self.settings.shaping_adopt_foreign_queues,
        )
        a_ecrire = {a.name for a in plan.actions if a.path == "/queue/simple" and a.name}
        conflits = {c.name: c.detail for c in plan.conflicts}
        motifs = {e.login: e.reason for e in ecartes}
        specs = {f.name: f for f in files}

        points: list[dict[str, Any]] = []
        for lien in liens:
            points.append(
                self._point(
                    kind=self.POINT_LIEN,
                    label=lien.name,
                    spec=specs.get(lien.queue_name),
                    queue_name=lien.queue_name,
                    detail={
                        "interface": lien.interface,
                        "capacity_mbps": lien.measured_capacity_mbps,
                        "trim_factor": lien.trim_factor,
                        "override": lien.override_down_mbps is not None
                        or lien.override_up_mbps is not None,
                    },
                    a_ecrire=a_ecrire,
                    conflits=conflits,
                    motifs=motifs,
                )
            )
        for abonne in abonnes:
            points.append(
                self._point(
                    kind=self.POINT_ABONNE,
                    label=abonne.login,
                    spec=specs.get(abonne.queue_name),
                    queue_name=abonne.queue_name,
                    detail={
                        "nature": abonne.kind,
                        "address": abonne.address,
                        "plan_down_mbps": abonne.plan_down_mbps,
                        "plan_up_mbps": abonne.plan_up_mbps,
                        "override": abonne.override_down_mbps is not None
                        or abonne.override_up_mbps is not None,
                        "boost": abonne.boost_active(),
                    },
                    a_ecrire=a_ecrire,
                    conflits=conflits,
                    motifs=motifs,
                )
            )

        # Les files posees a la main par l'exploitant SONT du shaping : les
        # omettre donnerait une carte qui contredit le routeur. Elles ne sont
        # jamais touchees par le controleur, et c'est dit.
        for file_tierce in etat.foreign_queues:
            if parse_flag(file_tierce.get("disabled")):
                continue
            points.append(
                {
                    "kind": self.POINT_ABONNE,
                    "name": str(file_tierce.get("name") or ""),
                    "label": str(file_tierce.get("name") or ""),
                    "target": str(file_tierce.get("target") or ""),
                    "parent": str(file_tierce.get("parent") or "") or None,
                    "down_mbps": None,
                    "up_mbps": None,
                    "limit": str(file_tierce.get("max-limit") or ""),
                    "source": "posee par l'exploitant, hors controleur",
                    "state": self.ETAT_MANUELLE,
                    "reason": "cette file ne porte pas freeqos:managed : le controleur ne la "
                    "modifie jamais",
                    "detail": {},
                    "children": [],
                }
            )

        return {
            "router": router_name,
            "pop_name": collector.config.effective_pop_name,
            "reachable": etat.reachable,
            "error": etat.error,
            "points": _en_arbre(points),
            "counts": _compter(points),
        }

    def _point(
        self,
        *,
        kind: str,
        label: str,
        spec: QueueSpec | None,
        queue_name: str,
        detail: dict[str, Any],
        a_ecrire: set[str],
        conflits: dict[str, str],
        motifs: dict[str, str],
    ) -> dict[str, Any]:
        """Un point de la carte : ce qui est bride, a combien, et dans quel etat."""
        if queue_name in conflits:
            etat, motif = self.ETAT_CONFLIT, conflits[queue_name]
        elif spec is None:
            etat = self.ETAT_ECARTE
            motif = motifs.get(label, "aucune file : rien a appliquer ici")
        elif queue_name in a_ecrire:
            etat = self.ETAT_A_POSER
            motif = (
                "sera ecrite au prochain passage de la reconciliation"
                if self._enforcement_enabled
                else "l'enforcement est desactive : rien n'est ecrit tant qu'il ne l'est pas"
            )
        else:
            etat, motif = self.ETAT_POSEE, "file en place, conforme a ce qui est prevu"
        return {
            "kind": kind,
            "name": queue_name,
            "label": label,
            "target": spec.target if spec else None,
            "parent": spec.parent if spec else None,
            "down_mbps": spec.max_down_mbps if spec else None,
            "up_mbps": spec.max_up_mbps if spec else None,
            "source": _origine_du_debit(kind, detail),
            "state": etat,
            "reason": motif,
            "detail": detail,
            "children": [],
        }

    # ------------------------------------------- files des clients declares
    #
    # Etats rendus a l'interface. Ils repondent tous a la meme question, celle
    # qu'on se pose apres avoir declare un client : "est-ce qu'il est bride, et
    # sinon qu'est-ce qui manque ?".
    ETAT_POSEE = "file-posee"
    ETAT_RETIREE = "file-retiree"
    ETAT_A_POSER = "file-a-poser"
    ETAT_ECARTE = "ecarte"
    ETAT_SANS_ROUTEUR = "sans-routeur"
    ETAT_CONFLIT = "conflit"
    ETAT_ERREUR = "erreur"

    @staticmethod
    def queue_name_for(reference: str) -> str:
        """Le nom de file d'un client declare. Meme regle que le planificateur."""
        return f"{PREFIX}{slugify(reference)}"

    async def enforce_static_client(
        self,
        *,
        reference: str,
        pop_name: str,
        author: str,
        removing: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pose (ou retire) la file de CE client, tout de suite.

        POURQUOI NE PAS ATTENDRE LA RECONCILIATION. Elle passe toutes les deux
        minutes et fait le travail -- mais entre la declaration et son passage,
        le client n'est pas bride et rien ne dit pourquoi. L'exploitant ne peut
        pas distinguer "ca arrive" de "ca n'arrivera jamais parce que le PoP est
        mal ecrit". Poser la file au moment de la saisie supprime cette fenetre,
        et surtout : le rapport rendu ici NOMME ce qui manque quand rien n'est
        pose.

        RIEN D'AUTRE QUE CETTE FILE N'EST TOUCHE. Le plan est calcule en entier
        -- il faut les parents et les types CAKE -- puis restreint au nom de ce
        client. Declarer un abonne n'ecrit donc pas les files des autres, et un
        retrait ne peut pas emporter le PoP meme calcule avec ``prune``.
        """
        routeurs = self.registry.collectors
        match = resolve_pop(pop_name, routeurs)
        rapport: dict[str, Any] = {
            "reference": reference,
            "pop_name": match.pop_name or pop_name,
            "pop_declared": pop_name,
            "pop_resolution": match.resolution,
            "enforcement_enabled": self._enforcement_enabled,
            "applied": 0,
            "routers": [],
        }
        if not match.found:
            rapport["state"] = self.ETAT_SANS_ROUTEUR
            rapport["reason"] = explain_pop(match, pop_name, routeurs)
            return rapport

        nom_file = self.queue_name_for(reference)
        for collector in match.collectors:
            rapport["routers"].append(
                await self._enforce_one(
                    collector.name,
                    reference=reference,
                    queue_name=nom_file,
                    author=author,
                    removing=removing,
                    dry_run=dry_run,
                )
            )
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])
        principal = self._etat_principal(rapport["routers"])
        rapport["state"] = principal["state"]
        rapport["reason"] = principal["reason"]
        rapport["router"] = principal["router"]
        return rapport

    # ------------------------------------------------ plafonds reellement tenus
    async def limit_audit(self, router_name: str | None = None) -> dict[str, Any]:
        """Les plafonds decides sont-ils REELLEMENT tenus par le reseau ?

        Ni le plan ("ce que je veux ecrire") ni le journal ("ce que j'ai ecrit")
        ne repondent a cette question. Une file peut exister, porter le bon
        debit, se lire sans erreur -- et ne rien brider : fasttrack actif, file
        masquee par une autre, file desactivee a la main. Chacune de ces causes
        est silencieuse sur RouterOS. C'est ici qu'on va les chercher.

        Lecture seule. Ce qui CORRIGE, c'est l'application immediate d'un
        plafond et la boucle de reconciliation ; ce qui se lit ici, c'est
        l'ecart entre ce qui est decide et ce que le reseau applique vraiment.
        """
        noms = [router_name] if router_name else [c.name for c in self.registry.collectors]
        routeurs: list[dict[str, Any]] = []
        for nom in noms:
            try:
                routeurs.append(await self._audit_un_routeur(nom))
            except Exception as exc:  # noqa: BLE001 - un PoP muet n'annule pas les autres
                logger.exception("Audit des plafonds impossible sur %s", nom)
                routeurs.append(
                    {
                        "router": nom,
                        "error": f"{type(exc).__name__}: {exc}",
                        "queues": [],
                        "fasttrack": {"active": None, "rules": [], "detail": "routeur non lu"},
                        "enforced": 0,
                        "leaking": 0,
                        "counts": {},
                    }
                )
        return {
            "enforcement_enabled": self._enforcement_enabled,
            "last_reconcile": self.last_reconcile,
            "routers": routeurs,
            "enforced": sum(int(r.get("enforced") or 0) for r in routeurs),
            "leaking": sum(int(r.get("leaking") or 0) for r in routeurs),
        }

    async def _audit_un_routeur(self, router_name: str) -> dict[str, Any]:
        """L'audit d'un routeur, sur le MEME etat desire que l'ecriture."""
        collector = self._collector(router_name)
        liens, abonnes = await self.build_targets(router_name)
        etat = await self._inspect_one(collector)
        _, files, _ = desired_state(
            links=liens,
            subscribers=abonnes,
            safety_factor=self.settings.shaping_safety_factor,
            floor_mbps=self.settings.shaping_floor_mbps,
            target_mode=self.settings.subscriber_queue_target,
            queue_unmeasured_links=self.settings.shaping_queue_for_detected_links,
        )
        rapport = audit_router(
            router_name=router_name,
            desired=files,
            rows=etat.simple_queues,
            firewall=await self._firewall_rules(collector),
        )
        # Qui est derriere chaque file : l'exploitant cherche un ABONNE, pas un
        # nom de file. Le rapprochement se fait sur le nom calcule, celui-la
        # meme qui sert de cle de reconciliation.
        par_file = {a.queue_name: a for a in abonnes}
        for ligne in rapport["queues"]:
            abonne = par_file.get(str(ligne["name"]))
            if abonne is not None:
                ligne["login"] = abonne.login
                ligne["kind"] = abonne.kind
        return rapport

    @staticmethod
    async def _firewall_rules(collector: MikrotikCollector) -> list[dict[str, Any]] | None:
        """Regles de pare-feu, ou [] si le compte n'a pas le droit de les lire.

        Un compte en lecture seule tres restreint peut se voir refuser
        ``/ip/firewall/filter``. Ce n'est pas une raison de faire echouer
        l'audit : on rend ``None``, que le verdict distingue soigneusement d'une
        liste vide. "Je n'ai pas pu lire" et "il n'y a rien" sont deux reponses
        differentes, et les confondre ferait chercher la panne ailleurs.
        """
        client = collector._client  # noqa: SLF001
        timeout = max(collector.config.timeout_s * 4, 10.0)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(client.firewall_filters), timeout=timeout
            )
        except Exception:  # noqa: BLE001
            logger.warning("Pare-feu illisible sur %s : fasttrack non verifie", collector.name)
            return None

    async def enforce_subscriber(
        self,
        *,
        login: str,
        author: str,
        removing: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pose TOUT DE SUITE le plafond de cet abonne, PPPoE ou a IP fixe.

        POURQUOI. Un plafond saisi dans l'interface n'etait qu'une ligne en base
        jusqu'au passage suivant de la reconciliation -- et, enforcement coupe,
        jusqu'a jamais. Entre les deux, l'exploitant lisait "100 kbps impose" sur
        une ligne qui passait 497 kbps : l'interface affichait une INTENTION en
        la presentant comme un FAIT. Ecrire au moment de la saisie supprime cette
        fenetre, et le rapport rendu ici dit ce qui a reellement ete ecrit, ou ce
        qui l'a empeche.

        Le mecanisme est celui, deja eprouve, de la declaration d'un client a IP
        fixe : plan complet du routeur -- il faut les parents et les types CAKE
        -- puis RESTRICTION a la chaine de cette seule file. Fixer le debit d'un
        abonne n'ecrit donc pas les files des autres.
        """
        nom_file = self.queue_name_for(login)
        routeurs = await self._routers_for_logins({login})
        rapport: dict[str, Any] = {
            "login": login,
            "queue": nom_file,
            "enforcement_enabled": self._enforcement_enabled,
            "applied": 0,
            "routers": [],
        }
        if not routeurs:
            rapport["state"] = self.ETAT_SANS_ROUTEUR
            rapport["reason"] = (
                "aucun routeur ne porte cet abonne : ni echantillon de mesure, ni "
                "fiche d'inventaire ne le rattachent a un PoP connu"
            )
            rapport["router"] = None
            return rapport
        for router_name in routeurs:
            rapport["routers"].append(
                await self._enforce_one(
                    router_name,
                    reference=login,
                    queue_name=nom_file,
                    author=author,
                    removing=removing,
                    dry_run=dry_run,
                )
            )
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])
        principal = self._etat_principal(rapport["routers"])
        rapport["state"] = principal["state"]
        rapport["reason"] = principal["reason"]
        rapport["router"] = principal["router"]
        return rapport

    async def enforce_link(
        self,
        *,
        link_key: str,
        author: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pose tout de suite l'enveloppe d'un lien, pour les memes raisons.

        Un plafond de lien est encore plus sensible qu'un plafond d'abonne : il
        borne tout ce qui passe derriere. Le laisser en attente d'un cycle,
        c'est laisser le lien saturer pendant ce temps-la.
        """
        rapport: dict[str, Any] = {
            "link_key": link_key,
            "enforcement_enabled": self._enforcement_enabled,
            "applied": 0,
            "routers": [],
        }
        if self.repository is None:
            rapport["state"] = self.ETAT_ERREUR
            rapport["reason"] = "topologie indisponible : le lien ne peut pas etre retrouve"
            rapport["router"] = None
            return rapport
        liens = await self.repository.links()
        lien = next((ligne for ligne in liens if ligne.get("key") == link_key), None)
        if lien is None:
            rapport["state"] = self.ETAT_SANS_ROUTEUR
            rapport["reason"] = f"aucun lien connu sous la cle '{link_key}'"
            rapport["router"] = None
            return rapport
        nom = str(lien.get("target_name") or lien.get("interface") or link_key)
        nom_file = f"{PREFIX}parent-{slugify(nom)}"
        rapport["queue"] = nom_file
        router_name = str(lien.get("discovered_by") or "")
        if not router_name:
            rapport["state"] = self.ETAT_SANS_ROUTEUR
            rapport["reason"] = "ce lien n'est rattache a aucun routeur connu"
            rapport["router"] = None
            return rapport
        rapport["routers"].append(
            await self._enforce_one(
                router_name,
                reference=nom,
                queue_name=nom_file,
                author=author,
                removing=False,
                dry_run=dry_run,
            )
        )
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])
        principal = self._etat_principal(rapport["routers"])
        rapport["state"] = principal["state"]
        rapport["reason"] = principal["reason"]
        rapport["router"] = principal["router"]
        return rapport

    async def enforce_policy(
        self, scope: str, target_key: str, *, author: str, removing: bool = False
    ) -> dict[str, Any]:
        """Applique le plafond qui vient d'etre saisi, quel qu'en soit le sujet."""
        if scope == "link":
            return await self.enforce_link(link_key=target_key, author=author)
        return await self.enforce_subscriber(login=target_key, author=author, removing=removing)

    def _plan_impossible(self) -> str | None:
        """Ce qui empeche de planifier, ou None.

        Sans topologie ni metriques, ``build_targets`` rend deux listes vides :
        le client serait absent du plan, et l'absence d'action se lirait alors
        comme "file conforme". Ce serait faux, et faux dans le sens le plus
        trompeur -- on annoncerait un client bride qui ne l'est pas.
        """
        if self.repository is None or self.metrics is None:
            return (
                "planification indisponible : ni topologie ni metriques (base non "
                "initialisee). La file sera calculee des que la base repondra."
            )
        return None

    async def _enforce_one(
        self,
        router_name: str,
        *,
        reference: str,
        queue_name: str,
        author: str,
        removing: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Le sort de cette file sur UN routeur, applique ou seulement decrit."""
        ligne: dict[str, Any] = {
            "router": router_name,
            "applied": 0,
            "actions": [],
            "state": self.ETAT_POSEE,
            "reason": "file deja conforme sur le routeur",
        }
        empeche = self._plan_impossible()
        if empeche is not None:
            ligne["state"] = self.ETAT_ERREUR
            ligne["reason"] = empeche
            return ligne
        try:
            plan = await self.plan_router(router_name, prune=removing)
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas la saisie
            logger.exception("Plan impossible sur %s pour '%s'", router_name, reference)
            ligne["state"] = self.ETAT_ERREUR
            ligne["reason"] = f"{type(exc).__name__}: {exc}"
            return ligne

        ecarte = next((s for s in plan.skipped if s.login == reference), None)
        conflit = next((c for c in plan.conflicts if c.name == queue_name), None)
        restreint = plan.restrict_to(plan.parent_chain(queue_name), keep_types=not removing)
        ligne["actions"] = [action.summary() for action in restreint.actions]

        if conflit is not None:
            ligne["state"] = self.ETAT_CONFLIT
            ligne["reason"] = conflit.detail
            return ligne
        if ecarte is not None:
            # Le motif vient du planificateur lui-meme : "aucun debit a
            # appliquer", "adresse revendiquee aussi par...". C'est la reponse
            # exacte a "pourquoi ce client n'a pas de file", et elle est rendue
            # telle quelle plutot que reformulee.
            ligne["state"] = self.ETAT_ECARTE
            ligne["reason"] = ecarte.reason
            return ligne
        if restreint.is_empty:
            if removing:
                ligne["state"] = self.ETAT_RETIREE
                ligne["reason"] = "aucune file de ce client sur le routeur"
            return ligne

        if dry_run or not self._enforcement_enabled:
            ligne["state"] = self.ETAT_A_POSER
            ligne["reason"] = (
                "l'enforcement est desactive : la file est calculee, rien n'est ecrit "
                "tant qu'il ne sera pas actif (onglet Shaping)"
                if not self._enforcement_enabled
                else "simulation : rien n'a ete ecrit"
            )
            return ligne

        try:
            resultat = await self.apply(restreint, dry_run=False, author=author)
        except Exception as exc:  # noqa: BLE001 - la fiche est deja enregistree
            logger.exception("Ecriture impossible sur %s pour '%s'", router_name, reference)
            ligne["state"] = self.ETAT_ERREUR
            ligne["reason"] = f"{type(exc).__name__}: {exc}"
            return ligne

        ligne["applied"] = resultat.applied
        rates = [o for o in resultat.outcomes if not o.ok]
        if rates:
            ligne["state"] = self.ETAT_ERREUR
            ligne["reason"] = "; ".join(str(o.detail) for o in rates if o.detail)
            return ligne
        # L'etat vient de ce qui a ete FAIT, pas de l'intention.
        #
        # Retirer une surcharge n'efface pas forcement la file : l'abonne
        # retombe sur son plan souscrit, et la commande envoyee est alors un
        # 'set' vers ce debit-la. Annoncer "file retiree" parce qu'on a demande
        # un plan avec purge laisserait croire l'abonne sans plafond alors qu'il
        # vient d'en recevoir un autre.
        retiree = any(
            action.verb == "remove" and action.name == queue_name for action in restreint.actions
        )
        ligne["state"] = self.ETAT_RETIREE if retiree else self.ETAT_POSEE
        ligne["reason"] = (
            "file retiree du routeur" if retiree else f"{resultat.applied} commande(s) appliquee(s)"
        )
        return ligne

    # Du plus urgent au moins urgent : c'est cet ordre qui decide ce qu'on
    # montre en resume quand un PoP porte plusieurs routeurs.
    _ORDRE_ETATS = (
        ETAT_ERREUR,
        ETAT_CONFLIT,
        ETAT_ECARTE,
        ETAT_A_POSER,
        ETAT_POSEE,
        ETAT_RETIREE,
    )

    def _etat_principal(self, lignes: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """Ce qu'on retient quand plusieurs routeurs portent le meme PoP.

        Le plus urgent gagne : un client bride sur un routeur et en erreur sur
        l'autre doit se voir, pas se noyer.
        """
        if not lignes:
            return {"state": self.ETAT_SANS_ROUTEUR, "reason": "aucun routeur", "router": None}
        rang = {etat: i for i, etat in enumerate(self._ORDRE_ETATS)}
        return min(lignes, key=lambda r: rang.get(str(r["state"]), len(rang)))

    async def static_clients_enforcement(self) -> list[dict[str, Any]]:
        """Etat de la file de CHAQUE fiche declaree. N'ecrit rien.

        Un plan par routeur concerne, pas un par client : l'exploitant veut
        l'etat de son inventaire, pas dix lectures du meme routeur. La question
        posee est toujours la meme -- "ce client est-il bride, et sinon qu'est-ce
        qui manque ?" -- et la reponse vient du planificateur, donc elle ne peut
        pas diverger de ce qui serait reellement ecrit.
        """
        clients = await self._static_clients_all()
        if not clients:
            return []
        routeurs = self.registry.collectors
        matches = {c.reference: resolve_pop(c.pop_name, routeurs) for c in clients}
        empeche = self._plan_impossible()

        plans: dict[str, Plan | str] = {}
        for match in matches.values():
            for collector in match.collectors:
                if collector.name in plans:
                    continue
                if empeche is not None:
                    plans[collector.name] = empeche
                    continue
                try:
                    plans[collector.name] = await self.plan_router(collector.name, prune=False)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Plan impossible sur %s", collector.name)
                    plans[collector.name] = f"{type(exc).__name__}: {exc}"

        etats: list[dict[str, Any]] = []
        for client in clients:
            match = matches[client.reference]
            ligne: dict[str, Any] = {
                "reference": client.reference,
                "pop_name": match.pop_name or client.pop_name,
                "pop_declared": client.pop_name,
                "pop_resolution": match.resolution,
                "enforcement_enabled": self._enforcement_enabled,
                "routers": [],
            }
            if not match.found:
                ligne["state"] = self.ETAT_SANS_ROUTEUR
                ligne["reason"] = explain_pop(match, client.pop_name, routeurs)
                ligne["router"] = None
                etats.append(ligne)
                continue
            nom_file = self.queue_name_for(client.reference)
            for collector in match.collectors:
                plan = plans.get(collector.name)
                if isinstance(plan, str) or plan is None:
                    ligne["routers"].append(
                        {
                            "router": collector.name,
                            "state": self.ETAT_ERREUR,
                            "reason": plan or "routeur non lu",
                            "applied": 0,
                            "actions": [],
                        }
                    )
                    continue
                ligne["routers"].append(self._etat_dans_le_plan(plan, client.reference, nom_file))
            principal = self._etat_principal(ligne["routers"])
            ligne["state"] = principal["state"]
            ligne["reason"] = principal["reason"]
            ligne["router"] = principal["router"]
            etats.append(ligne)
        return etats

    def _etat_dans_le_plan(self, plan: Plan, reference: str, queue_name: str) -> dict[str, Any]:
        """Lit le plan d'un routeur du point de vue d'UN client.

        Aucune action a son nom et aucun motif d'ecart : la file existe et
        correspond. C'est la seule deduction possible, et elle est exacte parce
        que le planificateur ne produit une action que sur un ECART.
        """
        ecarte = next((s for s in plan.skipped if s.login == reference), None)
        conflit = next((c for c in plan.conflicts if c.name == queue_name), None)
        actions = [a.summary() for a in plan.actions if (a.name or "") == queue_name]
        if conflit is not None:
            return {
                "router": plan.router_name,
                "state": self.ETAT_CONFLIT,
                "reason": conflit.detail,
                "applied": 0,
                "actions": actions,
            }
        if ecarte is not None:
            return {
                "router": plan.router_name,
                "state": self.ETAT_ECARTE,
                "reason": ecarte.reason,
                "applied": 0,
                "actions": actions,
            }
        if actions:
            return {
                "router": plan.router_name,
                "state": self.ETAT_A_POSER,
                "reason": (
                    "file a poser : elle sera ecrite a la prochaine reconciliation"
                    if self._enforcement_enabled
                    else "l'enforcement est desactive : rien ne sera ecrit tant qu'il ne "
                    "sera pas actif (onglet Shaping)"
                ),
                "applied": 0,
                "actions": actions,
            }
        return {
            "router": plan.router_name,
            "state": self.ETAT_POSEE,
            "reason": "file posee et conforme sur le routeur",
            "applied": 0,
            "actions": [],
        }

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
            resultat["at"] = datetime.now(tz=UTC).isoformat()
            self.last_reconcile = resultat
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
        # Garder la trace du passage, meme vide : "rien a faire" est une reponse,
        # et c'est celle que l'exploitant doit lire quand tout est deja en place.
        resultat["at"] = datetime.now(tz=UTC).isoformat()
        self.last_reconcile = resultat
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
        sans_secteur: list[str] = []
        for note in notes:
            secteur = rattachements.get(str(note["login"]))
            if secteur is None:
                # Abonne dont on ignore le secteur : il ne peut incriminer
                # personne. On ne devine pas un rattachement.
                sans_secteur.append(str(note["login"]))
                continue
            par_secteur.setdefault(secteur, []).append(note)

        # NE PAS SE TAIRE QUAND ON NE PEUT PAS CONCLURE.
        #
        # Une boucle qui rend {scored: 12, sectors: [], errors: []} est
        # indiscernable d'un reseau en pleine sante : c'est exactement le cas ou
        # tous les abonnes sont notes mais aucun n'est rattache a un secteur, et
        # la boucle ne peut alors RIEN faire. Le dire coute une ligne et evite de
        # chercher la panne ailleurs.
        resultat["unattached"] = sans_secteur
        if sans_secteur and not par_secteur:
            resultat["errors"].append(
                f"{len(sans_secteur)} abonne(s) notes mais aucun rattache a un secteur : "
                f"la boucle n'a aucun secteur a evaluer. Le rattachement vient de la "
                f"jointure caller-id <-> station UISP a la decouverte, ou du champ "
                f"'secteur' de la fiche pour un client a IP fixe."
            )
        elif sans_secteur:
            resultat["errors"].append(
                f"{len(sans_secteur)} abonne(s) notes sans secteur connu : ils ne comptent "
                f"dans l'evaluation d'aucun secteur ({', '.join(sorted(sans_secteur)[:5])}"
                f"{', ...' if len(sans_secteur) > 5 else ''})."
            )

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
        # Rapprochement TOLERANT, le meme que pour l'inventaire statique.
        #
        # Une egalite de chaine faisait de "francophonie" (saisi a la main) et
        # "PoP Francophonie" (porte par le routeur) deux sites differents :
        # poser un plafond sur cet abonne repondait alors "aucun routeur ne le
        # porte", sans que rien ne dise pourquoi.
        routeurs = self.registry.collectors
        retenus: list[str] = []
        for pop in pops:
            for collector in resolve_pop(str(pop), routeurs).collectors:
                if collector.name not in retenus:
                    retenus.append(collector.name)
        return retenus

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


def _index_sans_ambiguite(
    candidats: dict[str, set[str]], quoi: str, snapshot: TopologySnapshot
) -> dict[str, str]:
    """``indice -> routeur``, en ECARTANT tout indice revendique par plusieurs.

    Ces index servent a reconnaitre un routeur gere quand un voisin l'annonce.
    Un indice partage par deux routeurs n'identifie plus personne : le retenir
    reviendrait a rattacher les voisins de l'un a l'autre, silencieusement.

    Et les collisions arrivent pour de bon. Des CHR deployees depuis la meme
    image partagent les MAC de leurs interfaces ; les configurations modeles
    donnent le meme /30 de liaison a tous les sites ; deux equipements peuvent
    porter la meme identite RouterOS. Chaque cas est signale, parce qu'il
    explique a lui seul qu'une partie du reseau n'apparaisse pas comme prevu.
    """
    index: dict[str, str] = {}
    for indice, proprietaires in sorted(candidats.items()):
        if len(proprietaires) == 1:
            index[indice] = next(iter(proprietaires))
            continue
        noms = sorted(cle.removeprefix("router:") for cle in proprietaires)
        snapshot.warnings.append(
            f"{quoi} {indice} revendiquee par {len(noms)} routeurs ({', '.join(noms)}) : "
            f"elle n'identifie plus aucun d'eux. Leurs voisins restent des cases a part. "
            f"Des equipements clones depuis la meme image donnent ce symptome."
        )
        logger.warning("%s %s partagee par %s", quoi, indice, noms)
    return index


def _identites_physiques(
    *, device_id: Any = None, mac: Any = None, node_key: Any = None
) -> list[str]:
    """Toutes les facons de designer le MEME equipement, normalisees.

    Un backhaul et le bout distant d'un lien peuvent se reconnaitre par trois
    biais : l'identifiant UISP, la MAC, ou la cle de noeud qui derive de l'un
    des deux (``mac:AA:BB:..`` / ``uisp:<id>``). Un exploitant sans UISP met
    d'ailleurs la MAC dans ``uisp_device_id`` -- le README le recommande -- donc
    les deux champs doivent etre essayes l'un comme l'autre, et compares apres
    normalisation : ``dc:9f:db:11:22:33`` et ``DC-9F-DB-11-22-33`` sont la meme
    radio.
    """
    identites: list[str] = []

    def ajouter(valeur: str) -> None:
        if valeur and valeur not in identites:
            identites.append(valeur)

    for brut in (device_id, mac, node_key):
        texte = str(brut or "").strip()
        if not texte:
            continue
        # Une cle de noeud porte son prefixe : on compare la partie utile.
        nu = texte.split(":", 1)[1] if texte.startswith(("mac:", "uisp:")) else texte
        normalisee = normalize_mac(nu)
        if normalisee:
            ajouter(f"mac:{normalisee}")
        else:
            ajouter(f"id:{nu.casefold()}")
    return identites


def _identites_du_bout_distant(lien: dict[str, Any]) -> list[str]:
    """Identites physiques de l'equipement a l'autre bout d'un lien."""
    return _identites_physiques(
        device_id=lien.get("target_uisp_device_id"),
        mac=lien.get("target_mac"),
        node_key=lien.get("target_key"),
    )


def _identites_du_backhaul(backhaul: dict[str, Any]) -> list[str]:
    """Identites physiques d'un backhaul de l'inventaire."""
    return _identites_physiques(device_id=backhaul.get("uisp_device_id"))


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


def _origine_du_debit(kind: str, detail: dict[str, Any]) -> str:
    """D'ou vient le plafond de ce point. C'est la question qui suit "combien".

    Un exploitant qui voit 80 Mbps doit savoir s'il regarde une capacite radio
    mesuree, un plafond qu'il a lui-meme saisi, ou un resserrage decide par la
    boucle QoE -- les trois se corrigent a des endroits differents.
    """
    if detail.get("override"):
        origine = "surcharge saisie a la main"
    elif kind == ShapingService.POINT_LIEN:
        origine = (
            "capacite mesuree du lien"
            if detail.get("capacity_mbps")
            else "aucune capacite connue : file illimitee, prete a recevoir un plafond"
        )
    elif detail.get("boost"):
        origine = "boost en cours"
    elif detail.get("plan_down_mbps") or detail.get("plan_up_mbps"):
        origine = "plan souscrit"
    else:
        origine = "aucun debit connu"
    trim = detail.get("trim_factor")
    if isinstance(trim, (int, float)) and trim < 1.0:
        origine += f" - resserre a {round(float(trim) * 100)} % par la boucle QoE"
    return origine


def _en_arbre(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Range chaque abonne sous le lien qu'il traverse.

    C'est la hierarchie que RouterOS applique : la rendre a plat obligerait a
    relire les noms de files pour la reconstruire, ce qui est exactement le
    travail qu'on veut eviter a l'exploitant. Un point dont le parent est
    inconnu reste a la racine -- il partage alors la capacite du PoP entier, et
    le voir a la racine le dit.
    """
    par_nom = {p["name"]: p for p in points if p.get("name")}
    racines: list[dict[str, Any]] = []
    for point in points:
        parent = par_nom.get(str(point.get("parent") or ""))
        if parent is not None and parent is not point:
            parent["children"].append(point)
        else:
            racines.append(point)
    ordre = {
        ShapingService.ETAT_CONFLIT: 0,
        ShapingService.ETAT_ECARTE: 1,
        ShapingService.ETAT_A_POSER: 2,
        ShapingService.ETAT_POSEE: 3,
        ShapingService.ETAT_MANUELLE: 4,
    }

    def trier(liste: list[dict[str, Any]]) -> None:
        liste.sort(key=lambda p: (p["kind"] != ShapingService.POINT_LIEN, str(p["label"]).lower()))
        for point in liste:
            point["children"].sort(
                key=lambda p: (ordre.get(p["state"], 9), str(p["label"]).lower())
            )

    trier(racines)
    return racines


def _compter(points: list[dict[str, Any]]) -> dict[str, int]:
    """Le decompte par etat : ce qui bride, et ce qui ne bride pas encore."""
    compte: dict[str, int] = {"total": len(points)}
    for point in points:
        compte[str(point["state"])] = compte.get(str(point["state"]), 0) + 1
        if point["kind"] == ShapingService.POINT_LIEN:
            compte["liens"] = compte.get("liens", 0) + 1
        else:
            compte["abonnes"] = compte.get("abonnes", 0) + 1
    return compte


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

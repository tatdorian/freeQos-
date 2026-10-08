"""Configurer l'export NetFlow sur les routeurs, sans y aller a la main.

POURQUOI LE CONTROLEUR LE FAIT LUI-MEME
---------------------------------------
Le collecteur ecoute. Tant qu'aucun routeur n'exporte, il n'a rien a montrer --
et le message "aucun datagramme recu" demandait a l'exploitant d'aller taper
deux commandes sur CHAQUE routeur. Sur un parc de trente PoPs, cela veut dire
soixante commandes, et un PoP oublie ne se signale jamais : ses abonnes
apparaissent simplement comme s'ils ne consommaient rien.

Le controleur a deja un acces en ecriture gouverne, trace et reversible. Il
pose donc l'export lui-meme :

    /ip/traffic-flow set enabled=yes interfaces=all
    /ip/traffic-flow/target add dst-address=<collecteur> port=2055 version=9

L'ADRESSE DU COLLECTEUR N'EST PAS DEVINEE. Elle est celle que le systeme
utiliserait pour joindre CE routeur : on ouvre une socket UDP vers lui et on lit
l'adresse locale choisie par la table de routage. Aucun paquet n'est emis. Sur
un controleur multi-interfaces, c'est la seule reponse correcte -- une adresse
saisie a la main serait fausse pour la moitie du parc, et le trafic partirait
dans le vide.

MEMES GARDE-FOUS QUE LE RESTE DE L'ECRITURE
--------------------------------------------
Le plan est calcule a part et affichable ; l'ecriture passe par
``ShapingService.apply``, donc par ``ENFORCEMENT_ENABLED``, le coupe-circuit et
l'audit. Une cible deja posee vers un AUTRE collecteur n'est jamais touchee :
un exploitant qui envoie deja ses flux a un outil tiers doit pouvoir continuer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterRole
from app.db.flows_repo import NetflowExportersRepository
from app.enforcement.models import (
    MANAGED_COMMENT,
    Plan,
    PlanAction,
    is_ours,
    managed_comment,
    needs_claim,
)
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService

logger = logging.getLogger(__name__)

JOB_NETFLOW_EXPORT = "netflow_export"

PATH_FLOW = "/ip/traffic-flow"
PATH_TARGET = "/ip/traffic-flow/target"
PATH_IPFIX = "/ip/traffic-flow/ipfix"

#: Champs d'export qui portent l'adresse et le port APRES traduction. Sans eux,
#: une sortie internet qui masque ses clients exporte le trafic DESCENDANT vers
#: son adresse publique : il n'est rattache a personne, et seul le montant est
#: mesure. RouterOS les range dans /ip/traffic-flow/ipfix (v9 et IPFIX).
CHAMPS_NAT: tuple[str, ...] = ("nat-src-address", "nat-dst-address", "nat-src-port", "nat-dst-port")


#: Commentaire pose sur les cibles traffic-flow ecrites par freeQoS. C'est lui
#: qui permet de retirer une cible devenue perimee sans toucher a celle qu'un
#: exploitant a posee vers un autre outil.
def commentaire_cible() -> str:
    """Commentaire de nos cibles, avec l'identifiant de cette instance."""
    return managed_comment("netflow")


COMMENTAIRE_CIBLE = f"{MANAGED_COMMENT} netflow"

ETAT_POSE = "pose"
ETAT_A_POSER = "a poser"
ETAT_ERREUR = "erreur"


def dans_kubernetes() -> bool:
    """Vrai sous Kubernetes (containerd, CRI-O...), ou ``/.dockerenv`` n'existe pas."""
    return (
        bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        or Path("/var/run/secrets/kubernetes.io").exists()
    )


def dans_un_conteneur() -> bool:
    """Vrai dans un conteneur : Docker, Podman, ou un pod Kubernetes.

    CONSTATE : sous Kubernetes, seul ``/.dockerenv`` etait teste ; freeQoS se
    croyait sur l'hote et annoncait l'IP du POD (10.42.3.231, puis .232 au
    redeploiement), injoignable depuis les routeurs.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists() or dans_kubernetes()


def local_address_for(host: str, port: int = 8728) -> str | None:
    """L'adresse locale que le systeme utiliserait pour joindre ``host``.

    ``connect`` sur une socket UDP n'emet RIEN : il ne fait que demander au
    noyau quelle route il prendrait, et donc quelle adresse source. C'est
    exactement la question posee -- "par ou ce routeur me voit-il" -- et la
    seule facon d'y repondre sur un controleur qui a plusieurs interfaces.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sonde:
            sonde.connect((host, port))
            adresse = str(sonde.getsockname()[0])
    except OSError as exc:
        logger.debug("Adresse locale indeterminable vers %s : %s", host, exc)
        return None
    if adresse in {"0.0.0.0", "127.0.0.1"}:  # noqa: S104 - valeurs inutilisables telles quelles
        return None
    return adresse


def _loopback_of(collector: Any) -> str | None:
    loopback = getattr(collector, "loopback", None)
    return str(loopback) if loopback and _is_ip(str(loopback)) else None


def source_for(collector: Any) -> str | None:
    """L'adresse d'ou partent les flux : le LOOPBACK du routeur.

    C'est aussi celle sous laquelle l'exporteur est reconnu. Le loopback ne
    depend pas du chemin, la ou l'adresse d'interface (une VLAN) change avec
    lui. A defaut de loopback connu, l'adresse de gestion, comme avant.
    """
    loopback = _loopback_of(collector)
    if loopback:
        return loopback
    host = str(collector.config.host)
    return host if _is_ip(host) else None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _vrai(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def _duree_en_s(value: Any) -> float | None:
    """Une duree RouterOS ('1m', '30s', '00:01:00') en secondes.

    RouterOS relit une duree dans SA forme, pas dans celle qu'on a ecrite :
    ``1m`` peut revenir en ``00:01:00``. Comparer les chaines ferait voir un
    ecart a chaque passage, donc une reecriture perpetuelle du meme reglage.
    """
    texte = str(value or "").strip().lower()
    if not texte:
        return None
    if ":" in texte:
        morceaux = texte.split(":")
        try:
            valeurs = [float(m) for m in morceaux]
        except ValueError:
            return None
        total = 0.0
        for valeur in valeurs:
            total = total * 60 + valeur
        return total
    unites = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = 0.0
    nombre = ""
    for caractere in texte:
        if caractere.isdigit() or caractere == ".":
            nombre += caractere
        elif caractere in unites and nombre:
            total += float(nombre) * unites[caractere]
            nombre = ""
        else:
            return None
    if nombre:
        total += float(nombre)
    return total


def _meme_duree(actuel: Any, voulu: Any) -> bool:
    gauche = _duree_en_s(actuel)
    droite = _duree_en_s(voulu)
    if gauche is None or droite is None:
        return str(actuel or "") == str(voulu or "")
    return gauche == droite


@dataclass
class RouterExportState:
    """Ce qu'un routeur exporte aujourd'hui, et ce qu'il lui manque."""

    router: str
    host: str
    collector: str | None = None
    enabled: bool = False
    interfaces: str = ""
    active_timeout: str = ""
    inactive_timeout: str = ""
    targets: list[dict[str, Any]] = field(default_factory=list)
    ours: dict[str, Any] | None = None
    #: Cibles de freeQoS qui visent une adresse qui n'est plus la sienne.
    stale: list[dict[str, Any]] = field(default_factory=list)
    #: Champs NAT que le routeur n'exporte pas (lus a "no"). Vide quand ils y
    #: sont, ou quand le routeur ne sait pas les regler (lecture refusee).
    nat_fields_missing: list[str] = field(default_factory=list)
    state: str = ETAT_A_POSER
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "router": self.router,
            "host": self.host,
            "collector": self.collector,
            "enabled": self.enabled,
            "interfaces": self.interfaces,
            "active_timeout": self.active_timeout,
            "inactive_timeout": self.inactive_timeout,
            "targets": self.targets,
            "nat_fields_missing": self.nat_fields_missing,
            "configured": self.ours is not None and self.enabled,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass
class NetflowExportService:
    shaping: ShapingService
    registry: RouterRegistry
    exporters_repo: NetflowExportersRepository | None = None
    port: int = 2055
    version: int = 9
    interfaces: str = "all"
    #: Delai au bout duquel un flux ENCORE ACTIF est quand meme exporte.
    #:
    #: Le defaut RouterOS est de TRENTE MINUTES. Une session de streaming, un
    #: telechargement, une visio : rien de tout cela n'apparait avant une
    #: demi-heure, alors que c'est precisement ce qu'on veut voir en direct.
    active_flow_timeout: str = "1m"
    #: Delai au bout duquel un flux TERMINE est exporte. Le defaut (15 s) est
    #: deja correct ; on l'ecrit quand meme pour que la valeur soit connue
    #: plutot que subie -- c'est ce delai qui decide en combien de temps un ping
    #: apparait dans l'interface.
    inactive_flow_timeout: str = "15s"
    #: Adresse annoncee aux routeurs. Vide = deduite par routeur (le cas normal).
    collector_address: str | None = None
    enabled: bool = True
    last_run: dict[str, Any] = field(default_factory=dict)
    #: Routeurs qui refusent le commentaire sur une cible traffic-flow : on y
    #: pose la cible sans marque plutot que de ne plus exporter du tout.
    sans_commentaire: set[str] = field(default_factory=set)

    # ---------------------------------------------------------------- lecture
    def collector_for(self, collector: MikrotikCollector) -> str | None:
        if self.collector_address:
            return self.collector_address
        return local_address_for(collector.config.host, collector.config.port)

    async def resolve_collector(self, collector: MikrotikCollector) -> str | None:
        """L'adresse a laquelle CE routeur doit envoyer ses flux.

        DANS DOCKER, l'adresse locale est celle du conteneur (172.x) : le routeur
        ne peut pas l'atteindre. Le routeur, lui, sait d'ou vient notre session
        API (/user/active) : c'est l'adresse de l'hote telle qu'il la voit, et
        le port NetFlow y est publie. Aucune saisie : le deploiement reste une
        seule commande.
        """
        if self.collector_address:
            return self.collector_address
        locale = local_address_for(collector.config.host, collector.config.port)
        if dans_un_conteneur():
            vue = await self._adresse_vue_par(collector)
            if vue:
                return vue
            # DANS UN CONTENEUR, l'adresse locale est celle du conteneur ou du
            # pod : le routeur ne peut pas l'atteindre, et elle change a chaque
            # redeploiement. Mieux vaut ne rien poser et le dire que poser une
            # cible morte (cf. ``state_of`` : NETFLOW_COLLECTOR_ADDRESS).
            return None
        return locale

    async def _adresse_vue_par(self, collector: MikrotikCollector) -> str | None:
        client = getattr(collector, "_client", None)
        lire = getattr(client, "active_users", None)
        if lire is None:
            return None
        try:
            lignes = await asyncio.wait_for(asyncio.to_thread(lire), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - l'adresse locale reste le repli
            logger.info("Sessions actives illisibles sur %s : %s", collector.name, exc)
            return None
        utilisateur = collector.config.username
        candidates = [
            str(ligne.get("address") or "").strip()
            for ligne in lignes or []
            if str(ligne.get("via") or "").startswith("api")
            and _is_ip(str(ligne.get("address") or "").strip())
        ]
        miennes = [
            str(ligne.get("address") or "").strip()
            for ligne in lignes or []
            if ligne.get("name") == utilisateur
            and str(ligne.get("via") or "").startswith("api")
            and _is_ip(str(ligne.get("address") or "").strip())
        ]
        choix = miennes or candidates
        return choix[0] if choix else None

    async def _read(
        self, collector: MikrotikCollector
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        client = collector._client  # noqa: SLF001
        timeout = max(collector.config.timeout_s * 3, 8.0)

        def lire() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return client.traffic_flow(), client.traffic_flow_targets()

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    async def _nat_fields_missing(self, collector: MikrotikCollector) -> list[str]:
        """Les champs NAT que ce routeur laisse hors de ses enregistrements.

        Lecture FACULTATIVE : un RouterOS trop ancien n'a pas ce menu, et ce
        n'est pas une raison de ne plus poser l'export. On ne reclame que ce qui
        est lu explicitement a "no" -- un champ absent n'est pas un champ coupe.
        """
        client = collector._client  # noqa: SLF001
        lire = getattr(client, "traffic_flow_ipfix", None)
        if lire is None:
            return []
        timeout = max(collector.config.timeout_s * 3, 8.0)
        try:
            reglage = await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - champ facultatif
            logger.debug("%s : /ip/traffic-flow/ipfix illisible (%s)", collector.name, exc)
            return []
        return [c for c in CHAMPS_NAT if c in reglage and not _vrai(reglage.get(c))]

    def is_ours(self, collector: MikrotikCollector, cible: dict[str, Any]) -> bool:
        """Cette cible a-t-elle ete posee par freeQoS ?

        Oui si elle porte notre commentaire. Les cibles posees AVANT ce marquage
        sont reconnues a leur signature exacte : notre port, notre version, et
        l'adresse source que freeQoS impose (le loopback du routeur). Une cible
        vers un autre outil, posee par l'exploitant, n'a pas cette signature et
        n'est jamais touchee.
        """
        if MANAGED_COMMENT in str(cible.get("comment") or ""):
            # Posee par une AUTRE instance de freeQoS : pas la notre.
            return is_ours(cible)
        source = source_for(collector)
        return bool(
            source
            and str(cible.get("src-address") or "") == source
            and str(cible.get("port") or "") == str(self.port)
            and str(cible.get("version") or "") == str(self.version)
        )

    async def state_of(self, collector: MikrotikCollector) -> RouterExportState:
        etat = RouterExportState(router=collector.name, host=collector.config.host)
        etat.collector = await self.resolve_collector(collector)
        # Les flux partent du loopback : on s'assure de le connaitre AVANT de
        # calculer la cible (lu sur le routeur s'il n'est ni declare ni decouvert).
        chercher = getattr(collector, "ensure_loopback", None)
        if chercher is not None:
            await chercher()
        try:
            reglage, cibles = await self._read(collector)
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas les autres
            etat.state = ETAT_ERREUR
            etat.reason = f"{type(exc).__name__}: {exc}"
            return etat

        etat.enabled = _vrai(reglage.get("enabled"))
        etat.interfaces = str(reglage.get("interfaces") or "")
        etat.active_timeout = str(reglage.get("active-flow-timeout") or "")
        etat.inactive_timeout = str(reglage.get("inactive-flow-timeout") or "")
        etat.targets = [
            {
                "dst_address": str(c.get("dst-address") or ""),
                "port": str(c.get("port") or ""),
                "version": str(c.get("version") or ""),
                "src_address": str(c.get("src-address") or ""),
                "freeqos": self.is_ours(collector, c),
            }
            for c in cibles
        ]
        if etat.collector is None:
            etat.state = ETAT_ERREUR
            etat.reason = (
                "collector address unknown: freeQoS runs in a container "
                f"({'Kubernetes' if dans_kubernetes() else 'Docker'}) and the router does not "
                "show where our API session comes from. Set NETFLOW_COLLECTOR_ADDRESS to the "
                "address the routers must send their flows to (the host, the Kubernetes "
                "Service/LoadBalancer, or the node with the published UDP port)."
                if dans_un_conteneur()
                else "collector address unknown from this controller: set NETFLOW_COLLECTOR_ADDRESS"
            )
            return etat

        etat.ours = next(
            (
                c
                for c in cibles
                if str(c.get("dst-address") or "") == etat.collector
                and str(c.get("port") or "") == str(self.port)
            ),
            None,
        )
        # CONSTATE : l'adresse du collecteur change avec le deploiement (IP du
        # conteneur, du pod, de la VM) et chaque changement AJOUTAIT une cible
        # -- 172.18.0.3, 100.100.101.114, 10.42.3.231, 10.42.3.232,
        # 192.168.188.23 sur un meme routeur. Les cibles de freeQoS qui ne
        # visent plus son adresse actuelle sont retirees.
        etat.stale = [c for c in cibles if c is not etat.ours and self.is_ours(collector, c)]
        for ligne, brute in zip(etat.targets, cibles, strict=True):
            ligne["stale"] = any(brute is c for c in etat.stale)
        etat.nat_fields_missing = await self._nat_fields_missing(collector)
        lent = not _meme_duree(etat.active_timeout, self.active_flow_timeout)
        if etat.enabled and etat.ours is not None and not lent and not etat.nat_fields_missing:
            etat.state = ETAT_POSE
            etat.reason = f"exports to {etat.collector}:{self.port}"
        else:
            etat.state = ETAT_A_POSER
            if not etat.enabled:
                etat.reason = "the export is off on this router"
            elif etat.ours is None:
                etat.reason = "no target points at this collector"
            elif not lent:
                etat.reason = (
                    "NAT addresses left out of the export: traffic coming back to a "
                    "masqueraded client cannot be tied to it"
                )
            else:
                # LE PIEGE LE PLUS COUTEUX DE TRAFFIC-FLOW. Avec le defaut de
                # RouterOS, un flux encore actif n'est exporte qu'au bout de
                # trente minutes : tout se passe comme si le streaming en cours
                # n'existait pas.
                etat.reason = f"active flows only exported after {etat.active_timeout or '?'}"
        return etat

    async def states(self) -> list[RouterExportState]:
        return [await self.state_of(c) for c in self.registry.collectors]

    # ------------------------------------------------------------------- plan
    def plan_for(self, collector: MikrotikCollector, etat: RouterExportState) -> Plan:
        """Les commandes qui manquent a CE routeur, et rien d'autre.

        Un routeur deja configure rend un plan vide : c'est le cas courant, et
        il ne coute qu'une lecture. Une cible qui pointe vers un AUTRE
        collecteur est laissee telle quelle -- l'exploitant a le droit d'envoyer
        ses flux a deux endroits.
        """
        plan = Plan(router_name=collector.name)
        # RIEN NE SE CALCULE A L'AVEUGLE. Sans adresse de collecteur, la cible
        # partirait dans le vide ; sans lecture reussie, l'etat par defaut ("pas
        # d'export, aucune cible") ferait croire qu'il faut tout poser -- et on
        # reecrirait un routeur dont on ne sait rien.
        if etat.collector is None or etat.state == ETAT_ERREUR:
            return plan

        voulu: dict[str, str] = {"enabled": "yes"}
        if self.interfaces:
            voulu["interfaces"] = self.interfaces
        if self.active_flow_timeout:
            voulu["active-flow-timeout"] = self.active_flow_timeout
        if self.inactive_flow_timeout:
            voulu["inactive-flow-timeout"] = self.inactive_flow_timeout

        actuel = {
            "enabled": "yes" if etat.enabled else "no",
            "interfaces": etat.interfaces,
            "active-flow-timeout": etat.active_timeout,
            "inactive-flow-timeout": etat.inactive_timeout,
        }
        # Les delais sont compares en SECONDES : RouterOS relit '1m' la ou on a
        # ecrit '60s', et comparer les chaines produirait un ecart a chaque
        # passage, donc une reecriture perpetuelle du meme reglage.
        ecarts = {
            cle: (actuel.get(cle) or None, valeur)
            for cle, valeur in voulu.items()
            if not _meme_duree(actuel.get(cle), valeur)
        }
        if ecarts:
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=PATH_FLOW,
                    fields={cle: apres for cle, (_, apres) in ecarts.items()},
                    name=f"{collector.name} : export NetFlow",
                    reason="flow export must be on, and must export without waiting",
                    changes=ecarts,
                )
            )

        if etat.nat_fields_missing:
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=PATH_IPFIX,
                    fields=dict.fromkeys(etat.nat_fields_missing, "yes"),
                    name=f"{collector.name} : champs NAT exportes",
                    reason=(
                        "downloads behind NAT reach the public address: only the "
                        "translated address ties them to the client"
                    ),
                    changes=dict.fromkeys(etat.nat_fields_missing, ("no", "yes")),
                )
            )

        if etat.ours is None:
            champs = {
                "dst-address": etat.collector,
                "port": str(self.port),
                "version": str(self.version),
            }
            # src-address FIXE L'ADRESSE D'OU LES FLUX PARTENT, donc celle sous
            # laquelle l'exporteur sera reconnu. Sans elle, RouterOS choisit
            # selon sa table de routage et l'exporteur peut apparaitre sous une
            # adresse qu'aucune declaration ne connait -- il tombe alors en
            # 'unknown', et ses octets ne sont rattaches a aucun point de mesure.
            source = source_for(collector)
            if source:
                champs["src-address"] = source
            if collector.name not in self.sans_commentaire:
                champs["comment"] = commentaire_cible()
            plan.actions.append(
                PlanAction(
                    verb="add",
                    path=PATH_TARGET,
                    fields=champs,
                    name=f"{collector.name} : cible {etat.collector}:{self.port}",
                    reason="this router does not send its flows to any known collector yet",
                )
            )
        elif (source := _loopback_of(collector)) and str(
            etat.ours.get("src-address") or ""
        ) != source:
            # Cible posee depuis une autre adresse (celle de gestion, ou celle
            # que RouterOS choisissait seul) : on la fait partir du loopback.
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=PATH_TARGET,
                    target_id=str(etat.ours.get(".id") or ""),
                    fields={"src-address": source},
                    name=f"{collector.name} : cible {etat.collector}:{self.port}",
                    reason="flows must leave from the router's loopback",
                    changes={"src-address": (str(etat.ours.get("src-address") or ""), source)},
                )
            )
        elif str(etat.ours.get("version") or "") != str(self.version):
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=PATH_TARGET,
                    target_id=str(etat.ours.get(".id") or ""),
                    fields={"version": str(self.version)},
                    name=f"{collector.name} : cible {etat.collector}:{self.port}",
                    reason="export version aligned",
                    changes={"version": (str(etat.ours.get("version") or ""), str(self.version))},
                )
            )
        elif collector.name not in self.sans_commentaire and (
            MANAGED_COMMENT not in str(etat.ours.get("comment") or "") or needs_claim(etat.ours)
        ):
            # Cible deja juste, posee avant le marquage : on la marque, pour
            # qu'elle soit reconnue comme la notre le jour ou l'adresse changera.
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=PATH_TARGET,
                    target_id=str(etat.ours.get(".id") or ""),
                    fields={"comment": commentaire_cible()},
                    name=f"{collector.name} : cible {etat.collector}:{self.port}",
                    reason="target marked as placed by freeQoS",
                    changes={"comment": (str(etat.ours.get("comment") or ""), commentaire_cible())},
                )
            )
        else:
            plan.unchanged += 1

        # Les cibles perimees partent APRES la pose de la bonne : le routeur
        # n'est jamais laisse sans export, meme si une commande echoue.
        for perimee in etat.stale:
            ident = str(perimee.get(".id") or "")
            if not ident:
                continue
            plan.actions.append(
                PlanAction(
                    verb="remove",
                    path=PATH_TARGET,
                    target_id=ident,
                    name=(
                        f"{collector.name} : ancienne cible "
                        f"{perimee.get('dst-address')}:{perimee.get('port')}"
                    ),
                    reason="placed by freeQoS toward an address that is no longer its own",
                )
            )
        return plan

    # --------------------------------------------------------------- ecriture
    async def apply_all(
        self, *, author: str, dry_run: bool = True, router_name: str | None = None
    ) -> dict[str, Any]:
        rapport: dict[str, Any] = {
            "dry_run": dry_run,
            "enforcement_enabled": self.shaping.enforcement_enabled,
            "port": self.port,
            "version": self.version,
            "routers": [],
            "applied": 0,
        }
        for collector in self.registry.collectors:
            if router_name is not None and collector.name != router_name:
                continue
            rapport["routers"].append(await self._apply_one(collector, author, dry_run))
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])
        etats = [r["state"] for r in rapport["routers"]]
        rapport["state"] = (
            ETAT_ERREUR
            if ETAT_ERREUR in etats
            else (ETAT_A_POSER if ETAT_A_POSER in etats else ETAT_POSE)
        )
        self.last_run = {
            "at": datetime.now(tz=UTC),
            "state": rapport["state"],
            "applied": rapport["applied"],
        }
        return rapport

    async def _apply_one(
        self, collector: MikrotikCollector, author: str, dry_run: bool
    ) -> dict[str, Any]:
        etat = await self.state_of(collector)
        ligne = etat.to_dict()
        ligne["applied"] = 0
        ligne["actions"] = []
        if etat.state == ETAT_ERREUR:
            return ligne

        plan = self.plan_for(collector, etat)
        ligne["actions"] = [action.command for action in plan.actions]
        if plan.is_empty:
            ligne["state"] = ETAT_POSE
            return ligne
        if dry_run or not self.shaping.enforcement_enabled:
            ligne["state"] = ETAT_A_POSER
            ligne["reason"] = (
                "writing is off: commands are computed, nothing is sent"
                if not self.shaping.enforcement_enabled
                else "simulation"
            )
            return ligne
        try:
            resultat = await self.shaping.apply(plan, dry_run=False, author=author)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Export NetFlow non configure sur %s", collector.name)
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = f"{type(exc).__name__}: {exc}"
            return ligne
        rates = [o for o in resultat.outcomes if not o.ok]
        if (
            rates
            and collector.name not in self.sans_commentaire
            and any("comment" in o.action.fields for o in rates)
        ):
            # Ce routeur refuse le commentaire sur une cible : on pose sans
            # marque plutot que de le laisser sans export.
            logger.warning(
                "%s : commentaire refuse sur la cible NetFlow (%s), cible posee sans marque",
                collector.name,
                "; ".join(str(o.detail) for o in rates if o.detail),
            )
            self.sans_commentaire.add(collector.name)
            return await self._apply_one(collector, author, dry_run)
        ligne["applied"] = resultat.applied
        if rates:
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = "; ".join(str(o.detail) for o in rates if o.detail)
            return ligne
        ligne["state"] = ETAT_POSE
        ligne["reason"] = f"{resultat.applied} command(s) applied"
        await self._declare(collector)
        return ligne

    async def _declare(self, collector: MikrotikCollector) -> None:
        """Inscrit le routeur comme exporteur, avec son point de mesure.

        SANS CETTE DECLARATION, les flux arriveraient marques 'unknown' : leurs
        octets ne seraient rattaches a aucun point de mesure, donc absents de la
        consommation. Le point se deduit du ROLE du routeur -- une passerelle
        regarde depuis la sortie internet, tout le reste depuis le PoP.
        """
        source = source_for(collector)
        if self.exporters_repo is None or not source:
            return
        vantage = "edge" if collector.config.role == RouterRole.GATEWAY else "pop"
        try:
            await self.exporters_repo.declare(
                {
                    "address": source,
                    "name": collector.name,
                    "vantage": vantage,
                    "pop_name": collector.config.effective_pop_name,
                    "sampling_rate": 1,
                    "enabled": True,
                    "note": "declared automatically when the export was configured",
                }
            )
        except Exception as exc:  # noqa: BLE001 - la configuration reste valable
            logger.warning("Exporteur %s non declare : %s", collector.name, exc)

    async def ensure(self) -> dict[str, Any]:
        """Passage periodique : pose ce qui manque, ne touche a rien d'autre.

        Un routeur ajoute apres coup, un routeur reinitialise, un export coupe a
        la main : tous reviennent d'eux-memes. C'est le seul moyen pour qu'un PoP
        oublie ne reste pas silencieux indefiniment.
        """
        if not self.enabled:
            return {"state": "desactive"}
        if not self.shaping.enforcement_enabled:
            return {"state": ETAT_A_POSER, "reason": "writing is off"}
        return await self.apply_all(author="system:netflow-export", dry_run=False)

    async def status(self) -> dict[str, Any]:
        etats = await self.states()
        return {
            "auto": self.enabled,
            "port": self.port,
            "version": self.version,
            "interfaces": self.interfaces,
            "enforcement_enabled": self.shaping.enforcement_enabled,
            "configured": sum(1 for e in etats if e.state == ETAT_POSE),
            "routers": [e.to_dict() for e in etats],
            "last_run": self.last_run or None,
        }

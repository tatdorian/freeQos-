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
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterRole
from app.db.flows_repo import NetflowExportersRepository
from app.enforcement.models import Plan, PlanAction
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService

logger = logging.getLogger(__name__)

JOB_NETFLOW_EXPORT = "netflow_export"

PATH_FLOW = "/ip/traffic-flow"
PATH_TARGET = "/ip/traffic-flow/target"

ETAT_POSE = "pose"
ETAT_A_POSER = "a poser"
ETAT_ERREUR = "erreur"


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

    # ---------------------------------------------------------------- lecture
    def collector_for(self, collector: MikrotikCollector) -> str | None:
        if self.collector_address:
            return self.collector_address
        return local_address_for(collector.config.host, collector.config.port)

    async def _read(
        self, collector: MikrotikCollector
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        client = collector._client  # noqa: SLF001
        timeout = max(collector.config.timeout_s * 3, 8.0)

        def lire() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return client.traffic_flow(), client.traffic_flow_targets()

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    async def state_of(self, collector: MikrotikCollector) -> RouterExportState:
        etat = RouterExportState(router=collector.name, host=collector.config.host)
        etat.collector = self.collector_for(collector)
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
            }
            for c in cibles
        ]
        if etat.collector is None:
            etat.state = ETAT_ERREUR
            etat.reason = (
                "adresse du collecteur indeterminable depuis ce controleur : "
                "renseignez NETFLOW_COLLECTOR_ADDRESS"
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
        lent = not _meme_duree(etat.active_timeout, self.active_flow_timeout)
        if etat.enabled and etat.ours is not None and not lent:
            etat.state = ETAT_POSE
            etat.reason = f"exporte vers {etat.collector}:{self.port}"
        else:
            etat.state = ETAT_A_POSER
            if not etat.enabled:
                etat.reason = "l'export est coupe sur ce routeur"
            elif etat.ours is None:
                etat.reason = "aucune cible ne pointe vers ce collecteur"
            else:
                # LE PIEGE LE PLUS COUTEUX DE TRAFFIC-FLOW. Avec le defaut de
                # RouterOS, un flux encore actif n'est exporte qu'au bout de
                # trente minutes : tout se passe comme si le streaming en cours
                # n'existait pas.
                etat.reason = f"flux actifs exportes seulement apres {etat.active_timeout or '?'}"
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
                    reason="l'export de flux doit etre actif, et exporter sans attendre",
                    changes=ecarts,
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
            if _is_ip(collector.config.host):
                champs["src-address"] = collector.config.host
            plan.actions.append(
                PlanAction(
                    verb="add",
                    path=PATH_TARGET,
                    fields=champs,
                    name=f"{collector.name} : cible {etat.collector}:{self.port}",
                    reason="ce routeur n'envoie encore ses flux a aucun collecteur connu",
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
                    reason="version d'export alignee",
                    changes={"version": (str(etat.ours.get("version") or ""), str(self.version))},
                )
            )
        else:
            plan.unchanged += 1
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
                "ecriture desactivee : les commandes sont calculees, rien n'est envoye"
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
        ligne["applied"] = resultat.applied
        rates = [o for o in resultat.outcomes if not o.ok]
        if rates:
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = "; ".join(str(o.detail) for o in rates if o.detail)
            return ligne
        ligne["state"] = ETAT_POSE
        ligne["reason"] = f"{resultat.applied} commande(s) appliquee(s)"
        await self._declare(collector)
        return ligne

    async def _declare(self, collector: MikrotikCollector) -> None:
        """Inscrit le routeur comme exporteur, avec son point de mesure.

        SANS CETTE DECLARATION, les flux arriveraient marques 'unknown' : leurs
        octets ne seraient rattaches a aucun point de mesure, donc absents de la
        consommation. Le point se deduit du ROLE du routeur -- une passerelle
        regarde depuis la sortie internet, tout le reste depuis le PoP.
        """
        if self.exporters_repo is None or not _is_ip(collector.config.host):
            return
        vantage = "edge" if collector.config.role == RouterRole.GATEWAY else "pop"
        try:
            await self.exporters_repo.declare(
                {
                    "address": collector.config.host,
                    "name": collector.name,
                    "vantage": vantage,
                    "pop_name": collector.config.effective_pop_name,
                    "sampling_rate": 1,
                    "enabled": True,
                    "note": "declare automatiquement a la configuration de l'export",
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
            return {"state": ETAT_A_POSER, "reason": "ecriture desactivee"}
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

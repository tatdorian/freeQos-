"""Restrictions de trafic : de la regle saisie aux routeurs, et retour.

CE SERVICE EST LA CHARNIERE. Trois choses se rencontrent ici, et nulle part
ailleurs :

  - la REGLE, telle qu'un humain l'a ecrite ("bloquer Netflix pour ces trois
    clients") ;
  - le CATALOGUE et ce que NetFlow a DECOUVERT, qui transforment "Netflix" en
    une liste d'adresses -- et qui la font changer toute seule ;
  - les ROUTEURS, ou la liste et la regle sont reellement posees.

RIEN N'EST ECRIT SANS PASSER PAR LE MEME CHEMIN QUE LES FILES. Le plan est
calcule a part (``enforcement/restrictions``), affichable avant execution, et
l'ecriture passe par ``ShapingService.apply`` : donc par le drapeau
``ENFORCEMENT_ENABLED``, par le coupe-circuit sur le nombre d'actions, et par
l'audit. Une restriction n'a aucun privilege que les files n'aient pas.

LA BOUCLE QUI REND TOUT CELA VIVANT
------------------------------------
Un job periodique recalcule les adresses de chaque regle et pousse la
DIFFERENCE. C'est la reponse a "je veux que ce soit dynamique" : quand NetFlow
voit un abonne atteindre une adresse encore inconnue, cette adresse est nommee
par l'enrichissement, puis -- si elle releve d'un service restreint -- rejoint
la liste posee sur le routeur au passage suivant. Personne ne reecrit la regle.

ET CE QUI RESTE VOLONTAIREMENT MANUEL. Aucune regle ne se cree toute seule.
Voir passer du streaming ne dit pas qu'il faut le brider ; c'est une decision
commerciale, pas une deduction technique.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.collectors.mikrotik import MikrotikCollector
from app.db.destinations_repo import DestinationsRepository
from app.db.flows_repo import FlowsRepository
from app.db.traffic_rules_repo import TrafficRulesRepository
from app.enforcement.models import Plan
from app.enforcement.restrictions import (
    ACTION_LIMIT,
    RouterRestrictionState,
    RuleTarget,
    merge_addresses,
    plan_lift,
    plan_restrictions,
)
from app.services import ipfinder
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService

logger = logging.getLogger(__name__)

JOB_RESTRICTIONS = "traffic_restrictions"

#: Etats rendus a l'interface. Les memes mots que pour les files, pour que
#: l'exploitant n'ait pas deux vocabulaires a retenir.
ETAT_POSEE = "posee"
ETAT_A_POSER = "a poser"
ETAT_ERREUR = "erreur"
ETAT_SANS_ROUTEUR = "aucun routeur"
ETAT_SANS_ADRESSE = "aucune adresse"
#: Une regle suspendue dont les lignes ont bien quitte tous les routeurs. Sans
#: cet etat, elle garderait l'affichage "posee" de sa derniere pose -- et on
#: croirait encore bloque un trafic qui passe, ou l'inverse.
ETAT_LEVEE = "levee"


class InvalidRuleError(ValueError):
    """La regle ne designe rien, ou se contredit."""


def validate(payload: dict[str, Any]) -> None:
    """Refuse une regle qui ne veut rien dire, AVANT de l'enregistrer.

    Les deux refus ci-dessous evitent le meme accident : une regle qui, faute de
    critere, viserait TOUT le trafic de TOUS les clients. Sur un routeur de
    sortie internet, l'appliquer coupe le reseau entier -- et la regle aurait
    l'air parfaitement normale dans la liste.
    """
    services = list(payload.get("services") or [])
    categories = list(payload.get("categories") or [])
    prefixes = list(payload.get("prefixes") or [])
    if not (services or categories or prefixes):
        raise InvalidRuleError(
            "une restriction doit designer du trafic : choisissez un service, une "
            "famille, ou saisissez au moins un bloc d'adresses. Sans critere, la "
            "regle viserait tout internet."
        )
    inconnus = [s for s in services if s not in ipfinder.PAR_CLE]
    if inconnus:
        raise InvalidRuleError(f"service(s) inconnu(s) du catalogue : {', '.join(inconnus)}")
    hors_liste = [c for c in categories if c not in ipfinder.CATEGORIES]
    if hors_liste:
        raise InvalidRuleError(f"famille(s) inconnue(s) : {', '.join(hors_liste)}")
    if payload.get("action") == ACTION_LIMIT and not (
        payload.get("limit_down_mbps") or payload.get("limit_up_mbps")
    ):
        raise InvalidRuleError(
            "un plafond sans debit ne plafonne rien : saisissez un debit "
            "descendant, montant, ou les deux."
        )
    if str(payload.get("scope") or "all") == "subscribers" and not payload.get("logins"):
        raise InvalidRuleError(
            "portee 'abonnes choisis' sans aucun abonne : la regle ne viserait personne."
        )


@dataclass
class RestrictionService:
    shaping: ShapingService
    registry: RouterRegistry
    rules_repo: TrafficRulesRepository | None = None
    destinations: DestinationsRepository | None = None
    flows_repo: FlowsRepository | None = None
    #: Plafond d'adresses par regle. Une liste que le routeur parcourt a chaque
    #: paquet ne doit pas grossir sans limite parce qu'un service a beaucoup de
    #: serveurs.
    address_limit: int = 5_000
    last_run: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- lecture
    async def resolve(self, rule: dict[str, Any]) -> RuleTarget:
        """Transforme une regle saisie en adresses concretes.

        C'EST ICI QUE LA REGLE DEVIENT VIVANTE. Les blocs publies du catalogue
        donnent la base ; les adresses que NetFlow a rattachees au meme service
        viennent la completer. Une adresse deja couverte par un bloc n'est pas
        ajoutee : elle ne changerait rien et ferait grossir la liste.
        """
        services = {str(s) for s in rule.get("services") or []}
        categories = {str(c) for c in rule.get("categories") or []}
        # Choisir une famille revient a choisir tous ses services : sinon une
        # regle "streaming" ne poserait que les adresses deja vues, et manquerait
        # les blocs publies de Netflix ou Twitch.
        services |= ipfinder.services_in_categories(categories)

        base = list(rule.get("prefixes") or []) + ipfinder.service_prefixes(services)
        decouvertes: list[str] = []
        if self.destinations is not None and (services or categories):
            try:
                decouvertes = await self.destinations.addresses_for(
                    services=services, categories=categories, limit=self.address_limit
                )
            except Exception as exc:  # noqa: BLE001 - une base muette ne vide pas la regle
                logger.warning("Adresses decouvertes non relues pour '%s' : %s", rule["name"], exc)

        clients: list[str] = []
        if str(rule.get("scope") or "all") == "subscribers":
            logins = [str(login) for login in rule.get("logins") or []]
            if self.flows_repo is not None and logins:
                par_login = await self.flows_repo.prefixes_for_logins(logins)
                for prefixes in par_login.values():
                    clients.extend(prefixes)

        return RuleTarget(
            rule_id=int(rule["id"]),
            name=str(rule["name"]),
            action=str(rule.get("action") or "block"),
            limit_down_mbps=rule.get("limit_down_mbps"),
            limit_up_mbps=rule.get("limit_up_mbps"),
            destinations=tuple(merge_addresses(base, decouvertes, limit=self.address_limit)),
            clients=tuple(clients),
            protocol=rule.get("protocol") or None,
            ports=rule.get("ports") or None,
        )

    async def targets(self) -> list[RuleTarget]:
        """Toutes les regles ACTIVES, resolues.

        Une regle desactivee n'est pas resolue : elle n'apparait donc dans aucun
        etat desire, et la reconciliation retire d'elle-meme ce qu'elle avait
        pose. Desactiver une restriction la leve reellement, sans avoir a la
        supprimer.
        """
        if self.rules_repo is None:
            return []
        regles = await self.rules_repo.list_all(enabled_only=True)
        return [await self.resolve(regle) for regle in regles]

    def routers_for(self, rule: dict[str, Any]) -> list[str]:
        """Les routeurs concernes par une regle.

        Par defaut TOUS ceux de l'inventaire actif : une restriction dont on ne
        dit rien doit tenir partout, sinon elle tient selon le chemin qu'un
        paquet emprunte -- c'est-a-dire au hasard.
        """
        connus = [collector.name for collector in self.registry.collectors]
        demandes = [str(r) for r in rule.get("routers") or []]
        if not demandes:
            return connus
        return [nom for nom in connus if nom in demandes]

    async def _read_state(self, collector: MikrotikCollector) -> RouterRestrictionState:
        """Lit les quatre tables concernees sur un routeur, en une fois.

        Une lecture qui echoue n'est pas rattrapee ici : sans savoir ce que le
        routeur porte deja, tout plan calcule serait une reecriture complete --
        exactement le genre de plan qu'il ne faut jamais appliquer.
        """
        client = collector._client  # noqa: SLF001
        timeout = max(collector.config.timeout_s * 4, 10.0)

        def lire() -> RouterRestrictionState:
            return RouterRestrictionState(
                address_list=client.firewall_address_list(),
                filters=client.firewall_filters(),
                mangle=client.firewall_mangle(),
                queue_trees=client.queue_trees(),
            )

        return await asyncio.wait_for(asyncio.to_thread(lire), timeout=timeout)

    async def plan_router(self, router_name: str, targets: list[RuleTarget] | None = None) -> Plan:
        cibles = targets if targets is not None else await self.targets()
        collector = self._collector(router_name)
        etat = await self._read_state(collector)
        return plan_restrictions(router_name, cibles, etat, address_limit=self.address_limit)

    def _collector(self, router_name: str) -> MikrotikCollector:
        for collector in self.registry.collectors:
            if collector.name == router_name:
                return collector
        raise KeyError(f"routeur '{router_name}' absent de l'inventaire actif")

    # --------------------------------------------------------------- ecriture
    async def apply_all(
        self, *, author: str, dry_run: bool = True, router_name: str | None = None
    ) -> dict[str, Any]:
        """Calcule et (eventuellement) applique le plan sur chaque routeur vise.

        ``dry_run`` reste le defaut : montrer d'abord, ecrire ensuite. Un echec
        sur un routeur n'empeche pas les autres -- un PoP injoignable ne doit pas
        laisser une restriction a moitie posee ailleurs sans qu'on le sache.
        """
        regles = await self.rules_repo.list_all() if self.rules_repo is not None else []
        actives = [r for r in regles if r.get("enabled")]
        cibles = [await self.resolve(regle) for regle in actives]

        # TOUS LES ROUTEURS SONT VISITES, ET C'EST INDISPENSABLE. Un routeur que
        # plus aucune regle ne vise peut porter les lignes d'une regle supprimee,
        # desactivee, ou dont la liste de routeurs vient d'etre reduite. Ne pas y
        # passer laisserait ce trafic bloque pour toujours, sans plus rien dans
        # l'interface qui l'explique. Avec une liste de cibles VIDE, le plan de
        # ce routeur ne contient que des retraits -- exactement ce qu'il faut.
        vises = [collector.name for collector in self.registry.collectors]
        if router_name is not None:
            vises = [router_name]

        # Chaque routeur ne recoit que les regles qui le visent : une regle
        # epinglee sur le PoP Nord ne doit pas apparaitre sur le PoP Sud.
        par_routeur: dict[str, list[RuleTarget]] = {nom: [] for nom in vises}
        for regle, cible in zip(actives, cibles, strict=True):
            for nom in self.routers_for(regle):
                if nom in par_routeur:
                    par_routeur[nom].append(cible)

        rapport: dict[str, Any] = {
            "dry_run": dry_run,
            "enforcement_enabled": self.shaping.enforcement_enabled,
            "rules": len(actives),
            "routers": [],
            "applied": 0,
        }
        for nom in sorted(vises):
            rapport["routers"].append(await self._apply_one(nom, par_routeur[nom], author, dry_run))
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])

        detail = (
            "; ".join(f"{r['router']}: {r['state']}" for r in rapport["routers"]) or "aucun routeur"
        )
        etat = self._etat_global(rapport["routers"])
        rapport["state"] = etat
        if self.rules_repo is not None and not dry_run:
            for regle in actives:
                await self.rules_repo.record_apply(int(regle["id"]), state=etat, detail=detail)
            if etat == ETAT_POSEE and router_name is None:
                # Tous les routeurs sont conformes : une regle suspendue n'y a
                # donc plus rien. Elle cesse de s'afficher comme posee.
                for regle in regles:
                    if not regle.get("enabled") and regle.get("last_state") not in (
                        None,
                        ETAT_LEVEE,
                    ):
                        await self.rules_repo.record_apply(
                            int(regle["id"]), state=ETAT_LEVEE, detail=detail
                        )
        self.last_run = {
            "at": datetime.now(tz=UTC),
            "state": etat,
            "rules": len(actives),
            "applied": rapport["applied"],
        }
        return rapport

    async def _apply_one(
        self,
        router_name: str,
        cibles: list[RuleTarget],
        author: str,
        dry_run: bool,
        *,
        lift_rule_id: int | None = None,
    ) -> dict[str, Any]:
        ligne: dict[str, Any] = {
            "router": router_name,
            "applied": 0,
            "state": ETAT_POSEE,
            "reason": "restrictions deja conformes sur ce routeur",
            "actions": [],
        }
        try:
            if lift_rule_id is None:
                plan = await self.plan_router(router_name, cibles)
            else:
                etat = await self._read_state(self._collector(router_name))
                plan = plan_lift(router_name, lift_rule_id, etat)
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas les autres
            logger.warning("Plan de restriction impossible sur %s : %s", router_name, exc)
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = f"{type(exc).__name__}: {exc}"
            return ligne

        ligne["actions"] = [action.summary() for action in plan.actions]
        ligne["counts"] = plan.counts()
        ligne["skipped"] = [{"rule": s.login, "reason": s.reason} for s in plan.skipped]
        ligne["conflicts"] = [{"rule": c.name, "detail": c.detail} for c in plan.conflicts]
        if plan.is_empty:
            return ligne
        if dry_run or not self.shaping.enforcement_enabled:
            ligne["state"] = ETAT_A_POSER
            ligne["reason"] = (
                "l'enforcement est desactive : les commandes sont calculees, rien "
                "n'est ecrit tant qu'il ne sera pas actif (Reglages > Shaping et ecriture)"
                if not self.shaping.enforcement_enabled
                else "simulation : rien n'a ete ecrit"
            )
            return ligne
        try:
            resultat = await self.shaping.apply(plan, dry_run=False, author=author)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ecriture des restrictions impossible sur %s", router_name)
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = f"{type(exc).__name__}: {exc}"
            return ligne
        ligne["applied"] = resultat.applied
        rates = [o for o in resultat.outcomes if not o.ok]
        if rates or resultat.aborted_reason:
            # Un plan refuse par le coupe-circuit n'a RIEN ecrit : le montrer
            # "posee" faisait croire a une restriction posee -- ou levee -- qui
            # ne l'etait pas, et rien ne disait pourquoi.
            ligne["state"] = ETAT_ERREUR
            ligne["reason"] = "; ".join(
                [str(o.detail) for o in rates if o.detail]
                + ([resultat.aborted_reason] if resultat.aborted_reason else [])
            )
            return ligne
        ligne["reason"] = f"{resultat.applied} commande(s) appliquee(s)"
        return ligne

    async def lift(self, rule_id: int, *, author: str) -> dict[str, Any]:
        """Retire TOUT DE SUITE ce qu'une regle a pose, sur chaque routeur.

        Appele quand l'exploitant suspend ou supprime une restriction. Avant,
        rien n'etait retire avant la reconciliation suivante -- plusieurs
        minutes pendant lesquelles l'adresse restait bloquee, et pour toujours
        si le plan complet d'un routeur echouait sur une AUTRE regle avant
        d'atteindre ses retraits (le plan s'arrete a la premiere erreur).

        Seules des SUPPRESSIONS de lignes portant la marque de cette regle sont
        envoyees : lever une restriction ne pose rien et ne touche a aucune
        autre. Le drapeau d'ecriture reste le dernier mot, comme partout.
        """
        vises = [collector.name for collector in self.registry.collectors]
        rapport: dict[str, Any] = {
            "rule_id": rule_id,
            "enforcement_enabled": self.shaping.enforcement_enabled,
            "routers": [],
            "applied": 0,
        }
        if not self.shaping.enforcement_enabled:
            rapport["state"] = ETAT_A_POSER
            rapport["reason"] = (
                "l'enforcement est desactive : rien n'est retire des routeurs tant "
                "qu'il ne sera pas actif (Reglages > Shaping et ecriture)"
            )
            return rapport
        for nom in sorted(vises):
            rapport["routers"].append(
                await self._apply_one(nom, [], author, False, lift_rule_id=rule_id)
            )
        rapport["applied"] = sum(int(r["applied"]) for r in rapport["routers"])
        etat = self._etat_global(rapport["routers"])
        rapport["state"] = ETAT_LEVEE if etat == ETAT_POSEE else etat
        return rapport

    @staticmethod
    def _etat_global(lignes: list[dict[str, Any]]) -> str:
        """Le plus urgent gagne : une restriction en erreur quelque part doit se
        voir, pas se noyer dans une majorite de routeurs conformes."""
        if not lignes:
            return ETAT_SANS_ROUTEUR
        for etat in (ETAT_ERREUR, ETAT_A_POSER):
            if any(ligne["state"] == etat for ligne in lignes):
                return etat
        return ETAT_POSEE

    async def reconcile(self) -> dict[str, Any]:
        """Passage periodique : pousse ce qui a change, et RIEN d'autre.

        Le plan est vide quand rien n'a bouge -- c'est le cas le plus frequent,
        et il ne coute qu'une lecture par routeur. Quand NetFlow a decouvert une
        adresse de plus, le plan contient exactement une ligne : l'ajouter.
        """
        if self.rules_repo is None:
            return {"state": "indisponible", "reason": "base non initialisee"}
        if not self.shaping.enforcement_enabled:
            # Inutile de lire quatre tables par routeur pour un plan qu'on ne
            # peut pas appliquer. L'interface, elle, calcule a la demande.
            return {"state": ETAT_A_POSER, "reason": "enforcement desactive"}
        return await self.apply_all(author="system:restrictions", dry_run=False)

    def status(self) -> dict[str, Any]:
        return {
            "enforcement_enabled": self.shaping.enforcement_enabled,
            "address_limit": self.address_limit,
            "last_run": self.last_run or None,
        }

"""La boucle qui met un nom sur les adresses que NetFlow decouvre.

POURQUOI UNE BOUCLE, ET PAS UNE RESOLUTION A LA VOLEE
-----------------------------------------------------
Nommer une adresse peut demander une requete DNS, donc une attente. Le faire
DANS la reception des datagrammes reviendrait a bloquer le collecteur sur un
resolveur lent -- et un collecteur en retard jette des datagrammes UDP que
personne ne retransmet, c'est-a-dire des octets qui manqueront a des factures.

La reception ne fait donc qu'INSCRIRE l'adresse ("je l'ai vue"). Cette boucle-ci
vient ensuite lui chercher un nom, a son rythme, par lots bornes.

CE QUI REND LA DECOUVERTE DYNAMIQUE
------------------------------------
Aucune liste a tenir a jour. Une adresse jamais vue entre dans ``ip_intel`` avec
``resolved_at`` a NULL au moment ou un client l'atteint ; la boucle pioche
exactement la-dedans, les plus recentes d'abord. Une adresse atteinte il y a dix
secondes est donc nommee au passage suivant -- et, si elle releve d'un service
restreint, la restriction la prend en compte dans la foulee.

QUATRE SOURCES, ET UNE SEULE EST GRATUITE
------------------------------------------
Le CATALOGUE repond sans rien demander a personne : il fonctionne sur une VM
coupee d'internet, et il repond instantanement. Le NOM INVERSE coute une requete
DNS. RDAP coute un appel HTTP sortant. La GEOLOCALISATION aussi, sauf si une
base locale est posee a cote.

LES DEUX DERNIERES SONT COUPEES PAR DEFAUT, et pour une raison qui n'est pas
technique : les interroger revient a ENVOYER A UN TIERS les adresses que vos
clients atteignent. C'est une information sur eux, pas sur vous. Un reseau
souverain a le droit de ne pas en vouloir, et l'activation reste donc un geste
explicite.

La geolocalisation prefere une base LOCALE (format MaxMind .mmdb) des qu'un
fichier est indique : meme reponse, aucun appel sortant, rien qui fuite. C'est
la seule forme qu'on puisse recommander sans reserve.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.db.destinations_repo import DestinationsRepository
from app.services import ipfinder
from app.services.ipfinder import ReverseDns, Verdict

logger = logging.getLogger(__name__)

JOB_INTEL = "ip_intel"


@dataclass(frozen=True)
class Enrichment:
    """Tout ce qu'un passage a appris sur une adresse.

    Les trois sources sont rendues SEPAREMENT plutot que fondues : le verdict
    dit "Netflix", le nom inverse dit pourquoi, et le registre dit chez qui.
    L'interface montre les trois, et un exploitant qui doute peut refaire le
    raisonnement.
    """

    address: str
    verdict: Verdict
    hostname: str | None = None
    registry: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntelService:
    """Enrichit les adresses vues, par lots, sans jamais bloquer la collecte."""

    destinations: DestinationsRepository | None = None
    enabled: bool = True
    rdns_enabled: bool = True
    rdap_enabled: bool = False
    rdap_url: str = "https://rdap.org/ip/"
    #: Localisation. Coupee par defaut : l'activer envoie a un tiers les
    #: adresses que vos clients atteignent.
    geoip_enabled: bool = False
    #: Service interroge quand aucune base locale n'est posee. ``{ip}`` est
    #: remplace par l'adresse.
    geoip_url: str = "https://ipapi.co/{ip}/json/"
    #: Base MaxMind locale (.mmdb). PREFEREE des qu'elle existe : meme reponse,
    #: aucun appel sortant. Demande le paquet 'geoip2'.
    geoip_db: str | None = None
    #: Adresses traitees par passage. Volontairement modeste : la file se vide
    #: en quelques tours, et un pic de decouverte ne se paie pas en rafale de
    #: requetes DNS.
    batch_size: int = 40
    #: Resolutions menees de front. Au-dela, un resolveur lent se transforme en
    #: file d'attente cote controleur.
    concurrency: int = 8
    timeout_s: float = 2.0
    #: Au-dela, on cesse de redemander : la majorite d'internet n'a pas de nom
    #: inverse, et insister ferait une requete perpetuelle par adresse muette.
    max_attempts: int = 3

    resolver: ReverseDns = field(default_factory=ReverseDns)
    _geoip_reader: Any = None
    _geoip_broken: bool = False
    resolved: int = 0
    named: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        self.resolver.timeout_s = self.timeout_s

    def apply_runtime(
        self,
        *,
        enabled: bool,
        rdns_enabled: bool,
        rdap_enabled: bool,
        batch_size: int,
        max_attempts: int,
        geoip_enabled: bool = False,
    ) -> None:
        """Reprend les reglages pilotables a chaud depuis l'interface."""
        self.enabled = enabled
        self.rdns_enabled = rdns_enabled
        self.rdap_enabled = rdap_enabled
        self.geoip_enabled = geoip_enabled
        self.batch_size = batch_size
        self.max_attempts = max_attempts

    # ----------------------------------------------------------------- verdict
    async def analyse(self, address: str) -> Enrichment:
        """Le verdict pour UNE adresse, toutes sources autorisees confondues.

        Le catalogue est consulte en premier parce qu'il est gratuit, mais il
        n'a pas le dernier mot : un nom inverse qui nomme le service l'emporte
        (cf. ``ipfinder.identify``). Un cache Netflix heberge chez l'operateur
        n'est dans aucun bloc publie, et c'est precisement le cas ou se tromper
        coute le plus cher.
        """
        nom: str | None = None
        if self.rdns_enabled:
            # CHAQUE SOURCE EST ISOLEE. Un resolveur qui casse, un registre qui
            # limite le debit, un service de localisation en panne : aucun ne
            # doit couter le verdict que les autres ont deja rendu -- ni, pire,
            # faire perdre le LOT ENTIER d'adresses en cours d'enrichissement.
            # Ce sont des bonus, pas un socle.
            nom = await self._sans_casser(
                "nom inverse", asyncio.to_thread(self.resolver.lookup, address)
            )
        asn: int | None = None
        registre: dict[str, Any] = {}
        if self.rdap_enabled:
            registre = await self._sans_casser("registre", self._rdap(address)) or {}
            asn = registre.get("asn")
        if self.geoip_enabled:
            # La localisation COMPLETE le registre, elle ne l'ecrase pas : RDAP
            # dit qui detient le bloc, la geolocalisation ou il est annonce. Les
            # deux different souvent, et c'est le registre qui fait autorite sur
            # l'organisation.
            localisation = await self._sans_casser("localisation", self._geoip(address)) or {}
            for cle, valeur in localisation.items():
                registre.setdefault(cle, valeur)
            asn = asn or registre.get("asn")
        verdict = ipfinder.identify(address, hostname=nom, asn=asn)
        return Enrichment(address=address, verdict=verdict, hostname=nom, registry=registre)

    async def _sans_casser(self, source: str, attendu: Any) -> Any:
        """Attend une source d'enrichissement, et rend None si elle echoue.

        Sans cette barriere, une seule adresse dont la resolution part en
        exception faisait perdre TOUT le lot : les autres adresses repartaient
        en file d'attente, et le verdict du catalogue -- qui ne coute rien et ne
        peut pas echouer -- etait jete avec le reste.
        """
        try:
            return await attendu
        except Exception as exc:  # noqa: BLE001 - une source est un bonus
            logger.debug("Source '%s' muette : %s", source, exc)
            self.last_error = f"{source} : {exc}"
            return None

    # ------------------------------------------------------------ localisation
    def _geoip_local(self, address: str) -> dict[str, Any] | None:
        """Localisation lue dans une base MaxMind posee a cote, si elle existe.

        AUCUN APPEL SORTANT : c'est la seule forme de geolocalisation qu'on
        puisse recommander sans reserve, puisque rien ne quitte le controleur.
        Le paquet 'geoip2' n'est pas une dependance : son absence fait
        simplement retomber sur le service HTTP, si celui-ci est autorise.
        """
        if not self.geoip_db or self._geoip_broken:
            return None
        if self._geoip_reader is None:
            try:
                import geoip2.database

                self._geoip_reader = geoip2.database.Reader(self.geoip_db)
            except Exception as exc:  # noqa: BLE001 - base absente ou paquet manquant
                logger.warning(
                    "Base de geolocalisation inutilisable (%s) : %s. "
                    "Le service HTTP prend le relais s'il est autorise.",
                    self.geoip_db,
                    exc,
                )
                self._geoip_broken = True
                return None
        try:
            fiche = self._geoip_reader.city(address)
        except Exception:  # noqa: BLE001 - adresse absente de la base
            return {}
        return {
            "country": fiche.country.iso_code,
            "city": fiche.city.name,
            "region": fiche.subdivisions.most_specific.name,
            "latitude": fiche.location.latitude,
            "longitude": fiche.location.longitude,
        }

    async def _geoip(self, address: str) -> dict[str, Any]:
        """Ou cette adresse est annoncee : pays, region, ville, coordonnees.

        LA BASE LOCALE D'ABORD. Elle repond sans rien emettre ; le service HTTP
        n'est interroge que faute de mieux, et il recoit alors l'adresse que
        votre client a jointe -- une information sur lui.

        Un echec n'est pas une erreur d'exploitation : le service peut limiter
        le debit, etre injoignable, ou ne rien savoir de cette adresse. On rend
        un dictionnaire vide et on garde ce que les autres sources ont dit.
        """
        locale = await asyncio.to_thread(self._geoip_local, address)
        if locale is not None:
            return {cle: valeur for cle, valeur in locale.items() if valeur is not None}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                reponse = await client.get(self.geoip_url.format(ip=address))
                reponse.raise_for_status()
                charge = reponse.json()
        except Exception as exc:  # noqa: BLE001 - la localisation est un bonus
            logger.debug("Geolocalisation muette pour %s : %s", address, exc)
            return {}
        if not isinstance(charge, dict):
            return {}
        asn = str(charge.get("asn") or "")
        return {
            cle: valeur
            for cle, valeur in {
                # Les noms de champs varient d'un service a l'autre : on accepte
                # les deux formes les plus repandues plutot que d'imposer un
                # fournisseur precis.
                "country": charge.get("country_code") or charge.get("countryCode"),
                "city": charge.get("city"),
                "region": charge.get("region") or charge.get("regionName"),
                "latitude": charge.get("latitude") or charge.get("lat"),
                "longitude": charge.get("longitude") or charge.get("lon"),
                "org": charge.get("org") or charge.get("isp"),
                "asn": int(asn[2:]) if asn.upper().startswith("AS") and asn[2:].isdigit() else None,
            }.items()
            if valeur not in (None, "")
        }

    async def _rdap(self, address: str) -> dict[str, Any]:
        """Organisation, AS, pays et bloc annonce, lus au registre.

        Un echec n'est pas une erreur d'exploitation : le registre peut etre
        injoignable, limiter le debit, ou ne rien savoir de cette adresse. On
        rend un dictionnaire vide et on garde ce que les autres sources ont dit.
        """
        url = f"{self.rdap_url.rstrip('/')}/{address}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                reponse = await client.get(url, headers={"Accept": "application/rdap+json"})
                reponse.raise_for_status()
                charge = reponse.json()
        except Exception as exc:  # noqa: BLE001 - le registre est un bonus, pas un socle
            logger.debug("RDAP muet pour %s : %s", address, exc)
            return {}
        if not isinstance(charge, dict):
            return {}
        organisation = str(charge.get("name") or "") or None
        pays = str(charge.get("country") or "") or None
        debut = charge.get("startAddress")
        longueur = charge.get("cidr0_cidrs")
        reseau: str | None = None
        if isinstance(longueur, list) and longueur:
            premier = longueur[0]
            if isinstance(premier, dict):
                base = premier.get("v4prefix") or premier.get("v6prefix")
                if base and premier.get("length") is not None:
                    reseau = f"{base}/{premier['length']}"
        if reseau is None and debut:
            reseau = str(debut)
        asn: int | None = None
        for entite in charge.get("entities") or []:
            if not isinstance(entite, dict):
                continue
            for role in entite.get("roles") or []:
                if str(role) in {"registrant", "administrative"}:
                    handle = str(entite.get("handle") or "")
                    if handle.upper().startswith("AS") and handle[2:].isdigit():
                        asn = int(handle[2:])
        return {"org": organisation, "country": pays, "network": reseau, "asn": asn}

    # ------------------------------------------------------------------ boucle
    async def resolve_pending(self, limit: int | None = None) -> int:
        """Nomme un lot d'adresses en attente. Rend le nombre traite."""
        if not self.enabled or self.destinations is None:
            return 0
        try:
            adresses = await self.destinations.pending(
                limit=limit or self.batch_size, max_attempts=self.max_attempts
            )
        except Exception as exc:  # noqa: BLE001 - la base peut ne pas etre prete
            self.last_error = f"file d'attente illisible : {exc}"
            logger.debug("Enrichissement : %s", self.last_error)
            return 0
        if not adresses:
            self.last_run_at = datetime.now(tz=UTC)
            return 0
        return await self._resolve(adresses)

    async def resolve_now(self, address: str) -> dict[str, Any]:
        """Force la (re)analyse d'une adresse, a la demande de l'exploitant.

        Sert quand un service vient de changer de nom inverse, ou quand le
        catalogue a ete enrichi depuis la derniere resolution.
        """
        if self.destinations is None:
            return {"address": address, "resolved": 0}
        await self.destinations.forget_resolution(address)
        traite = await self._resolve([address])
        fiche = await self.destinations.get_intel(address)
        return {"address": address, "resolved": traite, "intel": fiche}

    async def _resolve(self, adresses: list[str]) -> int:
        verrou = asyncio.Semaphore(max(1, self.concurrency))
        maintenant = datetime.now(tz=UTC)

        async def un(address: str) -> dict[str, Any]:
            async with verrou:
                trouve = await self.analyse(address)
            verdict = trouve.verdict
            return {
                "address": address,
                "hostname": trouve.hostname,
                "service": verdict.service,
                "category": verdict.category if verdict.known else None,
                "source": verdict.source,
                "org": trouve.registry.get("org"),
                "asn": trouve.registry.get("asn"),
                "country": trouve.registry.get("country"),
                "city": trouve.registry.get("city"),
                "region": trouve.registry.get("region"),
                "latitude": trouve.registry.get("latitude"),
                "longitude": trouve.registry.get("longitude"),
                "network": trouve.registry.get("network") or verdict.matched_prefix,
                # POSE MEME QUAND ON N'A RIEN TROUVE. "Cette adresse n'a pas de
                # nom" est une reponse : sans cette date, l'adresse resterait
                # dans la file et serait redemandee a chaque passage.
                "resolved_at": maintenant,
            }

        try:
            verdicts = await asyncio.gather(*(un(a) for a in adresses))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"resolution interrompue : {exc}"
            logger.warning("Enrichissement : %s", self.last_error)
            return 0
        if self.destinations is not None:
            try:
                await self.destinations.save_intel(list(verdicts))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"verdicts non enregistres : {exc}"
                logger.warning("Enrichissement : %s", self.last_error)
                return 0
        self.resolved += len(verdicts)
        self.named += sum(1 for v in verdicts if v["service"])
        self.last_run_at = datetime.now(tz=UTC)
        self.last_error = None
        return len(verdicts)

    async def status(self) -> dict[str, Any]:
        """De quoi diagnostiquer sans ouvrir un terminal.

        ``pending`` qui ne descend jamais veut dire une chose precise : le
        resolveur DNS du controleur ne repond pas, ou la cadence est trop lente
        pour le nombre d'adresses decouvertes.
        """
        en_attente = 0
        if self.destinations is not None:
            try:
                en_attente = await self.destinations.count_pending(max_attempts=self.max_attempts)
            except Exception:  # noqa: BLE001
                en_attente = -1
        return {
            "enabled": self.enabled,
            "rdns_enabled": self.rdns_enabled,
            "rdap_enabled": self.rdap_enabled,
            "geoip_enabled": self.geoip_enabled,
            "geoip_local": bool(self.geoip_db) and not self._geoip_broken,
            "batch_size": self.batch_size,
            "resolved": self.resolved,
            "named": self.named,
            "pending": en_attente,
            "catalogue_services": len(ipfinder.CATALOGUE),
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
        }

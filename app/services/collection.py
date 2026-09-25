"""Orchestration d'un cycle de collecte.

C'est la boucle CENTRALE LENTE du systeme : elle collecte, normalise, resout les
identifiants et ecrit. Elle ne reagit pas au temps reel.

La boucle LOCALE RAPIDE (reaction aux fades radio a la latence) vit sur le PoP et
n'est deliberement PAS implementee ici : cette application se contente de fixer et
de tenir a jour les baselines que cette boucle locale respectera.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from app.collectors.mikrotik import MikrotikCollector
from app.collectors.pop_census import PartialCensusError
from app.collectors.radius import PlanProvider
from app.collectors.uisp import BackhaulCapacityProvider
from app.config import BackhaulConfig, Settings
from app.db.directory import Directory
from app.db.writer import MetricsWriter
from app.models import (
    KIND_PPPOE,
    KIND_STATIC,
    BackhaulSample,
    InterfaceSample,
    Plan,
    PppoeSession,
    RunResult,
    StaticClient,
    SubscriberSample,
    VlanCounter,
    VlanSighting,
)
from app.services.pop_match import resolve_pop
from app.services.rates import RateTracker
from app.services.rtt import PathProber, RttProber
from app.services.vlan_sites import SITE_VLAN, VlanSite, sites_from

logger = logging.getLogger(__name__)

JOB_SUBSCRIBERS = "collect_subscribers"
JOB_BACKHAULS = "collect_backhauls"
JOB_LINKS = "collect_links"
JOB_PLANS = "refresh_plans"
JOB_INVENTORY = "reload_inventory"
JOB_RTT = "probe_rtt"
JOB_BOOSTS = "expire_boosts"
JOB_RECONCILE = "reconcile_shaping"
JOB_QOE_LOOP = "qoe_closed_loop"
JOB_VLAN_CLIENTS = "detect_vlan_clients"
JOB_TOPOLOGY = "discover_topology"

# Origine du plan d'un client a IP fixe. Ce n'est pas RADIUS et ca ne doit pas
# en avoir l'air : le debit vient de la fiche saisie par l'operateur.
PLAN_SOURCE_STATIC = "static-inventory"

# Drapeau basculable a chaud (base + interface), amorce par RTT_ENABLED. La sonde
# est toujours instanciee et planifiee ; ce drapeau decide juste si elle sonde.
FLAG_RTT = "rtt_enabled"


class AntennasProvider(Protocol):
    """Contrat du provider des antennes ajoutees depuis l'interface (airOS en base).

    Deux responsabilites, volontairement reunies dans UN seul objet : lister les
    antennes de la base et les rattacher a leur PoP (``backhaul_configs``), et
    lire leur capacite du moment (``get_capacities``). Ce contrat explicite est
    ce qui manquait : le parametre etait annote ``Any``, ce qui laissait injecter
    un objet ne portant que la moitie du contrat (le provider de capacite sans
    ``backhaul_configs``), et le cycle backhaul echouait en boucle sans que rien
    ne l'attrape au montage.
    """

    async def backhaul_configs(self) -> list[BackhaulConfig]: ...

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]: ...

    async def raw_devices(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class StaticClientsProvider(Protocol):
    """Contrat de l'inventaire declaratif des clients a IP fixe.

    Volontairement reduit a une seule methode : le service de collecte n'a
    besoin de rien d'autre que la liste a prendre en compte ce cycle-ci, et
    l'inventaire se relit tout seul a chaque tour (une fiche modifiee depuis
    l'interface est prise en compte au cycle suivant, sans redemarrage).
    """

    async def load_enabled(self) -> list[StaticClient]: ...


class VlanSightingsProvider(Protocol):
    """Contrat du depot des observations ARP.

    LA REGLE, INCHANGEE : aucune adresse observee ne peut devenir un abonne. Le
    service enregistre ce qu'il a vu, oublie ce qui est perime, et ne lit JAMAIS
    les candidats -- c'est structurel, pas cosmetique. Une detection qui se
    transformerait en fiche toute seule ferait naitre des abonnes que personne
    n'a vendus.

    ``vlan_sites`` ne l'entame pas : il ne rend aucune adresse, aucune MAC,
    aucun candidat. Il rend le NOM des VLAN sur lesquels quelque chose a parle,
    et rien d'autre. Ce nom sert a appeler un site par le nom que l'exploitant a
    lui-meme ecrit sur son routeur, au lieu de "VLAN 101" -- il ne cree aucun
    abonne, il en range.
    """

    async def record(self, sightings: Sequence[VlanSighting], *, seen_at: datetime) -> int: ...

    async def prune(self, *, older_than_s: float) -> int: ...

    async def vlan_sites(self, *, max_age_s: float | None = None) -> list[dict[str, Any]]: ...


class CollectionService:
    def __init__(
        self,
        settings: Settings,
        *,
        collectors: Sequence[MikrotikCollector],
        backhaul_provider: BackhaulCapacityProvider,
        plan_provider: PlanProvider,
        directory: Directory,
        writer: MetricsWriter,
        backhauls: Sequence[BackhaulConfig] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rtt_prober: RttProber | None = None,
        path_prober: PathProber | None = None,
        antennas_provider: AntennasProvider | None = None,
        static_clients: StaticClientsProvider | None = None,
        sightings: VlanSightingsProvider | None = None,
    ) -> None:
        self.settings = settings
        self.collectors = list(collectors)
        self.backhaul_provider = backhaul_provider
        # Provider des antennes ajoutees depuis l'interface (airOS en base). Il
        # relit sa liste tout seul a chaque cycle : rien a recharger ici.
        self.antennas_provider = antennas_provider
        self.plan_provider = plan_provider
        # Inventaire des clients a IP fixe. Absent = deploiement 100 % PPPoE,
        # et tout ce qui suit se comporte exactement comme avant.
        self.static_clients = static_clients
        # Depot des observations ARP. Absent = detection coupee, et tout le
        # reste se comporte exactement comme avant.
        self.sightings = sightings
        # Collecteur NetFlow, branche apres coup par le conteneur (il est cree
        # plus tard). Source de secours du debit des clients a IP fixe.
        self.netflow: Any = None
        self.directory = directory
        self.writer = writer
        self.backhauls = list(backhauls if backhauls is not None else settings.enabled_backhauls)
        # Horloge monotone injectable : elle sert a dater les intervalles entre
        # compteurs, jamais les enregistrements (qui portent un horodatage UTC).
        # L'injecter rend les tests de debit deterministes sans toucher au module time.
        self._clock = clock

        self.rates = RateTracker(
            max_plausible_bps=settings.max_plausible_bps,
            min_interval_s=settings.min_rate_interval_s,
        )
        # Tracker distinct de celui des abonnes : memes garde-fous (reset de
        # compteur au redemarrage du routeur, debit aberrant rejete), mais un
        # etat separe pour que le prune des sessions n'efface pas les ports.
        self.interface_rates = RateTracker(
            max_plausible_bps=settings.max_plausible_bps,
            min_interval_s=settings.min_rate_interval_s,
        )
        self._known_logins: set[str] = set()
        self.last_results: dict[str, RunResult] = {}
        # Sonde de latence optionnelle. Sans elle, rtt_ms reste NULL : la colonne
        # existe depuis la phase 1, elle attendait juste une source.
        self.rtt_prober = rtt_prober
        # Latence par segment (PoP -> amont, PoP -> internet), meme drapeau.
        self.path_prober = path_prober
        # Activation vivante de la sonde : amorcee par l'env, ensuite pilotee
        # depuis l'interface (le container la relit en base au demarrage).
        self.rtt_enabled = settings.rtt_enabled
        # Cibles du prochain tour de sonde, rafraichies a chaque cycle.
        self._rtt_targets: list[tuple[int, str, MikrotikCollector]] = []

    def set_collectors(self, collectors: Sequence[MikrotikCollector]) -> None:
        """Remplace l'ensemble des collecteurs a chaud.

        Appele par le RouterRegistry apres un ajout ou une suppression de PoP
        depuis l'interface. Le RateTracker n'est PAS purge : ses cles sont
        prefixees par le nom du routeur, donc les series des routeurs conserves
        gardent leur point de reference, et celles des routeurs retires seront
        eliminees au prochain prune.
        """
        self.collectors = list(collectors)

    # ------------------------------------------------------------------
    # Abonnes
    # ------------------------------------------------------------------
    async def collect_subscribers(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []

        # Les routeurs sont interroges en parallele : un PoP injoignable ne doit
        # pas retarder ni annuler la collecte des autres.
        gathered = await asyncio.gather(
            *(collector.collect() for collector in self.collectors),
            return_exceptions=True,
        )

        sessions_by_router: list[tuple[MikrotikCollector, list[PppoeSession]]] = []
        for collector, outcome in zip(self.collectors, gathered, strict=True):
            if isinstance(outcome, BaseException):
                message = f"{collector.name}: {type(outcome).__name__}: {outcome}"
                errors.append(message)
                logger.error("Collecte impossible sur %s : %s", collector.name, outcome)
                continue
            sessions_by_router.append((collector, outcome))

        rows: list[tuple[int, SubscriberSample]] = []
        # tuple[str | None, object] : le second membre est un datetime, mais le
        # contrat Directory.touch_subscribers l'accepte en ``object`` (invariance
        # des dict), on aligne donc l'annotation dessus.
        seen: dict[int, tuple[str | None, object]] = {}
        active_keys: set[str] = set()
        rtt_targets: list[tuple[int, str, MikrotikCollector]] = []

        for collector, sessions in sessions_by_router:
            try:
                pop_id = await self.directory.ensure_pop(
                    collector.config.effective_pop_name, collector.config.host
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{collector.name}: PoP non resolu: {exc}")
                logger.exception("Resolution du PoP impossible pour %s", collector.name)
                continue

            plans = await self._plans_for_new_logins([s.login for s in sessions])

            for session in sessions:
                key = f"{collector.name}/{session.login}"
                active_keys.add(key)
                try:
                    subscriber_id = await self.directory.ensure_subscriber(
                        session.login,
                        pop_id=pop_id,
                        plan=plans.get(session.login),
                        kind=KIND_PPPOE,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{session.login}: abonne non resolu: {exc}")
                    continue
                self._known_logins.add(session.login)

                rate = self.rates.update(
                    key,
                    ts=monotonic,
                    rx_bytes=session.rx_bytes,
                    tx_bytes=session.tx_bytes,
                    uptime_s=session.uptime_s,
                )
                rows.append(
                    (
                        subscriber_id,
                        SubscriberSample(
                            ts=started_at,
                            login=session.login,
                            router_name=session.router_name,
                            pop_name=session.pop_name,
                            address=session.address,
                            uptime_s=session.uptime_s,
                            rx_bytes=session.rx_bytes,
                            tx_bytes=session.tx_bytes,
                            rx_bps=rate.rx_bps,
                            tx_bps=rate.tx_bps,
                            # Derniere mesure de latence si elle n'est pas perimee.
                            rtt_ms=(
                                self.rtt_prober.get(subscriber_id)
                                if self.rtt_prober is not None
                                else None
                            ),
                        ),
                    )
                )
                seen[subscriber_id] = (session.address, started_at)
                if session.address:
                    rtt_targets.append((subscriber_id, session.address, collector))

        # Clients a IP fixe : meme cycle, meme table, meme ecriture. Ils sont
        # traites APRES les sessions pour que 'seen' et 'rows' partent ensemble
        # en une seule ecriture, et parce qu'un inventaire illisible ne doit
        # jamais empecher les abonnes PPPoE d'etre enregistres.
        await self._collect_static_clients(
            started_at=started_at,
            monotonic=monotonic,
            rows=rows,
            seen=seen,
            active_keys=active_keys,
            rtt_targets=rtt_targets,
            errors=errors,
        )

        self.rates.prune(active_keys)
        self._rtt_targets = rtt_targets
        if self.rtt_prober is not None:
            self.rtt_prober.forget_all_but({sid for sid, _, _ in rtt_targets})

        written = 0
        try:
            written = await self.writer.write_subscriber_metrics(rows)
            await self.directory.touch_subscribers(seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Ecriture des metriques abonnes impossible")

        result = RunResult(
            job=JOB_SUBSCRIBERS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=written,
            errors=errors,
        )
        await self._finalize(result)
        return result

    async def _collect_static_clients(
        self,
        *,
        started_at: datetime,
        monotonic: float,
        rows: list[tuple[int, SubscriberSample]],
        seen: dict[int, tuple[str | None, object]],
        active_keys: set[str],
        rtt_targets: list[tuple[int, str, MikrotikCollector]],
        errors: list[str],
    ) -> None:
        """Materialise les clients a IP fixe declares, et les mesure si on peut.

        DEUX SOURCES, UNE SEULE TABLE. Un abonne PPPoE se decouvre dans
        /ppp/active ; un client statique se lit dans l'inventaire. A partir de
        la ligne 'subscribers', plus rien ne les distingue sauf leur 'kind' --
        et c'est tout l'interet : plan, surcharges, boosts, files et interface
        suivent ensuite exactement le meme chemin.

        LA MESURE EST OPTIONNELLE, ET C'EST UNE LIMITE ASSUMEE. Le seul compteur
        par client dont on dispose est celui de sa file. Tant qu'aucune file ne
        vise son adresse, le client existe, porte son plan et apparait dans
        l'interface, mais sans debit : c'est plus honnete qu'un zero qui se
        lirait comme une absence de trafic.
        """
        if self.static_clients is None:
            return
        try:
            clients = await self.static_clients.load_enabled()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"inventaire statique illisible: {exc}")
            logger.exception("Lecture de l'inventaire des clients statiques impossible")
            return
        if not clients:
            return

        # Le PoP saisi dans la fiche est rapproche du PoP porte par un routeur a
        # la casse, aux accents et au mot "PoP" pres. Une egalite stricte faisait
        # de "francophonie" et "Francophonie" deux sites distincts : le client
        # n'avait alors ni collecteur, ni compteur, ni file -- sans qu'aucune
        # erreur ne soit levee nulle part.
        par_client = {
            client.reference: resolve_pop(client.pop_name, self.collectors) for client in clients
        }

        # Compteurs de files, lus UNE fois par routeur concerne. Un routeur
        # injoignable coute juste la mesure de ses clients, pas leur existence.
        routeurs = {
            collector.name: collector
            for match in par_client.values()
            for collector in match.collectors
        }
        compteurs: dict[str, dict[str, tuple[int | None, int | None]]] = {}
        if routeurs:
            noms = list(routeurs)
            mesures = await asyncio.gather(
                *(routeurs[nom].queue_counters() for nom in noms),
                return_exceptions=True,
            )
            for nom, mesure in zip(noms, mesures, strict=True):
                if isinstance(mesure, BaseException):
                    logger.warning("Compteurs de files illisibles sur %s : %s", nom, mesure)
                    compteurs[nom] = {}
                else:
                    compteurs[nom] = mesure

        # Compteurs des interfaces VLAN, lus seulement sur les routeurs qui
        # portent au moins un client declare par son VLAN. C'est la mesure de
        # ce client quand aucune file ne le vise encore (ecriture coupee).
        par_vlan = vlan_occupancy(clients, par_client)
        routeurs_vlan = sorted({routeur for routeur, _vlan in par_vlan})
        compteurs_vlan: dict[str, dict[int, list[VlanCounter]]] = {}
        if routeurs_vlan:
            mesures_vlan = await asyncio.gather(
                *(routeurs[nom].vlan_counters() for nom in routeurs_vlan),
                return_exceptions=True,
            )
            for nom, lu in zip(routeurs_vlan, mesures_vlan, strict=True):
                if isinstance(lu, BaseException):
                    logger.warning("Compteurs VLAN illisibles sur %s : %s", nom, lu)
                    compteurs_vlan[nom] = {}
                else:
                    compteurs_vlan[nom] = lu

        # Les VLAN qui portent des clients sont des SITES a part entiere. Chez un
        # operateur radio, un VLAN porte un village ou un relais ; le routeur
        # n'en est que la tete. Tant que seul le site du routeur existait, tous
        # les clients de tous les VLAN d'un meme NAS tombaient dans un seul sac.
        sites_vlan = await self._sites_vlan(clients, par_client)

        for client in clients:
            match = par_client[client.reference]
            collector = match.collectors[0] if match.collectors else None
            # Le PoP retenu est celui du ROUTEUR quand il a ete rapproche : sans
            # cela, une difference de casse ferait naitre un PoP fantome en base,
            # et les mesures du client iraient s'y ranger au lieu du vrai site.
            pop_name = match.pop_name or client.pop_name
            site = sites_vlan.get(client.reference)
            try:
                if site is not None:
                    pop_id = await self.directory.ensure_pop(
                        site.name,
                        collector.config.host if collector is not None else None,
                        kind=SITE_VLAN,
                        router_name=site.router_name,
                        vlan_id=site.vlan_id,
                        vlan_interface=site.vlan_interface,
                    )
                    pop_name = site.name
                else:
                    pop_id = await self.directory.ensure_pop(
                        pop_name,
                        collector.config.host if collector is not None else None,
                        router_name=collector.name if collector is not None else None,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{client.reference}: PoP non resolu: {exc}")
                continue

            plan = None
            if client.plan_down_mbps is not None or client.plan_up_mbps is not None:
                plan = Plan(
                    down_mbps=client.plan_down_mbps,
                    up_mbps=client.plan_up_mbps,
                    source=PLAN_SOURCE_STATIC,
                )
            try:
                subscriber_id = await self.directory.ensure_subscriber(
                    client.reference, pop_id=pop_id, plan=plan, kind=KIND_STATIC
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{client.reference}: client statique non resolu: {exc}")
                continue

            # Cle de suivi prefixee : elle ne peut pas entrer en collision avec
            # celle d'une session PPPoE, qui est '<routeur>/<login>'.
            key = f"static/{client.reference}"
            octets = _counters_for(compteurs.get(collector.name, {}) if collector else {}, client)
            if octets == (None, None) and collector is not None and client.vlan is not None:
                # Pas de file : le compteur de l'interface VLAN, s'il n'appartient
                # qu'a ce client. Cle de suivi DISTINCTE : le jour ou sa file est
                # posee, passer d'un compteur a l'autre sous la meme cle ferait
                # un delta absurde (des gigaoctets en dix secondes).
                vlan = sole_vlan_counter(
                    compteurs_vlan.get(collector.name, {}),
                    client.vlan,
                    clients_on_vlan=par_vlan.get((collector.name, client.vlan), 0),
                )
                if vlan is not None:
                    octets = (vlan.rx_bytes, vlan.tx_bytes)
                    key = f"static/{client.reference}@{vlan.interface}"
            active_keys.add(key)
            rate = self.rates.update(
                key,
                ts=monotonic,
                rx_bytes=octets[0],
                tx_bytes=octets[1],
            )
            rx_bps, tx_bps = rate.rx_bps, rate.tx_bps
            if rx_bps is None and tx_bps is None and octets == (None, None):
                # AUCUNE FILE NE LE COMPTE (pas encore posee, ecriture coupee,
                # ou son trafic ne traverse pas le routeur de son PoP) : NetFlow
                # l'a peut-etre vu passer. Mieux vaut ce debit, a la minute pres,
                # que rien du tout pour un client qu'on vient d'ajouter.
                netflow = self.netflow
                if netflow is not None and netflow.measuring:
                    rx_bps, tx_bps = netflow.rate_for(subscriber_id)
            rows.append(
                (
                    subscriber_id,
                    SubscriberSample(
                        ts=started_at,
                        login=client.reference,
                        router_name=collector.name if collector is not None else "",
                        pop_name=pop_name,
                        address=client.address,
                        # Pas de session, donc pas d'anciennete de session : la
                        # remplir avec la duree depuis la saisie serait un
                        # contresens.
                        uptime_s=None,
                        rx_bytes=octets[0],
                        tx_bytes=octets[1],
                        rx_bps=rx_bps,
                        tx_bps=tx_bps,
                        rtt_ms=(
                            self.rtt_prober.get(subscriber_id)
                            if self.rtt_prober is not None
                            else None
                        ),
                    ),
                )
            )
            seen[subscriber_id] = (client.address, started_at)

            # Sonder une adresse de reseau n'a pas de sens : on ne mesure la
            # latence que des clients declares sur une adresse unique.
            hote = _single_host(client.address)
            if hote is not None and collector is not None:
                rtt_targets.append((subscriber_id, hote, collector))

    async def _plans_for_new_logins(self, logins: Sequence[str]) -> dict[str, Plan]:
        """Ne demande un plan que pour les logins jamais vus.

        Interroger RADIUS pour tous les abonnes a chaque cycle de 10 s serait
        inutile et couteux : les plans changent rarement, et le job dedie
        ``refresh_plans`` s'occupe de leur rafraichissement periodique.
        """
        unknown = [login for login in logins if login not in self._known_logins]
        if not unknown:
            return {}
        try:
            return dict(await self.plan_provider.get_plans(unknown))
        except Exception:  # noqa: BLE001
            # Un plan manquant ne doit pas empecher d'ecrire les metriques.
            logger.exception("Recuperation des plans impossible pour %d login(s)", len(unknown))
            return {}

    # ------------------------------------------------------------------
    # Detection des clients sur VLAN routee
    # ------------------------------------------------------------------
    async def _sites_vlan(
        self, clients: Sequence[StaticClient], par_client: dict[str, Any]
    ) -> dict[str, VlanSite]:
        """``reference du client -> site de son VLAN``, quand il en a un.

        LE NOM VIENT DU TERRAIN. La fiche d'un client ne porte qu'un numero de
        VLAN ; c'est l'observation ARP qui connait le nom de l'interface, donc
        le nom du site. Les deux sont rapproches ici, une fois, plutot que dans
        chaque appelant.

        Un inventaire sans VLAN, ou des observations illisibles, ne font rien
        echouer : on retombe simplement sur le site du routeur, c'est-a-dire sur
        le comportement d'avant.
        """
        avec_vlan = [c for c in clients if c.vlan is not None]
        if not avec_vlan:
            return {}

        vues: list[dict[str, Any]] = []
        if self.sightings is not None:
            try:
                vues = await self.sightings.vlan_sites(
                    max_age_s=self.settings.vlan_sighting_retention_s
                )
            except Exception:  # noqa: BLE001 - un site sans nom vaut mieux qu'un cycle perdu
                logger.exception("VLAN observes illisibles : sites nommes d'apres le numero seul")

        # Le routeur de chaque client, pour que son site sache qui le dessert.
        declares = [
            {
                "reference": c.reference,
                "vlan": c.vlan,
                "router_name": (
                    par_client[c.reference].collectors[0].name
                    if par_client[c.reference].collectors
                    else None
                ),
            }
            for c in avec_vlan
        ]
        sites = sites_from(vues, declares)

        par_reference: dict[str, VlanSite] = {}
        for ligne in declares:
            routeur, tag = ligne["router_name"], ligne["vlan"]
            if not routeur or tag is None:
                continue
            trouve = next(
                (s for s in sites if s.router_name == routeur and s.vlan_id == int(tag)), None
            )
            if trouve is not None:
                par_reference[str(ligne["reference"])] = trouve
        return par_reference

    async def detect_vlan_clients(self) -> RunResult:
        """Repere qui parle sur les VLAN routees, pour AIDER a la declaration.

        CE JOB NE CREE RIEN. Il enregistre des observations, point. Deux
        lectures s'en deduisent ailleurs, au moment de l'affichage :

          - une adresse comprise dans le bloc d'un client declare confirme sa
            presence ;
          - une adresse qui ne correspond a rien devient un candidat propose a
            l'operateur.

        La separation est volontairement structurelle : ce job ecrit dans
        ``vlan_sightings`` et n'a meme pas de methode pour lire les candidats.
        Aucun chemin de code ne peut donc transformer une detection en fiche,
        en plan ou en file -- il faut passer par l'interface, et par un humain
        qui saisit un debit souscrit que seul lui connait.
        """
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []

        if self.sightings is None or not self.settings.vlan_detect_enabled:
            result = RunResult(
                job=JOB_VLAN_CLIENTS,
                started_at=started_at,
                duration_s=self._clock() - monotonic,
                ok=True,
                items=0,
            )
            await self._finalize(result)
            return result

        # Les routeurs se voient les uns les autres : sans cette liste, chaque
        # PoP proposerait ses voisins comme clients a declarer, a chaque cycle.
        materiel = [c.config.host for c in self.collectors if c.config.host]
        gathered = await asyncio.gather(
            *(
                collector.collect_vlan_clients(known_equipment=materiel)
                for collector in self.collectors
            ),
            return_exceptions=True,
        )

        vues: list[VlanSighting] = []
        for collector, outcome in zip(self.collectors, gathered, strict=True):
            if isinstance(outcome, BaseException):
                # Un routeur mal lu ne doit annuler ni les autres routeurs, ni ce
                # qu'on a quand meme vu sur lui : un recensement partiel porte ses
                # observations avec son erreur, et les deux sont conservees.
                errors.append(f"{collector.name}: {type(outcome).__name__}: {outcome}")
                logger.warning("Recensement incomplet sur %s : %s", collector.name, outcome)
                if isinstance(outcome, PartialCensusError):
                    vues.extend(outcome.sightings)
                continue
            vues.extend(outcome)

        enregistrees = 0
        try:
            enregistrees = await self.sightings.record(vues, seen_at=started_at)
            oubliees = await self.sightings.prune(
                older_than_s=self.settings.vlan_sighting_retention_s
            )
            if oubliees:
                logger.debug("Detection VLAN : %d observation(s) perimee(s) oubliee(s)", oubliees)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Enregistrement des observations VLAN impossible")

        result = RunResult(
            job=JOB_VLAN_CLIENTS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=enregistrees,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    # Debit des liens (compteurs de ports)
    # ------------------------------------------------------------------
    async def collect_links(self) -> RunResult:
        """Debit de chaque port physique, derive de deux lectures successives.

        Un job separe de celui des abonnes, pour trois raisons : un routeur lent
        sur /interface/ethernet ne doit pas retarder les metriques abonnes, la
        cadence des ports peut etre plus lache que celle des sessions, et le job
        se coupe seul (intervalle <= 0) sans toucher au reste.
        """
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []

        gathered = await asyncio.gather(
            *(collector.collect_interfaces() for collector in self.collectors),
            return_exceptions=True,
        )

        rows: list[InterfaceSample] = []
        active_keys: set[str] = set()
        for collector, outcome in zip(self.collectors, gathered, strict=True):
            if isinstance(outcome, BaseException):
                errors.append(f"{collector.name}: {type(outcome).__name__}: {outcome}")
                logger.error(
                    "Lecture des interfaces impossible sur %s : %s", collector.name, outcome
                )
                continue

            for sample in outcome:
                key = f"{collector.name}/{sample.interface}"
                active_keys.add(key)
                rate = self.interface_rates.update(
                    key,
                    ts=monotonic,
                    rx_bytes=sample.rx_bytes,
                    tx_bytes=sample.tx_bytes,
                )
                sample.ts = started_at
                sample.rx_bps = rate.rx_bps
                sample.tx_bps = rate.tx_bps
                rows.append(sample)

        # Un port supprime ou un routeur retire ne doit pas garder son point de
        # reference : sinon un ecart de plusieurs heures produirait un debit faux
        # le jour ou le meme nom reapparait.
        self.interface_rates.prune(active_keys)

        written = 0
        try:
            written = await self.writer.write_interface_metrics(rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Ecriture des metriques d'interface impossible")

        result = RunResult(
            job=JOB_LINKS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=written,
            errors=errors,
        )
        await self._finalize(result)
        return result

    async def measure_link(self, router_name: str, interface: str) -> dict[str, Any]:
        """Mesure instantanee d'un port, a la demande.

        Ne passe pas par la base : c'est une question posee au routeur au moment
        ou l'operateur clique.
        """
        for collector in self.collectors:
            if collector.name == router_name:
                return await collector.measure_interface(interface)
        raise KeyError(router_name)

    # ------------------------------------------------------------------
    # Backhauls
    # ------------------------------------------------------------------
    async def collect_backhauls(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        written = 0

        # Deux sources, une seule logique : les backhauls du fichier (via le
        # provider statique) et les antennes ajoutees depuis l'interface (via le
        # provider airOS en base). Chacune apporte sa liste et son fournisseur.
        file_backhauls = [b for b in self.backhauls if b.uisp_device_id]
        db_antennas: list[BackhaulConfig] = []
        if self.antennas_provider is not None:
            try:
                db_antennas = await self.antennas_provider.backhaul_configs()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"antennes (base): {exc}")
                logger.exception("Liste des antennes airOS non lue")

        if not file_backhauls and not db_antennas:
            result = RunResult(JOB_BACKHAULS, started_at, self._clock() - monotonic, True, 0)
            await self._finalize(result)
            return result

        samples: dict[str, BackhaulSample] = {}
        for provider, configs in (
            (self.backhaul_provider, file_backhauls),
            (self.antennas_provider, db_antennas),
        ):
            if provider is None or not configs:
                continue
            try:
                lot = await provider.get_capacities(
                    [c.uisp_device_id for c in configs if c.uisp_device_id]
                )
                samples.update(lot)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"fournisseur de capacite: {exc}")
                logger.exception("Lecture de la capacite backhaul impossible")

        rows: list[tuple[int, BackhaulSample]] = []
        for config in [*file_backhauls, *db_antennas]:
            sample = samples.get(config.uisp_device_id or "")
            if sample is None:
                continue
            try:
                pop_id = await self.directory.ensure_pop(config.pop_name)
                backhaul_id = await self.directory.ensure_backhaul(
                    config.name,
                    pop_id=pop_id,
                    uisp_device_id=config.uisp_device_id,
                    nominal_capacity_mbps=config.nominal_capacity_mbps,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{config.name}: backhaul non resolu: {exc}")
                continue
            rows.append((backhaul_id, sample))

        try:
            written = await self.writer.write_backhaul_metrics(rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Ecriture des metriques backhaul impossible")

        result = RunResult(
            job=JOB_BACKHAULS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=written,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    # Latence
    # ------------------------------------------------------------------
    async def probe_rtt(self) -> RunResult:
        """Sonde un lot d'abonnes. Les mesures sont rattachees au cycle suivant."""
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        answered = 0

        # Coupee depuis l'interface : on ne sonde pas, mais le job reste planifie
        # pour repartir des qu'on la reactive, sans redemarrage.
        if self.rtt_prober is not None and self.rtt_enabled:
            try:
                answered = await self.rtt_prober.probe(self._rtt_targets)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                logger.exception("Sonde de latence impossible")
            if self.path_prober is not None:
                try:
                    await self.path_prober.probe(self.collectors)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"latence par segment: {exc}")
                    logger.exception("Sonde de latence par segment impossible")

        result = RunResult(
            job=JOB_RTT,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=answered,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------
    async def refresh_plans(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        updated = 0

        try:
            # SEULS les abonnes PPPoE sont concernes. RADIUS ne connait pas les
            # clients a IP fixe, et un serveur qui repondrait quand meme --
            # catch-all, plan par defaut -- ecraserait le debit declare dans
            # l'inventaire par une valeur inventee. C'est precisement ce qu'il
            # ne faut pas : pour eux, la fiche fait foi.
            logins = await self.directory.list_subscriber_logins(kind=KIND_PPPOE)
            self._known_logins.update(logins)
            if logins:
                plans = await self.plan_provider.get_plans(list(logins))
                updated = await self.directory.update_plans(
                    {logins[login]: plan for login, plan in plans.items() if login in logins}
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            logger.exception("Rafraichissement des plans impossible")

        result = RunResult(
            job=JOB_PLANS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=updated,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    async def _finalize(self, result: RunResult) -> None:
        self.last_results[result.job] = result
        try:
            await self.writer.record_run(result)
        except Exception:  # noqa: BLE001
            # L'historique d'execution est un confort d'exploitation, jamais un
            # motif d'echec du cycle.
            logger.warning("Historisation du run '%s' impossible", result.job)

    async def aclose(self) -> None:
        for collector in self.collectors:
            try:
                collector.close()
            except Exception:  # noqa: BLE001
                pass
        await self.backhaul_provider.aclose()
        if self.antennas_provider is not None:
            try:
                await self.antennas_provider.aclose()
            except Exception:  # noqa: BLE001
                pass
        await self.plan_provider.aclose()


def _single_host(address: str) -> str | None:
    """Rend l'adresse nue si elle designe UNE machine, sinon None.

    Un client declare en /29 n'a pas d'adresse a sonder : la latence n'a de sens
    que vers un hote precis.
    """
    try:
        reseau = ipaddress.ip_network(address, strict=False)
    except ValueError:
        return None
    if reseau.prefixlen != reseau.max_prefixlen:
        return None
    return str(reseau.network_address)


def vlan_occupancy(
    clients: Sequence[StaticClient], par_client: dict[str, Any]
) -> dict[tuple[str, int], int]:
    """``(routeur, VLAN) -> nombre de clients declares dessus``.

    C'est ce compte qui dit si le compteur d'une interface VLAN appartient a
    UN client : a deux sur le meme VLAN, il est leur somme, et l'attribuer a
    l'un des deux serait inventer une mesure.
    """
    compte: dict[tuple[str, int], int] = {}
    for client in clients:
        if client.vlan is None:
            continue
        match = par_client.get(client.reference)
        collectors = getattr(match, "collectors", None) or []
        if not collectors:
            continue
        cle = (collectors[0].name, client.vlan)
        compte[cle] = compte.get(cle, 0) + 1
    return compte


def sole_vlan_counter(
    counters: dict[int, list[VlanCounter]], vlan: int, *, clients_on_vlan: int
) -> VlanCounter | None:
    """Le compteur de l'interface VLAN, s'il ne mesure que CE client.

    Trois conditions, chacune une facon differente de se tromper :
    le client est seul declare sur ce VLAN ; le VLAN n'est pose que sur UNE
    interface (sinon laquelle ?) ; aucun serveur PPPoE ne l'ecoute (sinon le
    compteur porte aussi tous ses abonnes PPPoE).
    """
    if clients_on_vlan != 1:
        return None
    candidats = counters.get(vlan) or []
    if len(candidats) != 1:
        return None
    seul = candidats[0]
    if seul.pppoe or (seul.rx_bytes is None and seul.tx_bytes is None):
        return None
    return seul


def _counters_for(
    compteurs: dict[str, tuple[int | None, int | None]], client: StaticClient
) -> tuple[int | None, int | None]:
    """Retrouve les compteurs de la file qui vise ce client.

    RouterOS rend ses cibles sous forme canonique (``10.0.0.5/32``), ce que
    l'inventaire stocke aussi. On accepte tout de meme l'adresse nue, parce
    qu'une file posee a la main par l'operateur a pu etre saisie sans prefixe.
    """
    trouve = compteurs.get(client.address)
    if trouve is not None:
        return trouve
    hote = _single_host(client.address)
    if hote is not None:
        trouve = compteurs.get(hote)
        if trouve is not None:
            return trouve
    return (None, None)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)

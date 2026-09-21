"""Assemblage des dependances.

Toute la construction d'objets est isolee ici : les tests peuvent monter un
conteneur partiel (writer memoire, faux routeur, provider mock) sans toucher a
FastAPI ni a PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.collectors.radius import (
    FreeradiusSqlPlanProvider,
    MockPlanProvider,
    PlanProvider,
)
from app.collectors.uisp import (
    AirOsProvider,
    AirOsTarget,
    BackhaulCapacityProvider,
    DbAirOsProvider,
    MockBackhaulProvider,
    UispProvider,
)
from app.config import Settings
from app.db.antennas_repo import AntennasRepository
from app.db.api_keys_repo import ApiKeysRepository
from app.db.database import Database
from app.db.destinations_repo import DestinationsRepository
from app.db.directory import Directory, PgDirectory
from app.db.flows_repo import FlowsRepository, NetflowExportersRepository
from app.db.model_repo import ModelRepository
from app.db.repository import MetricsRepository
from app.db.routers_repo import RoutersRepository
from app.db.settings_repo import SettingsRepository
from app.db.static_clients_repo import StaticClientsRepository, VlanSightingsRepository
from app.db.topology_repo import TopologyRepository
from app.db.traffic_rules_repo import TrafficRulesRepository
from app.db.writer import MetricsWriter, PgMetricsWriter
from app.models import Plan
from app.scheduler import Scheduler
from app.services.collection import (
    JOB_BACKHAULS,
    JOB_BOOSTS,
    JOB_INVENTORY,
    JOB_LINKS,
    JOB_PLANS,
    JOB_QOE_LOOP,
    JOB_RECONCILE,
    JOB_RTT,
    JOB_SUBSCRIBERS,
    JOB_TOPOLOGY,
    JOB_VLAN_CLIENTS,
    CollectionService,
)
from app.services.crypto import KeySource, SecretBox, load_or_create_key
from app.services.intel import JOB_INTEL, IntelService
from app.services.netflow_export import JOB_NETFLOW_EXPORT, NetflowExportService
from app.services.netflow_service import JOB_NETFLOW, NetflowService
from app.services.registry import RouterRegistry
from app.services.restrictions import JOB_RESTRICTIONS, RestrictionService
from app.services.rtt import RttProber
from app.services.runtime_config import RuntimeConfig
from app.services.shaping import ShapingService, discover_with_devices

logger = logging.getLogger(__name__)


def build_plan_provider(settings: Settings) -> PlanProvider:
    if settings.plan_provider == "freeradius_sql":
        if not settings.radius_dsn:
            raise ValueError("PLAN_PROVIDER=freeradius_sql exige RADIUS_DSN")
        logger.info("Plans abonnes : FreeRADIUS (SQL)")
        return FreeradiusSqlPlanProvider(
            settings.radius_dsn,
            default_plan=Plan(
                down_mbps=settings.radius_default_down_mbps,
                up_mbps=settings.radius_default_up_mbps,
                source="radius:default",
            ),
        )
    logger.info("Plans abonnes : simulateur (aucune base RADIUS requise)")
    return MockPlanProvider()


def build_backhaul_provider(settings: Settings) -> BackhaulCapacityProvider:
    if settings.backhaul_provider == "uisp":
        if not settings.uisp_base_url or not settings.uisp_token:
            raise ValueError("BACKHAUL_PROVIDER=uisp exige UISP_BASE_URL et UISP_TOKEN")
        logger.info("Capacite backhaul : UISP %s (lecture seule)", settings.uisp_base_url)
        return UispProvider(
            settings.uisp_base_url,
            settings.uisp_token.get_secret_value(),
            verify_tls=settings.uisp_verify_tls,
            timeout_s=settings.uisp_timeout_s,
        )
    if settings.backhaul_provider == "airos":
        return _build_airos_provider(settings)
    logger.info("Capacite backhaul : simulateur (aucune radio requise)")
    return MockBackhaulProvider(
        base_capacity_mbps=settings.mock_backhaul_capacity_mbps,
        variation_pct=settings.mock_backhaul_variation_pct,
        period_s=settings.mock_backhaul_period_s,
        seed=settings.mock_backhaul_seed,
        nominal_by_device={
            backhaul.uisp_device_id: backhaul.nominal_capacity_mbps
            for backhaul in settings.enabled_backhauls
            if backhaul.uisp_device_id and backhaul.nominal_capacity_mbps
        },
    )


def _build_airos_provider(settings: Settings) -> AirOsProvider:
    """Construit les cibles airOS a partir des backhauls qui portent une api_host."""
    mot_de_passe_global = (
        settings.airos_password.get_secret_value() if settings.airos_password else None
    )
    targets: list[AirOsTarget] = []
    for backhaul in settings.enabled_backhauls:
        if not backhaul.api_host:
            continue
        targets.append(
            AirOsTarget(
                key=backhaul.airos_key,
                host=backhaul.api_host,
                username=backhaul.api_username or settings.airos_username or "",
                password=backhaul.resolve_api_password(mot_de_passe_global) or "",
                verify_tls=(
                    settings.airos_verify_tls
                    if backhaul.api_verify_tls is None
                    else backhaul.api_verify_tls
                ),
            )
        )
    if not targets:
        raise ValueError(
            "BACKHAUL_PROVIDER=airos exige au moins un backhaul avec 'api_host' "
            "(l'adresse de management de l'antenne Ubiquiti)"
        )
    logger.info("Capacite backhaul : airOS direct sur %d antenne(s)", len(targets))
    return AirOsProvider(targets, timeout_s=settings.airos_timeout_s)


@dataclass
class Container:
    settings: Settings
    database: Database
    writer: MetricsWriter
    repository: MetricsRepository
    directory: Directory
    plan_provider: PlanProvider
    backhaul_provider: BackhaulCapacityProvider
    collection: CollectionService
    scheduler: Scheduler
    secrets: SecretBox
    registry: RouterRegistry
    shaping: ShapingService
    runtime_config: RuntimeConfig | None = None
    settings_repo: SettingsRepository | None = None
    routers_repo: RoutersRepository | None = None
    topology_repo: TopologyRepository | None = None
    antennas_repo: AntennasRepository | None = None
    static_clients_repo: StaticClientsRepository | None = None
    sightings_repo: VlanSightingsRepository | None = None
    api_keys_repo: ApiKeysRepository | None = None
    model_repo: ModelRepository | None = None
    flows_repo: FlowsRepository | None = None
    exporters_repo: NetflowExportersRepository | None = None
    destinations_repo: DestinationsRepository | None = None
    traffic_rules_repo: TrafficRulesRepository | None = None
    netflow: NetflowService | None = None
    netflow_export: NetflowExportService | None = None
    intel: IntelService | None = None
    restrictions: RestrictionService | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


async def build_container(settings: Settings) -> Container:
    if settings.enforcement_enabled:
        # L'enforcement existe (phase 2), mais il reste une capacite d'ecriture
        # sur des equipements de production : on le dit fort au demarrage.
        logger.warning(
            "ENFORCEMENT ACTIF : ce controleur peut ecrire sur les routeurs. "
            "Seules les files marquees '%s' sont modifiees, et chaque plan reste "
            "soumis a une application explicite.",
            "freeqos:managed",
        )
    else:
        logger.info("Enforcement desactive : le controleur reste en lecture seule")

    database = Database(
        settings.asyncpg_dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        command_timeout=settings.db_command_timeout_s,
        auto_create=settings.db_auto_create,
    )
    await database.connect()
    if settings.db_auto_migrate:
        await database.migrate()
        await database.apply_policies(
            chunk_interval_hours=settings.chunk_interval_hours,
            compression_after_days=settings.compression_after_days,
            retention_days=settings.retention_days,
        )

    # Reglages d'exploitation : la BASE fait foi. On les applique AVANT de
    # construire quoi que ce soit, pour que collecteurs, cadences et politique de
    # shaping partent deja des bonnes valeurs. L'environnement n'a servi qu'a
    # fournir le defaut.
    settings_repo = SettingsRepository(database.pool)
    runtime_config = RuntimeConfig(settings)
    try:
        ignores = runtime_config.load(await settings_repo.load())
    except Exception:  # noqa: BLE001 - table pas encore creee : on garde les defauts
        logger.warning("Reglages non relus depuis la base : les defauts s'appliquent")
    else:
        if ignores:
            logger.warning("Reglages ignores (inconnus ou hors bornes) : %s", ", ".join(ignores))
        if runtime_config.overrides:
            logger.info(
                "%d reglage(s) repris de la base : %s",
                len(runtime_config.overrides),
                ", ".join(sorted(runtime_config.overrides)),
            )

    writer = PgMetricsWriter(database.pool)
    repository = MetricsRepository(database.pool)
    directory = PgDirectory(database.pool)
    plan_provider = build_plan_provider(settings)
    backhaul_provider = build_backhaul_provider(settings)

    cle, source, chemin = load_or_create_key(
        env_key=settings.app_secret_key,
        key_file=settings.app_secret_key_file,
        autogenerate=settings.app_secret_key_autogenerate,
    )
    secrets = SecretBox(cle)
    if not secrets.available:
        # Non bloquant : l'inventaire fichier fonctionne sans cle. Seul l'ajout
        # de PoP depuis l'interface est indisponible.
        logger.warning("Ajout de PoP par l'interface desactive : %s", secrets.unavailable_reason)
    elif source == KeySource.GENERATED:
        await _warn_if_secrets_orphaned(database, chemin)

    routers_repo = RoutersRepository(database.pool, secrets)
    topology_repo = TopologyRepository(database.pool)
    # Inventaire declaratif des clients a IP fixe. Aucun secret : ce sont des
    # adresses et des plans, pas des identifiants d'acces.
    static_clients_repo = StaticClientsRepository(database.pool)
    # Observations ARP. Depot SEPARE de l'inventaire : le controleur ecrit
    # ici, jamais dans static_clients, pour qu'aucune detection ne puisse
    # devenir une fiche sans passer par un humain.
    sightings_repo = VlanSightingsRepository(database.pool)
    # API publique : cles, modele de reseau (contrat Preseem) et mesures de
    # trafic. Aucun secret d'equipement ici -- des identifiants externes, des
    # debits et des octets.
    api_keys_repo = ApiKeysRepository(database.pool)
    model_repo = ModelRepository(database.pool)
    flows_repo = FlowsRepository(database.pool)
    exporters_repo = NetflowExportersRepository(database.pool)
    # Ce que les clients atteignent, et ce qu'on en sait. Deux natures dans un
    # seul depot : la mesure s'efface avec la retention, la connaissance reste.
    destinations_repo = DestinationsRepository(database.pool)
    # Les restrictions de trafic, telles qu'elles sont saisies. Aucune adresse
    # n'y est figee : une regle est un critere, resolu a chaque passage.
    traffic_rules_repo = TrafficRulesRepository(database.pool)
    antennas_repo = AntennasRepository(database.pool, secrets)
    # Provider des antennes ajoutees depuis l'interface : il relit sa liste dans
    # la base a chaque cycle, donc un ajout est collecte sans redemarrage.
    #
    # Les DEUX chargeurs viennent du meme depot : ``load_targets`` (secrets
    # dechiffres, pour ouvrir la session airOS) et ``backhaul_configs`` (nom, PoP
    # et cle stable, pour rattacher la capacite lue). Les separer ici serait
    # exactement l'erreur qui laissait le cycle backhaul echouer : le provider
    # doit porter le contrat complet.
    antennas_provider = DbAirOsProvider(
        antennas_repo.load_targets,
        config_loader=antennas_repo.backhaul_configs,
        timeout_s=settings.airos_timeout_s,
    )
    registry = RouterRegistry(settings, repository=routers_repo)
    shaping = ShapingService(
        settings,
        registry=registry,
        repository=topology_repo,
        metrics=repository,
        static_clients=static_clients_repo,
    )
    # Le drapeau d'ecriture vient de la base une fois amorce : le basculer depuis
    # l'interface ne doit pas demander un redemarrage.
    await shaping.load_flags()

    # Sonde de latence : TOUJOURS instanciee et planifiee. Son execution est
    # gouvernee par un drapeau basculable depuis l'interface (comme l'enforcement),
    # amorce par RTT_ENABLED puis relu en base -- rien a mettre dans l'env.
    rtt_prober = RttProber(
        batch_size=settings.rtt_batch_size,
        max_age_s=settings.rtt_max_age_s,
        count=settings.rtt_count,
    )

    collection = CollectionService(
        settings,
        collectors=await registry.reload(),
        backhaul_provider=backhaul_provider,
        antennas_provider=antennas_provider,
        plan_provider=plan_provider,
        directory=directory,
        writer=writer,
        rtt_prober=rtt_prober,
        static_clients=static_clients_repo,
        sightings=sightings_repo,
    )

    # Amorce le drapeau de la sonde RTT : la base fait foi une fois posee, sinon
    # on l'y ecrit depuis RTT_ENABLED. Ensuite, l'interface le bascule a chaud.
    await _bootstrap_rtt_flag(collection, topology_repo, settings)

    async def discover_topology() -> None:
        """Decouverte periodique du graphe.

        SANS CE JOB, la topologie n'existait que si quelqu'un cliquait
        "Relancer la decouverte" : l'onglet Arbre reseau
        restait vide sur une installation neuve, quel que soit l'etat des
        PoPs et de leur API. Le reglage topology_refresh_interval_s etait
        declare et ne pilotait rien.

        Le planificateur execute chaque job une premiere fois immediatement :
        l'arbre est donc peuple des le demarrage, sans geste de l'exploitant.
        """
        await discover_with_devices(shaping, (backhaul_provider, collection.antennas_provider))

    async def reload_inventory() -> None:
        """Relit l'inventaire, et REDECOUVRE si le graphe ne lui correspond plus.

        Un routeur ajoute, retire, desactive ou dont le role change modifie
        l'arbre : attendre le prochain cycle de decouverte (un quart d'heure par
        defaut) laissait l'interface afficher un reseau qui n'existait plus.
        Ce job-ci tourne toutes les minutes ; il est donc le bon endroit pour
        s'en apercevoir, et il ne coute rien quand rien ne bouge.

        La comparaison porte sur l'inventaire de la DERNIERE DECOUVERTE, pas sur
        le rechargement precedent : l'inventaire est relu par d'autres chemins
        (l'API qui liste les routeurs, par exemple), et comparer deux
        rechargements successifs laisserait le premier venu effacer l'ecart avant
        que ce job ne l'ait vu.
        """
        collection.set_collectors(await registry.reload())
        if registry.inventory_signature() == shaping.last_inventory_signature:
            return
        logger.info("Inventaire modifie : la topologie est redecouverte sans attendre")
        await discover_topology()

    scheduler = Scheduler()
    scheduler.add_job(
        JOB_SUBSCRIBERS, settings.subscriber_interval_s, collection.collect_subscribers
    )
    scheduler.add_job(JOB_BACKHAULS, settings.backhaul_interval_s, collection.collect_backhauls)
    scheduler.add_job(JOB_LINKS, settings.link_interval_s, collection.collect_links)
    scheduler.add_job(JOB_PLANS, settings.plan_refresh_interval_s, collection.refresh_plans)
    scheduler.add_job(JOB_INVENTORY, settings.inventory_refresh_interval_s, reload_inventory)
    scheduler.add_job(JOB_RTT, settings.rtt_interval_s, collection.probe_rtt)

    async def expire_boosts() -> None:
        await shaping.expire_boosts()

    scheduler.add_job(JOB_BOOSTS, settings.boost_check_interval_s, expire_boosts)

    async def reconcile_shaping() -> None:
        await shaping.reconcile()

    scheduler.add_job(JOB_RECONCILE, settings.shaping_reconcile_interval_s, reconcile_shaping)

    async def qoe_closed_loop() -> None:
        await shaping.adjust_for_qoe()

    scheduler.add_job(JOB_QOE_LOOP, settings.qoe_loop_interval_s, qoe_closed_loop)
    scheduler.add_job(
        JOB_VLAN_CLIENTS, settings.vlan_detect_interval_s, collection.detect_vlan_clients
    )

    scheduler.add_job(JOB_TOPOLOGY, settings.topology_refresh_interval_s, discover_topology)

    # Collecteur NetFlow. Il ECOUTE : aucune sonde, aucun miroir de port, aucune
    # charge ajoutee au coeur. Les exporteurs sont declares depuis l'interface,
    # avec leur point de mesure ('edge' en amont du coeur, 'pop' au PoP).
    netflow = NetflowService(
        flows_repo=flows_repo,
        exporters_repo=exporters_repo,
        destinations_repo=destinations_repo,
        bind=settings.netflow_bind,
        port=settings.netflow_port,
        enabled=settings.netflow_enabled,
        accounting_vantage=settings.netflow_accounting_vantage,
        customer_networks=tuple(settings.netflow_customer_networks),
        host_limit=settings.netflow_host_limit,
        track_hosts=settings.netflow_track_hosts,
        host_retention_s=settings.netflow_host_retention_s,
        track_destinations=settings.netflow_track_destinations,
        destination_limit=settings.netflow_destination_limit,
        destination_retention_s=settings.netflow_destination_retention_s,
        infrastructure_networks=tuple(settings.netflow_infrastructure_networks),
    )

    def adresses_d_exploitation() -> list[str]:
        """Ce qui appartient au reseau, pas aux clients.

        Le controleur lui-meme, les routeurs qu'il interroge, et les exporteurs
        declares. Cette liste se deduit de l'inventaire : elle suit le parc sans
        que personne n'ait a la tenir.
        """
        adresses: list[str] = []
        for collector in registry.collectors:
            adresses.append(collector.config.host)
            if collector.config.loopback:
                adresses.append(collector.config.loopback)
        adresses.extend(netflow.exporters)
        return adresses

    await netflow.start()
    netflow.set_infrastructure(adresses_d_exploitation())

    # Met un nom sur les adresses que NetFlow decouvre. Il ne touche jamais a la
    # reception : celle-ci se contente d'INSCRIRE l'adresse, et cette boucle-ci
    # vient lui chercher un nom a son rythme. Un resolveur DNS lent ne doit
    # jamais faire perdre un datagramme.
    intel = IntelService(
        destinations=destinations_repo,
        enabled=settings.ipfinder_enabled,
        rdns_enabled=settings.ipfinder_rdns_enabled,
        rdap_enabled=settings.ipfinder_rdap_enabled,
        rdap_url=settings.ipfinder_rdap_url,
        geoip_enabled=settings.ipfinder_geoip_enabled,
        geoip_url=settings.ipfinder_geoip_url,
        geoip_db=settings.ipfinder_geoip_db,
        batch_size=settings.ipfinder_batch_size,
        concurrency=settings.ipfinder_concurrency,
        timeout_s=settings.ipfinder_timeout_s,
        max_attempts=settings.ipfinder_max_attempts,
    )

    # Les restrictions de trafic. Elles ECRIVENT par le meme chemin que les
    # files -- ShapingService.apply -- donc sous le meme drapeau, le meme
    # coupe-circuit et le meme audit. Une restriction n'a aucun privilege qu'une
    # file n'ait pas.
    restrictions = RestrictionService(
        shaping=shaping,
        registry=registry,
        rules_repo=traffic_rules_repo,
        destinations=destinations_repo,
        flows_repo=flows_repo,
        address_limit=settings.restriction_address_limit,
    )

    async def flush_netflow() -> None:
        # Les reglages de trafic sont pilotables a chaud depuis l'interface (ils
        # vivent en base comme les autres) : la fenetre suivante doit les
        # prendre, sans redemarrage.
        netflow.apply_runtime(
            accounting_vantage=settings.netflow_accounting_vantage,
            track_hosts=settings.netflow_track_hosts,
            host_limit=settings.netflow_host_limit,
            host_retention_s=settings.netflow_host_retention_s,
            track_destinations=settings.netflow_track_destinations,
            destination_limit=settings.netflow_destination_limit,
            destination_retention_s=settings.netflow_destination_retention_s,
        )
        netflow.set_infrastructure(adresses_d_exploitation())
        await netflow.flush()

    # Pose l'export NetFlow sur les routeurs. Sans lui, le collecteur ecoute
    # dans le vide et le message "aucun datagramme recu" demandait deux
    # commandes a la main sur chaque PoP -- donc un PoP oublie qui se tait sans
    # que rien ne le signale.
    netflow_export = NetflowExportService(
        shaping=shaping,
        registry=registry,
        exporters_repo=exporters_repo,
        port=settings.netflow_port,
        version=settings.netflow_export_version,
        interfaces=settings.netflow_export_interfaces,
        active_flow_timeout=settings.netflow_export_active_timeout,
        inactive_flow_timeout=settings.netflow_export_inactive_timeout,
        collector_address=settings.netflow_collector_address,
        enabled=settings.netflow_export_auto,
    )

    async def ensure_netflow_export() -> None:
        netflow_export.enabled = settings.netflow_export_auto
        netflow_export.collector_address = settings.netflow_collector_address
        await netflow_export.ensure()

    async def resolve_intel() -> None:
        intel.apply_runtime(
            enabled=settings.ipfinder_enabled,
            rdns_enabled=settings.ipfinder_rdns_enabled,
            rdap_enabled=settings.ipfinder_rdap_enabled,
            batch_size=settings.ipfinder_batch_size,
            max_attempts=settings.ipfinder_max_attempts,
            geoip_enabled=settings.ipfinder_geoip_enabled,
        )
        await intel.resolve_pending()

    async def reconcile_restrictions() -> None:
        restrictions.address_limit = settings.restriction_address_limit
        await restrictions.reconcile()

    # TOUJOURS planifie, meme collecteur coupe -- exactement comme la sonde RTT.
    # Une cadence declaree dans les reglages doit piloter un job qui EXISTE :
    # sinon la changer depuis l'interface ne reprogramme rien, en silence, et
    # allumer NETFLOW_ENABLED demanderait un redemarrage pour que la cadence
    # reprenne effet. Le job ne coute rien quand le collecteur est coupe : il
    # rend la main immediatement.
    scheduler.add_job(JOB_NETFLOW, settings.netflow_flush_interval_s, flush_netflow)
    # Nommer les adresses nouvelles, et reconcilier les restrictions. TOUJOURS
    # planifies, meme coupes : un reglage change depuis l'interface doit
    # reprogrammer un job qui EXISTE, sinon la cadence saisie ne pilote rien et
    # rallumer la fonction demanderait un redemarrage.
    scheduler.add_job(JOB_INTEL, settings.ipfinder_interval_s, resolve_intel)
    scheduler.add_job(JOB_NETFLOW_EXPORT, settings.netflow_export_interval_s, ensure_netflow_export)
    scheduler.add_job(JOB_RESTRICTIONS, settings.restrictions_interval_s, reconcile_restrictions)

    # Changer une cadence depuis l'interface doit reprogrammer la boucle, pas
    # seulement l'affichage : le scheduler relit interval_s a chaque tour.
    runtime_config.on_interval_change = scheduler.set_interval

    return Container(
        settings=settings,
        database=database,
        writer=writer,
        repository=repository,
        directory=directory,
        plan_provider=plan_provider,
        backhaul_provider=backhaul_provider,
        collection=collection,
        scheduler=scheduler,
        secrets=secrets,
        registry=registry,
        shaping=shaping,
        runtime_config=runtime_config,
        settings_repo=settings_repo,
        routers_repo=routers_repo,
        topology_repo=topology_repo,
        antennas_repo=antennas_repo,
        static_clients_repo=static_clients_repo,
        sightings_repo=sightings_repo,
        api_keys_repo=api_keys_repo,
        model_repo=model_repo,
        flows_repo=flows_repo,
        exporters_repo=exporters_repo,
        destinations_repo=destinations_repo,
        traffic_rules_repo=traffic_rules_repo,
        netflow=netflow,
        netflow_export=netflow_export,
        intel=intel,
        restrictions=restrictions,
    )


async def _bootstrap_rtt_flag(collection: Any, topology_repo: Any, settings: Settings) -> None:
    """Amorce l'activation de la sonde RTT : base prioritaire, sinon RTT_ENABLED.

    Meme logique que le drapeau d'enforcement : une fois pose en base, c'est lui
    qui fait foi, et l'interface le bascule sans redemarrage.
    """
    from app.services.collection import FLAG_RTT

    collection.rtt_enabled = settings.rtt_enabled
    if topology_repo is None:
        return
    try:
        stored = await topology_repo.get_flag(FLAG_RTT)
    except Exception:  # noqa: BLE001 - table pas encore creee
        return
    if stored is None:
        try:
            await topology_repo.set_flag(
                FLAG_RTT,
                settings.rtt_enabled,
                updated_by="bootstrap",
                reason="valeur initiale issue de RTT_ENABLED",
            )
        except Exception:  # noqa: BLE001
            pass
        return
    collection.rtt_enabled = stored
    if stored:
        logger.info("Sonde RTT ACTIVE d'apres la base (/ping depuis le PoP).")


async def _warn_if_secrets_orphaned(database: Database, key_file: Path | None) -> None:
    """Alerte si une cle NEUVE arrive alors que des secrets sont deja stockes.

    C'est le scenario catastrophe : fichier de cle perdu (volume non monte,
    conteneur recree), donc mots de passe de routeurs devenus indechiffrables.
    Le controleur continue de tourner — l'inventaire fichier n'est pas concerne —
    mais l'operateur doit le savoir tout de suite, pas le decouvrir au prochain
    cycle de collecte.
    """
    try:
        async with database.pool.acquire() as conn:
            existants = await conn.fetchval("SELECT count(*) FROM routers")
    except Exception:  # noqa: BLE001 - table pas encore creee au tout premier demarrage
        return
    if existants:
        logger.error(
            "Une NOUVELLE cle de chiffrement vient d'etre generee alors que %d "
            "routeur(s) sont deja enregistres : leurs mots de passe sont "
            "desormais illisibles. Restaurez l'ancien fichier de cle (%s) ou "
            "resaisissez ces mots de passe dans l'interface.",
            existants,
            key_file,
        )


async def shutdown_container(container: Container) -> None:
    await container.scheduler.stop()
    if container.netflow is not None:
        # Une derniere fenetre est ecrite a l'arret : sans elle, un redemarrage
        # quotidien perdrait une minute de trafic par jour.
        await container.netflow.stop()
    container.shaping.close()
    container.registry.close_all()
    await container.collection.aclose()
    await container.database.close()

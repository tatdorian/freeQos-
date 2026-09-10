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
from app.db.database import Database
from app.db.directory import Directory, PgDirectory
from app.db.repository import MetricsRepository
from app.db.routers_repo import RoutersRepository
from app.db.topology_repo import TopologyRepository
from app.db.writer import MetricsWriter, PgMetricsWriter
from app.models import Plan
from app.scheduler import Scheduler
from app.services.collection import (
    JOB_BACKHAULS,
    JOB_BOOSTS,
    JOB_INVENTORY,
    JOB_LINKS,
    JOB_PLANS,
    JOB_RECONCILE,
    JOB_RTT,
    JOB_SUBSCRIBERS,
    CollectionService,
)
from app.services.crypto import KeySource, SecretBox, load_or_create_key
from app.services.registry import RouterRegistry
from app.services.rtt import RttProber
from app.services.shaping import ShapingService

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
    routers_repo: RoutersRepository | None = None
    topology_repo: TopologyRepository | None = None
    antennas_repo: AntennasRepository | None = None
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
    antennas_repo = AntennasRepository(database.pool, secrets)
    # Provider des antennes ajoutees depuis l'interface : il relit sa liste dans
    # la base a chaque cycle, donc un ajout est collecte sans redemarrage.
    antennas_provider = DbAirOsProvider(
        antennas_repo.load_targets, timeout_s=settings.airos_timeout_s
    )
    registry = RouterRegistry(settings, repository=routers_repo)
    shaping = ShapingService(
        settings, registry=registry, repository=topology_repo, metrics=repository
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
    )

    # Amorce le drapeau de la sonde RTT : la base fait foi une fois posee, sinon
    # on l'y ecrit depuis RTT_ENABLED. Ensuite, l'interface le bascule a chaud.
    await _bootstrap_rtt_flag(collection, topology_repo, settings)

    async def reload_inventory() -> None:
        collection.set_collectors(await registry.reload())

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
        routers_repo=routers_repo,
        topology_repo=topology_repo,
        antennas_repo=antennas_repo,
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
    container.shaping.close()
    container.registry.close_all()
    await container.collection.aclose()
    await container.database.close()

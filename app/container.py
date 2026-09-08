"""Assemblage des dependances.

Toute la construction d'objets est isolee ici : les tests peuvent monter un
conteneur partiel (writer memoire, faux routeur, provider mock) sans toucher a
FastAPI ni a PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.collectors.radius import (
    FreeradiusSqlPlanProvider,
    MockPlanProvider,
    PlanProvider,
)
from app.collectors.uisp import BackhaulCapacityProvider, MockBackhaulProvider, UispProvider
from app.config import Settings
from app.db.database import Database
from app.db.directory import Directory, PgDirectory
from app.db.repository import MetricsRepository
from app.db.routers_repo import RoutersRepository
from app.db.writer import MetricsWriter, PgMetricsWriter
from app.models import Plan
from app.scheduler import Scheduler
from app.services.collection import (
    JOB_BACKHAULS,
    JOB_INVENTORY,
    JOB_PLANS,
    JOB_RTT,
    JOB_SUBSCRIBERS,
    CollectionService,
)
from app.services.crypto import SecretBox
from app.services.registry import RouterRegistry
from app.services.rtt import RttProber

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
    routers_repo: RoutersRepository | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


async def build_container(settings: Settings) -> Container:
    if settings.enforcement_enabled:
        # Garde-fou explicite : la phase 2 n'existe pas encore, et l'application
        # doit rester strictement observatrice tant qu'elle n'est pas ecrite.
        raise RuntimeError(
            "ENFORCEMENT_ENABLED=true mais aucun enforcement n'est implemente "
            "(phase 2). Le controleur reste en lecture seule."
        )

    database = Database(
        settings.asyncpg_dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        command_timeout=settings.db_command_timeout_s,
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

    secrets = SecretBox(settings.app_secret_key)
    if not secrets.available:
        # Non bloquant : l'inventaire fichier fonctionne sans cle. Seul l'ajout
        # de PoP depuis l'interface est indisponible.
        logger.warning("Ajout de PoP par l'interface desactive : %s", secrets.unavailable_reason)
    routers_repo = RoutersRepository(database.pool, secrets)
    registry = RouterRegistry(settings, repository=routers_repo)

    rtt_prober = None
    if settings.rtt_enabled:
        logger.info(
            "Sonde de latence active : %d abonne(s) toutes les %gs (/ping depuis le PoP)",
            settings.rtt_batch_size,
            settings.rtt_interval_s,
        )
        rtt_prober = RttProber(
            batch_size=settings.rtt_batch_size,
            max_age_s=settings.rtt_max_age_s,
            count=settings.rtt_count,
        )

    collection = CollectionService(
        settings,
        collectors=await registry.reload(),
        backhaul_provider=backhaul_provider,
        plan_provider=plan_provider,
        directory=directory,
        writer=writer,
        rtt_prober=rtt_prober,
    )

    async def reload_inventory() -> None:
        collection.set_collectors(await registry.reload())

    scheduler = Scheduler()
    scheduler.add_job(
        JOB_SUBSCRIBERS, settings.subscriber_interval_s, collection.collect_subscribers
    )
    scheduler.add_job(JOB_BACKHAULS, settings.backhaul_interval_s, collection.collect_backhauls)
    scheduler.add_job(JOB_PLANS, settings.plan_refresh_interval_s, collection.refresh_plans)
    scheduler.add_job(JOB_INVENTORY, settings.inventory_refresh_interval_s, reload_inventory)
    if rtt_prober is not None:
        scheduler.add_job(JOB_RTT, settings.rtt_interval_s, collection.probe_rtt)

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
        routers_repo=routers_repo,
    )


async def shutdown_container(container: Container) -> None:
    await container.scheduler.stop()
    container.registry.close_all()
    await container.collection.aclose()
    await container.database.close()

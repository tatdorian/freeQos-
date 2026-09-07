"""Tests d'integration base de donnees.

Ignores par defaut : ils ne s'executent que si ``TEST_DATABASE_URL`` designe une
base joignable. Ils valident le SQL reel (schema, vues, date_bin, upserts) que
les doubles memoire ne peuvent pas verifier.

    export TEST_DATABASE_URL=postgresql://qos:qos@localhost:5432/qos_test
    pytest tests/test_db_integration.py

Ils passent aussi bien sur TimescaleDB que sur un PostgreSQL nu : le schema
degrade proprement en tables classiques quand l'extension est absente.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db.database import Database
from app.db.directory import PgDirectory
from app.db.repository import MetricsRepository
from app.db.writer import PgMetricsWriter
from app.models import BackhaulSample, Plan, RunResult, SubscriberSample

DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL non defini : tests d'integration ignores"
)

NOW = datetime.now(tz=timezone.utc).replace(microsecond=0)


@pytest.fixture
async def database():
    db = Database(DSN, min_size=1, max_size=4)
    await db.connect(retries=1)
    await db.migrate()
    await db.apply_policies(compression_after_days=7, retention_days=90)
    async with db.pool.acquire() as conn:
        # Repartir d'une base propre a chaque test.
        await conn.execute(
            "TRUNCATE subscriber_metrics, backhaul_metrics, qoe_scores, "
            "collector_runs, subscribers, backhauls, pops RESTART IDENTITY CASCADE"
        )
    yield db
    await db.close()


async def test_le_schema_s_applique_et_est_rejouable(database: Database) -> None:
    """Idempotence : le schema est rejoue a chaque demarrage de l'application."""
    await database.migrate()
    await database.migrate()

    async with database.pool.acquire() as conn:
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    assert {
        "pops",
        "subscribers",
        "backhauls",
        "subscriber_metrics",
        "backhaul_metrics",
        "qoe_scores",
        "collector_runs",
    } <= tables


async def test_referentiel_upsert(database: Database) -> None:
    directory = PgDirectory(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    assert await directory.ensure_pop("PoP Nord") == pop_id  # idempotent

    plan = Plan(down_mbps=100, up_mbps=20, source="mock:test")
    subscriber_id = await directory.ensure_subscriber("dupont", pop_id=pop_id, plan=plan)
    directory.clear_cache()
    assert await directory.ensure_subscriber("dupont") == subscriber_id

    async with database.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM subscribers WHERE id = $1", subscriber_id)
    # Un second passage sans plan ne doit pas ecraser le plan deja connu.
    assert row["plan_down_mbps"] == 100
    assert row["pop_id"] == pop_id


async def test_ecriture_et_relecture_des_metriques(database: Database) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    subscriber_id = await directory.ensure_subscriber(
        "dupont", pop_id=pop_id, plan=Plan(100, 20, "mock")
    )

    rows = [
        (
            subscriber_id,
            SubscriberSample(
                ts=NOW - timedelta(seconds=10 * i),
                login="dupont",
                router_name="pop-nord",
                pop_name="PoP Nord",
                address="10.20.0.10",
                uptime_s=3600 + i,
                rx_bytes=1000 * i,
                tx_bytes=5000 * i,
                rx_bps=1_000_000.0 * i,
                tx_bps=5_000_000.0 * i,
            ),
        )
        for i in range(6)
    ]
    assert await writer.write_subscriber_metrics(rows) == 6

    # Idempotence : rejouer le meme lot ne duplique rien (PK (subscriber_id, ts)).
    await writer.write_subscriber_metrics(rows)
    async with database.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM subscriber_metrics") == 6

    points = await repo.subscriber_metrics(
        subscriber_id,
        start=NOW - timedelta(minutes=5),
        end=NOW + timedelta(seconds=1),
        bucket_seconds=60,
    )
    # Les buckets sont alignes sur l'epoch : les 6 echantillons peuvent tomber
    # dans un ou deux buckets selon l'heure d'execution. On verifie donc
    # l'agregation, pas le decoupage.
    assert points
    assert sum(point["samples"] for point in points) == 6
    assert max(point["tx_bps_max"] for point in points) == 25_000_000.0
    assert points == sorted(points, key=lambda point: point["bucket"])


async def test_vue_dernier_echantillon(database: Database) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    petit = await directory.ensure_subscriber("petit", pop_id=pop_id)
    gros = await directory.ensure_subscriber("gros", pop_id=pop_id)

    def sample(ts: datetime, rx: float, tx: float) -> SubscriberSample:
        return SubscriberSample(
            ts=ts, login="x", router_name="r", pop_name="p", rx_bps=rx, tx_bps=tx
        )

    await writer.write_subscriber_metrics(
        [
            (petit, sample(NOW - timedelta(seconds=10), 1e6, 2e6)),
            (petit, sample(NOW, 1.5e6, 3e6)),
            (gros, sample(NOW - timedelta(seconds=10), 50e6, 90e6)),
            (gros, sample(NOW, 60e6, 95e6)),
        ]
    )

    latest = await repo.subscriber_latest(limit=10)
    assert [row["pppoe_login"] for row in latest] == ["gros", "petit"]
    # Seul le dernier point de chaque abonne remonte.
    assert latest[0]["tx_bps"] == 95e6
    assert latest[0]["ts"] == NOW


async def test_metriques_backhaul(database: Database) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    backhaul_id = await directory.ensure_backhaul(
        "bh-nord", pop_id=pop_id, uisp_device_id="dev-1", nominal_capacity_mbps=500
    )

    await writer.write_backhaul_metrics(
        [
            (
                backhaul_id,
                BackhaulSample(
                    ts=NOW - timedelta(seconds=30),
                    device_id="dev-1",
                    capacity_mbps=480,
                    capacity_down_mbps=360,
                    capacity_up_mbps=120,
                    signal_dbm=-50,
                    airtime_pct=30,
                ),
            ),
            (
                backhaul_id,
                BackhaulSample(
                    ts=NOW,
                    device_id="dev-1",
                    capacity_mbps=180,  # fade
                    capacity_down_mbps=135,
                    capacity_up_mbps=45,
                    signal_dbm=-72,
                    airtime_pct=68,
                ),
            ),
        ]
    )

    latest = await repo.backhaul_latest()
    assert latest[0]["capacity_mbps"] == 180

    points = await repo.backhaul_metrics(
        backhaul_id,
        start=NOW - timedelta(minutes=5),
        end=NOW + timedelta(seconds=1),
        bucket_seconds=300,
    )
    # Le creux de capacite est ce qui contraint le debit parent du shaping.
    assert points[-1]["capacity_mbps_min"] == 180


async def test_historique_des_cycles_et_compteurs(database: Database) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    await directory.ensure_subscriber("dupont", pop_id=pop_id)
    await writer.record_run(
        RunResult(
            job="collect_subscribers",
            started_at=NOW,
            duration_s=0.12,
            ok=False,
            items=3,
            errors=["pop-sud injoignable"],
        )
    )

    runs = await repo.recent_runs(limit=5)
    assert runs[0]["ok"] is False
    assert runs[0]["error"] == "pop-sud injoignable"

    counters = await repo.counters()
    assert counters["pops"] == 1
    assert counters["subscribers"] == 1


async def test_touch_et_mise_a_jour_des_plans(database: Database) -> None:
    directory = PgDirectory(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    subscriber_id = await directory.ensure_subscriber("dupont", pop_id=pop_id)

    await directory.touch_subscribers({subscriber_id: ("10.20.0.42", NOW)})
    await directory.update_plans({subscriber_id: Plan(300, 50, "radius:user")})

    row = await repo.get_subscriber(subscriber_id)
    assert row is not None
    assert row["last_ip"] == "10.20.0.42"
    assert row["plan_down_mbps"] == 300
    assert row["plan_source"] == "radius:user"


async def test_suppression_en_cascade(database: Database) -> None:
    """Supprimer un abonne doit emporter ses metriques : sinon la retention
    Timescale laisserait des orphelins impossibles a rattacher."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    subscriber_id = await directory.ensure_subscriber("temporaire", pop_id=pop_id)
    await writer.write_subscriber_metrics(
        [
            (
                subscriber_id,
                SubscriberSample(
                    ts=NOW, login="temporaire", router_name="r", pop_name="p", rx_bps=1.0
                ),
            )
        ]
    )

    async with database.pool.acquire() as conn:
        await conn.execute("DELETE FROM subscribers WHERE id = $1", subscriber_id)
        assert await conn.fetchval("SELECT count(*) FROM subscriber_metrics") == 0


async def test_bout_en_bout_routeur_vers_api(database: Database) -> None:
    """Le chemin complet, avec la vraie base : faux routeur -> service de
    collecte -> TimescaleDB -> requetes de lecture de l'API."""
    from app.collectors.mikrotik import MikrotikCollector
    from app.collectors.radius import MockPlanProvider
    from app.collectors.uisp import MockBackhaulProvider
    from app.config import BackhaulConfig, RouterConfig, Settings
    from app.services.collection import CollectionService
    from tests.conftest import FakeRouterOsClient
    from tests.test_collection_service import Clock

    router = RouterConfig(
        name="pop-nord", host="192.0.2.11", password="lab", pop_name="PoP Nord"
    )
    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[router],
        backhauls=[
            BackhaulConfig(
                name="bh-nord",
                pop_name="PoP Nord",
                uisp_device_id="dev-1",
                nominal_capacity_mbps=500,
            )
        ],
        scheduler_enabled=False,
        db_auto_migrate=False,
    )

    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0, uptime="01:00:00")
    client.add_session("martin", rx_byte=0, tx_byte=0, uptime="00:30:00")

    clock = Clock()
    service = CollectionService(
        settings,
        collectors=[MikrotikCollector(router, client=client)],
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=PgDirectory(database.pool),
        writer=PgMetricsWriter(database.pool),
        clock=clock,
    )

    # Premier cycle : referentiel cree, compteurs memorises, pas encore de debit.
    first = await service.collect_subscribers()
    assert first.ok and first.items == 2

    # Deuxieme cycle 10 s plus tard : dupont a consomme 12,5 Mo / 25 Mo.
    clock.advance(10.0)
    client.advance("dupont", rx_delta=12_500_000, tx_delta=25_000_000, uptime="01:00:10")
    second = await service.collect_subscribers()
    assert second.ok and second.items == 2

    assert (await service.collect_backhauls()).items == 1

    repo = MetricsRepository(database.pool)

    latest = await repo.subscriber_latest(limit=10)
    par_login = {row["pppoe_login"]: row for row in latest}
    assert par_login["dupont"]["rx_bps"] == 10_000_000.0  # upload abonne
    assert par_login["dupont"]["tx_bps"] == 20_000_000.0  # download abonne
    assert par_login["martin"]["rx_bps"] == 0.0           # en ligne mais inactif
    # Le classement top talkers place le plus consommateur en tete.
    assert latest[0]["pppoe_login"] == "dupont"

    # Le plan a bien ete resolu et rattache a l'abonne.
    assert par_login["dupont"]["plan_down_mbps"] > 0

    pops = await repo.list_pops()
    assert pops[0]["name"] == "PoP Nord"
    assert pops[0]["subscriber_count"] == 2

    backhauls = await repo.backhaul_latest()
    assert backhauls[0]["name"] == "bh-nord"
    assert backhauls[0]["capacity_mbps"] > 0

    counters = await repo.counters()
    assert counters["subscribers"] == 2
    assert counters["active_subscribers"] == 2

    runs = await repo.recent_runs()
    assert {run["job"] for run in runs} == {"collect_subscribers", "collect_backhauls"}

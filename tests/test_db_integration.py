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
from datetime import UTC, datetime, timedelta

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

NOW = datetime.now(tz=UTC).replace(microsecond=0)


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
            "collector_runs, subscribers, backhauls, routers, pops, "
            "topology_nodes, topology_links, subscriber_attachments, "
            "shaping_policies, enforcement_audit, runtime_flags "
            "RESTART IDENTITY CASCADE"
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
        "routers",
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

    router = RouterConfig(name="pop-nord", host="192.0.2.11", password="lab", pop_name="PoP Nord")
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
    assert par_login["martin"]["rx_bps"] == 0.0  # en ligne mais inactif
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


# ---------------------------------------------------------------------------
# Inventaire dynamique des routeurs
# ---------------------------------------------------------------------------


async def test_cycle_de_vie_d_un_routeur_en_base(database: Database) -> None:
    from app.db.routers_repo import (
        DuplicateRouterError,
        RouterNotFoundError,
        RoutersRepository,
    )
    from app.services.crypto import SecretBox, generate_key

    secrets = SecretBox(generate_key())
    repo = RoutersRepository(database.pool, secrets)

    created = await repo.create(
        {"name": "pop-nord", "host": "10.10.0.11", "pop_name": "PoP Nord"},
        "mot-de-passe-du-routeur",
    )
    assert created["name"] == "pop-nord"
    assert "password_enc" not in created  # jamais renvoye

    # Le secret est chiffre au repos.
    async with database.pool.acquire() as conn:
        stocke = await conn.fetchval(
            "SELECT password_enc FROM routers WHERE id = $1", created["id"]
        )
    assert "mot-de-passe-du-routeur" not in stocke
    assert stocke.startswith("fernet:")

    # Il redevient utilisable pour construire un collecteur.
    configs = await repo.load_configs()
    assert len(configs) == 1
    assert configs[0].resolve_password() == "mot-de-passe-du-routeur"

    with pytest.raises(DuplicateRouterError):
        await repo.create({"name": "pop-nord", "host": "10.10.0.99"}, "x")

    # Modification partielle : le secret n'est pas touche.
    updated = await repo.update(created["id"], {"host": "10.10.0.12"})
    assert updated["host"] == "10.10.0.12"
    assert (await repo.load_configs())[0].resolve_password() == "mot-de-passe-du-routeur"

    await repo.record_success(created["id"], {"identity": "chr-nord", "version": "7.21.5"})
    assert (await repo.get_public(created["id"]))["identity"] == "chr-nord"

    await repo.record_failure(created["id"], "connexion refusee")
    assert (await repo.get_public(created["id"]))["last_error"] == "connexion refusee"

    await repo.delete(created["id"])
    with pytest.raises(RouterNotFoundError):
        await repo.get_public(created["id"])


async def test_routeur_dont_le_secret_est_illisible_est_ecarte(database: Database) -> None:
    """Cle changee ou valeur alteree : ce routeur est ecarte avec un diagnostic,
    les autres continuent de tourner."""
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    repo = RoutersRepository(database.pool, SecretBox(generate_key()))
    bon = await repo.create({"name": "pop-bon", "host": "10.10.0.11"}, "secret")
    casse = await repo.create({"name": "pop-casse", "host": "10.10.0.12"}, "secret")

    async with database.pool.acquire() as conn:
        await conn.execute(
            "UPDATE routers SET password_enc = 'en-clair-anomalie' WHERE id = $1", casse["id"]
        )

    configs = await repo.load_configs()

    assert [c.name for c in configs] == ["pop-bon"]
    assert (await repo.get_public(casse["id"]))["last_error"] is not None
    assert (await repo.get_public(bon["id"]))["last_error"] is None


# ---------------------------------------------------------------------------
# Vues du tableau de bord
# ---------------------------------------------------------------------------


async def test_vues_du_tableau_de_bord(database: Database) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    backhaul_id = await directory.ensure_backhaul(
        "bh-nord", pop_id=pop_id, uisp_device_id="dev-1", nominal_capacity_mbps=500
    )
    a = await directory.ensure_subscriber("alice", pop_id=pop_id, plan=Plan(100, 20, "mock"))
    b = await directory.ensure_subscriber("bob", pop_id=pop_id, plan=Plan(300, 50, "mock"))

    def sample(ts: datetime, rx: float, tx: float) -> SubscriberSample:
        return SubscriberSample(
            ts=ts, login="x", router_name="r", pop_name="PoP Nord", rx_bps=rx, tx_bps=tx
        )

    await writer.write_subscriber_metrics(
        [
            (a, sample(NOW - timedelta(seconds=20), 1e6, 10e6)),
            (a, sample(NOW, 2e6, 20e6)),
            (b, sample(NOW - timedelta(seconds=20), 5e6, 50e6)),
            (b, sample(NOW, 6e6, 60e6)),
        ]
    )
    await writer.write_backhaul_metrics(
        [
            (
                backhaul_id,
                BackhaulSample(
                    ts=NOW,
                    device_id="dev-1",
                    capacity_mbps=400,
                    signal_dbm=-55,
                    airtime_pct=40,
                ),
            )
        ]
    )
    await directory.touch_subscribers({a: ("10.0.0.1", NOW), b: ("10.0.0.2", NOW)})

    overview = await repo.overview()
    assert overview["online"] == 2
    # Somme des derniers echantillons : 20 + 60 Mbps.
    assert overview["tx_bps"] == 80e6
    assert overview["sold_down_mbps"] == 400.0
    assert overview["backhaul_capacity_mbps"] == 400.0

    tree = await repo.network_tree()
    assert tree[0]["name"] == "PoP Nord"
    assert tree[0]["online"] == 2
    assert tree[0]["tx_bps"] == 80e6
    assert tree[0]["backhauls"][0]["capacity_mbps"] == 400.0

    series = await repo.throughput_series(
        start=NOW - timedelta(minutes=5), end=NOW + timedelta(seconds=1), bucket_seconds=300
    )
    # Un seul bucket : la moyenne par abonne est sommee, jamais les echantillons
    # bruts (sinon un abonne a deux mesures compterait double).
    assert len(series) == 1
    assert series[0]["tx_bps"] == pytest.approx(15e6 + 55e6)
    assert series[0]["subscribers"] == 2


async def test_throughput_ne_double_compte_pas(database: Database) -> None:
    """Regression : sommer directement les echantillons gonflerait le total des
    que le bucket depasse la periode de collecte."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    seul = await directory.ensure_subscriber("solo", pop_id=pop_id)

    # Six echantillons a 100 Mbps dans le meme bucket d'une minute.
    await writer.write_subscriber_metrics(
        [
            (
                seul,
                SubscriberSample(
                    ts=NOW - timedelta(seconds=10 * i),
                    login="solo",
                    router_name="r",
                    pop_name="p",
                    rx_bps=0.0,
                    tx_bps=100e6,
                ),
            )
            for i in range(6)
        ]
    )

    series = await repo.throughput_series(
        start=NOW - timedelta(minutes=5), end=NOW + timedelta(seconds=1), bucket_seconds=300
    )

    # Les buckets sont alignes sur l'epoch : les echantillons peuvent se repartir
    # sur deux buckets selon l'heure d'execution. Ce qui compte est que CHACUN
    # vaille 100 Mbps et non un multiple : sommer les echantillons bruts
    # donnerait 600 Mbps.
    assert series
    assert all(point["tx_bps"] == pytest.approx(100e6) for point in series)
    assert all(point["subscribers"] == 1 for point in series)


# ---------------------------------------------------------------------------
# Topologie et enforcement (phase 2)
# ---------------------------------------------------------------------------


async def test_persistance_de_la_topologie(database: Database) -> None:
    from app.collectors.topology import TopologySnapshot, build_from_router
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot,
        router_name="pop-nord",
        pop_name="PoP Nord",
        host="10.10.0.11",
        neighbors=[
            {
                "interface": "ether1",
                "identity": "gw",
                "mac-address": "AA:BB:CC:00:00:01",
                "platform": "MikroTik",
            },
            {
                "interface": "ether2",
                "identity": "bh",
                "mac-address": "DC:9F:DB:11:22:33",
                "platform": "Ubiquiti",
            },
        ],
        interfaces=[{"name": "ether1", "type": "ether"}],
        ethernet=[{"name": "ether1", "speed": "1Gbps"}, {"name": "ether2", "rate": "100Mbps"}],
        addresses=[{"interface": "ether2", "address": "10.50.0.1/30"}],
    )

    compte = await repo.save_snapshot(snapshot)
    assert compte["nodes"] == 3 and compte["links"] == 2

    noeuds = {n["key"]: n for n in await repo.nodes()}
    assert noeuds["router:pop-nord"]["kind"] == "pop"
    assert noeuds["mac:DC:9F:DB:11:22:33"]["kind"] == "radio"
    assert noeuds["router:pop-nord"]["fresh"] is True

    liens = {lk["interface"]: lk for lk in await repo.links()}
    assert liens["ether1"]["capacity_mbps"] == 1000.0
    assert liens["ether2"]["target_name"] == "bh"


async def test_la_decouverte_est_idempotente(database: Database) -> None:
    """Rejouee toutes les 15 minutes : elle ne doit pas dupliquer le graphe."""
    from app.collectors.topology import TopologySnapshot, build_from_router
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)

    def snapshot():
        s = TopologySnapshot()
        build_from_router(
            s,
            router_name="pop-nord",
            pop_name="PoP Nord",
            host="10.10.0.11",
            neighbors=[
                {
                    "interface": "ether1",
                    "identity": "gw",
                    "mac-address": "AA:BB:CC:00:00:01",
                    "platform": "MikroTik",
                }
            ],
            interfaces=[],
            ethernet=[{"name": "ether1", "speed": "1Gbps"}],
            addresses=[],
        )
        return s

    await repo.save_snapshot(snapshot())
    await repo.save_snapshot(snapshot())

    assert len(await repo.nodes()) == 2
    assert len(await repo.links()) == 1


async def test_un_equipement_disparu_reste_dans_le_graphe(database: Database) -> None:
    """Un fade ou un redemarrage ne doit pas effacer un lien : c'est last_seen
    qui dit ce qui est frais, pas la presence de la ligne."""
    from app.collectors.topology import TopologySnapshot, build_from_router
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)
    complet = TopologySnapshot()
    build_from_router(
        complet,
        router_name="pop",
        pop_name="P",
        host="h",
        neighbors=[
            {"interface": "e1", "identity": "a", "mac-address": "AA:BB:CC:00:00:01"},
            {"interface": "e2", "identity": "b", "mac-address": "AA:BB:CC:00:00:02"},
        ],
        interfaces=[],
        ethernet=[],
        addresses=[],
    )
    await repo.save_snapshot(complet)

    partiel = TopologySnapshot()
    build_from_router(
        partiel,
        router_name="pop",
        pop_name="P",
        host="h",
        neighbors=[{"interface": "e1", "identity": "a", "mac-address": "AA:BB:CC:00:00:01"}],
        interfaces=[],
        ethernet=[],
        addresses=[],
    )
    await repo.save_snapshot(partiel)

    assert len(await repo.links()) == 2


async def test_correction_manuelle_du_role_prime(database: Database) -> None:
    from app.collectors.topology import TopologyNode, TopologySnapshot
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="mac:AA", name="mystere", kind="unknown"))
    await repo.save_snapshot(snapshot)

    await repo.set_node_kind("mac:AA", "sector")
    noeud = (await repo.nodes())[0]

    assert noeud["kind"] == "sector"  # ce que voit l'interface
    assert noeud["kind_detected"] == "unknown"  # ce que l'heuristique avait trouve

    # Et on peut revenir a la detection automatique.
    await repo.set_node_kind("mac:AA", None)
    assert (await repo.nodes())[0]["kind"] == "unknown"


async def test_cycle_de_vie_d_une_surcharge(database: Database) -> None:
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)

    await repo.upsert_policy(
        scope="link",
        target_key="lien-1",
        max_down_mbps=300,
        max_up_mbps=100,
        note="bride pendant travaux",
        updated_by="ui",
    )
    # Le meme couple (scope, cible) se met a jour, il ne se duplique pas.
    await repo.upsert_policy(scope="link", target_key="lien-1", max_down_mbps=500, max_up_mbps=200)

    politiques = await repo.policies("link")
    assert len(politiques) == 1
    assert politiques[0]["max_down_mbps"] == 500

    carte = await repo.policy_map("link")
    assert carte["lien-1"]["max_up_mbps"] == 200

    assert await repo.delete_policy("link", "lien-1") is True
    assert await repo.delete_policy("link", "lien-1") is False


async def test_journal_des_commandes(database: Database) -> None:
    """La trace dont on a besoin le jour ou il faut expliquer un changement."""
    from app.db.topology_repo import TopologyRepository
    from app.enforcement.models import PlanAction

    repo = TopologyRepository(database.pool)
    action = PlanAction(
        verb="set",
        path="/queue/simple",
        fields={"max-limit": "20000000/100000000"},
        target_id="*7",
        name="freeqos-dupont",
    )

    await repo.record_audit("pop-nord", dry_run=False, outcomes=[(action, True, "*7")])

    lignes = await repo.audit(limit=10)
    assert len(lignes) == 1
    assert lignes[0]["router_name"] == "pop-nord"
    assert lignes[0]["dry_run"] is False
    assert lignes[0]["command"].startswith("/queue/simple/set")
    assert "max-limit=20000000/100000000" in lignes[0]["command"]


async def test_cycle_de_vie_d_un_boost(database: Database) -> None:
    """Pose, expiration, purge : le cycle complet contre le vrai SQL."""
    from datetime import UTC, datetime, timedelta

    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)

    # Un boost encore valide.
    await repo.set_boost(
        scope="subscriber",
        target_key="alice",
        down_mbps=500,
        up_mbps=100,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        reason="geste commercial",
        updated_by="ui",
    )
    # Un boost deja echu.
    await repo.set_boost(
        scope="subscriber",
        target_key="bob",
        down_mbps=300,
        up_mbps=None,
        expires_at=datetime.now(tz=UTC) - timedelta(minutes=5),
    )

    actifs = await repo.active_boosts()
    assert [b["target_key"] for b in actifs] == ["alice"]
    assert actifs[0]["seconds_left"] > 3500
    assert actifs[0]["boost_reason"] == "geste commercial"

    echus = await repo.expired_boosts()
    assert [b["target_key"] for b in echus] == ["bob"]

    assert await repo.purge_expired_boosts() == 1
    assert await repo.expired_boosts() == []
    # Le boost valide n'a pas ete emporte.
    assert len(await repo.active_boosts()) == 1

    # Le boost cohabite avec une surcharge permanente sans l'ecraser.
    await repo.upsert_policy(
        scope="subscriber", target_key="alice", max_down_mbps=150, max_up_mbps=30
    )
    politique = (await repo.policies("subscriber"))[0]
    assert politique["max_down_mbps"] == 150
    assert politique["boost_down_mbps"] == 500

    assert await repo.clear_boost("subscriber", "alice") is True
    assert await repo.clear_boost("subscriber", "alice") is False
    # Retirer le boost laisse la surcharge en place.
    assert (await repo.policies("subscriber"))[0]["max_down_mbps"] == 150


async def test_drapeaux_runtime(database: Database) -> None:
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)

    assert await repo.get_flag("enforcement_enabled") is None

    await repo.set_flag("enforcement_enabled", True, updated_by="ui", reason="bascule de nuit")
    assert await repo.get_flag("enforcement_enabled") is True

    detail = await repo.flag_detail("enforcement_enabled")
    assert detail["reason"] == "bascule de nuit"
    assert detail["updated_by"] == "ui"

    await repo.set_flag("enforcement_enabled", False, updated_by="ui")
    assert await repo.get_flag("enforcement_enabled") is False


async def test_seconds_left_est_un_nombre(database: Database) -> None:
    """EXTRACT(EPOCH ...) renvoie un Decimal, serialise en CHAINE par l'API :
    l'interface ferait alors une division sur du texte."""
    from datetime import UTC, datetime, timedelta

    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)
    await repo.set_boost(
        scope="subscriber",
        target_key="alice",
        down_mbps=500,
        up_mbps=None,
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
    )

    restant = (await repo.active_boosts())[0]["seconds_left"]

    assert isinstance(restant, float)
    assert 1700 < restant < 1801


async def test_suppression_d_un_pop_emporte_ses_donnees(database: Database) -> None:
    """Retirer un routeur de l'inventaire ne suffit pas : le site, ses abonnes et
    leur historique restent en base tant qu'on ne fait pas le menage."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    garde = await directory.ensure_pop("Site conserve")
    jetable = await directory.ensure_pop("Site obsolete", "10.10.0.99")
    reste = await directory.ensure_subscriber("reste", pop_id=garde)
    part = await directory.ensure_subscriber("part", pop_id=jetable)
    await directory.ensure_backhaul("bh-obsolete", pop_id=jetable, uisp_device_id="dev-x")

    for abonne in (reste, part):
        await writer.write_subscriber_metrics(
            [
                (
                    abonne,
                    SubscriberSample(ts=NOW, login="x", router_name="r", pop_name="p", rx_bps=1.0),
                )
            ]
        )

    emporte = await repo.delete_pop(jetable)

    assert emporte == {"subscribers": 1, "backhauls": 1}
    async with database.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM pops") == 1
        assert await conn.fetchval("SELECT count(*) FROM subscribers") == 1
        assert await conn.fetchval("SELECT count(*) FROM backhauls") == 0
        # Les metriques de l'abonne parti suivent, celles de l'autre restent.
        assert await conn.fetchval("SELECT count(*) FROM subscriber_metrics") == 1

    with pytest.raises(LookupError):
        await repo.delete_pop(jetable)

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

import asyncpg
import pytest

from app.collectors.topology import TopologyLink, TopologyNode, TopologySnapshot
from app.db.database import Database
from app.db.directory import PgDirectory
from app.db.repository import MetricsRepository
from app.db.static_clients_repo import (
    DuplicateStaticClientError,
    StaticClientNotFoundError,
    StaticClientsRepository,
    VlanSightingsRepository,
)
from app.db.topology_repo import TopologyRepository
from app.db.writer import PgMetricsWriter
from app.models import (
    BackhaulSample,
    InterfaceSample,
    Plan,
    RunResult,
    SubscriberSample,
    VlanSighting,
)

DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL non defini : tests d'integration ignores"
)


def _maintenant() -> datetime:
    """Horodatage de reference, recalcule a CHAQUE test.

    Une constante figee a l'import derivait de l'heure de PostgreSQL au fil de
    la suite : les vues qui filtrent sur "les deux dernieres minutes" finissaient
    par ne plus voir les echantillons ecrits par le test. Flake garanti le jour
    ou la suite ralentit.
    """
    return datetime.now(tz=UTC).replace(microsecond=0)


def _ancre_dans_un_seul_pas(now: datetime, *, bucket_seconds: int, recul_s: int) -> datetime:
    """Recule ``now`` juste assez pour que ``now`` ET ``now - recul_s`` tombent
    dans le MEME pas de ``date_bin``.

    ``date_bin`` aligne ses pas sur l'EPOCH, pas sur l'heure du test : deux
    echantillons distants de 20 s se retrouvent dans deux pas differents des que
    la suite tourne dans les 20 premieres secondes d'un pas de 5 minutes. C'est
    le comportement attendu de la requete, pas un bug -- mais un test qui COMPTE
    les pas doit s'en affranchir, sinon il echoue une fois sur quinze sans rien
    dire de la logique qu'il verifie.

    Le recul vaut au plus ``recul_s + 1`` secondes : les filtres de fraicheur des
    vues (2 et 5 minutes) ne s'en apercoivent pas.
    """
    reste = int(now.timestamp()) % bucket_seconds
    if reste >= recul_s:
        return now
    return now - timedelta(seconds=reste + 1)


@pytest.fixture
def now() -> datetime:
    return _maintenant()


@pytest.fixture
async def database():
    db = Database(DSN, min_size=1, max_size=4)
    await db.connect(retries=1)
    await db.migrate()
    await db.apply_policies(compression_after_days=7, retention_days=90)
    async with db.pool.acquire() as conn:
        # Repartir d'une base propre a chaque test.
        await conn.execute(
            "TRUNCATE subscriber_metrics, backhaul_metrics, interface_metrics, "
            "qoe_scores, collector_runs, subscribers, backhauls, routers, "
            "airos_antennas, pops, "
            "topology_nodes, topology_links, subscriber_attachments, "
            "shaping_policies, enforcement_audit, runtime_flags, runtime_settings, "
            "static_clients, vlan_sightings, "
            "api_keys, model_accounts, model_packages, model_sites, "
            "model_access_points, netflow_exporters, flow_metrics, "
            "flow_app_metrics, flow_hosts "
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
        "interface_metrics",
        "qoe_scores",
        "qoe_link_states",
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


async def test_ecriture_et_relecture_des_metriques(database: Database, now: datetime) -> None:
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
                ts=now - timedelta(seconds=10 * i),
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
        start=now - timedelta(minutes=5),
        end=now + timedelta(seconds=1),
        bucket_seconds=60,
    )
    # Les buckets sont alignes sur l'epoch : les 6 echantillons peuvent tomber
    # dans un ou deux buckets selon l'heure d'execution. On verifie donc
    # l'agregation, pas le decoupage.
    assert points
    assert sum(point["samples"] for point in points) == 6
    assert max(point["tx_bps_max"] for point in points) == 25_000_000.0
    assert points == sorted(points, key=lambda point: point["bucket"])


async def test_vue_dernier_echantillon(database: Database, now: datetime) -> None:
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
            (petit, sample(now - timedelta(seconds=10), 1e6, 2e6)),
            (petit, sample(now, 1.5e6, 3e6)),
            (gros, sample(now - timedelta(seconds=10), 50e6, 90e6)),
            (gros, sample(now, 60e6, 95e6)),
        ]
    )

    latest = await repo.subscriber_latest(limit=10)
    assert [row["login"] for row in latest] == ["gros", "petit"]
    # Seul le dernier point de chaque abonne remonte.
    assert latest[0]["tx_bps"] == 95e6
    assert latest[0]["ts"] == now


async def test_metriques_backhaul(database: Database, now: datetime) -> None:
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
                    ts=now - timedelta(seconds=30),
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
                    ts=now,
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
        start=now - timedelta(minutes=5),
        end=now + timedelta(seconds=1),
        bucket_seconds=300,
    )
    # Le creux de capacite est ce qui contraint le debit parent du shaping.
    assert points[-1]["capacity_mbps_min"] == 180


async def test_historique_des_cycles_et_compteurs(database: Database, now: datetime) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    await directory.ensure_subscriber("dupont", pop_id=pop_id)
    await writer.record_run(
        RunResult(
            job="collect_subscribers",
            started_at=now,
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


async def test_touch_et_mise_a_jour_des_plans(database: Database, now: datetime) -> None:
    directory = PgDirectory(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    subscriber_id = await directory.ensure_subscriber("dupont", pop_id=pop_id)

    await directory.touch_subscribers({subscriber_id: ("10.20.0.42", now)})
    await directory.update_plans({subscriber_id: Plan(300, 50, "radius:user")})

    row = await repo.get_subscriber(subscriber_id)
    assert row is not None
    assert row["last_ip"] == "10.20.0.42"
    assert row["plan_down_mbps"] == 300
    assert row["plan_source"] == "radius:user"


async def test_suppression_en_cascade(database: Database, now: datetime) -> None:
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
                    ts=now, login="temporaire", router_name="r", pop_name="p", rx_bps=1.0
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
    par_login = {row["login"]: row for row in latest}
    assert par_login["dupont"]["rx_bps"] == 10_000_000.0  # upload abonne
    assert par_login["dupont"]["tx_bps"] == 20_000_000.0  # download abonne
    assert par_login["martin"]["rx_bps"] == 0.0  # en ligne mais inactif
    # Le classement top talkers place le plus consommateur en tete.
    assert latest[0]["login"] == "dupont"

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


async def test_conteneur_reel_cable_la_collecte_des_antennes(database: Database) -> None:
    """Regression P0-3 au VRAI point de montage : ``build_container`` assemble le
    provider des antennes (DbAirOsProvider), et un cycle backhaul complet doit
    reussir.

    Avant le correctif, ``collect_backhauls`` appelait ``backhaul_configs()`` sur
    un provider qui ne portait pas cette methode : le cycle echouait a chaque
    tour, invisible parce que le parametre etait annote ``Any``. La suite testait
    les pieces, pas le montage : ce test monte le conteneur reel.
    """
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.crypto import generate_key

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        # Cle fournie : pas d'ecriture de data/secret.key pendant les tests.
        app_secret_key=generate_key(),
        # L'antenne de test est injoignable : on borne l'attente.
        airos_timeout_s=0.5,
    )

    container = await build_container(settings)
    try:
        assert container.antennas_repo is not None
        # Une antenne "ajoutee depuis l'interface" (aucun mot de passe requis).
        await container.antennas_repo.create(
            {
                "name": "bh-toit",
                "pop_name": "PoP Nord",
                "host": "203.0.113.9",  # TEST-NET-3 : jamais joignable
                "device_key": "device-toit",
                "nominal_capacity_mbps": 300,
                "timeout_s": 0.5,
            },
            None,
        )

        result = await container.collection.collect_backhauls()

        # L'antenne est injoignable, donc aucun echantillon ecrit -- mais le CYCLE
        # doit reussir : c'est la preuve que backhaul_configs() est bien cable.
        assert result.ok is True, result.errors
        assert all("backhaul_configs" not in err for err in result.errors)
        assert all("has no attribute" not in err for err in result.errors)
    finally:
        await shutdown_container(container)


async def test_reglages_persistes_en_base(database: Database) -> None:
    """Les reglages d'exploitation vivent en base, avec leur vrai type.

    Le stockage est en JSONB precisement pour distinguer un reglage
    volontairement VIDE (JSON null : "ne pose pas ce champ CAKE") d'un reglage
    non surcharge (pas de ligne du tout) -- distinction qu'un TEXT perdrait.
    """
    from app.db.settings_repo import SettingsRepository

    repo = SettingsRepository(database.pool)
    assert await repo.load() == {}

    await repo.set("cake_nat", True, updated_by="ui", reason="CGNAT")
    await repo.set("cake_overhead", 38)
    await repo.set("shaping_safety_factor", 0.85)
    await repo.set("cake_diffserv", None, reason="on laisse RouterOS decider")

    valeurs = await repo.load()
    # Les types survivent a l'aller-retour : pas de "True" ni de "38" en chaine.
    assert valeurs["cake_nat"] is True
    assert valeurs["cake_overhead"] == 38
    assert valeurs["shaping_safety_factor"] == 0.85
    # Present, et volontairement vide.
    assert "cake_diffserv" in valeurs
    assert valeurs["cake_diffserv"] is None

    # Reecrire met a jour, ne duplique pas.
    await repo.set("cake_overhead", 44)
    assert (await repo.load())["cake_overhead"] == 44

    journal = await repo.history()
    ligne = next(x for x in journal if x["name"] == "cake_nat")
    assert ligne["updated_by"] == "ui"
    assert ligne["reason"] == "CGNAT"

    assert await repo.delete("cake_overhead") is True
    assert await repo.delete("cake_overhead") is False
    assert "cake_overhead" not in await repo.load()


async def test_les_reglages_de_la_base_pilotent_le_conteneur(database: Database) -> None:
    """Bout en bout : une valeur posee en base doit gouverner le controleur au
    demarrage, sans aucune variable d'environnement."""
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.db.settings_repo import SettingsRepository
    from app.services.crypto import generate_key

    await SettingsRepository(database.pool).set("cake_overhead", 44)
    await SettingsRepository(database.pool).set("shaping_reconcile_interval_s", 300.0)

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
        cake_overhead=22,  # ce que dirait l'environnement
        shaping_reconcile_interval_s=120.0,  # idem
    )

    container = await build_container(settings)
    try:
        # La base a gagne, sur la valeur ET sur la cadence du job.
        assert container.settings.cake_overhead == 44
        job = next(j for j in container.scheduler.status() if j["job"] == "reconcile_shaping")
        assert job["interval_s"] == 300.0

        # Et un changement a chaud reprogramme la boucle sans redemarrage.
        assert container.runtime_config is not None
        container.runtime_config.set("shaping_reconcile_interval_s", 600.0)
        job = next(j for j in container.scheduler.status() if j["job"] == "reconcile_shaping")
        assert job["interval_s"] == 600.0
    finally:
        await shutdown_container(container)


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


async def test_vues_du_tableau_de_bord(database: Database, now: datetime) -> None:
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    # Ce test COMPTE les pas de temps rendus : il faut donc que ses deux
    # echantillons, distants de 20 s, tombent dans le meme pas de 300 s. Sans
    # cette ancre il echouait quand la suite tournait dans les 20 premieres
    # secondes d'un pas -- une fois sur quinze environ.
    now = _ancre_dans_un_seul_pas(now, bucket_seconds=300, recul_s=20)

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
            (a, sample(now - timedelta(seconds=20), 1e6, 10e6)),
            (a, sample(now, 2e6, 20e6)),
            (b, sample(now - timedelta(seconds=20), 5e6, 50e6)),
            (b, sample(now, 6e6, 60e6)),
        ]
    )
    await writer.write_backhaul_metrics(
        [
            (
                backhaul_id,
                BackhaulSample(
                    ts=now,
                    device_id="dev-1",
                    capacity_mbps=400,
                    signal_dbm=-55,
                    airtime_pct=40,
                ),
            )
        ]
    )
    await directory.touch_subscribers({a: ("10.0.0.1", now), b: ("10.0.0.2", now)})

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
        start=now - timedelta(minutes=5), end=now + timedelta(seconds=1), bucket_seconds=300
    )
    # Un seul bucket : la moyenne par abonne est sommee, jamais les echantillons
    # bruts (sinon un abonne a deux mesures compterait double).
    assert len(series) == 1
    assert series[0]["tx_bps"] == pytest.approx(15e6 + 55e6)
    assert series[0]["subscribers"] == 2


async def test_throughput_ne_double_compte_pas(database: Database, now: datetime) -> None:
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
                    ts=now - timedelta(seconds=10 * i),
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
        start=now - timedelta(minutes=5), end=now + timedelta(seconds=1), bucket_seconds=300
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
        changes={"max-limit": ("5M/20M", "20000000/100000000")},
    )

    await repo.record_audit(
        "pop-nord", dry_run=False, outcomes=[(action, True, "*7")], author="alice"
    )

    lignes = await repo.audit(limit=10)
    assert len(lignes) == 1
    assert lignes[0]["router_name"] == "pop-nord"
    assert lignes[0]["dry_run"] is False
    assert lignes[0]["command"].startswith("/queue/simple/set")
    assert "max-limit=20000000/100000000" in lignes[0]["command"]
    # P0-1 : l'auteur est trace. P0-4 : le detail des changements aussi.
    assert lignes[0]["author"] == "alice"
    changes = lignes[0]["changes"]
    if isinstance(changes, str):
        import json as _json

        changes = _json.loads(changes)
    assert changes == {"max-limit": ["5M/20M", "20000000/100000000"]}


async def test_cycle_de_vie_d_un_boost(database: Database, now: datetime) -> None:
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


async def test_seconds_left_est_un_nombre(database: Database, now: datetime) -> None:
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


async def test_suppression_d_un_pop_emporte_ses_donnees(database: Database, now: datetime) -> None:
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
                    SubscriberSample(ts=now, login="x", router_name="r", pop_name="p", rx_bps=1.0),
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


async def test_la_limite_appliquee_remonte_avec_sa_source(
    database: Database, now: datetime
) -> None:
    """L'interface doit afficher ce que le routeur applique, pas le plan
    commercial : un abonne bride a 512 kbps ne doit pas s'afficher a 500 Mbps."""
    from datetime import timedelta

    from app.db.topology_repo import TopologyRepository

    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)
    politiques = TopologyRepository(database.pool)

    pop_id = await directory.ensure_pop("Site")
    for login in ("normal", "bride", "boostee"):
        abonne = await directory.ensure_subscriber(
            login, pop_id=pop_id, plan=Plan(500, 100, "radius")
        )
        await writer.write_subscriber_metrics(
            [
                (
                    abonne,
                    SubscriberSample(
                        ts=now,
                        login=login,
                        router_name="r",
                        pop_name="Site",
                        rx_bps=1e6,
                        tx_bps=1e6,
                    ),
                )
            ]
        )

    await politiques.upsert_policy(
        scope="subscriber",
        target_key="bride",
        max_down_mbps=0.512,
        max_up_mbps=0.128,
        note="impaye",
    )
    await politiques.set_boost(
        scope="subscriber",
        target_key="boostee",
        down_mbps=1500,
        up_mbps=300,
        expires_at=now + timedelta(hours=1),
        reason="geste commercial",
    )

    par_login = {r["login"]: r for r in await repo.subscriber_latest(limit=10)}

    # Sans surcharge : le plan fait foi.
    assert par_login["normal"]["effective_down_mbps"] == 500
    assert par_login["normal"]["limit_source"] == "plan"

    # Bride : c'est la surcharge qui est appliquee, et le plan reste visible.
    assert par_login["bride"]["effective_down_mbps"] == 0.512
    assert par_login["bride"]["effective_up_mbps"] == 0.128
    assert par_login["bride"]["limit_source"] == "override"
    assert par_login["bride"]["plan_down_mbps"] == 500
    assert par_login["bride"]["policy_note"] == "impaye"

    # Boost : il prime sur tout le reste tant qu'il court.
    assert par_login["boostee"]["effective_down_mbps"] == 1500
    assert par_login["boostee"]["limit_source"] == "boost"
    assert par_login["boostee"]["boost_reason"] == "geste commercial"

    # La fiche detaillee dit la meme chose.
    fiche = await repo.get_subscriber(par_login["bride"]["subscriber_id"])
    assert fiche["effective_down_mbps"] == 0.512
    assert fiche["limit_source"] == "override"


async def test_boost_expire_ne_compte_plus_dans_la_limite(
    database: Database, now: datetime
) -> None:
    """Un boost echu ne doit plus etre affiche comme la limite en vigueur."""
    from datetime import timedelta

    from app.db.topology_repo import TopologyRepository

    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("Site")
    abonne = await directory.ensure_subscriber(
        "ancien-boost", pop_id=pop_id, plan=Plan(100, 20, "radius")
    )
    await writer.write_subscriber_metrics(
        [
            (
                abonne,
                SubscriberSample(
                    ts=now, login="ancien-boost", router_name="r", pop_name="Site", tx_bps=1.0
                ),
            )
        ]
    )
    await TopologyRepository(database.pool).set_boost(
        scope="subscriber",
        target_key="ancien-boost",
        down_mbps=900,
        up_mbps=None,
        expires_at=now - timedelta(minutes=5),
    )

    ligne = (await repo.subscriber_latest(limit=5))[0]

    assert ligne["effective_down_mbps"] == 100
    assert ligne["limit_source"] == "plan"


# ------------------------------------------------------- debit des liens


async def _poser_un_lien(
    database: Database, *, interface: str = "ether2", cible: str = "mac:AA:BB:CC:00:00:02"
) -> str:
    """Un routeur, un voisin, une adjacence : le minimum pour porter un debit."""
    cle = f"router:pop-nord|{interface}|{cible}"
    async with database.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO topology_nodes (key, name, kind) VALUES
                ('router:pop-nord', 'PoP Nord', 'pop'), ($1, 'voisin', 'radio')
            ON CONFLICT (key) DO NOTHING
            """,
            cible,
        )
        await conn.execute(
            """
            INSERT INTO topology_links
                   (key, source_key, target_key, kind, interface, capacity_mbps, discovered_by)
            VALUES ($1, 'router:pop-nord', $2, 'ethernet', $3, 1000, 'pop-nord')
            ON CONFLICT (key) DO NOTHING
            """,
            cle,
            cible,
            interface,
        )
    return cle


async def test_le_debit_mesure_remonte_sur_le_lien(database: Database, now: datetime) -> None:
    """La jointure qui compte : (discovered_by, interface) contre les compteurs."""
    from app.db.topology_repo import TopologyRepository

    cle = await _poser_un_lien(database)
    writer = PgMetricsWriter(database.pool)
    await writer.write_interface_metrics(
        [
            InterfaceSample(
                ts=now,
                router_name="pop-nord",
                interface="ether2",
                rx_bps=12_000_000,
                tx_bps=340_000_000,
                running=True,
                capacity_mbps=1000,
            )
        ]
    )

    lien = await TopologyRepository(database.pool).link(cle)

    assert lien is not None
    assert lien["tx_bps"] == 340_000_000
    assert lien["rx_bps"] == 12_000_000
    assert lien["port_capacity_mbps"] == 1000
    assert lien["measure_fresh"] is True
    assert lien["interface_links"] == 1


async def test_un_port_partage_signale_le_nombre_de_voisins(
    database: Database, now: datetime
) -> None:
    """Deux voisins derriere un switch : le compteur du port est le meme pour
    les deux liens. Sans ce compte, l'interface ferait croire a deux mesures
    independantes et le total serait double."""
    from app.db.topology_repo import TopologyRepository

    await _poser_un_lien(database, cible="mac:AA:BB:CC:00:00:02")
    await _poser_un_lien(database, cible="mac:AA:BB:CC:00:00:03")
    await PgMetricsWriter(database.pool).write_interface_metrics(
        [
            InterfaceSample(
                ts=now, router_name="pop-nord", interface="ether2", rx_bps=1.0, tx_bps=2.0
            )
        ]
    )

    liens = await TopologyRepository(database.pool).links()

    assert len(liens) == 2
    assert {lien["interface_links"] for lien in liens} == {2}
    assert {lien["tx_bps"] for lien in liens} == {2.0}


async def test_un_lien_sans_mesure_reste_lisible(database: Database) -> None:
    """Un lien decouvert mais jamais mesure ne doit pas disparaitre de la liste :
    la jointure est bien une LEFT JOIN."""
    from app.db.topology_repo import TopologyRepository

    cle = await _poser_un_lien(database)
    lien = await TopologyRepository(database.pool).link(cle)

    assert lien is not None
    assert lien["rx_bps"] is None
    assert lien["measured_at"] is None


async def test_la_serie_de_debit_est_agregee_par_bucket(database: Database, now: datetime) -> None:
    from app.db.topology_repo import TopologyRepository

    writer = PgMetricsWriter(database.pool)
    await writer.write_interface_metrics(
        [
            InterfaceSample(
                ts=now - timedelta(seconds=decalage),
                router_name="pop-nord",
                interface="ether2",
                rx_bps=1_000_000.0 * (i + 1),
                tx_bps=10_000_000.0 * (i + 1),
                capacity_mbps=1000,
            )
            for i, decalage in enumerate((0, 10, 20, 300))
        ]
    )

    serie = await TopologyRepository(database.pool).interface_series(
        router_name="pop-nord", interface="ether2", minutes=60, bucket_seconds=60
    )

    assert len(serie) >= 2
    # La pointe est conservee a cote de la moyenne : c'est elle qui dit si le
    # lien a sature, une moyenne sur une minute la gommerait.
    assert max(p["tx_peak_bps"] for p in serie) == 40_000_000.0
    assert all(p["capacity_mbps"] == 1000 for p in serie)


async def test_la_derniere_mesure_par_port_est_bien_la_plus_recente(
    database: Database, now: datetime
) -> None:
    from app.db.topology_repo import TopologyRepository

    writer = PgMetricsWriter(database.pool)
    await writer.write_interface_metrics(
        [
            InterfaceSample(
                ts=now - timedelta(minutes=5),
                router_name="pop-nord",
                interface="ether1",
                tx_bps=1.0,
            ),
            InterfaceSample(ts=now, router_name="pop-nord", interface="ether1", tx_bps=999.0),
        ]
    )

    lignes = await TopologyRepository(database.pool).interface_latest()

    assert len(lignes) == 1
    assert lignes[0]["tx_bps"] == 999.0
    assert lignes[0]["fresh"] is True


async def test_la_vue_expose_l_adresse_qui_portera_la_file(
    database: Database, now: datetime
) -> None:
    """L'interface doit pouvoir montrer la cible AVANT d'ecrire quoi que ce
    soit : sans cette colonne, l'exploitant fixe un debit sans savoir sur quelle
    adresse il atterrira."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("Site")
    abonne = await directory.ensure_subscriber(
        "avec-ip", pop_id=pop_id, plan=Plan(100, 20, "radius")
    )
    await writer.write_subscriber_metrics(
        [
            (
                abonne,
                SubscriberSample(
                    ts=now, login="avec-ip", router_name="r", pop_name="Site", tx_bps=1.0
                ),
            )
        ]
    )
    await directory.touch_subscribers({abonne: ("10.20.0.42", now)})

    ligne = (await repo.subscriber_latest(limit=5))[0]

    assert str(ligne["last_ip"]) == "10.20.0.42"


# =========================================================================
# Clients a IP fixe : migration du schema et depot d'inventaire
# =========================================================================


async def test_migration_depuis_une_base_avec_pppoe_login(database: Database) -> None:
    """Une installation existante doit garder ses abonnes a travers le renommage.

    La colonne s'appelait 'pppoe_login' ; elle devenait un mensonge des qu'un
    client a IP fixe entrait dans la table. Le renommage ne doit ni perdre une
    ligne, ni casser les cles etrangeres qui pointent sur subscribers.
    """
    async with database.pool.acquire() as conn:
        # On remet la table dans son etat d'AVANT. La vue depend des deux
        # colonnes touchees, on la retire d'abord : le schema la reconstruit.
        await conn.execute("DROP VIEW IF EXISTS subscriber_latest")
        await conn.execute("ALTER TABLE subscribers DROP COLUMN kind")
        await conn.execute("ALTER TABLE subscribers RENAME COLUMN login TO pppoe_login")
        await conn.execute(
            "INSERT INTO subscribers (pppoe_login, plan_down_mbps) VALUES ('ancien', 100)"
        )

    await database.migrate()

    async with database.pool.acquire() as conn:
        colonnes = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'subscribers'"
            )
        }
        assert "login" in colonnes
        assert "pppoe_login" not in colonnes

        ligne = await conn.fetchrow("SELECT login, kind, plan_down_mbps FROM subscribers")
        assert ligne["login"] == "ancien"
        assert ligne["plan_down_mbps"] == 100
        # Les abonnes existants sont tous du PPPoE : c'est le defaut.
        assert ligne["kind"] == "pppoe"

    # Et le schema reste rejouable une fois la migration passee.
    await database.migrate()


async def test_la_contrainte_de_nature_est_posee(database: Database) -> None:
    async with database.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO subscribers (login, kind) VALUES ('x', 'chimere')")


async def test_un_client_statique_et_un_abonne_partagent_l_espace_de_noms(
    database: Database,
) -> None:
    """L'argument central du modele : RouterOS n'a qu'un espace de noms de
    files, donc la base doit interdire deux abonnes homonymes."""
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO subscribers (login, kind) VALUES ('dupont', 'pppoe')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO subscribers (login, kind) VALUES ('dupont', 'static')")


async def test_depot_inventaire_cycle_complet(database: Database) -> None:
    repo = StaticClientsRepository(database.pool)

    cree = await repo.create(
        {
            "reference": "mairie-vitre",
            "label": "Mairie de Vitre",
            "pop_name": "PoP Nord",
            "address": "10.0.0.5",
            "vlan": 120,
            "plan_down_mbps": 200.0,
            "plan_up_mbps": 50.0,
        }
    )
    # L'adresse ressort sous forme canonique, prete a servir de cible de file.
    assert cree["address"] == "10.0.0.5/32"
    assert cree["vlan"] == 120

    # Un sous-reseau garde son prefixe : le bloc entier du client est plafonne.
    modifie = await repo.update(cree["id"], {"address": "10.0.0.0/29"})
    assert modifie["address"] == "10.0.0.0/29"

    actifs = await repo.load_enabled()
    assert [c.reference for c in actifs] == ["mairie-vitre"]
    assert actifs[0].address == "10.0.0.0/29"
    assert actifs[0].display_name == "Mairie de Vitre"

    # Suspendre garde la fiche mais la retire du cycle.
    await repo.update(cree["id"], {"enabled": False})
    assert await repo.load_enabled() == []
    assert len(await repo.list_all()) == 1

    with pytest.raises(DuplicateStaticClientError):
        await repo.create(
            {"reference": "mairie-vitre", "pop_name": "PoP Nord", "address": "10.0.0.9"}
        )

    await repo.delete(cree["id"])
    assert await repo.list_all() == []
    with pytest.raises(StaticClientNotFoundError):
        await repo.delete(cree["id"])


async def test_la_vue_expose_la_nature(database: Database, now: datetime) -> None:
    """L'interface doit pouvoir distinguer les deux natures sans requete de plus."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord")
    ppp = await directory.ensure_subscriber("dupont", pop_id=pop_id)
    fixe = await directory.ensure_subscriber(
        "mairie-vitre",
        pop_id=pop_id,
        plan=Plan(down_mbps=200, up_mbps=50, source="static-inventory"),
        kind="static",
    )
    await writer.write_subscriber_metrics(
        [
            (ppp, SubscriberSample(ts=now, login="dupont", router_name="r", pop_name="PoP Nord")),
            (
                fixe,
                SubscriberSample(
                    ts=now, login="mairie-vitre", router_name="r", pop_name="PoP Nord"
                ),
            ),
        ]
    )

    par_login = {r["login"]: r for r in await repo.subscriber_latest(limit=10)}
    assert par_login["dupont"]["kind"] == "pppoe"
    assert par_login["mairie-vitre"]["kind"] == "static"
    assert par_login["mairie-vitre"]["plan_down_mbps"] == 200

    # Et le filtre par nature ne rend que ce qu'on demande.
    statiques = await repo.subscriber_latest(limit=10, kind="static")
    assert [r["login"] for r in statiques] == ["mairie-vitre"]
    listes = await repo.list_subscribers(kind="pppoe")
    assert [r["login"] for r in listes] == ["dupont"]


async def test_l_inventaire_fait_autorite_sur_le_plan_d_un_statique(
    database: Database,
) -> None:
    """Retirer un debit de la fiche doit le retirer en base.

    Un plan PPPoE absent veut dire "RADIUS n'a rien dit ce cycle-ci" et ne doit
    rien ecraser ; pour un client statique, la fiche est la seule source, et son
    silence est une decision.
    """
    directory = PgDirectory(database.pool)
    await directory.ensure_subscriber(
        "mairie", plan=Plan(down_mbps=200, up_mbps=50, source="static-inventory"), kind="static"
    )
    await directory.ensure_subscriber("mairie", plan=None, kind="static")

    async with database.pool.acquire() as conn:
        ligne = await conn.fetchrow("SELECT plan_down_mbps FROM subscribers WHERE login = 'mairie'")
    assert ligne["plan_down_mbps"] is None

    # Le chemin PPPoE, lui, garde son plan quand la source se tait.
    await directory.ensure_subscriber(
        "dupont", plan=Plan(down_mbps=100, up_mbps=20, source="radius")
    )
    await directory.ensure_subscriber("dupont", plan=None)
    async with database.pool.acquire() as conn:
        ligne = await conn.fetchrow("SELECT plan_down_mbps FROM subscribers WHERE login = 'dupont'")
    assert ligne["plan_down_mbps"] == 100


async def test_conteneur_reel_cable_les_clients_statiques(database: Database) -> None:
    """Le montage complet, pas seulement les pieces.

    Un client declare dans la base doit, sans rien d'autre, ressortir en abonne
    materialise apres un cycle de collecte : c'est ce que ``build_container``
    doit avoir cable de bout en bout (depot -> service de collecte -> shaping).
    """
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.crypto import generate_key

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
    )

    container = await build_container(settings)
    try:
        assert container.static_clients_repo is not None
        await container.static_clients_repo.create(
            {
                "reference": "mairie-vitre",
                "pop_name": "PoP Nord",
                "address": "10.0.0.0/29",
                "plan_down_mbps": 200.0,
                "plan_up_mbps": 50.0,
            }
        )

        resultat = await container.collection.collect_subscribers()
        assert resultat.ok, resultat.errors

        lignes = await container.repository.list_subscribers(kind="static")
        assert [ligne["login"] for ligne in lignes] == ["mairie-vitre"]
        assert lignes[0]["plan_down_mbps"] == 200.0
        assert lignes[0]["plan_source"] == "static-inventory"
        # L'adresse declaree est bien redescendue sur la fiche d'abonne.
        assert str(lignes[0]["last_ip"]).startswith("10.0.0.0")

        # Et le service de shaping voit le meme inventaire.
        actifs = await container.shaping.static_clients.load_enabled()
        assert [c.reference for c in actifs] == ["mairie-vitre"]
    finally:
        await shutdown_container(container)


# =========================================================================
# Boucle fermee QoE (phase 4)
# =========================================================================
async def test_score_de_qoe_composite_par_abonne(database: Database, now: datetime) -> None:
    """Le signal qui declenche la boucle fermee, lu sur du vrai SQL.

    Deux abonnes, memes debits, latences opposees : celui dont le RTT GONFLE
    quand le lien se remplit doit decrocher, celui qui reste plat doit rester
    dans les clous. C'est exactement ce qu'une moyenne de RTT ne montrerait pas.
    """
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    sain = await directory.ensure_subscriber("sain", pop_id=pop_id, plan=Plan(100, 20, "mock"))
    gonfle = await directory.ensure_subscriber("gonfle", pop_id=pop_id, plan=Plan(100, 20, "mock"))

    def echantillon(ts: datetime, rtt: float, charge: float) -> SubscriberSample:
        return SubscriberSample(
            ts=ts,
            login="x",
            router_name="r",
            pop_name="PoP Nord",
            rx_bps=0.0,
            tx_bps=charge,
            rtt_ms=rtt,
        )

    lignes = []
    for index in range(10):
        ts = now - timedelta(seconds=30 * (10 - index))
        charge = 0.0 if index < 5 else 90e6  # la charge arrive a mi-fenetre
        lignes.append((sain, echantillon(ts, 12.0, charge)))
        # Le meme profil de charge, mais la latence passe de 12 a 320 ms.
        lignes.append((gonfle, echantillon(ts, 12.0 if index < 5 else 320.0, charge)))
    await writer.write_subscriber_metrics(lignes)

    notes = {r["login"]: r for r in await repo.qoe_subscribers(minutes=30)}

    assert notes["sain"]["severity"] == "ok"
    assert notes["sain"]["score"] >= 80
    assert notes["gonfle"]["severity"] == "crit"
    assert notes["gonfle"]["grade"] == "F"
    assert notes["gonfle"]["score"] < 55  # sous le seuil par defaut de la boucle
    # Meme fonction de score que la heatmap : le detail est aussi dans /bufferbloat.
    detail = await repo.bufferbloat(minutes=30)
    par_login = {r["login"]: r for r in detail["subscribers"]}
    assert par_login["gonfle"]["qoe"]["score"] == notes["gonfle"]["score"]


async def test_la_heatmap_porte_les_echantillons_de_chaque_pas(
    database: Database, now: datetime
) -> None:
    """La ligne QoE n'est plus un proxy latence : chaque pas remonte ses couples
    (rtt, charge), pour que la latence SOUS CHARGE soit calculable par pas."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    lignes = []
    for index in range(6):
        abonne = await directory.ensure_subscriber(
            f"abonne-{index}", pop_id=pop_id, plan=Plan(100, 20, "mock")
        )
        charge = 0.0 if index < 4 else 90e6
        lignes.append(
            (
                abonne,
                SubscriberSample(
                    ts=now,
                    login=f"abonne-{index}",
                    router_name="r",
                    pop_name="PoP Nord",
                    rx_bps=0.0,
                    tx_bps=charge,
                    rtt_ms=10.0 if index < 4 else 280.0,
                ),
            )
        )
    await writer.write_subscriber_metrics(lignes)

    heat = await repo.heatmap(minutes=15, buckets=15)

    qoe = next(r for r in heat["rows"] if r["key"] == "qoe")
    remplies = [c for c in qoe["cells"] if c["severity"] != "none"]
    assert remplies, "le pas contenant les mesures doit etre colore"
    cellule = remplies[-1]
    # Les abonnes charges pinguent a 280 ms, les autres a 10 : la cellule le voit.
    assert cellule["basis"] == "composite"
    assert cellule["severity"] == "crit"
    assert cellule["bloat_ms"] is not None and cellule["bloat_ms"] > 200


async def test_cycle_de_vie_d_un_resserrage_qoe(database: Database) -> None:
    """L'etat de la boucle fermee : resserrage, delai de garde, retour a la normale.

    Seuls les liens REELLEMENT resserres remontent dans ``qoe_trims`` : un
    facteur a 1.0 est l'absence de decision, il n'a pas a laisser croire que la
    boucle agit sur ce lien.
    """
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)

    await repo.save_qoe_link_state(
        link_key="lien-secteur-1",
        sector_key="mac:AA:BB:CC:DD:EE:FF",
        trim_factor=0.9,
        healthy_cycles=0,
        scored_count=4,
        degraded_count=2,
        worst_score=18.0,
        last_action="tighten",
        last_reason="2/4 abonne(s) sous 55",
        triggered=True,
    )

    assert await repo.qoe_trims() == {"lien-secteur-1": 0.9}
    etat = (await repo.qoe_link_states())["lien-secteur-1"]
    assert etat["last_action"] == "tighten"
    assert etat["degraded_count"] == 2
    declenche_a = etat["last_trigger_at"]
    assert declenche_a is not None

    # Cycle sain sans changement de resserrage : la date de declenchement ne doit
    # PAS avancer, sinon le journal ne voudrait plus rien dire.
    await repo.save_qoe_link_state(
        link_key="lien-secteur-1",
        sector_key="mac:AA:BB:CC:DD:EE:FF",
        trim_factor=0.9,
        healthy_cycles=1,
        scored_count=4,
        degraded_count=0,
        worst_score=88.0,
        last_action="hold",
        last_reason="QoE retablie depuis 1/3 cycle(s)",
        triggered=False,
    )

    etat = (await repo.qoe_link_states())["lien-secteur-1"]
    assert etat["healthy_cycles"] == 1
    assert etat["last_trigger_at"] == declenche_a
    assert await repo.qoe_trims() == {"lien-secteur-1": 0.9}

    # Retour a 1.0 : la boucle ne tient plus ce lien.
    await repo.save_qoe_link_state(
        link_key="lien-secteur-1",
        sector_key="mac:AA:BB:CC:DD:EE:FF",
        trim_factor=1.0,
        healthy_cycles=0,
        scored_count=4,
        degraded_count=0,
        worst_score=91.0,
        last_action="relax",
        last_reason="QoE retablie depuis 3 cycles",
        triggered=True,
    )

    assert await repo.qoe_trims() == {}
    assert (await repo.qoe_link_states())["lien-secteur-1"]["trim_factor"] == 1.0


async def test_le_conteneur_reel_planifie_la_boucle_fermee_qoe(database: Database) -> None:
    """Le job de la phase 4 doit etre CABLE, pas seulement ecrit.

    Meme esprit que la regression P0-3 : la suite peut tester la decision, le
    plan et le depot sans jamais verifier que le scheduler declenche quoi que ce
    soit. Ici on monte le conteneur reel et on fait tourner un cycle complet.
    """
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.collection import JOB_QOE_LOOP
    from app.services.crypto import generate_key

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
    )

    container = await build_container(settings)
    try:
        assert JOB_QOE_LOOP in container.scheduler.job_names()

        # Un cycle complet sur une base vide : aucun abonne note, donc aucune
        # decision. La boucle est INERTE tant que la sonde RTT ne donne rien --
        # elle n'invente pas de degradation.
        await container.scheduler.run_once(JOB_QOE_LOOP)
        resultat = await container.shaping.adjust_for_qoe()

        assert resultat["scored"] == 0
        assert resultat["sectors"] == []
        assert resultat["routers"] == []
        assert await container.topology_repo.qoe_trims() == {}
    finally:
        await shutdown_container(container)


# =========================================================================
# Detection ARP : ce que la vraie base sait rapprocher
# =========================================================================


def _vue(adresse: str, **kwargs) -> VlanSighting:
    base = {
        "router_name": "pop-nord",
        "pop_name": "PoP Nord",
        "address": adresse,
        "vlan_interface": "vlan120",
        "mac": "AA:BB:CC:00:00:01",
        "vlan_id": 120,
    }
    return VlanSighting(**{**base, **kwargs})


async def test_une_adresse_du_bloc_declare_confirme_la_presence(
    database: Database, now: datetime
) -> None:
    """LA raison d'etre du SQL de ce depot : le rapprochement se fait par
    CONTENANCE reseau, pas par egalite.

    Un client declare en 10.0.0.0/29 doit etre reconnu present quand c'est
    10.0.0.3 qui parle. Une jointure sur l'egalite ne l'aurait jamais vu, et
    l'operateur aurait cru son client muet.
    """
    inventaire = StaticClientsRepository(database.pool)
    observations = VlanSightingsRepository(database.pool)

    await inventaire.create(
        {"reference": "mairie", "pop_name": "PoP Nord", "address": "10.0.0.0/29"}
    )
    await observations.record([_vue("10.0.0.3")], seen_at=now)

    presence = await observations.presence()
    assert set(presence) == {"mairie"}
    assert presence["mairie"]["mac"] == "AA:BB:CC:00:00:01"
    assert presence["mairie"]["vlan_interface"] == "vlan120"

    # Et cette adresse n'est donc PAS un candidat : elle est deja couverte.
    assert await observations.candidates() == []


async def test_une_adresse_hors_de_tout_bloc_devient_candidate(
    database: Database, now: datetime
) -> None:
    inventaire = StaticClientsRepository(database.pool)
    observations = VlanSightingsRepository(database.pool)

    await inventaire.create(
        {"reference": "mairie", "pop_name": "PoP Nord", "address": "10.0.0.0/29"}
    )
    await observations.record([_vue("10.0.0.3"), _vue("10.20.0.77")], seen_at=now)

    candidats = await observations.candidates()
    assert [c["address"] for c in candidats] == ["10.20.0.77"]
    assert candidats[0]["vlan_id"] == 120
    assert candidats[0]["router_name"] == "pop-nord"
    assert candidats[0]["pop_name"] == "PoP Nord"


async def test_declarer_un_client_retire_son_candidat(database: Database, now: datetime) -> None:
    """Le cycle complet du brief : une adresse est detectee, un humain la
    declare, elle disparait des candidats et devient une presence confirmee.
    Aucun code ne l'a promue : c'est la declaration qui change la lecture.
    """
    inventaire = StaticClientsRepository(database.pool)
    observations = VlanSightingsRepository(database.pool)

    await observations.record([_vue("10.20.0.77")], seen_at=now)
    assert [c["address"] for c in await observations.candidates()] == ["10.20.0.77"]
    assert await observations.presence() == {}

    await inventaire.create(
        {
            "reference": "clinique",
            "pop_name": "PoP Nord",
            "address": "10.20.0.77",
            "plan_down_mbps": 100.0,
        }
    )

    assert await observations.candidates() == []
    assert set(await observations.presence()) == {"clinique"}


async def test_first_seen_ne_recule_jamais(database: Database, now: datetime) -> None:
    """L'anciennete d'un candidat est une information de tri pour l'operateur :
    une adresse vue depuis trois jours n'a pas le meme sens qu'une nouvelle."""
    observations = VlanSightingsRepository(database.pool)

    await observations.record([_vue("10.20.0.77")], seen_at=now - timedelta(hours=6))
    await observations.record([_vue("10.20.0.77", mac="AA:BB:CC:00:00:99")], seen_at=now)

    candidats = await observations.candidates()
    assert len(candidats) == 1
    assert candidats[0]["first_seen"] < candidats[0]["last_seen"]
    # La MAC, elle, suit la derniere observation.
    assert candidats[0]["mac"] == "AA:BB:CC:00:00:99"


async def test_deux_routeurs_peuvent_voir_la_meme_adresse(
    database: Database, now: datetime
) -> None:
    """Des plans d'adressage prives se recoupent d'un PoP a l'autre : la cle
    primaire porte le routeur, sinon un PoP ecraserait le candidat de l'autre."""
    observations = VlanSightingsRepository(database.pool)

    await observations.record(
        [_vue("10.20.0.77"), _vue("10.20.0.77", router_name="pop-sud", pop_name="PoP Sud")],
        seen_at=now,
    )

    candidats = await observations.candidates()
    assert sorted(c["router_name"] for c in candidats) == ["pop-nord", "pop-sud"]


async def test_les_observations_perimees_sont_oubliees(database: Database, now: datetime) -> None:
    observations = VlanSightingsRepository(database.pool)
    await observations.record([_vue("10.20.0.77")], seen_at=now - timedelta(days=3))
    await observations.record([_vue("10.20.0.78")], seen_at=now)

    # Filtre a la lecture...
    recents = await observations.candidates(max_age_s=3600)
    assert [c["address"] for c in recents] == ["10.20.0.78"]

    # ... et purge effective.
    oubliees = await observations.prune(older_than_s=86_400)
    assert oubliees == 1
    assert [c["address"] for c in await observations.candidates()] == ["10.20.0.78"]


async def test_le_plafond_de_candidats_est_respecte(database: Database, now: datetime) -> None:
    observations = VlanSightingsRepository(database.pool)
    await observations.record([_vue(f"10.20.0.{i}") for i in range(1, 40)], seen_at=now)
    assert len(await observations.candidates(limit=5)) == 5


async def test_conteneur_reel_cable_la_detection(database: Database) -> None:
    """Le montage complet : un cycle de detection sur le conteneur reel doit
    remplir vlan_sightings, et RIEN d'autre.

    C'est la garantie structurelle du brief verifiee de bout en bout : aucun
    abonne, aucun plan, aucune file ne nait d'une detection.
    """
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.collection import JOB_VLAN_CLIENTS
    from app.services.crypto import generate_key

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
    )

    container = await build_container(settings)
    try:
        assert container.sightings_repo is not None
        assert JOB_VLAN_CLIENTS in container.scheduler.job_names()

        # Aucun routeur dans l'inventaire : le cycle passe sans rien trouver.
        resultat = await container.collection.detect_vlan_clients()
        assert resultat.ok, resultat.errors

        # On injecte une observation comme le ferait un routeur reel, puis on
        # verifie que le referentiel d'abonnes reste VIDE.
        await container.sightings_repo.record([_vue("10.20.0.77")], seen_at=datetime.now(tz=UTC))
        assert [c["address"] for c in await container.sightings_repo.candidates()] == ["10.20.0.77"]
        assert await container.repository.list_subscribers() == []

        # Et le service de shaping n'a aucun moyen de lire ces observations.
        assert not hasattr(container.shaping, "sightings")
    finally:
        await shutdown_container(container)


# =========================================================================
# Loopback : identite des routeurs dans la topologie
# =========================================================================


async def test_le_loopback_traverse_la_base(database: Database) -> None:
    """Aller-retour complet : saisie, relecture en RouterConfig, modification.

    La colonne est un INET ; le contrat cote code est une adresse d'hote nue.
    C'est cette traduction qui doit tenir, sinon la cle de rapprochement ne se
    compare plus (``10.255.0.2`` contre ``10.255.0.2/32``).
    """
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    repo = RoutersRepository(database.pool, SecretBox(generate_key()))

    cree = await repo.create(
        {
            "name": "core-rennes",
            "host": "10.10.0.2",
            "role": "core",
            "pop_name": "Coeur Rennes",
            # Saisi en /32 : doit ressortir nu.
            "loopback": "10.255.0.2/32",
        },
        "secret",
    )
    assert cree["loopback"] == "10.255.0.2"

    configs = await repo.load_configs()
    assert [c.loopback for c in configs] == ["10.255.0.2"]
    assert configs[0].role == "core"

    modifie = await repo.update(cree["id"], {"loopback": "10.255.0.99"})
    assert modifie["loopback"] == "10.255.0.99"


async def test_la_base_refuse_deux_routeurs_au_meme_loopback(database: Database) -> None:
    """L'unicite est verifiee A LA SAISIE, pas seulement a la reconciliation.

    Prise trop tard, une collision se traduirait par un arbre silencieusement
    faux ; prise ici, elle devient un message que l'operateur peut corriger.
    """
    from app.db.routers_repo import DuplicateRouterError, RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    repo = RoutersRepository(database.pool, SecretBox(generate_key()))
    await repo.create({"name": "a", "host": "1.1.1.1", "loopback": "10.255.0.1"}, "s")

    with pytest.raises(DuplicateRouterError, match="loopback"):
        await repo.create({"name": "b", "host": "1.1.1.2", "loopback": "10.255.0.1"}, "s")

    # Le message doit distinguer les deux unicites de la table.
    with pytest.raises(DuplicateRouterError, match="nomme 'a'"):
        await repo.create({"name": "a", "host": "1.1.1.3"}, "s")


async def test_plusieurs_routeurs_sans_loopback_restent_possibles(database: Database) -> None:
    """L'index unique est partiel : NULL n'entre pas en collision avec NULL.

    Sans cela, declarer un deuxieme routeur avant d'avoir renseigne son
    loopback deviendrait impossible.
    """
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    repo = RoutersRepository(database.pool, SecretBox(generate_key()))
    await repo.create({"name": "a", "host": "1.1.1.1"}, "s")
    await repo.create({"name": "b", "host": "1.1.1.2"}, "s")

    configs = await repo.load_configs()
    assert {c.loopback for c in configs} == {None}


# =========================================================================
# Un PoP ne disparait pas en silence
# =========================================================================


async def test_une_fiche_invalide_ne_vide_plus_tout_l_inventaire(database: Database) -> None:
    """LE BUG LE PLUS GRAVE TROUVE EN CHEMIN.

    ``RouterRole('chimere')`` leve. L'exception remontait de ``load_configs``
    jusqu'au garde-fou large du registre, qui l'attrapait -- et TOUT
    l'inventaire en base disparaissait a cause d'une seule ligne. Un parc de
    vingt PoPs s'evaporait parce qu'une fiche avait un role mal saisi.
    """
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    repo = RoutersRepository(database.pool, SecretBox(generate_key()))
    await repo.create({"name": "bon", "host": "10.0.0.1", "role": "pop"}, "s")
    await repo.create({"name": "casse", "host": "10.0.0.2", "role": "pop"}, "s")
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE routers SET role = 'chimere' WHERE name = 'casse'")

    configs, ecartes = await repo.load_configs_with_report()

    # Le routeur sain survit...
    assert [c.name for c in configs] == ["bon"]
    # ... et l'autre est ECARTE, pas perdu.
    assert [e["name"] for e in ecartes] == ["casse"]
    assert "fiche invalide" in ecartes[0]["reason"]
    assert ecartes[0]["host"] == "10.0.0.2"

    # Le motif est aussi pose sur la fiche, donc visible dans l'inventaire.
    public = {r["name"]: r for r in await repo.list_public()}
    assert "fiche invalide" in (public["casse"]["last_error"] or "")


async def test_un_secret_illisible_est_rapporte_avec_sa_fiche(database: Database) -> None:
    """Le cas du lab : la cle de chiffrement a change (volume perdu, secret
    regenere). Le routeur devient illisible et sortait de la collecte, de
    l'arbre et de la detection ARP en ne laissant qu'une ligne de log."""
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key

    await RoutersRepository(database.pool, SecretBox(generate_key())).create(
        {"name": "pop-nord", "host": "10.10.0.10", "role": "pop", "pop_name": "PoP Nord"}, "s"
    )

    # Nouvelle cle : le secret d'hier ne se dechiffre plus.
    autre = RoutersRepository(database.pool, SecretBox(generate_key()))
    configs, ecartes = await autre.load_configs_with_report()

    assert configs == []
    assert len(ecartes) == 1
    assert ecartes[0]["name"] == "pop-nord"
    assert ecartes[0]["pop_name"] == "PoP Nord"
    assert ecartes[0]["source"] == "db"
    assert "secret illisible" in ecartes[0]["reason"]


async def test_le_registre_reel_remonte_l_ecart_jusqu_a_l_interface(
    database: Database,
) -> None:
    """Le montage complet : depot reel, registre reel. C'est ce chemin qui
    alimente ``/pops/routers`` et donc l'avertissement affiche."""
    from app.config import Settings
    from app.db.routers_repo import RoutersRepository
    from app.services.crypto import SecretBox, generate_key
    from app.services.registry import RouterRegistry

    await RoutersRepository(database.pool, SecretBox(generate_key())).create(
        {"name": "pop-nord", "host": "10.10.0.10", "role": "pop", "pop_name": "PoP Nord"}, "s"
    )
    depot = RoutersRepository(database.pool, SecretBox(generate_key()))
    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    registre = RouterRegistry(settings, repository=depot)

    await registre.reload()

    assert registre.collectors == []
    assert [e["name"] for e in registre.skipped] == ["pop-nord"]
    assert registre.skipped[0]["source"] == "db"


async def test_le_conteneur_reel_decouvre_la_topologie_tout_seul(database: Database) -> None:
    """LE BUG QUI VIDAIT LES ONGLETS TOPOLOGIE ET ARBRE RESEAU.

    Ces deux vues lisent le graphe en base ; le graphe n'est ecrit que par
    discover(). Or AUCUN job ne l'appelait : le reglage
    topology_refresh_interval_s etait declare et ne pilotait rien. La topologie
    n'existait donc que si un humain cliquait "Relancer la decouverte", et une
    installation neuve affichait deux onglets vides quel que soit l'etat des
    PoPs et de leur API.

    Meme classe de bug que JOB_QOE_LOOP : un reglage, une fonction, et le
    cablage manquant entre les deux. Ce test monte le conteneur reel.
    """
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.collection import JOB_TOPOLOGY
    from app.services.crypto import generate_key

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
    )

    container = await build_container(settings)
    try:
        assert JOB_TOPOLOGY in container.scheduler.job_names()

        # Le job tourne sans routeur : il doit reussir, pas exploser.
        await container.scheduler.run_once(JOB_TOPOLOGY)

        # Et la cadence est pilotable depuis la base, comme les autres.
        assert container.runtime_config is not None
        noms = {r["name"] for r in container.runtime_config.describe()}
        assert "topology_refresh_interval_s" in noms
        assert "qoe_loop_interval_s" in noms
    finally:
        await shutdown_container(container)


async def test_chaque_cadence_pilote_un_job_qui_existe(database: Database) -> None:
    """Un reglage de cadence qui ne correspond a aucun job est un piege : il
    s'affiche, se modifie, et ne change rien. C'est exactement ce qui est
    arrive deux fois -- a la boucle QoE, puis a la decouverte de topologie."""
    from app.config import Settings
    from app.container import build_container, shutdown_container
    from app.services.crypto import generate_key
    from app.services.runtime_config import REGLAGES

    settings = Settings(
        _env_file=None,
        database_url=DSN,
        routers=[],
        backhauls=[],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
        db_auto_migrate=True,
        app_secret_key=generate_key(),
    )
    container = await build_container(settings)
    try:
        jobs = set(container.scheduler.job_names())
        orphelines = [r.name for r in REGLAGES if r.job and r.job not in jobs]
        assert not orphelines, f"cadences sans job : {orphelines}"
    finally:
        await shutdown_container(container)


# ---------------------------------------------------------------------------
# UN ROUTEUR RETIRE DOIT SORTIR DE L'ARBRE
#
# La persistance est volontairement additive : un PoP momentanement illisible ne
# doit pas disparaitre. Mais un routeur SUPPRIME de l'inventaire n'en sortait
# jamais non plus -- l'exploitant le retirait depuis l'interface et le voyait
# encore, sans rien pour l'expliquer.
# ---------------------------------------------------------------------------
def _graphe_deux_routeurs() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    for nom in ("pop-1", "core-1"):
        snapshot.add_node(
            TopologyNode(
                key=f"router:{nom}",
                name=nom,
                kind="pop",
                router_name=nom,
                attributes={"managed": True},
            )
        )
    snapshot.add_node(TopologyNode(key="mac:DC:9F:DB:11:22:33", name="BH-Nord", kind="radio"))
    snapshot.add_link(
        TopologyLink(
            source_key="router:pop-1",
            target_key="router:core-1",
            kind="ethernet",
            interface="ether1",
            discovered_by="pop-1",
        )
    )
    snapshot.add_link(
        TopologyLink(
            source_key="router:core-1",
            target_key="mac:DC:9F:DB:11:22:33",
            kind="ethernet",
            interface="ether5",
            discovered_by="core-1",
        )
    )
    return snapshot


async def test_un_routeur_retire_sort_du_graphe(database: Database) -> None:
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_deux_routeurs())

    efface = await repo.forget_removed_routers(["pop-1"])

    assert efface == 1
    cles = {n["key"] for n in await repo.nodes()}
    assert "router:core-1" not in cles
    assert "router:pop-1" in cles
    # Le reste du graphe n'est pas touche : la radio existe toujours.
    assert "mac:DC:9F:DB:11:22:33" in cles


async def test_les_liens_du_routeur_retire_partent_avec_lui(database: Database) -> None:
    """Un lien vers une case effacee, ou decouvert par le routeur parti, n'a
    plus personne pour le rafraichir."""
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_deux_routeurs())

    await repo.forget_removed_routers(["pop-1"])

    assert await repo.links() == []


async def test_un_inventaire_vide_n_efface_rien(database: Database) -> None:
    """Garde-fou : une base momentanement illisible rend un inventaire vide.
    Purger la-dessus viderait tout l'arbre sur un incident passager."""
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_deux_routeurs())

    assert await repo.forget_removed_routers([]) == 0
    assert len(await repo.nodes()) == 3


async def test_un_inventaire_inchange_n_efface_rien(database: Database) -> None:
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_deux_routeurs())

    assert await repo.forget_removed_routers(["pop-1", "core-1"]) == 0
    assert len(await repo.nodes()) == 3


# ---------------------------------------------------------------------------
# OUBLIER CE QUI A VRAIMENT DISPARU
#
# ``save_snapshot`` n'efface jamais rien, a dessein : un equipement
# momentanement invisible ne doit pas quitter l'arbre. Le revers, c'est qu'une
# adresse de gestion changee, un lien de test demonte ou un voisin croise
# pendant une migration y restaient POUR TOUJOURS. Aucun geste ne permettait de
# les retirer, sinon masquer les cases une par une.
# ---------------------------------------------------------------------------
async def _vieillir(database: Database, cles: list[str], minutes: int) -> None:
    async with database.pool.acquire() as conn:
        await conn.execute(
            "UPDATE topology_nodes SET last_seen = now() - ($2 || ' minutes')::interval "
            " WHERE key = ANY($1::text[])",
            cles,
            str(minutes),
        )


def _graphe_avec_fantomes() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    snapshot.add_node(
        TopologyNode(key="router:DS-CCR", name="DS-CCR", kind="core", attributes={"managed": True})
    )
    snapshot.add_node(TopologyNode(key="mac:AA:VIVANT", name="NAS-AGADEZ", kind="pop"))
    snapshot.add_node(TopologyNode(key="ip:10.255.255.2", name="10.255.255.2", kind="unknown"))
    snapshot.add_node(TopologyNode(key="mac:BB:FANTOME", name="MikroTik", kind="pop"))
    for cible, port in (
        ("mac:AA:VIVANT", "ether1"),
        ("ip:10.255.255.2", "ether2"),
        ("mac:BB:FANTOME", "ether3"),
    ):
        snapshot.add_link(
            TopologyLink(
                source_key="router:DS-CCR",
                target_key=cible,
                kind="ethernet",
                interface=port,
                discovered_by="DS-CCR",
            )
        )
    return snapshot


async def test_les_cases_disparues_sont_oubliees(database: Database) -> None:
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_avec_fantomes())
    await _vieillir(database, ["ip:10.255.255.2", "mac:BB:FANTOME"], 240)

    compte = await repo.forget_stale(older_than_minutes=60)

    assert compte["nodes"] == 2
    cles = {n["key"] for n in await repo.nodes()}
    assert cles == {"router:DS-CCR", "mac:AA:VIVANT"}


async def test_un_equipement_encore_vu_reste(database: Database) -> None:
    """Le seuil protege ce qui vit : sans cela on effacerait un PoP en cours de
    redemarrage."""
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_avec_fantomes())

    assert (await repo.forget_stale(older_than_minutes=60))["nodes"] == 0
    assert len(await repo.nodes()) == 4


async def test_un_routeur_de_l_inventaire_n_est_jamais_oublie(database: Database) -> None:
    """Sa case est DECLAREE, pas decouverte : elle doit rester meme injoignable
    depuis des jours -- c'est tout l'interet de la poser."""
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_avec_fantomes())
    # TOUT est vieilli, le routeur declare comme les cases decouvertes.
    await _vieillir(
        database,
        ["router:DS-CCR", "mac:AA:VIVANT", "ip:10.255.255.2", "mac:BB:FANTOME"],
        60 * 24 * 30,
    )

    await repo.forget_stale(older_than_minutes=60)

    assert {n["key"] for n in await repo.nodes()} == {"router:DS-CCR"}


async def test_une_case_reliee_a_la_main_est_epargnee(database: Database) -> None:
    """Un lien pose a la main est une DECISION d'exploitant, pas une
    observation : l'effacer supprimerait son travail sans le dire."""
    repo = TopologyRepository(database.pool)
    await repo.save_snapshot(_graphe_avec_fantomes())
    await repo.add_manual_link("router:DS-CCR", "mac:BB:FANTOME")
    await _vieillir(database, ["ip:10.255.255.2", "mac:BB:FANTOME"], 240)

    compte = await repo.forget_stale(older_than_minutes=60)

    assert compte["nodes"] == 1
    cles = {n["key"] for n in await repo.nodes()}
    assert "mac:BB:FANTOME" in cles
    assert "ip:10.255.255.2" not in cles


# =========================================================================
# Capacite : le SQL des quatre analyses qui portent sur la duree
# =========================================================================


async def test_les_analyses_de_capacite_repondent_sur_du_sql_reel(
    database: Database, now: datetime
) -> None:
    """Ces quatre requetes ne peuvent pas etre validees par un double memoire :
    integration de debits, agregats par instant, LATERAL sur la topologie. Une
    erreur SQL y passerait inapercue jusqu'a la page de production."""
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    bavard = await directory.ensure_subscriber("bavard", pop_id=pop_id, plan=Plan(100, 20, "mock"))
    muet = await directory.ensure_subscriber("muet", pop_id=pop_id, plan=Plan(50, 10, "mock"))
    backhaul_id = await directory.ensure_backhaul(
        "bh-nord", pop_id=pop_id, nominal_capacity_mbps=500
    )

    await writer.write_backhaul_metrics(
        [
            (
                backhaul_id,
                BackhaulSample(ts=now, device_id="bh-nord", capacity_mbps=400.0, online=True),
            )
        ]
    )
    # Le bavard consomme ; le muet existe mais n'a jamais rien fait passer.
    await writer.write_subscriber_metrics(
        [
            (
                bavard,
                SubscriberSample(
                    ts=now - timedelta(seconds=10 * i),
                    login="bavard",
                    router_name="pop-nord",
                    pop_name="PoP Nord",
                    rx_bps=10_000_000.0,
                    # 95 Mbps sur un plan a 100 : au plafond.
                    tx_bps=95_000_000.0,
                ),
            )
            for i in range(6)
        ]
        + [
            (
                muet,
                SubscriberSample(
                    ts=now - timedelta(days=30),
                    login="muet",
                    router_name="pop-nord",
                    pop_name="PoP Nord",
                    rx_bps=0.0,
                    tx_bps=0.0,
                ),
            )
        ]
    )
    await writer.write_interface_metrics(
        [
            InterfaceSample(
                ts=now - timedelta(seconds=10 * i),
                router_name="pop-nord",
                interface="ether2",
                rx_bps=20_000_000.0,
                tx_bps=900_000_000.0,
                capacity_mbps=1000.0,
            )
            for i in range(3)
        ]
    )

    # 1. Survente : 150 Mbps vendus sur 400 mesures, pointe a 95 Mbps.
    pops = await repo.capacity_by_pop(hours=24)
    nord = next(p for p in pops if p["pop_name"] == "PoP Nord")
    assert float(nord["sold_down_mbps"]) == 150.0
    assert float(nord["capacity_mbps"]) == 400.0
    assert float(nord["peak_bps"]) == 95_000_000.0

    # 2. Occupation : la pointe du port et l'heure a laquelle elle tombe.
    liens = await repo.link_occupancy(hours=24)
    port = next(ligne for ligne in liens if ligne["interface"] == "ether2")
    assert float(port["peak_tx_bps"]) == 900_000_000.0
    assert float(port["capacity_mbps"]) == 1000.0
    assert port["peak_tx_at"] is not None

    # 3. Volume : integration du debit sur la cadence reellement observee.
    usage = await repo.subscriber_usage(hours=24, limit=10)
    ligne = next(u for u in usage if u["login"] == "bavard")
    # 6 echantillons a 105 Mbps (10 + 95), cadence observee de 10 s. Chaque
    # echantillon PORTE les 10 secondes qui l'ont precede -- c'est ainsi que le
    # debit est calcule, par delta de compteurs -- donc 6 x 10 s de trafic et
    # non 5 : 630 Mbit/s / 8 x 10 s = 787,5 Mo.
    assert float(ligne["bytes"]) == pytest.approx(787_500_000.0)
    assert ligne["capped_samples"] == 6
    assert ligne["last_traffic_at"] is not None

    # 4. Lignes muettes : declarees, plus rien depuis des jours.
    muets = await repo.silent_subscribers(days=7, limit=10)
    assert [m["login"] for m in muets] == ["muet"]
    assert muets[0]["last_traffic_at"] is None


async def test_la_liste_des_abonnes_porte_tout_l_effectif_du_pop(
    database: Database, now: datetime
) -> None:
    """UN ABONNE DECLARE ET JAMAIS MESURE EXISTE QUAND MEME.

    La liste partait des MESURES : un abonne qui ne s'est jamais connecte, ou
    dont le PoP n'est plus collecte, n'apparaissait nulle part. Il etait
    indiscernable d'un abonne qui n'existe pas -- alors qu'il est facture.
    """
    directory = PgDirectory(database.pool)
    writer = PgMetricsWriter(database.pool)
    repo = MetricsRepository(database.pool)

    pop_id = await directory.ensure_pop("PoP Nord", "10.10.0.11")
    vu = await directory.ensure_subscriber("vu", pop_id=pop_id, plan=Plan(100, 20, "mock"))
    await directory.ensure_subscriber("jamais-vu", pop_id=pop_id, plan=Plan(50, 10, "mock"))
    await writer.write_subscriber_metrics(
        [
            (
                vu,
                SubscriberSample(
                    ts=now,
                    login="vu",
                    router_name="pop-nord",
                    pop_name="PoP Nord",
                    rx_bps=1_000_000.0,
                    tx_bps=9_000_000.0,
                ),
            )
        ]
    )

    mesures = await repo.subscriber_latest(limit=50)
    effectif = await repo.subscriber_latest(limit=50, include_unmeasured=True)

    assert [r["login"] for r in mesures] == ["vu"]
    assert sorted(r["login"] for r in effectif) == ["jamais-vu", "vu"]

    # Un abonne sans mesure sort avec des trous, JAMAIS avec des zeros : un zero
    # se lirait comme une absence de trafic, un trou dit qu'on ne sait pas.
    absent = next(r for r in effectif if r["login"] == "jamais-vu")
    assert absent["ts"] is None
    assert absent["rx_bps"] is None and absent["tx_bps"] is None
    # Son plan, lui, est connu : c'est bien un abonne, pas une ligne vide.
    assert absent["plan_down_mbps"] == 50
    assert absent["pop_name"] == "PoP Nord"

    # Le classement par debit ne le remonte pas devant celui qui consomme.
    assert [r["login"] for r in effectif] == ["vu", "jamais-vu"]

    # Et les filtres continuent de s'appliquer a l'effectif entier.
    par_nature = await repo.subscriber_latest(limit=50, include_unmeasured=True, kind="pppoe")
    assert sorted(r["login"] for r in par_nature) == ["jamais-vu", "vu"]
    par_recherche = await repo.subscriber_latest(limit=50, include_unmeasured=True, search="jamais")
    assert [r["login"] for r in par_recherche] == ["jamais-vu"]


async def test_position_libre_d_une_case_sans_equipement(database: Database) -> None:
    """Les etiquettes d'abonnes se rangent, alors qu'aucun equipement ne les porte.

    Elles sont calculees a l'affichage depuis la liste des abonnes : aucune
    ligne de topologie ne les attend, et c'est pour cela qu'elles etaient les
    seules cases de l'arbre qu'on ne pouvait pas deplacer. Leur position vit
    donc a part, plutot que dans de faux equipements qui apparaitraient ensuite
    dans tous les comptages.
    """
    from app.db.topology_repo import TopologyRepository

    repo = TopologyRepository(database.pool)
    cle = "abos:router:NAS-BASSORA|test-ba"

    assert await repo.set_node_position(cle, 822.0, 142.0) is True

    layout = await repo.node_layout()
    assert layout[cle] == {"pos_x": 822.0, "pos_y": 142.0}

    # Remettre en automatique OUBLIE la position : une ligne nulle resterait la
    # pour toujours sans rien dire.
    assert await repo.set_node_position(cle, None, None) is True
    assert cle not in await repo.node_layout()


async def test_un_site_de_vlan_dit_quel_routeur_le_dessert(database: Database) -> None:
    """Le referentiel doit porter le routeur d'un site de VLAN.

    C'est par lui que le shaping retrouve le routeur d'un abonne range dans un
    VLAN. Un site sans routeur serait un site dont les abonnes ne sont jamais
    brides -- et rien ne le dirait.
    """
    from app.db.directory import PgDirectory
    from app.db.repository import MetricsRepository

    directory = PgDirectory(database.pool)
    await directory.ensure_pop(
        "Francophonie",
        "10.10.0.11",
        kind="vlan",
        router_name="NAS-BASSORA",
        vlan_id=101,
        vlan_interface="vlan-francophonie",
    )

    sites = {s["name"]: s for s in await MetricsRepository(database.pool).pop_sites()}

    assert sites["Francophonie"]["router_name"] == "NAS-BASSORA"
    assert sites["Francophonie"]["kind"] == "vlan"
    assert sites["Francophonie"]["vlan_id"] == 101


async def test_un_site_de_routeur_ne_se_degrade_pas_en_site_de_vlan(
    database: Database,
) -> None:
    """Un homonyme de VLAN ne doit pas transformer un site de routeur en VLAN :
    le site declare par l'exploitant reste la verite."""
    from app.db.directory import PgDirectory
    from app.db.repository import MetricsRepository

    directory = PgDirectory(database.pool)
    await directory.ensure_pop("BASSORA", "10.10.0.11", router_name="NAS-BASSORA")
    directory.clear_cache()
    await directory.ensure_pop("BASSORA", None, kind="vlan", router_name="NAS-BASSORA", vlan_id=7)

    sites = {s["name"]: s for s in await MetricsRepository(database.pool).pop_sites()}

    assert sites["BASSORA"]["kind"] == "router"


async def test_les_vlan_observes_donnent_leur_nom_aux_sites(
    database: Database, now: datetime
) -> None:
    """Le NOM d'un site de VLAN vient du terrain, pas de la fiche du client.

    La fiche ne porte qu'un numero ; c'est l'observation ARP qui connait
    l'interface, donc le nom que l'exploitant a lui-meme ecrit sur son routeur.
    Ce SQL est ce qui fait le pont entre les deux -- une interface par couple
    (routeur, VLAN), la plus recemment vue.
    """
    observations = VlanSightingsRepository(database.pool)
    await observations.record(
        [
            _vue("10.0.0.3", vlan_interface="vlan-francophonie", vlan_id=101),
            _vue("10.0.0.4", vlan_interface="vlan-francophonie", vlan_id=101),
            _vue("10.0.1.5", vlan_interface="vlan-mairie", vlan_id=102),
            # Meme VLAN, autre routeur : deux sites, pas un.
            _vue("10.0.2.6", router_name="pop-sud", vlan_interface="vlan101", vlan_id=101),
        ],
        seen_at=now,
    )

    sites = await observations.vlan_sites()

    par_cle = {(s["router_name"], s["vlan_id"]): s for s in sites}
    assert len(sites) == 3
    assert par_cle[("pop-nord", 101)]["vlan_interface"] == "vlan-francophonie"
    assert par_cle[("pop-nord", 101)]["addresses_seen"] == 2
    assert par_cle[("pop-sud", 101)]["vlan_interface"] == "vlan101"


async def test_un_vlan_muet_depuis_longtemps_n_est_plus_un_site(
    database: Database, now: datetime
) -> None:
    """Un VLAN sur lequel plus rien ne parle n'est plus un site vivant."""
    observations = VlanSightingsRepository(database.pool)
    await observations.record(
        [_vue("10.0.0.3", vlan_interface="vlan-francophonie", vlan_id=101)],
        seen_at=now - timedelta(days=3),
    )

    assert await observations.vlan_sites(max_age_s=3600) == []
    assert len(await observations.vlan_sites()) == 1


# =========================================================================
# API PUBLIQUE ET TRAFIC : le SQL que les doubles memoire ne verifient pas
# =========================================================================


async def test_les_cles_d_api_ne_stockent_jamais_le_secret(database: Database) -> None:
    from app.db.api_keys_repo import ApiKeysRepository
    from app.services.api_keys import hash_key

    depot = ApiKeysRepository(database.pool)
    fiche, secret = await depot.create(name="facturation", scopes=["write"], created_by="test")
    assert fiche["scopes"] == ["read", "write"]

    async with database.pool.acquire() as conn:
        stocke = await conn.fetchval("SELECT key_hash FROM api_keys WHERE id = $1", fiche["id"])
    assert stocke == hash_key(secret)
    assert secret not in stocke

    assert (await depot.authenticate(secret)) is not None
    assert (await depot.authenticate(secret + "x")) is None

    await depot.set_enabled(fiche["id"], enabled=False)
    assert (await depot.authenticate(secret)) is None


async def test_un_service_de_l_api_devient_un_client_declare(database: Database) -> None:
    """LE POINT LE PLUS STRUCTURANT DU MODELE : l'API n'a pas sa propre table de
    clients. Elle ecrit dans l'inventaire que l'interface montre deja."""
    from app.db.model_repo import ModelRepository

    depot = ModelRepository(database.pool)
    await depot.put_site("tour-nord", {"name": "PoP Nord"})
    await depot.put_access_point("sect-n1", {"name": "Secteur N1", "tower": "tour-nord"})
    await depot.put_package(
        "pack-100", {"name": "100/20", "down_speed": 100_000, "up_speed": 20_000}
    )
    await depot.put_account("cust-41", {"name": "Mairie de Vitre"})

    fiche = await depot.put_service(
        "svc-4321",
        {
            "account": "cust-41",
            "package": "pack-100",
            "parent_device_id": "sect-n1",
            "attachments": [
                {"cpe_mac": "00:10:0b:6e:4c:ff", "network_prefixes": ["10.0.0.0/29", "10.0.1.5"]},
            ],
        },
    )
    # Le PoP remonte par la chaine service -> point d'acces -> site.
    assert fiche["pop_name"] == "PoP Nord"
    assert fiche["down_speed"] == 100_000
    assert fiche["attachments"][0]["network_prefixes"] == ["10.0.0.0/29", "10.0.1.5/32"]

    async with database.pool.acquire() as conn:
        ligne = await conn.fetchrow(
            "SELECT source, pop_name, plan_down_mbps, cpe_mac, extra_prefixes "
            "FROM static_clients WHERE reference = 'svc-4321'"
        )
    assert ligne["source"] == "api"
    assert ligne["pop_name"] == "PoP Nord"
    assert ligne["plan_down_mbps"] == 100.0  # 100 000 kbit/s convertis a la frontiere
    assert ligne["cpe_mac"] == "00:10:0B:6E:4C:FF"

    # Le PUT est idempotent : la facturation doit pouvoir rejouer son inventaire.
    await depot.put_service(
        "svc-4321", {"package": "pack-100", "attachments": [{"network_prefixes": ["10.0.0.0/29"]}]}
    )
    assert len(await depot.list_services()) == 1


async def test_l_api_n_ecrase_pas_une_fiche_saisie_a_la_main(database: Database) -> None:
    from app.db.model_repo import ModelConflictError, ModelRepository
    from app.db.static_clients_repo import StaticClientsRepository

    saisie = StaticClientsRepository(database.pool)
    await saisie.create(
        {
            "reference": "mairie-vitre",
            "pop_name": "PoP Nord",
            "address": "10.0.0.5",
            "vlan": 812,
            "plan_down_mbps": 50,
            "plan_up_mbps": 10,
        }
    )
    depot = ModelRepository(database.pool)
    with pytest.raises(ModelConflictError):
        await depot.put_service(
            "mairie-vitre", {"attachments": [{"network_prefixes": ["10.9.9.9"]}]}
        )
    with pytest.raises(ModelConflictError):
        await depot.delete_service("mairie-vitre")

    async with database.pool.acquire() as conn:
        intacte = await conn.fetchrow(
            "SELECT source, host(address) AS address FROM static_clients "
            "WHERE reference = 'mairie-vitre'"
        )
    assert intacte["source"] == "manual"
    assert intacte["address"] == "10.0.0.5"


async def test_les_clients_sont_ranges_par_vlan(database: Database) -> None:
    from app.db.static_clients_repo import StaticClientsRepository

    depot = StaticClientsRepository(database.pool)
    for reference, vlan in (("c1", 812), ("c2", 812), ("c3", 900)):
        await depot.create(
            {
                "reference": reference,
                "pop_name": "PoP Nord",
                "address": f"10.0.{vlan % 250}.{len(reference)}",
                "vlan": vlan,
                "plan_down_mbps": 50,
                "plan_up_mbps": 10,
            }
        )
    await depot.create(
        {
            "reference": "sans-vlan",
            "pop_name": "PoP Nord",
            "address": "10.5.0.1",
            "plan_down_mbps": 20,
            "plan_up_mbps": 5,
        }
    )
    lignes = {r["vlan"]: r for r in await depot.by_vlan()}
    assert set(lignes) == {812, 900}  # la fiche sans VLAN n'y figure pas
    assert lignes[812]["clients"] == 2
    assert lignes[812]["vendu_down_mbps"] == 100.0
    assert lignes[812]["depuis_api"] == 0


async def test_une_fenetre_de_trafic_s_ecrit_et_se_relit(database: Database, now: datetime) -> None:
    from app.db.flows_repo import FlowsRepository
    from app.services.flows import AppCounters, FlushBatch, HostCounters, SubscriberCounters

    async with database.pool.acquire() as conn:
        pop_id = await conn.fetchval("INSERT INTO pops (name) VALUES ('PoP Nord') RETURNING id")
        abonne = await conn.fetchval(
            "INSERT INTO subscribers (login, kind, pop_id) VALUES ('dupont', 'pppoe', $1) "
            "RETURNING id",
            pop_id,
        )

    depot = FlowsRepository(database.pool)
    lot = FlushBatch(
        ts=now,
        subscribers=[
            SubscriberCounters(abonne, "edge", down_bytes=1000, up_bytes=200, flows=3),
            # LE MEME OCTET, VU AU PoP : il ne doit pas se fondre dans le precedent.
            SubscriberCounters(abonne, "pop", down_bytes=1000, up_bytes=200, flows=3),
        ],
        apps=[AppCounters(abonne, "web", down_bytes=900, up_bytes=100)],
        hosts=[
            HostCounters("172.16.9.9", 812, exporter="10.10.0.1", pop_name="PoP Nord", up_bytes=90),
            # Sans etiquette VLAN : le cas le plus COURANT cote sortie internet.
            HostCounters("172.16.9.10", None, exporter="10.10.0.2", down_bytes=40),
        ],
    )
    assert await depot.write_batch(lot) == 2

    # Rejouer la meme fenetre CUMULE, ne duplique pas.
    await depot.write_batch(lot)

    totaux = await depot.totals(minutes=60, vantage="edge")
    assert totaux["down_bytes"] == 2000
    assert totaux["subscribers"] == 1

    top = await depot.top_subscribers(minutes=60, vantage="edge", limit=10)
    assert top[0]["login"] == "dupont"
    assert top[0]["down_bytes"] == 2000

    usages = await depot.applications(minutes=60)
    assert usages[0]["app"] == "web"

    serie = await depot.subscriber_series(
        subscriber_id=abonne,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
        bucket_seconds=60,
        vantage="edge",
    )
    assert sum(int(p["down_bytes"]) for p in serie) == 2000

    hotes = await depot.hosts(limit=10)
    par_adresse = {h["address"]: h for h in hotes}
    assert par_adresse["172.16.9.9"]["vlan_id"] == 812
    # La sentinelle 0 est retraduite en absence d'etiquette.
    assert par_adresse["172.16.9.10"]["vlan_id"] is None
    assert par_adresse["172.16.9.10"]["down_bytes"] == 80

    vlans = await depot.vlans()
    assert [v["vlan_id"] for v in vlans] == [812]


async def test_un_hote_declare_disparait_de_l_aide_a_la_saisie(database: Database) -> None:
    """LA LISTE EST RELUE A CHAQUE APPEL, PAS GRAVEE A L'ECRITURE.

    Declarer un client doit le faire disparaitre TOUT DE SUITE, pas au prochain
    flush : sinon l'exploitant le redeclare, ou croit que sa saisie n'a rien fait.
    """
    from app.db.flows_repo import FlowsRepository
    from app.db.static_clients_repo import StaticClientsRepository
    from app.services.flows import FlushBatch, HostCounters

    depot = FlowsRepository(database.pool)
    await depot.write_batch(
        FlushBatch(
            ts=_maintenant(),
            subscribers=[],
            apps=[],
            hosts=[HostCounters("10.0.0.2", 812, up_bytes=10)],
        )
    )
    assert [h["address"] for h in await depot.hosts(limit=10)] == ["10.0.0.2"]

    await StaticClientsRepository(database.pool).create(
        {
            "reference": "nouveau",
            "pop_name": "PoP Nord",
            "address": "10.0.0.0/29",
            "vlan": 812,
            "plan_down_mbps": 50,
            "plan_up_mbps": 10,
        }
    )
    assert await depot.hosts(limit=10) == []


async def test_les_blocs_declares_alimentent_l_index_de_rattachement(
    database: Database,
) -> None:
    from app.db.flows_repo import FlowsRepository
    from app.db.model_repo import ModelRepository
    from app.db.static_clients_repo import StaticClientsRepository
    from app.services.flows import PrefixIndex

    async with database.pool.acquire() as conn:
        pppoe = await conn.fetchval(
            "INSERT INTO subscribers (login, kind, last_ip) "
            "VALUES ('dupont', 'pppoe', '10.20.0.10') RETURNING id"
        )
    await StaticClientsRepository(database.pool).create(
        {
            "reference": "mairie",
            "pop_name": "PoP Nord",
            "address": "10.0.0.0/29",
            "plan_down_mbps": 50,
            "plan_up_mbps": 10,
        }
    )
    async with database.pool.acquire() as conn:
        fixe = await conn.fetchval(
            "INSERT INTO subscribers (login, kind) VALUES ('mairie', 'static') RETURNING id"
        )
    # Un service de l'API porte plusieurs prefixes : tous doivent compter.
    await ModelRepository(database.pool).put_service(
        "svc-1", {"attachments": [{"network_prefixes": ["10.30.0.0/30", "10.31.0.0/30"]}]}
    )
    async with database.pool.acquire() as conn:
        service = await conn.fetchval(
            "INSERT INTO subscribers (login, kind) VALUES ('svc-1', 'static') RETURNING id"
        )

    entrees = await FlowsRepository(database.pool).subscriber_prefixes()
    index = PrefixIndex.build(entrees)
    assert index.lookup("10.20.0.10") == pppoe
    assert index.lookup("10.0.0.3") == fixe
    assert index.lookup("10.30.0.1") == service
    assert index.lookup("10.31.0.1") == service  # le prefixe additionnel aussi
    assert index.lookup("8.8.8.8") is None


async def test_un_exporteur_inconnu_s_inscrit_tout_seul(database: Database) -> None:
    """IL DOIT SE VOIR. Un PoP qui exporte vers un collecteur qui l'ignore en
    silence reste invisible pendant des semaines."""
    from app.db.flows_repo import NetflowExportersRepository

    depot = NetflowExportersRepository(database.pool)
    await depot.record_activity({"10.10.0.1": {"version": "v9", "packets": 3, "flows": 40}})
    lignes = await depot.list_all()
    assert lignes[0]["address"] == "10.10.0.1"
    assert lignes[0]["vantage"] == "unknown"
    assert lignes[0]["flows_seen"] == 40

    # L'activite s'ACCUMULE, elle ne remplace pas.
    await depot.record_activity({"10.10.0.1": {"version": "v9", "packets": 2, "flows": 10}})
    assert (await depot.list_all())[0]["flows_seen"] == 50

    # Declarer ensuite ne perd pas les compteurs deja observes.
    fiche = await depot.declare(
        {
            "address": "10.10.0.1",
            "name": "Sortie internet",
            "vantage": "edge",
            "sampling_rate": 1000,
        }
    )
    assert fiche["vantage"] == "edge"
    assert fiche["flows_seen"] == 50


async def test_une_fenetre_survit_a_un_abonne_disparu(database: Database, now: datetime) -> None:
    """UNE FICHE SUPPRIMEE ENTRE LA MESURE ET L'ECRITURE NE DOIT PAS EMPORTER LA
    FENETRE ENTIERE -- donc le trafic de tous les autres."""
    from app.db.flows_repo import FlowsRepository
    from app.services.flows import FlushBatch, SubscriberCounters

    async with database.pool.acquire() as conn:
        vivant = await conn.fetchval(
            "INSERT INTO subscribers (login, kind) VALUES ('vivant', 'pppoe') RETURNING id"
        )
    depot = FlowsRepository(database.pool)
    ecrites = await depot.write_batch(
        FlushBatch(
            ts=now,
            subscribers=[
                SubscriberCounters(vivant, "edge", down_bytes=500),
                SubscriberCounters(vivant + 9999, "edge", down_bytes=700),
            ],
            apps=[],
            hosts=[],
        )
    )
    assert ecrites == 2  # le lot est accepte en entier
    totaux = await depot.totals(minutes=60, vantage="edge")
    assert totaux["down_bytes"] == 500  # seule la ligne de l'abonne vivant est posee


async def test_la_consommation_se_lit_par_periode(database: Database, now: datetime) -> None:
    from app.db.flows_repo import FlowsRepository
    from app.services.flows import FlushBatch, SubscriberCounters

    async with database.pool.acquire() as conn:
        abonne = await conn.fetchval(
            "INSERT INTO subscribers (login, kind) VALUES ('svc-1', 'static') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO static_clients (reference, pop_name, address, source, account_ref) "
            "VALUES ('svc-1', 'PoP Nord', '10.0.0.5', 'api', 'cust-41')"
        )
    depot = FlowsRepository(database.pool)
    for recul in (0, 3600):
        await depot.write_batch(
            FlushBatch(
                ts=now - timedelta(seconds=recul),
                subscribers=[SubscriberCounters(abonne, "edge", down_bytes=1000, up_bytes=100)],
                apps=[],
                hosts=[],
            )
        )

    debut, fin = now - timedelta(days=1), now + timedelta(minutes=1)
    total = await depot.usage(start=debut, end=fin, vantage="edge")
    assert len(total) == 1
    assert total[0]["down_bytes"] == 2000
    assert total[0]["account"] == "cust-41"

    par_heure = await depot.usage(start=debut, end=fin, vantage="edge", bucket="hour")
    assert len(par_heure) == 2

    par_mois = await depot.usage(start=debut, end=fin, vantage="edge", bucket="month")
    assert sum(int(p["down_bytes"]) for p in par_mois) == 2000

    # L'autre point de mesure ne doit RIEN rendre : le compter aussi doublerait
    # la facture.
    assert await depot.usage(start=debut, end=fin, vantage="pop") == []


async def test_le_point_d_acces_du_facturier_n_usurpe_pas_un_secteur(
    database: Database,
) -> None:
    """``parent_device_id`` EST UN IDENTIFIANT EXTERNE, PAS UNE CLE DE TOPOLOGIE.

    Le recopier dans ``sector_key`` paraitrait utile et serait nuisible : le
    planificateur lit ``client.sector_key or rattachement_automatique``, donc une
    valeur qui ne designe aucun noeud MASQUE le rattachement reel -- celui que
    la jointure caller-id / UISP a etabli -- sans jamais rien rattacher elle-meme.
    Le client se retrouverait pendu a la racine, hors du partage du secteur qu'il
    sature pourtant.
    """
    from app.db.model_repo import ModelRepository

    await ModelRepository(database.pool).put_service(
        "svc-9",
        {
            "parent_device_id": "sect-n1",
            "attachments": [{"network_prefixes": ["10.0.0.5"]}],
        },
    )
    async with database.pool.acquire() as conn:
        ligne = await conn.fetchrow(
            "SELECT sector_key, access_point_ref FROM static_clients WHERE reference = 'svc-9'"
        )
    assert ligne["access_point_ref"] == "sect-n1"
    assert ligne["sector_key"] is None

    # Un appelant qui NOMME une cle de topologie, lui, est suivi.
    await ModelRepository(database.pool).put_service(
        "svc-10",
        {
            "parent_device_id": "sect-n1",
            "sector_key": "mac:AA:BB:CC:DD:EE:FF",
            "attachments": [{"network_prefixes": ["10.0.0.6"]}],
        },
    )
    async with database.pool.acquire() as conn:
        secteur = await conn.fetchval(
            "SELECT sector_key FROM static_clients WHERE reference = 'svc-10'"
        )
    assert secteur == "mac:AA:BB:CC:DD:EE:FF"


async def test_la_mac_du_cpe_est_normalisee_a_la_modification(database: Database) -> None:
    from app.db.static_clients_repo import StaticClientsRepository

    depot = StaticClientsRepository(database.pool)
    fiche = await depot.create(
        {
            "reference": "mairie",
            "pop_name": "PoP Nord",
            "address": "10.0.0.5",
            "cpe_mac": "aa-bb-cc-dd-ee-ff",
            "plan_down_mbps": 50,
            "plan_up_mbps": 10,
        }
    )
    assert fiche["cpe_mac"] == "AA:BB:CC:DD:EE:FF"
    modifiee = await depot.update(fiche["id"], {"cpe_mac": "11-22-33-44-55-66"})
    assert modifiee["cpe_mac"] == "11:22:33:44:55:66"


async def test_un_client_declare_sur_une_seule_adresse_n_est_plus_propose(
    database: Database,
) -> None:
    """``<<=`` ET NON ``<<`` : L'OPERATEUR STRICT EXCLUT L'EGALITE.

    Un client declare sur une adresse unique porte un /32. Avec l'operateur
    strict, ``10.0.0.5 << 10.0.0.5/32`` est FAUX : la fiche existait, la file
    etait posee, et l'adresse restait malgre tout proposee comme "non declaree",
    indefiniment. L'exploitant la redeclarait, ou concluait que sa saisie
    n'avait servi a rien.
    """
    from app.db.flows_repo import FlowsRepository
    from app.db.static_clients_repo import StaticClientsRepository
    from app.services.flows import FlushBatch, HostCounters

    depot = FlowsRepository(database.pool)
    await depot.write_batch(
        FlushBatch(
            ts=_maintenant(),
            subscribers=[],
            apps=[],
            hosts=[
                HostCounters("10.0.0.5", 812, up_bytes=10),
                HostCounters("10.0.1.5", None, up_bytes=10),
                HostCounters("10.20.0.10", None, up_bytes=10),
                HostCounters("172.16.9.9", 900, up_bytes=10),
            ],
        )
    )
    assert len(await depot.hosts(limit=10)) == 4

    saisie = StaticClientsRepository(database.pool)
    # Adresse unique : c'est le cas que l'operateur strict manquait.
    await saisie.create(
        {
            "reference": "unique",
            "pop_name": "PoP Nord",
            "address": "10.0.0.5",
            "plan_down_mbps": 50,
            "plan_up_mbps": 10,
        }
    )
    # Prefixe ADDITIONNEL d'un service de l'API : il compte comme le premier.
    from app.db.model_repo import ModelRepository

    await ModelRepository(database.pool).put_service(
        "svc-2",
        {"attachments": [{"network_prefixes": ["10.9.0.0/30", "10.0.1.5"]}]},
    )
    # Abonne PPPoE : son adresse de session compte aussi.
    async with database.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO subscribers (login, kind, last_ip) "
            "VALUES ('dupont', 'pppoe', '10.20.0.10')"
        )

    restants = [h["address"] for h in await depot.hosts(limit=10)]
    assert restants == ["172.16.9.9"], f"encore proposes a tort : {restants}"


async def test_les_destinations_se_regroupent_par_lieu(database: Database) -> None:
    """La carte : un point par lieu, les pays au complet, et ce qui n'est pas
    localise rendu A PART plutot qu'ecarte -- sans quoi la carte laisserait
    croire que tout le trafic y figure."""
    from app.db.destinations_repo import DestinationsRepository

    async with database.pool.acquire() as conn:
        await conn.execute("TRUNCATE flow_destinations, ip_intel")
        await conn.executemany(
            "INSERT INTO ip_intel (address, service, category, org, country, city, "
            "latitude, longitude) VALUES ($1::inet, $2, $3, $4, $5, $6, $7, $8)",
            [
                ("45.57.0.1", "netflix", "streaming", "Netflix", "FR", "Paris", 48.8566, 2.3522),
                # Meme centre de donnees, a quelques metres : UN seul point.
                ("45.57.0.2", "netflix", "streaming", "Netflix", "FR", "Paris", 48.8571, 2.3519),
                ("8.8.8.8", None, None, "Google", "US", "Mountain View", 37.39, -122.08),
                ("1.2.3.4", None, None, None, None, None, None, None),
            ],
        )
        await conn.executemany(
            "INSERT INTO flow_destinations (client, address, down_bytes, up_bytes, flows) "
            "VALUES ($1::inet, $2::inet, $3, $4, 1)",
            [
                ("10.0.0.2", "45.57.0.1", 5_000, 100),
                ("10.0.0.3", "45.57.0.1", 3_000, 100),
                ("10.0.0.2", "45.57.0.2", 1_000, 0),
                ("10.0.0.2", "8.8.8.8", 500, 50),
                ("10.0.0.4", "1.2.3.4", 700, 0),
            ],
        )

    repo = DestinationsRepository(database.pool)
    lieux = await repo.by_location(minutes=60)

    paris, mv = lieux["points"]
    assert paris["city"] == "Paris"
    assert paris["latitude"] == 48.86 and paris["longitude"] == 2.35
    assert paris["addresses"] == 2
    assert paris["clients"] == 2
    assert paris["down_bytes"] == 9_000
    assert paris["services"] == ["netflix"]
    # Les adresses les plus lourdes d'abord, sans doublon.
    assert paris["top_addresses"] == ["45.57.0.1", "45.57.0.2"]
    assert mv["country"] == "US"

    pays = {c["country"]: c for c in lieux["countries"]}
    assert pays["FR"]["cities"] == 1
    assert pays[None]["down_bytes"] == 700
    assert lieux["unlocated"] == {"addresses": 1, "bytes": 700}

    # Les filtres de la page s'appliquent aussi a la carte.
    seuls = await repo.by_location(minutes=60, category="streaming")
    assert [p["city"] for p in seuls["points"]] == ["Paris"]
    cherche = await repo.by_location(minutes=60, search="mountain")
    assert [p["city"] for p in cherche["points"]] == ["Mountain View"]

    # La liste des destinations porte desormais la position.
    top = await repo.top(minutes=60)
    assert {d["address"]: d["city"] for d in top}["45.57.0.1"] == "Paris"
    assert top[0]["latitude"] == 48.8566

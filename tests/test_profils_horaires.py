"""Profils de debit horaires : jour, soiree, nuit -- et le changement tout seul.

Ce fichier verrouille :
  1. la saisie : plages, jours, minuit, fuseau, refus des recouvrements ;
  2. l'heure : locale au profil, heure d'ete comprise ;
  3. la priorite : boost > surcharge > horaire > forfait, et UN profil par
     abonne (le plus precis) ;
  4. le plan RouterOS : la file porte le debit de la plage en cours ;
  5. la bascule : rien au premier passage, une ecriture quand la plage change,
     rien d'ecrit quand l'enforcement est coupe ;
  6. l'API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.db.repository import _with_effective_limits
from app.db.schedules_repo import ScheduleConflictError, ScheduleNotFoundError
from app.enforcement import schedules as module_horaire
from app.enforcement.planner import (
    SOURCE_BOOST,
    SOURCE_OVERRIDE,
    SOURCE_PLAN,
    SOURCE_SCHEDULE,
    SubscriberTarget,
    desired_state,
    effective_rate,
)
from app.enforcement.schedules import (
    BandwidthSchedule,
    InvalidScheduleError,
    ScheduleBook,
    parse_windows,
)
from app.main import register_routes
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_clients_statiques import InventaireMemoire, fiche
from tests.test_enforcement import FauxClientEcriture
from tests.test_shaping_service import DepotBoosts, MetriquesMinimales, make_service

SOIREE_NUIT = [
    {"label": "evening", "start": "18:00", "end": "23:00", "percent": 50},
    {"label": "night", "start": "23:00", "end": "06:00", "percent": 200},
]


def profil(ident: int = 1, **kwargs) -> BandwidthSchedule:
    base = {"id": ident, "name": f"profil-{ident}", "windows": SOIREE_NUIT, "timezone": "UTC"}
    return BandwidthSchedule.build(**{**base, **kwargs})


def a(heure: str, jour: int = 12) -> datetime:
    """Lundi 12 octobre 2026 (UTC) a l'heure dite."""
    h, m = (int(x) for x in heure.split(":"))
    return datetime(2026, 10, jour, h, m, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _carnet_vide():
    """Le carnet est global (le planificateur et l'affichage le partagent) :
    chaque test repart d'un carnet vide."""
    module_horaire.BOOK.replace([])
    yield
    module_horaire.BOOK.replace([])


# =========================================================================
# 1. Saisie
# =========================================================================


def test_une_plage_qui_passe_minuit() -> None:
    nuit = profil()
    assert nuit.window_at(a("23:30")).label == "night"
    assert nuit.window_at(a("05:59", jour=13)).label == "night"
    assert nuit.window_at(a("06:00", jour=13)) is None
    assert nuit.window_at(a("12:00")) is None
    assert nuit.window_at(a("18:00")).label == "evening"


def test_les_jours_sont_ceux_ou_la_plage_commence() -> None:
    """La nuit du vendredi court jusqu'au samedi matin, pas celle du samedi."""
    vendredi = profil(windows=[{"start": "22:00", "end": "06:00", "days": ["fri"], "percent": 300}])
    assert vendredi.window_at(a("23:00", jour=16)) is not None  # vendredi soir
    assert vendredi.window_at(a("02:00", jour=17)) is not None  # samedi 2h
    assert vendredi.window_at(a("02:00", jour=18)) is None  # dimanche 2h
    assert vendredi.window_at(a("23:00", jour=17)) is None  # samedi soir


def test_une_journee_entiere_s_ecrit_24h() -> None:
    dimanche = profil(windows=[{"start": "00:00", "end": "24:00", "days": ["sun"], "percent": 150}])
    assert dimanche.window_at(a("00:00", jour=18)) is not None
    assert dimanche.window_at(a("23:59", jour=18)) is not None
    assert dimanche.window_at(a("00:00", jour=19)) is None


@pytest.mark.parametrize(
    ("plages", "motif"),
    [
        ([], "at least one"),
        ([{"start": "18:00", "end": "18:00", "percent": 50}], "empty"),
        ([{"start": "25:00", "end": "18:00", "percent": 50}], "invalid time"),
        ([{"start": "18:00", "end": "23:00"}], "needs a rate"),
        ([{"start": "18:00", "end": "23:00", "percent": 50, "down_mbps": 5}], "EITHER"),
        ([{"start": "18:00", "end": "23:00", "percent": 0}], "percent"),
        ([{"start": "18:00", "end": "23:00", "percent": 50, "days": ["xyz"]}], "unknown day"),
        (
            [
                {"start": "18:00", "end": "23:00", "percent": 50},
                {"start": "22:00", "end": "06:00", "percent": 200},
            ],
            "overlap",
        ),
    ],
)
def test_saisies_refusees(plages: list, motif: str) -> None:
    with pytest.raises(InvalidScheduleError, match=motif):
        parse_windows(plages)


def test_des_plages_sur_des_jours_differents_ne_se_chevauchent_pas() -> None:
    plages = parse_windows(
        [
            {"start": "18:00", "end": "23:00", "days": ["mon", "tue"], "percent": 50},
            {"start": "17:00", "end": "23:00", "days": ["sat", "sun"], "percent": 40},
        ]
    )
    assert len(plages) == 2


def test_fuseau_inconnu_refuse() -> None:
    with pytest.raises(InvalidScheduleError, match="time zone"):
        profil(timezone="Mars/Olympus")


def test_une_portee_ciblee_exige_des_cibles() -> None:
    with pytest.raises(InvalidScheduleError, match="at least one target"):
        profil(scope="pops", targets=[])
    with pytest.raises(InvalidScheduleError, match="plan"):
        profil(scope="plans", targets=["vingt"])
    assert profil(scope="plans", targets=["20", "20.0", 50]).targets == ("20", "50")


# =========================================================================
# 2. L'heure locale
# =========================================================================


def test_l_heure_est_celle_du_fuseau_du_profil() -> None:
    """18:00 a Paris, c'est 16:00 UTC l'ete et 17:00 UTC l'hiver."""
    paris = profil(timezone="Europe/Paris")
    ete = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    hiver = datetime(2026, 12, 1, 17, 0, tzinfo=UTC)
    assert paris.window_at(ete).label == "evening"
    assert paris.window_at(hiver).label == "evening"
    assert paris.window_at(ete - timedelta(minutes=1)) is None
    assert paris.window_at(hiver - timedelta(minutes=1)) is None


def test_la_prochaine_bascule() -> None:
    assert profil().next_change(a("19:30")) == a("23:00")
    assert profil().next_change(a("12:00")) == a("18:00")


# =========================================================================
# 3. Priorite
# =========================================================================


def test_la_hierarchie_des_debits() -> None:
    plus_tard = datetime.now(tz=UTC) + timedelta(hours=1)
    commun = {"plan_mbps": 20.0, "schedule_mbps": 40.0}
    assert effective_rate(**commun, override_mbps=None, boost_mbps=None, boost_expires_at=None) == (
        40.0,
        SOURCE_SCHEDULE,
    )
    assert effective_rate(**commun, override_mbps=5.0, boost_mbps=None, boost_expires_at=None) == (
        5.0,
        SOURCE_OVERRIDE,
    )
    assert effective_rate(
        **commun, override_mbps=5.0, boost_mbps=100.0, boost_expires_at=plus_tard
    ) == (100.0, SOURCE_BOOST)
    assert effective_rate(
        plan_mbps=20.0, override_mbps=None, boost_mbps=None, boost_expires_at=None
    ) == (20.0, SOURCE_PLAN)


def test_le_profil_le_plus_precis_gouverne_seul() -> None:
    tous = profil(1, name="tous")
    site = profil(2, name="site", scope="pops", targets=["PoP Altair"])
    forfait = profil(3, name="forfait", scope="plans", targets=["20"])
    nomme = profil(4, name="nomme", scope="subscribers", targets=["dupont"])
    carnet = ScheduleBook([tous, site, forfait, nomme])

    def maitre(login: str, site_: str, plan: float) -> str | None:
        trouve = carnet.governing(login=login, sites=(site_,), plan_down=plan)
        return trouve.name if trouve else None

    assert maitre("dupont", "PoP Altair", 20) == "nomme"
    assert maitre("martin", "PoP Altair", 20) == "forfait"
    assert maitre("martin", "pop-altair", 50) == "site"  # nom de site normalise
    assert maitre("martin", "Ailleurs", 50) == "tous"


def test_a_precision_egale_le_plus_recent_gouverne() -> None:
    carnet = ScheduleBook([profil(1, name="ancien"), profil(7, name="recent")])
    assert carnet.governing(login="x", sites=(), plan_down=10).name == "recent"


def test_un_profil_suspendu_ne_gouverne_personne() -> None:
    carnet = ScheduleBook([profil(enabled=False)])
    assert carnet.rate_for(login="x", sites=(), plan_down=10, plan_up=2, now=a("19:00")) is None


def test_pourcentage_et_debit_fixe() -> None:
    carnet = ScheduleBook([profil()])
    soir = carnet.rate_for(login="x", sites=(), plan_down=20, plan_up=4, now=a("19:00"))
    assert (soir.down_mbps, soir.up_mbps) == (10, 2)
    nuit = carnet.rate_for(login="x", sites=(), plan_down=20, plan_up=4, now=a("02:00"))
    assert (nuit.down_mbps, nuit.up_mbps) == (40, 8)
    assert carnet.rate_for(login="x", sites=(), plan_down=20, plan_up=4, now=a("12:00")) is None

    fixe = ScheduleBook([profil(windows=[{"start": "18:00", "end": "23:00", "down_mbps": 8}])])
    soir = fixe.rate_for(login="x", sites=(), plan_down=20, plan_up=4, now=a("19:00"))
    # Un seul sens fixe : l'autre garde son forfait.
    assert (soir.down_mbps, soir.up_mbps) == (8, None)


def test_l_affichage_des_limites_lit_le_meme_carnet() -> None:
    journee = [{"start": "00:00", "end": "24:00", "percent": 50}]
    module_horaire.BOOK.replace([profil(windows=journee)])
    ligne = _with_effective_limits(
        {"login": "dupont", "pop_name": "PoP Test", "plan_down_mbps": 20, "plan_up_mbps": 4}
    )
    assert ligne["effective_down_mbps"] == 10
    assert ligne["effective_up_mbps"] == 2
    assert ligne["limit_source"] == SOURCE_SCHEDULE
    assert ligne["schedule_name"] == "profil-1"


# =========================================================================
# 4. Plan RouterOS
# =========================================================================


def test_la_file_porte_le_debit_de_la_plage() -> None:
    cible = SubscriberTarget(
        login="dupont",
        interface="<pppoe-dupont>",
        address="10.20.0.10",
        plan_down_mbps=20,
        plan_up_mbps=4,
        schedule_down_mbps=40,
        schedule_up_mbps=8,
    )
    _types, files, _ = desired_state(links=[], subscribers=[cible])
    assert files[0].max_limit == "8000000/40000000"


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


def service_horaire(settings: Settings, routeur: FakeRouterOsClient, **kwargs):
    service = make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        static_clients=InventaireMemoire([fiche()]),
        **kwargs,
    )
    service.metrics = MetriquesMinimales(
        [{"login": "dupont", "pop_name": "PoP Test", "plan_down_mbps": 20, "plan_up_mbps": 4}]
    )
    service.schedule_book = ScheduleBook(
        [profil(windows=[{"start": "00:00", "end": "24:00", "percent": 50}])]
    )
    return service


async def test_build_targets_pose_le_debit_horaire(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """PPPoE et client a IP fixe : le meme profil, le meme chemin."""
    service = service_horaire(settings, routeur)
    await service.registry.reload()

    _liens, abonnes = await service.build_targets("pop-test")

    par_login = {c.login: c for c in abonnes}
    assert par_login["dupont"].effective_down_at() == 10
    assert par_login["dupont"].rate_source() == SOURCE_SCHEDULE
    # Fiche statique : 200/50 declares -> 100/25 pendant la plage.
    assert par_login["mairie-vitre"].effective_down_at() == 100
    assert par_login["mairie-vitre"].effective_up_at() == 25


class DepotProfils:
    def __init__(self, profils: list[BandwidthSchedule]) -> None:
        self.profils = profils

    async def load_enabled(self) -> list[BandwidthSchedule]:
        return [p for p in self.profils if p.enabled]


async def test_premier_passage_rien_puis_bascule_ecrite(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = FauxClientEcriture()
    service = service_horaire(settings, routeur, write_client_factory=lambda c: ecriture)
    service.schedules_repo = DepotProfils([])
    await service.registry.reload()

    # Premier passage : aucune ecriture, la reconciliation du demarrage s'en charge.
    premier = await service.apply_schedules()
    assert premier["applied"] == 0 and ecriture.executed == []

    # Rien n'a change : rien a faire.
    assert (await service.apply_schedules())["applied"] == 0

    # Une plage commence (ici : un profil apparait en base) -> ecriture.
    service.schedules_repo.profils = [
        profil(windows=[{"start": "00:00", "end": "24:00", "percent": 50}])
    ]
    bascule = await service.apply_schedules()
    assert bascule["changed"] is True
    assert bascule["routers"] == ["pop-test"]
    commandes = [x.command for x in ecriture.executed]
    assert any("2000000/10000000" in c for c in commandes), commandes
    assert service.last_schedule_switch is bascule


async def test_sans_enforcement_rien_n_est_ecrit(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = service_horaire(settings, routeur)
    await service.registry.reload()

    resultat = await service.apply_schedules(force=True)

    assert resultat["routers"] == []
    assert "enforcement is off" in resultat["errors"][0]


async def test_une_base_illisible_garde_le_carnet(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Perdre les profils en pleine soiree remettrait tout le monde au forfait."""

    class DepotEnPanne:
        async def load_enabled(self):
            raise ConnectionError("base injoignable")

    service = service_horaire(settings, routeur)
    service.schedules_repo = DepotEnPanne()
    assert await service.load_schedules() == 1


# =========================================================================
# 6. API
# =========================================================================


class DepotProfilsApi:
    """Double du depot cote API, en memoire."""

    def __init__(self) -> None:
        self.lignes: list[dict] = []

    async def list_all(self) -> list[dict]:
        return [dict(x) for x in self.lignes]

    async def get(self, ident: int) -> dict:
        for ligne in self.lignes:
            if ligne["id"] == ident:
                return dict(ligne)
        raise ScheduleNotFoundError(str(ident))

    async def load_enabled(self) -> list[BandwidthSchedule]:
        from app.db.schedules_repo import to_model

        return [to_model(x) for x in self.lignes if x["enabled"]]

    async def create(self, payload: dict) -> dict:
        if any(x["name"] == payload["name"] for x in self.lignes):
            raise ScheduleConflictError(payload["name"])
        ligne = {**payload, "id": len(self.lignes) + 1}
        self.lignes.append(ligne)
        return dict(ligne)

    async def update(self, ident: int, payload: dict) -> dict:
        ligne = await self.get(ident)
        ligne.update(payload)
        self.lignes = [ligne if x["id"] == ident else x for x in self.lignes]
        return dict(ligne)

    async def delete(self, ident: int) -> None:
        await self.get(ident)
        self.lignes = [x for x in self.lignes if x["id"] != ident]


@pytest.fixture
def api(settings: Settings) -> tuple[TestClient, DepotProfilsApi, object]:
    depot = DepotProfilsApi()
    container = build_container(settings)
    container.schedules_repo = depot
    container.shaping.schedules_repo = depot
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), depot, container


def test_api_cree_valide_et_liste(api) -> None:
    client, depot, container = api
    reponse = client.post(
        "/api/v1/bandwidth-schedules",
        json={
            "name": "Soiree et nuit",
            "timezone": "Africa/Abidjan",
            "windows": [
                {"label": "evening", "start": "18:00", "end": "23:00", "percent": 70},
                {"label": "night", "start": "23:00", "end": "06:00", "down_mbps": 50},
            ],
        },
    )
    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["windows"][0]["days"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert "next_change_at" in corps
    # Enforcement coupe dans les tests : la reponse le dit, rien n'est ecrit.
    assert "enforcement is off" in corps["enforcement"]["errors"][0]
    # Le carnet est recharge tout de suite : le planificateur le voit.
    assert [p.name for p in container.shaping.schedule_book.schedules] == ["Soiree et nuit"]

    liste = client.get("/api/v1/bandwidth-schedules").json()
    assert [s["name"] for s in liste["schedules"]] == ["Soiree et nuit"]


def test_api_refuse_un_recouvrement(api) -> None:
    client, depot, _ = api
    reponse = client.post(
        "/api/v1/bandwidth-schedules",
        json={
            "name": "x",
            "windows": [
                {"start": "18:00", "end": "23:00", "percent": 70},
                {"start": "20:00", "end": "21:00", "percent": 10},
            ],
        },
    )
    assert reponse.status_code == 400
    assert "overlap" in reponse.json()["detail"]
    assert depot.lignes == []


def test_api_doublon_modification_suppression(api) -> None:
    client, depot, container = api
    corps = {"name": "nuit", "windows": [{"start": "23:00", "end": "06:00", "percent": 200}]}
    ident = client.post("/api/v1/bandwidth-schedules", json=corps).json()["id"]
    assert client.post("/api/v1/bandwidth-schedules", json=corps).status_code == 409

    modifie = client.patch(f"/api/v1/bandwidth-schedules/{ident}", json={"enabled": False})
    assert modifie.status_code == 200, modifie.text
    assert modifie.json()["enabled"] is False
    assert container.shaping.schedule_book.schedules == ()

    assert client.delete(f"/api/v1/bandwidth-schedules/{ident}").status_code == 200
    assert depot.lignes == []
    assert client.delete(f"/api/v1/bandwidth-schedules/{ident}").status_code == 404


def test_api_portee_par_site_sans_site_refusee(api) -> None:
    client, _, _ = api
    reponse = client.post(
        "/api/v1/bandwidth-schedules",
        json={
            "name": "x",
            "scope": "pops",
            "windows": [{"start": "18:00", "end": "23:00", "percent": 70}],
        },
    )
    assert reponse.status_code == 400


def test_api_apercu_des_clients_gouvernes(api) -> None:
    """« Qui est concerne, et a quel debit maintenant ? » -- le calcul du
    planificateur, profil plus precis compris."""
    client, _, _ = api
    toute_la_journee = [{"start": "00:00", "end": "24:00", "percent": 50}]
    general = client.post(
        "/api/v1/bandwidth-schedules", json={"name": "general", "windows": toute_la_journee}
    ).json()

    apercu = client.get(f"/api/v1/bandwidth-schedules/{general['id']}/preview").json()
    assert apercu["counts"] == {"governed": 1, "overridden": 0, "on_schedule_now": 1}
    [dupont] = apercu["subscribers"]
    assert (dupont["now_down_mbps"], dupont["now_up_mbps"]) == (50.0, 10.0)
    assert dupont["source"] == "schedule"

    # Un profil nomme, plus precis, prend dupont : le general ne le gouverne plus.
    client.post(
        "/api/v1/bandwidth-schedules",
        json={
            "name": "vip",
            "scope": "subscribers",
            "targets": ["dupont"],
            "windows": [{"start": "00:00", "end": "24:00", "percent": 300}],
        },
    )
    apercu = client.get(f"/api/v1/bandwidth-schedules/{general['id']}/preview").json()
    assert apercu["counts"]["governed"] == 0
    assert apercu["overridden"] == [{"login": "dupont", "governed_by": "vip"}]

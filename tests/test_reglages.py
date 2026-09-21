"""Reglages pilotes par la BASE, plus par l'environnement.

Regle verifiee ici : l'environnement ne fournit qu'un DEFAUT ; des qu'une valeur
existe en base, c'est elle qui fait foi, et elle prend effet sans redemarrage.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.main import register_routes
from app.scheduler import Scheduler
from app.services.runtime_config import (
    PAR_NOM,
    REGLAGES,
    ReglageInconnuError,
    RuntimeConfig,
    ValeurInvalideError,
)
from tests.test_api import InMemorySettingsRepository, build_container


# --------------------------------------------------------------- le moteur
def test_la_base_prime_sur_l_environnement(settings: Settings) -> None:
    """Le coeur de l'affaire : une valeur en base ecrase celle de l'env."""
    settings.cake_overhead = 22  # ce que disait l'environnement
    config = RuntimeConfig(settings)

    config.load({"cake_overhead": 38})

    # L'objet Settings partage par TOUT le controleur porte desormais la valeur
    # de la base : aucun consommateur n'a besoin d'etre modifie.
    assert settings.cake_overhead == 38
    entree = next(e for e in config.describe() if e["name"] == "cake_overhead")
    assert entree["value"] == 38
    assert entree["default"] == 22
    assert entree["source"] == "db"


def test_revenir_au_defaut(settings: Settings) -> None:
    settings.shaping_safety_factor = 0.90
    config = RuntimeConfig(settings)
    config.set("shaping_safety_factor", 0.70)
    assert settings.shaping_safety_factor == 0.70

    config.clear("shaping_safety_factor")

    assert settings.shaping_safety_factor == 0.90
    entree = next(e for e in config.describe() if e["name"] == "shaping_safety_factor")
    assert entree["source"] == "defaut"


def test_une_option_cake_peut_etre_volontairement_vide(settings: Settings) -> None:
    """None n'est pas 'pas de surcharge' : c'est 'ne pose pas ce champ CAKE'."""
    config = RuntimeConfig(settings)
    config.set("cake_diffserv", "diffserv4")
    assert settings.cake_diffserv == "diffserv4"

    config.set("cake_diffserv", None)

    assert settings.cake_diffserv is None
    # La valeur vide reste une surcharge posee en base, pas un retour au defaut.
    assert "cake_diffserv" in config.overrides


def test_valeurs_refusees(settings: Settings) -> None:
    config = RuntimeConfig(settings)
    with pytest.raises(ValeurInvalideError):
        config.set("shaping_safety_factor", 5.0)  # hors bornes
    with pytest.raises(ValeurInvalideError):
        config.set("cake_diffserv", "n-importe-quoi")  # hors des choix
    with pytest.raises(ValeurInvalideError):
        config.set("shaping_prune", "peut-etre")  # booleen invalide
    with pytest.raises(ValeurInvalideError):
        config.set("shaping_safety_factor", None)  # non nullable
    with pytest.raises(ReglageInconnuError):
        config.set("database_url", "postgres://ailleurs")  # hors perimetre
    # Rien n'a bouge.
    assert not config.overrides


def test_une_ligne_illisible_ne_bloque_pas_le_demarrage(settings: Settings) -> None:
    """Reglage retire depuis, ou valeur devenue hors bornes : on demarre quand
    meme sur le defaut plutot que de refuser de demarrer."""
    config = RuntimeConfig(settings)

    ignores = config.load(
        {"reglage_disparu": 1, "shaping_safety_factor": 99.0, "cake_overhead": 40}
    )

    assert sorted(ignores) == ["reglage_disparu", "shaping_safety_factor"]
    assert settings.cake_overhead == 40  # la ligne saine est bien appliquee


def test_les_booleens_acceptent_les_formes_usuelles(settings: Settings) -> None:
    config = RuntimeConfig(settings)
    assert config.set("shaping_prune", "false") is False
    assert config.set("shaping_prune", "oui") is True
    assert config.set("shaping_prune", 0) is False


def test_changer_une_cadence_reprogramme_le_scheduler(settings: Settings) -> None:
    """Une cadence changee doit atteindre la BOUCLE, pas seulement l'affichage."""
    scheduler = Scheduler()

    async def rien() -> None:
        return None

    scheduler.add_job("collect_subscribers", 10.0, rien)
    config = RuntimeConfig(settings, on_interval_change=scheduler.set_interval)

    config.set("subscriber_interval_s", 45.0)

    assert settings.subscriber_interval_s == 45.0
    job = next(j for j in scheduler.status() if j["job"] == "collect_subscribers")
    assert job["interval_s"] == 45.0


def test_une_cadence_nulle_est_refusee(settings: Settings) -> None:
    """Zero ferait tourner la boucle a vide, sans jamais dormir."""
    config = RuntimeConfig(settings)
    with pytest.raises(ValeurInvalideError):
        config.set("shaping_reconcile_interval_s", 0)


def test_le_registre_ne_couvre_que_des_reglages_reels(settings: Settings) -> None:
    """Un nom mal orthographie dans le registre passerait inapercu jusqu'au jour
    ou quelqu'un tenterait de le regler."""
    for reglage in REGLAGES:
        assert hasattr(settings, reglage.name), f"{reglage.name} n'existe pas dans Settings"
    assert len(PAR_NOM) == len(REGLAGES), "deux reglages portent le meme nom"


# ------------------------------------------------- effet reel sur le shaping
async def test_une_option_cake_de_la_base_atteint_le_plan(settings: Settings) -> None:
    """Bout en bout : une option posee en base doit sortir dans la commande
    RouterOS, sans redemarrage."""
    from app.services.registry import RouterRegistry
    from app.services.shaping import ShapingService
    from tests.conftest import FakeRouterOsClient

    client = FakeRouterOsClient()
    service = ShapingService(
        settings, registry=RouterRegistry(settings, client_factory=lambda config: client)
    )
    await service.registry.reload()
    config = RuntimeConfig(settings)

    # L'exploitant pose l'option depuis l'interface : aucune variable d'env.
    config.set("cake_nat", True)
    config.set("cake_diffserv", "diffserv4")

    from app.enforcement.planner import LinkTarget

    plan = await service.plan(
        "pop-test",
        links=[LinkTarget(name="bh", interface="ether2", measured_capacity_mbps=500)],
        subscribers=[],
    )
    commandes = [a.command for a in plan.actions if a.path == "/queue/type"]
    assert any("cake-nat=yes" in c for c in commandes)
    assert any("cake-diffserv=diffserv4" in c for c in commandes)


# ----------------------------------------------------------------- l'API
@pytest.fixture
def depot() -> InMemorySettingsRepository:
    return InMemorySettingsRepository()


@pytest.fixture
def client(settings: Settings, depot: InMemorySettingsRepository) -> TestClient:
    container = build_container(settings, settings_repo=depot)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    test_client = TestClient(app)
    test_client.container = container  # type: ignore[attr-defined]
    return test_client


def test_api_liste_les_reglages_et_ce_qui_reste_hors_de_portee(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()

    noms = {e["name"] for e in body["settings"]}
    assert {"cake_nat", "shaping_safety_factor", "shaping_reconcile_interval_s"} <= noms
    assert {"shaping", "cake", "enforcement", "cadences"} <= set(body["groups"])
    # L'interface doit dire franchement ce qui ne peut PAS venir de la base.
    assert any("DATABASE_URL" in e["name"] for e in body["bootstrap_only"])
    # Chaque entree porte de quoi construire un formulaire.
    entree = next(e for e in body["settings"] if e["name"] == "cake_diffserv")
    assert entree["kind"] == "choix"
    assert "diffserv4" in entree["choices"]
    assert entree["nullable"] is True
    assert entree["help"]


def test_api_fixe_un_reglage_et_le_persiste(
    client: TestClient, depot: InMemorySettingsRepository, settings: Settings
) -> None:
    reponse = client.put("/api/v1/settings/cake_nat", json={"value": True, "reason": "CGNAT"})

    assert reponse.status_code == 200
    assert reponse.json() == {"name": "cake_nat", "value": True, "source": "db", "applied": True}
    # Applique a chaud...
    assert settings.cake_nat is True
    # ... ET persiste, avec la raison.
    assert depot.rows["cake_nat"] is True
    assert depot.meta["cake_nat"]["reason"] == "CGNAT"

    corps = client.get("/api/v1/settings").json()
    assert "cake_nat" in corps["from_db"]


def test_api_refuse_une_valeur_hors_bornes(
    client: TestClient, depot: InMemorySettingsRepository, settings: Settings
) -> None:
    avant = settings.shaping_safety_factor

    reponse = client.put("/api/v1/settings/shaping_safety_factor", json={"value": 42})

    assert reponse.status_code == 422
    # Ni applique, ni persiste : on n'ecrit en base que ce qu'on a su appliquer.
    assert settings.shaping_safety_factor == avant
    assert "shaping_safety_factor" not in depot.rows


def test_api_reglage_inconnu(client: TestClient) -> None:
    assert client.put("/api/v1/settings/database_url", json={"value": "x"}).status_code == 404


def test_api_retour_au_defaut(
    client: TestClient, depot: InMemorySettingsRepository, settings: Settings
) -> None:
    defaut = settings.cake_overhead
    client.put("/api/v1/settings/cake_overhead", json={"value": 38})
    assert settings.cake_overhead == 38

    reponse = client.delete("/api/v1/settings/cake_overhead")

    assert reponse.status_code == 200
    assert settings.cake_overhead == defaut
    assert "cake_overhead" not in depot.rows


def test_api_journal_des_reglages(client: TestClient) -> None:
    client.put("/api/v1/settings/cake_wash", json={"value": True, "reason": "DSCP client douteux"})

    journal = client.get("/api/v1/settings/history").json()

    ligne = next(x for x in journal if x["name"] == "cake_wash")
    assert ligne["value"] is True
    assert ligne["reason"] == "DSCP client douteux"
    assert ligne["updated_by"] == "ui"


def test_api_changer_une_cadence_reprogramme_le_job(client: TestClient) -> None:
    reponse = client.put("/api/v1/settings/subscriber_interval_s", json={"value": 30})

    assert reponse.status_code == 200
    jobs = client.container.scheduler.status()  # type: ignore[attr-defined]
    job = next(j for j in jobs if j["job"] == "collect_subscribers")
    assert job["interval_s"] == 30.0


def test_un_reglage_a_choix_est_declare_comme_tel() -> None:
    """LE TYPE COMMANDE LE CONTROLE DE SAISIE.

    Un reglage a choix declare en 'str' retombe sur le controle par defaut de
    l'interface -- un champ NUMERIQUE. Pour une valeur qui vaut 'edge' ou 'pop',
    le champ est inutilisable, et rien dans le code Python ne le signale.
    """
    from app.services.runtime_config import REGLAGES

    for reglage in REGLAGES:
        if reglage.choices:
            assert reglage.kind == "choix", (
                f"{reglage.name} propose des choix mais est declare '{reglage.kind}' : "
                "l'interface le rendra comme un champ numerique"
            )

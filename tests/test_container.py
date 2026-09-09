"""Assemblage des dependances et garde-fous hors-bande."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.collectors.radius import FreeradiusSqlPlanProvider, MockPlanProvider
from app.collectors.uisp import AirOsProvider, MockBackhaulProvider, UispProvider
from app.config import BackhaulConfig, RouterConfig, Settings
from app.container import build_backhaul_provider, build_plan_provider
from app.services.registry import collectors_from_settings


def test_enforcement_desactive_par_defaut_meme_conteneur_construit() -> None:
    """Le drapeau reste a false tant que l'operateur ne l'a pas leve lui-meme.

    Depuis la phase 2 il n'empeche plus le demarrage, mais il reste le dernier
    rempart avant toute ecriture : cf. tests/test_shaping_service.py.
    """
    assert Settings(_env_file=None).enforcement_enabled is False


def test_choix_du_fournisseur_de_plans(settings: Settings) -> None:
    assert isinstance(build_plan_provider(settings), MockPlanProvider)

    settings.plan_provider = "freeradius_sql"
    settings.radius_dsn = "postgresql://radius:x@10.0.0.5:5432/radius"
    assert isinstance(build_plan_provider(settings), FreeradiusSqlPlanProvider)


def test_freeradius_sans_dsn_est_refuse(settings: Settings) -> None:
    settings.plan_provider = "freeradius_sql"
    settings.radius_dsn = None
    with pytest.raises(ValueError, match="RADIUS_DSN"):
        build_plan_provider(settings)


def test_choix_du_fournisseur_de_capacite(settings: Settings) -> None:
    assert isinstance(build_backhaul_provider(settings), MockBackhaulProvider)

    settings.backhaul_provider = "uisp"
    settings.uisp_base_url = "https://uisp.test"
    settings.uisp_token = SecretStr("jeton")
    provider = build_backhaul_provider(settings)
    assert isinstance(provider, UispProvider)


def test_uisp_sans_jeton_est_refuse(settings: Settings) -> None:
    settings.backhaul_provider = "uisp"
    settings.uisp_base_url = "https://uisp.test"
    settings.uisp_token = None
    with pytest.raises(ValueError, match="UISP_TOKEN"):
        build_backhaul_provider(settings)


def test_choix_du_fournisseur_airos(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider airos : on interroge directement l'API locale des antennes."""
    monkeypatch.setenv("BH_NORD_PASS", "s3cret")
    settings.backhaul_provider = "airos"
    settings.airos_username = "qos-ro"
    settings.backhauls = [
        BackhaulConfig(
            name="bh-nord",
            pop_name="Site 1",
            uisp_device_id="bh-nord",
            api_host="10.0.0.2",
            api_password_env="BH_NORD_PASS",
        )
    ]
    provider = build_backhaul_provider(settings)
    assert isinstance(provider, AirOsProvider)


def test_airos_sans_antenne_est_refuse(settings: Settings) -> None:
    """Sans aucune api_host, le provider airos n'a rien a interroger."""
    settings.backhaul_provider = "airos"
    settings.backhauls = [BackhaulConfig(name="bh", pop_name="Site 1")]
    with pytest.raises(ValueError, match="api_host"):
        build_backhaul_provider(settings)


def test_routeur_sans_secret_est_ignore_pas_fatal(settings: Settings) -> None:
    """Un secret manquant sur un PoP ne doit pas empecher les autres de tourner."""
    settings.routers = [
        RouterConfig(name="ok", host="192.0.2.11", password="present"),
        RouterConfig(name="ko", host="192.0.2.12", password_env="VARIABLE_ABSENTE"),
    ]
    collectors = collectors_from_settings(settings)
    assert [c.name for c in collectors] == ["ok"]

"""Assemblage des dependances et garde-fous hors-bande."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.collectors.radius import FreeradiusSqlPlanProvider, MockPlanProvider
from app.collectors.uisp import MockBackhaulProvider, UispProvider
from app.config import RouterConfig, Settings
from app.container import build_backhaul_provider, build_container, build_plan_provider
from app.services.registry import collectors_from_settings


async def test_enforcement_active_bloque_le_demarrage(settings: Settings) -> None:
    """Phase 1 : aucun enforcement n'existe. Activer le drapeau doit echouer
    bruyamment plutot que laisser croire que du shaping est pousse."""
    settings.enforcement_enabled = True
    with pytest.raises(RuntimeError, match="phase 2"):
        await build_container(settings)


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


def test_routeur_sans_secret_est_ignore_pas_fatal(settings: Settings) -> None:
    """Un secret manquant sur un PoP ne doit pas empecher les autres de tourner."""
    settings.routers = [
        RouterConfig(name="ok", host="192.0.2.11", password="present"),
        RouterConfig(name="ko", host="192.0.2.12", password_env="VARIABLE_ABSENTE"),
    ]
    collectors = collectors_from_settings(settings)
    assert [c.name for c in collectors] == ["ok"]

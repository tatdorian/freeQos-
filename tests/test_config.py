"""Configuration : inventaire multi-routeurs et gestion des secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import MissingSecretError, RouterConfig, Settings


def test_routeurs_depuis_une_variable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ROUTERS",
        json.dumps(
            [
                {"name": "pop-nord", "host": "10.10.0.11", "password_env": "MT_NORD"},
                {
                    "name": "pop-sud",
                    "host": "10.10.0.12",
                    "password_env": "MT_SUD",
                    "enabled": False,
                },
            ]
        ),
    )
    settings = Settings(_env_file=None)

    assert [r.name for r in settings.routers] == ["pop-nord", "pop-sud"]
    assert [r.name for r in settings.enabled_routers] == ["pop-nord"]
    assert settings.routers[0].port == 8728  # API binaire par defaut
    assert settings.routers[0].username == "qos-ro"  # lecture seule par defaut


def test_inventaire_depuis_un_fichier_yaml(tmp_path: Path) -> None:
    inventory = tmp_path / "routers.yml"
    inventory.write_text(
        """
routers:
  - name: pop-nord
    host: 10.10.0.11
    password_env: MT_NORD
    pop_name: PoP Nord
backhauls:
  - name: bh-nord
    pop_name: PoP Nord
    uisp_device_id: dev-1
    nominal_capacity_mbps: 500
""",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, routers_file=inventory)

    assert settings.routers[0].effective_pop_name == "PoP Nord"
    assert settings.backhauls[0].uisp_device_id == "dev-1"
    assert settings.enabled_backhauls[0].nominal_capacity_mbps == 500


def test_l_environnement_surcharge_le_fichier(tmp_path: Path) -> None:
    """Pratique en lab : rediriger un PoP vers un CHR sans toucher a l'inventaire."""
    inventory = tmp_path / "routers.yml"
    inventory.write_text(
        "routers:\n  - name: pop-nord\n    host: 10.10.0.11\n    password_env: MT_NORD\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        routers_file=inventory,
        routers=[RouterConfig(name="pop-nord", host="192.168.56.10", password_env="MT_NORD")],
    )

    assert len(settings.routers) == 1
    assert settings.routers[0].host == "192.168.56.10"


def test_inventaire_absent_n_empeche_pas_le_demarrage(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, routers_file=tmp_path / "absent.yml")
    assert settings.routers == []


def test_mot_de_passe_lu_dans_l_environnement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT_NORD", "mot-de-passe-du-lab")
    router = RouterConfig(name="pop-nord", host="10.10.0.11", password_env="MT_NORD")
    assert router.resolve_password() == "mot-de-passe-du-lab"


def test_secret_manquant_signale_explicitement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MT_ABSENT", raising=False)
    router = RouterConfig(name="pop-nord", host="10.10.0.11", password_env="MT_ABSENT")
    with pytest.raises(MissingSecretError, match="MT_ABSENT"):
        router.resolve_password()


def test_aucun_secret_configure() -> None:
    router = RouterConfig(name="pop-nord", host="10.10.0.11")
    with pytest.raises(MissingSecretError):
        router.resolve_password()


def test_le_mot_de_passe_ne_fuit_pas_dans_les_representations() -> None:
    router = RouterConfig(name="pop", host="10.0.0.1", password="tres-secret")
    assert "tres-secret" not in repr(router)
    assert "tres-secret" not in str(router.model_dump())


def test_dsn_asyncpg_normalise_le_schema_sqlalchemy() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@db:5432/qos")
    assert settings.asyncpg_dsn == "postgresql://u:p@db:5432/qos"


def test_enforcement_desactive_par_defaut() -> None:
    """Phase 1 : le controleur doit rester strictement observateur."""
    assert Settings(_env_file=None).enforcement_enabled is False


# ------------------------------------- boucle fermee QoE (phase 4)
def test_reglages_par_defaut_de_la_boucle_qoe() -> None:
    """La boucle est planifiee mais prudente : elle resserre par crans de 10 %,
    ne descend jamais sous 50 % et rend un cran apres 3 cycles sains."""
    settings = Settings(_env_file=None)

    assert settings.qoe_loop_interval_s > 0
    assert settings.qoe_trim_step == 0.10
    assert settings.qoe_trim_floor == 0.50
    assert settings.qoe_recovery_cycles == 3
    # Un abonne isole n'accuse pas son secteur : il faut une correlation.
    assert settings.qoe_min_degraded_subscribers == 2


@pytest.mark.parametrize(
    ("champ", "valeur", "motif"),
    [
        ("qoe_trim_step", 0.0, "QOE_TRIM_STEP"),
        ("qoe_trim_step", 0.9, "QOE_TRIM_STEP"),
        ("qoe_trim_floor", 0.0, "QOE_TRIM_FLOOR"),
        ("qoe_trim_floor", 1.5, "QOE_TRIM_FLOOR"),
        ("qoe_recovery_cycles", 0, "QOE_RECOVERY_CYCLES"),
        ("qoe_min_degraded_subscribers", 0, "QOE_MIN_DEGRADED_SUBSCRIBERS"),
    ],
)
def test_un_reglage_absurde_est_refuse_au_chargement(champ: str, valeur: float, motif: str) -> None:
    """Mieux vaut echouer au demarrage qu'a la premiere degradation, quand plus
    personne ne regarde la configuration : un pas nul ne resserrerait jamais
    rien, un plancher a zero autoriserait a couper un secteur."""
    with pytest.raises(ValidationError, match=motif):
        Settings(_env_file=None, **{champ: valeur})

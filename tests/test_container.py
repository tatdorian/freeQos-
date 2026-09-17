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


# ---------------------------------------------------------------------------
# UN INVENTAIRE QUI CHANGE DOIT REDECOUVRIR LA TOPOLOGIE
#
# L'arbre est construit depuis l'inventaire. Quand l'inventaire bouge, l'arbre
# ment jusqu'a la decouverte suivante -- un quart d'heure par defaut. Un routeur
# ajoute n'apparaissait donc pas, un routeur supprime restait affiche.
#
# L'interface declenchait bien une decouverte apres un AJOUT, mais elle est le
# mauvais endroit pour porter cette garantie : elle ne le faisait ni sur une
# suppression, ni sur une desactivation, ni quand l'inventaire change par un
# autre chemin (appel direct a l'API, edition du YAML, autre onglet). La
# signature d'inventaire vit donc cote serveur.
# ---------------------------------------------------------------------------
def _routeur(nom: str, **kwargs) -> RouterConfig:
    base = {
        "name": nom,
        "host": "10.0.0.1",
        "username": "qos-ro",
        "password": SecretStr("p"),
        "role": "pop",
    }
    return RouterConfig(**{**base, **kwargs})


def _registre(configs: list[RouterConfig]):
    from app.services.registry import RouterRegistry
    from tests.conftest import FakeRouterOsClient

    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=configs,
        scheduler_enabled=False,
    )
    return RouterRegistry(settings, client_factory=lambda cfg: FakeRouterOsClient())


async def test_la_signature_ne_bouge_pas_sans_changement() -> None:
    """Sinon on redecouvrirait tout le reseau chaque minute."""
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    avant = registre.inventory_signature()
    await registre.reload()

    assert registre.inventory_signature() == avant


async def test_un_routeur_ajoute_change_la_signature() -> None:
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    avant = registre.inventory_signature()

    registre._settings.routers.append(_routeur("pop-2", host="10.0.0.2"))
    await registre.reload()

    assert registre.inventory_signature() != avant


async def test_un_routeur_retire_change_la_signature() -> None:
    """Le cas que l'interface ne couvrait pas : supprimer ne relancait rien,
    et le routeur restait affiche dans l'arbre."""
    registre = _registre([_routeur("pop-1"), _routeur("pop-2", host="10.0.0.2")])
    await registre.reload()
    avant = registre.inventory_signature()

    registre._settings.routers.pop()
    await registre.reload()

    assert registre.inventory_signature() != avant


async def test_un_role_modifie_change_la_signature() -> None:
    """Le role decide de la NATURE du noeud, donc de sa hauteur dans l'arbre :
    passer un PoP en coeur reorganise l'arbre autour de lui."""
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    avant = registre.inventory_signature()

    registre._settings.routers[0] = _routeur("pop-1", role="core")
    await registre.reload()

    assert registre.inventory_signature() != avant


async def test_un_pop_renomme_change_la_signature() -> None:
    """Le nom du PoP est le libelle affiche sur la case."""
    registre = _registre([_routeur("pop-1", pop_name="Site 1")])
    await registre.reload()
    avant = registre.inventory_signature()

    registre._settings.routers[0] = _routeur("pop-1", pop_name="Site Nord")
    await registre.reload()

    assert registre.inventory_signature() != avant


async def test_un_role_modifie_atteint_le_collecteur_sans_rouvrir_la_session() -> None:
    """Le collecteur portait la configuration de sa CREATION, indefiniment.

    Le role ne fait pas partie des parametres de connexion : le collecteur etait
    donc reutilise tel quel, avec son ancien role. Or le role decide de la nature
    du noeud dans l'arbre. Le corriger depuis l'interface ne changeait rien avant
    un redemarrage -- et la connexion ne doit pas etre rouverte pour autant.
    """
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    client_initial = registre.collectors[0]._client

    registre._settings.routers[0] = _routeur("pop-1", role="core")
    await registre.reload()

    assert registre.collectors[0].config.role.value == "core"
    assert registre.collectors[0]._client is client_initial


async def test_un_loopback_corrige_atteint_le_collecteur() -> None:
    """Meme mecanique : le loopback est l'IDENTITE du routeur dans l'arbre."""
    registre = _registre([_routeur("pop-1")])
    await registre.reload()

    registre._settings.routers[0] = _routeur("pop-1", loopback="10.255.0.7")
    await registre.reload()

    assert str(registre.collectors[0].config.loopback) == "10.255.0.7"


async def test_un_mot_de_passe_corrige_rouvre_la_session() -> None:
    """Celui-la DOIT reconstruire : la session porte les identifiants.

    Le mot de passe ne figurait pas dans l'empreinte de connexion. Corriger des
    identifiants depuis l'interface laissait donc le routeur injoignable jusqu'au
    redemarrage suivant, sans que rien n'explique pourquoi.
    """
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    client_initial = registre.collectors[0]._client

    registre._settings.routers[0] = _routeur("pop-1", password=SecretStr("nouveau"))
    await registre.reload()

    assert registre.collectors[0]._client is not client_initial


async def test_une_configuration_identique_ne_rouvre_rien() -> None:
    """Le contraire couterait une session API par minute et par routeur, et
    perdrait la continuite du calcul de debit a chaque fois."""
    registre = _registre([_routeur("pop-1")])
    await registre.reload()
    client_initial = registre.collectors[0]._client

    await registre.reload()

    assert registre.collectors[0]._client is client_initial

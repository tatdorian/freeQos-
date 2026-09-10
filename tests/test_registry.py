"""Inventaire vivant : fusion fichier + base, rechargement a chaud."""

from __future__ import annotations

from app.config import RouterConfig, Settings
from app.services.registry import SOURCE_DB, SOURCE_FILE, RouterRegistry
from tests.conftest import FakeRouterOsClient


class FakeRoutersRepository:
    """Double memoire du depot base."""

    def __init__(self, configs: list[RouterConfig] | None = None,
                 hidden: set[str] | None = None) -> None:
        self.configs = configs or []
        self.failures: list[tuple[int, str]] = []
        self.hidden = hidden or set()

    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        return [c for c in self.configs if c.enabled or not enabled_only]

    async def hidden_file_routers(self) -> set[str]:
        return set(self.hidden)

    async def find_id_by_name(self, name: str) -> int | None:
        for index, config in enumerate(self.configs, start=1):
            if config.name == name:
                return index
        return None

    async def record_failure(self, router_id: int, error: str) -> None:
        self.failures.append((router_id, error))


def make_registry(settings: Settings, repository=None) -> RouterRegistry:
    return RouterRegistry(
        settings, repository=repository, client_factory=lambda config: FakeRouterOsClient()
    )


async def test_fusion_fichier_et_base(settings: Settings) -> None:
    repository = FakeRoutersRepository(
        [RouterConfig(name="pop-base", host="192.0.2.50", password="x")]
    )
    registry = make_registry(settings, repository)

    collectors = await registry.reload()

    assert {c.name for c in collectors} == {"pop-test", "pop-base"}
    assert registry.source_of("pop-test") == SOURCE_FILE
    assert registry.source_of("pop-base") == SOURCE_DB


async def test_le_fichier_prime_en_cas_d_homonymie(settings: Settings) -> None:
    """Une declaration versionnee et revue doit primer sur une saisie au clavier."""
    repository = FakeRoutersRepository(
        [RouterConfig(name="pop-test", host="10.99.99.99", password="x")]
    )
    registry = make_registry(settings, repository)

    await registry.reload()

    collector = registry.collectors[0]
    assert collector.config.host == "192.0.2.11"  # celui du fichier
    assert registry.source_of("pop-test") == SOURCE_FILE


async def test_ajout_a_chaud(settings: Settings) -> None:
    repository = FakeRoutersRepository()
    registry = make_registry(settings, repository)
    await registry.reload()
    assert len(registry.collectors) == 1

    repository.configs.append(RouterConfig(name="pop-neuf", host="192.0.2.60", password="x"))
    collectors = await registry.reload()

    assert {c.name for c in collectors} == {"pop-test", "pop-neuf"}


async def test_suppression_ferme_la_connexion(settings: Settings) -> None:
    repository = FakeRoutersRepository(
        [RouterConfig(name="pop-jetable", host="192.0.2.70", password="x")]
    )
    registry = make_registry(settings, repository)
    await registry.reload()
    jetable = next(c for c in registry.collectors if c.name == "pop-jetable")

    repository.configs.clear()
    await registry.reload()

    assert {c.name for c in registry.collectors} == {"pop-test"}
    assert jetable._client.closed is True


async def test_collecteur_inchange_reutilise(settings: Settings) -> None:
    """Point critique : recreer un collecteur ferait repartir le calcul de debit
    sans point de reference, donc un trou dans la serie a chaque rechargement."""
    registry = make_registry(settings, FakeRoutersRepository())
    premier = (await registry.reload())[0]
    second = (await registry.reload())[0]
    assert premier is second


async def test_changement_d_adresse_reconstruit_le_collecteur(settings: Settings) -> None:
    repository = FakeRoutersRepository(
        [RouterConfig(name="pop-base", host="192.0.2.50", password="x")]
    )
    registry = make_registry(settings, repository)
    avant = next(c for c in (await registry.reload()) if c.name == "pop-base")

    repository.configs[0] = RouterConfig(name="pop-base", host="192.0.2.51", password="x")
    apres = next(c for c in (await registry.reload()) if c.name == "pop-base")

    assert avant is not apres
    assert apres.config.host == "192.0.2.51"
    assert avant._client.closed is True


async def test_secret_manquant_ecarte_le_routeur_sans_bloquer(settings: Settings) -> None:
    settings.routers = [
        RouterConfig(name="ok", host="192.0.2.11", password="present"),
        RouterConfig(name="ko", host="192.0.2.12", password_env="VARIABLE_ABSENTE"),
    ]
    registry = make_registry(settings)

    collectors = await registry.reload()

    assert [c.name for c in collectors] == ["ok"]
    assert len(registry.skipped) == 1
    assert registry.skipped[0]["name"] == "ko"
    assert "VARIABLE_ABSENTE" in registry.skipped[0]["reason"]


async def test_un_routeur_fichier_masque_disparait_sans_avertissement(settings: Settings) -> None:
    """Retirer un routeur fichier depuis l'interface l'ecarte de l'inventaire ET
    de la liste des ignores, sans editer le YAML."""
    settings.routers = [
        RouterConfig(name="garde", host="192.0.2.11", password="present"),
        RouterConfig(name="pop-nord", host="192.0.2.12", password_env="MT_POP_NORD_PASSWORD"),
    ]
    repository = FakeRoutersRepository(hidden={"pop-nord"})
    registry = make_registry(settings, repository)

    collectors = await registry.reload()

    assert [c.name for c in collectors] == ["garde"]
    # Masque : ni actif, ni dans les avertissements.
    assert registry.skipped == []


async def test_base_indisponible_conserve_l_inventaire_fichier(settings: Settings) -> None:
    class DepotCasse:
        async def load_configs(self, **kwargs):
            raise RuntimeError("base injoignable")

    registry = make_registry(settings, DepotCasse())
    collectors = await registry.reload()

    assert [c.name for c in collectors] == ["pop-test"]


async def test_describe_marque_ce_qui_est_modifiable(settings: Settings) -> None:
    repository = FakeRoutersRepository(
        [RouterConfig(name="pop-base", host="192.0.2.50", password="x")]
    )
    registry = make_registry(settings, repository)
    await registry.reload()

    par_nom = {entry["name"]: entry for entry in registry.describe()}
    assert par_nom["pop-test"]["editable"] is False  # vient du fichier
    assert par_nom["pop-base"]["editable"] is True  # vient de la base


async def test_close_all(settings: Settings) -> None:
    registry = make_registry(settings)
    collectors = await registry.reload()
    registry.close_all()
    assert registry.collectors == []
    assert all(c._client.closed for c in collectors)

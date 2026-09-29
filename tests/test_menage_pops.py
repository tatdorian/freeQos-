"""Menage des PoPs vides : ce qui est declare quelque part n'est jamais retire."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.config import BackhaulConfig, RouterConfig
from app.services.pop_cleanup import declared_pop_names, purge_empty_pops


class Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_public(self) -> list[dict[str, Any]]:
        return self.rows

    async def list_all(self) -> list[dict[str, Any]]:
        return self.rows


class Routeurs:
    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        assert enabled_only is False  # un routeur desactive garde son site
        return [
            RouterConfig(name="nas-off", host="192.0.2.9", pop_name="Site coupe", enabled=False)
        ]


def conteneur(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "settings": SimpleNamespace(
            routers=[RouterConfig(name="nas-1", host="192.0.2.1", pop_name="Site fichier")],
            backhauls=[BackhaulConfig(name="bh", pop_name="Site du lien")],
        ),
        "collection": SimpleNamespace(collectors=[]),
        "routers_repo": Routeurs(),
        "antennas_repo": Repo([{"pop_name": "Site antenne"}]),
        "static_clients_repo": Repo([{"pop_name": "Site statique"}]),
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_tout_site_declare_est_garde() -> None:
    noms = await declared_pop_names(conteneur())
    assert {
        "Site fichier",
        "Site coupe",
        "Site du lien",
        "Site antenne",
        "Site statique",
    } <= noms


async def test_un_menage_effectif_vide_le_cache_de_la_collecte() -> None:
    vides: list[bool] = []

    class Metriques:
        async def purge_empty_pops(self, *, keep: set[str]) -> list[str]:
            assert "Site fichier" in keep
            return ["PoP Nord", "PoP Sud"]

    c = conteneur(
        repository=Metriques(),
        directory=SimpleNamespace(clear_cache=lambda: vides.append(True)),
    )
    assert await purge_empty_pops(c) == ["PoP Nord", "PoP Sud"]
    assert vides == [True]

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
            return ["PoP Altair", "PoP Vega"]

    c = conteneur(
        repository=Metriques(),
        directory=SimpleNamespace(clear_cache=lambda: vides.append(True)),
    )
    assert await purge_empty_pops(c) == ["PoP Altair", "PoP Vega"]
    assert vides == [True]


def test_un_site_que_rien_ne_declare_est_signale_et_se_retire_d_un_geste(settings) -> None:  # type: ignore[no-untyped-def]
    """Les sites d'essai restaient affiches, avec leurs abonnes, alors qu'aucun
    equipement ne les portait plus."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_container
    from app.main import register_routes
    from tests.test_api import FakeRepository, build_container

    class Depot(FakeRepository):
        def __init__(self) -> None:
            self.sites = [
                {"id": 1, "name": "PoP Test", "subscriber_count": 1, "backhaul_count": 1},
                {"id": 2, "name": "Site d'essai", "subscriber_count": 3, "backhaul_count": 0},
            ]
            self.retires: list[int] = []

        async def list_pops(self) -> list[dict[str, Any]]:
            return [dict(s) for s in self.sites if s["id"] not in self.retires]

        async def delete_pop(self, pop_id: int) -> dict[str, int]:
            self.retires.append(pop_id)
            return {"subscribers": 0, "backhauls": 0}

    conteneur_api = build_container(settings)
    depot = Depot()
    conteneur_api.repository = depot
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: conteneur_api
    client = TestClient(app)

    sites = {s["name"]: s["declared"] for s in client.get("/api/v1/pops").json()}
    assert sites == {"PoP Test": True, "Site d'essai": False}
    assert client.post("/api/v1/pops/orphans/delete").status_code == 400  # confirmation exigee
    reponse = client.post("/api/v1/pops/orphans/delete?confirm=true")
    assert reponse.json() == {"deleted": ["Site d'essai"]}
    assert depot.retires == [2]  # le site porte par un routeur est intact

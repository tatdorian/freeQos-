"""Plan et limite forcee : une seule verite.

La limite posee dans Subscribers (Rate) primait sur le plan sans que la page
Plans le dise (100/20 « Default » pour un client bride a 300k/750k). Changer le
plan la levait pas, le reset des files la reposait. Desormais : Plans l'affiche,
un plan choisi la leve, le reset des files ramene chaque client a son plan."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.api.plans import _lever_limite_forcee, _limites_forcees
from app.api.shaping import clean_router_queues


class Topo:
    def __init__(self) -> None:
        self.lignes = [
            {"target_key": "nestle", "max_down_mbps": 0.3, "max_up_mbps": 0.75, "enabled": True},
            # Boost seul (sans plafond permanent) : ce n'est pas une limite forcee.
            {"target_key": "boost", "max_down_mbps": None, "max_up_mbps": None, "enabled": True},
        ]
        self.supprimes: list[str] = []

    async def policies(self, scope: str | None = None) -> list[dict[str, Any]]:
        return self.lignes

    async def delete_policy(self, scope: str, target_key: str) -> bool:
        avant = len(self.lignes)
        self.lignes = [x for x in self.lignes if x["target_key"] != target_key]
        self.supprimes.append(target_key)
        return len(self.lignes) < avant


async def test_la_page_plans_voit_la_limite_forcee() -> None:
    conteneur = SimpleNamespace(topology_repo=Topo())
    assert await _limites_forcees(conteneur) == {"nestle": (0.3, 0.75)}


async def test_choisir_un_plan_leve_la_limite_forcee() -> None:
    topo = Topo()
    conteneur = SimpleNamespace(topology_repo=topo)
    assert await _lever_limite_forcee(conteneur, "nestle") is True
    assert await _limites_forcees(conteneur) == {}
    # Rien a lever : pas d'erreur, juste « non ».
    assert await _lever_limite_forcee(conteneur, "inconnu") is False


async def test_le_reset_des_files_ramene_les_clients_a_leur_plan() -> None:
    topo = Topo()

    class Shaping:
        async def build_targets(self, router: str) -> tuple[list[Any], list[Any]]:
            return [], [SimpleNamespace(login="nestle"), SimpleNamespace(login="test-fp")]

        async def clean_queues(self, router: str, *, author: str) -> dict[str, Any]:
            # Les limites sont levees AVANT la reconstruction : celle-ci pose les plans.
            assert "nestle" in topo.supprimes
            return {"router": router, "removed": 2, "recreated": 2, "kept_foreign": 0, "errors": []}

    conteneur = SimpleNamespace(topology_repo=topo, shaping=Shaping(), restrictions=None)
    rapport = await clean_router_queues("NAS-FRANCOPHONIE", conteneur, reset_limits=True)
    assert rapport["limits_reset"] == ["nestle"]

    topo2 = Topo()
    conteneur2 = SimpleNamespace(topology_repo=topo2, shaping=Shaping(), restrictions=None)
    # Sans reset des limites, rien n'est leve (l'assert de clean_queues tomberait).
    conteneur2.shaping.clean_queues = (  # type: ignore[method-assign]
        lambda router, author: _rapport_vide(router)
    )
    rapport2 = await clean_router_queues("NAS-FRANCOPHONIE", conteneur2, reset_limits=False)
    assert rapport2["limits_reset"] == [] and topo2.supprimes == []


async def _rapport_vide(router: str) -> dict[str, Any]:
    return {"router": router, "removed": 0, "recreated": 0, "kept_foreign": 0, "errors": []}

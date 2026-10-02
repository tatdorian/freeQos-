"""Restreindre au niveau d'un SITE : un PoP de routeur, ou un site de VLAN.

DEMANDE EXPLICITE : "je peux faire la restriction au niveau d'un PoP ou au
niveau d'un abonne". Un site de routeur vise tout ce qui passe par lui ; un
site de VLAN ne vise que le reseau de sa VLAN -- les autres clients du meme
routeur ne sont pas touches.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from app.collectors import mikrotik
from app.services.restrictions import InvalidRuleError, RestrictionService, validate


class Shaping:
    async def _pop_sites(self) -> list[dict[str, Any]]:
        return [
            {"name": "NAS-BASSORA", "kind": "router", "router_name": "nas-b"},
            {
                "name": "VLAN 2060",
                "kind": "vlan",
                "router_name": "nas-f",
                "vlan_interface": "vlan2060-nestle-siege",
                "vlan_id": 2060,
            },
        ]

    async def _routers_for_site(self, nom: str) -> list[str]:
        return {"NAS-BASSORA": ["nas-b"]}.get(nom, [])


def _service() -> RestrictionService:
    service = RestrictionService.__new__(RestrictionService)
    service.shaping = Shaping()  # type: ignore[assignment]
    return service


async def test_un_pop_de_routeur_vise_tout_son_trafic() -> None:
    assert await _service().site_targets({"pops": ["NAS-BASSORA"]}) == {"nas-b": ()}


async def test_un_site_de_vlan_ne_vise_que_sa_vlan() -> None:
    mikrotik._RESEAUX_CONNECTES.clear()
    mikrotik._RESEAUX_CONNECTES["nas-f"] = [
        (ipaddress.ip_network("100.100.105.240/30"), "vlan2060-nestle-siege"),
        (ipaddress.ip_network("10.9.0.0/24"), "vlan2070"),
    ]
    cibles = await _service().site_targets({"pops": ["VLAN 2060"]})
    assert cibles == {"nas-f": ("100.100.105.240/30",)}
    mikrotik._RESEAUX_CONNECTES.clear()


def test_un_site_vide_est_refuse() -> None:
    with pytest.raises(InvalidRuleError):
        validate({"prefixes": ["1.2.3.4"], "scope": "pops", "pops": []})
    validate({"prefixes": ["1.2.3.4"], "scope": "pops", "pops": ["NAS-BASSORA"]})

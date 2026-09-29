"""Ou va chaque port : le voisin se lit dans la route par defaut et les adresses.

Constate sur le banc de bout en bout : les ports que la decouverte n'avait pas
relies a un lien s'affichaient "-" (vue des ports) et "- now / 0.0 Mbps"
(risques de saturation) alors qu'ils portaient du trafic.
"""

from __future__ import annotations

from typing import Any

from app.api.metrics import name_ports
from app.collectors import mikrotik
from app.services.capacity import hotspot_rows


def setup_function() -> None:
    mikrotik._AMONTS.clear()
    mikrotik._PROPRIETAIRES.clear()


def _reseau() -> None:
    mikrotik.remember_addresses(
        "gw",
        [
            {"address": "203.0.113.2/30", "interface": "ether1"},
            {"address": "10.0.0.1/30", "interface": "ether2"},
        ],
    )
    mikrotik.remember_addresses("core", [{"address": "10.0.0.2/30", "interface": "ether1"}])
    mikrotik.remember_upstream("gw", "203.0.113.1", "ether1")
    mikrotik.remember_upstream("core", "10.0.0.1", "ether1")


def test_chaque_port_nomme_son_voisin() -> None:
    _reseau()
    ports: list[dict[str, Any]] = [
        {"router_name": "gw", "interface": "ether1"},
        {"router_name": "gw", "interface": "ether2"},
        {"router_name": "core", "interface": "ether1"},
        {"router_name": "core", "interface": "ether9", "link_name": "Sector 9"},
    ]
    amonts = name_ports(ports, ["gw", "core"])
    noms = {(p["router_name"], p["interface"]): p["link_name"] for p in ports}
    assert amonts == {"gw": "ether1", "core": "ether1"}
    # Passerelle hors de nos routeurs : la sortie de l'operateur.
    assert noms[("gw", "ether1")] == "Internet (203.0.113.1)"
    # Le port qui porte la passerelle du coeur mene au coeur.
    assert noms[("gw", "ether2")] == "core"
    # Le port amont du coeur mene au routeur qui porte sa passerelle.
    assert noms[("core", "ether1")] == "gw"
    # Un nom deja donne par la decouverte n'est jamais remplace.
    assert noms[("core", "ether9")] == "Sector 9"


def test_un_routeur_rediscover_oublie_ses_anciennes_adresses() -> None:
    mikrotik.remember_addresses("r1", [{"address": "10.9.9.1/24", "interface": "ether1"}])
    mikrotik.remember_addresses("r1", [{"address": "10.9.8.1/24", "interface": "ether1"}])
    assert mikrotik.router_owning("10.9.9.1") is None
    assert mikrotik.router_owning("10.9.8.1") == "r1"


def test_sans_lien_le_debit_actuel_vient_du_port() -> None:
    occupation = [
        {
            "router_name": "core",
            "interface": "ether1",
            "capacity_mbps": 1000.0,
            "peak_rx_bps": 200e6,
            "peak_tx_bps": 30e6,
        }
    ]
    live = {("core", "ether1"): {"rx_bps": 150e6, "tx_bps": 20e6, "link_name": "gw"}}
    [ligne] = hotspot_rows(
        occupation,
        [],
        upstream={"core": ("10.0.0.1", "ether1")},
        roles={"core": "core"},
        live=live,
    )
    assert ligne["name"] == "gw"
    # Port amont : ce qu'il recoit est le descendant des abonnes.
    assert ligne["now_down_mbps"] == 150.0
    assert ligne["now_up_mbps"] == 20.0

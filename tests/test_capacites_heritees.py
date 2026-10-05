"""Une VLAN ou un bridge n'a pas de debit negocie : il herite du port qui le
porte. Sans cela, 52 liens tombaient dans « capacite inconnue » sur un PoP
ordinaire, et leur saturation n'etait jamais suivie."""

from __future__ import annotations

from app.collectors.mikrotik import inherited_capacities, is_physical_interface
from app.services.capacity import hotspot_rows


def test_une_vlan_herite_de_son_port() -> None:
    capacites = {"ether6": 1000.0, "ether2": 100.0}
    vlans = [{"name": "vlan300-cust-inet", "interface": "ether2"}]
    assert inherited_capacities(capacites, vlans, []) == {"vlan300-cust-inet": 100.0}


def test_un_bridge_vaut_son_port_le_plus_rapide_et_ses_vlan_aussi() -> None:
    capacites = {"ether6": 1000.0, "ether7": 100.0}
    ports = [
        {"bridge": "lan-bridge", "interface": "ether6"},
        {"bridge": "lan-bridge", "interface": "ether7"},
    ]
    vlans = [{"name": "vlan2060-nestle-siege", "interface": "lan-bridge"}]
    herite = inherited_capacities(capacites, vlans, ports)
    assert herite == {"lan-bridge": 1000.0, "vlan2060-nestle-siege": 1000.0}


def test_rien_d_invente_sans_port_connu() -> None:
    vlans = [{"name": "vlan9", "interface": "sfp1"}]
    assert inherited_capacities({}, vlans, []) == {}


def test_la_boucle_locale_n_est_pas_un_port() -> None:
    assert not is_physical_interface({"name": "lo", "type": "loopback"})
    assert is_physical_interface({"name": "ether1", "type": "ether"})
    assert is_physical_interface({"name": "vlan2060", "type": "vlan"})


def test_les_anciennes_mesures_de_lo_sont_ignorees() -> None:
    occupation = [
        {"router_name": "r", "interface": "lo", "peak_rx_bps": 1e6, "peak_tx_bps": 1e6},
        {
            "router_name": "r",
            "interface": "ether1",
            "peak_rx_bps": 1e6,
            "peak_tx_bps": 1e6,
            "capacity_mbps": 100.0,
        },
    ]
    lignes = hotspot_rows(occupation, [], upstream={}, roles={})
    assert [ligne["interface"] for ligne in lignes] == ["ether1"]

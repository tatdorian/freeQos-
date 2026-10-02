"""Upload affiche au DOUBLE : le meme flux exporte depuis deux interfaces.

Une VLAN posee sur un bridge fait exporter chaque paquet montant a l'entree du
bridge ET a celle de la VLAN. Le descendant entre par le WAN, une seule fois.
"""

from __future__ import annotations

from app.collectors.netflow import Flow
from app.services.netflow_service import NetflowService


def _flux(entree: int | None, octets: int = 1000) -> Flow:
    return Flow(
        src="172.16.35.253",
        dst="1.1.1.1",
        src_port=50000,
        dst_port=443,
        protocol=6,
        octets=octets,
        packets=10,
        input_snmp=entree,
    )


def test_le_meme_flux_vu_sur_une_seconde_interface_est_ecarte() -> None:
    service = NetflowService()
    assert not service._doublon("10.0.0.1", _flux(5))  # bridge
    assert service._doublon("10.0.0.1", _flux(7))  # la VLAN : meme trafic
    # Les enregistrements suivants sur l'interface retenue comptent toujours.
    assert not service._doublon("10.0.0.1", _flux(5, octets=2000))


def test_deux_exporteurs_ne_sont_pas_des_doublons() -> None:
    """Deux routeurs differents : c'est la deduplication par point de vue qui
    s'en charge, pas celle-ci."""
    service = NetflowService()
    assert not service._doublon("10.0.0.1", _flux(5))
    assert not service._doublon("10.0.0.2", _flux(7))


def test_sans_interface_annoncee_rien_n_est_ecarte() -> None:
    service = NetflowService()
    assert not service._doublon("10.0.0.1", _flux(None))
    assert not service._doublon("10.0.0.1", _flux(None))

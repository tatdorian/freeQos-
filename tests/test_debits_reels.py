"""Les VRAIS debits d'une conversation -- constate lors d'un test de bande passante.

Un /tool bandwidth-test de 1m52 a ~0,9 Mbit/s par sens (vers le loopback du NAS)
s'affichait : Down 2,7 GiB, Up 2,7 GiB, 12,8 Mbps de moyenne, 5,5 Mbps en direct.
Trois erreurs :
  1. les volumes etaient le CUMUL depuis la premiere vue du couple (des jours) ;
  2. la moyenne divisait (down + up) par la periode entiere ;
  3. le "direct" divisait un enregistrement portant jusqu'a 60 s de trafic par
     l'age de la fenetre (parfois 20 s), les deux sens additionnes.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest

from app.collectors.netflow import Flow, NetflowDecoder
from app.services.flows import RESEAUX_CLIENTS_PAR_DEFAUT, FlowAggregator, PrefixIndex


class Horloge:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _agregateur(horloge: Horloge) -> FlowAggregator:
    return FlowAggregator(
        index=PrefixIndex.build([("100.64.1.11/32", 7)]),
        customer_networks=FlowAggregator.parse_networks(list(RESEAUX_CLIENTS_PAR_DEFAUT)),
        clock=horloge,
    )


def _enregistrement(src: str, dst: str, sport: int, octets: int, duree_ms: int) -> Flow:
    return Flow(
        src=src,
        dst=dst,
        src_port=sport,
        dst_port=2000,
        protocol=6,
        octets=octets,
        packets=octets // 1400,
        duration_ms=duree_ms,
    )


def test_le_debit_en_direct_est_celui_du_test_sens_par_sens() -> None:
    """20 connexions, 0,9 Mbit/s par sens : chaque enregistrement couvre 60 s."""
    horloge = Horloge()
    a = _agregateur(horloge)
    par_connexion = int(0.9e6 / 8 * 60 / 20)  # octets en 60 s, par connexion
    for i in range(20):
        a.add(
            _enregistrement("100.64.1.11", "8.8.4.4", 40000 + i, par_connexion, 60_000),
            vantage="pop",
        )
        a.add(
            _enregistrement("8.8.4.4", "100.64.1.11", 40000 + i, par_connexion, 60_000),
            vantage="pop",
        )
    horloge.t += 20  # la fenetre n'a que 20 s : ca ne doit rien changer
    bas, haut = a.live_rates()[("100.64.1.11", "8.8.4.4")]
    assert bas == pytest.approx(0.9e6, rel=0.01)
    assert haut == pytest.approx(0.9e6, rel=0.01)


def test_une_conversation_terminee_ne_compte_plus_en_direct() -> None:
    horloge = Horloge()
    a = _agregateur(horloge)
    a.add(_enregistrement("100.64.1.11", "8.8.4.4", 40000, 1_000_000, 10_000), vantage="pop")
    horloge.t += 120
    assert a.live_rates() == {}


def test_le_meme_paquet_vu_au_pop_et_a_la_sortie_ne_compte_qu_une_fois() -> None:
    horloge = Horloge()
    a = _agregateur(horloge)
    flux = _enregistrement("100.64.1.11", "8.8.4.4", 40000, 1_000, 1_000)
    a.add(flux, vantage="pop")
    a.add(flux, vantage="internet")
    lot = a.flush(datetime.now(tz=UTC))
    (conversation,) = lot.destinations
    assert conversation.up_bytes == 1_000
    assert conversation.active_s == pytest.approx(1.0)


def test_les_compteurs_cumules_ne_sont_pas_additionnes_a_chaque_export() -> None:
    """IPFIX : octetTotalCount (85) est un cumul ; octetDeltaCount (1) fait foi."""
    decodeur = NetflowDecoder()
    modele = struct.pack("!HHHH", 2, 4 + 4 + 4 * 4, 256, 4) + struct.pack(
        "!HHHHHHHH", 8, 4, 12, 4, 1, 8, 85, 8
    )
    donnees = (
        struct.pack("!HH", 256, 4 + 24)
        + bytes([100, 64, 1, 11, 8, 8, 4, 4])
        + struct.pack("!QQ", 1_000, 50_000_000)
    )
    corps = modele + donnees
    entete = struct.pack("!HHIII", 10, 16 + len(corps), 0, 1, 0)
    (flux,) = decodeur.decode(entete + corps, "10.0.0.1").flows
    assert flux.octets == 1_000


def test_la_duree_des_enregistrements_v9_est_lue() -> None:
    decodeur = NetflowDecoder()
    modele = struct.pack("!HHHH", 0, 4 + 4 + 5 * 4, 256, 5) + struct.pack(
        "!HHHHHHHHHH", 8, 4, 12, 4, 1, 4, 22, 4, 21, 4
    )
    donnees = (
        struct.pack("!HH", 256, 4 + 20)
        + bytes([100, 64, 1, 11, 8, 8, 4, 4])
        + struct.pack("!III", 5_000, 10_000, 70_000)
    )
    entete = struct.pack("!HHIIII", 9, 2, 80_000, 1_700_000_000, 1, 0)
    (flux,) = decodeur.decode(entete + modele + donnees, "10.0.0.1").flows
    assert flux.duration_ms == 60_000


def test_hors_horodatage_l_enregistrement_est_suppose_plein() -> None:
    a = _agregateur(Horloge())
    a.add(
        Flow(
            src="100.64.1.11", dst="8.8.4.4", src_port=1, dst_port=2, protocol=6, octets=7_500_000
        ),
        vantage="pop",
    )
    _bas, haut = a.live_rates()[("100.64.1.11", "8.8.4.4")]
    assert haut == pytest.approx(1e6)  # 7,5 Mo sur 60 s = 1 Mbit/s

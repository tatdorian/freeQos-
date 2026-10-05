"""Un client est compte a UN seul endroit.

Vu par trois NAS, son trafic etait additionne trois fois cote PoP (130 MiB
d'upload pour 43 reellement passes a la sortie) ; ses applications cumulaient
bordure ET PoP. Par point de vue, seul l'exporteur qui le voit le mieux compte,
et ses applications viennent du point de vue qui compte."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collectors.netflow import Flow
from app.services.flows import FlowAggregator, PrefixIndex


def _agregat() -> FlowAggregator:
    return FlowAggregator(index=PrefixIndex.build([("10.0.0.5/32", 7)]))


def _flux(octets: int) -> Flow:
    return Flow(
        src="10.0.0.5",
        dst="8.8.8.8",
        src_port=5000,
        dst_port=443,
        protocol=6,
        octets=octets,
        packets=1,
    )


def test_trois_nas_qui_voient_le_meme_client_ne_le_comptent_qu_une_fois() -> None:
    agregat = _agregat()
    for nas in ("11.11.11.75", "11.11.11.81", "11.11.11.82"):
        agregat.add(_flux(1000), vantage="pop", exporter=nas)
    lot = agregat.flush(datetime.now(tz=UTC), vantage="pop")

    [compte] = [c for c in lot.subscribers if c.vantage == "pop"]
    assert compte.up_bytes == 1000  # et non 3000


def test_le_meilleur_point_de_vue_l_emporte() -> None:
    agregat = _agregat()
    agregat.add(_flux(400), vantage="pop", exporter="nas-a")
    agregat.add(_flux(1000), vantage="pop", exporter="nas-b")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="pop")
    assert [c.up_bytes for c in lot.subscribers] == [1000]


def test_les_applications_viennent_du_point_de_vue_qui_compte() -> None:
    agregat = _agregat()
    agregat.add(_flux(1000), vantage="edge", exporter="ds-ccr")
    agregat.add(_flux(1000), vantage="pop", exporter="nas-a")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")

    assert sum(a.up_bytes for a in lot.apps) == 1000  # et non 2000
    # Les deux points de vue restent mesures, chacun une fois.
    assert sorted(c.vantage for c in lot.subscribers) == ["edge", "pop"]


def _conversation(octets: int, duree_ms: int | None, *, port: int = 5000) -> Flow:
    # Le client 10.0.0.5 televerse vers une adresse publique.
    return Flow(
        src="10.0.0.5",
        dst="1.1.1.1",
        src_port=port,
        dst_port=443,
        protocol=6,
        octets=octets,
        packets=1,
        duration_ms=duree_ms,
    )


def _agregat_destinations() -> FlowAggregator:
    return FlowAggregator(
        index=PrefixIndex.build([("10.0.0.5/32", 7)]),
        customer_networks=FlowAggregator.parse_networks(["10.0.0.0/8"]),
        track_destinations=True,
    )


def test_une_conversation_vue_par_deux_exporteurs_n_est_comptee_qu_une_fois() -> None:
    """Constate : 282 MiB affiches pour ~126 reellement passes en une heure."""
    agregat = _agregat_destinations()
    for nas in ("11.11.11.75", "11.11.11.76"):
        agregat.add(_conversation(1_000_000, 60_000), vantage="pop", exporter=nas)
    [conversation] = agregat.live_destinations()
    assert conversation.up_bytes == 1_000_000


def test_un_enregistrement_de_30_minutes_donne_le_vrai_debit() -> None:
    """Un routeur laisse a son defaut exporte un flux long toutes les 30 min :
    diviser ce volume par une minute affichait 4,2 Mbps pour 270 kbps reels."""
    agregat = _agregat_destinations()
    octets = 270_000 // 8 * 1800  # 270 kbps pendant 30 minutes
    agregat.add(_conversation(octets, 1_800_000), vantage="pop", exporter="nas")
    [(bas, haut)] = agregat.live_rates().values()
    assert haut == pytest.approx(270_000, rel=0.01)
    [conversation] = agregat.live_destinations()
    assert conversation.up_bytes * 8 / conversation.active_s == pytest.approx(270_000, rel=0.01)

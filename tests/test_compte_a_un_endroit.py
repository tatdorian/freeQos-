"""Un client est compte a UN seul endroit.

Vu par trois NAS, son trafic etait additionne trois fois cote PoP (130 MiB
d'upload pour 43 reellement passes a la sortie) ; ses applications cumulaient
bordure ET PoP. Par point de vue, seul l'exporteur qui le voit le mieux compte,
et ses applications viennent du point de vue qui compte."""

from __future__ import annotations

from datetime import UTC, datetime

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

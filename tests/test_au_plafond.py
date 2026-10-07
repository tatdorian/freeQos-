"""Utiliser tout son forfait n'est pas un probleme de reseau.

Constate : un client en bandwidth-test a 100 % de sa limite etait note
« poor », latence rouge, pings perdus -- alors que c'etait sa propre file qui
le retardait, pas le reseau."""

from __future__ import annotations

from app.services.latency_clients import AU_PLAFOND, MAUVAIS, verdict


def test_au_plafond_toute_la_periode_il_est_classe_a_part() -> None:
    etat, motifs = verdict(
        median_ms=None,
        p95_ms=None,
        loss_pct=None,
        bloat_ms=None,
        qoe_score=None,
        capped_samples=120,
        capped_median_ms=139.0,
    )
    assert etat == AU_PLAFOND
    assert "not a network issue" in motifs[0] and "139 ms" in motifs[0]


def test_des_pings_perdus_au_plafond_ne_comptent_pas() -> None:
    etat, _ = verdict(
        median_ms=10.0,
        p95_ms=12.0,
        loss_pct=100.0,
        bloat_ms=None,
        qoe_score=95.0,
        at_cap_now=True,
    )
    assert etat != MAUVAIS


def test_des_pings_perdus_sans_plafond_sont_un_vrai_probleme() -> None:
    etat, motifs = verdict(
        median_ms=10.0,
        p95_ms=12.0,
        loss_pct=100.0,
        bloat_ms=None,
        qoe_score=95.0,
        at_cap_now=False,
    )
    assert etat == MAUVAIS
    assert any("loss" in m for m in motifs)


def test_tous_les_clients_muets_c_est_la_sonde() -> None:
    """Constate : 100 % de perte chez les quatre clients de trois routeurs,
    latence habituelle 7 a 11 ms. Ce n'est pas eux, c'est la sonde."""
    from app.services.latency_clients import build_rows, sondes_muettes

    series = {
        1: {"router": "NAS-FRANCOPHONIE", "sent": 5, "received": 0, "loss_pct": 100.0},
        2: {"router": "NAS-FRANCOPHONIE", "sent": 5, "received": 0, "loss_pct": 100.0},
        3: {"router": "NAS-BASSORA", "sent": 5, "received": 0, "loss_pct": 100.0},
    }
    assert sondes_muettes(series) == {"NAS-FRANCOPHONIE", "NAS-BASSORA"}
    latences = [
        {"subscriber_id": i, "login": f"c{i}", "median_ms": 10.0, "p95_ms": 13.0} for i in (1, 2, 3)
    ]
    lignes = build_rows(latences, {}, series)
    assert all(ligne["experience"] == "good" for ligne in lignes)
    assert all(ligne["probe_silent"] for ligne in lignes)
    assert "check the probe" in lignes[0]["reasons"][-1]


def test_un_client_seul_muet_reste_un_vrai_probleme() -> None:
    from app.services.latency_clients import build_rows

    series = {
        1: {"router": "R", "sent": 5, "received": 0, "loss_pct": 100.0},
        2: {"router": "R", "sent": 5, "received": 5, "loss_pct": 0.0},
    }
    latences = [
        {"subscriber_id": i, "login": f"c{i}", "median_ms": 10.0, "p95_ms": 13.0} for i in (1, 2)
    ]
    par_id = {ligne["subscriber_id"]: ligne for ligne in build_rows(latences, {}, series)}
    assert par_id[1]["experience"] == "poor"


async def test_sans_reponse_depuis_le_loopback_la_sonde_part_sans_source() -> None:
    """Si la reponse du client ne sait pas revenir vers le loopback, toute la
    serie est perdue. On retente sans source ; si ca repond, le routeur sonde
    desormais sans loopback."""
    from typing import Any

    from app.collectors.mikrotik import MikrotikCollector
    from app.config import RouterConfig
    from tests.conftest import FakeRouterOsClient

    class Routeur(FakeRouterOsClient):
        def ping(
            self,
            address: str,
            count: int = 1,
            src_address: str | None = None,
            interval: str | None = None,
        ) -> list[dict[str, Any]]:
            self.ping_reply = None if src_address else "8ms"
            return super().ping(address, count, src_address, interval)

    client = Routeur()
    collecteur = MikrotikCollector(
        RouterConfig(name="r", host="192.0.2.1", password="x"), client=client
    )

    async def loopback() -> str:
        return "11.11.11.75"

    collecteur.ensure_loopback = loopback  # type: ignore[method-assign]

    stats = await collecteur.ping_stats("10.0.0.5", 5)
    assert stats.received == 5
    assert client.ping_sources == ["11.11.11.75", None]
    # La fois suivante, directement sans source.
    await collecteur.ping_stats("10.0.0.5", 5)
    assert client.ping_sources[-1] is None and len(client.ping_sources) == 3


def test_le_diagnostic_de_la_sonde_rend_la_reponse_brute(settings) -> None:  # type: ignore[no-untyped-def]
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_container
    from app.main import register_routes
    from tests.conftest import FakeRouterOsClient
    from tests.test_api import build_container

    routeur = FakeRouterOsClient()
    routeur.ping_reply = "7ms"
    conteneur = build_container(settings, client=routeur)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: conteneur
    corps = (
        TestClient(app)
        .get("/api/v1/rtt/diagnose", params={"router": "pop-test", "address": "10.0.0.5"})
        .json()
    )
    dernier = next(a for a in corps["attempts"] if a["source"] is None and a["interval"] == "200ms")
    assert dernier["stats"]["received"] == 5 and dernier["raw"]
    # Et le meme ping que dans le terminal du routeur, pour comparer.
    assert corps["attempts"][-1]["interval"] == "1s (terminal)"

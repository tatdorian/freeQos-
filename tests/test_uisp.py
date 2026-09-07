"""Fournisseur de capacite backhaul : simulateur et client UISP."""

from __future__ import annotations

import httpx
import pytest

from app.collectors.uisp import (
    MockBackhaulProvider,
    UispProvider,
    normalize_capacity_to_mbps,
    parse_uisp_device,
)


# --------------------------------------------------------------- simulateur
async def test_mock_est_deterministe() -> None:
    """Meme seed, meme instant, meme resultat : les tests restent reproductibles."""
    a = MockBackhaulProvider(seed=42, clock=lambda: 1_000.0)
    b = MockBackhaulProvider(seed=42, clock=lambda: 1_000.0)
    assert (await a.get_capacities(["dev-1"]))["dev-1"].capacity_mbps == (
        await b.get_capacities(["dev-1"])
    )["dev-1"].capacity_mbps


async def test_mock_varie_dans_le_temps() -> None:
    """C'est le point de la simulation : la capacite radio n'est pas constante."""
    now = 0.0
    provider = MockBackhaulProvider(
        base_capacity_mbps=400, variation_pct=40, period_s=600, clock=lambda: now
    )
    valeurs = []
    for instant in range(0, 600, 50):
        now = float(instant)
        valeurs.append((await provider.get_capacities(["dev-1"]))["dev-1"].capacity_mbps)

    assert len(set(valeurs)) > 1
    assert max(valeurs) - min(valeurs) > 50  # amplitude reellement exploitable


async def test_mock_devices_decorreles() -> None:
    """Deux paraboles ne subissent pas le meme fade au meme instant."""
    provider = MockBackhaulProvider(seed=7, clock=lambda: 1234.0)
    samples = await provider.get_capacities(["dev-1", "dev-2"])
    assert samples["dev-1"].capacity_mbps != samples["dev-2"].capacity_mbps


async def test_mock_signal_degrade_avec_la_capacite() -> None:
    provider = MockBackhaulProvider(base_capacity_mbps=400)
    provider.set_capacity("dev-1", 400)
    nominal = provider.sample_for("dev-1")
    provider.set_capacity("dev-1", 40)
    fade = provider.sample_for("dev-1")

    assert fade.signal_dbm < nominal.signal_dbm
    assert fade.airtime_pct > nominal.airtime_pct
    assert fade.capacity_mbps == 40


async def test_mock_permet_de_rejouer_un_fade() -> None:
    provider = MockBackhaulProvider()
    provider.set_capacity("dev-1", 12.5)
    samples = await provider.get_capacities(["dev-1"])
    assert samples["dev-1"].capacity_mbps == 12.5
    provider.clear_overrides()
    assert provider.sample_for("dev-1").capacity_mbps != 12.5


# ------------------------------------------------------------------- unites
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (500_000_000, 500.0),  # bps
        (500_000, 500.0),  # kbps
        (500, 500.0),  # deja en Mbps
        (0, 0.0),
        (None, None),
        ("bruit", None),
    ],
)
def test_normalisation_des_unites(value: object, expected: float | None) -> None:
    assert normalize_capacity_to_mbps(value) == expected


# --------------------------------------------------------------- parsing v2.1
def test_parse_device_uisp() -> None:
    device = {
        "identification": {"id": "abc-123", "name": "BH Nord", "status": "active"},
        "overview": {
            "status": "active",
            "downlinkCapacity": 450_000_000,
            "uplinkCapacity": 150_000_000,
            "signal": -52,
            "airTime": 34.5,
            "downlinkMcs": "MCS9",
        },
    }
    sample = parse_uisp_device(device)

    assert sample.device_id == "abc-123"
    assert sample.capacity_down_mbps == 450.0
    assert sample.capacity_up_mbps == 150.0
    # Capacite utile d'un PtP = le sens le plus faible.
    assert sample.capacity_mbps == 150.0
    assert sample.signal_dbm == -52.0
    assert sample.airtime_pct == 34.5
    assert sample.online is True


def test_parse_device_schema_partiel() -> None:
    """Le schema UISP varie : un champ absent ne doit pas casser la collecte."""
    sample = parse_uisp_device({"id": "xyz", "downlinkCapacity": 100_000_000})
    assert sample.device_id == "xyz"
    assert sample.capacity_mbps == 100.0
    assert sample.signal_dbm is None


def test_parse_device_hors_ligne() -> None:
    sample = parse_uisp_device(
        {"identification": {"id": "d1"}, "overview": {"status": "disconnected"}}
    )
    assert sample.online is False


# ----------------------------------------------------------------- client API
async def test_uisp_provider_envoie_le_token_et_filtre() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["token"] = request.headers.get("X-Auth-Token")
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "identification": {"id": "dev-1"},
                    "overview": {
                        "status": "active",
                        "downlinkCapacity": 300_000_000,
                        "uplinkCapacity": 300_000_000,
                    },
                },
                {
                    "identification": {"id": "dev-inconnu"},
                    "overview": {"downlinkCapacity": 10_000_000},
                },
            ],
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://uisp.test/nms/api/v2.1",
        headers={"X-Auth-Token": "jeton-secret"},
    )
    provider = UispProvider("https://uisp.test", "jeton-secret", client=client)

    samples = await provider.get_capacities(["dev-1"])

    assert captured["token"] == "jeton-secret"
    assert captured["url"] == "https://uisp.test/nms/api/v2.1/devices"
    assert set(samples) == {"dev-1"}
    assert samples["dev-1"].capacity_mbps == 300.0
    await provider.aclose()


async def test_uisp_provider_accepte_une_reponse_enveloppee() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "dev-1", "downlinkCapacity": 200}]})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://uisp.test/nms/api/v2.1"
    )
    provider = UispProvider("https://uisp.test", "t", client=client)
    samples = await provider.get_capacities([])
    assert samples["dev-1"].capacity_mbps == 200.0
    await provider.aclose()


async def test_uisp_provider_propage_les_erreurs_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://uisp.test/nms/api/v2.1"
    )
    provider = UispProvider("https://uisp.test", "mauvais-jeton", client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_capacities(["dev-1"])
    await provider.aclose()

"""Fournisseur de capacite backhaul : simulateur et client UISP."""

from __future__ import annotations

import httpx
import pytest

from app.collectors.uisp import (
    AirOsClient,
    AirOsProvider,
    AirOsTarget,
    MockBackhaulProvider,
    UispProvider,
    normalize_capacity_to_mbps,
    parse_airos_status,
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


async def test_mock_respecte_la_capacite_nominale_de_chaque_lien() -> None:
    """Un lien declare a 300 Mbps ne doit pas en afficher 580 en lab : sinon le
    rapport 'capacite mesuree / nominal' de l'interface n'a aucun sens."""
    provider = MockBackhaulProvider(
        base_capacity_mbps=450,
        variation_pct=30,
        nominal_by_device={"petit": 100.0, "gros": 1000.0},
        clock=lambda: 4242.0,
    )
    samples = await provider.get_capacities(["petit", "gros", "inconnu"])

    # Chaque lien reste dans +/- 30 % de SA capacite nominale.
    assert 70 <= samples["petit"].capacity_mbps <= 130
    assert 700 <= samples["gros"].capacity_mbps <= 1300
    # Un device non declare retombe sur la valeur par defaut.
    assert 315 <= samples["inconnu"].capacity_mbps <= 585


# ------------------------------------------------------------------- airOS
def test_parse_airos_utilise_la_capacite_du_lien() -> None:
    """airMAX expose txcapacity/rxcapacity en kbps : c'est la capacite estimee,
    pas le debit instantane."""
    sample = parse_airos_status(
        {
            "host": {"hostname": "BH-Nord", "hwaddr": "DC:9F:DB:11:22:33"},
            "wireless": {
                "mode": "sta",
                "apmac": "AA:BB:CC:DD:EE:FF",
                "signal": -58,
                "txcapacity": 150000,  # kbps -> 150 Mbps
                "rxcapacity": 130000,  # kbps -> 130 Mbps
            },
        },
        key="bh-nord",
    )

    assert sample.device_id == "bh-nord"
    assert sample.capacity_down_mbps == 150.0
    assert sample.capacity_up_mbps == 130.0
    # La capacite utile d'un PtP est bornee par son sens le plus faible.
    assert sample.capacity_mbps == 130.0
    assert sample.signal_dbm == -58.0
    # La MAC de l'AP sert a rattacher la radio aux voisins MikroTik.
    assert sample.raw["mac"] == "AA:BB:CC:DD:EE:FF"
    assert sample.online is True


def test_parse_airos_retombe_sur_les_debits_phy() -> None:
    """Vieux firmware sans txcapacity : on prend txrate/rxrate (Mbps)."""
    sample = parse_airos_status({"wireless": {"txrate": 300, "rxrate": 300}}, key="vieux")
    assert sample.capacity_mbps == 300.0


def test_parse_airos_sans_wireless_est_hors_ligne() -> None:
    """Une antenne qui ne renvoie pas de bloc wireless est consideree muette."""
    sample = parse_airos_status({"host": {"hostname": "x"}}, key="muet")
    assert sample.online is False
    assert sample.capacity_mbps is None


async def test_airos_client_lit_le_status_sans_login_si_possible() -> None:
    """Beaucoup de parcs laissent /status.cgi accessible : inutile de s'authentifier."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.url.path)
        return httpx.Response(200, json={"wireless": {"txcapacity": 100000}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://10.0.0.2")
    airos = AirOsClient(AirOsTarget(key="bh", host="10.0.0.2"), client=client)
    status = await airos.fetch_status()

    assert status["wireless"]["txcapacity"] == 100000
    assert appels == ["/status.cgi"]  # aucun passage par /login.cgi
    await airos.aclose()


async def test_airos_client_s_authentifie_si_refuse() -> None:
    """Si l'antenne refuse, on passe par /login.cgi puis on relit le status."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.url.path)
        if request.url.path == "/status.cgi" and "/login.cgi" not in appels:
            return httpx.Response(403, text="denied")
        if request.url.path == "/login.cgi":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"wireless": {"txcapacity": 200000}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://10.0.0.3")
    airos = AirOsClient(
        AirOsTarget(key="bh", host="10.0.0.3", username="ro", password="s3cret"),
        client=client,
    )
    status = await airos.fetch_status()

    assert status["wireless"]["txcapacity"] == 200000
    assert "/login.cgi" in appels
    await airos.aclose()


async def test_airos_provider_lit_plusieurs_antennes_et_filtre() -> None:
    """Chaque radio est lue directement ; get_capacities filtre sur les cles."""

    def fake_client(target: AirOsTarget) -> AirOsClient:
        def handler(request: httpx.Request) -> httpx.Response:
            capacite = 100000 if target.key == "bh-a" else 250000
            return httpx.Response(
                200,
                json={
                    "host": {"hwaddr": f"00:11:22:33:44:{target.key[-1]}0"},
                    "wireless": {"txcapacity": capacite, "rxcapacity": capacite},
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=f"https://{target.host}"
        )
        return AirOsClient(target, client=client)

    provider = AirOsProvider(
        [
            AirOsTarget(key="bh-a", host="10.0.0.2"),
            AirOsTarget(key="bh-b", host="10.0.0.3"),
        ],
        client_factory=fake_client,
    )

    samples = await provider.get_capacities(["bh-a", "bh-b", "inconnu"])
    assert set(samples) == {"bh-a", "bh-b"}
    assert samples["bh-a"].capacity_mbps == 100.0
    assert samples["bh-b"].capacity_mbps == 250.0

    # raw_devices renormalise vers la forme attendue par la topologie.
    devices = await provider.raw_devices()
    ids = {d["identification"]["id"] for d in devices}
    assert ids == {"bh-a", "bh-b"}
    assert all(d["identification"]["mac"] for d in devices)
    await provider.aclose()


async def test_airos_provider_ignore_une_antenne_muette() -> None:
    """Une radio injoignable retire sa capacite mais n'entraine pas les autres."""

    def fake_client(target: AirOsTarget) -> AirOsClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if target.key == "hs":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"wireless": {"txcapacity": 100000}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=f"https://{target.host}"
        )
        return AirOsClient(target, client=client)

    provider = AirOsProvider(
        [AirOsTarget(key="ok", host="10.0.0.2"), AirOsTarget(key="hs", host="10.0.0.9")],
        client_factory=fake_client,
    )
    samples = await provider.get_capacities(["ok", "hs"])
    assert set(samples) == {"ok"}
    await provider.aclose()


async def test_db_airos_provider_relit_ses_antennes_a_chaque_cycle() -> None:
    """Ajouter une antenne dans la base la rend collectee, sans redemarrage."""
    from app.collectors.uisp import DbAirOsProvider

    antennes: list[AirOsTarget] = []

    async def loader() -> list[AirOsTarget]:
        return list(antennes)

    def fake_client(target: AirOsTarget) -> AirOsClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"wireless": {"txcapacity": 100000}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=f"https://{target.host}"
        )
        return AirOsClient(target, client=client)

    provider = DbAirOsProvider(loader, client_factory=fake_client)

    # Base vide au depart : rien a lire.
    assert await provider.get_capacities(["bh"]) == {}

    # L'operateur ajoute une antenne : le cycle suivant la voit.
    antennes.append(AirOsTarget(key="bh", host="10.0.0.2"))
    samples = await provider.get_capacities(["bh"])
    assert samples["bh"].capacity_mbps == 100.0

    # Il la retire : elle disparait de la collecte.
    antennes.clear()
    assert await provider.get_capacities(["bh"]) == {}
    await provider.aclose()

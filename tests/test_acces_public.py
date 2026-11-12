"""Nom de domaine et HTTPS : la config poussee au proxy, la validation, et le
fichier Compose qui demarre tout seul."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.services import public_access as acces

RACINE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("saisie", "attendu"),
    [
        ("qos.example.com", "qos.example.com"),
        ("QoS.Example.com.", "qos.example.com"),
        ("https://qos.example.com/login", "qos.example.com"),
    ],
)
def test_un_domaine_se_normalise(saisie: str, attendu: str) -> None:
    assert acces.check_domain(saisie) == attendu


@pytest.mark.parametrize(
    "saisie", ["198.51.100.2", "localhost", "qos", "-qos.example.com", "a b.com"]
)
def test_ce_qui_n_est_pas_un_domaine_est_refuse(saisie: str) -> None:
    with pytest.raises(acces.InvalidDomainError):
        acces.check_domain(saisie)


def test_la_config_initiale_du_proxy_est_celle_sans_domaine() -> None:
    """Le fichier livre a Caddy et la config « sans domaine » sont la meme."""
    initiale = json.loads((RACINE / "deploy" / "caddy" / "initial.json").read_text())
    assert initiale == acces.caddy_config(None)


def test_un_domaine_sert_https_redirige_http_et_garde_l_ip() -> None:
    conf = acces.caddy_config("qos.example.com", "noc@example.com")
    serveurs = conf["apps"]["http"]["servers"]
    https = serveurs["freeqos_https"]
    assert https["listen"] == [":443"]
    assert https["routes"][0]["match"] == [{"host": ["qos.example.com"]}]
    assert https["routes"][0]["handle"][0]["upstreams"] == [{"dial": "app:8000"}]
    http = serveurs["freeqos_http"]["routes"]
    assert http[0]["handle"][0]["status_code"] == 308
    # L'adresse IP reste servie : l'acces de secours.
    assert "match" not in http[1] and http[1]["handle"][0]["handler"] == "reverse_proxy"
    [politique] = conf["apps"]["tls"]["automation"]["policies"]
    assert politique["subjects"] == ["qos.example.com"]
    assert politique["issuers"] == [{"module": "acme", "email": "noc@example.com"}]
    # L'API d'administration n'accepte que le nom interne du proxy.
    assert conf["admin"]["origins"] == ["proxy:2019"]


async def test_pousser_la_config_passe_par_post_load(monkeypatch) -> None:
    recu: dict[str, Any] = {}

    def repondre(requete: httpx.Request) -> httpx.Response:
        recu["url"] = str(requete.url)
        recu["corps"] = json.loads(requete.content)
        return httpx.Response(200)

    vrai = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: vrai(transport=httpx.MockTransport(repondre), **k)
    )
    await acces.push("http://proxy:2019", acces.caddy_config("qos.example.com"))
    assert recu["url"] == "http://proxy:2019/load"
    assert "freeqos_https" in recu["corps"]["apps"]["http"]["servers"]


async def test_un_proxy_absent_se_dit_en_clair(monkeypatch) -> None:
    def refuser(requete: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=requete)

    vrai = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **k: vrai(transport=httpx.MockTransport(refuser), **k)
    )
    with pytest.raises(RuntimeError, match="proxy' container"):
        await acces.push("http://proxy:2019", acces.caddy_config(None))


def test_la_pile_redemarre_seule_et_le_proxy_est_branche() -> None:
    compose = yaml.safe_load((RACINE / "docker-compose.yml").read_text())
    services = compose["services"]
    for nom in ("timescaledb", "app", "proxy"):
        assert services[nom]["restart"] == "unless-stopped", nom
    proxy = services["proxy"]
    assert "${HTTP_PORT:-80}:80" in proxy["ports"] and "${HTTPS_PORT:-443}:443" in proxy["ports"]
    # L'API d'administration de Caddy n'est JAMAIS publiee.
    assert not any("2019" in str(p) for p in proxy["ports"])
    assert "--resume" in proxy["command"]
    app = services["app"]["environment"]
    sous_reseau = compose["networks"]["default"]["ipam"]["config"][0]["subnet"]
    assert sous_reseau.split(":-")[-1].rstrip("}") in app["FORWARDED_ALLOW_IPS"]
    assert app["PROXY_ADMIN_URL"].endswith("http://proxy:2019}")

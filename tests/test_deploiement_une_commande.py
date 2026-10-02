"""Deploiement en UNE commande : rien a saisir, rien a preparer.

DEMANDE EXPLICITE : "que le projet soit deployable seulement en une seule
commande via docker, qu'il n'y ait absolument rien a faire".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig
from app.services import netflow_export
from app.services.netflow_export import NetflowExportService
from tests.conftest import FakeRouterOsClient

RACINE = Path(__file__).resolve().parents[1]


def test_le_compose_n_exige_ni_env_ni_inventaire_ni_port_de_base() -> None:
    compose = yaml.safe_load((RACINE / "docker-compose.yml").read_text())
    app = compose["services"]["app"]
    # .env facultatif.
    assert all(isinstance(e, dict) and e.get("required") is False for e in app["env_file"])
    # Aucun fichier d'inventaire monte : les routeurs vivent en base.
    assert not any("config" in str(v) for v in app.get("volumes", []))
    # La base n'est pas publiee : un PostgreSQL deja present ne bloque rien.
    assert "ports" not in compose["services"]["timescaledb"]
    # NetFlow publie en UDP.
    assert any(str(p).endswith("/udp") for p in app["ports"])


async def test_dans_docker_l_adresse_netflow_vient_du_routeur(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dans le conteneur, l'adresse locale (172.x) est injoignable : le routeur
    dit lui-meme d'ou il voit notre session API."""
    client = FakeRouterOsClient()
    client.active_user_rows = [  # type: ignore[attr-defined]
        {"name": "admin", "via": "winbox", "address": "192.168.88.10"},
        {"name": "qos-ro", "via": "api", "address": "192.168.88.2"},
    ]
    collecteur = MikrotikCollector(
        RouterConfig(name="nas-a", host="192.0.2.11", username="qos-ro", password="x"),
        client=client,
    )
    monkeypatch.setattr(netflow_export, "dans_un_conteneur", lambda: True)
    monkeypatch.setattr(netflow_export, "local_address_for", lambda *a, **k: "172.18.0.3")
    service = NetflowExportService.__new__(NetflowExportService)
    service.collector_address = None

    assert await service.resolve_collector(collecteur) == "192.168.88.2"

    # Une adresse forcee reste prioritaire.
    service.collector_address = "10.0.0.9"
    assert await service.resolve_collector(collecteur) == "10.0.0.9"

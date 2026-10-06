"""BOUT EN BOUT : une application externe pilote le controleur par cle d'API.

LA VRAIE APPLICATION (create_app, son cycle de vie, la vraie base) ; seul le
RouterOS est simule -- un routeur qui GARDE ce qu'on lui ecrit, si bien que
chaque assertion porte sur ce qui se trouve reellement "sur le routeur" a la
fin, pas sur ce que l'API a repondu.

Le scenario d'un integrateur :

1. une cle d'API ``write`` ajoute un NOUVEAU ROUTEUR ; sa mise en service pose
   les types CAKE et l'export NetFlow tout seule ;
2. la collecte voit l'abonne PPPoE ; la cle lui pousse un FORFAIT -> sa file
   porte ce debit sur le routeur ;
3. la cle change le forfait -> la file suit ;
4. la cle FORCE une limite (prioritaire sur le forfait) -> la file suit ;
5. la cle remet le client au forfait par defaut -> la file suit ;
6. un service Preseem (``/model/v1``) a IP fixe -> sa file est posee ;
   le supprimer -> sa file disparait ;
7. les garde-fous : cle lecture refusee en ecriture, cle inconnue refusee,
   une cle ne gere ni les cles ni les comptes.
"""

from __future__ import annotations

import itertools
import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeRouterOsClient

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")

_IDS = itertools.count(0x1000)


class RouteurQuiRetient(FakeRouterOsClient):
    """Faux RouterOS dont les tables gardent ce que l'enforcement y ecrit."""


TABLES = {
    "/queue/simple": "simple_queue_rows",
    "/queue/type": "queue_type_rows",
    "/queue/tree": "queue_tree_rows",
    "/ip/firewall/filter": "firewall_filter_rows",
    "/ip/firewall/address-list": "firewall_address_list_rows",
    "/ip/firewall/mangle": "firewall_mangle_rows",
    "/ip/traffic-flow/target": "traffic_flow_target_rows",
}


def _ecrivain(routeur: RouteurQuiRetient) -> type:
    class Ecrivain:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def execute(self, action: Any) -> dict[str, Any]:
            if action.path == "/ip/traffic-flow":
                routeur.traffic_flow_row.update(action.fields)
                return {"id": ""}
            table = getattr(routeur, TABLES[action.path])
            if action.verb == "add":
                ident = f"*{next(_IDS):X}"
                table.append({".id": ident, **action.fields})
                return {"id": ident}
            ligne = next(x for x in table if x.get(".id") == action.target_id)
            if action.verb == "set":
                ligne.update(action.fields)
            else:
                table.remove(ligne)
            return {"id": action.target_id}

        def close(self) -> None:
            pass

    return Ecrivain


@pytest.fixture
def routeur() -> RouteurQuiRetient:
    r = RouteurQuiRetient(identity="nas-test")
    r.grant_write("qos")
    r.add_session("dupont", address="10.20.0.10")
    r.interfaces_rows.append(
        {
            ".id": "*1",
            "name": "ether1",
            "type": "ether",
            "running": "true",
            "rx-byte": "0",
            "tx-byte": "0",
        }
    )
    r.address_rows = [
        {"address": "10.20.0.1/24", "interface": "ether2", "network": "10.20.0.0"},
        {"address": "192.0.2.10/24", "interface": "ether1", "network": "192.0.2.0"},
    ]
    return r


@pytest.fixture
def app_client(routeur, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    import asyncpg

    import app.collectors.mikrotik as mk
    import app.services.shaping as sh
    from app.config import Settings
    from app.main import create_app

    async def _vider() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "TRUNCATE routers, pops, subscribers, subscriber_metrics, client_plans, "
                "shaping_policies, enforcement_audit, static_clients, api_keys, "
                "app_users, app_sessions, model_services_unplaced, topology_nodes, "
                "topology_links RESTART IDENTITY CASCADE"
            )
        finally:
            await conn.close()

    import asyncio

    asyncio.run(_vider())
    monkeypatch.setattr(mk, "LibrouterosReadClient", lambda *_a, **_k: routeur)
    monkeypatch.setattr(sh, "LibrouterosWriteClient", _ecrivain(routeur))
    reglages = Settings(
        _env_file=None,
        database_url=DSN,
        auth_enabled=True,
        scheduler_enabled=False,
        netflow_enabled=False,
        rtt_enabled=False,
        enforcement_enabled=True,
        routers=[],
        routers_file=None,
        app_secret_key_file=tmp_path / "secret.key",
        default_plan_down_mbps=100.0,
        default_plan_up_mbps=20.0,
    )
    with TestClient(create_app(reglages)) as client:
        yield client


def _cles(client: TestClient) -> tuple[str, str]:
    """Une cle d'ecriture et une cle de lecture, creees comme dans l'onglet API."""
    depot = client.app.state.container.api_keys_repo

    async def creer() -> tuple[str, str]:
        _f, ecriture = await depot.create(name="facturation", scopes=["read", "write"])
        _f, lecture = await depot.create(name="supervision", scopes=["read"])
        return ecriture, lecture

    # Dans la boucle de l'application : le pool de connexions y vit.
    assert client.portal is not None
    return client.portal.call(creer)


def _file(routeur: RouteurQuiRetient, cible: str) -> dict[str, Any] | None:
    return next(
        (q for q in routeur.simple_queue_rows if str(q.get("target", "")).startswith(cible)),
        None,
    )


def _attendre(condition, delai: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("condition jamais atteinte")


def test_une_application_externe_pilote_routeurs_et_limites(app_client, routeur) -> None:
    ecriture, lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}

    # --- 1. un nouveau routeur, ajoute par l'API
    r = app_client.post(
        "/api/v1/pops/routers",
        headers=cle,
        json={
            "name": "nas-test",
            "host": "192.0.2.10",
            "username": "qos",
            "password": "secret-routeur",
            "role": "pop",
            "pop_name": "PoP Test",
        },
    )
    assert r.status_code == 201, r.text

    def mise_en_service_finie() -> bool:
        etat = app_client.get("/api/v1/pops/provisioning/nas-test", headers=cle).json()
        return bool(etat.get("finished_at"))

    _attendre(mise_en_service_finie, 20)
    # Types CAKE poses, export NetFlow active : sans aucun geste.
    assert any("cake" in str(t.get("kind", "")) for t in routeur.queue_type_rows)
    assert routeur.traffic_flow_row.get("enabled") in ("yes", "true", True)

    # --- 2. la collecte voit l'abonne ; un forfait pousse par la cle -> file
    r = app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle)
    assert r.status_code == 200, r.text
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 50, "up_mbps": 10})
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "10000000/50000000"
    assert "freeqos:managed" in str(file.get("comment", ""))

    # --- 3. le forfait change -> la file suit
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 200, "up_mbps": 40})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "40000000/200000000"

    # --- 4. une limite FORCEE prime sur le forfait
    r = app_client.put(
        "/api/v1/shaping/policies",
        headers=cle,
        json={"scope": "subscriber", "target_key": "dupont", "max_down_mbps": 5, "max_up_mbps": 1},
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "1000000/5000000"

    # --- 5. retour au forfait par defaut (la limite forcee est levee avec lui)
    r = app_client.delete("/api/v1/plans/dupont", headers=cle)
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "20000000/100000000"

    # --- 6. un service Preseem a IP fixe : sa file, puis plus rien
    r = app_client.put(
        "/model/v1/services/pro-42",
        headers=cle,
        json={
            "id": "pro-42",
            "account": "acme",
            "down_speed": 300000,
            "up_speed": 300000,
            "attachments": [{"network_prefixes": ["10.20.0.42"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.42")["max-limit"] == "300000000/300000000"
    assert app_client.delete("/model/v1/services/pro-42", headers=cle).status_code == 200
    assert _file(routeur, "10.20.0.42") is None

    # --- 7. garde-fous
    lecteur = {"Authorization": f"Bearer {lecture}"}
    assert app_client.get("/api/v1/plans", headers=lecteur).status_code == 200
    assert (
        app_client.put(
            "/api/v1/plans/dupont", headers=lecteur, json={"down_mbps": 1, "up_mbps": 1}
        ).status_code
        == 403
    )
    assert (
        app_client.get(
            "/api/v1/plans", headers={"Authorization": "Bearer fqos_faux_cle"}
        ).status_code
        == 401
    )
    assert (
        app_client.post(
            "/api/v1/api-keys", headers=cle, json={"name": "x", "scopes": ["read"]}
        ).status_code
        == 403
    )
    assert app_client.get("/api/v1/users", headers=cle).status_code == 403
    # Sans cle ni session : rien.
    assert app_client.get("/api/v1/plans").status_code == 401

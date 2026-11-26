"""« Declared clients only » : un client n'existe que s'il est DECLARE.

1. Une session non declaree n'est pas un client : ni fiche, ni file -- sauf si
   la limite par defaut couvre les sessions sans forfait (1 kbps), et alors la
   session est listee avec son IP, sa MAC et son routeur.
2. Une session qui porte l'IP d'un client declare a IP fixe EST ce client : pas
   de second client, pas de fusion, la file est la sienne.
3. Declarer deux fois la meme adresse est refuse.
4. « Removed » est vide ; eteindre le mode remet tout comme avant.
"""

from __future__ import annotations

import os

import pytest

from app.services import declared
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


def test_ce_qui_est_declare() -> None:
    import ipaddress

    d = declared.Declared(
        logins={"martin"},
        networks=[
            (ipaddress.ip_network("10.0.0.8/32"), "ACME-POSTE"),
            (ipaddress.ip_network("10.0.0.0/29"), "ACME-SIEGE"),
        ],
    )
    assert d.covers("martin", None)
    assert d.carrier_of("10.0.0.8") == "ACME-POSTE"
    assert d.carrier_of("10.0.0.3") == "ACME-SIEGE"
    assert d.carrier_of("10.9.9.9") is None
    assert not d.covers("inconnu", "10.9.9.9")

    fiches = [
        {"reference": "A", "address": "10.0.0.0/29", "extra_prefixes": []},
        {"reference": "B", "address": "10.0.0.5/32", "extra_prefixes": []},
        {"reference": "C", "address": "10.1.0.5/32", "extra_prefixes": ["172.16.0.0/30"]},
        {"reference": "D", "address": "10.2.0.5/32", "enabled": False},
    ]
    assert declared.conflicts(fiches) == [{"client": "A", "other": "B", "address": "10.0.0.0/29"}]
    assert declared.overlapping("172.16.0.2", [], fiches) == {
        "client": "C",
        "address": "172.16.0.0/30",
    }
    assert declared.overlapping("10.1.0.5", [], fiches, ignore="C") is None
    # Une fiche desactivee ne bloque rien.
    assert declared.overlapping("10.2.0.5", [], fiches) is None


def _abonnes(client, cle) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.get("/api/v1/subscribers/latest?limit=500&include_unmeasured=true", headers=cle)
    assert r.status_code == 200, r.text
    return {str(x["login"]) for x in r.json()}


def _reglage(client, cle, nom, valeur) -> None:  # type: ignore[no-untyped-def]
    r = client.put(f"/api/v1/settings/{nom}", headers=cle, json={"value": valeur})
    assert r.status_code == 200, r.text


def _appliquer(client, cle) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_seuls_les_clients_declares(app_client, routeur) -> None:  # noqa: F811
    routeur.add_session("durand", address="10.20.0.11")
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
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
            "write_enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    _attendre(
        lambda: bool(
            app_client.get("/api/v1/pops/provisioning/nas-test", headers=cle)
            .json()
            .get("finished_at")
        ),
        20,
    )
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    # Mode eteint (le defaut) : les sessions sont des clients, comme avant.
    assert {"dupont", "durand"} <= _abonnes(app_client, cle)
    apercu = app_client.get("/api/v1/clients/registry", headers=cle).json()
    assert apercu["enabled"] is False
    assert {"dupont", "durand"} <= {c["login"] for c in apercu["detected"]}

    # Un ancien retire, qui ne servira plus a rien.
    r = app_client.post(
        "/api/v1/clients/remove", headers=cle, json={"logins": ["ancien"], "confirm": True}
    )
    assert r.status_code == 200, r.text

    # --- allume
    _reglage(app_client, cle, "declared_clients_only", True)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME-SIEGE",
            "pop_name": "PoP Test",
            "address": "10.20.0.10",
            "plan_down_mbps": 20,
            "plan_up_mbps": 10,
        },
    )
    assert r.status_code in (200, 201), r.text
    assert "merged_into" not in r.json()
    # La meme adresse une seconde fois : refusee.
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "ACME-BIS", "pop_name": "PoP Test", "address": "10.20.0.10"},
    )
    assert r.status_code == 409, r.text
    assert "ACME-SIEGE" in r.json()["detail"]

    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    logins = _abonnes(app_client, cle)
    assert "ACME-SIEGE" in logins
    assert not {"dupont", "durand"} & logins

    sessions = app_client.get("/api/v1/clients/unlisted-sessions", headers=cle).json()
    par_login = {s["login"]: s for s in sessions["sessions"]}
    assert par_login["dupont"]["declared_client"] == "ACME-SIEGE"
    assert par_login["durand"]["address"] == "10.20.0.11"
    assert par_login["durand"]["router"] == "nas-test"
    assert par_login["durand"]["limited"] is False

    # La file du client declare est posee (sur la session qui porte son IP) ;
    # la session non declaree n'en a pas.
    _appliquer(app_client, cle)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"
    assert _file(routeur, "10.20.0.11") is None

    # « Removed » est vide.
    assert app_client.post("/api/v1/clients/removed/clean", headers=cle).status_code == 200
    assert app_client.get("/api/v1/clients/removed", headers=cle).json()["count"] == 0

    # --- la limite par defaut couvre les sessions non declarees : 1 kbps
    _reglage(app_client, cle, "default_plan_down_mbps", 0.001)
    _reglage(app_client, cle, "default_plan_up_mbps", 0.001)
    _reglage(app_client, cle, "default_plan_for_detected_clients", True)
    _appliquer(app_client, cle)
    assert _file(routeur, "10.20.0.11")["max-limit"] == "1000/1000"
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"
    sessions = app_client.get("/api/v1/clients/unlisted-sessions", headers=cle).json()
    assert {s["login"]: s["limited"] for s in sessions["sessions"]} == {
        "dupont": False,
        "durand": True,
    }
    assert sessions["limit"] == {"down_mbps": 0.001, "up_mbps": 0.001}

    # --- eteint : comme avant
    _reglage(app_client, cle, "default_plan_for_detected_clients", False)
    _reglage(app_client, cle, "declared_clients_only", False)
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    assert {"dupont", "durand"} <= _abonnes(app_client, cle)

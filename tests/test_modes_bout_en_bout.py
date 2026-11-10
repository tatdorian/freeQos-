# ruff: noqa: F811 - les fixtures importees portent le nom des parametres
"""Modes de debit, de bout en bout : base reelle, API, et file sur le routeur.

Ignore sans ``TEST_DATABASE_URL`` (cf. test_bout_en_bout_api_externe).
"""

from __future__ import annotations

from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    pytestmark,
    routeur,
)


def _brancher(app_client, cle) -> None:  # type: ignore[no-untyped-def]
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
    _attendre(
        lambda: bool(
            app_client.get("/api/v1/pops/provisioning/nas-test", headers=cle)
            .json()
            .get("finished_at")
        ),
        20,
    )
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code == 200, r.text


def test_un_mode_pose_son_debit_puis_automatic_rend_le_forfait(app_client, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"

    backup = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Backup", "down_value": 6, "up_value": 6},
    ).json()
    nuit = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Night", "rate_kind": "percent", "down_value": 200, "up_value": 100},
    ).json()

    # Un client en Backup : 6/6, quel que soit son forfait.
    r = app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": backup["id"]})
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "6000000/6000000"
    ligne = next(
        x
        for x in app_client.get("/api/v1/subscribers/latest", headers=cle).json()
        if x["login"] == "dupont"
    )
    assert ligne["mode_name"] == "Backup"

    # Un mode en part du forfait : 200 % de 20 descendant, 100 % de 10 montant.
    app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": nuit["id"]})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/40000000"

    # Le mode change de debit : le client suit tout de suite.
    r = app_client.patch(
        f"/api/v1/bandwidth-modes/{nuit['id']}", headers=cle, json={"down_value": 300}
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/60000000"

    # Un mode donne a un client ne se supprime pas.
    assert (
        app_client.delete(f"/api/v1/bandwidth-modes/{nuit['id']}", headers=cle).status_code == 409
    )

    # Automatic : retour au forfait.
    r = app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": None})
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"
    assert (
        app_client.delete(f"/api/v1/bandwidth-modes/{nuit['id']}", headers=cle).status_code == 200
    )


def test_un_mode_de_crise_coupe_et_refuse_le_boost(app_client, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    crise = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Extreme backup", "down_value": 2, "up_value": 2, "disables_boost": True},
    ).json()

    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={"logins": ["dupont"], "action": "boost", "boost_multiplier": 3, "boost_minutes": 30},
    )
    assert r.json()["succeeded"] == 1, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "30000000/60000000"

    # Le mode de crise efface le boost en cours...
    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={"logins": ["dupont"], "action": "mode", "mode_id": crise["id"]},
    )
    assert r.json()["results"][0]["boost_stopped"] is True
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/2000000"

    # ... et en refuse un nouveau, client par client.
    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={"logins": ["dupont", "inconnu"], "action": "boost", "boost_multiplier": 2},
    )
    corps = r.json()
    assert corps["failed"] == 2
    assert "Extreme backup" in corps["results"][0]["detail"]


def test_le_journal_raconte_chaque_geste_et_le_forfait_garde_le_mode(app_client, routeur) -> None:
    """Qui a change quoi : chaque geste est au journal. Et changer le forfait
    d'un client ne lui retire ni son mode ni son boost."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    nuit = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Night", "rate_kind": "percent", "down_value": 200, "up_value": 100},
    ).json()
    app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": nuit["id"]})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/40000000"

    # Nouveau forfait : le mode reste, et suit le forfait (200 % de 30).
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 30, "up_mbps": 10})
    assert r.status_code == 200, r.text
    ligne = next(
        x
        for x in app_client.get("/api/v1/subscribers/latest", headers=cle).json()
        if x["login"] == "dupont"
    )
    assert ligne["mode_name"] == "Night"
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/60000000"

    app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={"logins": ["dupont"], "action": "boost", "boost_multiplier": 2, "boost_minutes": 30},
    )
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 25, "up_mbps": 10})
    assert app_client.get("/api/v1/shaping/boosts", headers=cle).json(), "le boost reste"

    journal = app_client.get("/api/v1/activity?login=dupont", headers=cle).json()
    actions = [a["action"] for a in journal]
    assert actions[:4] == ["plan", "boost", "plan", "mode"]
    assert journal[3]["detail"].startswith("Night: 40")
    assert journal[3]["written"] is True
    assert all(a["actor"].startswith("api:") for a in journal)
    assert app_client.get("/api/v1/activity?q=Night", headers=cle).json()

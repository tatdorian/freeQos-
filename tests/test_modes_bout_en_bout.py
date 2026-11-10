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


def test_un_boost_jusqu_a_arret_tourne_jusqu_au_stop(app_client, routeur) -> None:
    """Allume maintenant, eteint plus tard : pas de minuterie, c'est "Stop
    boost" qui le termine."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={
            "logins": ["dupont"],
            "action": "boost",
            "boost_multiplier": 2,
            "boost_until_stopped": True,
        },
    )
    assert r.json()["succeeded"] == 1, r.text
    [boost] = app_client.get("/api/v1/shaping/boosts", headers=cle).json()
    assert boost["seconds_left"] > 365 * 86400
    assert _file(routeur, "10.20.0.10")["max-limit"] == "20000000/40000000"

    r = app_client.post(
        "/api/v1/clients/actions", headers=cle, json={"logins": ["dupont"], "action": "stop_boost"}
    )
    assert r.json()["results"][0]["stopped"] is True
    assert app_client.get("/api/v1/shaping/boosts", headers=cle).json() == []
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"
    detail = app_client.get("/api/v1/activity?login=dupont", headers=cle).json()
    assert [a["action"] for a in detail[:2]] == ["stop_boost", "boost"]
    assert "until stopped" in detail[1]["detail"]


def test_un_forfait_temporaire_pour_une_duree_choisie(app_client, routeur) -> None:
    """Un debit explicite pour 3 jours, ici PLUS BAS que le forfait : le client
    y est tout de suite, et le journal dit quoi et pour combien de temps."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={
            "logins": ["dupont"],
            "action": "boost",
            "boost_down_mbps": 5,
            "boost_up_mbps": 2,
            "boost_minutes": 3 * 1440,
        },
    )
    assert r.json()["succeeded"] == 1, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/5000000"
    [boost] = app_client.get("/api/v1/shaping/boosts", headers=cle).json()
    assert 2.9 * 86400 < boost["seconds_left"] <= 3 * 86400
    [geste] = app_client.get("/api/v1/activity?login=dupont&limit=1", headers=cle).json()
    assert geste["detail"] == "temporary rate 5 / 2 Mbps for 3 days"


def test_un_profil_horaire_donne_un_mode_a_des_clients_choisis(app_client, routeur) -> None:  # noqa: F811
    """Une plage peut donner un MODE : le profil garde la reference, pas le debit,
    et suit le mode quand on le modifie."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
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

    plage = {"label": "all day", "start": "00:00", "end": "24:00", "mode_id": backup["id"]}
    r = app_client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={"name": "Secours", "scope": "subscribers", "targets": ["dupont"], "windows": [plage]},
    )
    assert r.status_code == 201, r.text
    profil = r.json()
    assert profil["windows"][0]["mode_name"] == "Backup"
    assert profil["windows"][0]["percent"] is None
    assert _file(routeur, "10.20.0.10")["max-limit"] == "6000000/6000000"

    # Le mode change : la plage suit, sans retoucher le profil.
    r = app_client.patch(
        f"/api/v1/bandwidth-modes/{backup['id']}",
        headers=cle,
        json={"down_value": 8, "up_value": 8},
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "8000000/8000000"

    # Un mode qu'un profil utilise ne se supprime pas en silence.
    r = app_client.delete(f"/api/v1/bandwidth-modes/{backup['id']}", headers=cle)
    assert r.status_code == 409
    assert "Secours" in r.json()["detail"]

    # Un mode en part du forfait : 200 % de 20 descendant, 100 % de 10 montant.
    r = app_client.patch(
        f"/api/v1/bandwidth-schedules/{profil['id']}",
        headers=cle,
        json={"windows": [{**plage, "mode_id": nuit["id"]}]},
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/40000000"

    # Un mode inconnu, ou un mode ET un debit : refuses.
    r = app_client.patch(
        f"/api/v1/bandwidth-schedules/{profil['id']}",
        headers=cle,
        json={"windows": [{**plage, "mode_id": 999999}]},
    )
    assert r.status_code == 400
    r = app_client.patch(
        f"/api/v1/bandwidth-schedules/{profil['id']}",
        headers=cle,
        json={"windows": [{**plage, "percent": 50}]},
    )
    assert r.status_code == 400
    liste = app_client.get("/api/v1/bandwidth-schedules", headers=cle).json()["schedules"]
    assert liste[0]["windows"][0]["mode_name"] == "Night"


def test_la_carte_place_un_site_et_le_rend(app_client, routeur) -> None:  # noqa: F811
    """Placer un site (Administrator), le relire, le deplacer, le retirer."""
    ecriture, lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    r = app_client.put(
        "/api/v1/site-locations/PoP Test",
        headers=cle,
        json={"latitude": 13.5137, "longitude": 2.1098},
    )
    assert r.status_code == 200, r.text
    assert r.json()["latitude"] == 13.5137
    app_client.put(
        "/api/v1/site-locations/PoP Test", headers=cle, json={"latitude": 13.6, "longitude": 2.2}
    )
    lieux = app_client.get("/api/v1/site-locations", headers=cle).json()["locations"]
    assert [(x["name"], x["latitude"]) for x in lieux] == [("PoP Test", 13.6)]
    # Hors de la Terre : refuse.
    r = app_client.put(
        "/api/v1/site-locations/PoP Test", headers=cle, json={"latitude": 95, "longitude": 2}
    )
    assert r.status_code == 422
    # Une cle en lecture voit la carte, sans pouvoir placer.
    lire = {"Authorization": f"Bearer {lecture}"}
    assert app_client.get("/api/v1/site-locations", headers=lire).status_code == 200
    r = app_client.put(
        "/api/v1/site-locations/Autre", headers=lire, json={"latitude": 1, "longitude": 1}
    )
    assert r.status_code == 403
    assert app_client.delete("/api/v1/site-locations/PoP Test", headers=cle).status_code == 200
    assert app_client.get("/api/v1/site-locations", headers=cle).json()["locations"] == []
    # Un client se place pareil, par son login.
    r = app_client.put(
        "/api/v1/client-locations/dupont", headers=cle, json={"latitude": 13.52, "longitude": 2.11}
    )
    assert r.status_code == 200, r.text
    lieux = app_client.get("/api/v1/client-locations", headers=lire).json()["locations"]
    assert [(x["name"], x["longitude"]) for x in lieux] == [("dupont", 2.11)]
    assert app_client.delete("/api/v1/client-locations/dupont", headers=lire).status_code == 403
    assert app_client.delete("/api/v1/client-locations/dupont", headers=cle).status_code == 200
    assert app_client.delete("/api/v1/client-locations/dupont", headers=cle).status_code == 404


def test_un_boost_multiplie_le_debit_en_vigueur_puis_le_rend(app_client, routeur) -> None:  # noqa: F811
    """Le cas reel : un client bride a 550 kbps, boost x1,5 -> 825 kbps (et
    non x1,5 de son forfait de base). A la fin du boost, arrete OU echu, il
    retrouve ses 550 kbps, pas son forfait."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)  # forfait 20 / 10
    bride = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Bride", "down_value": 0.55, "up_value": 0.55},
    ).json()
    app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": bride["id"]})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "550000/550000"

    r = app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={
            "logins": ["dupont"],
            "action": "boost",
            "boost_multiplier": 1.5,
            "boost_minutes": 30,
        },
    )
    assert r.json()["succeeded"] == 1, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "825000/825000"
    detail = app_client.get("/api/v1/activity?login=dupont", headers=cle).json()[0]["detail"]
    assert "550 kbps -> 825 kbps" in detail

    # Arrete : retour au mode, pas au forfait.
    app_client.post(
        "/api/v1/clients/actions", headers=cle, json={"logins": ["dupont"], "action": "stop_boost"}
    )
    assert _file(routeur, "10.20.0.10")["max-limit"] == "550000/550000"

    # Echu : pareil, par le job d'echeance.
    app_client.post(
        "/api/v1/clients/actions",
        headers=cle,
        json={"logins": ["dupont"], "action": "boost", "boost_multiplier": 2, "boost_minutes": 30},
    )
    assert _file(routeur, "10.20.0.10")["max-limit"] == "1100000/1100000"
    conteneur = app_client.app.state.container

    async def echoir() -> None:
        async with conteneur.database.pool.acquire() as conn:
            await conn.execute(
                "UPDATE shaping_policies SET boost_expires_at = now() - interval '1 minute' "
                "WHERE target_key = 'dupont'"
            )
        await conteneur.shaping.expire_boosts()

    assert app_client.portal is not None
    app_client.portal.call(echoir)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "550000/550000"
    assert app_client.get("/api/v1/shaping/boosts", headers=cle).json() == []

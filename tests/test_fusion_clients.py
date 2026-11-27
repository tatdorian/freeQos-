"""Un client declare a la main sur l'IP d'un client deja connu : UN seul client.

CE QUI EST FIXE ICI
-------------------
1. LE CLIENT DECLARE A LA MAIN GAGNE : une session seulement DETECTEE sur son
   IP (ni forfait, ni nom, ni mode) est retiree de freeQoS, comme avec
   « Remove ». La fiche reste, avec sa file a son nom.
2. Meme une session AVEC forfait est retiree : il ne reste qu'une file sur
   l'IP, celle du client declare.
3. Une fiche declaree AVANT que la session n'apparaisse : la tache periodique
   retire la session detectee ; la fiche garde son mode.
4. Une fiche poussee par l'API de facturation n'est jamais fusionnee.
"""

from __future__ import annotations

import os

import pytest

from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


def _logins(client, cle) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.get("/api/v1/subscribers", headers=cle)
    assert r.status_code == 200, r.text
    corps = r.json()
    lignes = corps if isinstance(corps, list) else corps.get("subscribers", corps.get("items", []))
    return {str(x["login"]) for x in lignes}


def _references(client, cle) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.get("/api/v1/static-clients", headers=cle)
    assert r.status_code == 200, r.text
    corps = r.json()
    lignes = corps if isinstance(corps, list) else corps.get("clients", corps.get("items", []))
    return {str(x["reference"]) for x in lignes}


def _brancher(client, cle) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
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
            client.get("/api/v1/pops/provisioning/nas-test", headers=cle).json().get("finished_at")
        ),
        20,
    )
    assert client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_la_session_seulement_detectee_est_retiree(app_client, routeur) -> None:  # noqa: F811
    """Le client declare a la main gagne : la session detectee sur son IP part
    dans « Removed », la fiche reste avec sa file a son nom."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    assert "dupont" in _logins(app_client, cle)

    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME OFFICE",
            "pop_name": "PoP Test",
            "address": "10.20.0.10",
            "plan_down_mbps": 20,
            "plan_up_mbps": 5,
        },
    )
    assert r.status_code == 201, r.text
    assert "merged_into" not in r.json()
    assert r.json()["removed_duplicate"] == "dupont"
    assert "ACME OFFICE" in _references(app_client, cle)
    retires = app_client.get("/api/v1/clients/removed", headers=cle).json()
    assert [c["login"] for c in retires["clients"]] == ["dupont"]

    # La session ne revient pas, et la file est celle de la fiche.
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    logins = _logins(app_client, cle)
    assert "dupont" not in logins and "ACME OFFICE" in logins
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "5000000/20000000"
    assert file["name"] == "ACME-OFFICE"
    activite = app_client.get("/api/v1/activity", headers=cle, params={"login": "ACME OFFICE"})
    assert any(a["action"] == "Duplicate removed" for a in activite.json())


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_meme_une_session_avec_forfait_est_retiree(app_client, routeur) -> None:  # noqa: F811
    """Le client declare a la main gagne TOUJOURS : une session qui porte son
    IP part, meme si elle avait un forfait. Sa file a elle disparait, il ne
    reste que celle du client declare (plus de doublon 16 / 4 Mbps)."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 4, "up_mbps": 4})
    assert r.status_code == 200, r.text
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["name"] == "dupont"

    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME OFFICE",
            "pop_name": "PoP Test",
            "address": "10.20.0.10",
            "plan_down_mbps": 16,
            "plan_up_mbps": 16,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["removed_duplicate"] == "dupont"
    assert "merged_into" not in r.json()
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    sur_l_ip = [
        q for q in routeur.simple_queue_rows if str(q.get("target", "")).startswith("10.20.0.10")
    ]
    assert [q["name"] for q in sur_l_ip] == ["ACME-OFFICE"], sur_l_ip
    assert sur_l_ip[0]["max-limit"] == "16000000/16000000"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_une_fiche_declaree_avant_la_session_retire_la_session_ensuite(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)

    # Personne sur 10.20.0.30 : la fiche reste une fiche.
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME SHOP",
            "label": "ACME Shop",
            "pop_name": "PoP Test",
            "address": "10.20.0.30",
        },
    )
    assert r.status_code == 201, r.text
    assert "merged_into" not in r.json()
    mode = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Slow", "down_value": 3, "up_value": 1},
    )
    assert mode.status_code == 201, mode.text
    r = app_client.put(
        "/api/v1/clients/ACME SHOP/mode",
        headers=cle,
        json={"mode_id": mode.json()["id"]},
    )
    assert r.status_code == 200, r.text

    # Une session seulement detectee apparait sur la meme IP : la tache
    # periodique la retire, la fiche declaree a la main reste.
    routeur.add_session("martin", address="10.20.0.30")
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    r = app_client.post("/api/v1/jobs/merge_clients/run", headers=cle)
    assert r.status_code == 200, r.text
    assert "ACME SHOP" in _references(app_client, cle)
    retires = app_client.get("/api/v1/clients/removed", headers=cle).json()
    assert "martin" in [c["login"] for c in retires["clients"]]
    assert "martin" not in _logins(app_client, cle)

    # Le mode reste sur la fiche.
    depot = app_client.app.state.container.topology_repo

    async def politiques() -> dict:  # type: ignore[type-arg]
        return await depot.policy_map("subscriber")

    assert app_client.portal is not None
    carte = app_client.portal.call(politiques)
    assert carte["ACME SHOP"]["mode_id"] == mode.json()["id"]
    assert "martin" not in carte


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_une_fiche_de_l_api_n_est_jamais_fusionnee(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    conteneur = app_client.app.state.container

    async def marquer_api() -> None:
        async with conteneur.database.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO static_clients (reference, label, pop_name, address, source) "
                "VALUES ('SVC-0042', 'Billing', 'PoP Test', '10.20.0.10', 'api')"
            )

    assert app_client.portal is not None
    app_client.portal.call(marquer_api)
    r = app_client.post("/api/v1/jobs/merge_clients/run", headers=cle)
    assert r.status_code == 200, r.text
    assert "SVC-0042" in _references(app_client, cle)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_deux_fiches_a_ip_fixe_sur_la_meme_ip_n_en_font_qu_une(app_client, routeur) -> None:  # noqa: F811
    """Constate : « ACME OFFICE » saisi a la main et « ACME-OFFICE » sur la meme
    IP s'affichaient deux fois (l'un hors ligne, l'autre mesure). La fiche a la
    main rejoint celle de l'API ; entre deux fiches a la main, la premiere."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    conteneur = app_client.app.state.container

    async def fiche_api() -> None:
        async with conteneur.database.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO static_clients (reference, label, pop_name, address, source) "
                "VALUES ('ACME-OFFICE', 'ACME', 'PoP Test', '10.20.0.60', 'api')"
            )

    assert app_client.portal is not None
    app_client.portal.call(fiche_api)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME OFFICE",
            "label": "ACME Office",
            "pop_name": "PoP Test",
            "address": "10.20.0.60",
            "plan_down_mbps": 30,
            "plan_up_mbps": 10,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["merged_into"] == "ACME-OFFICE"
    references = _references(app_client, cle)
    assert "ACME OFFICE" not in references and "ACME-OFFICE" in references

    # Deux fiches a la main : la seconde rejoint la premiere.
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "ACME SHOP", "pop_name": "PoP Test", "address": "10.20.0.70"},
    )
    assert r.status_code == 201 and "merged_into" not in r.json(), r.text
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME-SHOP",
            "label": "Shop",
            "pop_name": "PoP Test",
            "address": "10.20.0.70",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["merged_into"] == "ACME SHOP"
    assert "ACME-SHOP" not in _references(app_client, cle)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_renommer_un_client_ne_le_dedouble_pas(app_client, routeur) -> None:  # noqa: F811
    """Constate : modifier le nom d'un client depuis « Add a client » laissait
    l'ancien (hors ligne) a cote d'un nouveau sans historique. Le meme client
    change de nom : historique, mode, scheduler et file le suivent."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME SHOP",
            "pop_name": "PoP Test",
            "address": "10.20.0.80",
            "plan_down_mbps": 12,
            "plan_up_mbps": 4,
        },
    )
    assert r.status_code == 201, r.text
    ident = r.json()["id"]
    mode = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Slow", "down_value": 3, "up_value": 1},
    )
    assert mode.status_code == 201, mode.text
    r = app_client.put(
        "/api/v1/clients/ACME SHOP/mode", headers=cle, json={"mode_id": mode.json()["id"]}
    )
    assert r.status_code == 200, r.text
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    conteneur = app_client.app.state.container

    async def ident_abonne(login: str) -> int | None:
        async with conteneur.database.pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM subscribers WHERE login = $1", login)

    assert app_client.portal is not None
    avant = app_client.portal.call(ident_abonne, "ACME SHOP")
    assert avant is not None

    r = app_client.patch(
        f"/api/v1/static-clients/{ident}", headers=cle, json={"reference": "ACME STORE"}
    )
    assert r.status_code == 200, r.text
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200

    # Un seul client, le meme (meme identifiant, donc meme historique).
    assert _references(app_client, cle) >= {"ACME STORE"}
    assert "ACME SHOP" not in _references(app_client, cle)
    logins = _logins(app_client, cle)
    assert "ACME STORE" in logins and "ACME SHOP" not in logins
    assert app_client.portal.call(ident_abonne, "ACME STORE") == avant

    # Son mode a suivi.
    depot = conteneur.topology_repo

    async def politiques() -> dict:  # type: ignore[type-arg]
        return await depot.policy_map("subscriber")

    carte = app_client.portal.call(politiques)
    assert "ACME SHOP" not in carte
    assert carte["ACME STORE"]["mode_id"] == mode.json()["id"]

    # Une seule file sur le routeur, au nouveau nom.
    noms = [str(q.get("name", "")) for q in routeur.simple_queue_rows]
    assert "ACME-STORE" in noms, noms
    assert "ACME-SHOP" not in noms, noms

    # Le journal le dit.
    activite = app_client.get("/api/v1/activity", headers=cle, params={"login": "ACME STORE"})
    assert any(a["action"] == "Renamed" for a in activite.json())


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_renommer_vers_le_nom_d_une_session_est_refuse(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "ACME SHOP", "pop_name": "PoP Test", "address": "10.20.0.81"},
    )
    assert r.status_code == 201, r.text
    r = app_client.patch(
        f"/api/v1/static-clients/{r.json()['id']}", headers=cle, json={"reference": "dupont"}
    )
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


def _liste_clients(client, cle) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.get(
        "/api/v1/subscribers/latest",
        headers=cle,
        params={"limit": 500, "include_unmeasured": "true"},
    )
    assert r.status_code == 200, r.text
    return {str(x["login"]) for x in r.json()}


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_un_client_supprime_ou_retire_quitte_la_liste(app_client, routeur) -> None:  # noqa: F811
    """Constate : un client a IP fixe supprime de l'inventaire restait dans la
    liste des clients, hors ligne pour toujours. Il en sort ; un client retire
    de freeQoS aussi."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "ACME DEPOT", "pop_name": "PoP Test", "address": "10.20.0.90"},
    )
    assert r.status_code == 201, r.text
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    assert "ACME DEPOT" in _liste_clients(app_client, cle)

    r = app_client.delete(f"/api/v1/static-clients/{r.json()['id']}", headers=cle)
    assert r.status_code == 204, r.text
    assert "ACME DEPOT" not in _liste_clients(app_client, cle)

    assert "dupont" in _liste_clients(app_client, cle)
    r = app_client.post(
        "/api/v1/clients/remove", headers=cle, json={"logins": ["dupont"], "confirm": True}
    )
    assert r.status_code == 200, r.text
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    assert "dupont" not in _liste_clients(app_client, cle)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_le_scheduler_d_un_client_fusionne_est_rebranche(app_client, routeur) -> None:  # noqa: F811
    """Constate : une fiche fusionnee dans sa session laissait son scheduler
    viser l'ancien nom (« not seen now ») -- il ne s'appliquait plus. La tache
    de fusion le rebranche sur le client qui porte ce nom affiche."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    # La session « dupont » porte le nom affiche d'une ancienne fiche fusionnee.
    r = app_client.put(
        "/api/v1/clients/dupont/profile", headers=cle, json={"display_name": "ACME-HOME"}
    )
    assert r.status_code == 200, r.text
    r = app_client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={
            "name": "Night",
            "timezone": "Africa/Niamey",
            "scope": "subscribers",
            "targets": ["ACME-HOME", "client-inconnu"],
            "windows": [{"start": "22:00", "end": "06:00", "down_mbps": 8, "up_mbps": 8}],
        },
    )
    assert r.status_code == 201, r.text
    ident = r.json()["id"]

    assert app_client.post("/api/v1/jobs/merge_clients/run", headers=cle).status_code == 200
    horaires = app_client.get("/api/v1/bandwidth-schedules", headers=cle).json()
    liste = horaires["schedules"] if isinstance(horaires, dict) else horaires
    cible = next(h for h in liste if h["id"] == ident)
    # Rebranche sur le client ; une cible que rien ne designe reste telle quelle.
    assert sorted(cible["targets"]) == ["client-inconnu", "dupont"]

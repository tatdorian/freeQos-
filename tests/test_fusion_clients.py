"""Un client declare a la main sur l'IP d'un client deja connu : UN seul client.

CE QUI EST FIXE ICI
-------------------
1. Declarer a la main (IP fixe) l'adresse d'une session PPPoE deja connue ne
   cree PAS un second client : la fiche est fusionnee dans la session. Le nom
   saisi devient son nom affiche, le debit saisi son forfait, et sa file est
   posee sur la session.
2. Une fiche declaree AVANT que la session n'apparaisse est fusionnee par la
   tache periodique, avec son mode.
3. Une fiche poussee par l'API de facturation n'est jamais fusionnee.
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
def test_declarer_a_la_main_un_client_deja_connu_le_fusionne(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    assert "dupont" in _logins(app_client, cle)

    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME OFFICE",
            "label": "ACME Office",
            "pop_name": "PoP Test",
            "address": "10.20.0.10",
            "plan_down_mbps": 20,
            "plan_up_mbps": 5,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["merged_into"] == "dupont"

    # Un seul client : la session, sous le nom saisi.
    assert "ACME OFFICE" not in _references(app_client, cle)
    logins = _logins(app_client, cle)
    assert "dupont" in logins
    assert "ACME OFFICE" not in logins
    fiche = app_client.get("/api/v1/clients/dupont/profile", headers=cle).json()
    assert fiche["profile"]["display_name"] == "ACME Office"

    # Le debit saisi est son forfait, et sa file est posee sur la session.
    forfaits = app_client.get("/api/v1/plans", headers=cle).json()
    plan = next(c for c in forfaits["clients"] if c["login"] == "dupont")
    assert plan["down_mbps"] == 20
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "5000000/20000000"
    assert "acme" not in str(file.get("name", "")).lower()

    # Le journal du client le dit.
    activite = app_client.get("/api/v1/activity", headers=cle, params={"login": "dupont"})
    assert activite.status_code == 200, activite.text
    assert any(a["action"] == "Merged" for a in activite.json())


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_une_fiche_declaree_avant_la_session_est_fusionnee_ensuite(app_client, routeur) -> None:  # noqa: F811
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

    # La session apparait sur la meme IP : la tache periodique fusionne.
    routeur.add_session("martin", address="10.20.0.30")
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    r = app_client.post("/api/v1/jobs/merge_clients/run", headers=cle)
    assert r.status_code == 200, r.text
    assert "ACME SHOP" not in _references(app_client, cle)
    assert "martin" in _logins(app_client, cle)
    fiche = app_client.get("/api/v1/clients/martin/profile", headers=cle).json()
    assert fiche["profile"]["display_name"] == "ACME Shop"

    # Le mode a suivi : il vaut pour la session.
    depot = app_client.app.state.container.topology_repo

    async def politiques() -> dict:  # type: ignore[type-arg]
        return await depot.policy_map("subscriber")

    assert app_client.portal is not None
    carte = app_client.portal.call(politiques)
    assert "ACME SHOP" not in carte
    assert carte["martin"]["mode_id"] == mode.json()["id"]


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

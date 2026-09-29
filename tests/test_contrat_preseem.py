"""Contrat Preseem, appel pour appel.

DEMANDE EXPLICITE : une integration ecrite pour Preseem doit marcher en
changeant seulement l'URL de base et la cle. Ces tests rejouent a l'identique
les appels du client PHP de reference (github.com/jimlucas/Preseem_API, repris
par AmirMehrabi/Preseem), ecrit d'apres la documentation officielle :

    base    https://api.preseem.com/model/v1/
    auth    CURLOPT_USERPWD "<cle>:"            (Basic, cle en utilisateur)
    LIST    GET    <objet>?page=1&limit=500     -> reponse.data
    CREATE  PUT    <objet>/<id>  (JSON, id dans le corps)
    GET     GET    <objet>/<id>
    DELETE  DELETE <objet>/<id>
    succes  200 partout ; le client journalise toute autre reponse en erreur.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_api_publique import basic, ecriture, pieces  # noqa: F401

BASE = "/model/v1/"
OBJETS = ("access_points", "accounts", "packages", "services", "sites")


def _put(client: TestClient, secret: str, objet: str, params: dict[str, Any]) -> Any:
    # _api_create : l'identifiant est dans l'URL (rawurlencode) ET dans le corps.
    from urllib.parse import quote

    return client.put(
        BASE + objet + "/" + quote(params["id"], safe=""), headers=basic(secret), json=params
    )


def test_la_liste_se_lit_dans_data_avec_page_et_limit(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    for objet in OBJETS:
        reponse = client.get(BASE + objet + "?page=1&limit=500", headers=basic(secret))
        assert reponse.status_code == 200
        assert reponse.json()["data"] == []


def test_les_exemples_du_client_de_reference_passent_tels_quels(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    # preseem_api_access_point_new.php
    r = _put(
        client,
        secret,
        "access_points",
        {
            "id": "10",
            "name": "Access Point #10",
            "tower": "Cline Butte ",
            "ip_address": "192.168.10.10",
        },
    )
    assert r.status_code == 200
    # preseem_api_account_new.php / populate.php
    assert (
        _put(
            client,
            secret,
            "accounts",
            {
                "id": "41",
                "name": "Name of Account Owner 41",
            },
        ).status_code
        == 200
    )
    assert (
        _put(
            client,
            secret,
            "packages",
            {
                "id": "gold",
                "name": "Gold",
                "up_speed": 2000,
                "down_speed": 10000,
            },
        ).status_code
        == 200
    )
    assert _put(client, secret, "sites", {"id": "s1", "name": "Site 1"}).status_code == 200
    # preseem_api_service_new.php : attachments = [{cpe_mac, network_prefixes}]
    r = _put(
        client,
        secret,
        "services",
        {
            "id": "ServiceLocation_4321",
            "account": "CustomerName_1234",
            "package": "",
            "parent_device_id": "",
            "up_speed": 2000,
            "down_speed": 10000,
            "attachments": [{"cpe_mac": "00:10:0b:6e:4c:ff", "network_prefixes": ["12.12.12.12"]}],
        },
    )
    assert r.status_code == 200, r.text
    service = r.json()
    assert service["id"] == "ServiceLocation_4321"
    assert service["up_speed"] == 2000 and service["down_speed"] == 10000
    assert service["attachments"][0]["network_prefixes"] == ["12.12.12.12/32"]

    # _api_get
    lu = client.get(BASE + "access_points/10", headers=basic(secret))
    assert lu.status_code == 200
    assert lu.json()["tower"] == "Cline Butte "
    # _api_list, puis le parcours de populate.php : foreach ($results->data as $item)
    for objet in OBJETS:
        donnees = client.get(BASE + objet + "?page=1&limit=500", headers=basic(secret)).json()
        assert [item["id"] for item in donnees["data"]]
        for item in donnees["data"]:
            assert (
                client.delete(BASE + objet + "/" + item["id"], headers=basic(secret)).status_code
                == 200
            )


def test_un_debit_absent_est_omis_et_non_null(ecriture) -> None:  # noqa: F811
    """'If not set, this field is omitted in the returned json.'"""
    client, secret, _, _ = ecriture
    r = _put(client, secret, "packages", {"id": "sans-debit", "name": "Sans debit"})
    assert "up_speed" not in r.json() and "down_speed" not in r.json()
    lu = client.get(BASE + "packages/sans-debit", headers=basic(secret)).json()
    assert None not in lu.values()


def test_aucun_champ_interne_ne_fuit(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    r = _put(
        client,
        secret,
        "services",
        {
            "id": "svc-1",
            "account": "41",
            "down_speed": 10000,
            "up_speed": 2000,
            "attachments": [{"network_prefixes": ["10.0.0.9"]}],
        },
    )
    for interne in ("pop_name", "vlan", "enabled", "source", "enforcement", "updated_at"):
        assert interne not in r.json()
    # Le compte rendu de pose reste lisible, dans un en-tete.
    assert "X-FreeQoS-Enforcement" in r.headers


def test_les_codes_d_erreur_sont_ceux_de_preseem(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    # 400 : id de l'URI different de celui du JSON
    r = client.put(BASE + "accounts/a", headers=basic(secret), json={"id": "b", "name": "x"})
    assert r.status_code == 400
    # 400 : JSON mal forme ("Bad json")
    r = client.put(
        BASE + "accounts/a",
        headers={**basic(secret), "Content-Type": "application/json"},
        content=b"{pas du json",
    )
    assert r.status_code == 400
    # 404 : objet absent, a la lecture comme a la suppression
    assert client.get(BASE + "sites/absent", headers=basic(secret)).status_code == 404
    assert client.delete(BASE + "sites/absent", headers=basic(secret)).status_code == 404
    # 401 : sans cle, ou cle refusee
    assert client.get(BASE + "sites").status_code == 401
    assert client.get(BASE + "sites", headers=basic("fqos_faux")).status_code == 401


def test_la_pagination_decoupe_la_liste(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    for i in range(5):
        _put(client, secret, "accounts", {"id": f"c{i}", "name": f"Client {i}"})
    page1 = client.get(BASE + "accounts?page=1&limit=2", headers=basic(secret)).json()
    page3 = client.get(BASE + "accounts?page=3&limit=2", headers=basic(secret)).json()
    page4 = client.get(BASE + "accounts?page=4&limit=2", headers=basic(secret)).json()
    assert len(page1["data"]) == 2 and len(page3["data"]) == 1 and page4["data"] == []
    tout = client.get(BASE + "accounts", headers=basic(secret)).json()
    assert len(tout["data"]) == 5


def test_une_fiche_saisie_a_la_main_est_invisible_pour_la_facturation(ecriture) -> None:  # noqa: F811
    """Une synchronisation qui supprime ce qu'elle ne connait pas ne doit pas
    tomber sur un client declare dans l'interface (et recevoir 409 a chaque
    passage) : comme chez Preseem, seul ce que l'API a cree est liste."""
    client, secret, _, modele = ecriture
    modele.services["mairie"] = {"id": "mairie", "source": "manual", "attachments": []}
    donnees = client.get(BASE + "services?page=1&limit=500", headers=basic(secret)).json()
    assert "mairie" not in [s["id"] for s in donnees["data"]]


def test_la_mac_du_cpe_est_rendue_en_minuscules(ecriture) -> None:  # noqa: F811
    client, secret, _, _ = ecriture
    r = _put(
        client,
        secret,
        "services",
        {
            "id": "svc-mac",
            "account": "41",
            "attachments": [{"cpe_mac": "00:10:0B:6E:4C:FF", "network_prefixes": ["10.0.0.7"]}],
        },
    )
    assert r.json()["attachments"][0]["cpe_mac"] == "00:10:0b:6e:4c:ff"

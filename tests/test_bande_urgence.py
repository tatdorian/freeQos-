"""Bande passante NORMALE et d'URGENCE, client par client (demande client).

CE QUI EST FIXE ICI
-------------------
1. Le forfait est la bande passante normale : elle vaut quand le client n'a
   pas de mode.
2. Un mode marque « emergency » donne a chaque client SA bande passante
   d'urgence ; un client sans valeur d'urgence recoit le debit du mode.
3. Changer la bande passante d'urgence d'un client deja en mode d'urgence
   s'applique tout de suite ; la retirer lui rend le debit du mode.
4. Un scheduler qui donne un mode d'urgence applique celle de chaque client.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.db.modes_repo import mode_rates
from app.enforcement.schedules import BandwidthSchedule, ScheduleBook
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    _file,
    app_client,
    routeur,
)
from tests.test_fusion_clients import _brancher

DSN = os.environ.get("TEST_DATABASE_URL")

URGENCE = {"id": 9, "name": "Emergency", "rate_kind": "fixed", "down_value": 4, "up_value": 2}


# ------------------------------------------------------------------ unitaire
def test_un_mode_d_urgence_donne_la_valeur_du_client() -> None:
    assert mode_rates({**URGENCE, "emergency": True}, 20, 10, (8.0, 3.0)) == (8.0, 3.0)


def test_sans_valeur_d_urgence_le_debit_du_mode() -> None:
    assert mode_rates({**URGENCE, "emergency": True}, 20, 10, None) == (4.0, 2.0)


def test_un_mode_ordinaire_ignore_la_valeur_d_urgence() -> None:
    assert mode_rates({**URGENCE, "emergency": False}, 20, 10, (8.0, 3.0)) == (4.0, 2.0)


def test_un_scheduler_en_mode_d_urgence_donne_celle_de_chaque_client() -> None:
    profil = BandwidthSchedule.build(
        id=1,
        name="Storm",
        windows=[{"start": "00:00", "end": "24:00", "mode_id": 9}],
        modes={9: {**URGENCE, "emergency": True}},
    )
    carnet = ScheduleBook([profil])
    maintenant = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    avec = carnet.rate_for(
        login="a", sites=(), plan_down=20, plan_up=10, now=maintenant, emergency=(8.0, 3.0)
    )
    sans = carnet.rate_for(login="b", sites=(), plan_down=20, plan_up=10, now=maintenant)
    assert avec is not None and (avec.down_mbps, avec.up_mbps) == (8.0, 3.0)
    assert sans is not None and (sans.down_mbps, sans.up_mbps) == (4.0, 2.0)


# ------------------------------------------------------------- bout en bout
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_normale_sans_mode_urgence_en_mode_d_urgence(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.10")
    assert file is not None and file["max-limit"] == "10000000/20000000"  # normale

    mode = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Emergency", "down_value": 4, "up_value": 2, "emergency": True},
    )
    assert mode.status_code == 201, mode.text
    assert mode.json()["emergency"] is True
    ident = mode.json()["id"]

    # Sans valeur d'urgence : le debit du mode.
    r = app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": ident})
    assert r.status_code == 200, r.text
    assert r.json()["emergency_rate"] == "mode"
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/4000000"

    # Sa bande passante d'urgence : appliquee tout de suite, il est en urgence.
    r = app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 8, "up_mbps": 3}
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied_now"]["emergency_rate"] == "client"
    assert _file(routeur, "10.20.0.10")["max-limit"] == "3000000/8000000"
    lue = app_client.get("/api/v1/clients/dupont/emergency", headers=cle).json()
    assert (lue["down_mbps"], lue["up_mbps"]) == (8, 3)

    # La liste des clients la porte, et dit le debit reellement applique.
    lignes = app_client.get(
        "/api/v1/subscribers/latest",
        headers=cle,
        params={"include_unmeasured": "true", "limit": 50},
    ).json()
    ligne = next(x for x in lignes if x["login"] == "dupont")
    assert ligne["emergency_down_mbps"] == 8
    assert ligne["effective_down_mbps"] == 8

    # Plus de mode : la bande passante normale revient.
    r = app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": None})
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"

    # Retiree en mode d'urgence : le debit du mode.
    app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": ident})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "3000000/8000000"
    r = app_client.delete("/api/v1/clients/dupont/emergency", headers=cle)
    assert r.status_code == 200 and r.json()["removed"] is True, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/4000000"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_un_mode_ordinaire_ne_lit_pas_la_valeur_d_urgence(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 8, "up_mbps": 3}
    )
    mode = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Backup", "down_value": 6, "up_value": 6},
    )
    assert mode.status_code == 201 and mode.json()["emergency"] is False
    app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": mode.json()["id"]})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "6000000/6000000"

    # Le mode devient un mode d'urgence : ses clients passent a la leur.
    r = app_client.patch(
        f"/api/v1/bandwidth-modes/{mode.json()['id']}", headers=cle, json={"emergency": True}
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "3000000/8000000"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_la_valeur_d_urgence_d_un_client_inconnu_est_refusee(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.put(
        "/api/v1/clients/personne/emergency", headers=cle, json={"down_mbps": 8, "up_mbps": 3}
    )
    assert r.status_code == 404, r.text
    r = app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 0.01, "up_mbps": 3}
    )
    assert r.status_code == 422, r.text


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_un_scheduler_d_urgence_lance_a_la_main(app_client, routeur) -> None:  # noqa: F811
    """Sans page Modes : les debits sont saisis a la main sur le client, et
    c'est un scheduler qui passe les clients en bande passante d'urgence."""
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 8, "up_mbps": 3}
    )
    r = app_client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={
            "name": "Storm",
            "scope": "all",
            "windows": [{"start": "03:00", "end": "03:01", "days": ["sun"], "emergency": True}],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["windows"][0]["emergency"] is True
    ident = r.json()["id"]
    r = app_client.post(f"/api/v1/bandwidth-schedules/{ident}/start", headers=cle, json={})
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "3000000/8000000"

    # Sa valeur change pendant que le scheduler tourne : la file suit.
    app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 9, "up_mbps": 4}
    )
    assert _file(routeur, "10.20.0.10")["max-limit"] == "4000000/9000000"

    r = app_client.post(f"/api/v1/bandwidth-schedules/{ident}/stop", headers=cle)
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"


def test_l_interface_ne_propose_plus_de_debit_d_urgence() -> None:
    """Demande de l'exploitant : pas de debit d'urgence dans l'interface. Un
    client a son forfait ; les schedulers le changent d'un %. L'API reste."""
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert "<h3>Emergency bandwidth</h3>" not in js
    assert "cp-em-save" not in js
    assert "'<div class=\"card\"><h3>Plan</h3>'" in js
    assert '<button type="button" data-v="emergency" hidden>' in html
    assert "Their emergency bandwidth" not in html

"""Les files simples d'un routeur, et en retirer celles qu'on choisit.

1. La liste montre TOUTES les files, la marque de freeQoS ou non, et les
   DOUBLONS (deux files sur la meme adresse).
2. Retirer les files cochees retire celles-la, et rien d'autre.
3. Sans ``confirm``, rien ; une file dynamique (posee par le routeur) est refusee.
"""

from __future__ import annotations

import os

import pytest

from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    _file,
    app_client,
    routeur,
)
from tests.test_fusion_clients import _brancher

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_voir_les_doublons_et_retirer_celui_qu_on_choisit(app_client, routeur) -> None:  # noqa: F811
    ecriture, lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={
            "reference": "ACME-SHOP",
            "pop_name": "PoP Test",
            "address": "10.20.0.40",
            "plan_down_mbps": 16,
            "plan_up_mbps": 16,
        },
    )
    assert r.status_code == 201, r.text
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    # Une file posee A LA MAIN sur la meme adresse, et une file dynamique.
    routeur.simple_queue_rows.append(
        {".id": "*MAIN", "name": "OF0040", "target": "10.20.0.40/32", "max-limit": "4M/4M"}
    )
    routeur.simple_queue_rows.append(
        {".id": "*DYN", "name": "<pppoe-x>", "target": "<pppoe-x>", "dynamic": "true"}
    )

    d = app_client.get("/api/v1/pops/routers/nas-test/simple-queues", headers=cle).json()
    par_nom = {q["name"]: q for q in d["queues"]}
    assert par_nom["ACME-SHOP"]["ours"] is True
    assert par_nom["ACME-SHOP"]["same_target_as"] == ["OF0040"]
    assert par_nom["OF0040"]["ours"] is False
    assert par_nom["OF0040"]["same_target_as"] == ["ACME-SHOP"]
    assert par_nom["<pppoe-x>"]["dynamic"] is True
    assert d["duplicates"] == 2

    # Garde-fous : sans confirm, rien ; une cle en lecture, refusee.
    url = "/api/v1/pops/routers/nas-test/simple-queues/remove"
    assert app_client.post(url, headers=cle, json={"ids": ["*MAIN"]}).status_code == 400
    r = app_client.post(
        url,
        headers={"Authorization": f"Bearer {lecture}"},
        json={"ids": ["*MAIN"], "confirm": True},
    )
    assert r.status_code == 403

    r = app_client.post(url, headers=cle, json={"ids": ["*MAIN", "*DYN"], "confirm": True})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["removed"] == 1 and corps["names"] == ["OF0040"]
    assert any("dynamic" in e for e in corps["errors"])
    noms = {q["name"] for q in routeur.simple_queue_rows}
    assert "OF0040" not in noms
    assert {"ACME-SHOP", "<pppoe-x>"} <= noms
    assert _file(routeur, "10.20.0.40")["max-limit"] == "16000000/16000000"

"""Devices › « Set by freeQoS » : tout ce que freeQoS a pose sur un routeur.

Lu en direct, sans rien ecrire : files des clients, types CAKE, restrictions,
listes d'adresses, export NetFlow. Seules les lignes a la marque freeQoS y
figurent -- une file posee a la main est comptee a part, jamais listee comme
la notre. C'est la meme selection que « Remove freeQoS ».
"""

from __future__ import annotations

import os

import pytest

from app.web.ui import STATIC_DIR
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_ce_que_freeqos_a_pose_sur_le_routeur(app_client, routeur) -> None:  # noqa: F811
    ecriture, lecture = _cles(app_client)
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
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 50, "up_mbps": 10})
    assert r.status_code == 200, r.text
    # Une file posee A LA MAIN sur le routeur : jamais listee comme la notre.
    routeur.simple_queue_rows.append(
        {".id": "*FFF", "name": "main-office", "target": "10.9.9.9/32", "max-limit": "1M/1M"}
    )
    ecrits_avant = len(routeur.simple_queue_rows)

    r = app_client.get(
        "/api/v1/pops/routers/nas-test/freeqos-lines",
        headers={"Authorization": f"Bearer {lecture}"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    par_cle = {s["key"]: s for s in d["sections"]}
    files = par_cle["simple"]["rows"]
    assert [f["name"] for f in files] == ["dupont"]
    assert files[0]["max-limit"] == "10000000/50000000"
    assert {t["name"] for t in par_cle["type"]["rows"]} == {"freeqos-cake-up", "freeqos-cake-down"}
    assert d["other_simple_queues"] == 1
    assert d["total"] == 3
    assert d["netflow"]["enabled"] is True
    assert any(c["ours"] for c in d["netflow"]["targets"])
    # Lecture seule : rien n'a bouge sur le routeur.
    assert len(routeur.simple_queue_rows) == ecrits_avant

    assert (
        app_client.get("/api/v1/pops/routers/inconnu/freeqos-lines", headers=cle).status_code == 404
    )


def test_le_bouton_est_dans_devices() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "data-freeqos-lines=" in js
    assert "Set by freeQoS</button>" in js
    assert "'/freeqos-lines'" in js

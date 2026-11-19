"""Chaque creneau d'un planning est aussi un mode, sans rien faire.

LE MANQUE (retour terrain) : les creneaux Evening / Night d'un planning
n'etaient pas des modes. Un client ne pouvait pas etre mis en Night a la main
(la liste n'offrait qu'Automatic), ni garder son forfait a toute heure.

CE QUI EST FIXE ICI
-------------------
1. Enregistrer un planning cree un mode par creneau nomme (meme pourcentage du
   forfait) et le mode Constant (100 %).
2. Le pourcentage se modifie dans le PLANNING : le mode suit.
3. Un mode de l'exploitant portant le meme nom n'est jamais modifie.
4. Le mode Constant garde le forfait d'un client pendant un creneau.
"""

# ruff: noqa: F811 - les fixtures importees portent le nom des parametres

from __future__ import annotations

import os

import pytest

from app.enforcement.planner import effective_rate
from app.services.slot_modes import NOTE_CRENEAU
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _vider_le_registre_des_plannings():  # type: ignore[no-untyped-def]
    """Le registre des plannings est partage par tout le processus : un planning
    laisse ici (toute la journee a 70 %) brideraient les tests suivants."""
    from app.enforcement.schedules import BOOK

    yield
    BOOK.replace([])


TOUTE_LA_JOURNEE = [
    {"label": "evening", "start": "00:00", "end": "12:00", "percent": 70},
    {"label": "night", "start": "12:00", "end": "24:00", "percent": 200},
]


def _modes(client, cle) -> dict[str, dict]:  # type: ignore[no-untyped-def,type-arg]
    r = client.get("/api/v1/bandwidth-modes", headers=cle)
    assert r.status_code == 200, r.text
    return {m["name"]: m for m in r.json()["modes"]}


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_les_creneaux_deviennent_des_modes(app_client, routeur) -> None:
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    r = app_client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={"name": "Day and night", "timezone": "UTC", "windows": TOUTE_LA_JOURNEE},
    )
    assert r.status_code == 201, r.text
    assert set(r.json()["slot_modes"]) == {"Evening", "Night", "Constant"}

    modes = _modes(app_client, cle)
    assert modes["Evening"]["rate_kind"] == "percent"
    assert modes["Evening"]["down_value"] == 70
    assert modes["Night"]["down_value"] == 200
    assert modes["Constant"]["down_value"] == 100
    assert modes["Evening"]["note"] == NOTE_CRENEAU

    # Le % se change dans le planning : le mode suit.
    ident = r.json()["id"]
    plages = [dict(TOUTE_LA_JOURNEE[0], percent=60), TOUTE_LA_JOURNEE[1]]
    r = app_client.patch(
        f"/api/v1/bandwidth-schedules/{ident}", headers=cle, json={"windows": plages}
    )
    assert r.status_code == 200, r.text
    assert r.json()["slot_modes"] == ["Evening"]
    assert _modes(app_client, cle)["Evening"]["down_value"] == 60

    # Rien de change : rien n'est reecrit.
    r = app_client.patch(
        f"/api/v1/bandwidth-schedules/{ident}", headers=cle, json={"windows": plages}
    )
    assert r.json()["slot_modes"] == []


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_un_mode_de_l_exploitant_n_est_jamais_modifie(app_client, routeur) -> None:
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    r = app_client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Night", "rate_kind": "fixed", "down_value": 8, "up_value": 2},
    )
    assert r.status_code == 201, r.text
    r = app_client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={"name": "Day and night", "timezone": "UTC", "windows": TOUTE_LA_JOURNEE},
    )
    assert r.status_code == 201, r.text
    assert "Night" not in r.json()["slot_modes"]
    nuit = _modes(app_client, cle)["Night"]
    assert nuit["rate_kind"] == "fixed"
    assert nuit["down_value"] == 8


def test_constant_garde_le_forfait_pendant_un_creneau() -> None:
    """Un mode passe avant le planning : Constant (100 %) = le forfait."""
    debit, _source = effective_rate(
        plan_mbps=20.0,
        override_mbps=20.0,  # Constant : 100 % du forfait
        boost_mbps=None,
        boost_expires_at=None,
        schedule_mbps=14.0,  # le soir a 70 %
    )
    assert debit == 20.0


def test_l_interface_explique_les_modes_des_creneaux() -> None:
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert "Pick <b>Evening</b> or <b>Night</b> to force it now" in js
    assert "Each named slot is also a" in html
    assert "Make modes" not in js

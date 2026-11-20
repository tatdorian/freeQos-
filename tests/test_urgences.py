"""Page Emergency : des urgences preparees, lancees d'un bouton.

CE QUI EST FIXE ICI
-------------------
1. Une urgence a un nom, un debit (% du plan, limite fixe, ou la bande
   passante d'urgence propre a chaque client) et des clients (tous, des sites,
   des clients choisis).
2. « Activate » applique tout de suite, devant le plan et les schedulers ;
   « Stop » rend a chacun son plan.
3. Plusieurs urgences : sur un client commun, la DERNIERE activee vaut.
4. Une limite fixe est un plafond : jamais au-dessus du plan d'un client.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.enforcement.emergency import (
    EmergencyBook,
    InvalidEmergencyError,
    check,
    to_model,
)
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    _file,
    app_client,
    routeur,
)
from tests.test_fusion_clients import _brancher

DSN = os.environ.get("TEST_DATABASE_URL")
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def _urgence(ident: int, **champs):  # type: ignore[no-untyped-def]
    return to_model(
        {"id": ident, "name": f"E{ident}", "active": True, "active_since": T0, **champs}
    )


# ------------------------------------------------------------------ unitaire
def test_un_pourcentage_du_plan() -> None:
    assert _urgence(1, kind="percent", percent=50).rate(20, 10) == (10, 5)
    assert _urgence(1, kind="percent", percent=50).rate(None, None) is None


def test_une_limite_fixe_est_un_plafond() -> None:
    u = _urgence(1, kind="fixed", down_mbps=8, up_mbps=3)
    assert u.rate(20, 10) == (8, 3)
    assert u.rate(5, 2) == (5, 2)  # jamais au-dessus du plan


def test_la_bande_passante_propre_au_client() -> None:
    u = _urgence(1, kind="client")
    assert u.rate(20, 10, (6.0, 2.0)) == (6.0, 2.0)
    assert u.rate(20, 10, None) is None


def test_qui_est_vise() -> None:
    assert _urgence(1, kind="percent", percent=50).governs("a", "Plateau")
    clients = _urgence(1, kind="percent", percent=50, scope="clients", targets=["a"])
    assert clients.governs("a", None) and not clients.governs("b", None)
    sites = _urgence(1, kind="percent", percent=50, scope="pops", targets=["Plateau"])
    assert sites.governs("x", "plateau") and not sites.governs("x", "Bassora")


def test_la_derniere_activee_l_emporte() -> None:
    carnet = EmergencyBook()
    carnet.replace(
        [
            _urgence(1, kind="percent", percent=50),
            to_model(
                {
                    "id": 2,
                    "name": "Late",
                    "kind": "fixed",
                    "down_mbps": 4,
                    "up_mbps": 2,
                    "active": True,
                    "active_since": T0 + timedelta(minutes=5),
                }
            ),
            to_model({"id": 3, "name": "Off", "kind": "percent", "percent": 10, "active": False}),
        ]
    )
    r = carnet.for_client(login="a", site=None, plan_down=20, plan_up=10)
    assert r is not None and (r.down_mbps, r.up_mbps, r.name) == (4, 2, "Late")


def test_la_saisie_est_verifiee() -> None:
    with pytest.raises(InvalidEmergencyError):
        check({"name": "", "kind": "percent", "percent": 50})
    with pytest.raises(InvalidEmergencyError):
        check({"name": "X", "kind": "percent", "percent": 150})
    with pytest.raises(InvalidEmergencyError):
        check({"name": "X", "kind": "fixed", "down_mbps": 0.01, "up_mbps": 1})
    with pytest.raises(InvalidEmergencyError):
        check({"name": "X", "kind": "percent", "percent": 50, "scope": "clients", "targets": []})


# ------------------------------------------------------------- bout en bout
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_activer_puis_arreter_plusieurs_urgences(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"

    moitie = app_client.post(
        "/api/v1/emergencies",
        headers=cle,
        json={"name": "Backhaul down", "kind": "percent", "percent": 50},
    )
    assert moitie.status_code == 201, moitie.text
    assert moitie.json()["clients"] >= 1 and moitie.json()["active"] is False
    serree = app_client.post(
        "/api/v1/emergencies",
        headers=cle,
        json={
            "name": "Power cut",
            "kind": "fixed",
            "down_mbps": 4,
            "up_mbps": 2,
            "scope": "clients",
            "targets": ["dupont"],
        },
    )
    assert serree.status_code == 201, serree.text

    # Un bouton : la moitie du plan, tout de suite.
    r = app_client.post(f"/api/v1/emergencies/{moitie.json()['id']}/activate", headers=cle)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert _file(routeur, "10.20.0.10")["max-limit"] == "5000000/10000000"

    # La seconde, activee apres, l'emporte sur le client commun.
    app_client.post(f"/api/v1/emergencies/{serree.json()['id']}/activate", headers=cle)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/4000000"

    # La liste des clients le dit.
    lignes = app_client.get(
        "/api/v1/subscribers/latest", headers=cle, params={"include_unmeasured": "true"}
    ).json()
    ligne = next(x for x in lignes if x["login"] == "dupont")
    assert ligne["emergency_name"] == "Power cut" and ligne["effective_down_mbps"] == 4

    # Stop sur la seconde : la premiere reprend ; Stop sur la premiere : le plan.
    app_client.post(f"/api/v1/emergencies/{serree.json()['id']}/stop", headers=cle)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "5000000/10000000"
    r = app_client.post(f"/api/v1/emergencies/{moitie.json()['id']}/stop", headers=cle)
    assert r.json()["active"] is False
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"

    # Le journal du client le dit.
    activite = app_client.get("/api/v1/activity", headers=cle, params={"login": "dupont"}).json()
    assert any("Emergency 'Power cut' on" in str(a.get("detail")) for a in activite)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_la_bande_passante_propre_et_la_modification_en_cours(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    app_client.put(
        "/api/v1/clients/dupont/emergency", headers=cle, json={"down_mbps": 6, "up_mbps": 2}
    )
    u = app_client.post(
        "/api/v1/emergencies", headers=cle, json={"name": "Own", "kind": "client"}
    ).json()
    app_client.post(f"/api/v1/emergencies/{u['id']}/activate", headers=cle)
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2000000/6000000"

    # Modifiee pendant qu'elle tourne : ses clients suivent tout de suite.
    r = app_client.patch(
        f"/api/v1/emergencies/{u['id']}", headers=cle, json={"kind": "percent", "percent": 25}
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10")["max-limit"] == "2500000/5000000"

    # Supprimee pendant qu'elle tourne : le plan revient.
    assert app_client.delete(f"/api/v1/emergencies/{u['id']}", headers=cle).status_code == 200
    assert _file(routeur, "10.20.0.10")["max-limit"] == "10000000/20000000"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_deux_urgences_du_meme_nom_sont_refusees(app_client, routeur) -> None:  # noqa: F811
    ecriture, _lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    corps = {"name": "Storm", "kind": "percent", "percent": 50}
    assert app_client.post("/api/v1/emergencies", headers=cle, json=corps).status_code == 201
    assert app_client.post("/api/v1/emergencies", headers=cle, json=corps).status_code == 409


def test_la_page_emergency() -> None:
    from app.web.ui import STATIC_DIR

    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'href="#/emergency" data-view="emergency"' in html
    assert 'id="view-emergency"' in html and 'id="em-form"' in html
    assert "emergency: loadEmergencyPage," in js
    assert "data-em-go=" in js and "data-em-stop=" in js
    assert "/emergencies/' + id + '/' + (marche ? 'activate' : 'stop')" in js

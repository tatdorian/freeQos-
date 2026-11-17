"""Retirer des clients de FREEQOS -- pas du routeur -- et garder les autres.

CE QUI EST FIXE ICI
-------------------
1. Un client retire NE REVIENT PAS, meme encore connecte : le releve suivant
   l'ignore (ni fiche, ni mesure).
2. RIEN DU CLIENT N'EST TOUCHE SUR LE ROUTEUR : sa session PPPoE reste. Seule
   la file que freeQoS avait posee pour lui est retiree, pour qu'aucun plafond
   oublie ne reste applique.
3. Les AUTRES clients ne bougent pas : leur file reste, leur forfait aussi.
4. On peut le RETABLIR : il revient au releve suivant, sans forfait.
5. Geste d'Administrator : une cle en lecture est refusee ; sans ``confirm``,
   rien ne se passe.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.services.exclusions import ExcludedClients
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


def test_la_liste_en_memoire() -> None:
    liste = ExcludedClients()

    async def scenario() -> None:
        await liste.add(["dupont", "martin"], "admin@example.com")
        assert "dupont" in liste
        assert liste.logins == frozenset({"dupont", "martin"})
        assert [c["login"] for c in await liste.list()] == ["dupont", "martin"]
        assert await liste.remove("dupont") is True
        assert await liste.remove("dupont") is False
        assert "dupont" not in liste
        assert 42 not in liste

    asyncio.run(scenario())


def _logins(client, cle) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.get("/api/v1/subscribers", headers=cle)
    assert r.status_code == 200, r.text
    corps = r.json()
    lignes = corps if isinstance(corps, list) else corps.get("subscribers", corps.get("items", []))
    return {str(x["login"]) for x in lignes}


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_retirer_un_client_de_freeqos_sans_toucher_au_routeur(app_client, routeur) -> None:  # noqa: F811
    routeur.add_session("durand", address="10.20.0.11")
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
    for login in ("dupont", "durand"):
        r = app_client.put(
            f"/api/v1/plans/{login}", headers=cle, json={"down_mbps": 50, "up_mbps": 10}
        )
        assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10") is not None
    assert _file(routeur, "10.20.0.11") is not None
    assert {"dupont", "durand"} <= _logins(app_client, cle)

    # Garde-fous : sans confirm, rien ; une cle en lecture, refusee.
    r = app_client.post("/api/v1/clients/remove", headers=cle, json={"logins": ["dupont"]})
    assert r.status_code == 400
    r = app_client.post(
        "/api/v1/clients/remove",
        headers={"Authorization": f"Bearer {lecture}"},
        json={"logins": ["dupont"], "confirm": True},
    )
    assert r.status_code == 403
    assert "dupont" in _logins(app_client, cle)

    # --- retrait
    r = app_client.post(
        "/api/v1/clients/remove", headers=cle, json={"logins": ["dupont"], "confirm": True}
    )
    assert r.status_code == 200, r.text
    rapport = r.json()["clients"][0]
    assert rapport["login"] == "dupont"
    assert rapport["plan_removed"] is True

    liste = app_client.get("/api/v1/clients/removed", headers=cle).json()
    assert liste["count"] == 1
    assert liste["clients"][0]["login"] == "dupont"

    # Encore connecte : le releve suivant ne le recree pas.
    assert any(s["name"] == "dupont" for s in routeur.active)
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    logins = _logins(app_client, cle)
    assert "dupont" not in logins
    assert "durand" in logins
    forfaits = app_client.get("/api/v1/plans", headers=cle).json()
    vus = {c["login"] for c in forfaits["clients"]} | {c["login"] for c in forfaits["pending"]}
    assert "dupont" not in vus
    assert "durand" in vus

    # La prochaine ecriture retire SA file (et seulement la sienne).
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    assert _file(routeur, "10.20.0.10") is None, routeur.simple_queue_rows
    assert _file(routeur, "10.20.0.11")["max-limit"] == "10000000/50000000"
    # Sur le routeur, la session du client est intacte.
    assert any(s["name"] == "dupont" for s in routeur.active)

    # Une nouvelle ecriture ne la repose pas.
    app_client.put("/api/v1/plans/durand", headers=cle, json={"down_mbps": 60, "up_mbps": 12})
    assert _file(routeur, "10.20.0.10") is None

    # --- retabli : il revient au releve suivant, sans forfait (donc sans file)
    r = app_client.delete("/api/v1/clients/removed/dupont", headers=cle)
    assert r.status_code == 200, r.text
    assert app_client.delete("/api/v1/clients/removed/dupont", headers=cle).status_code == 404
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    assert "dupont" in _logins(app_client, cle)
    assert app_client.get("/api/v1/clients/removed", headers=cle).json()["count"] == 0
    assert _file(routeur, "10.20.0.10") is None


def test_l_interface_propose_le_retrait_aux_administrateurs() -> None:
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="cli-sel-remove"' in html
    assert 'id="cli-removed"' in html
    assert "document.getElementById('cli-sel-remove').hidden = !peut('edit');" in js
    # Le texte dit clairement que le routeur n'est pas touche.
    assert "on the router</b>: PPPoE account, session and address stay as they are." in js

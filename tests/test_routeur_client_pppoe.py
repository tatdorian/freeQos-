"""Un routeur client d'un autre, en PPPoE : on peut le limiter sur son parent.

CONSTATE : un forfait pose sur un client PPPoE n'etait jamais ecrit. Son
adresse etait celle d'un autre routeur gere (le routeur du client, connecte en
PPPoE a son parent) : le garde-fou « infrastructure » l'ecartait partout,
sans le dire -- la page du client affichait seulement « No queue ».

CE QUI EST FIXE ICI
-------------------
1. Une session PPPoE ouverte sur un routeur, dont l'adresse appartient a un
   AUTRE de nos routeurs, est un client de ce routeur : son forfait s'applique.
2. Le garde-fou reste entier : l'adresse du routeur lui-meme, sa passerelle,
   un loopback couvert par un bloc, un client a IP fixe.
3. Sans file, la page du client dit POURQUOI.
"""

from __future__ import annotations

import pytest

from app.collectors import mikrotik
from app.enforcement.planner import SubscriberTarget, desired_state
from app.services import client_health as sante
from app.services.shaping import protect_infrastructure


@pytest.fixture(autouse=True)
def reseau(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mikrotik, "_AMONTS", {"NAS-PARENT": ("192.0.2.1", "ether1")})
    monkeypatch.setattr(
        mikrotik,
        "_PROPRIETAIRES",
        {
            # L'adresse PPPoE du routeur client, vue dans SA table d'adresses.
            "198.51.100.34": ("NAS-CHILD", "pppoe-out1"),
            # Une adresse du routeur parent lui-meme.
            "198.51.100.1": ("NAS-PARENT", "bridge-pppoe"),
        },
    )
    monkeypatch.setattr(mikrotik, "_LOOPBACKS_DETECTES", {"NAS-PARENT": "10.255.0.1"})


def _client(login: str, adresse: str, kind: str = "pppoe") -> SubscriberTarget:
    return SubscriberTarget(
        login=login,
        interface="",
        plan_down_mbps=100,
        plan_up_mbps=100,
        kind=kind,
        address=adresse,
    )


def test_le_routeur_client_est_limite_sur_son_parent() -> None:
    client = _client("office", "198.51.100.34")
    protect_infrastructure("NAS-PARENT", [], [client])
    assert client.skip_reason is None
    _types, files, ecartes = desired_state(links=[], subscribers=[client])
    assert [f.target for f in files] == ["198.51.100.34/32"]
    assert ecartes == []


def test_le_garde_fou_reste_entier() -> None:
    clients = [
        # L'adresse du routeur parent lui-meme : jamais.
        _client("lui-meme", "198.51.100.1"),
        # Sa passerelle : jamais.
        _client("passerelle", "192.0.2.1"),
        # Un bloc qui couvre son loopback : jamais.
        _client("bloc", "10.255.0.0/24", kind="static"),
        # Un client a IP FIXE sur l'adresse d'un autre routeur : jamais.
        _client("statique", "198.51.100.34", kind="static"),
    ]
    protect_infrastructure("NAS-PARENT", [], clients)
    assert all(c.skip_reason for c in clients), [c.login for c in clients if not c.skip_reason]


def test_sur_son_propre_routeur_il_reste_protege() -> None:
    """Vu depuis le routeur client lui-meme, c'est SON adresse : jamais bride."""
    client = _client("office", "198.51.100.34")
    protect_infrastructure("NAS-CHILD", [], [client])
    assert client.skip_reason


def test_sans_file_la_page_dit_pourquoi() -> None:
    r = sante.limite(None, True, {"code": "writing_off", "reason": "Writing to the routers is off"})
    assert r["title"] == "No queue" and "Writing to the routers is off" in r["detail"]
    assert r["level"] == "warn"
    r = sante.limite(None, True, {"code": "no_rate", "reason": "no rate to apply"})
    assert r["title"] == "No limit to hold" and r["level"] == "ok"
    r = sante.limite(None, True, {"code": "pending", "reason": "Will be written on NAS"})
    assert r["title"] == "Not written yet"
    # Sans motif : comme avant.
    assert sante.limite(None, True)["title"] == "No queue"


# --------------------------------------------------------------- bout en bout
import os  # noqa: E402

from tests.test_bout_en_bout_api_externe import (  # noqa: E402, F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_bout_en_bout_le_forfait_est_ecrit_et_le_motif_dit(app_client, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
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
    # L'adresse de la session de « dupont » est celle d'un AUTRE routeur gere :
    # c'est un routeur client de nas-test, connecte en PPPoE.
    mikrotik._PROPRIETAIRES["10.20.0.10"] = ("nas-child", "pppoe-out1")
    shaping = app_client.app.state.container.shaping
    assert app_client.portal is not None
    try:
        pourquoi = app_client.portal.call(shaping.why_no_queue, "dupont")
        assert pourquoi["code"] == "no_rate", pourquoi
        r = app_client.put(
            "/api/v1/plans/dupont", headers=cle, json={"down_mbps": 100, "up_mbps": 100}
        )
        assert r.status_code == 200, r.text
        file = _file(routeur, "10.20.0.10")
        assert file is not None, routeur.simple_queue_rows
        assert file["max-limit"] == "100000000/100000000"
    finally:
        mikrotik._PROPRIETAIRES.pop("10.20.0.10", None)


def test_la_carte_des_limites_montre_les_clients_avant_les_liens() -> None:
    """Les 25 premiers points seulement sont montres : les dizaines de liens
    sans file cachaient la ligne du client cherche."""
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function ordrePoint(p)" in js
    assert "r.points.slice().sort((a, b) => ordrePoint(a) - ordrePoint(b))" in js


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_la_file_va_la_ou_est_la_session_quel_que_soit_le_site(app_client, routeur) -> None:  # noqa: F811
    """Constate : session ouverte sur un routeur, client range dans un AUTRE
    site -> aucun routeur ne posait sa file. Son forfait etait affiche, jamais
    ecrit (« 0 command »)."""
    import asyncio

    import asyncpg

    ecriture, _ = _cles(app_client)
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

    async def ranger_ailleurs() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            pop = await conn.fetchval(
                "INSERT INTO pops (name) VALUES ('Autre site') "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            )
            await conn.execute("UPDATE subscribers SET pop_id = $1 WHERE login = 'dupont'", pop)
        finally:
            await conn.close()

    asyncio.run(ranger_ailleurs())
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 100, "up_mbps": 100})
    assert r.status_code == 200, r.text
    # POSEE TOUT DE SUITE, sur le routeur de la session.
    assert r.json()["enforcement"]["applied"] >= 1, r.json()["enforcement"]
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "100000000/100000000"
    # Et le passage automatique la garde (il envoyait « 0 command »).
    routeur.simple_queue_rows.remove(file)
    r = app_client.post("/api/v1/jobs/reconcile_shaping/run", headers=cle)
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows

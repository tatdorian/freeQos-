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


def test_un_ecart_de_lien_n_est_pas_celui_du_client_homonyme() -> None:
    from app.enforcement.planner import LinkTarget

    lien = LinkTarget(
        name="ACME OFFICE",
        interface="ether3",
        subnet="10.10.11.0/24",
        measured_capacity_mbps=100,
    )
    client = _client("ACME OFFICE", "198.51.100.77")
    _types, files, ecartes = desired_state(links=[lien], subscribers=[client], link_queues=False)
    assert [f.target for f in files] == ["198.51.100.77/32"]
    (ecart,) = ecartes
    assert ecart.kind == "link" and ecart.login == "ACME OFFICE"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_bout_en_bout_un_lien_homonyme_n_empeche_pas_la_file(app_client, routeur) -> None:  # noqa: F811
    """CONSTATE : « skipped -- link queues are off » sur le mode d'un client.
    L'equipement d'en face portait le nom du client : le lien, ecarte, avait le
    meme nom, et la pose immediate prenait son ecart pour celui du client."""
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
    depot = app_client.app.state.container.shaping.repository
    vrais_liens = depot.links

    async def liens_avec_homonyme() -> list[dict]:
        return [
            *await vrais_liens(),
            {
                "key": "mac:AA:BB:CC:00:00:01",
                "discovered_by": "nas-test",
                "interface": "ether2",
                "target_name": "dupont",
                "target_key": "mac:AA:BB:CC:00:00:01",
                "capacity_mbps": 100,
                "attributes": {},
            },
        ]

    depot.links = liens_avec_homonyme
    try:
        r = app_client.put(
            "/api/v1/plans/dupont", headers=cle, json={"down_mbps": 200, "up_mbps": 200}
        )
        assert r.status_code == 200, r.text
        rapport = r.json()["enforcement"]
        assert rapport["applied"] >= 1, rapport
        file = _file(routeur, "10.20.0.10")
        assert file is not None, routeur.simple_queue_rows
        assert file["max-limit"] == "200000000/200000000"
    finally:
        depot.links = vrais_liens


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_pas_de_faux_succes_quand_le_routeur_ne_porte_pas_le_client(app_client, routeur) -> None:  # noqa: F811
    """CONSTATE : « applied » dans l'activite, et aucune file sur le routeur. Le
    routeur ne portait pas le client : le plan n'avait rien a ecrire, et la pose
    repondait « queue already correct »."""
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
    import asyncio

    import asyncpg

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

    # Ni session sur ce routeur, ni site de ce routeur : il ne le porte pas.
    asyncio.run(ranger_ailleurs())
    routeur.active.clear()
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 50, "up_mbps": 10})
    assert r.status_code == 200, r.text
    rapport = r.json()["enforcement"]
    assert rapport["applied"] == 0
    assert rapport["state"] == "absent", rapport
    assert "is not carried by nas-test" in rapport["reason"]


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_sans_session_a_son_nom_la_file_se_pose_par_son_ip(app_client, routeur) -> None:  # noqa: F811
    """COMME PAR L'API : un client du site de ce routeur, sans session PPPoE a
    son nom, recoit sa file par son adresse IP mesuree recemment."""
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
    # Plus de session au nom du client sur le routeur ; son adresse est connue.
    routeur.active.clear()
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 5})
    assert r.status_code == 200, r.text
    rapport = r.json()["enforcement"]
    assert rapport["applied"] >= 1, rapport
    file = _file(routeur, "10.20.0.10")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "5000000/20000000"


async def test_le_site_d_un_routeur_est_toujours_servi_par_ce_routeur(settings) -> None:  # noqa: F811
    """CONSTATE : le referentiel attribuait le site du PoP NAS-SOUTH au routeur
    PARENT (un de ses VLAN reconnu comme ce site). NAS-SOUTH n'etait jamais
    retenu pour son propre site : la file de ses clients ne partait que vers le
    parent, en lecture seule -- donc nulle part."""
    from tests.conftest import FakeRouterOsClient
    from tests.test_shaping_service import make_service

    pop = settings.routers[0].effective_pop_name
    parent = settings.routers[0].model_copy(
        update={"name": "nas-parent", "pop_name": "Parent", "host": "192.0.2.99"}
    )
    settings.routers.append(parent)
    service = make_service(settings, FakeRouterOsClient())
    await service.registry.reload()

    async def referentiel() -> list[dict]:
        return [{"name": pop, "router_name": "nas-parent", "vlan_interface": "vlan9", "vlan_id": 9}]

    service._pop_sites = referentiel  # type: ignore[method-assign]  # noqa: SLF001
    routeurs = await service._routers_for_site(pop)  # noqa: SLF001
    assert routeurs[0] == settings.routers[0].name
    assert "nas-parent" in routeurs


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_le_diagnostic_d_un_client_dit_tout_routeur_par_routeur(app_client, routeur) -> None:  # noqa: F811
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
    d = app_client.get("/api/v1/shaping/explain/dupont", headers=cle).json()
    assert d["subscriber"]["kind"] == "pppoe"
    assert d["ip"] == "10.20.0.10"
    (r0,) = d["routers"]
    assert r0["router"] == "nas-test" and r0["client_site_matches"] is True
    assert r0["session_with_this_login"]["address"] == "10.20.0.10"
    assert r0["targets_for_this_client"][0]["login"] == "dupont"
    # Lecture seule : rien n'a ete ecrit.
    assert _file(routeur, "10.20.0.10") is None


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_fiche_ip_fixe_sur_l_ip_d_une_session_retiree(app_client, routeur) -> None:  # noqa: F811
    """CONSTATE (diagnostic de production) : la fiche a IP fixe « ACME
    OFFICE » portait l'IP de la session PPPoE SESS-0042, retiree de freeQoS. La
    fiche etait fusionnee dans la session et disparaissait avec elle : son mode
    200/200 n'etait jamais pose, nulle part."""
    routeur.add_session("sess-0042", address="10.20.0.50")
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
    # La session est retiree de freeQoS (elle reste sur le routeur).
    r = app_client.post(
        "/api/v1/clients/remove", headers=cle, json={"logins": ["sess-0042"], "confirm": True}
    )
    assert r.status_code == 200, r.text
    # La fiche a IP fixe, poussee par l'API sur la meme IP.
    r = app_client.put(
        "/model/v1/services/office",
        headers=cle,
        json={
            "id": "office",
            "account": "office",
            "down_speed": 300000,
            "up_speed": 300000,
            "attachments": [{"network_prefixes": ["10.20.0.50"]}],
        },
    )
    assert r.status_code == 200, r.text
    # Le mode « test » 200/200, pose sur la fiche.
    r = app_client.put(
        "/api/v1/shaping/policies",
        headers=cle,
        json={
            "scope": "subscriber",
            "target_key": "office",
            "max_down_mbps": 200,
            "max_up_mbps": 200,
        },
    )
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.50")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "200000000/200000000"
    assert file["name"] == "freeqos-office"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_fiche_ip_fixe_fusionnee_dans_une_session_gardee_son_mode_s_applique(
    app_client,  # noqa: F811
    routeur,  # noqa: F811
) -> None:
    """La session est gardee : la fiche se fond dans sa file (comme avant), et le
    mode pose sur la FICHE s'applique a cette file."""
    routeur.add_session("sess-0042", address="10.20.0.50")
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
    r = app_client.put(
        "/model/v1/services/office",
        headers=cle,
        json={
            "id": "office",
            "account": "office",
            "down_speed": 300000,
            "up_speed": 300000,
            "attachments": [{"network_prefixes": ["10.20.0.50"]}],
        },
    )
    assert r.status_code == 200, r.text
    r = app_client.put(
        "/api/v1/shaping/policies",
        headers=cle,
        json={
            "scope": "subscriber",
            "target_key": "office",
            "max_down_mbps": 200,
            "max_up_mbps": 200,
        },
    )
    assert r.status_code == 200, r.text
    file = _file(routeur, "10.20.0.50")
    assert file is not None, routeur.simple_queue_rows
    assert file["max-limit"] == "200000000/200000000"
    assert file["name"] == "freeqos-sess-0042"

"""Une application externe pose limites, modes, boosts et horaires PAR L'API.

Uniquement avec une cle d'API (aucune session, comme un logiciel de
facturation ou un script), sur une vraie base et un faux routeur qui retient
chaque ecriture. Chaque geste est verifie sur la FILE REELLEMENT POSEE.

CE QUE CES TESTS FIXENT
-----------------------
1. FORFAIT : ``PUT /api/v1/plans/{login}`` pose la file ; ``DELETE`` la retire.
2. LIMITE FORCEE : ``PUT /api/v1/shaping/policies`` (en Mbps ou en kbps) passe
   avant le forfait ; la retirer rend le forfait.
3. MODES : creer (``POST /bandwidth-modes``), donner a UN client
   (``PUT /clients/{login}/mode``) ou a PLUSIEURS (``POST /clients/actions``),
   rendre a Automatic ; changer le debit d'un mode suit tout de suite.
4. BOOSTS : multiplier le debit, ou un forfait temporaire ; l'arreter rend le
   debit d'avant.
5. HORAIRES : ``POST /bandwidth-schedules`` pour des clients nommes, applique
   tout de suite quand la plage est en cours.
6. CLIENT A IP FIXE : ``POST /static-clients`` avec son forfait pose sa file.
7. CLE EN LECTURE SEULE : lit tout, n'ecrit rien (403), et rien n'est pose.
8. API COMPATIBLE PRESEEM : ``PUT /model/v1/services/{id}`` pose le forfait.

Ignore sans ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    RouteurQuiRetient,
    _attendre,
    _cles,
    app_client,
    pytestmark,
    routeur,
)


def _brancher(client: TestClient, cle: dict[str, str]) -> None:
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
            "write_enabled": True,
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


def _file(routeur: RouteurQuiRetient, nom: str) -> dict[str, Any] | None:  # noqa: F811
    return next((q for q in routeur.simple_queue_rows if q.get("name") == nom), None)


def _limite(routeur: RouteurQuiRetient, login: str = "dupont") -> str | None:  # noqa: F811
    q = _file(routeur, login)
    return None if q is None else q.get("max-limit")


def _ok(r: Any) -> Any:
    assert r.status_code in (200, 201, 204), f"{r.request.method} {r.request.url.path}: {r.text}"
    return r.json() if r.content else None


def test_forfait_limite_forcee_et_retour(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)

    _ok(app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10}))
    assert _limite(routeur) == "10000000/20000000"

    # Une limite forcee, en kbps : elle passe AVANT le forfait.
    _ok(
        app_client.put(
            "/api/v1/shaping/policies",
            headers=cle,
            json={
                "scope": "subscriber",
                "target_key": "dupont",
                "max_down_kbps": 512,
                "max_up_kbps": 256,
            },
        )
    )
    assert _limite(routeur) == "256000/512000"

    _ok(app_client.delete("/api/v1/shaping/policies/subscriber/dupont", headers=cle))
    assert _limite(routeur) == "10000000/20000000"

    _ok(app_client.delete("/api/v1/plans/dupont", headers=cle))
    # Le banc a une limite par defaut (100/20) : sans forfait, c'est elle.
    assert _limite(routeur) in (None, "20000000/100000000")


def test_modes_un_client_plusieurs_clients_et_retour(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    _ok(app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10}))

    fixe = _ok(
        app_client.post(
            "/api/v1/bandwidth-modes",
            headers=cle,
            json={"name": "Backup", "down_value": 6, "up_value": 6},
        )
    )
    part = _ok(
        app_client.post(
            "/api/v1/bandwidth-modes",
            headers=cle,
            json={"name": "Evening", "down_value": 10, "up_value": 5},
        )
    )
    modes = {
        m["name"] for m in _ok(app_client.get("/api/v1/bandwidth-modes", headers=cle))["modes"]
    }
    assert {"Backup", "Evening"} <= modes

    # Un client.
    _ok(app_client.put("/api/v1/clients/dupont/mode", headers=cle, json={"mode_id": fixe["id"]}))
    assert _limite(routeur) == "6000000/6000000"

    # Le debit du mode change : le client suit tout de suite.
    _ok(
        app_client.patch(
            f"/api/v1/bandwidth-modes/{fixe['id']}", headers=cle, json={"down_value": 8}
        )
    )
    assert _limite(routeur) == "6000000/8000000"

    # Plusieurs clients d'un coup (ici un seul en ligne).
    rapport = _ok(
        app_client.post(
            "/api/v1/clients/actions",
            headers=cle,
            json={"logins": ["dupont"], "action": "mode", "mode_id": part["id"]},
        )
    )
    assert rapport["succeeded"] == 1, rapport
    assert _limite(routeur) == "5000000/10000000"

    # Retour a Automatic : le forfait.
    _ok(
        app_client.post(
            "/api/v1/clients/actions",
            headers=cle,
            json={"logins": ["dupont"], "action": "mode", "mode_id": None},
        )
    )
    assert _limite(routeur) == "10000000/20000000"


def test_boosts_par_l_api(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    _ok(app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10}))

    # Multiplier le debit en vigueur.
    _ok(
        app_client.post(
            "/api/v1/clients/actions",
            headers=cle,
            json={
                "logins": ["dupont"],
                "action": "boost",
                "boost_multiplier": 2,
                "boost_minutes": 30,
            },
        )
    )
    assert _limite(routeur) == "20000000/40000000"
    _ok(
        app_client.post(
            "/api/v1/clients/actions",
            headers=cle,
            json={"logins": ["dupont"], "action": "stop_boost"},
        )
    )
    assert _limite(routeur) == "10000000/20000000"

    # Un forfait temporaire par la route dediee.
    _ok(
        app_client.post(
            "/api/v1/shaping/boosts",
            headers=cle,
            json={"login": "dupont", "down_mbps": 50, "up_mbps": 25, "duration_minutes": 60},
        )
    )
    assert _limite(routeur) == "25000000/50000000"
    actifs = _ok(app_client.get("/api/v1/shaping/boosts", headers=cle))
    assert "dupont" in str(actifs)
    _ok(app_client.delete("/api/v1/shaping/boosts/dupont", headers=cle))
    assert _limite(routeur) == "10000000/20000000"


def test_un_horaire_par_l_api_s_applique_tout_de_suite(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    _ok(app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10}))
    horaire = _ok(
        app_client.post(
            "/api/v1/bandwidth-schedules",
            headers=cle,
            json={
                "name": "All day test",
                "timezone": "Africa/Niamey",
                "scope": "subscribers",
                "targets": ["dupont"],
                "windows": [{"start": "00:00", "end": "24:00", "down_mbps": 3, "up_mbps": 1}],
            },
        )
    )
    assert _limite(routeur) == "1000000/3000000"
    apercu = _ok(
        app_client.get(f"/api/v1/bandwidth-schedules/{horaire['id']}/preview", headers=cle)
    )
    assert "dupont" in str(apercu)
    _ok(app_client.delete(f"/api/v1/bandwidth-schedules/{horaire['id']}", headers=cle))
    assert _limite(routeur) == "10000000/20000000"


def test_un_client_a_ip_fixe_par_l_api(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    fiche = _ok(
        app_client.post(
            "/api/v1/static-clients",
            headers=cle,
            json={
                "reference": "b2b-bank",
                "pop_name": "PoP Test",
                "address": "10.20.0.200/30",
                "plan_down_mbps": 50,
                "plan_up_mbps": 50,
            },
        )
    )
    file = _file(routeur, "b2b-bank")
    assert file is not None, routeur.simple_queue_rows
    assert file["target"] == "10.20.0.200/30"
    assert file["max-limit"] == "50000000/50000000"
    # Le forfait change par l'API : la file suit.
    _ok(
        app_client.patch(
            f"/api/v1/static-clients/{fiche['id']}",
            headers=cle,
            json={"plan_down_mbps": 100},
        )
    )
    assert _file(routeur, "b2b-bank")["max-limit"] == "50000000/100000000"


def test_une_cle_en_lecture_seule_ne_pose_rien(app_client: TestClient, routeur) -> None:  # noqa: F811
    ecriture, lecture = _cles(app_client)
    _brancher(app_client, {"Authorization": f"Bearer {ecriture}"})
    avant = list(routeur.simple_queue_rows)
    lire = {"Authorization": f"Bearer {lecture}"}
    assert app_client.get("/api/v1/plans", headers=lire).status_code == 200
    assert app_client.get("/api/v1/bandwidth-modes", headers=lire).status_code == 200
    gestes = [
        ("PUT", "/api/v1/plans/dupont", {"down_mbps": 1, "up_mbps": 1}),
        ("POST", "/api/v1/bandwidth-modes", {"name": "X", "down_value": 1, "up_value": 1}),
        ("POST", "/api/v1/clients/actions", {"logins": ["dupont"], "action": "boost"}),
        ("PUT", "/api/v1/clients/dupont/mode", {"mode_id": None}),
    ]
    for methode, chemin, corps in gestes:
        r = app_client.request(methode, chemin, headers=lire, json=corps)
        assert r.status_code == 403, (chemin, r.status_code, r.text)
    assert routeur.simple_queue_rows == avant


def test_l_api_compatible_preseem_pose_le_forfait(app_client: TestClient, routeur) -> None:  # noqa: F811
    """Un logiciel de facturation qui parlait a Preseem : le service est rattache
    au client par son adresse (``network_prefixes``), debits en kbps. Ici
    l'adresse est celle de la session PPPoE de dupont : sa file prend le debit."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.put(
        "/model/v1/services/svc-dupont",
        headers=cle,
        json={
            "id": "svc-dupont",
            "account": "acc-1",
            "down_speed": 20000,
            "up_speed": 10000,
            "attachments": [{"network_prefixes": ["10.20.0.10"]}],
        },
    )
    assert r.status_code in (200, 201, 204), r.text

    def limite_sur_l_adresse() -> str | None:
        for q in routeur.simple_queue_rows:
            if str(q.get("target", "")).startswith("10.20.0.10"):
                return q.get("max-limit")
        return None

    _attendre(lambda: limite_sur_l_adresse() == "10000000/20000000", 5)
    # Le debit change cote facturation : la file suit.
    r = app_client.put(
        "/model/v1/services/svc-dupont",
        headers=cle,
        json={
            "id": "svc-dupont",
            "account": "acc-1",
            "down_speed": 40000,
            "up_speed": 10000,
            "attachments": [{"network_prefixes": ["10.20.0.10"]}],
        },
    )
    assert r.status_code in (200, 201, 204), r.text
    _attendre(lambda: limite_sur_l_adresse() == "10000000/40000000", 5)

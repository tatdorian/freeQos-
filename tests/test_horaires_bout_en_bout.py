"""Le debit qui change avec l'heure, pour les clients choisis : de bout en bout.

Vraie base, faux routeur qui retient chaque ecriture, horloge pilotee. On cree
un horaire comme dans l'interface (Schedules), pour DES CLIENTS NOMMES, puis on
fait passer le temps et on lit la file REELLEMENT posee sur le routeur.

CE QUE CES TESTS FIXENT
-----------------------
1. A L'HEURE DITE, tout seul : la plage commence, la file des clients vises
   prend le debit de la plage ; elle finit, la file revient au forfait.
2. SEULS LES CLIENTS CHOISIS changent : un client hors de l'horaire garde son
   forfait.
3. UNE PLAGE QUI PASSE MINUIT (23:00 -> 06:00) et UN FUSEAU (Africa/Niamey,
   UTC+1) : « 18:00 » est l'heure des abonnes, pas celle du serveur.
4. LES JOURS : une plage du lundi au vendredi ne joue pas le samedi.
5. UNE DECISION SUR UN CLIENT PRIME : un client mis dans un mode a la main
   garde son mode pendant la plage ; un boost passe par-dessus tout.
6. UN CLIENT SANS FORFAIT : une plage en Mbps lui pose sa file pendant la
   plage, et la file PART des que la plage finit -- tout de suite, pas au
   menage suivant.
7. SUSPENDRE OU SUPPRIMER L'HORAIRE rend le forfait tout de suite.

Ignore sans ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.test_bout_en_bout_api_externe import (
    DSN,
    RouteurQuiRetient,
    _cles,
    _ecrivain,
    pytestmark,  # noqa: F401 - ignore sans base
)

NIAMEY = ZoneInfo("Africa/Niamey")  # UTC+1, sans heure d'ete
#: Lundi 16 novembre 2026 (un lundi), heure de Niamey.
LUNDI = datetime(2026, 11, 16, tzinfo=NIAMEY)

CLIENTS = {
    "client-a": "10.20.0.10",  # vise, forfait 20/10
    "client-b": "10.20.0.11",  # vise, forfait 20/10, mis en mode a la main
    "client-c": "10.20.0.12",  # NON vise, forfait 20/10
    "client-d": "10.20.0.13",  # vise, SANS forfait
}


class Horloge:
    """Une seule heure pour tout le controleur, que le test avance."""

    instant = LUNDI.astimezone(UTC)


class HeurePilotee(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        if tz is None:
            return Horloge.instant.replace(tzinfo=None)
        return Horloge.instant.astimezone(tz)


def regler(jour: datetime, heure: str) -> None:
    h, m = (int(x) for x in heure.split(":"))
    Horloge.instant = jour.replace(hour=h, minute=m, second=30).astimezone(UTC)


@pytest.fixture
def banc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    import asyncpg

    import app.api.schedules as api_horaires
    import app.api.shaping as api_shaping
    import app.collectors.mikrotik as mk
    import app.enforcement.planner as planner
    import app.enforcement.schedules as horaires
    import app.services.shaping as sh
    from app.main import create_app

    async def vider() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            tables = [
                t
                for t in (
                    "routers, pops, subscribers, subscriber_metrics, client_plans, "
                    "shaping_policies, enforcement_audit, static_clients, api_keys, app_users, "
                    "app_sessions, topology_nodes, topology_links, bandwidth_modes, "
                    "bandwidth_schedules, client_activity, runtime_flags, runtime_settings, "
                    "owned_queues"
                ).split(", ")
                if await conn.fetchval("SELECT to_regclass($1)", t) is not None
            ]
            if tables:
                await conn.execute(
                    f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"  # noqa: S608
                )
        finally:
            await conn.close()

    asyncio.run(vider())
    regler(LUNDI, "12:00")
    for module in (sh, planner, horaires, api_horaires, api_shaping):
        monkeypatch.setattr(module, "datetime", HeurePilotee)

    routeur = RouteurQuiRetient(identity="nas-pop")
    routeur.grant_write("qos")
    for login, adresse in CLIENTS.items():
        routeur.add_session(login, address=adresse)
    routeur.interfaces_rows.append(
        {
            ".id": "*e1",
            "name": "ether1",
            "type": "ether",
            "running": "true",
            "rx-byte": "0",
            "tx-byte": "0",
        }
    )
    routeur.address_rows = [
        {"address": "10.20.0.1/24", "interface": "ether1", "network": "10.20.0.0"}
    ]
    monkeypatch.setattr(mk, "LibrouterosReadClient", lambda *_a, **_k: routeur)
    monkeypatch.setattr(sh, "LibrouterosWriteClient", _ecrivain(routeur))
    reglages = Settings(
        _env_file=None,
        database_url=DSN,
        auth_enabled=True,
        scheduler_enabled=False,
        netflow_enabled=False,
        rtt_enabled=False,
        enforcement_enabled=True,
        routers=[],
        routers_file=None,
        app_secret_key_file=tmp_path / "secret.key",
    )
    with TestClient(create_app(reglages)) as client:
        yield client, routeur
    asyncio.run(vider())


def _cle(client: TestClient) -> dict[str, str]:
    ecriture, _ = _cles(client)
    return {"Authorization": f"Bearer {ecriture}"}


def _brancher(client: TestClient, cle: dict[str, str]) -> None:
    import time

    r = client.post(
        "/api/v1/pops/routers",
        headers=cle,
        json={
            "name": "nas-pop",
            "host": "192.0.2.10",
            "username": "qos",
            "password": "secret-routeur",
            "role": "pop",
            "pop_name": "PoP Test",
            "write_enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    fin = time.monotonic() + 25
    while time.monotonic() < fin:
        if client.get("/api/v1/pops/provisioning/nas-pop", headers=cle).json().get("finished_at"):
            break
        time.sleep(0.2)
    assert client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    for login in ("client-a", "client-b", "client-c"):
        r = client.put(f"/api/v1/plans/{login}", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
        assert r.status_code == 200, r.text


def _limites(routeur: RouteurQuiRetient) -> dict[str, str | None]:
    """La file posee par freeQoS pour chaque client : max-limit (montant/descendant)."""
    sortie: dict[str, str | None] = dict.fromkeys(CLIENTS)
    for q in routeur.simple_queue_rows:
        for login in CLIENTS:
            if q.get("name") == login:
                sortie[login] = q.get("max-limit")
    return sortie


def _passe(client: TestClient, cle: dict[str, str], jour: datetime, heure: str) -> None:
    """Le temps passe : le job des horaires (toutes les 30 s) tourne."""
    regler(jour, heure)
    r = client.post("/api/v1/jobs/bandwidth_schedules/run", headers=cle)
    assert r.status_code == 200, r.text


def _creer_horaire(client: TestClient, cle: dict[str, str], **extra: Any) -> int:
    corps = {
        "name": "Evening and night",
        "timezone": "Africa/Niamey",
        "scope": "subscribers",
        "targets": ["client-a", "client-b", "client-d"],
        "windows": [
            {"label": "evening", "start": "18:00", "end": "23:00", "down_mbps": 5, "up_mbps": 2},
            {"label": "night", "start": "23:00", "end": "06:00", "percent": 200},
        ],
        **extra,
    }
    r = client.post("/api/v1/bandwidth-schedules", headers=cle, json=corps)
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


PLAN = "10000000/20000000"
SOIR = "2000000/5000000"
NUIT = "20000000/40000000"


def test_une_journee_complete(banc: tuple[TestClient, RouteurQuiRetient]) -> None:
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    _creer_horaire(client, cle)
    # Le premier passage du job prend ses reperes.
    _passe(client, cle, LUNDI, "12:00")
    assert _limites(routeur) == {
        "client-a": PLAN,
        "client-b": PLAN,
        "client-c": PLAN,
        "client-d": None,  # sans forfait, hors plage : aucune file
    }

    _passe(client, cle, LUNDI, "17:59")
    assert _limites(routeur)["client-a"] == PLAN

    # 18:00 heure de Niamey (17:00 UTC) : la soiree commence, tout seul.
    _passe(client, cle, LUNDI, "18:00")
    assert _limites(routeur) == {
        "client-a": SOIR,
        "client-b": SOIR,
        "client-c": PLAN,  # pas dans l'horaire : intact
        "client-d": SOIR,  # sans forfait : la plage en Mbps lui pose sa file
    }

    # 23:00 : la nuit (200 % du forfait) remplace la soiree.
    _passe(client, cle, LUNDI, "23:00")
    limites = _limites(routeur)
    assert limites["client-a"] == NUIT
    assert limites["client-c"] == PLAN
    # 200 % de rien : client-d n'a plus de debit -> sa file part TOUT DE SUITE.
    assert limites["client-d"] is None, limites

    # Passe minuit : toujours la nuit.
    _passe(client, cle, LUNDI.replace(day=17), "02:00")
    assert _limites(routeur)["client-a"] == NUIT

    # 06:00 le lendemain : retour au forfait.
    _passe(client, cle, LUNDI.replace(day=17), "06:00")
    assert _limites(routeur) == {
        "client-a": PLAN,
        "client-b": PLAN,
        "client-c": PLAN,
        "client-d": None,
    }


def test_un_scheduler_s_applique_a_la_vitesse_du_mode(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    """REGLE DES SCHEDULERS : le pourcentage s'applique a la vitesse du client,
    celle de son MODE s'il en a un. Backup (6/6) a 200 % la nuit = 12/12."""
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    backup = client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Backup", "down_value": 6, "up_value": 6},
    ).json()
    r = client.put("/api/v1/clients/client-b/mode", headers=cle, json={"mode_id": backup["id"]})
    assert r.status_code == 200, r.text
    _creer_horaire(client, cle)
    _passe(client, cle, LUNDI, "12:00")
    assert _limites(routeur)["client-b"] == "6000000/6000000"  # hors plage : son mode
    _passe(client, cle, LUNDI, "23:30")
    limites = _limites(routeur)
    assert limites["client-a"] == NUIT
    assert limites["client-b"] == "12000000/12000000"  # 200 % de son mode

    # Rendu a son plan pendant la plage : 200 % de son plan.
    auto = client.put("/api/v1/clients/client-b/mode", headers=cle, json={"mode_id": None})
    assert auto.status_code == 200, auto.text
    assert _limites(routeur)["client-b"] == NUIT


def test_les_jours_choisis_seulement(banc: tuple[TestClient, RouteurQuiRetient]) -> None:
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    jours = ["mon", "tue", "wed", "thu", "fri"]
    r = client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={
            "name": "Weekday evening",
            "timezone": "Africa/Niamey",
            "scope": "subscribers",
            "targets": ["client-a"],
            "windows": [
                {"start": "18:00", "end": "23:00", "down_mbps": 5, "up_mbps": 2, "days": jours}
            ],
        },
    )
    assert r.status_code == 201, r.text
    samedi = LUNDI.replace(day=21)
    _passe(client, cle, samedi, "12:00")
    _passe(client, cle, samedi, "18:00")
    assert _limites(routeur)["client-a"] == PLAN
    lundi_suivant = LUNDI.replace(day=23)
    _passe(client, cle, lundi_suivant, "18:00")
    assert _limites(routeur)["client-a"] == SOIR


def test_suspendre_puis_supprimer_rend_le_forfait_tout_de_suite(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    ident = _creer_horaire(client, cle)
    _passe(client, cle, LUNDI, "12:00")
    _passe(client, cle, LUNDI, "18:00")
    assert _limites(routeur)["client-a"] == SOIR

    r = client.patch(f"/api/v1/bandwidth-schedules/{ident}", headers=cle, json={"enabled": False})
    assert r.status_code == 200, r.text
    limites = _limites(routeur)
    assert limites["client-a"] == PLAN
    assert limites["client-d"] is None  # sa file de soiree part avec l'horaire

    r = client.patch(f"/api/v1/bandwidth-schedules/{ident}", headers=cle, json={"enabled": True})
    assert r.status_code == 200, r.text
    assert _limites(routeur)["client-a"] == SOIR

    assert client.delete(f"/api/v1/bandwidth-schedules/{ident}", headers=cle).status_code in (
        200,
        204,
    )
    limites = _limites(routeur)
    assert limites["client-a"] == PLAN
    assert limites["client-d"] is None


def test_un_boost_passe_par_dessus_l_horaire(banc: tuple[TestClient, RouteurQuiRetient]) -> None:
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    _creer_horaire(client, cle)
    _passe(client, cle, LUNDI, "12:00")
    _passe(client, cle, LUNDI, "18:00")
    assert _limites(routeur)["client-a"] == SOIR
    r = client.post(
        "/api/v1/shaping/boosts",
        headers=cle,
        json={"login": "client-a", "down_mbps": 50, "up_mbps": 20, "duration_minutes": 60},
    )
    assert r.status_code in (200, 201), r.text
    assert _limites(routeur)["client-a"] == "20000000/50000000"


def test_un_client_sans_forfait_rendu_a_automatic_perd_sa_file_tout_de_suite(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    """Le test a paliers : on met des clients SANS forfait dans un mode en Mbps,
    puis on les rend a Automatic -- leur file doit partir sur-le-champ."""
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    test = client.post(
        "/api/v1/bandwidth-modes",
        headers=cle,
        json={"name": "Test 10 Mbps", "down_value": 10, "up_value": 10},
    ).json()
    r = client.put("/api/v1/clients/client-d/mode", headers=cle, json={"mode_id": test["id"]})
    assert r.status_code == 200, r.text
    assert _limites(routeur)["client-d"] == "10000000/10000000"
    r = client.put("/api/v1/clients/client-d/mode", headers=cle, json={"mode_id": None})
    assert r.status_code == 200, r.text
    assert _limites(routeur)["client-d"] is None
    # Les clients avec forfait n'ont pas bouge.
    assert _limites(routeur)["client-a"] == PLAN


def test_une_purge_coupee_ne_retire_que_les_files_sans_debit() -> None:
    """Le retrait cible ne vide jamais un routeur : seules les files nommees
    (clients sans debit) partent ; une file d'un client simplement absent de la
    liste des sessions reste."""
    from app.enforcement.models import MANAGED_COMMENT
    from app.enforcement.planner import build_plan

    files = [
        {
            ".id": "*1",
            "name": "freeqos-sans-debit",
            "target": "10.0.0.1/32",
            "max-limit": "1M/1M",
            "comment": MANAGED_COMMENT,
        },
        {
            ".id": "*2",
            "name": "freeqos-hors-ligne",
            "target": "10.0.0.2/32",
            "max-limit": "1M/1M",
            "comment": MANAGED_COMMENT,
        },
        {".id": "*3", "name": "a-la-main", "target": "10.0.0.3/32", "max-limit": "1M/1M"},
    ]
    plan = build_plan(
        "r",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=files,
        prune=False,
        remove_names={"freeqos-sans-debit", "a-la-main"},
    )
    assert [a.name for a in plan.actions if a.verb == "remove"] == ["freeqos-sans-debit"]


def test_un_scheduler_se_lance_et_s_arrete_a_la_main(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    """« Start now » : a midi, le scheduler du soir s'applique tout de suite ;
    « Stop » rend le plan ; lance pour une heure, il s'arrete tout seul."""
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    r = client.post(
        "/api/v1/bandwidth-schedules",
        headers=cle,
        json={
            "name": "Evening",
            "timezone": "Africa/Niamey",
            "scope": "on_plan",
            "windows": [{"start": "18:00", "end": "23:00", "percent": 50}],
        },
    )
    assert r.status_code == 201, r.text
    ident = r.json()["id"]
    _passe(client, cle, LUNDI, "12:00")
    assert _limites(routeur)["client-a"] == PLAN

    r = client.post(f"/api/v1/bandwidth-schedules/{ident}/start", headers=cle, json={})
    assert r.status_code == 200, r.text
    assert r.json()["manual_active"] is True
    assert _limites(routeur)["client-a"] == "5000000/10000000"  # 50 % de 20/10

    r = client.post(f"/api/v1/bandwidth-schedules/{ident}/stop", headers=cle)
    assert r.status_code == 200, r.text
    assert _limites(routeur)["client-a"] == PLAN

    r = client.post(f"/api/v1/bandwidth-schedules/{ident}/start", headers=cle, json={"minutes": 60})
    assert r.status_code == 200, r.text
    assert _limites(routeur)["client-a"] == "5000000/10000000"
    _passe(client, cle, LUNDI, "13:30")
    assert _limites(routeur)["client-a"] == PLAN


def test_lancer_un_scheduler_arrete_celui_lance_sur_les_memes_clients(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    ids = {}
    for nom, heures, pct in (
        ("Evening", ("18:00", "23:00"), 50),
        ("Night", ("23:00", "06:00"), 200),
    ):
        r = client.post(
            "/api/v1/bandwidth-schedules",
            headers=cle,
            json={
                "name": nom,
                "timezone": "Africa/Niamey",
                "scope": "on_plan",
                "windows": [{"start": heures[0], "end": heures[1], "percent": pct}],
            },
        )
        assert r.status_code == 201, r.text
        ids[nom] = r.json()["id"]
    _passe(client, cle, LUNDI, "12:00")
    client.post(f"/api/v1/bandwidth-schedules/{ids['Evening']}/start", headers=cle, json={})
    r = client.post(f"/api/v1/bandwidth-schedules/{ids['Night']}/start", headers=cle, json={})
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] == ["Evening"]
    assert _limites(routeur)["client-a"] == NUIT


def test_le_scheduler_propre_au_client_passe_devant_le_groupe(
    banc: tuple[TestClient, RouteurQuiRetient],
) -> None:
    """« Evening » a 50 % pour tous les clients a leur plan, et pour client-a
    SON scheduler a 100 % aux memes heures : client-a garde sa vitesse, les
    autres passent a 50 %. Le chevauchement est permis (propre contre groupe)."""
    client, routeur = banc
    cle = _cle(client)
    _brancher(client, cle)
    for corps in (
        {
            "name": "Evening",
            "scope": "on_plan",
            "windows": [{"start": "18:00", "end": "23:00", "percent": 50}],
        },
        {
            "name": "client-a · own",
            "scope": "subscribers",
            "targets": ["client-a"],
            "windows": [{"start": "18:00", "end": "23:00", "percent": 100}],
        },
    ):
        r = client.post(
            "/api/v1/bandwidth-schedules",
            headers=cle,
            json={"timezone": "Africa/Niamey", **corps},
        )
        assert r.status_code == 201, r.text
    _passe(client, cle, LUNDI, "19:00")
    limites = _limites(routeur)
    assert limites["client-a"] == PLAN
    assert limites["client-c"] == "5000000/10000000"
    ligne = next(
        x
        for x in client.get("/api/v1/subscribers/latest", headers=cle).json()
        if x["login"] == "client-a"
    )
    assert ligne["schedule_personal"] is True

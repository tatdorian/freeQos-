"""Autoriser l'ecriture et lancer « Set up » ne ralentit RIEN tant qu'aucune limite n'est donnee.

CONSTATE EN PRODUCTION : des clients se sont plaints alors qu'aucune limite
n'etait posee. Ce fichier est la preuve, de bout en bout (vraie base, faux
routeur qui retient chaque ecriture), de ce que freeQoS ecrit sur un routeur.

AVEC LES REGLAGES PAR DEFAUT, ecriture autorisee, « Set up » deroule :

1. AUCUNE FILE tant qu'aucun client n'a de forfait ni de mode -- ni sur un
   client, ni sur un lien, ni sur une interface. Aucune regle de pare-feu,
   aucun mangle, aucune liste d'adresses, aucun arbre de files. Seuls les deux
   TYPES de file CAKE existent : un type que rien n'utilise ne voit passer
   aucun paquet.
2. DECLARER UN CLIENT SANS FORFAIT n'invente aucune limite (IP fixe ou PPPoE).
3. DONNER UN FORFAIT A UN CLIENT ecrit UNE file, sur SON adresse seule, a SON
   debit. Les autres clients n'ont toujours rien.
4. RETIRER LE FORFAIT retire la file.
5. Un routeur de role Gateway (plafonds en amont actifs par defaut) ne recoit
   rien non plus tant qu'aucun client n'a de debit.
6. freeQoS ne touche jamais a FastTrack ni aux files d'un autre.

Ignore sans ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

TABLES_QUI_TOUCHENT_LE_TRAFIC = (
    "simple_queue_rows",
    "queue_tree_rows",
    "firewall_filter_rows",
    "firewall_mangle_rows",
    "firewall_address_list_rows",
)
CLIENTS = [f"client-{i:02d}" for i in range(6)]


def _routeur(identity: str) -> RouteurQuiRetient:
    r = RouteurQuiRetient(identity=identity)
    r.grant_write("qos")
    for i, login in enumerate(CLIENTS):
        r.add_session(login, address=f"10.20.0.{10 + i}")
    # Un port montant, un port client, un voisin (lien radio) : de quoi
    # decouvrir des LIENS -- qui ne doivent recevoir aucune file.
    for nom in ("ether1", "ether2"):
        r.interfaces_rows.append(
            {
                ".id": f"*{nom}",
                "name": nom,
                "type": "ether",
                "running": "true",
                "rx-byte": "0",
                "tx-byte": "0",
            }
        )
    r.address_rows = [
        {"address": "10.20.0.1/24", "interface": "ether2", "network": "10.20.0.0"},
        {"address": "192.0.2.10/24", "interface": "ether1", "network": "192.0.2.0"},
    ]
    r.neighbor_rows = [
        {
            "identity": "AP-Sector",
            "mac-address": "AA:00:00:00:00:31",
            "address": "10.20.0.2",
            "interface": "ether2",
            "platform": "MikroTik",
            "board": "LHG",
        }
    ]
    # Une file posee a la main par l'exploitant, et sa regle FastTrack.
    r.simple_queue_rows.append(
        {".id": "*M1", "name": "by-hand", "target": "10.99.0.0/24", "max-limit": "5M/5M"}
    )
    r.firewall_filter_rows.append(
        {".id": "*F1", "chain": "forward", "action": "fasttrack-connection"}
    )
    return r


@pytest.fixture
def banc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    import asyncpg

    import app.collectors.mikrotik as mk
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
                    "bandwidth_schedules, traffic_rules, runtime_flags, runtime_settings"
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
    routeurs: dict[str, RouteurQuiRetient] = {}

    def lecteur(config: Any, *_a: Any, **_k: Any) -> RouteurQuiRetient:
        nom = getattr(config, "name", None) or "nas-pop"
        return routeurs.setdefault(nom, _routeur(nom))

    class Ecrivain:
        """Chaque routeur a sa table : l'ecriture va sur celui que la fiche nomme."""

        def __init__(self, config: Any, *_a: Any, **_k: Any) -> None:
            nom = getattr(config, "name", None) or "nas-pop"
            self._vrai = _ecrivain(routeurs.setdefault(nom, _routeur(nom)))()

        def execute(self, action: Any) -> dict[str, Any]:
            return self._vrai.execute(action)

        def close(self) -> None:
            pass

    monkeypatch.setattr(mk, "LibrouterosReadClient", lecteur)
    monkeypatch.setattr(sh, "LibrouterosWriteClient", Ecrivain)
    # LES REGLAGES PAR DEFAUT, a l'ecriture pres : c'est ce qu'une installation
    # neuve fait quand on autorise l'ecriture.
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
        yield client, routeurs
    asyncio.run(vider())


def _attendre(condition: Any, secondes: float = 25.0) -> None:
    fin = time.monotonic() + secondes
    while time.monotonic() < fin:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError("delai depasse")


def _brancher(client: TestClient, cle: dict[str, str], nom: str, role: str) -> None:
    r = client.post(
        "/api/v1/pops/routers",
        headers=cle,
        json={
            "name": nom,
            "host": "192.0.2.10" if role == "pop" else "192.0.2.20",
            "username": "qos",
            "password": "secret-routeur",
            "role": role,
            "pop_name": "PoP Test",
            "write_enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    _attendre(
        lambda: bool(
            client.get(f"/api/v1/pops/provisioning/{nom}", headers=cle).json().get("finished_at")
        )
    )
    rapport = client.get(f"/api/v1/pops/provisioning/{nom}", headers=cle).json()
    # Le « Set up » a bien pu ecrire : sinon le test ne prouverait rien.
    assert rapport["blocker"] in (None, ""), rapport
    for job in ("collect_subscribers", "reconcile_shaping"):
        assert client.post(f"/api/v1/jobs/{job}/run", headers=cle).status_code == 200


def _nos_files(r: RouteurQuiRetient) -> list[dict[str, Any]]:
    return [q for q in r.simple_queue_rows if q.get("name") != "by-hand"]


def _rien_qui_touche_le_trafic(r: RouteurQuiRetient) -> None:
    assert _nos_files(r) == [], _nos_files(r)
    assert r.queue_tree_rows == []
    assert r.firewall_mangle_rows == []
    assert r.firewall_address_list_rows == []
    # Le pare-feu et la file de l'exploitant sont exactement comme avant.
    assert r.firewall_filter_rows == [
        {".id": "*F1", "chain": "forward", "action": "fasttrack-connection"}
    ]
    assert [q for q in r.simple_queue_rows if q.get("name") == "by-hand"] == [
        {".id": "*M1", "name": "by-hand", "target": "10.99.0.0/24", "max-limit": "5M/5M"}
    ]


def test_set_up_sans_aucune_limite_n_ecrit_aucune_file(
    banc: tuple[TestClient, dict[str, RouteurQuiRetient]],
) -> None:
    client, routeurs = banc
    ecriture, _ = _cles(client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(client, cle, "nas-pop", "pop")
    pop = routeurs["nas-pop"]

    # Les six clients sont bien vus...
    vus = {s["login"] for s in client.get("/api/v1/subscribers/latest", headers=cle).json()}
    assert set(CLIENTS) <= vus
    # ... et RIEN de ce qui touche le trafic n'a ete ecrit.
    _rien_qui_touche_le_trafic(pop)
    # Seuls les deux types CAKE existent, et aucune file ne les utilise.
    assert sorted(t["name"] for t in pop.queue_type_rows) == [
        "freeqos-cake-down",
        "freeqos-cake-up",
    ]
    assert all(t.get("kind") == "cake" for t in pop.queue_type_rows)

    points = client.get("/api/v1/shaping/points", headers=cle).json()["routers"][0]
    aplat: list[dict[str, Any]] = []

    def deplier(noeuds: list[dict[str, Any]]) -> None:
        for n in noeuds:
            aplat.append(n)
            deplier(n.get("children") or [])

    deplier(points["points"])
    assert aplat
    assert not [p["name"] for p in aplat if p["state"] in ("file-posee", "file-a-poser")], aplat
    # Les six clients sont ecartes pour la bonne raison, dite en clair.
    motifs = " ".join(str(p.get("reason") or "") for p in aplat)
    assert "no queue is written" in motifs

    # Plusieurs passages de la reconciliation : toujours rien.
    for _ in range(3):
        client.post("/api/v1/jobs/reconcile_shaping/run", headers=cle)
    _rien_qui_touche_le_trafic(pop)


def test_declarer_un_client_sans_forfait_n_invente_aucune_limite(
    banc: tuple[TestClient, dict[str, RouteurQuiRetient]],
) -> None:
    client, routeurs = banc
    ecriture, _ = _cles(client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(client, cle, "nas-pop", "pop")
    pop = routeurs["nas-pop"]

    # IP fixe, forfait laisse vide (le formulaire dit « optional »).
    r = client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "b2b-bank", "pop_name": "PoP Test", "address": "10.20.0.200/30"},
    )
    assert r.status_code in (200, 201), r.text
    # PPPoE declare d'avance, sans forfait.
    r = client.post(
        "/api/v1/clients", headers=cle, json={"login": "client-00", "pop_name": "PoP Test"}
    )
    assert r.status_code in (200, 201, 409), r.text
    client.post("/api/v1/jobs/reconcile_shaping/run", headers=cle)
    _rien_qui_touche_le_trafic(pop)


def test_un_forfait_ecrit_une_seule_file_sur_la_seule_adresse_du_client(
    banc: tuple[TestClient, dict[str, RouteurQuiRetient]],
) -> None:
    client, routeurs = banc
    ecriture, _ = _cles(client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(client, cle, "nas-pop", "pop")
    pop = routeurs["nas-pop"]

    r = client.put("/api/v1/plans/client-03", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code == 200, r.text
    files = _nos_files(pop)
    assert len(files) == 1, files
    # SON adresse (/32), SON debit (montant/descendant), CAKE : rien de plus large.
    assert files[0]["target"] == "10.20.0.13/32"
    assert files[0]["max-limit"] == "10000000/20000000"
    assert "parent" not in files[0] or files[0]["parent"] in ("none", "")
    # Aucun lien, aucune interface, aucun autre client.
    assert pop.queue_tree_rows == [] and pop.firewall_mangle_rows == []

    # Le forfait retire, la file part : le routeur revient a zero file freeQoS.
    assert client.delete("/api/v1/plans/client-03", headers=cle).status_code in (200, 204)
    client.post("/api/v1/jobs/reconcile_shaping/run", headers=cle)
    _rien_qui_touche_le_trafic(pop)


def test_une_passerelle_ne_recoit_rien_tant_qu_aucun_client_n_a_de_debit(
    banc: tuple[TestClient, dict[str, RouteurQuiRetient]],
) -> None:
    client, routeurs = banc
    ecriture, _ = _cles(client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(client, cle, "nas-pop", "pop")
    _brancher(client, cle, "main-gw", "gateway")
    for _ in range(2):
        client.post("/api/v1/jobs/reconcile_shaping/run", headers=cle)
    for nom in ("nas-pop", "main-gw"):
        _rien_qui_touche_le_trafic(routeurs[nom])


def test_les_reglages_par_defaut_ne_brident_personne_d_office() -> None:
    """Sans forfait, pas de limite inventee ; pas de file de lien ; pas de file 0/0."""
    s = Settings(_env_file=None)
    assert s.default_plan_down_mbps == 0
    assert s.default_plan_up_mbps == 0
    assert s.default_plan_for_detected_clients is False
    assert s.shaping_link_queues is False
    assert s.shaping_queue_for_detected_links is False
    assert s.shaping_adopt_foreign_queues is False
    assert s.enforcement_enabled is False


def test_l_export_netflow_n_ecrit_que_ses_propres_reglages() -> None:
    """L'export NetFlow pose par « Set up » ne touche qu'a /ip/traffic-flow :
    jamais une file, jamais le pare-feu. Il compte les paquets, il n'en retient aucun."""
    from app.services import netflow_export as nf

    chemins = {nf.PATH_FLOW, nf.PATH_IPFIX, nf.PATH_TARGET}
    assert all(c.startswith("/ip/traffic-flow") for c in chemins), chemins
    source = Path(nf.__file__).read_text(encoding="utf-8")
    assert "/queue" not in source
    assert "/ip/firewall" not in source

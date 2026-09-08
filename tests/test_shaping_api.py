"""API de topologie et d'enforcement.

Ces endpoints touchent au seul chemin du projet capable d'ecrire sur un
equipement : les tests portent d'abord sur ce qui doit l'empecher.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.enforcement.models import MANAGED_COMMENT
from app.main import register_routes
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_enforcement import FauxClientEcriture


class FauxDepotTopologie:
    """Depot en memoire, meme contrat que la version PostgreSQL."""

    def __init__(self) -> None:
        self.saved_nodes = 0
        self.saved_links = 0
        self.node_kinds: dict[str, str | None] = {}
        self._policies: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self.link_rows: list[dict[str, Any]] = []
        self.node_rows: list[dict[str, Any]] = []
        self.attachment_rows: dict[str, str] = {}

    async def save_snapshot(self, snapshot):
        self.saved_nodes += len(snapshot.nodes)
        self.saved_links += len(snapshot.links)
        return {"nodes": len(snapshot.nodes), "links": len(snapshot.links)}

    async def nodes(self):
        return self.node_rows

    async def links(self):
        return self.link_rows

    async def attachments(self):
        return dict(self.attachment_rows)

    async def set_node_kind(self, key, kind):
        self.node_kinds[key] = kind

    async def upsert_policy(self, **kwargs):
        cle = (kwargs["scope"], kwargs["target_key"])
        self._policies[cle] = {**kwargs}
        return self._policies[cle]

    async def delete_policy(self, scope, target_key):
        return self._policies.pop((scope, target_key), None) is not None

    async def policies(self, scope=None):
        return [p for p in self._policies.values() if scope in (None, p["scope"])]

    async def policy_map(self, scope):
        return {p["target_key"]: p for p in await self.policies(scope)}

    async def record_audit(self, router_name, *, dry_run, outcomes):
        for action, ok, detail in outcomes:
            self.audit_rows.append(
                {
                    "ts": "2026-09-08T00:00:00Z",
                    "router_name": router_name,
                    "verb": action.verb,
                    "path": action.path,
                    "command": action.command,
                    "dry_run": dry_run,
                    "ok": ok,
                    "detail": detail,
                }
            )
        return len(outcomes)

    async def audit(self, *, limit=100):
        return self.audit_rows[-limit:]


@pytest.fixture
def topo() -> FauxDepotTopologie:
    depot = FauxDepotTopologie()
    depot.link_rows = [
        {
            "key": "router:pop-test|ether2|mac:DC:9F:DB:11:22:33",
            "source_key": "router:pop-test",
            "target_key": "mac:DC:9F:DB:11:22:33",
            "source_name": "PoP Test",
            "target_name": "bh-test",
            "target_kind": "radio",
            "kind": "ethernet",
            "interface": "ether2",
            "capacity_mbps": 1000.0,
            "discovered_by": "pop-test",
            "max_down_mbps": None,
            "max_up_mbps": None,
        }
    ]
    return depot


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    client.neighbor_rows = [
        {
            "interface": "ether2",
            "identity": "bh-test",
            "mac-address": "DC:9F:DB:11:22:33",
            "platform": "Ubiquiti",
        }
    ]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


def make_client(settings, topo, routeur, *, ecriture=None) -> TestClient:
    container = build_container(settings, topology_repo=topo, client=routeur)
    if ecriture is not None:
        container.shaping._write_client_factory = lambda config: ecriture
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    tc = TestClient(app)
    tc.container = container  # type: ignore[attr-defined]
    return tc


@pytest.fixture
def client(settings: Settings, topo, routeur) -> TestClient:
    return make_client(settings, topo, routeur)


# --------------------------------------------------------------- topologie
def test_topologie_documente_ses_sources(client: TestClient) -> None:
    """L'operateur doit savoir d'ou vient chaque arete du graphe."""
    body = client.get("/api/v1/topology").json()
    assert "/ip/neighbor" in body["sources"]["neighbors"]
    assert "caller-id" in body["sources"]["pppoe"]
    assert body["counts"]["links"] == 1


def test_decouverte(client: TestClient, topo: FauxDepotTopologie) -> None:
    body = client.post("/api/v1/topology/discover").json()
    assert body["nodes"] >= 2  # le PoP et son voisin radio
    assert body["links"] == 1
    assert topo.saved_links == 1


def test_correction_manuelle_du_role(client: TestClient, topo: FauxDepotTopologie) -> None:
    """La classification automatique est une heuristique : l'operateur tranche."""
    reponse = client.patch("/api/v1/topology/nodes/mac:AA:BB?kind=sector")
    assert reponse.status_code == 200
    assert topo.node_kinds["mac:AA:BB"] == "sector"


# ------------------------------------------------- analyse de l'existant
def test_analyse_distingue_les_files_tierces(
    settings: Settings, topo, routeur: FakeRouterOsClient
) -> None:
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-dupont", "comment": MANAGED_COMMENT},
        {".id": "*2", "name": "queue-radius", "comment": ""},
    ]
    body = make_client(settings, topo, routeur).get("/api/v1/shaping/state").json()

    assert body[0]["counts"]["managed"] == 1
    assert body[0]["counts"]["foreign"] == 1


# ---------------------------------------------------------------- politique
def test_fixer_un_debit_n_ecrit_pas_sur_le_routeur(
    client: TestClient, routeur: FakeRouterOsClient
) -> None:
    """Cliquer pour changer la bande passante enregistre une intention.
    L'ecriture est un second geste, explicite."""
    reponse = client.put(
        "/api/v1/shaping/policies",
        json={
            "scope": "link",
            "target_key": "router:pop-test|ether2|mac:DC:9F:DB:11:22:33",
            "max_down_mbps": 300,
            "max_up_mbps": 100,
        },
    )

    assert reponse.status_code == 200
    assert "plan" in reponse.json()["next_step"]
    # Aucune commande n'a ete envoyee.
    assert client.container.shaping._write_clients == {}  # type: ignore[attr-defined]


def test_validation_des_debits(client: TestClient) -> None:
    mauvais = {"scope": "link", "target_key": "x", "max_down_mbps": -5}
    assert client.put("/api/v1/shaping/policies", json=mauvais).status_code == 422
    assert (
        client.put(
            "/api/v1/shaping/policies", json={"scope": "inconnu", "target_key": "x"}
        ).status_code
        == 422
    )


def test_retrait_d_une_surcharge(client: TestClient) -> None:
    client.put(
        "/api/v1/shaping/policies",
        json={"scope": "subscriber", "target_key": "dupont", "max_down_mbps": 50},
    )
    assert client.delete("/api/v1/shaping/policies/subscriber/dupont").status_code == 200
    assert client.delete("/api/v1/shaping/policies/subscriber/dupont").status_code == 404


# --------------------------------------------------------------------- plan
def test_le_plan_montre_les_commandes_exactes(client: TestClient) -> None:
    body = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    assert body["router"] == "pop-test"
    commandes = [a["command"] for a in body["actions"]]
    assert any(c.startswith("/queue/type/add") and "kind=cake" in c for c in commandes)
    assert any(c.startswith("/queue/simple/add") for c in commandes)
    # Chaque action porte sa raison, pas seulement sa commande.
    assert all(a["reason"] for a in body["actions"])


def test_la_surcharge_se_retrouve_dans_le_plan(client: TestClient) -> None:
    """Le lien est a 1 Gbps ; on impose 300 Mbps, le plan doit le refleter."""
    client.put(
        "/api/v1/shaping/policies",
        json={
            "scope": "link",
            "target_key": "router:pop-test|ether2|mac:DC:9F:DB:11:22:33",
            "max_down_mbps": 300,
            "max_up_mbps": 300,
        },
    )
    body = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    parents = [a for a in body["actions"] if "parent-" in (a["name"] or "")]
    assert parents, "le parent devrait etre planifie"
    # 300 Mbps exactement : une surcharge n'est pas re-multipliee par le facteur.
    assert "300000000/300000000" in parents[0]["command"]


def test_plan_sur_routeur_inconnu(client: TestClient) -> None:
    assert client.post("/api/v1/shaping/plan", json={"router": "absent"}).status_code == 404


# -------------------------------------------------------- VERROUS d'ecriture
def test_application_reelle_exige_une_confirmation(client: TestClient) -> None:
    reponse = client.post("/api/v1/shaping/apply", json={"router": "pop-test", "dry_run": False})
    assert reponse.status_code == 400
    assert "confirm" in reponse.json()["detail"]


def test_application_reelle_refusee_si_enforcement_desactive(client: TestClient) -> None:
    """Le drapeau global est le dernier rempart."""
    reponse = client.post(
        "/api/v1/shaping/apply",
        json={"router": "pop-test", "dry_run": False, "confirm": True},
    )
    assert reponse.status_code == 409
    assert "ENFORCEMENT_ENABLED" in reponse.json()["detail"]


def test_dry_run_par_defaut(client: TestClient, topo: FauxDepotTopologie) -> None:
    body = client.post("/api/v1/shaping/apply", json={"router": "pop-test"}).json()

    assert body["result"]["dry_run"] is True
    assert body["result"]["ok"] is True
    # Meme simule, tout est journalise.
    assert topo.audit_rows and all(r["dry_run"] for r in topo.audit_rows)


def test_application_reelle_journalisee(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = FauxClientEcriture()
    client = make_client(settings, topo, routeur, ecriture=ecriture)

    body = client.post(
        "/api/v1/shaping/apply",
        json={"router": "pop-test", "dry_run": False, "confirm": True},
    ).json()

    assert body["result"]["ok"] is True
    assert body["result"]["applied"] > 0
    assert len(ecriture.executed) == body["result"]["applied"]
    # Le journal garde la trace exacte de ce qui a ete envoye.
    assert topo.audit_rows and not topo.audit_rows[0]["dry_run"]
    assert topo.audit_rows[0]["command"].startswith("/queue/")


def test_journal_expose(client: TestClient) -> None:
    client.post("/api/v1/shaping/apply", json={"router": "pop-test"})
    rows = client.get("/api/v1/shaping/audit").json()
    assert rows and "command" in rows[0]


# ------------------------------------- rattachement au BON backhaul
def test_sans_rattachement_l_abonne_n_a_pas_de_parent(client: TestClient) -> None:
    """Deviner serait pire que ne rien faire : rattacher un abonne au mauvais
    backhaul fausserait toute la gestion de contention."""
    body = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    files_abonnes = [
        a
        for a in body["actions"]
        if a["path"] == "/queue/simple" and "parent-" not in (a["name"] or "")
    ]
    assert files_abonnes
    assert all("parent=" not in a["command"] for a in files_abonnes)
    # Et le plan le dit explicitement.
    assert body["unparented_subscribers"] == len(files_abonnes)
    assert "caller-id" in body["notes"][0]


def test_avec_rattachement_le_parent_est_le_bon_lien(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    """La jointure caller-id / UISP donne le secteur, donc le lien traverse."""
    topo.attachment_rows = {"dupont": "mac:DC:9F:DB:11:22:33"}
    client = make_client(settings, topo, routeur)

    body = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    file_abonne = next(
        a for a in body["actions"] if a["path"] == "/queue/simple" and a["name"] == "freeqos-dupont"
    )
    assert "parent=freeqos-parent-bh-test" in file_abonne["command"]
    assert body["unparented_subscribers"] == 0


def test_rattachement_vers_un_lien_inconnu_reste_sans_parent(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    """Un secteur connu d'UISP mais absent des liens de ce routeur ne doit pas
    produire un parent inexistant : RouterOS rejetterait la commande."""
    topo.attachment_rows = {"dupont": "mac:ZZ:ZZ:ZZ:ZZ:ZZ:ZZ"}
    client = make_client(settings, topo, routeur)

    body = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    file_abonne = next(a for a in body["actions"] if a["name"] == "freeqos-dupont")
    assert "parent=" not in file_abonne["command"]

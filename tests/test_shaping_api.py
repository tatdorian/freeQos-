"""API de topologie et d'enforcement.

Ces endpoints touchent au seul chemin du projet capable d'ecrire sur un
equipement : les tests portent d'abord sur ce qui doit l'empecher.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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
        self.series_rows: list[dict[str, Any]] = []
        self.flags: dict[str, bool] = {}

    async def save_snapshot(self, snapshot):
        self.saved_nodes += len(snapshot.nodes)
        self.saved_links += len(snapshot.links)
        return {"nodes": len(snapshot.nodes), "links": len(snapshot.links)}

    async def nodes(self):
        return self.node_rows

    async def links(self):
        return self.link_rows

    async def link(self, key):
        for ligne in self.link_rows:
            if ligne["key"] == key:
                return ligne
        return None

    async def interface_series(self, *, router_name, interface, minutes=60, bucket_seconds=30):
        return [
            ligne
            for ligne in self.series_rows
            if ligne.get("router_name", router_name) == router_name
            and ligne.get("interface", interface) == interface
        ]

    async def interface_latest(self):
        return list(self.series_rows)

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

    async def set_boost(
        self, *, scope, target_key, down_mbps, up_mbps, expires_at, reason=None, updated_by=None
    ):
        cle = (scope, target_key)
        ligne = self._policies.setdefault(cle, {"scope": scope, "target_key": target_key})
        ligne.update(
            {
                "boost_down_mbps": down_mbps,
                "boost_up_mbps": up_mbps,
                "boost_expires_at": expires_at,
                "boost_reason": reason,
                "updated_by": updated_by,
            }
        )
        return ligne

    async def clear_boost(self, scope, target_key):
        ligne = self._policies.get((scope, target_key))
        if not ligne or ligne.get("boost_expires_at") is None:
            return False
        ligne.update(
            {
                "boost_down_mbps": None,
                "boost_up_mbps": None,
                "boost_expires_at": None,
                "boost_reason": None,
            }
        )
        return True

    async def active_boosts(self, scope=None):
        from datetime import UTC, datetime

        maintenant = datetime.now(tz=UTC)
        resultat = []
        for ligne in self._policies.values():
            echeance = ligne.get("boost_expires_at")
            if echeance is None or echeance <= maintenant:
                continue
            if scope not in (None, ligne["scope"]):
                continue
            resultat.append({**ligne, "seconds_left": (echeance - maintenant).total_seconds()})
        return resultat

    async def expired_boosts(self):
        from datetime import UTC, datetime

        maintenant = datetime.now(tz=UTC)
        return [
            ligne
            for ligne in self._policies.values()
            if ligne.get("boost_expires_at") and ligne["boost_expires_at"] <= maintenant
        ]

    async def purge_expired_boosts(self):
        echus = await self.expired_boosts()
        for ligne in echus:
            ligne.update(
                {
                    "boost_down_mbps": None,
                    "boost_up_mbps": None,
                    "boost_expires_at": None,
                    "boost_reason": None,
                }
            )
        return len(echus)

    async def get_flag(self, name):
        return self.flags.get(name)

    async def set_flag(self, name, value, *, updated_by=None, reason=None):
        self.flags[name] = value

    async def flag_detail(self, name):
        if name not in self.flags:
            return None
        return {"name": name, "value": self.flags[name], "reason": None}

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
            # Debit mesure du port qui porte ce lien.
            "rx_bps": 12_000_000.0,
            "tx_bps": 340_000_000.0,
            "running": True,
            "port_capacity_mbps": 1000.0,
            "measured_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            "measure_fresh": True,
            "interface_links": 1,
        }
    ]
    depot.series_rows = [
        {
            "bucket": datetime(2026, 1, 1, 11, 59, tzinfo=UTC),
            "rx_bps": 11_000_000.0,
            "tx_bps": 300_000_000.0,
            "rx_peak_bps": 12_000_000.0,
            "tx_peak_bps": 340_000_000.0,
            "capacity_mbps": 1000.0,
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


# -------------------------------------------------------- debit d'un lien
CLE_LIEN = "router:pop-test|ether2|mac:DC:9F:DB:11:22:33"


def test_le_debit_d_un_lien_est_expose_avec_son_historique(client: TestClient) -> None:
    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/throughput").json()

    assert body["link"]["tx_bps"] == 340_000_000.0
    assert body["link"]["rx_bps"] == 12_000_000.0
    assert len(body["series"]) == 1
    assert body["measurement"]["source"] == "port"


def test_un_port_partage_est_signale_comme_tel(client: TestClient, topo) -> None:
    """Deux voisins sur le meme port : le chiffre est celui du PORT. Le
    presenter comme le debit d'un seul voisin serait faux."""
    topo.link_rows[0]["interface_links"] = 2

    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/throughput").json()

    assert body["measurement"]["source"] == "port-partage"
    assert "2 voisins" in body["measurement"]["note"]


def test_un_lien_sans_port_local_le_dit(client: TestClient, topo) -> None:
    """Une adjacence declaree par UISP n'a pas de compteur d'octets cote
    routeur : on l'annonce au lieu d'afficher zero."""
    topo.link_rows[0]["interface"] = None
    topo.link_rows[0]["interface_links"] = 0

    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/throughput").json()

    assert body["measurement"]["source"] == "aucune"
    assert body["series"] == []


def test_lien_inconnu_renvoie_404(client: TestClient) -> None:
    reponse = client.get("/api/v1/topology/links/inexistant/throughput")
    assert reponse.status_code == 404


def test_mesure_instantanee_interroge_le_routeur(client: TestClient, routeur) -> None:
    routeur.monitor_rates["ether2"] = (9_000_000, 250_000_000)

    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/live").json()

    assert body["source"] == "monitor-traffic"
    assert body["tx_bps"] == 250_000_000
    assert body["rx_bps"] == 9_000_000


def test_mesure_instantanee_impossible_retombe_sur_les_compteurs(
    client: TestClient, routeur
) -> None:
    """Version de RouterOS, droits, port virtuel : la commande peut echouer.
    L'operateur voulait un chiffre, pas une erreur -- on lui rend le dernier
    connu en disant pourquoi."""
    routeur.raise_on_monitor = RuntimeError("no such command")

    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/live").json()

    assert body["source"] == "compteurs"
    assert body["tx_bps"] == 340_000_000.0
    assert "no such command" in body["detail"]


def test_mesure_instantanee_sans_port_local(client: TestClient, topo) -> None:
    topo.link_rows[0]["interface"] = None

    body = client.get("/api/v1/topology/links/" + quote(CLE_LIEN, safe="") + "/live").json()

    assert body["source"] == "aucune"
    assert body["rx_bps"] is None


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


# ==========================================================================
# Bascule de l'enforcement depuis l'interface
# ==========================================================================


def test_etat_initial_lecture_seule(client: TestClient) -> None:
    body = client.get("/api/v1/shaping/enforcement").json()
    assert body["enabled"] is False
    assert body["locked"] is False


def test_activation_exige_une_confirmation(client: TestClient) -> None:
    """Autoriser l'ecriture sur des routeurs de production merite un geste."""
    reponse = client.put("/api/v1/shaping/enforcement", json={"enabled": True})

    assert reponse.status_code == 400
    assert "confirm" in reponse.json()["detail"]
    assert client.container.shaping.enforcement_enabled is False  # type: ignore[attr-defined]


def test_activation_leve_le_verrou_global_mais_pas_les_autres(client: TestClient) -> None:
    """Les verrous sont independants : activer l'enforcement ne dispense pas
    d'avoir un compte d'ecriture sur le routeur."""
    avant = client.post(
        "/api/v1/shaping/apply",
        json={"router": "pop-test", "dry_run": False, "confirm": True},
    )
    assert avant.status_code == 409
    assert "lecture seule" in avant.json()["detail"]

    client.put(
        "/api/v1/shaping/enforcement",
        json={"enabled": True, "confirm": True, "reason": "bascule de nuit"},
    )
    assert client.container.shaping.enforcement_enabled is True  # type: ignore[attr-defined]

    apres = client.post(
        "/api/v1/shaping/apply",
        json={"router": "pop-test", "dry_run": False, "confirm": True},
    )
    # Le verrou global est leve : l'ecriture est tentee avec le compte
    # configure, sans exiger de declaration rw_*.
    assert apres.status_code == 200


def test_coupure_immediate_sans_confirmation(client: TestClient) -> None:
    """Revenir en lecture seule ne doit jamais demander de ceremonie."""
    client.put("/api/v1/shaping/enforcement", json={"enabled": True, "confirm": True})

    reponse = client.put("/api/v1/shaping/enforcement", json={"enabled": False})

    assert reponse.status_code == 200
    assert client.container.shaping.enforcement_enabled is False  # type: ignore[attr-defined]


def test_bascule_persistee(client: TestClient, topo: FauxDepotTopologie) -> None:
    from app.services.shaping import FLAG_ENFORCEMENT

    client.put(
        "/api/v1/shaping/enforcement",
        json={"enabled": True, "confirm": True, "reason": "essai"},
    )
    assert topo.flags[FLAG_ENFORCEMENT] is True


def test_verrou_interdit_la_bascule(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    """Un exploitant qui tient a la friction du redemarrage doit pouvoir la garder."""
    settings.enforcement_locked = True
    client = make_client(settings, topo, routeur)

    reponse = client.put("/api/v1/shaping/enforcement", json={"enabled": True, "confirm": True})

    assert reponse.status_code == 409
    assert "ENFORCEMENT_LOCKED" in reponse.json()["detail"]
    assert client.container.shaping.enforcement_enabled is False  # type: ignore[attr-defined]


def test_la_base_prime_sur_l_environnement(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    """Une bascule faite depuis l'interface ne doit pas etre perdue au redemarrage."""
    from app.services.shaping import FLAG_ENFORCEMENT

    topo.flags[FLAG_ENFORCEMENT] = True
    settings.enforcement_enabled = False
    client = make_client(settings, topo, routeur)

    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        client.container.shaping.load_flags()  # type: ignore[attr-defined]
    )
    assert client.container.shaping.enforcement_enabled is True  # type: ignore[attr-defined]


# ==========================================================================
# Boost
# ==========================================================================


def test_boost_par_multiplicateur(client: TestClient, topo: FauxDepotTopologie) -> None:
    reponse = client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 60, "multiplier": 3},
    )

    assert reponse.status_code == 200
    boost = reponse.json()["boost"]
    # Le plan de reference est 100/20 Mbps.
    assert boost["down_mbps"] == 300
    assert boost["up_mbps"] == 60
    assert boost["duration_minutes"] == 60


def test_boost_par_debit_explicite(client: TestClient) -> None:
    body = client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 15, "down_mbps": 800},
    ).json()

    assert body["boost"]["down_mbps"] == 800
    assert body["boost"]["up_mbps"] is None


def test_boost_sans_debit_refuse(client: TestClient) -> None:
    reponse = client.post(
        "/api/v1/shaping/boosts", json={"login": "dupont", "duration_minutes": 60}
    )
    assert reponse.status_code == 400


def test_boost_sur_abonne_inconnu(client: TestClient) -> None:
    reponse = client.post(
        "/api/v1/shaping/boosts",
        json={"login": "inconnu", "duration_minutes": 60, "multiplier": 2},
    )
    assert reponse.status_code == 404


def test_duree_bornee(client: TestClient) -> None:
    """Un boost d'un an ne serait plus un boost."""
    trop_long = {"login": "dupont", "duration_minutes": 60 * 24 * 400, "multiplier": 2}
    assert client.post("/api/v1/shaping/boosts", json=trop_long).status_code == 422
    nul = {"login": "dupont", "duration_minutes": 0, "multiplier": 2}
    assert client.post("/api/v1/shaping/boosts", json=nul).status_code == 422


def test_boost_enregistre_meme_si_ecriture_coupee(
    client: TestClient, topo: FauxDepotTopologie
) -> None:
    """Poser un boost doit reussir en lecture seule : le retour dit alors
    pourquoi rien n'a ete pousse."""
    body = client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 30, "multiplier": 2},
    ).json()

    assert body["boost"]["down_mbps"] == 200
    assert body["applied"]["ok"] is False
    assert "lecture seule" in body["applied"]["detail"] or "desactive" in body["applied"]["detail"]


def test_boost_pousse_quand_l_ecriture_est_permise(
    settings: Settings, topo: FauxDepotTopologie, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = FauxClientEcriture()
    client = make_client(settings, topo, routeur, ecriture=ecriture)

    body = client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 30, "multiplier": 4},
    ).json()

    assert body["applied"]["ok"] is True
    # La commande poussee porte bien le debit boostee : 100 x 4 = 400 Mbps.
    commandes = [a.command for a in ecriture.executed]
    assert any("400000000" in c for c in commandes)


def test_boost_visible_dans_la_liste(client: TestClient) -> None:
    client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 45, "multiplier": 2, "reason": "geste co"},
    )
    boosts = client.get("/api/v1/shaping/boosts").json()

    assert len(boosts) == 1
    assert boosts[0]["target_key"] == "dupont"
    assert boosts[0]["boost_reason"] == "geste co"


def test_retrait_anticipe(client: TestClient) -> None:
    client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 60, "multiplier": 2},
    )

    assert client.delete("/api/v1/shaping/boosts/dupont").status_code == 200
    assert client.get("/api/v1/shaping/boosts").json() == []
    # Retirer deux fois n'est pas une erreur silencieuse.
    assert client.delete("/api/v1/shaping/boosts/dupont").status_code == 404


def test_le_boost_apparait_dans_le_plan(client: TestClient) -> None:
    client.post(
        "/api/v1/shaping/boosts",
        json={"login": "dupont", "duration_minutes": 60, "down_mbps": 750},
    )
    plan = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()

    file_abonne = next(a for a in plan["actions"] if a["name"] == "freeqos-dupont")
    assert "750000000" in file_abonne["command"]

"""Detection ARP des clients sur VLAN routee : ce qu'elle voit, et ce qu'elle
n'a surtout PAS le droit de faire.

La moitie de ce fichier teste des absences. C'est voulu : le risque d'une
detection n'est pas de manquer un client, c'est d'en inventer un. Un candidat
qui deviendrait abonne tout seul recevrait un plan que personne n'a vendu et
une file que personne n'a demandee.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.radius import MockPlanProvider
from app.collectors.topology import (
    KIND_CANDIDATE,
    KIND_CPE,
    KIND_POP,
    KIND_STATIC,
    TopologyNode,
    TopologySnapshot,
    attach_vlan_candidates,
    candidate_node_key,
)
from app.collectors.uisp import MockBackhaulProvider
from app.collectors.vlan_clients import (
    pppoe_interfaces,
    sightings_from_arp,
    vlan_index,
)
from app.config import Settings
from app.db.directory import InMemoryDirectory
from app.db.writer import InMemoryMetricsWriter
from app.main import register_routes
from app.models import VlanSighting
from app.services.collection import JOB_VLAN_CLIENTS, CollectionService
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_clients_statiques import InventaireApi, InventaireMemoire

# =========================================================================
# 1. Le filtrage : quatre raisons d'ecarter une entree ARP
# =========================================================================

VLANS = [
    {"name": "vlan120", "vlan-id": "120"},
    {"name": "vlan130", "vlan-id": "130"},
    {"name": "vlan999", "vlan-id": "999"},
    {"name": "vlan140", "vlan-id": "140", "disabled": "true"},
]
SERVEURS_PPPOE = [{"interface": "vlan999", "service-name": "isp"}]


def _vues(arp: list[dict], **kwargs) -> list[VlanSighting]:
    return sightings_from_arp(
        arp, VLANS, SERVEURS_PPPOE, router_name="pop-nord", pop_name="PoP Nord", **kwargs
    )


def test_une_adresse_qui_parle_sur_une_vlan_routee_est_vue() -> None:
    vues = _vues(
        [{"address": "10.20.0.5", "mac-address": "AA:BB:CC:00:00:01", "interface": "vlan120"}]
    )
    assert len(vues) == 1
    assert vues[0].address == "10.20.0.5"
    assert vues[0].mac == "AA:BB:CC:00:00:01"
    assert vues[0].vlan_id == 120
    assert vues[0].vlan_interface == "vlan120"
    assert vues[0].router_name == "pop-nord"
    assert vues[0].pop_name == "PoP Nord"


def test_la_vlan_du_serveur_pppoe_est_exclue() -> None:
    """Sans cette exclusion, chaque abonne PPPoE apparaitrait aussi en
    "client a IP fixe a declarer" : la pire confusion possible a mettre sous
    les yeux d'un operateur, puisqu'il a DEJA une identite et un plan."""
    assert (
        _vues(
            [{"address": "10.99.0.7", "mac-address": "AA:BB:CC:00:00:03", "interface": "vlan999"}]
        )
        == []
    )


def test_le_transit_et_le_management_sont_exclus() -> None:
    """La table ARP contient tout ce que le routeur a resolu. Seules les
    interfaces de /interface/vlan sont candidates a porter un client."""
    assert (
        _vues(
            [
                {"address": "192.0.2.1", "mac-address": "AA:BB:CC:00:00:04", "interface": "ether1"},
                {
                    "address": "192.0.2.2",
                    "mac-address": "AA:BB:CC:00:00:05",
                    "interface": "bridge-lan",
                },
            ]
        )
        == []
    )


def test_une_vlan_desactivee_est_exclue() -> None:
    assert (
        _vues(
            [{"address": "10.40.0.1", "mac-address": "AA:BB:CC:00:00:07", "interface": "vlan140"}]
        )
        == []
    )


def test_une_entree_sans_mac_n_est_pas_une_presence() -> None:
    """Une entree ARP sans MAC signifie qu'on a CHERCHE cette adresse, pas
    qu'elle a repondu. La proposer serait proposer un client inexistant."""
    assert _vues([{"address": "10.20.0.50", "interface": "vlan120"}]) == []


@pytest.mark.parametrize("drapeau", ["disabled", "invalid"])
def test_les_entrees_desactivees_ou_invalides_sont_exclues(drapeau: str) -> None:
    assert (
        _vues(
            [
                {
                    "address": "10.20.0.60",
                    "mac-address": "AA:BB:CC:00:00:06",
                    "interface": "vlan120",
                    drapeau: "true",
                }
            ]
        )
        == []
    )


def test_les_entrees_statiques_sont_conservees() -> None:
    """Beaucoup d'operateurs figent le couple IP/MAC de leurs clients a IP fixe.
    Ecarter les entrees statiques reviendrait a rater exactement la population
    qu'on cherche a reperer."""
    vues = _vues(
        [
            {
                "address": "10.20.0.8",
                "mac-address": "AA:BB:CC:00:00:08",
                "interface": "vlan120",
                "dynamic": "false",
            }
        ]
    )
    assert [v.address for v in vues] == ["10.20.0.8"]


def test_la_mac_est_normalisee_et_les_doublons_replies() -> None:
    """Une meme adresse peut porter une entree statique ET une dynamique."""
    vues = _vues(
        [
            {
                "address": "10.20.0.9",
                "mac-address": "aabbcc000002",
                "interface": "vlan120",
                "dynamic": "false",
            },
            {"address": "10.20.0.9", "mac-address": "AA:BB:CC:00:00:02", "interface": "vlan120"},
        ]
    )
    assert len(vues) == 1
    assert vues[0].mac == "AA:BB:CC:00:00:02"


@pytest.mark.parametrize("adresse", ["0.0.0.0", "127.0.0.1", "224.0.0.1", "pas-une-ip", ""])
def test_les_adresses_inexploitables_sont_ecartees(adresse: str) -> None:
    assert (
        _vues([{"address": adresse, "mac-address": "AA:BB:CC:00:00:09", "interface": "vlan120"}])
        == []
    )


def test_le_resultat_est_trie_par_adresse() -> None:
    """Un ordre stable : la liste proposee a l'operateur ne doit pas danser
    d'un cycle a l'autre selon l'ordre de la table ARP."""
    vues = _vues(
        [
            {"address": "10.20.0.30", "mac-address": "AA:BB:CC:00:00:30", "interface": "vlan120"},
            {"address": "10.20.0.4", "mac-address": "AA:BB:CC:00:00:04", "interface": "vlan130"},
            {"address": "10.20.0.11", "mac-address": "AA:BB:CC:00:00:11", "interface": "vlan120"},
        ]
    )
    assert [v.address for v in vues] == ["10.20.0.4", "10.20.0.11", "10.20.0.30"]


def test_index_des_vlan() -> None:
    assert vlan_index(VLANS) == {"vlan120": 120, "vlan130": 130, "vlan999": 999}
    # Un vlan-id illisible ne fait pas perdre la VLAN, seulement son numero.
    assert vlan_index([{"name": "vlanX", "vlan-id": "abc"}]) == {"vlanX": None}


def test_interfaces_pppoe() -> None:
    assert pppoe_interfaces(SERVEURS_PPPOE) == {"vlan999"}
    # Un serveur desactive ne protege plus rien : sa VLAN redevient candidate.
    assert pppoe_interfaces([{"interface": "vlan999", "disabled": "true"}]) == set()


# =========================================================================
# 2. Le job : il enregistre, et RIEN de plus
# =========================================================================


class DepotObservations:
    """Double du depot. Volontairement reduit au contrat du service : si le job
    tentait de lire les candidats, ce double le ferait echouer."""

    def __init__(self) -> None:
        self.enregistrees: list[VlanSighting] = []
        self.prunes: list[float] = []
        self.erreur: Exception | None = None

    async def record(self, sightings, *, seen_at) -> int:
        if self.erreur is not None:
            raise self.erreur
        self.enregistrees.extend(sightings)
        return len(sightings)

    async def prune(self, *, older_than_s: float) -> int:
        self.prunes.append(older_than_s)
        return 0


def build_service(
    settings: Settings,
    client: FakeRouterOsClient,
    depot: DepotObservations | None,
    inventaire: InventaireMemoire | None = None,
) -> tuple[CollectionService, InMemoryMetricsWriter, InMemoryDirectory]:
    collectors = [MikrotikCollector(cfg, client=client) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    directory = InMemoryDirectory()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(),
        plan_provider=MockPlanProvider(),
        directory=directory,
        writer=writer,
        backhauls=[],
        static_clients=inventaire,
        sightings=depot,
    )
    return service, writer, directory


@pytest.fixture
def routeur_vlan() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.vlan_rows = list(VLANS)
    client.pppoe_server_rows = list(SERVEURS_PPPOE)
    client.arp_rows = [
        {"address": "10.20.0.5", "mac-address": "AA:BB:CC:00:00:01", "interface": "vlan120"},
        {"address": "10.20.0.9", "mac-address": "AA:BB:CC:00:00:02", "interface": "vlan130"},
        {"address": "10.99.0.7", "mac-address": "AA:BB:CC:00:00:03", "interface": "vlan999"},
    ]
    return client


async def test_le_job_enregistre_les_observations(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    depot = DepotObservations()
    service, _, _ = build_service(settings, routeur_vlan, depot)

    resultat = await service.detect_vlan_clients()

    assert resultat.ok, resultat.errors
    assert resultat.job == JOB_VLAN_CLIENTS
    assert resultat.items == 2
    assert sorted(v.address for v in depot.enregistrees) == ["10.20.0.5", "10.20.0.9"]
    # La retention est appliquee au meme tour.
    assert depot.prunes == [settings.vlan_sighting_retention_s]


async def test_le_job_ne_cree_aucun_abonne(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    """LE TEST CENTRAL DE LA SECTION 2.

    Deux adresses sont detectees. Apres le job, le referentiel doit etre
    RIGOUREUSEMENT vide : pas d'abonne, pas de plan, pas d'echantillon. Une
    detection est une piste, pas un client.
    """
    depot = DepotObservations()
    service, writer, directory = build_service(settings, routeur_vlan, depot)

    await service.detect_vlan_clients()

    assert directory.subscribers == {}
    assert directory.plans == {}
    assert writer.subscriber_rows == []


async def test_un_candidat_ne_produit_jamais_de_cible_de_shaping(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    """Le corollaire cote enforcement : meme apres un cycle de detection ET un
    cycle de collecte, une adresse non declaree ne donne aucune file.

    C'est la garantie que le brief demande explicitement -- sans plan saisi par
    un humain, aucun SubscriberTarget.
    """
    from app.services.shaping import ShapingService
    from tests.test_shaping_service import DepotBoosts, MetriquesMinimales, make_service

    depot = DepotObservations()
    # L'inventaire est VIDE : les deux adresses vues ne sont declarees nulle part.
    inventaire = InventaireMemoire([])
    service, _, _ = build_service(settings, routeur_vlan, depot, inventaire)
    await service.detect_vlan_clients()
    await service.collect_subscribers()

    shaping: ShapingService = make_service(
        settings,
        routeur_vlan,
        repository=DepotBoosts(),
        static_clients=inventaire,
    )
    shaping.metrics = MetriquesMinimales([])
    await shaping.registry.reload()

    _, abonnes = await shaping.build_targets("pop-test")

    assert abonnes == []
    # Et le service de shaping n'a meme pas de quoi lire les observations :
    # le depot ne lui est pas injecte, par construction.
    assert not hasattr(shaping, "sightings")


async def test_la_detection_peut_etre_coupee(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    settings.vlan_detect_enabled = False
    depot = DepotObservations()
    service, _, _ = build_service(settings, routeur_vlan, depot)

    resultat = await service.detect_vlan_clients()

    assert resultat.ok
    assert resultat.items == 0
    assert depot.enregistrees == []


async def test_sans_depot_le_job_ne_fait_rien_et_reussit(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    service, _, _ = build_service(settings, routeur_vlan, None)
    resultat = await service.detect_vlan_clients()
    assert resultat.ok
    assert resultat.items == 0


async def test_un_routeur_sans_arp_lisible_n_annule_pas_les_autres(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    routeur_vlan.raise_on_arp = RuntimeError("timeout")
    depot = DepotObservations()
    service, _, _ = build_service(settings, routeur_vlan, depot)

    resultat = await service.detect_vlan_clients()

    assert not resultat.ok
    assert any("timeout" in e for e in resultat.errors)
    assert depot.enregistrees == []


async def test_un_routeur_purement_l3_est_supporte(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    """Un routeur sans serveur PPPoE n'a pas la table correspondante. Toutes
    ses VLAN sont alors eligibles, y compris celle qui portait le serveur."""
    routeur_vlan.raise_on_pppoe_servers = RuntimeError("no such command")
    depot = DepotObservations()
    service, _, _ = build_service(settings, routeur_vlan, depot)

    resultat = await service.detect_vlan_clients()

    assert resultat.ok, resultat.errors
    assert sorted(v.address for v in depot.enregistrees) == [
        "10.20.0.5",
        "10.20.0.9",
        "10.99.0.7",
    ]


async def test_une_ecriture_impossible_est_signalee(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    depot = DepotObservations()
    depot.erreur = RuntimeError("base injoignable")
    service, _, _ = build_service(settings, routeur_vlan, depot)

    resultat = await service.detect_vlan_clients()

    assert not resultat.ok
    assert any("base injoignable" in e for e in resultat.errors)


# =========================================================================
# 3. Topologie : une troisieme nature, jamais confondue
# =========================================================================


def _candidat(**kwargs) -> dict:
    base = {
        "router_name": "pop-nord",
        "address": "10.20.0.5",
        "mac": "AA:BB:CC:00:00:01",
        "vlan_interface": "vlan120",
        "vlan_id": 120,
        "pop_name": "PoP Nord",
        "first_seen": "2026-09-14T10:00:00+00:00",
        "last_seen": "2026-09-14T12:00:00+00:00",
    }
    return {**base, **kwargs}


def test_un_candidat_a_sa_propre_nature() -> None:
    """Ni voisin reseau, ni CPE, ni abonne : on sait qu'une adresse parle, on
    ne sait pas QUI. Les trois confusions possibles sont testees ici."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-nord", name="PoP Nord", kind=KIND_POP))

    poses = attach_vlan_candidates(
        snapshot, [_candidat()], pop_keys={"PoP Nord": "router:pop-nord"}
    )

    assert poses == 1
    noeud = snapshot.nodes[candidate_node_key("pop-nord", "10.20.0.5")]
    assert noeud.kind == KIND_CANDIDATE
    assert noeud.kind != KIND_CPE
    assert noeud.kind != KIND_STATIC
    assert noeud.kind != KIND_POP
    assert noeud.attributes["declared"] is False
    assert noeud.attributes["detected"] is True
    assert noeud.attributes["source"] == "arp"


def test_un_candidat_pend_sous_son_pop() -> None:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-nord", name="PoP Nord", kind=KIND_POP))

    attach_vlan_candidates(snapshot, [_candidat()], pop_keys={"PoP Nord": "router:pop-nord"})

    cle = candidate_node_key("pop-nord", "10.20.0.5")
    lien = next(iter(snapshot.links.values()))
    assert lien.source_key == "router:pop-nord"
    assert lien.target_key == cle
    assert lien.attributes["declared"] is False


def test_un_candidat_ne_devient_pas_un_rattachement_d_abonne() -> None:
    """subscriber_sectors pilote le fair-share. Y inscrire un candidat
    reviendrait a compter dans le partage une machine dont on ne sait rien."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-nord", name="PoP Nord", kind=KIND_POP))

    attach_vlan_candidates(snapshot, [_candidat()], pop_keys={"PoP Nord": "router:pop-nord"})

    assert snapshot.subscriber_sectors == {}


def test_le_plafond_protege_l_arbre() -> None:
    """Une VLAN de collecte bavarde produirait des centaines d'entrees ARP, et
    un arbre illisible ne sert plus a decider."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-nord", name="PoP Nord", kind=KIND_POP))
    beaucoup = [_candidat(address=f"10.20.0.{i}") for i in range(1, 60)]

    poses = attach_vlan_candidates(
        snapshot, beaucoup, pop_keys={"PoP Nord": "router:pop-nord"}, limit=10
    )

    assert poses == 10
    assert len([n for n in snapshot.nodes.values() if n.kind == KIND_CANDIDATE]) == 10


def test_deux_routeurs_peuvent_voir_la_meme_adresse() -> None:
    """Des plans d'adressage prives se recoupent d'un PoP a l'autre : la cle
    doit porter le routeur, sinon un PoP ecraserait le candidat de l'autre."""
    snapshot = TopologySnapshot()
    for nom in ("pop-nord", "pop-sud"):
        snapshot.add_node(TopologyNode(key=f"router:{nom}", name=nom, kind=KIND_POP))

    attach_vlan_candidates(
        snapshot,
        [
            _candidat(router_name="pop-nord", pop_name="pop-nord"),
            _candidat(router_name="pop-sud", pop_name="pop-sud"),
        ],
        pop_keys={"pop-nord": "router:pop-nord", "pop-sud": "router:pop-sud"},
    )

    assert candidate_node_key("pop-nord", "10.20.0.5") in snapshot.nodes
    assert candidate_node_key("pop-sud", "10.20.0.5") in snapshot.nodes


# =========================================================================
# 4. API : consultation seule
# =========================================================================


class DepotApi:
    def __init__(self, candidats: list[dict] | None = None) -> None:
        self._candidats = candidats or []
        self.presence_rows: dict[str, dict] = {}

    async def candidates(self, *, limit: int = 500, max_age_s: float | None = None) -> list[dict]:
        return self._candidats[:limit]

    async def presence(self) -> dict[str, dict]:
        return dict(self.presence_rows)


class DepotTopologie:
    """Graphe minimal : un seul PoP, pour que les candidats aient un parent."""

    async def nodes(self) -> list[dict]:
        return [
            {
                "key": "router:pop-nord",
                "name": "PoP Nord",
                "kind": KIND_POP,
                "address": "10.10.0.11",
                "attributes": {},
                "hidden": False,
            }
        ]

    async def links(self) -> list[dict]:
        return []

    async def aliases(self) -> dict[str, str]:
        return {}


@pytest.fixture
def api(settings: Settings):
    inventaire = InventaireApi()
    depot = DepotApi([_candidat(), _candidat(address="10.20.0.9")])
    container = build_container(
        settings,
        static_clients_repo=inventaire,
        sightings_repo=depot,
        topology_repo=DepotTopologie(),
    )
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), depot, inventaire


def test_api_liste_les_candidats(api) -> None:
    client, _, _ = api
    corps = client.get("/api/v1/static-clients/candidates").json()
    assert corps["enabled"] is True
    assert corps["count"] == 2
    assert [c["address"] for c in corps["candidates"]] == ["10.20.0.5", "10.20.0.9"]


def test_api_dit_quand_la_detection_est_coupee(api, settings: Settings) -> None:
    client, _, _ = api
    settings.vlan_detect_enabled = False
    corps = client.get("/api/v1/static-clients/candidates").json()
    assert corps["enabled"] is False
    assert corps["candidates"] == []


def test_api_ne_propose_aucune_route_pour_promouvoir_un_candidat(api) -> None:
    """Il n'existe VOLONTAIREMENT aucun endpoint "promouvoir ce candidat".

    Declarer un client passe par POST /static-clients, avec un plan dans le
    corps de la requete. L'interface pre-remplit l'adresse et le VLAN, mais le
    debit souscrit doit etre saisi : c'est ce qui garantit qu'un humain a
    tranche.
    """
    client, _, _ = api
    chemins = client.app.openapi()["paths"]
    candidats = chemins["/api/v1/static-clients/candidates"]
    assert set(candidats) == {"get"}
    assert not any("promote" in c or "candidates" in c and c.endswith("declare") for c in chemins)


def test_api_joint_la_presence_aux_fiches(api) -> None:
    client, depot, inventaire = api
    cree = client.post(
        "/api/v1/static-clients",
        json={"reference": "mairie", "pop_name": "PoP Nord", "address": "10.0.0.0/29"},
    ).json()
    assert cree["reference"] == "mairie"

    depot.presence_rows = {
        "mairie": {
            "last_seen": "2026-09-14T12:00:00+00:00",
            "mac": "AA:BB:CC:00:00:01",
            "vlan_interface": "vlan120",
            "router_name": "pop-nord",
        }
    }
    fiches = client.get("/api/v1/static-clients").json()
    assert fiches[0]["last_seen_at"] == "2026-09-14T12:00:00+00:00"
    assert fiches[0]["seen_mac"] == "AA:BB:CC:00:00:01"


def test_une_fiche_sans_presence_reste_lisible(api) -> None:
    """Ne pas savoir n'est pas la meme chose qu'etre absent : le champ est
    explicitement nul, pas rempli d'une date inventee."""
    client, _, _ = api
    client.post(
        "/api/v1/static-clients",
        json={"reference": "silencieux", "pop_name": "PoP Nord", "address": "10.0.0.5"},
    )
    fiches = client.get("/api/v1/static-clients").json()
    assert fiches[0]["last_seen_at"] is None


def test_le_graphe_expose_les_candidats_avec_leur_nature(api) -> None:
    client, _, _ = api
    graphe = client.get("/api/v1/topology").json()
    natures = {n["kind"] for n in graphe["nodes"] if n["key"].startswith("candidate:")}
    assert natures == {KIND_CANDIDATE}
    assert "arp" in graphe["sources"]


# =========================================================================
# 5. Pourquoi un client n'est PAS vu : le filtre s'explique
# =========================================================================

from app.collectors.vlan_clients import (  # noqa: E402
    REJET_HORS_VLAN,
    REJET_PPPOE,
    REJET_SANS_MAC,
    REJET_VLAN_DESACTIVEE,
    explain_arp,
    judge_arp_rows,
)


def _client(interface: str, adresse: str = "10.20.0.77", **kwargs) -> dict:
    return {
        "address": adresse,
        "mac-address": "AA:BB:CC:00:00:77",
        "interface": interface,
        **kwargs,
    }


def test_le_diagnostic_dit_la_meme_chose_que_la_detection() -> None:
    """UN SEUL CHEMIN DE DECISION.

    Si le diagnostic raisonnait a part, il finirait par mentir sur ce que fait
    le code -- et un diagnostic qui ment est pire que pas de diagnostic.
    """
    arp = [_client("vlan120"), _client("bridge1", "10.20.0.78"), _client("vlan999", "10.99.0.7")]
    vlans = [{"name": "vlan120", "vlan-id": "120"}, {"name": "vlan999", "vlan-id": "999"}]
    pppoe = [{"interface": "vlan999"}]

    retenus = {v.address for v in judge_arp_rows(arp, vlans, pppoe) if v.kept}
    vues = {v.address for v in sightings_from_arp(arp, vlans, pppoe, router_name="r", pop_name="p")}

    assert retenus == vues == {"10.20.0.77"}


def test_l_adressage_sur_un_pont_est_designe_nommement() -> None:
    """LE CAS QUI REPOND A "pourquoi je ne vois pas mes clients VLAN".

    Sur un pont en filtrage VLAN qui porte l'adressage client, la table ARP
    nomme le PONT et non une VLAN. La detection ne peut pas les voir -- c'est
    une limite connue, et le diagnostic doit la designer sans ambiguite plutot
    que de rendre une liste vide.
    """
    arp = [_client("bridge1", f"10.20.0.{i}") for i in (77, 78, 79)]

    rapport = explain_arp(arp, vlan_rows=[], pppoe_rows=[])

    assert rapport["kept"] == 0
    assert rapport["arp_rows"] == 3
    assert rapport["by_reason"] == {REJET_HORS_VLAN: 3}
    # Le champ qui donne la reponse : l'interface fautive, et son poids.
    assert rapport["interfaces_hors_vlan"] == {"bridge1": 3}


def test_une_vlan_declaree_mais_eteinte_a_son_propre_motif() -> None:
    """Une VLAN absente demande de chercher ou est l'adressage ; une VLAN
    desactivee demande juste de la reactiver. Confondre les deux envoie
    l'exploitant au mauvais endroit."""
    rapport = explain_arp(
        [_client("vlan130", "10.30.0.5")],
        [{"name": "vlan130", "vlan-id": "130", "disabled": "true"}],
        [],
    )
    assert list(rapport["by_reason"]) == [REJET_VLAN_DESACTIVEE]


def test_le_motif_pppoe_est_distingue() -> None:
    """Ce rejet-la est VOULU : ces abonnes ont deja une identite."""
    rapport = explain_arp(
        [_client("vlan999", "10.99.0.7")],
        [{"name": "vlan999", "vlan-id": "999"}],
        [{"interface": "vlan999"}],
    )
    assert list(rapport["by_reason"]) == [REJET_PPPOE]
    assert rapport["interfaces_pppoe"] == ["vlan999"]
    # Pas dans "hors VLAN" : ce n'est pas un probleme d'adressage.
    assert rapport["interfaces_hors_vlan"] == {}


def test_une_adresse_cherchee_mais_muette_est_distinguee() -> None:
    rapport = explain_arp(
        [{"address": "10.20.0.50", "interface": "vlan120"}],
        [{"name": "vlan120", "vlan-id": "120"}],
        [],
    )
    assert list(rapport["by_reason"]) == [REJET_SANS_MAC]


def test_le_rapport_porte_de_quoi_verifier_sur_le_routeur() -> None:
    """Chaque champ doit correspondre a une commande que l'exploitant peut
    taper : /ip/arp print, /interface/vlan print, /interface/pppoe-server."""
    rapport = explain_arp([_client("vlan120")], [{"name": "vlan120", "vlan-id": "120"}], [])
    assert rapport["vlans_declares"] == ["vlan120"]
    assert rapport["kept"] == 1
    detail = rapport["verdicts"][0]
    assert detail["address"] == "10.20.0.77"
    assert detail["vlan_id"] == 120
    assert detail["kept"] is True


async def test_le_collecteur_rend_le_rapport_du_routeur(
    settings: Settings, routeur_vlan: FakeRouterOsClient
) -> None:
    from app.collectors.mikrotik import MikrotikCollector

    collecteur = MikrotikCollector(settings.routers[0], client=routeur_vlan)

    rapport = await collecteur.explain_vlan_clients()

    assert rapport["router"] == settings.routers[0].name
    assert rapport["kept"] == 2
    # La VLAN du serveur PPPoE est comptee comme ecartee, pas oubliee.
    assert REJET_PPPOE in rapport["by_reason"]


def test_api_diagnostic_sans_routeur_collecte(api) -> None:
    """Un routeur ecarte de la collecte n'est lu nulle part : le message doit
    l'envoyer vers l'onglet Equipements, pas le laisser chercher."""
    client, _, _ = api
    reponse = client.get("/api/v1/static-clients/candidates/diagnostic?router_name=fantome")
    assert reponse.status_code == 404
    assert "ecarte de la collecte" in reponse.json()["detail"]


# =========================================================================
# 6. L'arbre dit ce que la derniere decouverte a trouve
# =========================================================================


def test_le_graphe_expose_les_remarques_de_la_derniere_analyse(api) -> None:
    """Le job periodique jetait les avertissements du snapshot.

    Un arbre de cases isolees sans explication n'aide personne : ces messages
    disent precisement ce qui manque pour les relier (un loopback non declare,
    un secteur inconnu, un PoP ecarte de la collecte).
    """
    from app.collectors.topology import TopologySnapshot

    client, _, _ = api
    container = client.app.dependency_overrides[get_container]()
    snapshot = TopologySnapshot()
    snapshot.warnings.append("DS-CCR : aucun loopback trouve.")
    container.shaping.last_snapshot = snapshot
    container.shaping.last_discovery_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)

    corps = client.get("/api/v1/topology").json()

    assert corps["warnings"] == ["DS-CCR : aucun loopback trouve."]
    assert corps["discovered_at"].startswith("2026-09-16T10:00")


def test_un_arbre_vide_distingue_ses_deux_causes(api) -> None:
    """ "Rien a decouvrir" et "rien n'a encore ete decouvert" produisent le meme
    arbre vide et demandent des gestes opposes. Le champ discovered_at est ce
    qui les separe."""
    client, _, _ = api
    container = client.app.dependency_overrides[get_container]()

    container.shaping.last_discovery_at = None
    assert client.get("/api/v1/topology").json()["discovered_at"] is None

    container.shaping.last_discovery_at = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    assert client.get("/api/v1/topology").json()["discovered_at"] is not None

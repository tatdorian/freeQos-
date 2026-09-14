"""Clients a IP fixe : de la fiche declaree jusqu'a la file RouterOS.

Ce fichier verrouille les trois promesses du chantier :

  1. les deux natures COEXISTENT -- ajouter des clients statiques ne change
     rien au chemin PPPoE ;
  2. elles se DISTINGUENT partout ou il le faut (base, topologie, interface) ;
  3. elles suivent ENSUITE exactement le meme chemin plan() / apply().
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.radius import MockPlanProvider
from app.collectors.topology import (
    KIND_SECTOR,
    KIND_STATIC,
    TopologyNode,
    TopologySnapshot,
    attach_static_clients,
    static_client_node_key,
)
from app.collectors.uisp import MockBackhaulProvider
from app.config import Settings
from app.db.directory import InMemoryDirectory
from app.db.static_clients_repo import (
    DuplicateStaticClientError,
    InvalidStaticClientError,
    StaticClientNotFoundError,
    normalise_address,
    to_model,
)
from app.db.writer import InMemoryMetricsWriter
from app.enforcement.planner import TARGET_INTERFACE, SubscriberTarget
from app.main import register_routes
from app.models import KIND_PPPOE, StaticClient
from app.models import KIND_STATIC as SUB_KIND_STATIC
from app.services.collection import PLAN_SOURCE_STATIC, CollectionService
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_collection_service import Clock
from tests.test_shaping_service import DepotBoosts, MetriquesMinimales, make_service


class InventaireMemoire:
    """Double de StaticClientsRepository : ne garde que ce que lit le service."""

    def __init__(self, clients: list[StaticClient] | None = None) -> None:
        self.clients = clients or []
        self.lectures = 0
        self.erreur: Exception | None = None

    async def load_enabled(self) -> list[StaticClient]:
        self.lectures += 1
        if self.erreur is not None:
            raise self.erreur
        return list(self.clients)


def fiche(**kwargs) -> StaticClient:
    base = {
        "reference": "mairie-vitre",
        "pop_name": "PoP Test",
        "address": "10.0.0.5/32",
        "plan_down_mbps": 200.0,
        "plan_up_mbps": 50.0,
    }
    return StaticClient(**{**base, **kwargs})


# =========================================================================
# 1. Identite : un seul espace de noms, un discriminant explicite
# =========================================================================


def test_l_adresse_ne_fait_pas_partie_de_l_identite() -> None:
    """Le nom de file doit survivre a un demenagement.

    C'est l'argument qui a fait ecarter un identifiant synthetique du type
    'static:<vlan>:<ip>' : la cle de reconciliation en aurait dependu, et
    changer l'IP d'un client aurait detruit puis recree sa file, en perdant au
    passage ses surcharges et son historique.
    """
    avant = SubscriberTarget(
        login="mairie-vitre",
        interface="",
        plan_down_mbps=200,
        plan_up_mbps=50,
        kind=SUB_KIND_STATIC,
        address="10.0.0.5/32",
    )
    apres = SubscriberTarget(
        login="mairie-vitre",
        interface="",
        plan_down_mbps=200,
        plan_up_mbps=50,
        kind=SUB_KIND_STATIC,
        address="192.168.7.9/32",
    )
    assert avant.queue_name == apres.queue_name
    assert avant.queue_target() != apres.queue_target()


def test_un_client_statique_vise_toujours_son_adresse() -> None:
    """Il n'a pas d'interface a lui : la viser briderait ses voisins de VLAN."""
    cible = SubscriberTarget(
        login="mairie",
        interface="",
        plan_down_mbps=100,
        plan_up_mbps=20,
        kind=SUB_KIND_STATIC,
        address="10.0.0.5",
    )
    assert cible.queue_target() == "10.0.0.5/32"
    # Meme quand la configuration globale demande des files par interface.
    assert cible.queue_target(TARGET_INTERFACE) == "10.0.0.5/32"


def test_le_sous_reseau_declare_est_conserve() -> None:
    """Un professionnel a qui on a vendu un /29 doit voir son bloc plafonne,
    pas seulement sa premiere adresse."""
    cible = SubscriberTarget(
        login="clinique",
        interface="",
        plan_down_mbps=500,
        plan_up_mbps=500,
        kind=SUB_KIND_STATIC,
        address="10.0.0.0/29",
    )
    assert cible.queue_target() == "10.0.0.0/29"


def test_une_session_pppoe_reste_ramenee_a_un_hote() -> None:
    """Le chemin PPPoE ne doit RIEN changer : un prefixe large sur une session
    serait une erreur de saisie qui shaperait les voisins de l'abonne."""
    cible = SubscriberTarget(
        login="dupont",
        interface="<pppoe-dupont>",
        plan_down_mbps=100,
        plan_up_mbps=20,
        kind=KIND_PPPOE,
        address="10.20.0.10/24",
    )
    assert cible.queue_target() == "10.20.0.10/32"
    assert cible.queue_target(TARGET_INTERFACE) == "<pppoe-dupont>"


@pytest.mark.parametrize(
    ("saisie", "attendu"),
    [
        ("10.0.0.5", "10.0.0.5/32"),
        ("10.0.0.5/32", "10.0.0.5/32"),
        ("10.0.0.0/29", "10.0.0.0/29"),
        # Adresse d'hote portant un prefixe large : lue comme "le bloc du client".
        ("10.0.0.5/29", "10.0.0.0/29"),
        ("2001:db8::1", "2001:db8::1/128"),
    ],
)
def test_normalisation_des_adresses(saisie: str, attendu: str) -> None:
    assert normalise_address(saisie) == attendu


@pytest.mark.parametrize("saisie", ["", "pas-une-ip", "0.0.0.0", "127.0.0.1"])
def test_adresses_refusees(saisie: str) -> None:
    with pytest.raises(InvalidStaticClientError):
        normalise_address(saisie)


# =========================================================================
# 2. Collecte : materialisation et mesure
# =========================================================================


def build_service(
    settings: Settings,
    client: FakeRouterOsClient,
    inventaire: InventaireMemoire,
    clock: Clock | None = None,
) -> tuple[CollectionService, InMemoryMetricsWriter, InMemoryDirectory]:
    clock = clock or Clock()
    collectors = [MikrotikCollector(cfg, client=client) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    directory = InMemoryDirectory()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=directory,
        writer=writer,
        backhauls=[],
        clock=clock,
        static_clients=inventaire,
    )
    return service, writer, directory


async def test_un_client_declare_devient_un_abonne(settings: Settings) -> None:
    client = FakeRouterOsClient()
    inventaire = InventaireMemoire([fiche()])
    service, writer, directory = build_service(settings, client, inventaire)

    resultat = await service.collect_subscribers()

    assert resultat.ok, resultat.errors
    assert "mairie-vitre" in directory.subscribers
    sid = directory.subscribers["mairie-vitre"]
    assert directory.kinds[sid] == SUB_KIND_STATIC
    # Le plan vient de la fiche, et le dit.
    assert directory.plans[sid].source == PLAN_SOURCE_STATIC
    assert directory.plans[sid].down_mbps == 200.0
    # Il a bien produit un echantillon, meme sans trafic mesurable.
    assert [s.login for _, s in writer.subscriber_rows] == ["mairie-vitre"]


async def test_les_deux_natures_cohabitent(settings: Settings) -> None:
    """La preuve que rien n'est casse : meme cycle, meme ecriture, deux natures."""
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    inventaire = InventaireMemoire([fiche()])
    service, writer, directory = build_service(settings, client, inventaire)

    await service.collect_subscribers()

    logins = sorted(s.login for _, s in writer.subscriber_rows)
    assert logins == ["dupont", "mairie-vitre"]
    assert directory.kinds[directory.subscribers["dupont"]] == KIND_PPPOE
    assert directory.kinds[directory.subscribers["mairie-vitre"]] == SUB_KIND_STATIC


async def test_le_debit_vient_des_compteurs_de_sa_file(settings: Settings) -> None:
    """La file posee sur son adresse est le seul compteur par client dont on
    dispose : sans session PPPoE, aucune interface ne porte son trafic."""
    client = FakeRouterOsClient()
    clock = Clock()
    inventaire = InventaireMemoire([fiche()])
    service, writer, _ = build_service(settings, client, inventaire, clock=clock)

    # RouterOS rend la paire du point de vue de la CIBLE : <upload>/<download>.
    client.simple_queue_rows = [
        {"name": "freeqos-mairie-vitre", "target": "10.0.0.5/32", "bytes": "1000/5000"}
    ]
    await service.collect_subscribers()  # premiere mesure : pas encore de debit

    clock.advance(10)
    client.simple_queue_rows = [
        {"name": "freeqos-mairie-vitre", "target": "10.0.0.5/32", "bytes": "2000/15000"}
    ]
    await service.collect_subscribers()

    dernier = writer.subscriber_rows[-1][1]
    # 1000 octets montants en 10 s = 800 bps ; 10000 descendants = 8000 bps.
    assert dernier.rx_bps == pytest.approx(800)
    assert dernier.tx_bps == pytest.approx(8000)


async def test_sans_file_le_client_existe_mais_sans_debit(settings: Settings) -> None:
    """Limite assumee : pas de file posee, pas de mesure. Mieux vaut un trou
    qu'un zero, qui se lirait comme une absence de trafic."""
    client = FakeRouterOsClient()
    inventaire = InventaireMemoire([fiche()])
    service, writer, _ = build_service(settings, client, inventaire)

    await service.collect_subscribers()

    echantillon = writer.subscriber_rows[-1][1]
    assert echantillon.rx_bps is None
    assert echantillon.tx_bps is None
    assert echantillon.rx_bytes is None


async def test_un_inventaire_illisible_ne_casse_pas_le_cycle_pppoe(
    settings: Settings,
) -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    inventaire = InventaireMemoire([fiche()])
    inventaire.erreur = RuntimeError("base injoignable")
    service, writer, _ = build_service(settings, client, inventaire)

    resultat = await service.collect_subscribers()

    assert [s.login for _, s in writer.subscriber_rows] == ["dupont"]
    assert not resultat.ok
    assert any("inventaire statique" in e for e in resultat.errors)


async def test_sans_inventaire_le_comportement_est_inchange(settings: Settings) -> None:
    """Un deploiement 100 % PPPoE ne doit rien voir de ce chantier."""
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    collectors = [MikrotikCollector(cfg, client=client) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=writer,
        backhauls=[],
    )

    resultat = await service.collect_subscribers()

    assert resultat.ok
    assert [s.login for _, s in writer.subscriber_rows] == ["dupont"]


# =========================================================================
# 3. Topologie : une nature a part, comptee dans le partage
# =========================================================================


def test_le_client_est_pose_sous_son_secteur_declare() -> None:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="uisp:ap-1", name="Secteur Nord", kind=KIND_SECTOR))

    poses = attach_static_clients(snapshot, [fiche(sector_key="uisp:ap-1")])

    assert poses == 1
    noeud = snapshot.nodes[static_client_node_key("mairie-vitre")]
    # Sa propre nature : le confondre avec un CPE ferait croire a une
    # decouverte la ou il n'y a qu'une declaration.
    assert noeud.kind == KIND_STATIC
    assert noeud.attributes["declared"] is True
    assert any(
        lien.source_key == "uisp:ap-1" and lien.target_key == noeud.key
        for lien in snapshot.links.values()
    )
    # Le rattachement remonte : c'est lui qui fera compter le client dans le
    # partage d'un lien congestionne.
    assert snapshot.subscriber_sectors["mairie-vitre"] == "uisp:ap-1"


def test_sans_secteur_le_client_pend_sous_son_pop() -> None:
    """Moins precis, mais jamais faux : on prefere un rattachement large a un
    client flottant hors de l'arbre."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-test", name="PoP Test", kind="pop"))

    attach_static_clients(snapshot, [fiche()], pop_keys={"PoP Test": "router:pop-test"})

    cle = static_client_node_key("mairie-vitre")
    assert any(
        lien.source_key == "router:pop-test" and lien.target_key == cle
        for lien in snapshot.links.values()
    )


def test_un_secteur_inconnu_est_signale_sans_perdre_le_client() -> None:
    snapshot = TopologySnapshot()

    attach_static_clients(snapshot, [fiche(sector_key="uisp:fantome")])

    # Le noeud existe quand meme : l'operateur doit VOIR son client.
    assert static_client_node_key("mairie-vitre") in snapshot.nodes
    assert any("secteur inconnu" in avertissement for avertissement in snapshot.warnings)


# =========================================================================
# 4. Shaping : le meme chemin, jusqu'a la commande RouterOS
# =========================================================================


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    """Routeur nu : aucune session PPPoE, pour isoler le chemin statique."""
    client = FakeRouterOsClient()
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


def service_avec_inventaire(
    settings: Settings, routeur: FakeRouterOsClient, clients: list[StaticClient]
) -> ShapingService:
    service = make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        static_clients=InventaireMemoire(clients),
    )
    service.metrics = MetriquesMinimales([])
    return service


async def test_le_client_statique_produit_une_cible(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = service_avec_inventaire(settings, routeur, [fiche()])
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert len(abonnes) == 1
    cible = abonnes[0]
    assert cible.kind == SUB_KIND_STATIC
    assert cible.interface == ""
    assert cible.queue_target() == "10.0.0.5/32"


async def test_seul_le_pop_concerne_recoit_ses_clients(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = service_avec_inventaire(
        settings, routeur, [fiche(), fiche(reference="ailleurs", pop_name="PoP Lointain")]
    )
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert [a.login for a in abonnes] == ["mairie-vitre"]


async def test_le_plan_ecrit_la_meme_commande_que_pour_un_abonne(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """La preuve de bout en bout : le client statique traverse plan() et en
    ressort avec une file /queue/simple ordinaire."""
    settings.enforcement_enabled = True
    service = service_avec_inventaire(settings, routeur, [fiche()])
    await service.registry.reload()

    liens, abonnes = await service.build_targets("pop-test")
    plan = await service.plan("pop-test", links=liens, subscribers=abonnes)

    ajouts = [a for a in plan.actions if a.verb == "add" and a.path == "/queue/simple"]
    assert [a.fields["name"] for a in ajouts] == ["freeqos-mairie-vitre"]
    assert ajouts[0].fields["target"] == "10.0.0.5/32"
    # 50 Mbps montant / 200 Mbps descendant, format RouterOS.
    assert ajouts[0].fields["max-limit"] == "50000000/200000000"


async def test_la_surcharge_manuelle_s_applique_aussi_aux_statiques(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Meme hierarchie de debits que pour un abonne PPPoE : la surcharge
    manuelle prime sur le plan declare."""
    settings.enforcement_enabled = True
    depot = DepotBoosts()

    async def policy_map(scope):
        if scope == "subscriber":
            return {"mairie-vitre": {"max_down_mbps": 20.0, "max_up_mbps": 5.0}}
        return {}

    depot.policy_map = policy_map  # type: ignore[method-assign]
    service = make_service(
        settings, routeur, repository=depot, static_clients=InventaireMemoire([fiche()])
    )
    service.metrics = MetriquesMinimales([])
    await service.registry.reload()

    liens, abonnes = await service.build_targets("pop-test")
    plan = await service.plan("pop-test", links=liens, subscribers=abonnes)

    ajouts = [a for a in plan.actions if a.verb == "add" and a.path == "/queue/simple"]
    assert ajouts[0].fields["max-limit"] == "5000000/20000000"


async def test_un_client_desactive_disparait_du_plan(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """load_enabled ne rend que les fiches actives : suspendre un client
    retire sa file au cycle suivant, sans perdre sa fiche."""
    service = service_avec_inventaire(settings, routeur, [])
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert abonnes == []


async def test_le_pop_d_un_client_statique_est_retrouve_sans_metrique(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Un client boostee le jour de sa saisie n'a pas encore d'echantillon :
    l'inventaire doit suffire a savoir quel routeur replanifier."""
    service = service_avec_inventaire(settings, routeur, [fiche()])
    await service.registry.reload()

    assert await service._routers_for_logins({"mairie-vitre"}) == ["pop-test"]


# =========================================================================
# 5. API d'administration
# =========================================================================


class InventaireApi:
    """Double du depot, cote API : garde des lignes en memoire comme la base."""

    def __init__(self) -> None:
        self.lignes: list[dict] = []
        self._next_id = 1

    async def list_all(self, *, pop_name: str | None = None) -> list[dict]:
        return [
            dict(ligne)
            for ligne in self.lignes
            if pop_name is None or ligne["pop_name"] == pop_name
        ]

    async def get(self, client_id: int) -> dict:
        for ligne in self.lignes:
            if ligne["id"] == client_id:
                return dict(ligne)
        raise StaticClientNotFoundError(str(client_id))

    async def load_enabled(self) -> list[StaticClient]:
        return [to_model(ligne) for ligne in self.lignes if ligne["enabled"]]

    async def create(self, payload: dict) -> dict:
        if any(ligne["reference"] == payload["reference"] for ligne in self.lignes):
            raise DuplicateStaticClientError(payload["reference"])
        ligne = {**payload, "id": self._next_id, "address": normalise_address(payload["address"])}
        self._next_id += 1
        self.lignes.append(ligne)
        return dict(ligne)

    async def update(self, client_id: int, payload: dict) -> dict:
        ligne = await self.get(client_id)
        if "address" in payload and payload["address"] is not None:
            payload = {**payload, "address": normalise_address(payload["address"])}
        ligne.update({k: v for k, v in payload.items() if v is not None})
        self.lignes = [ligne if x["id"] == client_id else x for x in self.lignes]
        return dict(ligne)

    async def delete(self, client_id: int) -> None:
        avant = len(self.lignes)
        self.lignes = [x for x in self.lignes if x["id"] != client_id]
        if len(self.lignes) == avant:
            raise StaticClientNotFoundError(str(client_id))


@pytest.fixture
def api_client(settings: Settings) -> tuple[TestClient, InventaireApi]:
    inventaire = InventaireApi()
    container = build_container(settings, static_clients_repo=inventaire)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), inventaire


def test_api_declare_puis_liste(api_client) -> None:
    client, _ = api_client
    reponse = client.post(
        "/api/v1/static-clients",
        json={
            "reference": "mairie-vitre",
            "pop_name": "PoP Test",
            "address": "10.0.0.5",
            "plan_down_mbps": 200,
        },
    )
    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["address"] == "10.0.0.5/32"

    liste = client.get("/api/v1/static-clients").json()
    assert [f["reference"] for f in liste] == ["mairie-vitre"]


def test_api_refuse_une_adresse_invalide(api_client) -> None:
    client, _ = api_client
    reponse = client.post(
        "/api/v1/static-clients",
        json={"reference": "x", "pop_name": "PoP Test", "address": "pas-une-ip"},
    )
    assert reponse.status_code == 400


def test_api_refuse_un_doublon(api_client) -> None:
    client, _ = api_client
    corps = {"reference": "mairie", "pop_name": "PoP Test", "address": "10.0.0.5"}
    assert client.post("/api/v1/static-clients", json=corps).status_code == 201
    assert client.post("/api/v1/static-clients", json=corps).status_code == 409


def test_api_modifie_et_retire(api_client) -> None:
    client, inventaire = api_client
    cree = client.post(
        "/api/v1/static-clients",
        json={"reference": "mairie", "pop_name": "PoP Test", "address": "10.0.0.5"},
    ).json()

    modifie = client.patch(f"/api/v1/static-clients/{cree['id']}", json={"address": "10.0.0.0/29"})
    assert modifie.status_code == 200
    assert modifie.json()["address"] == "10.0.0.0/29"

    assert client.delete(f"/api/v1/static-clients/{cree['id']}").status_code == 204
    assert inventaire.lignes == []
    assert client.delete(f"/api/v1/static-clients/{cree['id']}").status_code == 404


def test_api_vlan_hors_bornes_refuse(api_client) -> None:
    client, _ = api_client
    reponse = client.post(
        "/api/v1/static-clients",
        json={"reference": "x", "pop_name": "PoP Test", "address": "10.0.0.5", "vlan": 9999},
    )
    assert reponse.status_code == 422


# =========================================================================
# 6. Non-regression : RADIUS ne doit pas parler a la place de l'inventaire
# =========================================================================


async def test_radius_ne_reecrit_pas_le_plan_d_un_client_statique(
    settings: Settings,
) -> None:
    """Piege reel : le job de rafraichissement des plans interrogeait RADIUS
    pour TOUS les abonnes.

    Un serveur RADIUS qui repond quand meme -- catch-all, plan par defaut, ou
    simplement un fournisseur de test -- ecraserait alors le debit declare dans
    l'inventaire par une valeur inventee. Pour un client a IP fixe, la fiche
    fait foi : RADIUS n'a rien a dire sur lui.
    """
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    inventaire = InventaireMemoire([fiche()])
    service, _, directory = build_service(settings, client, inventaire)

    await service.collect_subscribers()
    sid_statique = directory.subscribers["mairie-vitre"]
    assert directory.plans[sid_statique].source == PLAN_SOURCE_STATIC

    # MockPlanProvider fabrique un plan pour N'IMPORTE QUEL login.
    assert await service.plan_provider.get_plan("mairie-vitre") is not None

    resultat = await service.refresh_plans()

    assert resultat.ok
    # L'abonne PPPoE a bien ete rafraichi...
    assert directory.plans[directory.subscribers["dupont"]].source.startswith("mock:")
    # ... et le client statique a garde le plan de sa fiche.
    assert directory.plans[sid_statique].source == PLAN_SOURCE_STATIC
    assert directory.plans[sid_statique].down_mbps == 200.0

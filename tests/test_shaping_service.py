"""Service de shaping : verrous, analyse de l'existant, plan de bout en bout."""

from __future__ import annotations

import pytest

from app.config import RouterConfig, Settings
from app.enforcement.models import MANAGED_COMMENT, Plan, PlanAction
from app.enforcement.planner import LinkTarget, SubscriberTarget
from app.enforcement.routeros import MissingWriteCredentialsError
from app.services.registry import RouterRegistry
from app.services.shaping import (
    EnforcementDisabledError,
    ShapingService,
    _segment_du_lien,
)
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture


def make_service(settings: Settings, client: FakeRouterOsClient, **kwargs) -> ShapingService:
    registry = RouterRegistry(settings, client_factory=lambda config: client)
    service = ShapingService(settings, registry=registry, **kwargs)
    return service


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    client.neighbor_rows = [
        {
            "interface": "ether2",
            "identity": "BH-Nord",
            "mac-address": "DC:9F:DB:11:22:33",
            "platform": "Ubiquiti Networks Inc.",
        }
    ]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


# ------------------------------------------------------- VERROU PRINCIPAL
async def test_application_reelle_refusee_si_enforcement_desactive(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Le drapeau global est le dernier rempart : meme avec un plan valide et
    une confirmation, rien ne part si l'enforcement est desactive."""
    settings.enforcement_enabled = False
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    with pytest.raises(EnforcementDisabledError, match="ENFORCEMENT_ENABLED"):
        await service.apply(plan, dry_run=False)


async def test_dry_run_autorise_meme_enforcement_desactive(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Simuler doit rester possible : c'est ainsi qu'on prepare une bascule."""
    settings.enforcement_enabled = False
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    resultat = await service.apply(plan, dry_run=True)

    assert resultat.ok and resultat.dry_run is True


async def test_application_sans_compte_distinct_utilise_le_compte_configure(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Le compte configure fait l'affaire s'il a les droits : on ne bloque pas
    sur l'absence d'une declaration rw_*."""
    settings.enforcement_enabled = True
    settings.routers = [RouterConfig(name="pop-test", host="192.0.2.11", password="secret")]
    ecriture = FauxClientEcriture()
    service = make_service(settings, routeur, write_client_factory=lambda config: ecriture)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    resultat = await service.apply(plan, dry_run=False)

    assert resultat.ok
    assert [a.name for a in ecriture.executed] == ["x"]


async def test_exigence_stricte_bloque_sans_compte_distinct(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    settings.require_separate_write_account = True
    settings.routers = [RouterConfig(name="pop-test", host="192.0.2.11", password="secret")]
    service = make_service(settings, routeur)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    with pytest.raises(MissingWriteCredentialsError):
        await service.apply(plan, dry_run=False)


# ---------------------------------------------- droits reels du compte
async def test_droits_lus_sur_le_routeur(settings: Settings, routeur: FakeRouterOsClient) -> None:
    """On interroge /user et /user/group plutot que de se fier a l'inventaire."""
    service = make_service(settings, routeur)
    await service.registry.reload()

    verdict = await service.write_capability("pop-test")

    assert verdict.can_write is False
    assert "write" in verdict.missing


async def test_compte_complet_reconnu(settings: Settings, routeur: FakeRouterOsClient) -> None:
    routeur.grant_write("qos-ro")
    service = make_service(settings, routeur)
    await service.registry.reload()

    verdict = await service.write_capability("pop-test")

    assert verdict.can_write is True
    assert verdict.group == "full"


async def test_droits_non_verifiables_ne_bloquent_pas(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Compte RADIUS, /user non lisible : on tente et RouterOS tranche."""
    routeur.raise_on_users = PermissionError("no permission to read /user")
    service = make_service(settings, routeur)
    await service.registry.reload()

    verdict = await service.write_capability("pop-test")

    assert verdict.can_write is None
    assert "tranchera" in verdict.detail


async def test_application_reelle_aboutit(settings: Settings, routeur: FakeRouterOsClient) -> None:
    settings.enforcement_enabled = True
    ecriture = FauxClientEcriture()
    service = make_service(settings, routeur, write_client_factory=lambda config: ecriture)
    await service.registry.reload()
    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )

    resultat = await service.apply(plan, dry_run=False)

    assert resultat.ok
    assert [a.name for a in ecriture.executed] == ["x"]


# ------------------------------------------------ analyse de l'existant
async def test_inspection_distingue_nos_files_des_autres(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Avant d'ecrire, il faut savoir ce que l'operateur ou RADIUS ont deja pose."""
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-dupont", "comment": MANAGED_COMMENT},
        {".id": "*2", "name": "queue-radius-jean", "comment": ""},
        {".id": "*3", "name": "shaping-exploitant", "comment": "ne pas toucher"},
    ]
    service = make_service(settings, routeur)
    await service.registry.reload()

    etats = await service.inspect()

    etat = etats[0].to_dict()
    assert etat["counts"]["managed"] == 1
    assert etat["counts"]["foreign"] == 2
    assert {q["name"] for q in etat["foreign_queues"]} == {
        "queue-radius-jean",
        "shaping-exploitant",
    }


async def test_inspection_d_un_routeur_injoignable(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.raise_on_queues = ConnectionRefusedError("connexion refusee")
    service = make_service(settings, routeur)
    await service.registry.reload()

    etats = await service.inspect()

    assert etats[0].reachable is False
    assert "ConnectionRefusedError" in (etats[0].error or "")


# ----------------------------------------------------- plan de bout en bout
async def test_plan_complet_depuis_un_routeur_vierge(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()

    plan = await service.plan(
        "pop-test",
        links=[LinkTarget(name="bh-nord", interface="ether2", measured_capacity_mbps=500)],
        subscribers=[
            SubscriberTarget(
                login="dupont",
                interface="<pppoe-dupont>",
                address="10.20.0.10",
                plan_down_mbps=100,
                plan_up_mbps=20,
                parent="freeqos-parent-bh-nord",
            )
        ],
    )

    assert plan.counts() == {"add": 4, "set": 0, "remove": 0}
    commandes = [a.command for a in plan.actions]
    assert any("kind=cake" in c for c in commandes)
    assert any("cake-overhead=22" in c for c in commandes)
    # 500 x 0,9 = 450 Mbps sur le parent.
    assert any("450000000" in c for c in commandes)


def test_le_segment_du_lien_vient_de_ip_address() -> None:
    """La cible d'une file de lien se lit dans /ip/address, sous forme reseau."""
    assert _segment_du_lien({"attributes": '{"local_networks": ["172.16.38.1/23"]}'}) == (
        "172.16.38.0/23"
    )
    # Plusieurs adresses sur le port : on retient le segment le PLUS LARGE, pas
    # le /30 de gestion qui ne porte aucun abonne.
    assert _segment_du_lien(
        {"attributes": {"local_networks": ["10.0.0.1/30", "172.16.38.1/23"]}}
    ) == ("172.16.38.0/23")
    # Lien purement L2, ou attributs illisibles : pas de segment, pas de cible.
    assert _segment_du_lien({"attributes": {"local_networks": []}}) is None
    assert _segment_du_lien({"attributes": "pas du json"}) is None
    assert _segment_du_lien({}) is None


async def test_plan_sur_routeur_inconnu(settings: Settings, routeur: FakeRouterOsClient) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()
    with pytest.raises(KeyError):
        await service.plan("pop-inexistant", links=[], subscribers=[])


# ------------------------------------------------------------- decouverte
async def test_decouverte_construit_le_graphe(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = make_service(settings, routeur)
    await service.registry.reload()

    snapshot = await service.discover()

    assert "router:pop-test" in snapshot.nodes
    assert "mac:DC:9F:DB:11:22:33" in snapshot.nodes
    lien = next(iter(snapshot.links.values()))
    assert lien.interface == "ether2"
    assert lien.capacity_mbps == 1000.0


async def test_un_pop_injoignable_n_annule_pas_la_decouverte(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.raise_on_neighbors = TimeoutError("pas de reponse")
    service = make_service(settings, routeur)
    await service.registry.reload()

    snapshot = await service.discover()

    # Le PoP injoignable ne DISPARAIT pas : sa case reste, marquee injoignable,
    # pour que l'operateur ne croie pas l'avoir perdu.
    assert "router:pop-test" in snapshot.nodes
    noeud = snapshot.nodes["router:pop-test"]
    assert noeud.attributes["unreachable"] is True
    assert len(snapshot.warnings) == 1
    assert "TimeoutError" in snapshot.warnings[0]


# =========================================================================
# Expiration des boosts
# =========================================================================


class DepotBoosts:
    """Depot minimal centre sur les boosts."""

    def __init__(self, echus: list[dict] | None = None) -> None:
        self.echus = echus or []
        self.purges = 0
        self.flags: dict[str, bool] = {}
        self.audit_rows: list = []

    async def expired_boosts(self):
        return self.echus

    async def purge_expired_boosts(self):
        self.purges = len(self.echus)
        self.echus = []
        return self.purges

    async def policy_map(self, scope):
        return {}

    async def attachments(self):
        return {}

    async def links(self):
        return []

    async def get_flag(self, name):
        return self.flags.get(name)

    async def set_flag(self, name, value, *, updated_by=None, reason=None):
        self.flags[name] = value

    async def record_audit(self, router_name, *, dry_run, outcomes):
        self.audit_rows.extend(outcomes)
        return len(outcomes)


class MetriquesMinimales:
    def __init__(self, abonnes: list[dict]) -> None:
        self.abonnes = abonnes

    async def subscriber_latest(self, **kwargs):
        return self.abonnes

    async def backhaul_latest(self, **kwargs):
        return []


async def test_rien_a_faire_sans_boost_echu(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    depot = DepotBoosts()
    service = make_service(settings, routeur, repository=depot)
    await service.registry.reload()

    resultat = await service.expire_boosts()

    assert resultat["expired"] == 0
    assert depot.purges == 0


async def test_boost_echu_purge_et_file_ramenee(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Sans cette etape le boost resterait indefiniment : la file RouterOS ne
    sait rien de l'echeance."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    depot = DepotBoosts([{"scope": "subscriber", "target_key": "dupont"}])
    ecriture = FauxClientEcriture()
    service = make_service(
        settings, routeur, repository=depot, write_client_factory=lambda c: ecriture
    )
    service.metrics = MetriquesMinimales(
        [
            {
                "pppoe_login": "dupont",
                "pop_name": "PoP Test",
                "plan_down_mbps": 100,
                "plan_up_mbps": 20,
            }
        ]
    )
    await service.registry.reload()

    resultat = await service.expire_boosts()

    assert resultat["expired"] == 1
    assert resultat["routers"] == ["pop-test"]
    # La file est reecrite au debit du plan, pas au debit boostee.
    commandes = [a.command for a in ecriture.executed]
    assert any("20000000/100000000" in c for c in commandes)


async def test_boost_echu_sans_enforcement(settings: Settings, routeur: FakeRouterOsClient) -> None:
    """En lecture seule on purge quand meme la base, mais on dit clairement que
    la file du routeur garde son debit boostee."""
    settings.enforcement_enabled = False
    depot = DepotBoosts([{"scope": "subscriber", "target_key": "dupont"}])
    service = make_service(settings, routeur, repository=depot)
    await service.registry.reload()

    resultat = await service.expire_boosts()

    assert resultat["expired"] == 1
    assert resultat["routers"] == []
    assert resultat["errors"] and "enforcement desactive" in resultat["errors"][0]


async def test_seuls_les_routeurs_concernes_sont_replanifies(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Un boost sur un PoP ne doit pas declencher une reecriture de tout le parc."""
    depot = DepotBoosts([{"scope": "subscriber", "target_key": "dupont"}])
    service = make_service(settings, routeur, repository=depot)
    service.metrics = MetriquesMinimales(
        [
            {"pppoe_login": "dupont", "pop_name": "PoP Test"},
            {"pppoe_login": "autre", "pop_name": "PoP Lointain"},
        ]
    )
    await service.registry.reload()

    routeurs = await service._routers_for_logins({"dupont"})

    assert routeurs == ["pop-test"]
    assert await service._routers_for_logins({"inexistant"}) == []


# =========================================================================
# Bascule du drapeau
# =========================================================================


async def test_amorcage_du_drapeau(settings: Settings, routeur: FakeRouterOsClient) -> None:
    """Au premier demarrage la base est vide : elle recoit la valeur d'env."""
    settings.enforcement_enabled = True
    depot = DepotBoosts()
    service = make_service(settings, routeur, repository=depot)

    await service.load_flags()

    assert depot.flags["enforcement_enabled"] is True


async def test_la_base_fait_foi_apres_amorcage(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Une bascule faite depuis l'interface survit au redemarrage."""
    settings.enforcement_enabled = False
    depot = DepotBoosts()
    depot.flags["enforcement_enabled"] = True
    service = make_service(settings, routeur, repository=depot)

    await service.load_flags()

    assert service.enforcement_enabled is True


async def test_bascule_refusee_si_verrouille(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    from app.services.shaping import EnforcementLockedError

    settings.enforcement_locked = True
    service = make_service(settings, routeur, repository=DepotBoosts())

    with pytest.raises(EnforcementLockedError):
        await service.set_enforcement(True)
    assert service.enforcement_enabled is False


# =========================================================================
# L'adresse qui porte la file
# =========================================================================


async def test_l_adresse_vient_du_routeur_pas_de_la_base(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """La base peut avoir un cycle de retard. Entre-temps l'abonne a pu se
    reconnecter et le pool reattribuer son ancienne IP a un voisin : ecrire une
    file sur l'adresse stockee briderait le mauvais client."""
    routeur.active[0]["address"] = "10.20.0.77"
    depot = DepotBoosts()
    metriques = MetriquesMinimales(
        [
            {
                "pppoe_login": "dupont",
                "pop_name": "PoP Test",
                "plan_down_mbps": 100,
                "plan_up_mbps": 20,
                "last_ip": "10.20.0.10",
            }
        ]
    )
    service = make_service(settings, routeur, repository=depot, metrics=metriques)
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert [a.address for a in abonnes] == ["10.20.0.77"]
    assert abonnes[0].queue_target() == "10.20.0.77/32"


async def test_un_abonne_sans_session_n_est_pas_shape(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.active.clear()
    depot = DepotBoosts()
    metriques = MetriquesMinimales(
        [
            {
                "pppoe_login": "parti",
                "pop_name": "PoP Test",
                "plan_down_mbps": 100,
                "plan_up_mbps": 20,
            }
        ]
    )
    service = make_service(settings, routeur, repository=depot, metrics=metriques)
    await service.registry.reload()

    plan = await service.plan_router("pop-test")

    files = [a for a in plan.actions if a.path == "/queue/simple"]
    assert files == []
    assert [(s.login, "hors ligne" in s.reason) for s in plan.skipped] == [("parti", True)]


async def test_une_session_inconnue_de_la_base_est_shapee_si_surchargee(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Abonne apparu entre deux cycles de collecte : une bride posee a la main
    doit prendre effet tout de suite, pas au prochain tour."""
    routeur.add_session("nouveau", address="10.20.0.55")

    class DepotSurcharge(DepotBoosts):
        async def policy_map(self, scope):
            if scope != "subscriber":
                return {}
            return {"nouveau": {"max_down_mbps": 5.0, "max_up_mbps": 1.0, "enabled": True}}

    service = make_service(
        settings, routeur, repository=DepotSurcharge(), metrics=MetriquesMinimales([])
    )
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert [a.login for a in abonnes] == ["nouveau"]
    assert abonnes[0].queue_target() == "10.20.0.55/32"
    assert abonnes[0].effective_down == 5.0


async def test_une_lecture_de_sessions_en_echec_ne_produit_pas_de_plan(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Le pire scenario : /ppp/active tombe, tout le monde parait hors ligne, et
    la purge efface les files de tout le PoP. L'erreur doit remonter."""
    routeur.raise_on_ppp = TimeoutError("routeur muet")
    service = make_service(
        settings, routeur, repository=DepotBoosts(), metrics=MetriquesMinimales([])
    )
    await service.registry.reload()

    with pytest.raises((TimeoutError, Exception)):
        await service.build_targets("pop-test")


async def test_reconciliation_applique_la_limite_saisie(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Une limite saisie dans l'interface doit PLAFONNER sans intervention.

    Sans cette boucle elle reste une intention en base : elle n'atteint le
    routeur que si quelqu'un pense a demander un plan puis a l'appliquer."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    depot = DepotBoosts()
    # Surcharge posee a la main : 100 kbps dans les deux sens.
    depot.policy_map = lambda scope: _politiques(  # type: ignore[assignment]
        scope, {"dupont": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}}
    )
    ecriture = FauxClientEcriture()
    service = make_service(
        settings, routeur, repository=depot, write_client_factory=lambda c: ecriture
    )
    service.metrics = MetriquesMinimales(
        [{"pppoe_login": "dupont", "pop_name": "PoP Test", "plan_down_mbps": 100}]
    )
    await service.registry.reload()

    resultat = await service.reconcile()

    assert resultat["enabled"] is True
    assert resultat["routers"] == ["pop-test"]
    commandes = [a.command for a in ecriture.executed]
    # 100 kbps = 100000 bits/s, dans l'ordre montant/descendant.
    assert any("max-limit=100000/100000" in c for c in commandes)


async def test_reconciliation_ne_purge_jamais(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Meme garde-fou que pour les boosts : ce job ecrit sans revue humaine, un
    /ppp/active vide ne doit pas lui faire supprimer toutes les files du PoP."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    routeur.active.clear()
    routeur.simple_queue_rows = [
        {
            ".id": "*1",
            "name": "freeqos-dupont",
            "target": "10.20.0.10/32",
            "max-limit": "20M/100M",
            "comment": MANAGED_COMMENT,
        }
    ]
    ecriture = FauxClientEcriture()
    service = make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        metrics=MetriquesMinimales([]),
        write_client_factory=lambda c: ecriture,
    )
    await service.registry.reload()

    await service.reconcile()

    assert not [a for a in ecriture.executed if a.verb == "remove"]


async def test_reconciliation_n_ecrit_rien_en_lecture_seule(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """ENFORCEMENT_ENABLED reste le dernier rempart, boucle automatique ou pas."""
    settings.enforcement_enabled = False
    ecriture = FauxClientEcriture()
    service = make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        metrics=MetriquesMinimales([]),
        write_client_factory=lambda c: ecriture,
    )
    await service.registry.reload()

    resultat = await service.reconcile()

    assert resultat["enabled"] is False
    assert resultat["routers"] == [] and not ecriture.executed


async def test_un_routeur_injoignable_n_arrete_pas_la_reconciliation(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Un PoP en panne ne doit pas laisser les autres sans limite appliquee."""
    settings.enforcement_enabled = True
    service = make_service(
        settings, routeur, repository=DepotBoosts(), metrics=MetriquesMinimales([])
    )
    await service.registry.reload()
    routeur.raise_on_ppp = TimeoutError("routeur muet")

    resultat = await service.reconcile()

    assert resultat["routers"] == []
    assert resultat["errors"] and "pop-test" in resultat["errors"][0]


async def _politiques(scope: str, abonnes: dict) -> dict:
    return abonnes if scope == "subscriber" else {}


async def test_une_application_automatique_ne_purge_jamais(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Un job qui ecrit sans revue humaine n'a aucune raison de SUPPRIMER. Sans
    ce garde-fou, une coupure momentanee de /ppp/active suffirait a effacer
    toutes les files du PoP."""
    routeur.active.clear()
    routeur.simple_queue_rows = [
        {
            ".id": "*1",
            "name": "freeqos-parti",
            "target": "10.20.0.10/32",
            "max-limit": "20M/100M",
            "comment": MANAGED_COMMENT,
        }
    ]
    service = make_service(
        settings, routeur, repository=DepotBoosts(), metrics=MetriquesMinimales([])
    )
    await service.registry.reload()

    avec_purge = await service.plan_router("pop-test")
    sans_purge = await service.plan_router("pop-test", prune=False)

    assert avec_purge.counts()["remove"] == 1
    assert sans_purge.counts()["remove"] == 0

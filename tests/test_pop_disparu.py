"""Un PoP connu ne doit JAMAIS disparaitre sans explication.

LE DIAGNOSTIC QUI A MENE ICI
-----------------------------
Deux symptomes -- un PoP absent de l'arbre, un client VLAN jamais detecte --
avaient bien une cause commune, mais pas celle qu'on croyait. L'hypothese de
depart visait le lien manquant vers le PoP dans ``attach_vlan_candidates``. Elle
est fausse : un noeud sans lien reste affiche, il devient racine (verifie dans
tests/test_topologie_arbre_js.py).

La vraie cause est UN CRAN PLUS HAUT. ``registry.collectors`` alimente TOUT :

    pas de collecteur
      -> discover() ne pose aucune case (le repli "injoignable" est DANS la
         boucle sur les collecteurs, il ne peut donc pas s'y substituer)
      -> detect_vlan_clients() ne lit aucune table ARP
      -> aucune observation, donc aucun candidat dans l'onglet Abonnes

Un seul point de defaillance, deux symptomes. Et il etait silencieux : le depot
ecartait la fiche avec un simple log serveur.
"""

from __future__ import annotations

from app.collectors.topology import KIND_CORE, KIND_POP
from app.config import RouterConfig, Settings
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient


class DepotEcartes:
    """Depot qui ecarte certaines fiches, comme le vrai le fait."""

    def __init__(self, configs: list[RouterConfig], ecartes: list[dict[str, str]]) -> None:
        self._configs = configs
        self._ecartes = ecartes
        self.explose = False

    async def load_configs_with_report(self, *, enabled_only: bool = True):
        if self.explose:
            raise RuntimeError("base injoignable")
        return list(self._configs), list(self._ecartes)

    async def load_configs(self, *, enabled_only: bool = True):
        configs, _ = await self.load_configs_with_report()
        return configs

    async def find_id_by_name(self, name: str) -> int | None:
        return 1

    async def hidden_file_routers(self) -> set[str]:
        return set()


class DepotAncien:
    """Depot d'une generation precedente : il n'expose que ``load_configs``."""

    def __init__(self, configs: list[RouterConfig]) -> None:
        self._configs = configs

    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        return list(self._configs)

    async def find_id_by_name(self, name: str) -> int | None:
        return 1

    async def hidden_file_routers(self) -> set[str]:
        return set()


def _settings(routers: list[RouterConfig] | None = None) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=routers or [],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )


ECARTE = {
    "name": "pop-nord",
    "reason": "secret illisible : cle changee",
    "source": "db",
    "host": "10.10.0.10",
    "pop_name": "PoP Nord",
    "role": "pop",
}


# =========================================================================
# 1. L'ecart remonte jusqu'a l'interface
# =========================================================================


async def test_une_fiche_ecartee_remonte_dans_skipped() -> None:
    """LE TROU D'ORIGINE : ``registry.skipped`` ne portait que les routeurs du
    FICHIER. Une fiche en base ecartee ne figurait nulle part hors du log."""
    registre = RouterRegistry(_settings(), repository=DepotEcartes([], [ECARTE]))

    await registre.reload()

    assert registre.collectors == []
    assert [e["name"] for e in registre.skipped] == ["pop-nord"]
    # De quoi l'AFFICHER, et poser sa case dans l'arbre.
    ecarte = registre.skipped[0]
    assert ecarte["source"] == "db"
    assert ecarte["host"] == "10.10.0.10"
    assert ecarte["pop_name"] == "PoP Nord"
    assert ecarte["role"] == "pop"


async def test_une_base_illisible_produit_un_signal_exploitable() -> None:
    """Avant, l'inventaire en base s'evaporait derriere un ``logger.exception``
    et l'exploitant voyait un controleur qui tourne avec zero PoP."""
    depot = DepotEcartes([], [])
    depot.explose = True
    registre = RouterRegistry(_settings(), repository=depot)

    await registre.reload()

    assert len(registre.skipped) == 1
    assert "inventaire en base illisible" in registre.skipped[0]["reason"]
    assert "absents de la collecte" in registre.skipped[0]["reason"]


async def test_un_depot_sans_la_variante_detaillee_fonctionne_encore() -> None:
    """On n'impose pas la nouvelle methode a un double de test ou a un depot
    d'une autre generation."""
    config = RouterConfig(name="pop", host="1.1.1.1", username="u", password="p")
    registre = RouterRegistry(_settings(), repository=DepotAncien([config]))

    collecteurs = await registre.reload()

    assert [c.name for c in collecteurs] == ["pop"]
    assert registre.skipped == []


# =========================================================================
# 2. L'arbre : jamais absent
# =========================================================================


async def _decouvrir(registre: RouterRegistry) -> object:
    service = ShapingService(_settings(), registry=registre)
    await registre.reload()
    return await service.discover()


async def test_un_pop_ecarte_garde_sa_case_dans_l_arbre() -> None:
    """CRITERE DE SORTIE. Sans collecteur, la boucle de decouverte ne pouvait
    poser aucune case -- pas meme celle marquee "injoignable", qui est DANS
    cette boucle. Le PoP disparaissait, et rien ne distingue alors un PoP
    efface d'un PoP qui n'a jamais existe."""
    registre = RouterRegistry(_settings(), repository=DepotEcartes([], [ECARTE]))

    snapshot = await _decouvrir(registre)

    noeud = snapshot.nodes["router:pop-nord"]
    assert noeud.name == "PoP Nord"
    assert noeud.kind == KIND_POP
    assert noeud.address == "10.10.0.10"
    assert noeud.attributes["excluded"] is True
    assert noeud.attributes["managed"] is True
    assert "secret illisible" in noeud.attributes["error"]
    assert any("ecarte de la collecte" in a for a in snapshot.warnings)


async def test_un_coeur_ecarte_garde_son_role() -> None:
    """Sinon l'arbre se reorganiserait autour de l'incident : le coeur
    retomberait en PoP et la hierarchie changerait de forme."""
    registre = RouterRegistry(
        _settings(), repository=DepotEcartes([], [{**ECARTE, "role": "core"}])
    )

    snapshot = await _decouvrir(registre)

    assert snapshot.nodes["router:pop-nord"].kind == KIND_CORE


async def test_un_routeur_masque_ne_reapparait_pas() -> None:
    """Le masquage est VOULU : "Retirer" doit retirer, y compris de l'arbre.
    Le correctif ne doit pas ressusciter ce que l'exploitant a ecarte."""

    class DepotMasquant(DepotEcartes):
        async def hidden_file_routers(self) -> set[str]:
            return {"pop-nord"}

    registre = RouterRegistry(_settings(), repository=DepotMasquant([], [ECARTE]))

    snapshot = await _decouvrir(registre)

    assert registre.skipped == []
    assert "router:pop-nord" not in snapshot.nodes


async def test_une_panne_globale_ne_pose_pas_de_case_fantome() -> None:
    """L'entree sans nom decrit l'inventaire, pas un equipement : en faire une
    case poserait un routeur qui n'existe pas."""
    depot = DepotEcartes([], [])
    depot.explose = True
    registre = RouterRegistry(_settings(), repository=depot)

    snapshot = await _decouvrir(registre)

    assert snapshot.nodes == {}


# =========================================================================
# 3. La cause commune : pas de collecteur, pas d'ARP
# =========================================================================


async def test_sans_collecteur_aucune_detection_arp() -> None:
    """LE LIEN ENTRE LES DEUX SYMPTOMES.

    L'onglet Abonnes lit ``vlan_sightings``, rempli par un job qui itere sur
    ``collectors``. Il ne depend NI de l'arbre, NI de attach_vlan_candidates :
    un PoP ecarte ne produit donc aucune observation, et son client VLAN reste
    invisible meme si l'arbre etait parfait.
    """
    from app.collectors.radius import MockPlanProvider
    from app.collectors.uisp import MockBackhaulProvider
    from app.db.directory import InMemoryDirectory
    from app.db.writer import InMemoryMetricsWriter
    from app.services.collection import CollectionService
    from tests.test_detection_vlan import DepotObservations

    registre = RouterRegistry(_settings(), repository=DepotEcartes([], [ECARTE]))
    await registre.reload()
    depot = DepotObservations()
    service = CollectionService(
        _settings(),
        collectors=registre.collectors,
        backhaul_provider=MockBackhaulProvider(),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=InMemoryMetricsWriter(),
        backhauls=[],
        sightings=depot,
    )

    resultat = await service.detect_vlan_clients()

    # Le job "reussit" -- il n'a simplement rien a lire. C'est bien pour cela
    # que le symptome etait muet.
    assert resultat.ok
    assert depot.enregistrees == []


async def test_un_collecteur_present_produit_bien_une_observation() -> None:
    """Le controle inverse : la detection ARP n'est pas cassee en elle-meme."""
    from app.collectors.radius import MockPlanProvider
    from app.collectors.uisp import MockBackhaulProvider
    from app.db.directory import InMemoryDirectory
    from app.db.writer import InMemoryMetricsWriter
    from app.services.collection import CollectionService
    from tests.test_detection_vlan import DepotObservations

    client = FakeRouterOsClient(identity="pop-nord")
    client.vlan_rows = [{"name": "vlan120", "vlan-id": "120"}]
    client.arp_rows = [
        {"address": "10.20.0.77", "mac-address": "AA:BB:CC:00:00:77", "interface": "vlan120"}
    ]
    config = RouterConfig(
        name="pop-nord", host="10.10.0.10", username="u", password="p", pop_name="PoP Nord"
    )
    registre = RouterRegistry(
        _settings(), repository=DepotEcartes([config], []), client_factory=lambda c: client
    )
    await registre.reload()
    depot = DepotObservations()
    service = CollectionService(
        _settings(),
        collectors=registre.collectors,
        backhaul_provider=MockBackhaulProvider(),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=InMemoryMetricsWriter(),
        backhauls=[],
        sightings=depot,
    )

    await service.detect_vlan_clients()

    assert [v.address for v in depot.enregistrees] == ["10.20.0.77"]
    assert depot.enregistrees[0].pop_name == "PoP Nord"

"""Rattachement d'un abonne a son secteur radio, de la decouverte a la base.

CE QUE CE FICHIER VERROUILLE, ET POURQUOI IL EXISTE
---------------------------------------------------
Toute la chaine etait ecrite et testee piece par piece -- la jointure
``map_subscribers_to_sectors``, le depot ``save_attachments``, le
planificateur qui lit ``attachments()``, la boucle QoE qui groupe par secteur --
mais PERSONNE NE LES RELIAIT. ``save_attachments`` n'avait aucun appelant, la
table ``subscriber_attachments`` restait donc vide en permanence, et deux
fonctionnalites majeures en dependaient sans jamais pouvoir s'exercer :

  - les files d'abonnes PPPoE etaient posees SANS PARENT, donc hors de
    l'enveloppe du backhaul -- le goulot n2 du README n'etait jamais tenu ;
  - la boucle fermee QoE (phase 4) ne trouvait jamais un seul secteur a
    evaluer, quelle que soit la degradation mesuree.

Les tests unitaires de chaque piece passaient tous. C'est le CABLAGE qui
manquait, et c'est donc lui que ces tests tiennent.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.collectors.topology import TopologySnapshot
from app.config import RouterConfig, Settings
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService, discover_with_devices
from tests.conftest import FakeRouterOsClient

AP_MAC = "DC:9F:DB:11:22:33"
CPE_ALICE = "24:A4:3C:AA:00:01"
CPE_BOB = "24:A4:3C:AA:00:02"


class DepotRattachements:
    """Depot de topologie qui RETIENT ce qu'on lui demande d'ecrire."""

    def __init__(self) -> None:
        self.snapshots = 0
        self.rattachements: dict[str, tuple[str, str | None]] = {}

    async def save_snapshot(self, snapshot: TopologySnapshot) -> dict[str, int]:
        self.snapshots += 1
        return {"nodes": len(snapshot.nodes), "links": len(snapshot.links)}

    async def save_attachments(self, attachments: dict[str, tuple[str, str | None]]) -> int:
        self.rattachements.update(attachments)
        return len(attachments)

    async def aliases(self) -> dict[str, str]:
        return {}


class FauxClientsStatiques:
    def __init__(self, clients: list[Any] | None = None) -> None:
        self._clients = clients or []

    async def load_enabled(self) -> list[Any]:
        return list(self._clients)


class FicheStatique:
    """Juste ce que ``attach_static_clients`` lit d'une fiche."""

    def __init__(self, reference: str, *, sector_key: str | None, pop_name: str) -> None:
        self.reference = reference
        self.display_name = reference
        self.sector_key = sector_key
        self.pop_name = pop_name
        self.address = "10.20.5.50/32"
        self.vlan = None
        self.plan_down_mbps = 100.0
        self.plan_up_mbps = 20.0


def _pop_avec_abonnes(*, callers: dict[str, str]) -> FakeRouterOsClient:
    """Un PoP qui voit une radio sur ether2 et porte des sessions PPPoE."""
    client = FakeRouterOsClient(identity="pop-1")
    for login, mac in callers.items():
        client.add_session(login)
        client.active[-1]["caller-id"] = mac
    client.neighbor_rows = [
        {
            "interface": "ether2",
            "identity": "sector-a",
            "mac-address": AP_MAC,
            "platform": "Ubiquiti Networks Inc.",
        }
    ]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    client.address_rows = [{"address": "10.255.0.1/32", "interface": "lo"}]
    return client


def _service(client: FakeRouterOsClient, **kwargs: Any) -> ShapingService:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[
            RouterConfig(
                name="pop-1",
                host="10.0.0.1",
                username="u",
                password=SecretStr("p"),
                role="pop",
                pop_name="Site 1",
            )
        ],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    registry = RouterRegistry(settings, client_factory=lambda cfg: client)
    return ShapingService(settings, registry=registry, **kwargs)


def _stations_uisp() -> list[dict[str, Any]]:
    """L'AP, puis les deux CPE declares sous lui -- ce que rend /devices."""
    return [
        {"identification": {"id": "ap-1", "mac": AP_MAC, "name": "Secteur Nord", "role": "ap"}},
        {
            "identification": {"id": "sta-a", "mac": CPE_ALICE, "role": "station"},
            "attributes": {"apDevice": {"id": "ap-1"}},
        },
        {
            "identification": {"id": "sta-b", "mac": CPE_BOB, "role": "station"},
            "attributes": {"apDevice": {"id": "ap-1"}},
        },
    ]


# ---------------------------------------------------------------- la jointure
async def test_la_decouverte_rattache_les_abonnes_a_leur_secteur() -> None:
    """La jointure caller-id <-> station UISP tourne VRAIMENT a la decouverte.

    Elle avait un appelant nulle part : la fonctionnalite paraissait presente,
    aucun abonne n'etait jamais rattache."""
    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE, "bob": CPE_BOB})
    service = _service(client, repository=DepotRattachements())
    await service.registry.reload()

    snapshot = await service.discover(uisp_devices=_stations_uisp())

    assert set(snapshot.subscriber_sectors) == {"alice", "bob"}


async def test_le_secteur_designe_un_noeud_QUI_EXISTE_dans_le_graphe() -> None:
    """Le piege : l'AP est aussi vu en voisin MNDP, donc sa case garde sa cle
    ``mac:...``. Poser ``uisp:<id>`` designait un noeud absent -- le
    rattachement etait ecrit et ne servait a rien, ni au planificateur (qui
    cherche le lien desservant ce secteur) ni a la boucle QoE."""
    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE})
    service = _service(client, repository=DepotRattachements())
    await service.registry.reload()

    snapshot = await service.discover(uisp_devices=_stations_uisp())

    secteur = snapshot.subscriber_sectors["alice"]
    assert secteur in snapshot.nodes
    assert secteur == f"mac:{AP_MAC}"


async def test_un_cpe_inconnu_n_est_jamais_rattache_de_force() -> None:
    """Ne pas savoir vaut mieux que deviner : une file posee sous le mauvais
    secteur bride l'abonne avec des voisins qu'il ne partage pas."""
    client = _pop_avec_abonnes(callers={"inconnu": "00:11:22:33:44:55"})
    service = _service(client, repository=DepotRattachements())
    await service.registry.reload()

    snapshot = await service.discover(uisp_devices=_stations_uisp())

    assert snapshot.subscriber_sectors == {}
    assert any("caller-id" in avertissement for avertissement in snapshot.warnings)


async def test_sans_uisp_la_radio_du_graphe_sert_de_secteur() -> None:
    """Mode airOS ou radios lues en direct : aucun ``apDevice`` n'existe. Le
    rattachement doit rester possible quand la MAC du CPE est celle d'un
    equipement deja pose, sinon ces exploitants n'en ont jamais aucun."""
    client = _pop_avec_abonnes(callers={"alice": AP_MAC})
    service = _service(client, repository=DepotRattachements())
    await service.registry.reload()

    snapshot = await service.discover()

    assert snapshot.subscriber_sectors == {"alice": f"mac:{AP_MAC}"}


# ------------------------------------------------------------- la persistance
async def test_les_rattachements_sont_ECRITS_en_base() -> None:
    """LE test de ce fichier. ``save_attachments`` n'avait aucun appelant, donc
    le planificateur et la boucle QoE lisaient une table vide a perpetuite."""
    depot = DepotRattachements()
    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE, "bob": CPE_BOB})
    service = _service(client, repository=depot)
    await service.registry.reload()

    await service.discover(uisp_devices=_stations_uisp())

    assert set(depot.rattachements) == {"alice", "bob"}
    assert all(secteur == f"mac:{AP_MAC}" for secteur, _ in depot.rattachements.values())


async def test_la_mac_du_cpe_accompagne_le_rattachement() -> None:
    """Elle dit D'OU vient le rattachement : un abonne PPPoE observe se
    distingue ainsi d'un client statique declare."""
    depot = DepotRattachements()
    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE})
    service = _service(client, repository=depot)
    await service.registry.reload()

    await service.discover(uisp_devices=_stations_uisp())

    assert depot.rattachements["alice"][1] == CPE_ALICE


async def test_un_client_statique_declare_est_persiste_lui_aussi() -> None:
    """Les deux natures passent par le meme index, donc par la meme ecriture :
    le rattachement declare d'un client a IP fixe ne doit pas rester en memoire."""
    depot = DepotRattachements()
    client = _pop_avec_abonnes(callers={})
    fiche = FicheStatique("mairie", sector_key=f"mac:{AP_MAC}", pop_name="Site 1")
    service = _service(client, repository=depot, static_clients=FauxClientsStatiques([fiche]))
    await service.registry.reload()

    await service.discover()

    assert depot.rattachements["mairie"] == (f"mac:{AP_MAC}", None)


async def test_rien_a_ecrire_n_appelle_pas_le_depot() -> None:
    """Un reseau sans abonne rattache ne doit pas ecrire une table vide."""
    depot = DepotRattachements()
    client = _pop_avec_abonnes(callers={})
    service = _service(client, repository=depot)
    await service.registry.reload()

    await service.discover()

    assert depot.rattachements == {}
    assert depot.snapshots == 1


async def test_un_depot_sans_cette_capacite_ne_casse_pas_la_decouverte() -> None:
    """Le graphe doit survivre a un depot d'une generation anterieure."""

    class DepotAncien:
        async def save_snapshot(self, snapshot: TopologySnapshot) -> dict[str, int]:
            return {"nodes": len(snapshot.nodes), "links": len(snapshot.links)}

    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE})
    service = _service(client, repository=DepotAncien())
    await service.registry.reload()

    snapshot = await service.discover(uisp_devices=_stations_uisp())

    assert snapshot.subscriber_sectors == {"alice": f"mac:{AP_MAC}"}


async def test_le_chemin_du_job_periodique_rattache_aussi() -> None:
    """Le bouton 'Relancer la decouverte' et le job passent tous deux par
    ``discover_with_devices`` : la jointure doit vivre SOUS ce chemin, pas a
    cote, sinon elle ne tourne que dans un cas sur deux."""

    class FournisseurUisp:
        async def raw_devices(self) -> list[dict[str, Any]]:
            return _stations_uisp()

    depot = DepotRattachements()
    client = _pop_avec_abonnes(callers={"alice": CPE_ALICE})
    service = _service(client, repository=depot)
    await service.registry.reload()

    await discover_with_devices(service, (FournisseurUisp(),))

    assert depot.rattachements["alice"][0] == f"mac:{AP_MAC}"

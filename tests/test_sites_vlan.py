"""Un VLAN qui porte des clients est un site.

Chez un operateur radio, un VLAN porte un village ou un relais ; le routeur
n'en est que la tete. Tant que seul le site du ROUTEUR existait, tous les
clients de tous les VLAN d'un meme NAS tombaient dans un seul sac.

Le risque de cette reconnaissance est nomme et teste ici : un abonne range dans
un site de VLAN doit continuer d'etre rattache a son routeur, sinon il cesse
silencieusement d'etre shape -- et un abonne qu'on croit bride sans qu'il le
soit est pire qu'un abonne qu'on sait non bride.
"""

from __future__ import annotations

import pytest

from app.models import StaticClient, VlanSighting
from app.services.vlan_sites import (
    SITE_VLAN,
    VlanSite,
    routers_for_site,
    site_name,
    sites_from,
)


# ------------------------------------------------------------- nommage
@pytest.mark.parametrize(
    ("interface", "attendu"),
    [
        # Le nom vient de l'interface : c'est celui que l'exploitant a ecrit.
        ("vlan-francophonie", "Francophonie"),
        ("vlan_mairie", "Mairie"),
        ("vlan-zone-nord", "Zone Nord"),
        ("francophonie-vlan", "Francophonie"),
        # Un VLAN qui n'a qu'un numero garde son numero : inventer un nom serait
        # pire que de ne rien dire.
        ("vlan101", "VLAN 101"),
        ("vlan-101", "VLAN 101"),
        ("vlan.230", "VLAN 230"),
        # Sous-interface 802.1Q : 'ether1' ne designe pas un site, le tag si.
        ("ether1.101", "VLAN 101"),
        ("sfp-sfpplus1.230", "VLAN 230"),
    ],
)
def test_le_nom_du_site_vient_de_l_interface(interface: str, attendu: str) -> None:
    assert site_name(interface) == attendu


def test_un_sigle_reste_un_sigle() -> None:
    """'CCR' ne doit pas devenir 'Ccr' : c'est un nom d'equipement, pas un mot."""
    assert site_name("vlan-CCR") == "CCR"


def test_sans_interface_le_numero_suffit() -> None:
    assert site_name(None, 101) == "VLAN 101"
    assert site_name("", 101) == "VLAN 101"


def test_sans_rien_on_ne_dit_rien() -> None:
    """Pas de site invente a partir de rien."""
    assert site_name(None, None) is None
    assert site_name("  ") is None


# ------------------------------------------------- quels VLAN deviennent sites
def _vue(interface: str, *, tag: int | None = 101, routeur: str = "NAS-1") -> VlanSighting:
    return VlanSighting(
        router_name=routeur,
        pop_name="BASSORA",
        address="172.16.25.10",
        vlan_interface=interface,
        vlan_id=tag,
    )


def test_un_vlan_qui_porte_un_client_devient_un_site() -> None:
    sites = sites_from([_vue("vlan-francophonie")])

    assert [s.name for s in sites] == ["Francophonie"]
    assert sites[0].router_name == "NAS-1"
    assert sites[0].vlan_id == 101


def test_un_vlan_sans_client_ne_cree_rien() -> None:
    """Gestion, transit, supervision : une liste de sites ou l'on ne reconnait
    plus ses sites ne sert plus a rien."""
    assert sites_from([], []) == []


def test_les_clients_du_meme_vlan_se_comptent_sur_un_seul_site() -> None:
    sites = sites_from([_vue("vlan-francophonie"), _vue("vlan-francophonie")])

    assert len(sites) == 1
    assert sites[0].subscribers == 2


def test_deux_routeurs_portant_le_meme_nom_de_vlan_font_deux_sites() -> None:
    """Le VLAN 101 de BASSORA et celui de TALLADJE ne sont pas le meme lieu."""
    sites = sites_from([_vue("vlan101", routeur="NAS-1"), _vue("vlan101", routeur="NAS-2")])

    assert {s.router_name for s in sites} == {"NAS-1", "NAS-2"}
    assert len(sites) == 2


def test_un_client_declare_fait_exister_son_site_meme_silencieux() -> None:
    """Sinon le site clignoterait au gre des coupures, et une liste de PoP qui
    clignote ne se lit pas."""
    client = StaticClient(reference="mairie", pop_name="BASSORA", address="10.0.0.8/29", vlan=101)

    sites = sites_from([], [{"reference": "mairie", "vlan": 101, "router_name": "NAS-1"}])

    assert [s.name for s in sites] == ["VLAN 101"]
    assert client.vlan == 101


def test_le_client_declare_herite_du_nom_vu_sur_le_terrain() -> None:
    """Sa fiche n'a qu'un numero ; le terrain connait le nom. Deux sites pour le
    meme VLAN seraient une coupure en deux de la meme realite."""
    sites = sites_from(
        [_vue("vlan-francophonie", tag=101)],
        [{"reference": "mairie", "vlan": 101, "router_name": "NAS-1"}],
    )

    assert [s.name for s in sites] == ["Francophonie"]
    assert sites[0].subscribers == 2


# ------------------------------------------- le site doit ramener a son routeur
def test_un_site_de_vlan_dit_quel_routeur_le_dessert() -> None:
    """C'EST LE POINT CRITIQUE. Un site qui ne nommerait pas son routeur serait
    un site dont les abonnes ne seraient jamais shapes."""
    sites = sites_from([_vue("vlan-francophonie")])

    assert routers_for_site("Francophonie", sites) == ["NAS-1"]


def test_le_rapprochement_ignore_casse_et_accents() -> None:
    """Meme tolerance que pour les PoP de routeurs : 'mediatheque' saisi a la
    main et 'Médiathèque' vu sur le routeur designent le meme lieu."""
    sites = [VlanSite(name="Médiathèque", router_name="NAS-1", vlan_interface="vlan-mediatheque")]

    assert routers_for_site("mediatheque", sites) == ["NAS-1"]
    assert routers_for_site("MEDIATHEQUE", sites) == ["NAS-1"]


def test_un_nom_inconnu_ne_ramene_aucun_routeur() -> None:
    """La tolerance ne doit pas devenir un fourre-tout."""
    sites = sites_from([_vue("vlan-francophonie")])

    assert routers_for_site("Bassora", sites) == []
    assert routers_for_site("", sites) == []


def test_le_site_se_declare_comme_venant_d_un_vlan() -> None:
    """L'interface doit pouvoir distinguer un site de VLAN d'un site de routeur."""
    (site,) = sites_from([_vue("vlan-francophonie")])

    assert site.to_dict()["kind"] == SITE_VLAN
    assert site.to_dict()["vlan_interface"] == "vlan-francophonie"


# =========================================================================
# Le shaping doit suivre : un site de VLAN ramene a son routeur
# =========================================================================
#
# LE RISQUE DE CETTE FONCTIONNALITE, NOMME ET VERROUILLE. Ranger un abonne dans
# un site de VLAN change son ``pop_name``. Or le rapprochement abonne -> routeur
# se faisait par EGALITE de ce nom avec le PoP du routeur. Tel quel, reconnaitre
# les VLAN aurait fait sortir ces abonnes de l'etat desire : plus de file, pas
# d'erreur, et une interface qui continue d'afficher leur plafond. Un abonne
# qu'on croit bride sans qu'il le soit est pire qu'un abonne qu'on sait non
# bride.

from app.config import Settings  # noqa: E402
from app.models import KIND_STATIC  # noqa: E402
from tests.conftest import FakeRouterOsClient  # noqa: E402
from tests.test_shaping_service import (  # noqa: E402
    DepotBoosts,
    MetriquesMinimales,
    make_service,
)


class MetriquesAvecSites(MetriquesMinimales):
    """Metriques qui portent aussi le referentiel des sites."""

    def __init__(self, abonnes: list[dict], sites: list[dict] | None = None) -> None:
        super().__init__(abonnes)
        self.sites = sites or []

    async def pop_sites(self):  # type: ignore[no-untyped-def]
        return self.sites


def _service(settings: Settings, routeur: FakeRouterOsClient, metrics):  # type: ignore[no-untyped-def]
    return make_service(settings, routeur, repository=DepotBoosts(), metrics=metrics)


async def test_un_abonne_range_dans_un_site_de_vlan_reste_shape(
    settings: Settings, fake_client: FakeRouterOsClient
) -> None:
    """Son PoP n'est plus celui du routeur ; le referentiel dit que ce site est
    desservi par ce routeur, et cela suffit."""
    fake_client.add_session("mairie", rx_byte=0, tx_byte=0)
    metrics = MetriquesAvecSites(
        [
            {
                "login": "mairie",
                "pop_name": "Francophonie",
                "plan_down_mbps": 50.0,
                "plan_up_mbps": 10.0,
                "kind": "pppoe",
            }
        ],
        [{"name": "Francophonie", "kind": "vlan", "router_name": "pop-test", "vlan_id": 101}],
    )
    service = _service(settings, fake_client, metrics)
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert [a.login for a in abonnes] == ["mairie"]


async def test_un_site_de_vlan_d_un_autre_routeur_ne_suit_pas(
    settings: Settings, fake_client: FakeRouterOsClient
) -> None:
    """La reconnaissance des VLAN ne doit pas devenir un fourre-tout : le VLAN
    101 de BASSORA n'est pas celui de TALLADJE."""
    metrics = MetriquesAvecSites(
        [{"login": "mairie", "pop_name": "Francophonie", "kind": "pppoe"}],
        [{"name": "Francophonie", "kind": "vlan", "router_name": "un-autre-nas", "vlan_id": 101}],
    )
    service = _service(settings, fake_client, metrics)
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert abonnes == []


async def test_le_site_du_routeur_marche_toujours_sans_referentiel(
    settings: Settings, fake_client: FakeRouterOsClient
) -> None:
    """Une base muette ne doit pas faire disparaitre des abonnes : on retombe
    sur le rapprochement par nom, c'est-a-dire sur le comportement d'avant."""
    fake_client.add_session("dupont", rx_byte=0, tx_byte=0)
    metrics = MetriquesAvecSites(
        [{"login": "dupont", "pop_name": "PoP Test", "plan_down_mbps": 100.0, "kind": "pppoe"}],
        [],
    )
    service = _service(settings, fake_client, metrics)
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert [a.login for a in abonnes] == ["dupont"]


async def test_poser_un_plafond_trouve_le_routeur_d_un_site_de_vlan(
    settings: Settings, fake_client: FakeRouterOsClient
) -> None:
    """Sinon fixer une limite repondrait 'aucun routeur ne porte cet abonne' --
    le meme silence qu'on a deja repare une fois pour les PoP mal ecrits."""
    metrics = MetriquesAvecSites(
        [{"login": "mairie", "pop_name": "Francophonie", "kind": KIND_STATIC}],
        [{"name": "Francophonie", "kind": "vlan", "router_name": "pop-test", "vlan_id": 101}],
    )
    service = _service(settings, fake_client, metrics)
    await service.registry.reload()

    routeurs = await service._routers_for_logins({"mairie"})  # noqa: SLF001

    assert routeurs == ["pop-test"]


async def test_un_site_dont_le_routeur_a_disparu_ne_ment_pas(
    settings: Settings, fake_client: FakeRouterOsClient
) -> None:
    """Un site qui nomme un routeur retire de l'inventaire ne doit pas le rendre
    : on prefere 'aucun routeur' a un nom qui ne repond plus."""
    metrics = MetriquesAvecSites(
        [{"login": "mairie", "pop_name": "Francophonie", "kind": KIND_STATIC}],
        [{"name": "Francophonie", "kind": "vlan", "router_name": "nas-demonte", "vlan_id": 101}],
    )
    service = _service(settings, fake_client, metrics)
    await service.registry.reload()

    assert await service._routers_for_logins({"mairie"}) == []  # noqa: SLF001

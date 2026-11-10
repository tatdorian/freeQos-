"""Un client sur VLAN, PLUSIEURS plages d'adresses, UNE seule file.

D'ordinaire un client VLAN a un seul pool. Certains en ont plusieurs sur la
meme VLAN (un bloc public ajoute, un pool de service). Le contrat reste UN
debit souscrit : toutes les plages doivent partager une seule file, dont la
cible RouterOS les liste (``target=10.0.0.0/29,10.0.1.0/28``).

Ce fichier verrouille :
  1. la saisie : IP, bloc, ou plage de pool MikroTik (``a-b``) ;
  2. la cible : une liste canonique, stable d'un cycle a l'autre ;
  3. le plan : une file, un parent qui contient TOUT, pas de doublon ;
  4. le client seul sur sa VLAN : TOUS les reseaux de la VLAN ;
  5. l'API et la reconnaissance des adresses vues.
"""

from __future__ import annotations

import ipaddress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.api.pop_census import _rapprocher
from app.config import Settings
from app.db.static_clients_repo import (
    InvalidStaticClientError,
    normalise_address,
    normalise_extra_prefixes,
    split_address,
    to_model,
)
from app.enforcement.models import prefixes_target
from app.enforcement.planner import (
    LinkTarget,
    SubscriberTarget,
    build_plan,
    desired_state,
)
from app.main import register_routes
from app.models import KIND_STATIC
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_clients_statiques import (
    InventaireApi,
    build_service,
    fiche,
    routeur_vlan,
    service_avec_inventaire,
)


@pytest.fixture(autouse=True)
def _reseaux_connectes_isoles():
    """Les reseaux connectes sont un etat de module : rien ne doit fuir."""
    from app.collectors import mikrotik

    avant = dict(mikrotik._RESEAUX_CONNECTES)
    yield
    mikrotik._RESEAUX_CONNECTES.clear()
    mikrotik._RESEAUX_CONNECTES.update(avant)


# =========================================================================
# 1. Saisie
# =========================================================================


def test_une_plage_de_pool_devient_des_blocs_exacts() -> None:
    """``/ip/pool ranges=10.0.1.10-10.0.1.17`` : 8 adresses, ni plus ni moins."""
    assert normalise_extra_prefixes(["10.0.1.10-10.0.1.17"]) == [
        "10.0.1.10/31",
        "10.0.1.12/30",
        "10.0.1.16/31",
    ]


def test_ip_bloc_et_plage_se_melangent_sans_doublon() -> None:
    assert normalise_extra_prefixes("10.0.1.0/28, 10.0.2.5\n10.0.1.0/28 10.0.3.0-10.0.3.255") == [
        "10.0.1.0/28",
        "10.0.2.5/32",
        "10.0.3.0/24",
    ]


def test_aucune_plage_n_est_une_liste_vide() -> None:
    assert normalise_extra_prefixes(None) == []
    assert normalise_extra_prefixes([]) == []
    assert normalise_extra_prefixes("  ") == []


@pytest.mark.parametrize(
    "saisie",
    [
        "10.0.1.50-10.0.1.10",  # a l'envers
        "10.0.1.1-2001:db8::1",  # deux familles
        "10.0.1.1-pas-une-ip",
        "127.0.0.1",
        "n'importe quoi",
    ],
)
def test_plages_refusees(saisie: str) -> None:
    with pytest.raises(InvalidStaticClientError):
        normalise_extra_prefixes([saisie])


def test_une_plage_avec_des_espaces_autour_du_tiret() -> None:
    """« 10.0.1.10 - 10.0.1.13 » : decoupee sur les espaces, la plage devenait
    trois morceaux, dont un « - » refuse avec un message incomprehensible."""
    attendu = ["10.0.1.10/31", "10.0.1.12/31"]
    assert normalise_extra_prefixes("10.0.1.10 - 10.0.1.13") == attendu
    # Le formulaire envoie une liste deja decoupee : meme resultat.
    assert normalise_extra_prefixes(["10.0.1.10", "-", "10.0.1.13"]) == attendu


def test_l_adresse_principale_accepte_une_plage() -> None:
    adresse, plages = split_address("10.0.0.10-10.0.0.13", None)
    assert (adresse, plages) == ("10.0.0.10/31", ["10.0.0.12/31"])


def test_l_adresse_principale_accepte_plusieurs_ip() -> None:
    adresse, plages = split_address("10.0.0.5, 10.0.0.6 10.0.0.9", ["10.0.1.0/28"])
    assert adresse == "10.0.0.5/32"
    assert plages == ["10.0.0.6/32", "10.0.0.9/32", "10.0.1.0/28"]


def test_une_adresse_seule_ne_change_rien() -> None:
    assert split_address("10.0.0.0/29", ["10.0.0.0/29", "10.0.1.5"]) == (
        "10.0.0.0/29",
        ["10.0.0.0/29", "10.0.1.5/32"],
    )
    assert split_address("2001:db8::1", None) == ("2001:db8::1/128", [])


def test_l_adresse_principale_n_est_pas_repetee_dans_les_plages() -> None:
    adresse, plages = split_address("10.0.0.5, 10.0.0.6", ["10.0.0.5", "10.0.0.6"])
    assert (adresse, plages) == ("10.0.0.5/32", ["10.0.0.6/32"])


@pytest.mark.parametrize("saisie", ["", "  ", "10.0.0.20-10.0.0.10", "pas une ip, 10.0.0.1"])
def test_une_adresse_principale_invalide_est_refusee(saisie: str) -> None:
    with pytest.raises(InvalidStaticClientError):
        split_address(saisie, None)


def test_trop_de_blocs_est_refuse() -> None:
    """Une plage enorme mal saisie ne doit pas produire une cible illisible."""
    with pytest.raises(InvalidStaticClientError, match="too many"):
        normalise_extra_prefixes([f"10.{i}.0.1" for i in range(80)])


def test_la_fiche_porte_ses_plages_jusqu_au_modele() -> None:
    client = to_model(
        {
            "reference": "clinique",
            "pop_name": "PoP Test",
            "address": "10.0.0.0/29",
            "extra_prefixes": '["10.0.1.0/28"]',
        }
    )
    assert client.extra_prefixes == ("10.0.1.0/28",)
    assert client.prefixes == ("10.0.0.0/29", "10.0.1.0/28")


# =========================================================================
# 2. Cible de la file
# =========================================================================


def test_une_seule_plage_ne_change_rien() -> None:
    assert prefixes_target("10.0.0.5") == "10.0.0.5/32"
    assert prefixes_target(["10.0.0.0/29"]) == "10.0.0.0/29"


def test_la_cible_est_canonique_et_sans_bloc_contenu() -> None:
    """Triee, dedoublonnee, un bloc deja contenu dans un autre retire : c'est
    ce qui empeche un ``set target=`` a chaque cycle."""
    assert (
        prefixes_target(["10.0.1.0/28", "10.0.0.5/29", "10.0.0.3", "10.0.1.0/28"])
        == "10.0.0.0/29,10.0.1.0/28"
    )
    # Une cible deja en liste se relit a l'identique.
    assert prefixes_target("10.0.1.0/28,10.0.0.0/29") == "10.0.0.0/29,10.0.1.0/28"


def test_ipv4_avant_ipv6() -> None:
    assert prefixes_target(["2001:db8::/64", "10.0.0.0/29"]) == "10.0.0.0/29,2001:db8::/64"


def cible_statique(adresse: str, *plages: str, login: str = "clinique") -> SubscriberTarget:
    return SubscriberTarget(
        login=login,
        interface="",
        kind=KIND_STATIC,
        address=adresse,
        extra_addresses=plages,
        plan_down_mbps=100,
        plan_up_mbps=50,
    )


def test_le_client_statique_vise_toutes_ses_plages() -> None:
    cible = cible_statique("10.0.0.0/29", "10.0.1.0/28")
    assert cible.queue_target() == "10.0.0.0/29,10.0.1.0/28"


# =========================================================================
# 3. Plan
# =========================================================================


def test_une_seule_file_porte_tout_le_contrat() -> None:
    """UN max-limit pour toutes les plages : deux files donneraient deux debits."""
    _types, files, ecartes = desired_state(
        links=[], subscribers=[cible_statique("10.0.0.0/29", "10.0.1.0/28")]
    )
    abonnes = [f for f in files if f.name == "freeqos-clinique"]
    assert len(abonnes) == 1
    assert abonnes[0].target == "10.0.0.0/29,10.0.1.0/28"
    assert abonnes[0].max_limit == "50000000/100000000"
    assert ecartes == []


def test_une_plage_revendiquee_par_deux_clients_est_ambigue() -> None:
    """L'egalite de chaine ne le verrait pas : ``a,b`` et ``b`` different."""
    _types, files, ecartes = desired_state(
        links=[],
        subscribers=[
            cible_statique("10.0.0.0/29", "10.0.1.0/28"),
            cible_statique("10.0.1.0/28", login="voisin"),
        ],
    )
    assert not [f for f in files if f.name in ("freeqos-clinique", "freeqos-voisin")]
    motifs = {s.login: s.reason for s in ecartes}
    assert "10.0.1.0/28" in motifs["clinique"] and "voisin" in motifs["clinique"]
    assert "clinique" in motifs["voisin"]


def lien(nom: str, reseau: str) -> LinkTarget:
    return LinkTarget(name=nom, interface=nom, subnet=reseau, measured_capacity_mbps=500)


def test_le_parent_doit_contenir_toutes_les_plages() -> None:
    """RouterOS n'admet dans une file enfant que ce qui correspond a son parent :
    une plage hors du parent ne serait ni plafonnee ni comptee."""
    _types, files, _ = desired_state(
        links=[lien("vlan-a", "10.0.0.0/24"), lien("pop", "10.0.0.0/16")],
        subscribers=[cible_statique("10.0.0.0/29", "10.0.1.0/28")],
    )
    abonne = next(f for f in files if f.name == "freeqos-clinique")
    # Le /24 ne contient pas 10.0.1.0/28 : seul le /16 contient tout.
    assert abonne.parent == "freeqos-parent-pop"


def test_sans_parent_commun_la_file_reste_a_la_racine() -> None:
    _types, files, _ = desired_state(
        links=[lien("vlan-a", "10.0.0.0/24")],
        subscribers=[cible_statique("10.0.0.0/29", "192.168.9.0/28")],
    )
    abonne = next(f for f in files if f.name == "freeqos-clinique")
    assert abonne.parent is None


def test_une_cible_relue_dans_un_autre_ordre_ne_reecrit_rien() -> None:
    """RouterOS rend la liste dans SON ordre : ce n'est pas un ecart."""
    _types, files, _ = desired_state(
        links=[], subscribers=[cible_statique("10.0.0.0/29", "10.0.1.0/28")]
    )
    existante = files[0].routeros_fields()
    existante.update({".id": "*1", "target": "10.0.1.0/28,10.0.0.0/29"})
    plan = build_plan(
        "pop-test",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[existante],
    )
    assert plan.actions == []
    assert plan.unchanged == 1


def test_une_file_tierce_sur_les_memes_plages_est_reconnue() -> None:
    """Meme ensemble, autre ordre : c'est bien la meme cible, et on n'empile
    pas une seconde file qui ne briderait rien."""
    _types, files, _ = desired_state(
        links=[], subscribers=[cible_statique("10.0.0.0/29", "10.0.1.0/28")]
    )
    tierce = {
        ".id": "*9",
        "name": "clinique-a-la-main",
        "target": "10.0.1.0/28,10.0.0.0/29",
        "max-limit": "1000000/1000000",
    }
    plan = build_plan(
        "pop-test",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[tierce],
    )
    assert [a.verb for a in plan.actions] == ["set"]
    assert plan.actions[0].target_id == "*9"
    assert plan.actions[0].fields == {"max-limit": "50000000/100000000"}


# =========================================================================
# 4. Shaping et collecte
# =========================================================================


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


async def test_la_fiche_et_ses_plages_produisent_une_file(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    settings.enforcement_enabled = True
    service = service_avec_inventaire(
        settings, routeur, [fiche(address="10.0.0.0/29", extra_prefixes=("10.0.1.0/28",))]
    )
    await service.registry.reload()

    liens, abonnes = await service.build_targets("pop-test")
    plan = await service.plan("pop-test", links=liens, subscribers=abonnes)

    ajouts = [a for a in plan.actions if a.verb == "add" and a.path == "/queue/simple"]
    assert [a.fields["target"] for a in ajouts] == ["10.0.0.0/29,10.0.1.0/28"]


async def test_seul_sur_sa_vlan_la_file_vise_tous_ses_reseaux_et_ses_plages(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """VLAN a deux pools, client seul dessus, plus un bloc route derriere lui :
    tout est a lui, tout passe par la meme file."""
    service = service_avec_inventaire(
        settings,
        routeur,
        [fiche(address="10.60.0.2/32", vlan=2060, extra_prefixes=("41.1.2.0/29",))],
    )
    service.sole_vlan_networks = {"mairie-vitre": "10.60.0.0/29,10.61.0.0/24"}
    await service.registry.reload()

    _liens, abonnes = await service.build_targets("pop-test")

    assert abonnes[0].queue_target() == "10.60.0.0/29,10.61.0.0/24,41.1.2.0/29"


def _deux_pools_sur_la_vlan() -> None:
    from app.collectors import mikrotik

    mikrotik._RESEAUX_CONNECTES["pop-test"] = [
        (ipaddress.ip_network("10.60.0.0/29"), "vlan2060"),
        (ipaddress.ip_network("10.61.0.0/24"), "vlan2060"),
        (ipaddress.ip_network("172.16.0.0/30"), "ether1"),
    ]


async def test_la_collecte_retient_tous_les_reseaux_de_la_vlan(settings: Settings) -> None:
    """Avant : seul le premier reseau de la VLAN etait retenu, le second pool
    passait sans plafond."""
    from tests.test_clients_statiques import InventaireMemoire, fiche_vlan

    _deux_pools_sur_la_vlan()
    client = routeur_vlan(rx=1_000, tx=10_000)
    service, _writer, _ = build_service(settings, client, InventaireMemoire([fiche_vlan()]))
    await service.collect_subscribers()
    assert service.sole_vlan_networks == {"nestle": "10.60.0.0/29,10.61.0.0/24"}


async def test_la_file_en_liste_est_retrouvee_par_n_importe_quel_reseau(
    settings: Settings,
) -> None:
    from tests.test_clients_statiques import InventaireMemoire, fiche_vlan
    from tests.test_collection_service import Clock

    _deux_pools_sur_la_vlan()
    clock = Clock()
    client = routeur_vlan(rx=1_000, tx=10_000)
    client.simple_queue_rows = [
        {"name": "q", "target": "10.61.0.0/24,10.60.0.0/29", "bytes": "0/0"}
    ]
    service, writer, _ = build_service(
        settings, client, InventaireMemoire([fiche_vlan()]), clock=clock
    )
    await service.collect_subscribers()
    clock.advance(10)
    client.simple_queue_rows = [
        {"name": "q", "target": "10.61.0.0/24,10.60.0.0/29", "bytes": "250000/125000"}
    ]
    await service.collect_subscribers()

    ligne = writer.subscriber_rows[-1][1]
    assert ligne.rx_bps == pytest.approx(200_000)
    assert ligne.tx_bps == pytest.approx(100_000)


# =========================================================================
# 5. API et adresses vues
# =========================================================================


class InventaireApiPlages(InventaireApi):
    """Le double de l'API, avec la meme validation des plages que la base."""

    async def create(self, payload: dict) -> dict:
        plages = normalise_extra_prefixes(payload.get("extra_prefixes"))
        return await super().create({**payload, "extra_prefixes": plages})

    async def update(self, client_id: int, payload: dict) -> dict:
        if payload.get("extra_prefixes") is not None:
            payload = {
                **payload,
                "extra_prefixes": normalise_extra_prefixes(payload["extra_prefixes"]),
            }
        return await super().update(client_id, payload)


@pytest.fixture
def api_client(settings: Settings) -> tuple[TestClient, InventaireApiPlages]:
    inventaire = InventaireApiPlages()
    container = build_container(settings, static_clients_repo=inventaire)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), inventaire


def test_api_declare_un_client_a_plusieurs_plages(api_client) -> None:
    client, inventaire = api_client
    reponse = client.post(
        "/api/v1/static-clients",
        json={
            "reference": "clinique",
            "pop_name": "PoP Test",
            "address": "10.0.0.0/29",
            "vlan": 120,
            "extra_prefixes": ["10.0.1.0/28", "10.0.2.10-10.0.2.11"],
            "plan_down_mbps": 100,
        },
    )
    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["extra_prefixes"] == ["10.0.1.0/28", "10.0.2.10/31"]
    assert inventaire.lignes[0]["extra_prefixes"] == ["10.0.1.0/28", "10.0.2.10/31"]


def test_api_refuse_une_plage_invalide(api_client) -> None:
    client, _ = api_client
    reponse = client.post(
        "/api/v1/static-clients",
        json={
            "reference": "clinique",
            "pop_name": "PoP Test",
            "address": "10.0.0.0/29",
            "extra_prefixes": ["10.0.2.50-10.0.2.10"],
        },
    )
    assert reponse.status_code == 400
    assert "range" in reponse.json()["detail"]


def test_api_une_liste_vide_retire_les_plages(api_client) -> None:
    client, inventaire = api_client
    cree = client.post(
        "/api/v1/static-clients",
        json={
            "reference": "clinique",
            "pop_name": "PoP Test",
            "address": "10.0.0.0/29",
            "extra_prefixes": ["10.0.1.0/28"],
        },
    ).json()
    reponse = client.patch(f"/api/v1/static-clients/{cree['id']}", json={"extra_prefixes": []})
    assert reponse.status_code == 200, reponse.text
    assert inventaire.lignes[0]["extra_prefixes"] == []


def test_une_adresse_du_second_pool_n_est_pas_non_declaree() -> None:
    """Le recensement rapproche aussi par les plages additionnelles."""
    fiches = [
        {
            "reference": "clinique",
            "address": normalise_address("10.0.0.0/29"),
            "extra_prefixes": ["10.0.1.0/28"],
        }
    ]
    trouve = _rapprocher({"address": "10.0.1.7"}, fiches)
    assert trouve is not None and trouve["reference"] == "clinique"
    assert _rapprocher({"address": "10.0.2.7"}, fiches) is None

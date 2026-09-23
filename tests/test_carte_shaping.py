"""La page Shaping montre OU ca bride, pas les commandes envoyees.

CE QU'ON REPARE ICI
-------------------
L'ecran principal du shaping rendait le JOURNAL DES COMMANDES : des lignes
``/queue/simple/add name=freeqos-parent-NAS-... max-limit=0/0``. C'est la trace
d'un moyen, pas une reponse. Pour savoir ou le reseau est bride, et a combien,
il fallait relire du RouterOS et reconstruire de tete la hierarchie que le
controleur connait deja.

La carte rend cette hierarchie telle que RouterOS l'applique : chaque lien
parent porte les abonnes qui passent par lui. Et elle montre AUSSI les points
qui n'ont pas de file, avec leur motif -- une carte qui ne montrerait que ce qui
marche laisserait chercher le reste dans le journal, c'est-a-dire nulle part.

Les commandes, elles, partent toutes seules : la boucle de reconciliation
ecrit, cette page regarde. La derniere moitie du fichier verrouille ce partage.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.enforcement.models import MANAGED_COMMENT
from app.main import register_routes
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_enforcement import FauxClientEcriture
from tests.test_pose_immediate import RouteurQuiSeSouvient
from tests.test_shaping_api import FauxDepotTopologie
from tests.test_shaping_service import MetriquesMinimales, make_service

LIEN = {
    "key": "router:pop-test|ether2|mac:DC:9F:DB:11:22:33",
    "source_key": "router:pop-test",
    "target_key": "mac:DC:9F:DB:11:22:33",
    "target_name": "BH-Nord",
    "target_kind": "radio",
    "kind": "ethernet",
    "interface": "ether2",
    "capacity_mbps": 1000.0,
    "discovered_by": "pop-test",
}

ABONNE = {
    "login": "dupont",
    "pop_name": "PoP Test",
    "kind": "pppoe",
    "plan_down_mbps": 100.0,
    "plan_up_mbps": 20.0,
}


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", address="10.20.0.10")
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


@pytest.fixture
def depot() -> FauxDepotTopologie:
    depot = FauxDepotTopologie()
    depot.link_rows = [dict(LIEN)]
    # L'abonne est rattache au secteur d'en face : c'est ce qui le fait pendre
    # sous le lien plutot qu'a la racine du PoP.
    depot.attachment_rows = {"dupont": "mac:DC:9F:DB:11:22:33"}
    return depot


@pytest.fixture
def ecriture(routeur: FakeRouterOsClient) -> FauxClientEcriture:
    return RouteurQuiSeSouvient(routeur)


def _service(
    settings: Settings,
    routeur: FakeRouterOsClient,
    depot: FauxDepotTopologie,
    ecriture: FauxClientEcriture,
    abonnes: list[dict] | None = None,
) -> ShapingService:
    settings.enforcement_enabled = True
    return make_service(
        settings,
        routeur,
        repository=depot,
        metrics=MetriquesMinimales(abonnes if abonnes is not None else [dict(ABONNE)]),
        write_client_factory=lambda config: ecriture,
    )


def _par_label(carte: dict) -> dict[str, dict]:
    """Aplati l'arbre pour l'inspection, en gardant la profondeur."""
    trouves: dict[str, dict] = {}

    def descendre(points, profondeur):
        for point in points:
            trouves[point["label"]] = {**point, "profondeur": profondeur}
            descendre(point.get("children") or [], profondeur + 1)

    for routeur in carte["routers"]:
        descendre(routeur["points"], 0)
    return trouves


# =========================================================================
# 1. La carte : la hierarchie que RouterOS applique vraiment
# =========================================================================


async def test_l_abonne_pend_sous_le_lien_qu_il_traverse(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """C'est CE qui explique un debit : un abonne a 100 Mbps sous un backhaul
    plafonne a 900 partage ces 900 avec ses voisins. A plat, il faudrait le
    reconstruire de tete."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    carte = await service.shaping_points()

    points = _par_label(carte)
    assert points["BH-Nord"]["kind"] == "lien"
    assert points["BH-Nord"]["profondeur"] == 0
    assert points["dupont"]["profondeur"] == 1
    assert points["dupont"]["parent"] == points["BH-Nord"]["name"]


async def test_la_carte_dit_le_plafond_et_d_ou_il_vient(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Un exploitant qui voit 900 Mbps doit savoir s'il regarde une capacite
    mesuree, un plafond qu'il a saisi, ou un resserrage de la boucle QoE : les
    trois se corrigent a des endroits differents."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert points["BH-Nord"]["down_mbps"] == pytest.approx(900.0)  # 1000 x 0.90
    assert "capacite mesuree" in points["BH-Nord"]["source"]
    assert points["dupont"]["down_mbps"] == pytest.approx(100.0)
    assert points["dupont"]["source"] == "plan souscrit"


async def test_un_resserrage_qoe_est_dit_sur_le_point_concerne(
    settings: Settings, routeur, depot, ecriture
) -> None:
    depot.qoe_states = {LIEN["key"]: {"trim_factor": 0.8}}
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert "80 %" in points["BH-Nord"]["source"]
    assert points["BH-Nord"]["down_mbps"] == pytest.approx(720.0)


# =========================================================================
# 2. Ce qui N'EST PAS bride figure aussi, avec son motif
# =========================================================================


async def test_un_abonne_sans_plan_est_sur_la_carte_avec_son_motif(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Un abonne absent de la carte serait indiscernable d'un abonne bride."""
    service = _service(
        settings,
        routeur,
        depot,
        ecriture,
        abonnes=[{**ABONNE, "plan_down_mbps": None, "plan_up_mbps": None}],
    )
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert points["dupont"]["state"] == ShapingService.ETAT_ECARTE
    assert "no rate to apply" in points["dupont"]["reason"]


async def test_un_lien_desactive_a_la_main_est_sur_la_carte_avec_son_motif(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Il disparaissait purement et simplement du plan : un lien sans file
    parente ne partage rien, et ne rien dire etait tout aussi trompeur."""
    depot._policies[("link", LIEN["key"])] = {  # noqa: SLF001
        "scope": "link",
        "target_key": LIEN["key"],
        "enabled": False,
    }
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert points["BH-Nord"]["state"] == ShapingService.ETAT_ECARTE
    assert "disabled" in points["BH-Nord"]["reason"]


async def test_une_file_posee_a_la_main_figure_et_reste_intouchee(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Une file de l'exploitant EST du shaping : l'omettre donnerait une carte
    qui contredit le routeur."""
    routeur.simple_queue_rows = [
        {".id": "*9", "name": "bride-camera", "target": "10.30.0.7/32", "max-limit": "2M/2M"}
    ]
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert points["bride-camera"]["state"] == ShapingService.ETAT_MANUELLE
    assert "jamais" in points["bride-camera"]["reason"]


# =========================================================================
# 3. Posee ou a poser : la carte suit le routeur
# =========================================================================


async def test_tant_que_rien_n_est_ecrit_les_points_sont_a_poser(
    settings: Settings, routeur, depot, ecriture
) -> None:
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    points = _par_label(await service.shaping_points())

    assert points["BH-Nord"]["state"] == ShapingService.ETAT_A_POSER
    assert points["dupont"]["state"] == ShapingService.ETAT_A_POSER


async def test_apres_le_passage_de_la_boucle_les_points_sont_brides(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """LE PARTAGE QUI COMPTE : la reconciliation ecrit, la carte regarde. Ce que
    l'une a pose, l'autre le montre -- sans qu'on ait rien clique."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    resultat = await service.reconcile()
    points = _par_label(await service.shaping_points())

    assert resultat["applied"] >= 2
    assert points["BH-Nord"]["state"] == ShapingService.ETAT_POSEE
    assert points["dupont"]["state"] == ShapingService.ETAT_POSEE


async def test_la_carte_n_ecrit_rien_meme_enforcement_actif(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Regarder ne doit jamais configurer : sinon ouvrir un onglet deviendrait
    un geste d'exploitation."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    await service.shaping_points()

    assert ecriture.executed == []
    assert depot.audit_rows == []


async def test_enforcement_coupe_la_carte_le_dit_sur_chaque_point(
    settings: Settings, routeur, depot, ecriture
) -> None:
    service = _service(settings, routeur, depot, ecriture)
    service._enforcement_enabled = False  # noqa: SLF001
    await service.registry.reload()

    carte = await service.shaping_points()

    assert carte["enforcement_enabled"] is False
    assert "enforcement est desactive" in _par_label(carte)["dupont"]["reason"]


# =========================================================================
# 4. La preuve que les commandes partent seules
# =========================================================================


async def test_la_carte_rend_le_dernier_passage_de_la_boucle(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """Une page sans aucun bouton se lit comme une page qui ne fait rien. Ce
    champ est ce qui prouve le contraire."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()

    assert (await service.shaping_points())["last_reconcile"] is None
    await service.reconcile()
    carte = await service.shaping_points()

    assert carte["last_reconcile"]["applied"] >= 2
    assert carte["last_reconcile"]["at"]
    assert carte["reconcile_interval_s"] == settings.shaping_reconcile_interval_s


async def test_un_passage_sans_rien_a_faire_laisse_quand_meme_sa_trace(
    settings: Settings, routeur, depot, ecriture
) -> None:
    """ "Rien a faire" est une reponse, et c'est celle qu'on doit lire quand tout
    est deja en place."""
    service = _service(settings, routeur, depot, ecriture)
    await service.registry.reload()
    await service.reconcile()

    await service.reconcile()

    assert service.last_reconcile is not None
    assert service.last_reconcile["applied"] == 0


async def test_la_boucle_coupee_laisse_aussi_une_trace(
    settings: Settings, routeur, depot, ecriture
) -> None:
    service = _service(settings, routeur, depot, ecriture)
    service._enforcement_enabled = False  # noqa: SLF001
    await service.registry.reload()

    await service.reconcile()

    assert service.last_reconcile == {
        "enabled": False,
        "routers": [],
        "applied": 0,
        "errors": [],
        "at": service.last_reconcile["at"],
    }


# =========================================================================
# 5. L'API
# =========================================================================


@pytest.fixture
def api(settings: Settings, routeur: FakeRouterOsClient, depot: FauxDepotTopologie, ecriture):
    settings.enforcement_enabled = True
    container = build_container(settings, topology_repo=depot, client=routeur)
    container.shaping = ShapingService(
        settings,
        registry=container.registry,
        repository=depot,
        metrics=MetriquesMinimales([dict(ABONNE)]),
        write_client_factory=lambda config: ecriture,
    )
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), container


def test_api_rend_la_carte_en_arbre(api) -> None:
    client, _ = api

    corps = client.get("/api/v1/shaping/points").json()

    routeur = corps["routers"][0]
    assert routeur["router"] == "pop-test"
    racine = routeur["points"][0]
    assert racine["label"] == "BH-Nord"
    assert [e["label"] for e in racine["children"]] == ["dupont"]
    assert routeur["counts"]["liens"] == 1


def test_api_filtre_par_routeur(api) -> None:
    client, _ = api

    assert client.get("/api/v1/shaping/points?router=pop-test").json()["routers"]
    assert client.get("/api/v1/shaping/points?router=fantome").json()["routers"] == []


def test_api_un_routeur_muet_ne_vide_pas_la_carte(api) -> None:
    client, container = api
    container.registry.collectors[0]._client.raise_on_queues = RuntimeError("timeout")  # noqa: SLF001

    routeurs = client.get("/api/v1/shaping/points").json()["routers"]

    assert routeurs[0]["points"] == []
    assert "timeout" in routeurs[0]["error"]


def test_api_la_carte_est_en_lecture_seule(api) -> None:
    """Aucune route de la carte ne doit pouvoir ecrire : appliquer reste un
    geste explicite, ou le travail de la boucle."""
    client, _ = api
    assert set(client.app.openapi()["paths"]["/api/v1/shaping/points"]) == {"get"}


def test_api_les_files_manuelles_sont_rendues_aussi(api) -> None:
    client, container = api
    container.registry.collectors[0]._client.simple_queue_rows = [  # noqa: SLF001
        {".id": "*9", "name": "bride-camera", "target": "10.30.0.7/32", "max-limit": "2M/2M"},
        {
            ".id": "*10",
            "name": "freeqos-autre",
            "target": "10.30.0.8/32",
            "comment": MANAGED_COMMENT,
        },
    ]

    corps = client.get("/api/v1/shaping/points").json()

    labels = {p["label"]: p for p in corps["routers"][0]["points"]}
    assert labels["bride-camera"]["state"] == ShapingService.ETAT_MANUELLE
    # Une file du controleur devenue inutile n'est PAS un point de la carte :
    # elle n'a plus de cible declaree, la reconciliation s'en occupe.
    assert "freeqos-autre" not in labels

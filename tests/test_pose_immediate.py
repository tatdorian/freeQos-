"""Declarer un client POSE sa file, et dit ce qui l'en empeche.

DEUX DEFAUTS, UN SEUL SYMPTOME
------------------------------
"J'ai declare ce client, il ne remonte pas." Deux causes se cachaient derriere
cette phrase, et aucune ne produisait la moindre erreur :

1. LE POP SAISI NE CORRESPONDAIT PAS. Le rapprochement se faisait par egalite de
   chaine : "francophonie" et "Francophonie" etaient deux sites. Le client
   n'avait alors ni collecteur, ni compteur, ni file -- et un PoP fantome
   naissait en base, qui ressemblait au vrai a s'y meprendre.

2. RIEN N'ETAIT APPLIQUE AVANT LA RECONCILIATION. Elle passe toutes les deux
   minutes ; entre-temps, rien ne distinguait "ca arrive" de "ca n'arrivera
   jamais".

La moitie de ce fichier teste donc des MOTIFS : ce qui est rendu a l'exploitant
quand aucune file n'est posee. Un client sans file qui ne dit pas pourquoi est
exactement le defaut qu'on repare ici.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig, Settings
from app.enforcement.models import MANAGED_COMMENT, Plan, PlanAction
from app.main import register_routes
from app.services.pop_match import (
    RESOLUTION_AMBIGUE,
    RESOLUTION_EXACTE,
    RESOLUTION_NORMALISEE,
    explain,
    normalise_pop,
    pop_names,
    resolve_pop,
)
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_clients_statiques import InventaireApi
from tests.test_enforcement import FauxClientEcriture
from tests.test_shaping_service import DepotBoosts, MetriquesMinimales, make_service


def _collecteur(nom: str, pop: str) -> MikrotikCollector:
    return MikrotikCollector(
        RouterConfig(name=nom, host="192.0.2.11", password="secret-de-lab", pop_name=pop),
        client=FakeRouterOsClient(),
    )


# =========================================================================
# 1. Le PoP saisi et le PoP porte par un routeur
# =========================================================================


@pytest.mark.parametrize(
    ("saisi", "porte"),
    [
        ("francophonie", "Francophonie"),
        ("FRANCOPHONIE", "francophonie"),
        ("  francophonie  ", "Francophonie"),
        ("PoP Francophonie", "francophonie"),
        ("francophonie", "PoP-Francophonie"),
        ("Mediatheque", "Médiathèque"),
    ],
)
def test_le_pop_est_reconnu_malgre_la_casse_les_accents_et_le_mot_pop(
    saisi: str, porte: str
) -> None:
    """Ces ecritures designent le meme site pour n'importe quel exploitant. Les
    traiter comme des sites differents rendait le client invisible."""
    match = resolve_pop(saisi, [_collecteur("r1", porte)])
    assert match.found
    assert match.pop_name == porte
    # Le rapprochement est dit : l'exploitant doit pouvoir voir qu'il a eu lieu.
    assert match.resolution in (RESOLUTION_EXACTE, RESOLUTION_NORMALISEE)


def test_l_egalite_exacte_garde_la_priorite() -> None:
    """Tant que le nom correspond au caractere pres, la tolerance ne sert a rien
    et ne peut donc rien casser."""
    match = resolve_pop("Nord", [_collecteur("r1", "Nord"), _collecteur("r2", "nord")])
    assert match.resolution == RESOLUTION_EXACTE
    assert [c.name for c in match.collectors] == ["r1"]


def test_deux_pop_qui_se_ressemblent_ne_sont_jamais_fusionnes() -> None:
    """En choisir un poserait la file sur le MAUVAIS site : pire que de ne rien
    poser, et bien plus difficile a voir."""
    match = resolve_pop("nord", [_collecteur("r1", "Nord"), _collecteur("r2", "PoP Nord")])
    assert not match.found
    assert match.resolution == RESOLUTION_AMBIGUE


def test_un_pop_a_deux_routeurs_les_rend_tous_les_deux() -> None:
    """Redondance ou separation acces/coeur : oublier le routeur qui voit
    passer le trafic laisserait le client non bride."""
    match = resolve_pop("Nord", [_collecteur("r1", "Nord"), _collecteur("r2", "Nord")])
    assert [c.name for c in match.collectors] == ["r1", "r2"]


def test_un_pop_inconnu_nomme_ceux_qui_existent() -> None:
    """ "PoP inconnu" laisse chercher une faute de frappe a l'aveugle."""
    routeurs = [_collecteur("r1", "Francophonie"), _collecteur("r2", "Nord")]
    match = resolve_pop("francofonie", routeurs)

    assert not match.found
    message = explain(match, "francofonie", routeurs)
    assert "'Francophonie'" in message and "'Nord'" in message
    assert pop_names(routeurs) == ["Francophonie", "Nord"]


def test_la_normalisation_ne_vide_pas_un_nom_qui_commence_par_pop() -> None:
    """'PoP' seul reste 'pop' : sinon deux PoP nommes 'PoP 1' et 'PoP 2'
    deviendraient tous deux vides, donc identiques."""
    assert normalise_pop("PoP") == ""
    assert normalise_pop("PoP 1") == "1"
    assert normalise_pop("PoP 1") != normalise_pop("PoP 2")


# =========================================================================
# 2. Le client entre enfin dans l'etat desire du bon routeur
# =========================================================================


class InventaireMemoire:
    """Inventaire minimal, cote service."""

    def __init__(self, clients) -> None:
        self.clients = clients

    async def load_enabled(self):
        return list(self.clients)


def _client_statique(**kwargs):
    from app.models import StaticClient

    base = {
        "reference": "mairie",
        "pop_name": "francophonie",
        "address": "10.20.0.8/29",
        "plan_down_mbps": 100.0,
        "plan_up_mbps": 20.0,
    }
    return StaticClient(**{**base, **kwargs})


@pytest.fixture
def settings_francophonie(settings: Settings) -> Settings:
    settings.routers = [
        RouterConfig(
            name="pop-francophonie",
            host="192.0.2.11",
            password="secret-de-lab",
            pop_name="Francophonie",
        )
    ]
    settings.enforcement_enabled = True
    return settings


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    return FakeRouterOsClient()


class RouteurQuiSeSouvient(FauxClientEcriture):
    """Un faux routeur qui GARDE ce qu'on lui ecrit.

    Sans cette memoire, le double relit toujours un routeur vide : le plan
    suivant reproposerait la meme creation, et les tests ne pourraient pas
    distinguer "file posee" de "file a poser" -- justement la distinction qui
    compte pour l'exploitant.
    """

    def __init__(self, lecture: FakeRouterOsClient) -> None:
        super().__init__()
        self.lecture = lecture

    def execute(self, action):  # type: ignore[no-untyped-def]
        resultat = super().execute(action)
        if action.path == "/queue/type":
            # Sans cette memoire, les types CAKE seraient recrees a chaque
            # passage : un cycle de reconciliation n'aurait alors jamais "rien
            # a faire", et on ne pourrait pas tester ce cas.
            self.lecture.queue_type_rows.append(
                {".id": f"*t{len(self.lecture.queue_type_rows)}", **action.fields}
            )
            return resultat
        if action.path != "/queue/simple":
            return resultat
        lignes = self.lecture.simple_queue_rows
        nom = action.name or action.fields.get("name", "")
        if action.verb == "add":
            lignes.append({".id": f"*{len(lignes) + 1}", **action.fields})
        elif action.verb == "set":
            for ligne in lignes:
                if ligne.get("name") == nom or ligne.get(".id") == action.target_id:
                    ligne.update(action.fields)
        elif action.verb == "remove":
            self.lecture.simple_queue_rows = [
                ligne
                for ligne in lignes
                if ligne.get("name") != nom and ligne.get(".id") != action.target_id
            ]
        return resultat


@pytest.fixture
def ecriture(routeur: FakeRouterOsClient) -> FauxClientEcriture:
    return RouteurQuiSeSouvient(routeur)


def _service(settings: Settings, routeur, ecriture, inventaire) -> ShapingService:
    return make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        metrics=MetriquesMinimales([]),
        static_clients=inventaire,
        write_client_factory=lambda config: ecriture,
    )


async def test_un_client_saisi_en_minuscules_est_shape_par_son_routeur(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """LE DEFAUT D'ORIGINE. Le client declare sur 'francophonie' n'entrait dans
    l'etat desire d'aucun routeur : aucune file, aucune erreur, rien."""
    service = _service(
        settings_francophonie, routeur, ecriture, InventaireMemoire([_client_statique()])
    )
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-francophonie")

    assert [a.login for a in abonnes] == ["mairie"]
    assert abonnes[0].queue_target() == "10.20.0.8/29"


async def test_un_client_d_un_autre_pop_ne_suit_pas(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """La tolerance ne doit pas devenir un fourre-tout."""
    service = _service(
        settings_francophonie,
        routeur,
        ecriture,
        InventaireMemoire([_client_statique(pop_name="Nord")]),
    )
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-francophonie")

    assert abonnes == []


# =========================================================================
# 3. La pose immediate : ce qui est ecrit, et ce qui ne l'est pas
# =========================================================================


async def test_declarer_un_client_pose_sa_file_tout_de_suite(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    service = _service(
        settings_francophonie, routeur, ecriture, InventaireMemoire([_client_statique()])
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_POSEE
    assert rapport["applied"] >= 1
    assert rapport["router"] == "pop-francophonie"
    assert "freeqos-mairie" in [a.name for a in ecriture.executed]


async def test_seule_la_file_de_ce_client_est_ecrite(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """Declarer un client ne doit pas reecrire les files de tous les autres :
    ce sont des ecritures que personne n'a demandees, au pire moment pour les
    relire."""
    service = _service(
        settings_francophonie,
        routeur,
        ecriture,
        InventaireMemoire(
            [_client_statique(), _client_statique(reference="ecole", address="10.20.0.16/29")]
        ),
    )
    await service.registry.reload()

    await service.enforce_static_client(reference="mairie", pop_name="francophonie", author="test")

    noms = [a.name for a in ecriture.executed if a.path == "/queue/simple"]
    assert noms == ["freeqos-mairie"]


async def test_un_client_sans_debit_dit_pourquoi_il_n_a_pas_de_file(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """Le motif vient du planificateur lui-meme : il ne peut donc pas raconter
    autre chose que ce qui serait reellement ecrit."""
    service = _service(
        settings_francophonie,
        routeur,
        ecriture,
        InventaireMemoire([_client_statique(plan_down_mbps=None, plan_up_mbps=None)]),
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_ECARTE
    assert "no rate to apply" in rapport["reason"]
    assert ecriture.executed == []


async def test_un_pop_qui_ne_correspond_a_rien_est_dit_tout_de_suite(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """C'est LA reponse a "il ne remonte pas" : elle arrive a la saisie, pas
    apres une demi-heure de recherche."""
    service = _service(
        settings_francophonie,
        routeur,
        ecriture,
        InventaireMemoire([_client_statique(pop_name="francofonie")]),
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francofonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_SANS_ROUTEUR
    assert "'Francophonie'" in rapport["reason"]
    assert ecriture.executed == []


async def test_enforcement_desactive_calcule_sans_ecrire(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """Le verrou global reste le dernier mot. Mais l'exploitant apprend que sa
    file est prete et ce qui la retient, au lieu d'un silence."""
    settings_francophonie.enforcement_enabled = False
    service = _service(
        settings_francophonie, routeur, ecriture, InventaireMemoire([_client_statique()])
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_A_POSER
    assert "enforcement" in rapport["reason"]
    assert ecriture.executed == []


async def test_un_routeur_muet_ne_fait_pas_perdre_la_declaration(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    routeur.raise_on_queues = RuntimeError("timeout")
    service = _service(
        settings_francophonie, routeur, ecriture, InventaireMemoire([_client_statique()])
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_ERREUR
    assert "timeout" in rapport["reason"]


async def test_sans_base_on_ne_pretend_pas_que_la_file_est_conforme(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """Sans topologie ni metriques, le plan est vide : l'absence d'action se
    lirait comme "file posee". Ce serait faux dans le sens le plus trompeur."""
    service = make_service(
        settings_francophonie,
        routeur,
        static_clients=InventaireMemoire([_client_statique()]),
        write_client_factory=lambda config: ecriture,
    )
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test"
    )

    assert rapport["state"] == ShapingService.ETAT_ERREUR
    assert "base non initialisee" in rapport["reason"]


async def test_retirer_un_client_retire_sa_file_et_elle_seule(
    settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture
) -> None:
    """Le plan est calcule avec ``prune`` : sans la restriction au nom, un
    /ppp/active vide au mauvais moment emporterait les files du PoP entier."""
    routeur.simple_queue_rows = [
        {
            ".id": "*1",
            "name": "freeqos-mairie",
            "target": "10.20.0.8/29",
            "comment": MANAGED_COMMENT,
            "max-limit": "20M/100M",
        },
        {
            ".id": "*2",
            "name": "freeqos-dupont",
            "target": "10.20.0.50/32",
            "comment": MANAGED_COMMENT,
            "max-limit": "10M/50M",
        },
    ]
    # L'inventaire ne contient plus la fiche : c'est l'etat d'apres suppression.
    service = _service(settings_francophonie, routeur, ecriture, InventaireMemoire([]))
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="mairie", pop_name="francophonie", author="test", removing=True
    )

    assert rapport["state"] == ShapingService.ETAT_RETIREE
    retirees = [a.name for a in ecriture.executed if a.verb == "remove"]
    assert retirees == ["freeqos-mairie"]


# =========================================================================
# 4. Le plan restreint : ce que la restriction garde, et ce qu'elle jette
# =========================================================================


def _action(nom: str, parent: str | None = None, path: str = "/queue/simple") -> PlanAction:
    champs = {"name": nom}
    if parent:
        champs["parent"] = parent
    return PlanAction(verb="add", path=path, fields=champs, name=nom)


def test_la_restriction_garde_la_chaine_des_parents() -> None:
    """RouterOS refuse un enfant dont le parent n'existe pas encore : garder la
    file seule produirait une commande qui echoue."""
    plan = Plan(
        router_name="r",
        actions=[
            _action("freeqos-parent-bh"),
            _action("freeqos-parent-secteur", parent="freeqos-parent-bh"),
            _action("freeqos-mairie", parent="freeqos-parent-secteur"),
            _action("freeqos-voisin"),
        ],
    )

    restreint = plan.restrict_to(plan.parent_chain("freeqos-mairie"))

    assert [a.name for a in restreint.actions] == [
        "freeqos-parent-bh",
        "freeqos-parent-secteur",
        "freeqos-mairie",
    ]


def test_la_restriction_garde_les_types_cake_mais_pas_pour_un_retrait() -> None:
    plan = Plan(
        router_name="r",
        actions=[_action("freeqos-cake-down", path="/queue/type"), _action("freeqos-mairie")],
    )

    assert len(plan.restrict_to({"freeqos-mairie"}).actions) == 2
    assert [a.name for a in plan.restrict_to({"freeqos-mairie"}, keep_types=False).actions] == [
        "freeqos-mairie"
    ]


def test_une_chaine_circulaire_ne_boucle_pas() -> None:
    plan = Plan(
        router_name="r",
        actions=[_action("a", parent="b"), _action("b", parent="a")],
    )
    assert plan.parent_chain("a") == {"a", "b"}


# =========================================================================
# 5. L'API : la declaration rend compte de ce qu'elle a ecrit
# =========================================================================


@pytest.fixture
def api(settings_francophonie: Settings, routeur: FakeRouterOsClient, ecriture: FauxClientEcriture):
    inventaire = InventaireApi()
    container = build_container(
        settings_francophonie, static_clients_repo=inventaire, client=routeur
    )
    container.shaping = ShapingService(
        settings_francophonie,
        registry=container.registry,
        repository=DepotBoosts(),
        metrics=MetriquesMinimales([]),
        static_clients=inventaire,
        write_client_factory=lambda config: ecriture,
    )
    app = FastAPI()
    app.state.settings = settings_francophonie
    register_routes(app, settings_francophonie)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), inventaire, ecriture


def _declarer(client: TestClient, **surcharges):
    corps = {
        "reference": "mairie",
        "pop_name": "francophonie",
        "address": "10.20.0.8/29",
        "plan_down_mbps": 100,
        "plan_up_mbps": 20,
        **surcharges,
    }
    return client.post("/api/v1/static-clients", json=corps)


def test_api_la_declaration_pose_la_file_et_le_dit(api) -> None:
    client, _, ecriture = api

    reponse = _declarer(client)

    assert reponse.status_code == 201
    enforcement = reponse.json()["enforcement"]
    assert enforcement["state"] == ShapingService.ETAT_POSEE
    assert enforcement["applied"] >= 1
    assert "freeqos-mairie" in [a.name for a in ecriture.executed]


def test_api_une_declaration_reste_valide_meme_si_le_routeur_refuse(api) -> None:
    """La fiche est l'intention de l'exploitant : un routeur injoignable ne doit
    pas l'annuler, seulement se raconter."""
    client, inventaire, _ = api
    client_routeur = client.app.dependency_overrides[get_container]().registry.collectors[0]
    client_routeur._client.raise_on_queues = RuntimeError("timeout")  # noqa: SLF001

    reponse = _declarer(client)

    assert reponse.status_code == 201
    assert reponse.json()["enforcement"]["state"] == ShapingService.ETAT_ERREUR
    assert len(inventaire.lignes) == 1


def test_api_un_pop_inconnu_est_annonce_a_la_saisie(api) -> None:
    client, _, ecriture = api

    reponse = _declarer(client, pop_name="francofonie")

    enforcement = reponse.json()["enforcement"]
    assert enforcement["state"] == ShapingService.ETAT_SANS_ROUTEUR
    assert "'Francophonie'" in enforcement["reason"]
    assert ecriture.executed == []


def test_api_modifier_le_debit_reapplique_la_file(api) -> None:
    client, _, ecriture = api
    cree = _declarer(client).json()
    ecriture.executed.clear()

    reponse = client.patch(f"/api/v1/static-clients/{cree['id']}", json={"plan_down_mbps": 200})

    assert reponse.status_code == 200
    assert reponse.json()["enforcement"]["state"] == ShapingService.ETAT_POSEE
    assert [a.verb for a in ecriture.executed if a.name == "freeqos-mairie"] == ["set"]


def test_api_supprimer_une_fiche_retire_sa_file(api) -> None:
    client, _, ecriture = api
    cree = _declarer(client).json()
    ecriture.executed.clear()

    assert client.delete(f"/api/v1/static-clients/{cree['id']}").status_code == 204

    assert [a.name for a in ecriture.executed if a.verb == "remove"] == ["freeqos-mairie"]


def test_api_l_etat_des_files_dit_le_motif_de_chaque_fiche(api) -> None:
    """L'ecran qui repond a "pourquoi ce client n'a-t-il pas de file ?"."""
    client, _, _ = api
    _declarer(client)
    _declarer(client, reference="ecole", address="10.20.0.16/29", pop_name="francofonie")

    corps = client.get("/api/v1/static-clients/enforcement").json()

    etats = {ligne["reference"]: ligne for ligne in corps["clients"]}
    assert corps["enforcement_enabled"] is True
    assert etats["mairie"]["state"] == ShapingService.ETAT_POSEE
    assert etats["ecole"]["state"] == ShapingService.ETAT_SANS_ROUTEUR
    assert "'Francophonie'" in etats["ecole"]["reason"]

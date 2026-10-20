"""Ecriture autorisee ROUTEUR PAR ROUTEUR, et boucle QoE coupable depuis l'interface.

POURQUOI CE FICHIER EXISTE
--------------------------
L'exploitant deploie freeQoS a cote de Preseem, sur un reseau en production.
Le plan : tout declarer, observer des jours en lecture seule, puis laisser
freeQoS ecrire sur UN NAS pilote, puis NAS par NAS. Jusqu'ici, garder un routeur
declare en lecture seule obligeait a retirer la politique 'write' de son compte
RouterOS : un garde-fou qui vivait sur le routeur, pas dans l'interface.

Ce que ces tests verrouillent :

  1. un routeur en lecture seule ne recoit AUCUNE commande, par AUCUN chemin
     d'ecriture, interrupteur global allume -- geste explicite compris ;
  2. son plan reste calcule et montre, marque lecture seule ;
  3. la mise en service s'arrete sur "blocked", avec le geste qui debloque ;
  4. les routeurs existants continuent d'ecrire (defaut VRAI), et chaque bascule
     est journalisee avec son auteur et son motif ;
  5. la case du formulaire est DECOCHEE par defaut ;
  6. ``qoe_loop_enabled=false`` rend la boucle inerte ET rend ses resserrages.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.api.routers_admin import RouterInput
from app.config import RouterConfig, RouterRole, Settings, _load_inventory_file
from app.enforcement.capability import WriteCapability
from app.enforcement.models import Plan, PlanAction
from app.main import register_routes
from app.services import provisioning
from app.services.collection import JOB_RECONCILE, JOB_TOPOLOGY
from app.services.netflow_export import ETAT_A_POSER, NetflowExportService
from app.services.registry import RouterRegistry
from app.services.restrictions import JOB_RESTRICTIONS, RestrictionService
from app.services.runtime_config import PAR_NOM, RuntimeConfig
from app.services.shaping import (
    MOTIF_ROUTEUR_LECTURE_SEULE,
    RouterWriteDisabledError,
    ShapingService,
)
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_enforcement import FauxClientEcriture
from tests.test_mise_en_service import Export, Planificateur
from tests.test_mise_en_service import Shaping as ShapingMiseEnService
from tests.test_pops_api import InMemoryRoutersRepository
from tests.test_qoe_loop import LIEN, DepotQoe, MetriquesQoe, file_du_secteur
from tests.test_shaping_service import DepotBoosts, MetriquesMinimales, _politiques

RACINE = Path(__file__).resolve().parents[1]


# =========================================================================
# Doubles
# =========================================================================
class FabriqueEcriture:
    """Fabrique de clients d'ecriture qui COMPTE ses appels.

    Un routeur en lecture seule ne doit meme pas voir s'ouvrir une session
    d'ecriture : ne rien executer ne suffit pas, on ne doit pas s'y connecter.
    """

    def __init__(self) -> None:
        self.client = FauxClientEcriture()
        self.appels = 0

    def __call__(self, config: RouterConfig, **_: Any) -> FauxClientEcriture:
        self.appels += 1
        return self.client

    @property
    def commandes(self) -> list[str]:
        return [a.command for a in self.client.executed]


@pytest.fixture
def secteur() -> FakeRouterOsClient:
    """Le PoP des tests de la boucle QoE : trois abonnes derriere un secteur radio."""
    client = FakeRouterOsClient()
    for index, login in enumerate(("dupont", "durand", "martin"), start=10):
        client.add_session(login, address=f"10.20.0.{index}")
    client.address_rows = [{"address": "10.20.0.1/24", "interface": "ether2"}]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    client.grant_write("qos-ro")
    return client


def _service(
    settings: Settings,
    client: FakeRouterOsClient,
    *,
    ecriture: bool,
    fabrique: FabriqueEcriture,
    depot: Any = None,
    metriques: Any = None,
) -> ShapingService:
    """Interrupteur GLOBAL allume, interrupteur du routeur selon ``ecriture``."""
    settings.enforcement_enabled = True
    settings.routers[0].write_enabled = ecriture
    registry = RouterRegistry(settings, client_factory=lambda config: client)
    return ShapingService(
        settings,
        registry=registry,
        repository=depot,
        metrics=metriques,
        write_client_factory=fabrique,
    )


def _depot_avec_limite() -> DepotBoosts:
    """Une limite saisie a la main : la reconciliation a de quoi ecrire."""
    depot = DepotBoosts()
    depot.policy_map = lambda scope: _politiques(  # type: ignore[assignment]
        scope, {"dupont": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}}
    )
    return depot


def _metriques() -> MetriquesMinimales:
    return MetriquesMinimales([{"login": "dupont", "pop_name": "PoP Test", "plan_down_mbps": 100}])


# =========================================================================
# 1. Aucun chemin d'ecriture ne contourne l'interrupteur du routeur
# =========================================================================
async def test_la_reconciliation_ne_touche_pas_un_routeur_en_lecture_seule(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    service = _service(
        settings,
        routeur,
        ecriture=False,
        fabrique=fabrique,
        depot=_depot_avec_limite(),
        metriques=_metriques(),
    )
    await service.registry.reload()

    resultat = await service.reconcile()

    assert resultat["enabled"] is True
    assert resultat["read_only"] == ["pop-test"]
    assert resultat["routers"] == [] and resultat["applied"] == 0
    assert fabrique.appels == 0 and not fabrique.commandes

    # TEMOIN : le meme routeur, autorise, recoit bien la limite. C'est donc
    # l'interrupteur, et rien d'autre, qui retenait l'ecriture.
    settings.routers[0].write_enabled = True
    await service.registry.reload()
    await service.reconcile()
    assert any("max-limit=100000/100000" in c for c in fabrique.commandes)


async def test_un_geste_explicite_n_ecrit_pas_non_plus(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """L'interrupteur global laisse passer un geste humain ; celui du routeur,
    jamais : c'est tout son interet pendant un deploiement pilote."""
    fabrique = FabriqueEcriture()
    service = _service(
        settings,
        routeur,
        ecriture=False,
        fabrique=fabrique,
        depot=_depot_avec_limite(),
        metriques=_metriques(),
    )
    await service.registry.reload()

    rapport = await service.enforce_subscriber(login="dupont", author="ui:exploitant@isp")

    assert rapport["state"] == service.ETAT_A_POSER
    assert MOTIF_ROUTEUR_LECTURE_SEULE in rapport["reason"]
    # Le plan a bien ete calcule : on sait ce qui partirait.
    assert rapport["routers"][0]["actions"]

    plan = Plan(
        router_name="pop-test",
        actions=[PlanAction(verb="add", path="/queue/simple", fields={"name": "x"}, name="x")],
    )
    with pytest.raises(RouterWriteDisabledError, match="Devices"):
        await service.apply(plan, dry_run=False, author="ui:exploitant@isp", explicit=True)
    # La simulation, elle, reste permise : elle n'envoie rien.
    simule = await service.apply(plan, dry_run=True, author="ui:exploitant@isp")
    assert simule.dry_run
    assert fabrique.appels == 0 and not fabrique.commandes


async def test_boosts_profils_horaires_et_reset_n_ecrivent_rien(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    depot = _depot_avec_limite()
    depot.echus = [{"scope": "subscriber", "target_key": "dupont"}]
    service = _service(
        settings, routeur, ecriture=False, fabrique=fabrique, depot=depot, metriques=_metriques()
    )
    await service.registry.reload()

    boosts = await service.expire_boosts()
    assert boosts["expired"] == 1  # la base est bien purgee
    assert boosts["read_only"] == ["pop-test"] and boosts["routers"] == []

    profils = await service.apply_schedules(force=True, reason="test")
    assert profils["read_only"] == ["pop-test"] and profils["routers"] == []

    # Reset queues : refuse d'emblee, avant meme de lire les files.
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-dupont", "comment": "freeqos:managed"}
    ]
    with pytest.raises(RouterWriteDisabledError, match="Reset queues"):
        await service.clean_queues("pop-test", author="ui:clean-queues")

    assert fabrique.appels == 0 and not fabrique.commandes


async def test_la_boucle_qoe_decide_mais_n_ecrit_pas_sur_un_routeur_en_lecture_seule(
    settings: Settings, secteur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    depot = DepotQoe(capacity_mbps=200.0)
    service = _service(
        settings,
        secteur,
        ecriture=False,
        fabrique=fabrique,
        depot=depot,
        metriques=MetriquesQoe({"dupont": 18.0, "durand": 22.0, "martin": 93.0}),
    )
    await service.registry.reload()

    resultat = await service.adjust_for_qoe()

    # La decision est prise et enregistree, le plan calcule -- rien ne part.
    assert resultat["sectors"][0]["action"] == "tighten"
    assert resultat["plans"]
    assert resultat["routers"] == []
    assert any(MOTIF_ROUTEUR_LECTURE_SEULE in e for e in resultat["errors"])
    assert fabrique.appels == 0 and not fabrique.commandes


async def test_l_export_netflow_est_calcule_mais_pas_pose(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    service = _service(settings, routeur, ecriture=False, fabrique=fabrique)
    await service.registry.reload()
    export = NetflowExportService(
        shaping=service, registry=service.registry, collector_address="192.0.2.99"
    )

    rapport = await export.apply_all(author="ui:netflow-export", dry_run=False)

    ligne = rapport["routers"][0]
    assert ligne["state"] == ETAT_A_POSER
    assert ligne["write_enabled"] is False
    assert MOTIF_ROUTEUR_LECTURE_SEULE in ligne["reason"]
    assert ligne["actions"]  # la cible a poser est montree
    assert rapport["applied"] == 0
    # Le passage periodique ne fait pas mieux.
    await export.ensure()
    etat = (await export.status())["routers"][0]
    assert etat["write_enabled"] is False and MOTIF_ROUTEUR_LECTURE_SEULE in etat["reason"]
    assert fabrique.appels == 0 and not fabrique.commandes


class Regles:
    """Une restriction active, visant tout le monde."""

    def __init__(self) -> None:
        self.etats: list[tuple[int, str]] = []

    async def list_all(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "name": "bloc-test",
                "enabled": True,
                "scope": "all",
                "action": "block",
                "prefixes": ["93.184.216.34/32"],
            }
        ]

    async def record_apply(self, rule_id: int, *, state: str, detail: str) -> None:
        self.etats.append((rule_id, state))


async def test_les_restrictions_sont_calculees_mais_pas_posees(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    service = _service(settings, routeur, ecriture=False, fabrique=fabrique)
    await service.registry.reload()
    restrictions = RestrictionService(
        shaping=service,
        registry=service.registry,
        rules_repo=Regles(),  # type: ignore[arg-type]
    )

    # Geste explicite (auteur "ui:") : l'interrupteur global le laisserait passer.
    rapport = await restrictions.apply_all(author="ui:exploitant@isp", dry_run=False)

    ligne = rapport["routers"][0]
    assert ligne["actions"] and ligne["applied"] == 0
    assert MOTIF_ROUTEUR_LECTURE_SEULE in ligne["reason"]
    assert fabrique.appels == 0 and not fabrique.commandes

    # TEMOIN : autorise, le meme geste pose bien la restriction.
    settings.routers[0].write_enabled = True
    await service.registry.reload()
    autorise = await restrictions.apply_all(author="ui:exploitant@isp", dry_run=False)
    assert autorise["applied"] > 0 and fabrique.commandes


async def test_le_verdict_dit_ne_peut_pas_avec_le_motif_et_garde_les_droits_du_compte(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    service = _service(settings, routeur, ecriture=False, fabrique=FabriqueEcriture())
    await service.registry.reload()

    verdict = (await service.write_capability("pop-test")).to_dict()

    assert verdict["can_write"] is False
    assert verdict["write_enabled"] is False
    assert verdict["detail"].startswith(MOTIF_ROUTEUR_LECTURE_SEULE)
    # Le compte a tous les droits : rien ne lui manque SUR LE ROUTEUR, et on le
    # sait d'avance pour le jour ou l'on autorise ce routeur.
    assert verdict["account_can_write"] is True
    assert verdict["missing_policies"] == []


# =========================================================================
# 2. Le plan reste calcule et montre, marque lecture seule
# =========================================================================
async def test_la_carte_montre_ce_qui_serait_pose_marque_lecture_seule(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    service = _service(
        settings,
        routeur,
        ecriture=False,
        fabrique=fabrique,
        depot=_depot_avec_limite(),
        metriques=_metriques(),
    )
    await service.registry.reload()

    plan = await service.plan_router("pop-test", prune=False)
    assert plan.actions, "le plan d'un routeur en lecture seule doit rester calcule"

    carte = await service.shaping_points()
    assert carte["read_only_routers"] == ["pop-test"]
    [routeur_carte] = carte["routers"]
    assert routeur_carte["write_enabled"] is False

    def a_plat(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [q for p in points for q in [p, *a_plat(p.get("children") or [])]]

    a_poser = [p for p in a_plat(routeur_carte["points"]) if p["state"] == "file-a-poser"]
    assert a_poser
    assert all(MOTIF_ROUTEUR_LECTURE_SEULE in p["reason"] for p in a_poser)
    assert fabrique.appels == 0


def _api(settings: Settings, **kw: Any) -> TestClient:
    container = build_container(settings, **kw)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def test_l_apercu_du_plan_marque_le_routeur_en_lecture_seule(settings: Settings) -> None:
    settings.routers[0].write_enabled = False
    client = _api(settings)

    corps = client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()
    assert corps["write_enabled"] is False
    assert MOTIF_ROUTEUR_LECTURE_SEULE in corps["read_only_reason"]

    # Reset queues : refuse AVANT de lever les limites forcees.
    reponse = client.post("/api/v1/shaping/routers/pop-test/clean-queues")
    assert reponse.status_code == 409
    assert MOTIF_ROUTEUR_LECTURE_SEULE in reponse.json()["detail"]

    settings.routers[0].write_enabled = True
    assert client.post("/api/v1/shaping/plan", json={"router": "pop-test"}).json()["write_enabled"]


# =========================================================================
# 3. Mise en service : bloquee, avec le geste qui debloque
# =========================================================================
def _conteneur_mise_en_service(*, ecriture_routeur: bool, ecriture_globale: bool) -> Any:
    return SimpleNamespace(
        registry=SimpleNamespace(
            collectors=[
                SimpleNamespace(
                    name="nas-a", config=SimpleNamespace(write_enabled=ecriture_routeur)
                )
            ]
        ),
        scheduler=Planificateur(),
        shaping=ShapingMiseEnService(ecriture=ecriture_globale),
        netflow_export=Export(),
    )


async def test_la_mise_en_service_est_bloquee_avec_le_motif() -> None:
    c = _conteneur_mise_en_service(ecriture_routeur=False, ecriture_globale=True)

    rapport = await provisioning.run(c, "nas-a")

    assert rapport["state"] == "blocked"
    assert rapport["blocker"].startswith(MOTIF_ROUTEUR_LECTURE_SEULE)
    assert "Devices" in rapport["blocker"]
    lances = c.scheduler.lances
    # Rien de ce qui ecrit n'a tourne ; la lecture, si.
    assert JOB_RECONCILE not in lances and JOB_RESTRICTIONS not in lances
    assert "netflow_export" not in lances
    assert JOB_TOPOLOGY in lances


async def test_le_motif_du_routeur_passe_avant_celui_de_l_interrupteur_global() -> None:
    c = _conteneur_mise_en_service(ecriture_routeur=False, ecriture_globale=False)
    rapport = await provisioning.run(c, "nas-a")
    assert rapport["state"] == "blocked"
    assert rapport["blocker"].startswith(MOTIF_ROUTEUR_LECTURE_SEULE)
    assert "also off globally" in rapport["blocker"]


async def test_un_verdict_coupe_dans_freeqos_bloque_aussi() -> None:
    """Meme si la fiche du collecteur ne le dit pas (double), le verdict suffit."""

    class Coupe(ShapingMiseEnService):
        async def write_capability(self, nom: str) -> WriteCapability:
            return WriteCapability(
                username="qos",
                can_write=False,
                detail=MOTIF_ROUTEUR_LECTURE_SEULE,
                write_enabled=False,
                account_can_write=True,
            )

    c = _conteneur_mise_en_service(ecriture_routeur=True, ecriture_globale=True)
    c.shaping = Coupe()
    rapport = await provisioning.run(c, "nas-a")
    assert rapport["state"] == "blocked"
    assert rapport["blocker"].startswith(MOTIF_ROUTEUR_LECTURE_SEULE)


# =========================================================================
# 4. Stockage, API et journal
# =========================================================================
def test_les_routeurs_existants_ecrivent_par_defaut(tmp_path: Path) -> None:
    """Une installation existante doit continuer d'ecrire EXACTEMENT comme avant."""
    assert RouterConfig(name="x", host="10.0.0.1").write_enabled is True
    assert RouterInput(name="x", host="10.0.0.1", password="p").write_enabled is True  # type: ignore[arg-type]
    # Migration : la colonne arrive avec DEFAULT TRUE, donc les lignes deja la
    # gardent l'ecriture (le test d'integration le verifie sur une vraie base).
    schema = (RACINE / "app" / "db" / "schema.sql").read_text(encoding="utf-8")
    assert re.search(
        r"ALTER TABLE routers ADD COLUMN IF NOT EXISTS write_enabled\s+"
        r"BOOLEAN NOT NULL DEFAULT TRUE;",
        schema,
    )
    # Inventaire fichier : le champ est accepte, VRAI quand il est absent.
    fichier = tmp_path / "routers.yml"
    fichier.write_text(
        yaml.safe_dump(
            {
                "routers": [
                    {"name": "nas-a", "host": "10.0.0.1"},
                    {"name": "nas-b", "host": "10.0.0.2", "write_enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    lus = {r.name: r.write_enabled for r in _load_inventory_file(fichier).routers}
    assert lus == {"nas-a": True, "nas-b": False}
    reglages = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql://x/y",
        routers='[{"name": "nas-c", "host": "10.0.0.3", "write_enabled": false}]',  # type: ignore[arg-type]
    )
    assert reglages.routers[0].write_enabled is False


class RoutersEnMemoire(InMemoryRoutersRepository):
    """Le depot en memoire des tests de PoPs, avec l'interrupteur d'ecriture."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lecture_seule: dict[str, str | None] = {}

    async def create(self, payload: dict[str, Any], password: str) -> dict[str, Any]:
        return await super().create({"write_enabled": True, **payload}, password)

    async def write_enabled_of(self, router_id: int) -> bool:
        return bool(self.rows[router_id].get("write_enabled", True))

    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        configs = await super().load_configs(enabled_only=enabled_only)
        par_nom = {row["name"]: row for row in self.rows.values()}
        return [
            c.model_copy(update={"write_enabled": par_nom[c.name].get("write_enabled", True)})
            for c in configs
        ]

    async def read_only_file_routers(self) -> set[str]:
        return set(self.lecture_seule)

    async def set_file_router_read_only(
        self,
        name: str,
        read_only: bool,
        *,
        updated_by: str | None = None,
        reason: str | None = None,
    ) -> None:
        if read_only:
            self.lecture_seule[name] = reason
        else:
            self.lecture_seule.pop(name, None)


class Journal:
    """Double du depot de topologie : seul le journal des bascules est lu."""

    def __init__(self) -> None:
        self.bascules: list[dict[str, Any]] = []

    async def record_write_switch(self, router_name: str, **kwargs: Any) -> None:
        self.bascules.append({"router": router_name, **kwargs})


@pytest.fixture
def api_routeurs(settings: Settings) -> tuple[TestClient, RoutersEnMemoire, Journal]:
    from app.services.crypto import SecretBox, generate_key

    secrets = SecretBox(generate_key())
    depot = RoutersEnMemoire(secrets)
    journal = Journal()
    client = FakeRouterOsClient(identity="chr-nas")
    client.add_session("dupont", rx_byte=1000, tx_byte=2000)
    return (
        _api(
            settings,
            secrets=secrets,
            routers_repo=depot,
            topology_repo=journal,
            client=client,
        ),
        depot,
        journal,
    )


def _ecriture_de(client: TestClient, nom: str) -> bool:
    collecteur = next(c for c in client.container.registry.collectors if c.name == nom)  # type: ignore[attr-defined]
    return bool(collecteur.config.write_enabled)


def test_basculer_l_interrupteur_est_journalise_avec_auteur_et_motif(api_routeurs) -> None:
    client, depot, journal = api_routeurs
    cree = client.post(
        "/api/v1/pops/routers",
        json={
            "name": "nas-pilote",
            "host": "10.10.0.20",
            "password": "secret",
            "write_enabled": False,
        },
    )
    assert cree.status_code == 201, cree.text
    ident = cree.json()["id"]
    assert cree.json()["write_enabled"] is False
    assert _ecriture_de(client, "nas-pilote") is False
    # L'etat de depart est trace lui aussi.
    assert journal.bascules[-1]["write_enabled"] is False
    assert journal.bascules[-1]["before"] is None

    # Autoriser exige une confirmation, comme l'interrupteur global.
    refuse = client.patch(f"/api/v1/pops/routers/{ident}", json={"write_enabled": True})
    assert refuse.status_code == 400 and "confirm" in refuse.json()["detail"]
    assert _ecriture_de(client, "nas-pilote") is False

    autorise = client.patch(
        f"/api/v1/pops/routers/{ident}",
        json={"write_enabled": True, "confirm": True, "reason": "NAS pilote"},
    )
    assert autorise.status_code == 200, autorise.text
    assert autorise.json()["write_enabled"] is True
    assert _ecriture_de(client, "nas-pilote") is True
    dernier = journal.bascules[-1]
    assert (dernier["router"], dernier["before"], dernier["write_enabled"]) == (
        "nas-pilote",
        False,
        True,
    )
    assert dernier["reason"] == "NAS pilote"
    assert dernier["author"].startswith("ui:")

    # Couper est immediat : ni confirmation, ni motif exige.
    coupe = client.patch(f"/api/v1/pops/routers/{ident}", json={"write_enabled": False})
    assert coupe.status_code == 200
    assert _ecriture_de(client, "nas-pilote") is False
    assert journal.bascules[-1]["write_enabled"] is False
    assert "provisioning" not in coupe.json()  # rien a remettre en place

    # Renvoyer la meme valeur ne fabrique pas une fausse bascule.
    nombre = len(journal.bascules)
    client.patch(f"/api/v1/pops/routers/{ident}", json={"write_enabled": False})
    assert len(journal.bascules) == nombre

    liste = {r["name"]: r for r in client.get("/api/v1/pops/routers").json()["routers"]}
    assert liste["nas-pilote"]["write_enabled"] is False


def test_l_api_garde_l_ecriture_par_defaut(api_routeurs) -> None:
    """Une integration qui ne connait pas le champ garde son comportement."""
    client, _, journal = api_routeurs
    cree = client.post(
        "/api/v1/pops/routers", json={"name": "nas-api", "host": "10.10.0.21", "password": "s"}
    )
    assert cree.status_code == 201
    assert cree.json()["write_enabled"] is True
    assert _ecriture_de(client, "nas-api") is True
    assert journal.bascules[-1]["write_enabled"] is True


def test_un_routeur_du_fichier_se_coupe_depuis_l_interface(api_routeurs, settings) -> None:
    client, depot, journal = api_routeurs
    url = "/api/v1/pops/routers/file/pop-test/write"

    coupe = client.put(url, json={"enabled": False, "reason": "observation"})
    assert coupe.status_code == 200, coupe.text
    assert "pop-test" in depot.lecture_seule
    assert _ecriture_de(client, "pop-test") is False
    assert journal.bascules[-1]["write_enabled"] is False
    liste = {r["name"]: r for r in client.get("/api/v1/pops/routers").json()["routers"]}
    assert liste["pop-test"]["write_enabled"] is False
    assert liste["pop-test"]["write_locked"] is False

    assert client.put(url, json={"enabled": True}).status_code == 400
    rendu = client.put(url, json={"enabled": True, "confirm": True, "reason": "pilote"})
    assert rendu.status_code == 200
    assert _ecriture_de(client, "pop-test") is True

    # Le fichier garde le dernier mot : write_enabled: false n'est pas levable ici.
    settings.routers[0].write_enabled = False
    verrou = client.put(url, json={"enabled": True, "confirm": True})
    assert verrou.status_code == 409 and "routers file" in verrou.json()["detail"]
    assert (
        client.put("/api/v1/pops/routers/file/inconnu/write", json={"enabled": False}).status_code
        == 404
    )


async def test_le_registre_applique_la_lecture_seule_d_un_routeur_du_fichier(
    settings: Settings,
) -> None:
    from app.services.crypto import SecretBox, generate_key

    depot = RoutersEnMemoire(SecretBox(generate_key()))
    depot.lecture_seule["pop-test"] = "observation"
    registry = RouterRegistry(settings, repository=depot)  # type: ignore[arg-type]

    [collecteur] = await registry.reload()
    assert collecteur.config.write_enabled is False
    # La configuration chargee au demarrage n'est pas modifiee en place.
    assert settings.routers[0].write_enabled is True
    assert registry.describe()[0]["write_enabled"] is False

    depot.lecture_seule.clear()
    [collecteur] = await registry.reload()
    assert collecteur.config.write_enabled is True


# =========================================================================
# 5. L'interface : decochee par defaut, etat et interrupteur par routeur
# =========================================================================
HTML = (RACINE / "app" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
JS = (RACINE / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def test_la_case_du_formulaire_est_decochee_par_defaut() -> None:
    balise = re.search(r'<input[^>]*id="f-write"[^>]*>', HTML)
    assert balise is not None, "la case d'ecriture manque au formulaire"
    assert 'type="checkbox"' in balise.group(0)
    assert "checked" not in balise.group(0)
    assert "Allow freeQoS to write on this router (queues, NetFlow export, restrictions)" in HTML
    assert "stays read-only" in HTML
    # Le formulaire ENVOIE la valeur, sans compter sur le defaut de l'API.
    assert "write_enabled: document.getElementById('f-write').checked" in JS


def test_chaque_routeur_montre_son_etat_et_son_interrupteur() -> None:
    assert "<th>Writing</th>" in JS
    assert "routerWriteCell(r, ecriture)" in JS
    assert "writing allowed" in JS and "read-only" in JS
    corps = JS[JS.index("async function toggleRouterWrite") :]
    corps = corps[: corps.index("\n}\n")]
    # Autoriser : plan montre, confirmation et motif. Couper : immediat.
    assert "if (active) {" in corps
    assert "confirm(" in corps and "prompt(" in corps
    assert "write_enabled: active, confirm: true, reason: motif" in corps
    assert "/pops/routers/file/" in corps


def test_les_apercus_des_reglages_marquent_les_routeurs_en_lecture_seule() -> None:
    assert "p.write_enabled === false" in JS  # resume avant l'interrupteur global
    assert "plan.write_enabled === false" in JS  # plan d'un routeur
    assert "r.write_enabled === false" in JS  # carte du shaping
    assert "droits.write_enabled === false" in JS  # verdict des droits


# =========================================================================
# 6. Boucle QoE coupee depuis l'interface
# =========================================================================
class DepotQoeLiberable(DepotQoe):
    async def release_qoe_trims(self, reason: str) -> list[str]:
        liberes = [cle for cle, e in self.etats.items() if float(e["trim_factor"]) < 1.0]
        for cle in liberes:
            self.etats[cle] = {
                **self.etats[cle],
                "trim_factor": 1.0,
                "healthy_cycles": 0,
                "last_action": "release",
                "last_reason": reason,
            }
        return sorted(liberes)


class MetriquesComptees(MetriquesQoe):
    def __init__(self, notes: dict[str, float]) -> None:
        super().__init__(notes)
        self.lectures = 0

    async def qoe_subscribers(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.lectures += 1
        return await super().qoe_subscribers(**kwargs)


def test_le_reglage_est_pilotable_depuis_l_interface(settings: Settings) -> None:
    reglage = PAR_NOM["qoe_loop_enabled"]
    assert (reglage.group, reglage.kind) == ("enforcement", "bool")
    assert settings.qoe_loop_enabled is True  # defaut : la boucle tourne
    RuntimeConfig(settings).set("qoe_loop_enabled", "false")
    assert settings.qoe_loop_enabled is False


async def test_boucle_qoe_coupee_reste_inerte_et_rend_les_resserrages(
    settings: Settings, secteur: FakeRouterOsClient
) -> None:
    fabrique = FabriqueEcriture()
    depot = DepotQoeLiberable(capacity_mbps=200.0)
    # Un secteur resserre a 80 % par un cycle precedent.
    depot.etats[LIEN] = {"link_key": LIEN, "trim_factor": 0.8, "healthy_cycles": 0}
    metriques = MetriquesComptees({"dupont": 18.0, "durand": 22.0, "martin": 93.0})
    service = _service(
        settings,
        secteur,
        ecriture=True,
        fabrique=fabrique,
        depot=depot,
        metriques=metriques,
    )
    await service.registry.reload()
    settings.qoe_loop_enabled = False

    # AVANT meme que le job passe, le planificateur n'applique plus le resserrage.
    liens, _ = await service.build_targets("pop-test")
    assert [lien.trim_factor for lien in liens] == [1.0]

    resultat = await service.adjust_for_qoe()

    assert resultat["loop_enabled"] is False
    assert metriques.lectures == 0  # aucune note lue
    assert resultat["sectors"] == [] and resultat["plans"] == []
    assert resultat["released"] == [LIEN]
    assert await depot.qoe_trims() == {}
    assert depot.etats[LIEN]["last_action"] == "release"
    assert not fabrique.commandes  # la boucle n'ecrit rien elle-meme

    # La reconciliation suivante ramene le lien a son enveloppe NORMALE :
    # 200 Mbps * 0.90 de securite = 180 Mbps, et non plus 144.
    await service.reconcile()
    parent = file_du_secteur(fabrique.commandes)
    assert parent is not None and "max-limit=180000000/180000000" in parent

    # Remise en marche : on repart de zero, sans reappliquer l'ancien 80 %.
    settings.qoe_loop_enabled = True
    liens, _ = await service.build_targets("pop-test")
    assert [lien.trim_factor for lien in liens] == [1.0]


def test_le_role_d_un_routeur_n_influe_pas_sur_l_interrupteur() -> None:
    """Coeur et passerelle se pilotent comme un PoP : rien n'est implicite."""
    for role in RouterRole:
        assert RouterConfig(name="r", host="10.0.0.9", role=role).write_enabled is True

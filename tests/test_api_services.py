"""L'onglet Services vu par son API : ce qu'elle promet, et ce qu'elle refuse.

CE QUE CES TESTS PROTEGENT
--------------------------
1. LA VUE EN DIRECT DOIT ETRE EN DIRECT. ``/netflow/connections`` lit l'agregat
   EN MEMOIRE du collecteur, pas la base : c'est la seule reponse a "qu'est-ce
   que ce client fait la, maintenant". Si elle se mettait a lire la base, elle
   rendrait la minute precedente sans que rien ne le signale.
2. UNE ADRESSE VUE IL Y A DEUX SECONDES DOIT DEJA ETRE NOMMEE quand elle tombe
   dans un bloc publie. Attendre le passage de la boucle afficherait "inconnue"
   pour du Netflix parfaitement identifiable.
3. ENREGISTRER UNE REGLE N'ECRIT RIEN. C'est la promesse la plus importante du
   lot : croire qu'un trafic est bloque alors qu'il ne l'est pas est l'erreur
   la plus couteuse que ce produit puisse induire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.netflow import Flow
from app.config import RouterConfig, Settings
from app.enforcement.restrictions import PATH_ADDRESS_LIST, dst_list_name
from app.main import register_routes
from app.services.flows import FlowAggregator, PrefixIndex
from app.services.intel import IntelService
from app.services.netflow_service import NetflowService
from app.services.restrictions import RestrictionService
from tests.conftest import FakeRouterOsClient

MAINTENANT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class FauxDestinations:
    """Depot de destinations en memoire. Assez pour les routes de lecture."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.intel: dict[str, dict[str, Any]] = {}
        self.resolus: list[str] = []
        self.oublies: list[str] = []

    async def top(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    async def by_service(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "service": "netflix",
                "category": "streaming",
                "addresses": 3,
                "clients": 2,
                "subscribers": 1,
                "down_bytes": 900_000,
                "up_bytes": 12_000,
                "last_seen": MAINTENANT,
            }
        ]

    async def detail(self, address: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "address": address,
            "intel": self.intel.get(address),
            "totals": {
                "down_bytes": 900_000,
                "up_bytes": 12_000,
                "clients": 2,
                "subscribers": 1,
            },
            "clients": [
                {
                    "client": "10.0.0.2",
                    "subscriber_id": 1,
                    "login": "dupont",
                    "kind": "pppoe",
                    "pop_name": "PoP Test",
                    "port": 443,
                    "protocol": 6,
                    "app": "web",
                    "down_bytes": 900_000,
                    "up_bytes": 12_000,
                    "flows": 40,
                    "first_seen": MAINTENANT,
                    "last_seen": MAINTENANT,
                },
                {
                    "client": "10.9.9.9",
                    "subscriber_id": None,
                    "login": None,
                    "kind": None,
                    "pop_name": None,
                    "port": 0,
                    "protocol": 1,
                    "app": "diagnostic",
                    "down_bytes": 84,
                    "up_bytes": 84,
                    "flows": 1,
                    "first_seen": MAINTENANT,
                    "last_seen": MAINTENANT,
                },
            ],
        }

    async def intel_for(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        return {a: self.intel[a] for a in addresses if a in self.intel}

    async def get_intel(self, address: str) -> dict[str, Any] | None:
        return self.intel.get(address)

    async def pending(self, **kwargs: Any) -> list[str]:
        # La file d'attente est vide : ces tests portent sur les routes, pas sur
        # la boucle d'enrichissement, qui a les siens.
        return []

    async def count_pending(self, **kwargs: Any) -> int:
        return 0

    async def forget_resolution(self, address: str) -> None:
        self.oublies.append(address)

    async def save_intel(self, verdicts: list[dict[str, Any]]) -> int:
        for verdict in verdicts:
            self.intel[str(verdict["address"])] = dict(verdict)
            self.resolus.append(str(verdict["address"]))
        return len(verdicts)

    async def addresses_for(self, **kwargs: Any) -> list[str]:
        return []


class FauxFlows:
    async def subscribers_by_id(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        return {
            1: {
                "id": 1,
                "login": "dupont",
                "kind": "pppoe",
                "pop_name": "PoP Test",
                "plan_down_mbps": 100.0,
                "plan_up_mbps": 20.0,
            }
        }

    async def prefixes_for_logins(self, logins: list[str]) -> dict[str, list[str]]:
        return {login: ["10.0.0.2/32"] for login in logins}


class FauxRegles:
    """Depot de regles en memoire, avec le meme contrat que le vrai."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.suivant = 1

    async def list_all(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["enabled"] or not enabled_only]

    async def get(self, rule_id: int) -> dict[str, Any]:
        from app.db.traffic_rules_repo import RuleNotFoundError

        if rule_id not in self.rows:
            raise RuleNotFoundError(f"regle {rule_id} inconnue")
        return dict(self.rows[rule_id])

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        from app.db.traffic_rules_repo import RuleConflictError

        if any(r["name"] == payload["name"] for r in self.rows.values()):
            raise RuleConflictError("une regle s'appelle deja ainsi")
        ligne = {
            "id": self.suivant,
            "last_applied_at": None,
            "last_state": None,
            "last_detail": None,
            **payload,
        }
        self.rows[self.suivant] = ligne
        self.suivant += 1
        return dict(ligne)

    async def update(self, rule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        ligne = await self.get(rule_id)
        ligne.update(payload)
        self.rows[rule_id] = ligne
        return dict(ligne)

    async def delete(self, rule_id: int) -> None:
        await self.get(rule_id)
        del self.rows[rule_id]

    async def record_apply(self, rule_id: int, *, state: str, detail: str) -> None:
        self.rows[rule_id]["last_state"] = state
        self.rows[rule_id]["last_detail"] = detail
        self.rows[rule_id]["last_applied_at"] = MAINTENANT


@pytest.fixture
def netflow() -> NetflowService:
    """Un collecteur avec une fenetre DEJA remplie, sans socket ni datagramme.

    On alimente l'agregateur directement : ce qui est teste ici est la route,
    pas le decodage -- celui-ci a ses propres tests.
    """
    service = NetflowService(
        enabled=True,
        aggregator=FlowAggregator(index=PrefixIndex.build([("10.0.0.0/29", 1)])),
    )
    service.aggregator.add(
        Flow(
            src="45.57.12.34",
            dst="10.0.0.2",
            src_port=443,
            dst_port=51000,
            protocol=6,
            octets=900_000,
            packets=700,
        ),
        vantage="edge",
    )
    return service


@pytest.fixture
def destinations() -> FauxDestinations:
    depot = FauxDestinations()
    depot.rows = [
        {
            "address": "45.57.12.34",
            "hostname": "ipv4-c001.1.oca.nflxvideo.net",
            "service": "netflix",
            "category": "streaming",
            "source": "nom inverse",
            "org": None,
            "asn": None,
            "country": None,
            "resolved_at": MAINTENANT,
            "clients": 2,
            "subscribers": 1,
            "down_bytes": 900_000,
            "up_bytes": 12_000,
            "flows": 40,
            "last_seen": MAINTENANT,
            "first_seen": MAINTENANT,
            "port": 443,
            "protocol": 6,
            "app": "web",
        }
    ]
    return depot


@pytest.fixture
def restrictions(settings: Settings, router_config: RouterConfig) -> RestrictionService:
    client = FakeRouterOsClient()
    collector = MikrotikCollector(router_config, client=client)
    registry = SimpleNamespace(collectors=[collector])
    # L'ecriture est coupee : c'est l'etat par defaut du controleur, et celui
    # dans lequel ces tests doivent verifier qu'on ne pose rien.
    shaping = SimpleNamespace(enforcement_enabled=False)
    return RestrictionService(
        shaping=shaping,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        rules_repo=FauxRegles(),  # type: ignore[arg-type]
        destinations=FauxDestinations(),  # type: ignore[arg-type]
        flows_repo=FauxFlows(),  # type: ignore[arg-type]
    )


@pytest.fixture
def container(
    settings: Settings,
    netflow: NetflowService,
    destinations: FauxDestinations,
    restrictions: RestrictionService,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        netflow=netflow,
        flows_repo=FauxFlows(),
        destinations_repo=destinations,
        traffic_rules_repo=restrictions.rules_repo,
        restrictions=restrictions,
        intel=IntelService(destinations=destinations, rdns_enabled=False),  # type: ignore[arg-type]
        exporters_repo=None,
    )


@pytest.fixture
def client(settings: Settings, container: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


# ===================================================================== lecture


def test_les_connexions_en_cours_viennent_de_la_memoire_du_collecteur(
    client: TestClient, netflow: NetflowService
) -> None:
    """LA SEULE VUE EN DIRECT. Elle doit rendre ce que la fenetre porte a cet
    instant, y compris ce qui n'a jamais ete ecrit en base."""
    corps = client.get("/api/v1/netflow/connections").json()
    assert corps["tracked"] is True
    assert len(corps["connections"]) == 1
    ligne = corps["connections"][0]
    assert ligne["address"] == "45.57.12.34"
    assert ligne["down_bytes"] == 900_000
    # L'identifiant est traduit en login : '#1' n'aide personne. L'adresse du
    # client est rendue en plus, pour les machines qui n'ont pas de fiche.
    assert ligne["login"] == "dupont"
    assert ligne["client"] == "10.0.0.2"
    assert ligne["pop_name"] == "PoP Test"
    # La fenetre n'a pas ete consommee par la lecture.
    assert netflow.aggregator.destinations_in_window == 1


def test_une_adresse_pas_encore_enrichie_est_quand_meme_nommee(client: TestClient) -> None:
    """LE CATALOGUE REPOND TOUT DE SUITE. Afficher "inconnue" pour du Netflix
    parfaitement identifiable, le temps qu'une boucle passe, ferait douter de
    l'outil au premier regard."""
    ligne = client.get("/api/v1/netflow/connections").json()["connections"][0]
    assert ligne["service"] == "netflix"
    assert ligne["category"] == "streaming"
    assert ligne["source"] == "catalogue"
    # Et on dit qu'elle n'a pas encore ete enrichie, plutot que de le taire.
    assert ligne["pending"] is True


def test_les_destinations_rendent_aussi_la_repartition_par_service(client: TestClient) -> None:
    corps = client.get("/api/v1/netflow/destinations?minutes=60").json()
    assert corps["destinations"][0]["service"] == "netflix"
    assert corps["services"][0]["category"] == "streaming"


def test_la_fiche_d_une_adresse_dit_qui_la_joint(client: TestClient) -> None:
    """Y COMPRIS CE QUI N'A PAS DE FICHE. Une machine non declaree joint bien
    cette adresse : la masquer ferait disparaitre de la liste ce qu'on vient
    justement de lancer pour verifier."""
    corps = client.get("/api/v1/netflow/destinations/45.57.12.34").json()
    assert corps["address"] == "45.57.12.34"
    assert corps["clients"][0]["login"] == "dupont"
    assert corps["clients"][1]["client"] == "10.9.9.9"
    assert corps["clients"][1]["login"] is None
    # Le verdict du catalogue est recalcule a la volee, meme sans rien en base.
    assert corps["catalogue"]["service"] == "netflix"
    assert corps["catalogue"]["matched_prefix"] == "45.57.0.0/17"


def test_une_adresse_invalide_est_refusee_proprement(client: TestClient) -> None:
    reponse = client.get("/api/v1/netflow/destinations/pas-une-adresse")
    assert reponse.status_code == 422
    assert "invalide" in reponse.json()["detail"]


def test_relancer_l_analyse_remet_l_adresse_dans_la_file(
    client: TestClient, destinations: FauxDestinations
) -> None:
    reponse = client.post("/api/v1/netflow/destinations/45.57.12.34/resolve")
    assert reponse.status_code == 200
    assert destinations.oublies == ["45.57.12.34"]
    # Et le verdict est reecrit dans la foulee, sans attendre la boucle.
    assert destinations.intel["45.57.12.34"]["service"] == "netflix"


def test_le_catalogue_dit_ce_qu_il_sait_et_ce_qu_il_ignore(client: TestClient) -> None:
    corps = client.get("/api/v1/netflow/catalogue").json()
    par_cle = {s["key"]: s for s in corps["services"]}
    assert par_cle["netflix"]["prefixes"] > 0
    # YouTube n'a aucun bloc propre : la page doit pouvoir le DIRE, sinon un
    # exploitant croira que la regle part avec des adresses.
    assert par_cle["youtube"]["prefixes"] == 0
    assert par_cle["cloudflare"]["note"]
    assert "streaming" in corps["categories"]


# ================================================================ restrictions


def regle(**kwargs: Any) -> dict[str, Any]:
    corps: dict[str, Any] = {"name": "Pas de Netflix", "services": ["netflix"]}
    corps.update(kwargs)
    return corps


def test_enregistrer_une_regle_n_ecrit_rien_sur_les_routeurs(
    client: TestClient, restrictions: RestrictionService
) -> None:
    """LA PROMESSE LA PLUS IMPORTANTE DU LOT. Une regle saisie est une
    intention ; la pose est un geste separe, et il reste soumis a
    l'interrupteur d'ecriture."""
    reponse = client.post("/api/v1/traffic-rules", json=regle())
    assert reponse.status_code == 201
    assert reponse.json()["last_applied_at"] is None

    collector = restrictions.registry.collectors[0]
    assert collector._client.firewall_address_list_rows == []  # noqa: SLF001


def test_une_regle_sans_critere_est_refusee_par_l_api(client: TestClient) -> None:
    reponse = client.post(
        "/api/v1/traffic-rules", json={"name": "vide", "services": [], "categories": []}
    )
    assert reponse.status_code == 422
    assert "designer du trafic" in reponse.json()["detail"]


def test_un_service_inconnu_est_refuse_par_l_api(client: TestClient) -> None:
    reponse = client.post("/api/v1/traffic-rules", json=regle(services=["netflixx"]))
    assert reponse.status_code == 422


def test_deux_regles_ne_peuvent_pas_porter_le_meme_nom(client: TestClient) -> None:
    assert client.post("/api/v1/traffic-rules", json=regle()).status_code == 201
    assert client.post("/api/v1/traffic-rules", json=regle()).status_code == 409


def test_vider_les_criteres_d_une_regle_existante_est_refuse(client: TestClient) -> None:
    """LA VALIDATION PORTE SUR LA REGLE TELLE QU'ELLE SERA, pas sur le fragment
    envoye : retirer le dernier service la rendrait sans critere, donc visant
    tout internet."""
    cree = client.post("/api/v1/traffic-rules", json=regle()).json()
    reponse = client.patch(f"/api/v1/traffic-rules/{cree['id']}", json={"services": []})
    assert reponse.status_code == 422
    assert "designer du trafic" in reponse.json()["detail"]


def test_suspendre_une_regle_ne_la_supprime_pas(client: TestClient) -> None:
    cree = client.post("/api/v1/traffic-rules", json=regle()).json()
    reponse = client.patch(f"/api/v1/traffic-rules/{cree['id']}", json={"enabled": False})
    assert reponse.status_code == 200
    assert reponse.json()["enabled"] is False
    assert len(client.get("/api/v1/traffic-rules").json()["rules"]) == 1


def test_l_apercu_dit_ce_que_la_regle_vise_aujourd_hui(client: TestClient) -> None:
    """Une regle est un critere, pas une photo : avant de la poser, il faut
    pouvoir regarder ce qu'elle couvre reellement."""
    cree = client.post("/api/v1/traffic-rules", json=regle()).json()
    vue = client.get(f"/api/v1/traffic-rules/{cree['id']}/preview").json()
    assert vue["address_count"] > 0
    assert "45.57.0.0/17" in vue["addresses"]
    assert vue["routers"] == ["pop-test"]


def test_une_regle_par_abonne_borne_le_cote_client(client: TestClient) -> None:
    cree = client.post(
        "/api/v1/traffic-rules",
        json=regle(name="Netflix chez Dupont", scope="subscribers", logins=["dupont"]),
    ).json()
    vue = client.get(f"/api/v1/traffic-rules/{cree['id']}/preview").json()
    assert vue["clients"] == ["10.0.0.2/32"]


def test_la_pose_est_une_simulation_par_defaut(client: TestClient) -> None:
    client.post("/api/v1/traffic-rules", json=regle())
    rapport = client.post("/api/v1/traffic-rules/apply").json()
    assert rapport["dry_run"] is True
    assert rapport["applied"] == 0
    # Les commandes sont MONTREES : c'est ce que l'exploitant valide.
    routeur = rapport["routers"][0]
    assert routeur["router"] == "pop-test"
    assert any(PATH_ADDRESS_LIST.split("/")[-1] in a or "freeqos" in a for a in routeur["actions"])


def test_l_ecriture_reste_bloquee_tant_que_l_enforcement_est_coupe(client: TestClient) -> None:
    """Meme en demandant explicitement l'ecriture. Le drapeau global a le
    dernier mot, et la reponse dit pourquoi plutot que d'echouer."""
    client.post("/api/v1/traffic-rules", json=regle())
    rapport = client.post("/api/v1/traffic-rules/apply?dry_run=false").json()
    assert rapport["enforcement_enabled"] is False
    assert rapport["state"] == "a poser"
    assert "enforcement est desactive" in rapport["routers"][0]["reason"]


def test_le_plan_vise_la_liste_de_la_regle(client: TestClient) -> None:
    cree = client.post("/api/v1/traffic-rules", json=regle()).json()
    rapport = client.post("/api/v1/traffic-rules/apply").json()
    commandes = " ".join(rapport["routers"][0]["actions"])
    assert dst_list_name(cree["id"]) in commandes


def test_supprimer_une_regle_ne_nettoie_pas_le_routeur_en_douce(client: TestClient) -> None:
    """Supprimer une fiche ne doit pas declencher une ecriture sur des
    equipements de production sans que personne ne l'ait demande. C'est la
    reconciliation qui nettoie, et elle est observable."""
    cree = client.post("/api/v1/traffic-rules", json=regle()).json()
    assert client.delete(f"/api/v1/traffic-rules/{cree['id']}").status_code == 204
    assert client.get("/api/v1/traffic-rules").json()["rules"] == []


# ================================================== la boucle qui nomme


class FauxFile:
    """Une file d'attente d'enrichissement, en memoire.

    Elle imite le contrat exact du depot reel : ``pending`` ne rend que ce qui
    n'a pas encore ete resolu, et ``save_intel`` marque la resolution. C'est ce
    couple qui fait que la decouverte se termine au lieu de tourner en rond.
    """

    def __init__(self, attente: list[str]) -> None:
        self.attente = list(attente)
        self.enregistres: list[dict[str, Any]] = []

    async def pending(self, *, limit: int = 50, max_attempts: int = 3) -> list[str]:
        return self.attente[:limit]

    async def count_pending(self, **kwargs: Any) -> int:
        return len(self.attente)

    async def save_intel(self, verdicts: list[dict[str, Any]]) -> int:
        self.enregistres.extend(verdicts)
        for verdict in verdicts:
            if verdict["address"] in self.attente:
                self.attente.remove(verdict["address"])
        return len(verdicts)

    async def forget_resolution(self, address: str) -> None:
        self.attente.append(address)

    async def get_intel(self, address: str) -> dict[str, Any] | None:
        for verdict in reversed(self.enregistres):
            if verdict["address"] == address:
                return verdict
        return None


async def test_une_adresse_vue_est_nommee_au_passage_suivant() -> None:
    """C'EST TOUTE LA PROMESSE DU DYNAMIQUE. Personne ne declare l'adresse : le
    fait qu'un client l'ait atteinte la met en file, et la boucle la nomme."""
    file = FauxFile(["45.57.12.34"])
    service = IntelService(destinations=file, rdns_enabled=False)  # type: ignore[arg-type]

    assert await service.resolve_pending() == 1
    verdict = file.enregistres[0]
    assert verdict["service"] == "netflix"
    assert verdict["category"] == "streaming"
    # La file s'est videe : sans cela, la meme adresse serait redemandee a
    # chaque passage, pour toujours.
    assert file.attente == []


async def test_une_adresse_sans_nom_est_quand_meme_marquee_resolue() -> None:
    """ "Cette adresse n'a pas de nom" EST une reponse. Sans date de resolution,
    la majorite d'internet -- qui n'a pas de nom inverse -- resterait en file et
    genererait une requete DNS perpetuelle."""
    file = FauxFile(["198.51.100.7"])
    service = IntelService(destinations=file, rdns_enabled=False)  # type: ignore[arg-type]

    assert await service.resolve_pending() == 1
    verdict = file.enregistres[0]
    assert verdict["service"] is None
    assert verdict["resolved_at"] is not None


async def test_le_nom_inverse_se_lit_sans_toucher_au_reseau_dans_les_tests() -> None:
    """Le resolveur est injecte : la boucle ne doit jamais dependre d'un DNS
    joignable pour etre testee -- ni, en exploitation, pour rendre la main."""
    file = FauxFile(["203.0.113.9"])
    service = IntelService(destinations=file, rdns_enabled=True)  # type: ignore[arg-type]
    service.resolver.cache["203.0.113.9"] = "ipv4-c001.1.oca.nflxvideo.net"

    await service.resolve_pending()
    verdict = file.enregistres[0]
    assert verdict["service"] == "netflix"
    assert verdict["source"] == "nom inverse"
    assert verdict["hostname"] == "ipv4-c001.1.oca.nflxvideo.net"


async def test_l_enrichissement_coupe_ne_consomme_pas_la_file() -> None:
    file = FauxFile(["45.57.12.34"])
    service = IntelService(destinations=file, enabled=False)  # type: ignore[arg-type]
    assert await service.resolve_pending() == 0
    assert file.attente == ["45.57.12.34"]


async def test_l_etat_dit_combien_d_adresses_attendent() -> None:
    """UN COMPTEUR QUI NE DESCEND JAMAIS dit une chose precise : le resolveur
    ne repond pas, ou la cadence est trop lente pour ce qui est decouvert."""
    file = FauxFile(["45.57.12.34", "8.8.8.8"])
    service = IntelService(destinations=file, rdns_enabled=False)  # type: ignore[arg-type]
    assert (await service.status())["pending"] == 2
    await service.resolve_pending()
    etat = await service.status()
    assert etat["pending"] == 0
    assert etat["resolved"] == 2


async def test_une_machine_sans_fiche_apparait_dans_les_connexions_en_cours(
    settings: Settings, destinations: FauxDestinations, restrictions: RestrictionService
) -> None:
    """LE CAS QUI A REVELE L'ANGLE MORT : un ping lance depuis un poste qui
    n'est pas un abonne declare. Le flux traverse le reseau, le collecteur le
    voit, et l'interface doit le montrer -- sous l'adresse du poste."""
    netflow = NetflowService(
        enabled=True,
        # L'espace client se declare SUR LE SERVICE : c'est lui qui le repose
        # sur l'agregateur au demarrage, et le poser sur l'agregateur seul
        # serait efface.
        customer_networks=("10.0.0.0/8",),
        aggregator=FlowAggregator(index=PrefixIndex.build([("10.0.0.0/29", 1)])),
    )
    netflow.aggregator.add(
        Flow(src="10.9.9.9", dst="45.57.12.34", protocol=1, octets=84, packets=1),
        vantage="edge",
    )
    conteneur = SimpleNamespace(
        settings=settings,
        netflow=netflow,
        flows_repo=FauxFlows(),
        destinations_repo=destinations,
        traffic_rules_repo=restrictions.rules_repo,
        restrictions=restrictions,
        intel=IntelService(destinations=destinations, rdns_enabled=False),  # type: ignore[arg-type]
        exporters_repo=None,
    )
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: conteneur

    with TestClient(app) as http:
        corps = http.get("/api/v1/netflow/connections").json()

    assert len(corps["connections"]) == 1
    ligne = corps["connections"][0]
    assert ligne["client"] == "10.9.9.9"
    assert ligne["login"] is None
    assert ligne["address"] == "45.57.12.34"
    assert ligne["service"] == "netflix"

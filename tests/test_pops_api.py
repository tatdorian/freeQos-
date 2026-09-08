"""Connexion d'un PoP depuis l'interface.

Ces endpoints ecrivent en BASE (quels routeurs interroger), jamais sur un
equipement : le controleur reste hors-bande.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import RouterConfig, RouterRole, Settings
from app.db.routers_repo import DuplicateRouterError, RouterNotFoundError
from app.main import register_routes
from app.services.crypto import SecretBox, generate_key
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container


class InMemoryRoutersRepository:
    """Depot en memoire, avec le meme contrat que la version PostgreSQL."""

    def __init__(self, secrets: SecretBox) -> None:
        self._secrets = secrets
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if k != "password_enc"}

    async def list_public(self) -> list[dict[str, Any]]:
        return [self._public(row) for row in self.rows.values()]

    async def get_public(self, router_id: int) -> dict[str, Any]:
        if router_id not in self.rows:
            raise RouterNotFoundError(f"routeur {router_id} inconnu")
        return self._public(self.rows[router_id])

    async def create(self, payload: dict[str, Any], password: str) -> dict[str, Any]:
        if any(r["name"] == payload["name"] for r in self.rows.values()):
            raise DuplicateRouterError(f"un routeur nomme '{payload['name']}' existe deja")
        router_id = self._next_id
        self._next_id += 1
        self.rows[router_id] = {
            "id": router_id,
            "password_enc": self._secrets.encrypt(password),
            "last_ok_at": None,
            "last_error": None,
            "identity": None,
            "board_name": None,
            "routeros_version": None,
            **payload,
        }
        return self._public(self.rows[router_id])

    async def update(
        self, router_id: int, payload: dict[str, Any], password: str | None = None
    ) -> dict[str, Any]:
        if router_id not in self.rows:
            raise RouterNotFoundError(f"routeur {router_id} inconnu")
        self.rows[router_id].update({k: v for k, v in payload.items() if v is not None})
        if password:
            self.rows[router_id]["password_enc"] = self._secrets.encrypt(password)
        return self._public(self.rows[router_id])

    async def delete(self, router_id: int) -> None:
        if self.rows.pop(router_id, None) is None:
            raise RouterNotFoundError(f"routeur {router_id} inconnu")

    async def record_success(self, router_id: int, info: dict[str, Any]) -> None:
        self.rows[router_id].update(
            {
                "last_ok_at": "now",
                "last_error": None,
                "identity": info.get("identity"),
                "board_name": info.get("board_name"),
                "routeros_version": info.get("version"),
            }
        )

    async def record_failure(self, router_id: int, error: str) -> None:
        self.rows[router_id]["last_error"] = error

    async def find_id_by_name(self, name: str) -> int | None:
        for router_id, row in self.rows.items():
            if row["name"] == name:
                return router_id
        return None

    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        configs = []
        for row in self.rows.values():
            if enabled_only and not row.get("enabled", True):
                continue
            configs.append(
                RouterConfig(
                    name=row["name"],
                    host=row["host"],
                    port=row.get("port", 8728),
                    username=row.get("username", "qos-ro"),
                    password=self._secrets.decrypt(row["password_enc"]),
                    role=RouterRole(row.get("role", "pop")),
                    pop_name=row.get("pop_name"),
                    enabled=row.get("enabled", True),
                    use_ssl=row.get("use_ssl", False),
                    timeout_s=row.get("timeout_s", 5.0),
                    pppoe_interface_pattern=row.get("pppoe_interface_pattern", "<pppoe-{login}>"),
                )
            )
        return configs


NOUVEAU_POP = {
    "name": "pop-sud",
    "host": "10.10.0.12",
    "password": "secret-du-routeur",
    "username": "qos-ro",
    "pop_name": "PoP Sud",
}


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox(generate_key())


@pytest.fixture
def repo(secrets: SecretBox) -> InMemoryRoutersRepository:
    return InMemoryRoutersRepository(secrets)


@pytest.fixture
def fake_router() -> FakeRouterOsClient:
    client = FakeRouterOsClient(identity="chr-pop-sud")
    client.add_session("dupont", rx_byte=1000, tx_byte=2000)
    return client


@pytest.fixture
def client(
    settings: Settings,
    secrets: SecretBox,
    repo: InMemoryRoutersRepository,
    fake_router: FakeRouterOsClient,
) -> TestClient:
    container = build_container(settings, secrets=secrets, routers_repo=repo, client=fake_router)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    test_client = TestClient(app)
    test_client.container = container  # type: ignore[attr-defined]
    return test_client


# ----------------------------------------------------------------- lecture
def test_inventaire_expose_les_deux_sources(
    client: TestClient, repo: InMemoryRoutersRepository
) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    body = client.get("/api/v1/pops/routers").json()

    par_nom = {r["name"]: r for r in body["routers"]}
    assert par_nom["pop-test"]["source"] == "file"
    assert par_nom["pop-test"]["editable"] is False
    assert par_nom["pop-sud"]["source"] == "db"
    assert par_nom["pop-sud"]["editable"] is True
    assert body["secrets_available"] is True


# --------------------------------------------------------------- creation
def test_creation_et_prise_en_compte_a_chaud(client: TestClient) -> None:
    """Le nouveau PoP doit etre interroge sans redemarrage."""
    avant = {c.name for c in client.container.collection.collectors}  # type: ignore[attr-defined]
    assert "pop-sud" not in avant

    response = client.post("/api/v1/pops/routers", json=NOUVEAU_POP)

    assert response.status_code == 201
    assert response.json()["name"] == "pop-sud"
    apres = {c.name for c in client.container.collection.collectors}  # type: ignore[attr-defined]
    assert "pop-sud" in apres


def test_le_mot_de_passe_ne_ressort_jamais(client: TestClient) -> None:
    cree = client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    liste = client.get("/api/v1/pops/routers")

    for corps in (cree.text, liste.text):
        assert "secret-du-routeur" not in corps
        assert "password" not in corps.lower()


def test_le_mot_de_passe_est_chiffre_en_base(
    client: TestClient, repo: InMemoryRoutersRepository
) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    stocke = repo.rows[1]["password_enc"]

    assert "secret-du-routeur" not in stocke
    assert stocke.startswith("fernet:")


def test_nom_deja_pris(client: TestClient) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    doublon = client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    assert doublon.status_code == 409


def test_sans_cle_de_chiffrement_l_ecriture_est_refusee(
    settings: Settings, repo: InMemoryRoutersRepository, fake_router: FakeRouterOsClient
) -> None:
    """Plutot que d'ecrire un secret en clair, l'API refuse et le dit."""
    container = build_container(
        settings, secrets=SecretBox(None), routers_repo=repo, client=fake_router
    )
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container

    response = TestClient(app).post("/api/v1/pops/routers", json=NOUVEAU_POP)

    assert response.status_code == 409
    assert "APP_SECRET_KEY" in response.json()["detail"]
    assert repo.rows == {}


def test_validation_des_entrees(client: TestClient) -> None:
    mauvais = {**NOUVEAU_POP, "port": 99999}
    assert client.post("/api/v1/pops/routers", json=mauvais).status_code == 422
    assert client.post("/api/v1/pops/routers", json={"name": "x"}).status_code == 422


# ------------------------------------------------------------ modification
def test_modification_partielle_sans_resaisir_le_secret(
    client: TestClient, repo: InMemoryRoutersRepository
) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    avant = repo.rows[1]["password_enc"]

    response = client.patch("/api/v1/pops/routers/1", json={"host": "10.10.0.99"})

    assert response.status_code == 200
    assert response.json()["host"] == "10.10.0.99"
    assert repo.rows[1]["password_enc"] == avant


def test_desactivation_retire_le_collecteur(client: TestClient) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    assert "pop-sud" in {c.name for c in client.container.collection.collectors}  # type: ignore[attr-defined]

    client.patch("/api/v1/pops/routers/1", json={"enabled": False})

    assert "pop-sud" not in {c.name for c in client.container.collection.collectors}  # type: ignore[attr-defined]


def test_modification_d_un_routeur_inconnu(client: TestClient) -> None:
    assert client.patch("/api/v1/pops/routers/999", json={"host": "x"}).status_code == 404


# -------------------------------------------------------------- suppression
def test_suppression(client: TestClient, repo: InMemoryRoutersRepository) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)

    assert client.delete("/api/v1/pops/routers/1").status_code == 204

    assert repo.rows == {}
    assert "pop-sud" not in {c.name for c in client.container.collection.collectors}  # type: ignore[attr-defined]


def test_suppression_d_un_routeur_inconnu(client: TestClient) -> None:
    assert client.delete("/api/v1/pops/routers/999").status_code == 404


# ------------------------------------------------------------------ sonde
def test_test_de_connexion_reussi(client: TestClient) -> None:
    response = client.post("/api/v1/pops/routers/test", json=NOUVEAU_POP)

    body = response.json()
    assert body["reachable"] is True
    assert body["identity"] == "chr-pop-sud"
    assert body["board_name"] == "CHR"
    assert body["version"].startswith("7.21.5")
    assert body["ppp_active_sessions"] == 1
    # Le point qui compte : sans correlation, aucun debit ne sera calculable.
    assert body["correlated_sessions"] == 1


def test_test_de_connexion_echoue_avec_un_conseil(
    client: TestClient, fake_router: FakeRouterOsClient
) -> None:
    fake_router.raise_on_ppp = TimeoutError("timed out")

    body = client.post("/api/v1/pops/routers/test", json=NOUVEAU_POP).json()

    assert body["reachable"] is False
    assert "TimeoutError" in body["error"]
    # Le message doit dire quoi faire, pas seulement que ca a echoue.
    assert "nc -zv 10.10.0.12 8728" in body["hint"]


def test_conseil_specifique_aux_identifiants(
    client: TestClient, fake_router: FakeRouterOsClient
) -> None:
    fake_router.raise_on_ppp = RuntimeError("cannot log in")
    body = client.post("/api/v1/pops/routers/test", json=NOUVEAU_POP).json()
    assert "policy=read,api,test" in body["hint"]


def test_le_test_ne_persiste_rien(client: TestClient, repo: InMemoryRoutersRepository) -> None:
    client.post("/api/v1/pops/routers/test", json=NOUVEAU_POP)
    assert repo.rows == {}


def test_sonde_d_un_routeur_enregistre(client: TestClient, repo: InMemoryRoutersRepository) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)

    body = client.post("/api/v1/pops/routers/1/probe").json()

    assert body["reachable"] is True
    # Le diagnostic est memorise pour etre affiche dans l'inventaire.
    assert repo.rows[1]["identity"] == "chr-pop-sud"
    assert repo.rows[1]["last_error"] is None


def test_sonde_en_echec_memorisee(
    client: TestClient, repo: InMemoryRoutersRepository, fake_router: FakeRouterOsClient
) -> None:
    client.post("/api/v1/pops/routers", json=NOUVEAU_POP)
    fake_router.raise_on_ppp = ConnectionRefusedError("connection refused")

    body = client.post("/api/v1/pops/routers/1/probe").json()

    assert body["reachable"] is False
    assert "refus" in repo.rows[1]["last_error"].lower() or "refused" in repo.rows[1]["last_error"]

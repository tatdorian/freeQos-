"""Connexion et grades : ce que le SERVEUR permet, pas ce que l'interface montre.

CE QUE CES TESTS FIXENT
-----------------------
1. SANS COMPTE, ON EN CREE UN -- et un seul, en edition. Apres lui, l'ecran
   d'initialisation est ferme.
2. SANS SESSION, RIEN. Toute route d'exploitation repond 401.
3. LA LECTURE SEULE EST TENUE PAR L'API. Un compte 'read' lit tout et toute
   ecriture lui est refusee (403), quelle que soit la route -- masquer un
   bouton n'aurait rien interdit.
4. L'EDITION GERE LES COMPTES, sans jamais pouvoir supprimer le dernier
   compte d'edition.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.db.users_repo import InMemoryUsersRepository
from app.main import register_routes
from app.services.accounts import hash_password, verify_password
from tests.test_api import build_container

MDP = "correct-horse-1"


@pytest.fixture
def users() -> InMemoryUsersRepository:
    return InMemoryUsersRepository()


@pytest.fixture
def app_client(settings: Settings, users: InMemoryUsersRepository) -> TestClient:
    reglages = settings.model_copy(update={"auth_enabled": True})
    container = build_container(reglages)
    container.users_repo = users
    app = FastAPI()
    app.state.settings = reglages
    register_routes(app, reglages)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app, base_url="https://testserver")


def setup(client: TestClient, email: str = "admin@exemple.fr") -> None:
    r = client.post("/api/v1/auth/setup", json={"email": email, "password": MDP})
    assert r.status_code == 200, r.text


def login(client: TestClient, email: str, password: str = MDP) -> int:
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code


# ============================================================ premier compte


def test_sans_compte_l_interface_propose_d_en_creer_un(app_client: TestClient) -> None:
    corps = app_client.get("/api/v1/auth/status").json()
    assert corps["setup_required"] is True
    assert corps["user"] is None


def test_le_premier_compte_est_en_edition_et_ouvre_une_session(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    r = app_client.post("/api/v1/auth/setup", json={"email": " Admin@Exemple.FR ", "password": MDP})
    assert r.json()["user"] == {"id": 1, "email": "admin@exemple.fr", "role": "edit"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert app_client.get("/api/v1/auth/status").json()["user"]["role"] == "edit"
    # Le mot de passe n'est stocke qu'en empreinte.
    stocke = users.users[1]["password_hash"]
    assert MDP not in stocke and verify_password(MDP, stocke)


def test_l_initialisation_ne_sert_qu_une_fois(app_client: TestClient) -> None:
    setup(app_client)
    r = app_client.post("/api/v1/auth/setup", json={"email": "pirate@x.fr", "password": MDP})
    assert r.status_code == 409


def test_un_mot_de_passe_trop_court_est_refuse(app_client: TestClient) -> None:
    r = app_client.post("/api/v1/auth/setup", json={"email": "a@b.fr", "password": "court"})
    assert r.status_code == 422


# ============================================================ session


def test_sans_session_toute_route_d_exploitation_est_fermee(app_client: TestClient) -> None:
    setup(app_client)
    app_client.cookies.clear()
    assert app_client.get("/api/v1/overview").status_code == 401
    assert app_client.put("/api/v1/rtt", json={"enabled": True}).status_code == 401
    # La sante reste ouverte : le healthcheck Docker n'a pas de session.
    assert app_client.get("/health").status_code == 200


def test_un_mauvais_mot_de_passe_et_un_email_inconnu_repondent_pareil(
    app_client: TestClient,
) -> None:
    setup(app_client)
    app_client.cookies.clear()
    a = app_client.post(
        "/api/v1/auth/login", json={"email": "admin@exemple.fr", "password": "x" * 9}
    )
    b = app_client.post("/api/v1/auth/login", json={"email": "inconnu@x.fr", "password": "x" * 9})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_cinq_echecs_bloquent_la_connexion(app_client: TestClient) -> None:
    setup(app_client)
    app_client.cookies.clear()
    for _ in range(5):
        assert login(app_client, "admin@exemple.fr", "mauvais-mdp") == 401
    # Meme le BON mot de passe attend la fin du blocage.
    assert login(app_client, "admin@exemple.fr") == 429


def test_la_deconnexion_ferme_la_session(app_client: TestClient) -> None:
    setup(app_client)
    assert app_client.post("/api/v1/auth/logout").status_code == 204
    assert app_client.get("/api/v1/overview").status_code == 401


def test_une_ecriture_venue_d_un_autre_site_est_refusee(app_client: TestClient) -> None:
    setup(app_client)
    r = app_client.put(
        "/api/v1/rtt", json={"enabled": True}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403


# ============================================================ grades


def lecteur(client: TestClient) -> None:
    """Cree un compte 'read' depuis le compte d'edition, puis s'y connecte."""
    setup(client)
    r = client.post(
        "/api/v1/users", json={"email": "lecteur@exemple.fr", "password": MDP, "role": "read"}
    )
    assert r.status_code == 201, r.text
    client.cookies.clear()
    assert login(client, "lecteur@exemple.fr") == 200


def test_la_lecture_seule_lit_tout(app_client: TestClient) -> None:
    lecteur(app_client)
    assert app_client.get("/api/v1/overview").status_code == 200
    assert app_client.get("/api/v1/rtt").status_code == 200


def test_la_lecture_seule_ne_peut_rien_modifier(app_client: TestClient) -> None:
    lecteur(app_client)
    r = app_client.put("/api/v1/rtt", json={"enabled": True})
    assert r.status_code == 403
    assert "Read-only" in r.json()["detail"]
    assert app_client.post("/api/v1/netflow/flush").status_code == 403
    assert app_client.delete("/api/v1/static-clients/x").status_code == 403


def test_la_lecture_seule_ne_voit_pas_les_comptes(app_client: TestClient) -> None:
    lecteur(app_client)
    assert app_client.get("/api/v1/users").status_code == 403
    r = app_client.post("/api/v1/users", json={"email": "x@y.fr", "password": MDP, "role": "edit"})
    assert r.status_code == 403


def test_la_lecture_seule_change_son_propre_mot_de_passe(app_client: TestClient) -> None:
    lecteur(app_client)
    r = app_client.post("/api/v1/auth/password", json={"current": MDP, "new": "nouveau-mdp-2"})
    assert r.status_code == 200
    app_client.cookies.clear()
    assert login(app_client, "lecteur@exemple.fr", "nouveau-mdp-2") == 200


def test_l_edition_cree_des_comptes_avec_email_mot_de_passe_et_grade(
    app_client: TestClient,
) -> None:
    setup(app_client)
    r = app_client.post(
        "/api/v1/users", json={"email": "tech@exemple.fr", "password": MDP, "role": "edit"}
    )
    assert r.status_code == 201
    liste = app_client.get("/api/v1/users").json()
    assert [(u["email"], u["role"]) for u in liste] == [
        ("admin@exemple.fr", "edit"),
        ("tech@exemple.fr", "edit"),
    ]
    assert all("password_hash" not in u for u in liste)
    doublon = app_client.post(
        "/api/v1/users", json={"email": "TECH@exemple.fr", "password": MDP, "role": "read"}
    )
    assert doublon.status_code == 409


def test_le_dernier_compte_d_edition_ne_peut_pas_disparaitre(app_client: TestClient) -> None:
    setup(app_client)
    assert app_client.delete("/api/v1/users/1").status_code == 409
    assert app_client.patch("/api/v1/users/1", json={"role": "read"}).status_code == 409
    assert app_client.patch("/api/v1/users/1", json={"disabled": True}).status_code == 409


def test_retirer_l_edition_ferme_les_sessions_du_compte(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    setup(app_client)
    app_client.post("/api/v1/users", json={"email": "b@x.fr", "password": MDP, "role": "edit"})
    users.sessions[("session-de-b")] = {
        "user_id": 2,
        "expires_at": list(users.sessions.values())[0]["expires_at"],
    }
    r = app_client.patch("/api/v1/users/2", json={"role": "read"})
    assert r.json()["role"] == "read"
    assert "session-de-b" not in users.sessions


def test_un_compte_desactive_ne_se_connecte_plus(app_client: TestClient) -> None:
    setup(app_client)
    app_client.post("/api/v1/users", json={"email": "b@x.fr", "password": MDP, "role": "read"})
    assert app_client.patch("/api/v1/users/2", json={"disabled": True}).status_code == 200
    app_client.cookies.clear()
    assert login(app_client, "b@x.fr") == 401


def test_l_empreinte_resiste_a_une_comparaison_naive() -> None:
    a, b = hash_password(MDP), hash_password(MDP)
    assert a != b  # sel different a chaque fois
    assert verify_password(MDP, a) and not verify_password(MDP + "x", a)
    assert not verify_password(MDP, "n'importe quoi")


def test_la_session_n_est_pas_relue_en_base_a_chaque_requete(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    """Une page envoie une dizaine de requetes : une ecriture de session par
    requete, toutes sur la meme ligne, ralentissait chaque page."""
    setup(app_client)
    appels = {"n": 0}
    original = users.session_user

    async def compte(*args, **kwargs):  # type: ignore[no-untyped-def]
        appels["n"] += 1
        return await original(*args, **kwargs)

    users.session_user = compte  # type: ignore[method-assign]
    for _ in range(5):
        assert app_client.get("/api/v1/rtt").status_code == 200
    assert appels["n"] <= 1


def test_un_grade_retire_s_applique_malgre_le_cache(app_client: TestClient) -> None:
    setup(app_client)
    app_client.post("/api/v1/users", json={"email": "b@x.fr", "password": MDP, "role": "edit"})
    autre = TestClient(app_client.app, base_url="https://testserver")
    assert login(autre, "b@x.fr") == 200
    assert autre.put("/api/v1/rtt", json={"enabled": False}).status_code == 200
    app_client.patch("/api/v1/users/2", json={"role": "read"})
    # Sessions fermees et cache vide : la requete suivante est refusee.
    assert autre.put("/api/v1/rtt", json={"enabled": False}).status_code in (401, 403)

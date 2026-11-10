# ruff: noqa: F811 - les fixtures importees portent le nom des parametres
"""Niveaux d'acces : ce que le SERVEUR laisse faire a chacun.

Viewer < User < Supervisor < Technician < Administrator. Chaque niveau a les
droits des precedents. Le serveur fait foi : masquer un bouton n'interdit rien.

- User : les gestes du quotidien sur UN client (mode, boost) ;
- Supervisor : les memes sur plusieurs clients d'un coup ;
- Technician : tout le technique (routeurs, forfaits, modes, reglages, cles) ;
- Administrator : en plus, les comptes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.accounts import niveau_requis
from app.db.modes_repo import InvalidModeError, check_mode, mode_rates
from app.db.users_repo import InMemoryUsersRepository
from app.services.accounts import ROLES, check_role, hash_password, role_at_least
from tests.test_comptes import MDP, app_client, login, setup, users  # noqa: F401 - fixtures


def test_les_cinq_niveaux_sont_ordonnes() -> None:
    assert ROLES == ("read", "user", "supervisor", "technician", "edit")
    assert role_at_least("supervisor", "user")
    assert not role_at_least("user", "supervisor")
    assert role_at_least("edit", "technician")
    assert not role_at_least("inconnu", "read")
    assert check_role("Technician") == "technician"


@pytest.mark.parametrize(
    ("methode", "chemin", "niveau"),
    [
        ("POST", "/api/v1/shaping/boosts", "user"),
        ("DELETE", "/api/v1/shaping/boosts/dupont", "user"),
        ("PUT", "/api/v1/clients/dupont/mode", "user"),
        ("POST", "/api/v1/clients/actions", "user"),
        ("POST", "/api/v1/bandwidth-modes", "technician"),
        ("PATCH", "/api/v1/bandwidth-modes/3", "technician"),
        ("POST", "/api/v1/routers", "technician"),
        ("POST", "/api/v1/api-keys", "technician"),
        ("PUT", "/api/v1/shaping/policies", "technician"),
        # Un autre verbe sur un chemin ouvert reste technique.
        ("DELETE", "/api/v1/clients/dupont/mode", "technician"),
    ],
)
def test_le_niveau_requis_par_route(methode: str, chemin: str, niveau: str) -> None:
    assert niveau_requis(methode, chemin, "/api/v1") == niveau


def _compte(users: InMemoryUsersRepository, email: str, role: str) -> None:
    import asyncio

    asyncio.run(
        users.create(email=email, password_hash=hash_password(MDP), role=role, created_by="test")
    )


def _en_tant_que(client: TestClient, users: InMemoryUsersRepository, role: str) -> None:
    setup(client)
    email = f"{role}@exemple.fr"
    _compte(users, email, role)
    client.cookies.clear()
    assert login(client, email) == 200


ACTION_SEULE = {"logins": ["dupont"], "action": "stop_boost"}
ACTION_GROUPE = {"logins": ["dupont", "martin"], "action": "stop_boost"}


def test_un_viewer_ne_change_aucun_client(app_client: TestClient, users) -> None:  # noqa: F811
    _en_tant_que(app_client, users, "read")
    r = app_client.put("/api/v1/clients/dupont/mode", json={"mode_id": None})
    assert r.status_code == 403
    assert "Viewer" in r.json()["detail"]
    assert app_client.post("/api/v1/clients/actions", json=ACTION_SEULE).status_code == 403


def test_un_user_opere_un_client_mais_pas_un_groupe(app_client: TestClient, users) -> None:  # noqa: F811
    _en_tant_que(app_client, users, "user")
    # Un client : la garde laisse passer (la suite depend des donnees).
    assert app_client.put("/api/v1/clients/dupont/mode", json={"mode_id": None}).status_code != 403
    assert app_client.post("/api/v1/clients/actions", json=ACTION_SEULE).status_code != 403
    # Plusieurs clients : niveau Supervisor.
    r = app_client.post("/api/v1/clients/actions", json=ACTION_GROUPE)
    assert r.status_code == 403
    assert "Supervisor" in r.json()["detail"]
    # Rien de technique.
    r = app_client.post(
        "/api/v1/bandwidth-modes", json={"name": "Backup", "down_value": 6, "up_value": 6}
    )
    assert r.status_code == 403
    assert "Technician" in r.json()["detail"]


def test_un_supervisor_opere_un_groupe(app_client: TestClient, users) -> None:  # noqa: F811
    _en_tant_que(app_client, users, "supervisor")
    assert app_client.post("/api/v1/clients/actions", json=ACTION_GROUPE).status_code != 403
    r = app_client.post(
        "/api/v1/bandwidth-modes", json={"name": "Backup", "down_value": 6, "up_value": 6}
    )
    assert r.status_code == 403


def test_un_technician_fait_le_technique_mais_pas_les_comptes(
    app_client: TestClient,
    users,  # noqa: F811
) -> None:
    _en_tant_que(app_client, users, "technician")
    r = app_client.post(
        "/api/v1/bandwidth-modes", json={"name": "Backup", "down_value": 6, "up_value": 6}
    )
    assert r.status_code != 403  # 503 ici : pas de base, mais la garde a laisse passer
    assert app_client.post("/api/v1/clients/actions", json=ACTION_GROUPE).status_code != 403
    r = app_client.post(
        "/api/v1/users", json={"email": "x@exemple.fr", "password": MDP, "role": "read"}
    )
    assert r.status_code == 403


def test_l_administrateur_cree_un_compte_de_chaque_niveau(app_client: TestClient) -> None:  # noqa: F811
    setup(app_client)
    for role in ROLES:
        r = app_client.post(
            "/api/v1/users",
            json={"email": f"n-{role}@exemple.fr", "password": MDP, "role": role},
        )
        assert r.status_code in (200, 201), r.text
        assert r.json()["role"] == role
    moi = app_client.get("/api/v1/auth/status").json()["user"]
    assert moi["role_label"] == "Administrator"


# ------------------------------------------------------------------ modes
def test_un_mode_fixe_ou_en_part_du_forfait() -> None:
    fixe = {"name": "Backup", "rate_kind": "fixed", "down_value": 6, "up_value": 6}
    assert mode_rates(fixe, None, None) == (6.0, 6.0)
    nuit = {"name": "Night", "rate_kind": "percent", "down_value": 200, "up_value": 50}
    assert mode_rates(nuit, 20, 10) == (40.0, 5.0)
    with pytest.raises(InvalidModeError, match="no known plan"):
        mode_rates(nuit, None, None)


@pytest.mark.parametrize(
    "saisie",
    [
        {"name": "", "down_value": 1, "up_value": 1},
        {"name": "Automatic", "down_value": 1, "up_value": 1},
        {"name": "X", "down_value": 0, "up_value": 1},
        {"name": "X", "rate_kind": "percent", "down_value": 5000, "up_value": 1},
        {"name": "X", "rate_kind": "bizarre", "down_value": 1, "up_value": 1},
    ],
)
def test_un_mode_invalide_est_refuse(saisie: dict) -> None:
    with pytest.raises(InvalidModeError):
        check_mode(saisie)

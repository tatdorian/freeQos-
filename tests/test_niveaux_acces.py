# ruff: noqa: F811 - les fixtures importees portent le nom des parametres
"""Niveaux d'acces : ce que le SERVEUR laisse faire a chacun.

Viewer < Operator < Administrator. Chaque niveau a les droits du precedent. Le
serveur fait foi : masquer un bouton n'interdit rien.

- Viewer : lecture seule ;
- Operator : le mode, le boost et le forfait d'un ou de plusieurs clients ;
- Administrator : tout le reste (clients, PoPs, routeurs, antennes et leur
  API, modes, reglages, cles, comptes).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.accounts import niveau_requis
from app.db.modes_repo import InvalidModeError, check_mode, mode_rates
from app.db.users_repo import InMemoryUsersRepository
from app.services.accounts import ROLES, check_role, hash_password, role_at_least
from tests.test_comptes import MDP, app_client, login, setup, users  # noqa: F401 - fixtures


def test_les_trois_niveaux_sont_ordonnes() -> None:
    assert ROLES == ("read", "operator", "edit")
    assert role_at_least("edit", "operator")
    assert not role_at_least("operator", "edit")
    assert not role_at_least("read", "operator")
    assert not role_at_least("inconnu", "read")
    assert check_role("Operator") == "operator"


@pytest.mark.parametrize(
    ("methode", "chemin", "niveau"),
    [
        ("POST", "/api/v1/shaping/boosts", "operator"),
        ("DELETE", "/api/v1/shaping/boosts/dupont", "operator"),
        ("PUT", "/api/v1/clients/dupont/mode", "operator"),
        ("POST", "/api/v1/clients/actions", "operator"),
        ("PUT", "/api/v1/plans/dupont", "operator"),
        ("DELETE", "/api/v1/plans/dupont", "operator"),
        ("POST", "/api/v1/plans/refresh", "edit"),
        ("POST", "/api/v1/bandwidth-modes", "edit"),
        ("PATCH", "/api/v1/bandwidth-modes/3", "edit"),
        ("POST", "/api/v1/pops/routers", "edit"),
        ("POST", "/api/v1/static-clients", "edit"),
        ("POST", "/api/v1/api-keys", "edit"),
        ("PUT", "/api/v1/shaping/policies", "edit"),
        # Un autre verbe sur un chemin ouvert reste reserve a l'Administrator.
        ("DELETE", "/api/v1/clients/dupont/mode", "edit"),
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


def test_un_operator_opere_un_ou_plusieurs_clients(app_client: TestClient, users) -> None:
    _en_tant_que(app_client, users, "operator")
    # La garde laisse passer (la suite depend des donnees).
    assert app_client.put("/api/v1/clients/dupont/mode", json={"mode_id": None}).status_code != 403
    assert app_client.post("/api/v1/clients/actions", json=ACTION_SEULE).status_code != 403
    assert app_client.post("/api/v1/clients/actions", json=ACTION_GROUPE).status_code != 403
    r = app_client.put("/api/v1/plans/dupont", json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code != 403
    # Rien d'autre : ni les modes eux-memes, ni ajouter un client ou un routeur.
    r = app_client.post(
        "/api/v1/bandwidth-modes", json={"name": "Backup", "down_value": 6, "up_value": 6}
    )
    assert r.status_code == 403
    assert "Administrator" in r.json()["detail"]
    r = app_client.post(
        "/api/v1/static-clients",
        json={"reference": "x", "pop_name": "PoP", "address": "10.0.0.5"},
    )
    assert r.status_code == 403
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

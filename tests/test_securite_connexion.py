"""Securite de la connexion : ce qu'un attaquant en ligne peut -- et ne peut plus -- faire.

CE QUE CES TESTS FIXENT
-----------------------
1. UN MOT DE PASSE QU'UNE LISTE DEVINE EST REFUSE : trop court, trop connu,
   trop repetitif, ou reprenant l'email.
2. LA DEVINETTE EN LIGNE EST LENTE : cinq echecs bloquent, et chaque recidive
   double le blocage. Le changement de mot de passe a le meme frein.
3. TOUT EST TRACE : connexions reussies, refusees, bloquees, comptes crees ou
   modifies -- et chacun voit, a la connexion, la precedente.
4. CHACUN VOIT ET FERME SES SESSIONS ; une session a une duree de vie maximale,
   quelle que soit l'activite.
5. LE NAVIGATEUR EST MIS A CONTRIBUTION : politique de contenu stricte, pas
   d'affichage dans le cadre d'un autre site, pas de cache des reponses de l'API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.db.users_repo import MAX_SESSIONS_PER_USER, InMemoryUsersRepository
from app.main import register_routes
from app.services.accounts import InvalidAccountError, LoginThrottle, check_password
from app.web.headers import CSP, apply_security_headers
from tests.test_api import build_container

MDP = "correct-horse-1"
ADMIN = "admin@exemple.fr"


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

    @app.middleware("http")
    async def entetes(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        apply_security_headers(request, response)
        return response

    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app, base_url="https://testserver")


def setup(client: TestClient, email: str = ADMIN) -> None:
    r = client.post("/api/v1/auth/setup", json={"email": email, "password": MDP})
    assert r.status_code == 200, r.text


def login(client: TestClient, email: str = ADMIN, password: str = MDP):  # type: ignore[no-untyped-def]
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


# ============================================================ mots de passe


@pytest.mark.parametrize(
    ("mdp", "raison"),
    [
        ("court-1", "too short"),
        ("Password2024!", "too common"),
        ("azertyuiop123", "too common"),
        ("123456789012", "too common"),
        ("aaaaaabbbbbb", "too repetitive"),
        ("admin-exemple-2026", None),
        ("jean.dupont-2026", "email"),
    ],
)
def test_un_mot_de_passe_devinable_est_refuse(mdp: str, raison: str | None) -> None:
    if raison is None:
        assert check_password(mdp, email="jean.dupont@exemple.fr") == mdp
        return
    with pytest.raises(InvalidAccountError, match=raison):
        check_password(mdp, email="jean.dupont@exemple.fr")


def test_une_phrase_de_passe_longue_passe_sans_regle_de_composition() -> None:
    # Ni majuscule ni symbole imposes : la longueur fait la force.
    assert check_password("le chat dort sur le routeur")
    assert check_password(MDP)


def test_le_premier_compte_respecte_la_politique(app_client: TestClient) -> None:
    r = app_client.post(
        "/api/v1/auth/setup", json={"email": "a@b.fr", "password": "motdepasse2026"}
    )
    assert r.status_code == 422
    assert "common" in r.json()["detail"]


def test_l_interface_connait_la_longueur_minimale(app_client: TestClient) -> None:
    assert app_client.get("/api/v1/auth/status").json()["password_min"] == 12


# ============================================================ frein


def test_chaque_recidive_double_le_blocage() -> None:
    horloge = {"t": 0.0}
    frein = LoginThrottle(clock=lambda: horloge["t"])
    durees = []
    for _ in range(5):
        for _ in range(5):
            frein.failure("email:x")
        durees.append(frein.locked_for("email:x"))
        horloge["t"] += durees[-1] + 1
    assert durees == [300.0, 600.0, 1200.0, 2400.0, 3600.0]  # plafonne a une heure
    frein.success("email:x")
    for _ in range(5):
        frein.failure("email:x")
    assert frein.locked_for("email:x") == 300.0  # une reussite efface l'ardoise


def test_le_frein_dit_combien_d_essais_restent() -> None:
    frein = LoginThrottle()
    assert [frein.failure("a", "b") for _ in range(5)] == [4, 3, 2, 1, 0]


def test_des_emails_inventes_a_la_chaine_ne_font_pas_gonfler_la_memoire() -> None:
    frein = LoginThrottle(max_keys=100)
    for i in range(1000):
        frein.failure(f"email:robot{i}@x.fr")
    assert len(frein._echecs) <= 100


def test_l_utilisateur_est_prevenu_avant_le_blocage(app_client: TestClient) -> None:
    setup(app_client)
    app_client.cookies.clear()
    messages = [login(app_client, password="mauvais-mdp-1").json()["detail"] for _ in range(4)]
    assert "left" not in messages[0] and "left" not in messages[1]
    assert "2 attempts left" in messages[2]
    assert "1 attempt left" in messages[3]
    bloque = login(app_client, password="mauvais-mdp-1")
    assert bloque.status_code == 429 and "5 min" in bloque.json()["detail"]


def test_le_mot_de_passe_actuel_ne_se_devine_pas_avec_une_session_volee(
    app_client: TestClient,
) -> None:
    setup(app_client)
    for _ in range(5):
        r = app_client.post(
            "/api/v1/auth/password", json={"current": "devine-123456", "new": "nouveau-mdp-2"}
        )
    assert r.status_code == 429
    r = app_client.post("/api/v1/auth/password", json={"current": MDP, "new": "nouveau-mdp-2"})
    assert r.status_code == 429  # meme le bon attend


def test_le_nouveau_mot_de_passe_doit_changer(app_client: TestClient) -> None:
    setup(app_client)
    r = app_client.post("/api/v1/auth/password", json={"current": MDP, "new": MDP})
    assert r.status_code == 422 and "differ" in r.json()["detail"]


# ============================================================ journal


def test_connexions_et_echecs_sont_traces(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    setup(app_client)
    app_client.cookies.clear()
    login(app_client, password="mauvais-mdp-1")
    login(app_client, "inconnu@x.fr", "mauvais-mdp-1")
    assert login(app_client).status_code == 200
    evenements = [(e["event"], e["email"]) for e in users.journal]
    assert evenements == [
        ("setup", ADMIN),
        ("login_ok", ADMIN),
        ("login_failed", ADMIN),
        ("login_failed", "inconnu@x.fr"),
        ("login_ok", ADMIN),
    ]
    assert all(e["address"] == "testclient" for e in users.journal)


def test_la_connexion_montre_la_precedente(app_client: TestClient) -> None:
    setup(app_client)
    app_client.cookies.clear()
    precedente = login(app_client).json()["previous_login"]
    assert precedente is not None and precedente["address"] == "testclient"


def test_le_journal_est_reserve_a_l_edition(app_client: TestClient) -> None:
    setup(app_client)
    app_client.post(
        "/api/v1/users", json={"email": "lecteur@exemple.fr", "password": MDP, "role": "read"}
    )
    journal = app_client.get("/api/v1/auth/events").json()
    assert journal[0]["event"] == "user_created"
    assert journal[0]["actor"] == ADMIN and journal[0]["detail"] == "role read"
    app_client.cookies.clear()
    login(app_client, "lecteur@exemple.fr")
    assert app_client.get("/api/v1/auth/events").status_code == 403


def test_les_changements_de_compte_sont_traces(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    setup(app_client)
    app_client.post("/api/v1/users", json={"email": "b@x.fr", "password": MDP, "role": "edit"})
    app_client.patch("/api/v1/users/2", json={"role": "read", "password": "autre-mdp-2026"})
    app_client.delete("/api/v1/users/2")
    suite = [(e["event"], e["detail"]) for e in users.journal if e["email"] == "b@x.fr"]
    assert suite == [
        ("user_created", "role edit"),
        ("user_updated", "role read, password reset"),
        ("user_deleted", None),
    ]


# ============================================================ sessions


def test_chacun_voit_et_ferme_ses_sessions(app_client: TestClient) -> None:
    setup(app_client)
    autre = TestClient(app_client.app, base_url="https://testserver")
    assert login(autre).status_code == 200
    sessions = app_client.get("/api/v1/auth/sessions").json()
    assert len(sessions) == 2
    assert sum(s["current"] for s in sessions) == 1
    assert all(len(s["id"]) == 16 and "token" not in str(s) for s in sessions)
    r = app_client.post("/api/v1/auth/sessions/close-others")
    assert r.json() == {"closed": 1}
    assert autre.get("/api/v1/rtt").status_code == 401
    assert app_client.get("/api/v1/rtt").status_code == 200


def test_fermer_une_session_precise(app_client: TestClient) -> None:
    setup(app_client)
    autre = TestClient(app_client.app, base_url="https://testserver")
    login(autre)
    cible = next(s for s in app_client.get("/api/v1/auth/sessions").json() if not s["current"])
    assert app_client.delete(f"/api/v1/auth/sessions/{cible['id']}").status_code == 204
    assert autre.get("/api/v1/rtt").status_code == 401
    assert app_client.delete(f"/api/v1/auth/sessions/{cible['id']}").status_code == 404


def test_une_session_a_une_duree_de_vie_maximale(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    setup(app_client)
    session = next(iter(users.sessions.values()))
    session["created_at"] = datetime.now(tz=UTC) - timedelta(days=31)
    from app.api.accounts import forget_cached_sessions

    forget_cached_sessions()
    assert app_client.get("/api/v1/rtt").status_code == 401


def test_les_sessions_d_un_compte_sont_plafonnees(
    app_client: TestClient, users: InMemoryUsersRepository
) -> None:
    setup(app_client)
    for _ in range(MAX_SESSIONS_PER_USER + 3):
        TestClient(app_client.app, base_url="https://testserver").post(
            "/api/v1/auth/login", json={"email": ADMIN, "password": MDP}
        )
    assert len(users.sessions) == MAX_SESSIONS_PER_USER


# ============================================================ navigateur


def test_une_ecriture_d_un_autre_site_est_refusee_meme_sans_origin(
    app_client: TestClient,
) -> None:
    setup(app_client)
    r = app_client.put(
        "/api/v1/rtt", json={"enabled": True}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert r.status_code == 403
    r = app_client.put(
        "/api/v1/rtt", json={"enabled": True}, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert r.status_code == 200


def test_une_connexion_declenchee_par_un_autre_site_est_refusee(
    app_client: TestClient,
) -> None:
    setup(app_client)
    app_client.cookies.clear()
    r = app_client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN, "password": MDP},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_les_en_tetes_de_securite_sont_poses(app_client: TestClient) -> None:
    r = app_client.get("/api/v1/auth/status")
    assert r.headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "frame-ancestors 'none'" in CSP
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["strict-transport-security"].startswith("max-age=")
    clair = TestClient(app_client.app, base_url="http://testserver").get("/api/v1/auth/status")
    assert "strict-transport-security" not in clair.headers


def test_la_page_n_a_aucun_script_en_ligne(app_client: TestClient) -> None:
    """La politique n'autorise que les scripts servis : un script en ligne
    serait bloque par le navigateur, et la page avec."""
    page = app_client.get("/").text
    assert "<script>" not in page and "<script " in page
    assert "onclick=" not in page.lower()

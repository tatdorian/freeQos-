"""Page Alerts : chaque compte cree SES alertes, recues A SON adresse.

CE QUI EST FIXE ICI
-------------------
1. Trois sortes : des problemes du reseau (categories, eventuellement des
   sites), des clients choisis hors ligne, un CPU de routeur au-dessus d'un
   seuil.
2. Un probleme ne part que s'il dure depuis le delai de la regle ; sa
   resolution est annoncee (si demande). Un email par regle, a son proprietaire.
3. Un compte ne voit et ne modifie que ses alertes ; une cle d'API n'en cree
   pas (elle n'a pas d'adresse).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.alert_rules import InvalidAlertRuleError, check, matches, run_rules
from tests.test_alertes_email import FauxSmtp, faux_smtp  # noqa: F401 - fixture
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
MDP = "Tamarind-Hub-2026!"


# ------------------------------------------------------------------ unitaire
def test_une_regle_se_verifie() -> None:
    with pytest.raises(InvalidAlertRuleError):
        check({"name": "x", "kind": "events", "categories": []})
    with pytest.raises(InvalidAlertRuleError):
        check({"name": "x", "kind": "client_offline", "targets": []})
    with pytest.raises(InvalidAlertRuleError):
        check({"name": "x", "kind": "router_cpu", "threshold": 5})
    r = check({"name": "x", "kind": "events", "categories": ["site", "nope"], "targets": ["a"]})
    assert r["categories"] == ["site"] and r["targets"] == []


def test_les_problemes_d_une_categorie_et_d_un_site() -> None:
    problemes = [
        {
            "key": "site:Plateau",
            "category": "site",
            "site": "Plateau",
            "title": "Site Plateau down",
        },
        {
            "key": "site:Bassora",
            "category": "site",
            "site": "Bassora",
            "title": "Site Bassora down",
        },
        {"key": "job:x", "category": "measurement", "title": "Collection failing"},
    ]
    regle = {"kind": "events", "categories": ["site"], "sites": ["plateau"]}
    vus = matches(regle, problems=problemes, clients=[], cpu={}, now=NOW)
    assert [p["key"] for p in vus] == ["site:Plateau"]


def test_des_clients_choisis_hors_ligne() -> None:
    clients = [
        {"login": "a", "ts": NOW - timedelta(minutes=10), "pop_name": "Plateau"},
        {"login": "b", "ts": NOW - timedelta(seconds=20)},
        {"login": "c", "ts": NOW - timedelta(minutes=10)},
    ]
    regle = {"kind": "client_offline", "targets": ["a", "b"]}
    assert [p["key"] for p in matches(regle, problems=[], clients=clients, cpu={}, now=NOW)] == [
        "offline:a"
    ]


def test_un_cpu_au_dessus_du_seuil() -> None:
    cpu = {"nas1": {"max_core": 91, "avg": 40}, "nas2": {"max_core": 50, "avg": 20}}
    regle = {"kind": "router_cpu", "threshold": 85, "targets": []}
    [p] = matches(regle, problems=[], clients=[], cpu=cpu, now=NOW)
    assert p["key"] == "cpu:nas1" and "91 %" in p["title"]


class _Depot:
    """Le depot des regles, en memoire."""

    def __init__(self, regles: list[dict[str, Any]]) -> None:
        self.regles = regles
        self.etat: dict[int, dict[str, dict[str, Any]]] = {}
        self.marques: list[tuple[int, bool, str | None]] = []

    async def list(self, owner: str | None = None) -> list[dict[str, Any]]:
        return self.regles

    async def events(self, ident: int) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.etat.get(ident, {}).items()}

    async def upsert_event(self, ident: int, p: dict[str, Any], now: datetime) -> None:
        e = self.etat.setdefault(ident, {})
        avant = e.get(p["key"], {"first_seen": now, "notified_at": None})
        e[p["key"]] = {**avant, **p}

    async def forget(self, ident: int, keys: list[str]) -> None:
        for k in keys:
            self.etat.get(ident, {}).pop(k, None)

    async def mark(self, ident: int, *, ok: bool, error: str | None = None) -> None:
        self.marques.append((ident, ok, error))

    async def mark_notified(self, ident: int, keys: list[str], now: datetime) -> None:
        for k in keys:
            self.etat[ident][k]["notified_at"] = now


class _Notifs:
    async def config(self) -> dict[str, Any]:
        return {
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "from_addr": "qos@example.com",
            "enabled": True,
        }

    async def password(self) -> str | None:
        return None


class _Conteneur:
    def __init__(self, depot: _Depot) -> None:
        self.alert_rules_repo = depot
        self.notifications_repo = _Notifs()
        self.router_cpu = None
        self.public_access_repo = None


async def test_le_delai_puis_l_email_puis_la_resolution(faux_smtp) -> None:  # noqa: F811
    regle = {
        "id": 1,
        "owner_email": "noc@example.com",
        "name": "Sites",
        "kind": "events",
        "categories": ["site"],
        "sites": [],
        "delay_minutes": 5,
        "notify_resolved": True,
        "enabled": True,
    }
    depot = _Depot([regle])
    conteneur = _Conteneur(depot)
    probleme = [
        {
            "key": "site:Plateau",
            "category": "site",
            "level": "crit",
            "title": "Site Plateau down",
            "site": "Plateau",
        }
    ]
    # Vu, mais pas encore depuis 5 minutes : rien n'est envoye.
    await run_rules(conteneur, problems=probleme, clients=[], now=NOW)
    assert faux_smtp.envoyes == []
    await run_rules(conteneur, problems=probleme, clients=[], now=NOW + timedelta(minutes=5))
    [email] = faux_smtp.envoyes
    assert email["To"] == "noc@example.com" and "Sites" in email["Subject"]
    # Toujours la : pas de second email.
    await run_rules(conteneur, problems=probleme, clients=[], now=NOW + timedelta(minutes=6))
    assert len(faux_smtp.envoyes) == 1
    # Resolu : annonce une fois.
    await run_rules(conteneur, problems=[], clients=[], now=NOW + timedelta(minutes=7))
    assert len(faux_smtp.envoyes) == 2 and "Resolved" in faux_smtp.envoyes[1]["Subject"]


# ------------------------------------------------------------- bout en bout
def _compte(client, email: str, role: str) -> None:  # type: ignore[no-untyped-def]
    from app.services.accounts import hash_password

    depot = client.app.state.container.users_repo

    async def creer() -> None:
        await depot.create(
            email=email, password_hash=hash_password(MDP), role=role, created_by=None
        )

    client.portal.call(creer)


def _connecter(client, email: str) -> None:  # type: ignore[no-untyped-def]
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": email, "password": MDP})
    assert r.status_code == 200, r.text


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_chacun_ses_alertes_a_son_adresse(app_client, routeur, faux_smtp) -> None:  # noqa: F811
    _compte(app_client, "noc@example.com", "read")
    _compte(app_client, "tech@example.com", "operator")
    conteneur = app_client.app.state.container

    async def smtp() -> None:
        await conteneur.notifications_repo.save(
            {
                "host": "smtp.example.com",
                "port": 587,
                "security": "starttls",
                "from_addr": "qos@example.com",
                "recipients": [],
                "events": [],
                "grace_minutes": 3,
                "enabled": True,
            },
            password=None,
            actor="test",
        )

    app_client.portal.call(smtp)

    # Un Viewer cree SON alerte : elle part a son adresse.
    _connecter(app_client, "noc@example.com")
    r = app_client.post(
        "/api/v1/alert-rules",
        json={"name": "My sites", "kind": "events", "categories": ["site", "router"]},
    )
    assert r.status_code == 201, r.text
    ident = r.json()["id"]
    assert r.json()["owner_email"] == "noc@example.com"
    liste = app_client.get("/api/v1/alert-rules").json()
    assert liste["me"] == "noc@example.com" and liste["smtp"]["ready"] is True
    r = app_client.post(f"/api/v1/alert-rules/{ident}/test")
    assert r.status_code == 200, r.text
    assert faux_smtp.envoyes[-1]["To"] == "noc@example.com"

    # Un autre compte ne la voit pas et ne la touche pas.
    _connecter(app_client, "tech@example.com")
    assert app_client.get("/api/v1/alert-rules").json()["rules"] == []
    r = app_client.patch(f"/api/v1/alert-rules/{ident}", json={"enabled": False})
    assert r.status_code == 403, r.text

    # Une cle d'API n'a pas d'adresse : pas d'alerte personnelle.
    app_client.cookies.clear()
    ecriture, _lecture = _cles(app_client)
    r = app_client.post(
        "/api/v1/alert-rules",
        headers={"Authorization": f"Bearer {ecriture}"},
        json={"name": "x", "kind": "events", "categories": ["site"]},
    )
    assert r.status_code == 400, r.text


def test_la_page_alerts_propose_mes_alertes() -> None:
    from app.web.ui import STATIC_DIR

    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '<h2 style="margin-top:1.6rem">My email alerts</h2>' in html
    assert 'id="ma-form"' in html and 'data-v="router_cpu"' in html
    assert (
        "async function loadMesAlertes()" in js
        and "'/alert-rules/' + b.dataset.maTest + '/test'" in js
    )

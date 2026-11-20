"""Les files des clients : le nom du client, sans prefixe ni commentaire.

CE QUI EST FIXE ICI
-------------------
1. La file d'un client s'appelle comme lui (« ACME-OFFICE ») et ne porte
   aucun commentaire.
2. freeQoS SE SOUVIENT des files qu'il a posees (memoire, en base) : c'est ce
   qui les distingue d'une file posee a la main sous le meme nom, qui n'est
   JAMAIS touchee.
3. Une file d'avant (« freeqos-<client> », marquee) est RENOMMEE, pas recreee.
4. Retirer un client emporte sa file, meme encore sous son ancien nom.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from app.enforcement.models import (
    MANAGED_COMMENT,
    OWNED_QUEUES,
    mark_owned,
    subscriber_queue_name,
)
from app.enforcement.planner import SubscriberTarget, build_plan, desired_state
from app.services.shaping import ShapingService
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _memoire_vide():  # type: ignore[no-untyped-def]
    OWNED_QUEUES.clear()
    yield
    OWNED_QUEUES.clear()


def _abonne(login: str = "ACME-OFFICE", ip: str = "10.20.0.10") -> SubscriberTarget:
    return SubscriberTarget(
        login=login, interface="", plan_down_mbps=20, plan_up_mbps=10, address=ip
    )


def _desire(login: str = "ACME-OFFICE"):  # type: ignore[no-untyped-def]
    return desired_state(links=[], subscribers=[_abonne(login)])[1]


def test_le_nom_du_client_sans_prefixe_ni_commentaire() -> None:
    [file] = _desire()
    assert file.name == "ACME-OFFICE"
    assert subscriber_queue_name("ACME OFFICE") == "ACME-OFFICE"
    champs = file.routeros_fields()
    assert champs["name"] == "ACME-OFFICE"
    assert champs["comment"] == ""
    assert file.legacy_names == ("freeqos-ACME-OFFICE",)


def test_l_ancienne_file_est_renommee_pas_recreee() -> None:
    files = _desire()
    ancienne = {
        ".id": "*7",
        "name": "freeqos-ACME-OFFICE",
        "target": "10.20.0.10/32",
        "max-limit": files[0].max_limit,
        "comment": MANAGED_COMMENT,
        "parent": "none",
        "queue": files[0].queue,
        "disabled": "no",
    }
    plan = build_plan(
        "nas", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[ancienne]
    )
    [action] = plan.actions
    assert (action.verb, action.target_id) == ("set", "*7")
    assert action.fields == {"name": "ACME-OFFICE", "comment": ""}


def test_une_file_du_meme_nom_posee_a_la_main_n_est_jamais_touchee() -> None:
    a_la_main = {
        ".id": "*9",
        "name": "ACME-OFFICE",
        "target": "10.20.0.10/32",
        "max-limit": "1M/1M",
    }
    plan = build_plan(
        "nas",
        desired_types=[],
        desired_queues=_desire(),
        actual_types=[],
        actual_queues=[a_la_main],
        prune=True,
    )
    assert plan.actions == []
    assert "was not created by freeQoS" in plan.conflicts[0].detail


def test_la_memoire_dit_ce_qui_est_a_freeqos() -> None:
    OWNED_QUEUES["nas"] = {"ANCIEN-CLIENT"}
    lignes = mark_owned(
        "nas",
        [
            {".id": "*1", "name": "ANCIEN-CLIENT", "target": "10.20.0.50/32"},
            {".id": "*2", "name": "A-LA-MAIN", "target": "10.20.0.51/32"},
        ],
    )
    plan = build_plan(
        "nas",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=lignes,
        prune=True,
    )
    # Seule la file dont freeQoS se souvient part ; l'autre reste.
    assert [(a.verb, a.name) for a in plan.actions] == [("remove", "ANCIEN-CLIENT")]
    # Un autre routeur n'a pas cette memoire.
    autre = mark_owned("autre", [{".id": "*1", "name": "ANCIEN-CLIENT"}])
    assert "_freeqos" not in autre[0]


async def test_la_memoire_suit_les_ecritures() -> None:
    def issue(verb: str, name: str, fields: dict[str, Any], changes: Any = None) -> Any:
        action = SimpleNamespace(
            path="/queue/simple", verb=verb, name=name, fields=fields, changes=changes or {}
        )
        return SimpleNamespace(action=action, ok=True, detail="")

    service = SimpleNamespace(owned_pool=None)
    resultat = SimpleNamespace(
        router_name="nas",
        outcomes=[
            issue("add", "A", {"name": "A", "comment": ""}),
            issue("set", "B", {"name": "B", "comment": ""}, {"name": ("freeqos-B", "B")}),
            issue("add", "freeqos-link-x", {"name": "freeqos-link-x", "comment": MANAGED_COMMENT}),
        ],
    )
    await ShapingService._retenir_nos_files(service, resultat)  # type: ignore[arg-type]
    assert OWNED_QUEUES["nas"] == {"A", "B"}
    resultat.outcomes = [issue("remove", "A", {})]
    await ShapingService._retenir_nos_files(service, resultat)  # type: ignore[arg-type]
    assert OWNED_QUEUES["nas"] == {"B"}


# ------------------------------------------------------------- de bout en bout
@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_sur_le_routeur_et_en_base(app_client, routeur) -> None:  # noqa: F811
    """Une file d'avant est renommee sur le routeur, et freeQoS s'en souvient
    en base (elle survit a un redemarrage)."""
    from tests.test_type_de_file import _brancher

    routeur.simple_queue_rows.append(
        {
            ".id": "*AA",
            "name": "freeqos-dupont",
            "target": "10.20.0.10/32",
            "max-limit": "1000000/1000000",
            "comment": MANAGED_COMMENT,
        }
    )
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    # Son forfait d'abord : un client sans debit n'a pas de file a garder.
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code == 200, r.text
    _brancher(app_client, cle)
    r = app_client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text
    noms = [q["name"] for q in routeur.simple_queue_rows]
    assert "dupont" in noms and "freeqos-dupont" not in noms
    file = _file(routeur, "10.20.0.10")
    assert file[".id"] == "*AA"  # la meme file, renommee
    assert not file.get("comment")
    assert file["max-limit"] == "10000000/20000000"

    conteneur = app_client.app.state.container

    async def en_base() -> set[str]:
        async with conteneur.database.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name FROM owned_queues WHERE router = 'nas-test'")
        return {str(r["name"]) for r in rows}

    assert app_client.portal is not None
    assert "dupont" in app_client.portal.call(en_base)


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_un_routeur_renomme_garde_la_memoire_de_ses_files(app_client, routeur) -> None:  # noqa: F811
    """La memoire est rangee par nom de routeur : renommer le routeur la suit,
    sinon « Remove freeQoS » laisserait ses files de clients en place."""
    from tests.test_type_de_file import _brancher

    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    conteneur = app_client.app.state.container

    async def poser() -> None:
        async with conteneur.database.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO owned_queues (router, name) VALUES ('nas-test', 'ACME-OFFICE') "
                "ON CONFLICT DO NOTHING"
            )
        OWNED_QUEUES.setdefault("nas-test", set()).add("ACME-OFFICE")

    async def en_base(nom: str) -> set[str]:
        async with conteneur.database.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name FROM owned_queues WHERE router = $1", nom)
        return {str(r["name"]) for r in rows}

    assert app_client.portal is not None
    app_client.portal.call(poser)
    ident = next(
        r["id"]
        for r in app_client.get("/api/v1/pops/routers", headers=cle).json()["routers"]
        if r["name"] == "nas-test"
    )
    r = app_client.patch(f"/api/v1/pops/routers/{ident}", headers=cle, json={"name": "nas-renomme"})
    assert r.status_code == 200, r.text

    assert app_client.portal.call(en_base, "nas-renomme") == {"ACME-OFFICE"}
    assert app_client.portal.call(en_base, "nas-test") == set()
    assert "ACME-OFFICE" in OWNED_QUEUES["nas-renomme"]
    assert "nas-test" not in OWNED_QUEUES

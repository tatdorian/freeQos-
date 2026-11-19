"""Type des files freeQoS : CAKE ou FQ-CoDel, au choix dans les Reglages.

CE QUI EST FIXE ICI
-------------------
1. FQ-CoDel : deux types ``freeqos-fq-up`` / ``freeqos-fq-down`` (kind
   fq-codel, defauts RouterOS), et TOUTES nos files les portent.
2. Les debits ne changent pas : seul le champ ``queue`` des files bouge.
3. Basculer cree les nouveaux types, repointe nos files, puis retire les
   anciens EN DERNIER -- jamais un type encore porte par une file.
4. Un plan reduit a un client ne retire jamais de type.
5. SI FQ-CODEL NE FONCTIONNE PAS sur un routeur, il reste en CAKE.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from app.enforcement.models import managed_comment
from app.enforcement.planner import (
    QUEUE_TYPE_DOWN,
    QUEUE_TYPE_FQ_DOWN,
    QUEUE_TYPE_FQ_UP,
    QUEUE_TYPE_UP,
    SubscriberTarget,
    build_plan,
    desired_queue_types,
    desired_state,
)
from app.services.shaping import ShapingService
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    _file,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


def _abonne(login: str, ip: str) -> SubscriberTarget:
    return SubscriberTarget(
        login=login, interface="", plan_down_mbps=20, plan_up_mbps=10, address=ip
    )


def test_fq_codel_deux_types_et_nos_files_les_portent() -> None:
    types = desired_queue_types(kind="fq-codel", overhead=22, rtt_ms=50)
    assert [t.routeros_fields() for t in types] == [
        {"name": QUEUE_TYPE_FQ_UP, "kind": "fq-codel"},
        {"name": QUEUE_TYPE_FQ_DOWN, "kind": "fq-codel"},
    ]
    _, files, _ = desired_state(
        links=[], subscribers=[_abonne("dupont", "10.20.0.10")], queue_types=types
    )
    [file] = files
    assert file.routeros_fields()["queue"] == f"{QUEUE_TYPE_FQ_UP}/{QUEUE_TYPE_FQ_DOWN}"
    # Le debit est le meme qu'en CAKE.
    _, files_cake, _ = desired_state(
        links=[], subscribers=[_abonne("dupont", "10.20.0.10")], queue_types=desired_queue_types()
    )
    assert file.max_limit == files_cake[0].max_limit


def _types_cake() -> list[dict[str, Any]]:
    return [
        {".id": "*T1", "name": QUEUE_TYPE_UP, "kind": "cake"},
        {".id": "*T2", "name": QUEUE_TYPE_DOWN, "kind": "cake"},
    ]


def _file_routeur(nom: str, ip: str) -> dict[str, Any]:
    return {
        ".id": f"*{nom}",
        "name": nom,
        "target": f"{ip}/32",
        "max-limit": "10000000/20000000",
        "queue": f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}",
        "comment": managed_comment(),
        "parent": "none",
    }


def test_basculer_repointe_puis_retire_les_anciens_types_en_dernier() -> None:
    types = desired_queue_types(kind="fq-codel")
    _, files, _ = desired_state(
        links=[], subscribers=[_abonne("dupont", "10.20.0.10")], queue_types=types
    )
    plan = build_plan(
        "nas",
        desired_types=types,
        desired_queues=files,
        actual_types=_types_cake(),
        actual_queues=[_file_routeur("freeqos-dupont", "10.20.0.10")],
        prune=False,
    )
    resume = [(a.verb, a.path, a.name) for a in plan.actions]
    assert resume[:2] == [
        ("add", "/queue/type", QUEUE_TYPE_FQ_UP),
        ("add", "/queue/type", QUEUE_TYPE_FQ_DOWN),
    ]
    assert ("set", "/queue/simple", "freeqos-dupont") in resume
    assert resume[-2:] == [
        ("remove", "/queue/type", QUEUE_TYPE_UP),
        ("remove", "/queue/type", QUEUE_TYPE_DOWN),
    ]
    file = next(a for a in plan.actions if a.path == "/queue/simple")
    assert set(file.fields) == {"queue"}  # rien d'autre ne bouge


def test_un_type_encore_porte_par_une_file_reste() -> None:
    """Une file d'abonne hors ligne, gardee sans purge, porte encore CAKE."""
    types = desired_queue_types(kind="fq-codel")
    _, files, _ = desired_state(
        links=[], subscribers=[_abonne("dupont", "10.20.0.10")], queue_types=types
    )
    plan = build_plan(
        "nas",
        desired_types=types,
        desired_queues=files,
        actual_types=_types_cake(),
        actual_queues=[
            _file_routeur("freeqos-dupont", "10.20.0.10"),
            _file_routeur("freeqos-absent", "10.20.0.99"),
        ],
        prune=False,
    )
    assert not [a for a in plan.actions if a.verb == "remove" and a.path == "/queue/type"]


def test_un_plan_reduit_a_un_client_ne_retire_aucun_type() -> None:
    types = desired_queue_types(kind="fq-codel")
    _, files, _ = desired_state(
        links=[], subscribers=[_abonne("dupont", "10.20.0.10")], queue_types=types
    )
    plan = build_plan(
        "nas",
        desired_types=types,
        desired_queues=files,
        actual_types=_types_cake(),
        actual_queues=[_file_routeur("freeqos-dupont", "10.20.0.10")],
        prune=False,
    )
    reduit = plan.restrict_to({"freeqos-dupont"})
    assert [a.verb for a in reduit.actions if a.path == "/queue/type"] == ["add", "add"]


def test_un_routeur_qui_refuse_fq_codel_reste_en_cake() -> None:
    service = SimpleNamespace(settings=SimpleNamespace(queue_kind="fq-codel"), fq_codel_refused={})
    assert ShapingService.queue_kind_for(service, "nas") == "fq-codel"  # type: ignore[arg-type]
    action = SimpleNamespace(path="/queue/type", verb="add", fields={"kind": "fq-codel"})
    resultat = SimpleNamespace(
        router_name="nas",
        outcomes=[SimpleNamespace(action=action, ok=False, detail="input does not match")],
    )
    ShapingService._noter_refus_fq_codel(service, resultat)  # type: ignore[arg-type]
    assert service.fq_codel_refused == {"nas": "input does not match"}
    assert ShapingService.queue_kind_for(service, "nas") == "cake"  # type: ignore[arg-type]
    assert ShapingService.queue_kind_for(service, "autre") == "fq-codel"  # type: ignore[arg-type]


# ------------------------------------------------------------- de bout en bout
def _brancher(client, cle) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/api/v1/pops/routers",
        headers=cle,
        json={
            "name": "nas-test",
            "host": "192.0.2.10",
            "username": "qos",
            "password": "secret-routeur",
            "role": "pop",
            "pop_name": "PoP Test",
        },
    )
    assert r.status_code == 201, r.text
    _attendre(
        lambda: bool(
            client.get("/api/v1/pops/provisioning/nas-test", headers=cle).json().get("finished_at")
        ),
        20,
    )
    assert client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200


def _appliquer(client, cle) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/api/v1/shaping/apply",
        headers=cle,
        json={"router": "nas-test", "dry_run": False, "confirm": True},
    )
    assert r.status_code == 200, r.text


def _types(routeur) -> set[str]:  # type: ignore[no-untyped-def]  # noqa: F811
    return {str(t.get("name")) for t in routeur.queue_type_rows}


@pytest.fixture(autouse=True)
def _type_par_defaut():  # type: ignore[no-untyped-def]
    """Le reglage est garde en base : chaque test part de CAKE et y revient."""

    async def effacer() -> None:
        import asyncpg

        if not DSN:
            return
        conn = await asyncpg.connect(DSN)
        try:
            if await conn.fetchval("SELECT to_regclass('runtime_settings')") is not None:
                await conn.execute("DELETE FROM runtime_settings WHERE name = 'queue_kind'")
        finally:
            await conn.close()

    import asyncio

    asyncio.run(effacer())
    yield
    asyncio.run(effacer())


def _queue(routeur) -> str:  # type: ignore[no-untyped-def]  # noqa: F811
    file = _file(routeur, "10.20.0.10")
    return str(file.get("queue")) if file else ""


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_basculer_dans_les_reglages_et_revenir(app_client, routeur) -> None:  # noqa: F811
    """Changer le reglage SUFFIT : les files sont repointees tout de suite."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    r = app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    assert r.status_code == 200, r.text
    assert _queue(routeur) == f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}"
    limite = _file(routeur, "10.20.0.10")["max-limit"]

    r = app_client.put("/api/v1/settings/queue_kind", headers=cle, json={"value": "fq-codel"})
    assert r.status_code == 200, r.text
    _attendre(lambda: not _types(routeur) & {QUEUE_TYPE_UP, QUEUE_TYPE_DOWN}, 15)
    assert _queue(routeur) == f"{QUEUE_TYPE_FQ_UP}/{QUEUE_TYPE_FQ_DOWN}"
    assert _file(routeur, "10.20.0.10")["max-limit"] == limite  # le debit ne bouge pas
    assert _types(routeur) >= {QUEUE_TYPE_FQ_UP, QUEUE_TYPE_FQ_DOWN}

    r = app_client.put("/api/v1/settings/queue_kind", headers=cle, json={"value": "cake"})
    assert r.status_code == 200, r.text
    _attendre(lambda: not _types(routeur) & {QUEUE_TYPE_FQ_UP, QUEUE_TYPE_FQ_DOWN}, 15)
    assert _queue(routeur) == f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}"


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_si_le_routeur_refuse_fq_codel_il_reste_en_cake(app_client, routeur) -> None:  # noqa: F811
    import app.services.shaping as sh

    ecrivain = sh.LibrouterosWriteClient

    class Refuse(ecrivain):  # type: ignore[misc,valid-type]
        def execute(self, action: Any) -> dict[str, Any]:
            if action.path == "/queue/type" and action.fields.get("kind") == "fq-codel":
                raise RuntimeError("input does not match any value of kind")
            return super().execute(action)  # type: ignore[no-any-return]

    # Un routeur dont RouterOS ne connait pas fq-codel.
    shaping = app_client.app.state.container.shaping
    shaping._write_client_factory = lambda *_a, **_k: Refuse()
    shaping._write_clients.clear()
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)
    app_client.put("/api/v1/plans/dupont", headers=cle, json={"down_mbps": 20, "up_mbps": 10})
    app_client.put("/api/v1/settings/queue_kind", headers=cle, json={"value": "fq-codel"})

    def refuse() -> bool:
        reglages = app_client.get("/api/v1/settings", headers=cle).json()
        return "nas-test" in reglages["fq_codel_refused"]

    _attendre(refuse, 15)
    # Il reste en CAKE, sans erreur au passage suivant.
    _appliquer(app_client, cle)
    assert _queue(routeur) == f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}"
    assert _types(routeur) >= {QUEUE_TYPE_UP, QUEUE_TYPE_DOWN}


def test_les_reglages_proposent_cake_ou_fq_codel() -> None:
    from app.web.ui import STATIC_DIR

    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '<button type="button" data-qk="cake">CAKE</button>' in html
    assert '<button type="button" data-qk="fq-codel">FQ-CoDel</button>' in html
    assert "function renderQueueKind(body)" in js
    assert "these routers keep CAKE" in js

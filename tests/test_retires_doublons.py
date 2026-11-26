"""Clients RETIRES qui sont en fait des clients DECLARES : plus de doublon.

1. Meme identifiant, exactement, qu'un client declare : le retrait cachait ce
   client declare ; la declaration gagne, le retrait est leve.
2. Meme IP (dans l'adresse ou une plage d'un client declare, ou l'IP d'un client
   qui a un forfait), meme MAC de CPE, nom equivalent : c'est le meme client.
   Il sort de « Removed » mais reste ignore -- sinon il reviendrait en double.
3. Le client declare disparu, le retire revient dans « Removed ».
4. Aucune correspondance : il reste un retire comme les autres.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.exclusions import ExcludedClients
from app.services.removed_duplicates import resolve
from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _attendre,
    _cles,
    app_client,
    routeur,
)

DSN = os.environ.get("TEST_DATABASE_URL")


class Fiches:
    def __init__(self, fiches: list[dict[str, Any]]) -> None:
        self.fiches = fiches

    async def list_all(self, *, pop_name: str | None = None) -> list[dict[str, Any]]:
        return [dict(f) for f in self.fiches]


class Effectif:
    def __init__(self, lignes: list[dict[str, Any]]) -> None:
        self.lignes = lignes

    async def subscriber_latest(self, **_: Any) -> list[dict[str, Any]]:
        return [dict(x) for x in self.lignes]


def _conteneur(retires: ExcludedClients, fiches: Fiches, sessions: dict[str, Any]) -> Any:
    return SimpleNamespace(
        exclusions=retires,
        static_clients_repo=fiches,
        repository=Effectif(
            [
                {
                    "login": "client-forfait",
                    "kind": "pppoe",
                    "last_ip": "10.9.0.7",
                    "plan_down_mbps": 20.0,
                    "plan_up_mbps": 5.0,
                    "plan_source": "api",
                },
                # Pas de forfait (debit par defaut) : pas une reference.
                {
                    "login": "sans-forfait",
                    "kind": "pppoe",
                    "last_ip": "10.9.0.8",
                    "plan_down_mbps": 0.001,
                    "plan_up_mbps": 0.001,
                    "plan_source": "default",
                },
            ]
        ),
        collection=SimpleNamespace(removed_sessions=sessions),
    )


def test_les_retires_qui_sont_des_clients_declares_sont_ranges() -> None:
    retires = ExcludedClients()
    fiches = Fiches(
        [
            {
                "reference": "ACME-SIEGE",
                "label": "Acme head office",
                "address": "10.20.0.8/29",
                "extra_prefixes": ["172.16.5.0/30"],
                "cpe_mac": "24:5A:4C:12:34:56",
            },
            {"reference": "mairie", "address": "10.30.0.5/32", "extra_prefixes": []},
        ]
    )
    sessions = {
        "pppoe-acme": {"address": "10.20.0.10", "caller_id": None},
        "acme-routeur2": {"address": "172.16.5.2", "caller_id": None},
        "ancienne-box": {"address": "192.0.2.50", "caller_id": "24-5a-4c-12-34-56"},
        "session-forfait": {"address": "10.9.0.7", "caller_id": None},
        "session-sans-forfait": {"address": "10.9.0.8", "caller_id": None},
        "inconnu": {"address": "10.99.0.1", "caller_id": None},
    }
    conteneur = _conteneur(retires, fiches, sessions)

    async def scenario() -> None:
        await retires.add(
            [
                "mairie",  # meme identifiant qu'un client declare
                "pppoe-acme",  # IP dans son adresse /29
                "acme-routeur2",  # IP dans une de ses plages
                "ancienne-box",  # MAC du CPE
                "Acme Head-Office",  # nom equivalent (au libelle)
                "session-forfait",  # IP d'un client qui a un forfait
                "session-sans-forfait",  # IP d'un client SANS forfait : rien
                "inconnu",
                "hors-ligne",  # jamais vu connecte : rien a comparer
            ],
            "admin@example.com",
        )
        bilan = await resolve(conteneur)

        assert bilan["restored"] == ["mairie"]
        assert "mairie" not in retires
        memes = {m["login"]: (m["same_as"], m["reason"]) for m in bilan["same_as"]}
        assert memes == {
            "pppoe-acme": ("ACME-SIEGE", "same IP"),
            "acme-routeur2": ("ACME-SIEGE", "same IP"),
            "ancienne-box": ("ACME-SIEGE", "same MAC"),
            "Acme Head-Office": ("ACME-SIEGE", "same name"),
            "session-forfait": ("client-forfait", "same IP"),
        }
        # Toujours ignores : leves, ils reviendraient en double.
        assert all(login in retires for login in memes)
        restants = [c["login"] for c in await retires.list() if not c["same_as"]]
        assert restants == ["hors-ligne", "inconnu", "session-sans-forfait"]

        # Le client declare disparait : ses doublons reviennent dans « Removed ».
        fiches.fiches = [f for f in fiches.fiches if f["reference"] != "ACME-SIEGE"]
        bilan = await resolve(conteneur)
        assert sorted(bilan["back"]) == [
            "Acme Head-Office",
            "acme-routeur2",
            "ancienne-box",
            "pppoe-acme",
        ]
        restants = [c["login"] for c in await retires.list() if not c["same_as"]]
        assert "pppoe-acme" in restants

    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_de_bout_en_bout_un_retire_sur_l_ip_d_un_client_declare(app_client, routeur) -> None:  # noqa: F811
    """La session retiree, encore connectee, porte l'IP d'un client declare :
    elle quitte « Removed », reste ignoree, et le client declare reste seul."""
    ecriture, _ = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    r = app_client.post(
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
            app_client.get("/api/v1/pops/provisioning/nas-test", headers=cle)
            .json()
            .get("finished_at")
        ),
        20,
    )
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    r = app_client.post(
        "/api/v1/clients/remove", headers=cle, json={"logins": ["dupont"], "confirm": True}
    )
    assert r.status_code == 200, r.text
    r = app_client.post(
        "/api/v1/static-clients",
        headers=cle,
        json={"reference": "ACME-SIEGE", "pop_name": "PoP Test", "address": "10.20.0.10"},
    )
    assert r.status_code in (200, 201), r.text
    # Le releve voit la session retiree (sans l'enregistrer) : son IP est connue.
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200

    r = app_client.post("/api/v1/clients/removed/clean", headers=cle)
    assert r.status_code == 200, r.text
    assert r.json()["same_as"] == [
        {"login": "dupont", "same_as": "ACME-SIEGE", "reason": "same IP"}
    ]
    liste = app_client.get("/api/v1/clients/removed", headers=cle).json()
    assert liste["count"] == 0
    assert [(d["login"], d["same_as"]) for d in liste["duplicates"]] == [("dupont", "ACME-SIEGE")]
    # Toujours ignoree : le releve ne la recree pas.
    assert app_client.post("/api/v1/jobs/collect_subscribers/run", headers=cle).status_code == 200
    abonnes = app_client.get("/api/v1/subscribers", headers=cle).json()
    lignes = abonnes if isinstance(abonnes, list) else abonnes.get("subscribers", [])
    assert "dupont" not in {str(x["login"]) for x in lignes}

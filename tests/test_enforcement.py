"""Execution de l'enforcement : verrous, echecs, journal.

C'est le seul code du projet qui ecrit sur un equipement. Ces tests portent
d'abord sur ce qui doit EMPECHER une ecriture.
"""

from __future__ import annotations

import pytest

from app.config import RouterConfig
from app.enforcement.models import Plan, PlanAction
from app.enforcement.routeros import (
    MissingWriteCredentialsError,
    apply_plan,
    write_config,
)


class FauxClientEcriture:
    def __init__(self, echoue_sur: set[str] | None = None) -> None:
        self.executed: list[PlanAction] = []
        self.echoue_sur = echoue_sur or set()
        self.closed = False

    def execute(self, action):
        self.executed.append(action)
        nom = action.name or action.fields.get("name", "")
        if nom in self.echoue_sur:
            raise RuntimeError("failure: already have such name")
        return {"id": "*99"}

    def close(self) -> None:
        self.closed = True


def action(nom: str, verb: str = "add") -> PlanAction:
    return PlanAction(
        verb=verb, path="/queue/simple", fields={"name": nom}, name=nom, target_id="*1"
    )


def plan_de(*noms: str) -> Plan:
    return Plan(router_name="pop-nord", actions=[action(n) for n in noms])


# ------------------------------------------------- separation des comptes
def test_le_compte_d_ecriture_est_distinct_du_compte_de_lecture(monkeypatch) -> None:
    monkeypatch.setenv("MT_RW", "secret-ecriture")
    config = RouterConfig(
        name="pop",
        host="10.0.0.1",
        username="qos-ro",
        password="secret-lecture",
        rw_username="qos-rw",
        rw_password_env="MT_RW",
    )

    ecriture = write_config(config)

    assert ecriture.username == "qos-rw"
    assert ecriture.resolve_password() == "secret-ecriture"
    # La config de lecture n'est pas alteree.
    assert config.username == "qos-ro"


def test_sans_compte_d_ecriture_le_routeur_est_hors_de_portee() -> None:
    """Ne PAS declarer rw_* est un moyen sur de proteger un PoP."""
    config = RouterConfig(name="pop", host="10.0.0.1", password="lecture")
    with pytest.raises(MissingWriteCredentialsError, match="rw_username"):
        write_config(config)


def test_pas_de_repli_sur_le_compte_de_lecture(monkeypatch) -> None:
    """Retomber sur qos-ro annulerait la separation des privileges."""
    monkeypatch.delenv("MT_ABSENT", raising=False)
    config = RouterConfig(
        name="pop",
        host="10.0.0.1",
        password="lecture",
        rw_username="qos-rw",
        rw_password_env="MT_ABSENT",
    )
    with pytest.raises(MissingWriteCredentialsError):
        write_config(config)


def test_rw_username_sans_variable_de_mot_de_passe() -> None:
    config = RouterConfig(name="pop", host="10.0.0.1", password="x", rw_username="qos-rw")
    with pytest.raises(MissingWriteCredentialsError, match="rw_password_env"):
        write_config(config)


# ---------------------------------------------------------------- dry-run
async def test_dry_run_n_envoie_rien() -> None:
    client = FauxClientEcriture()
    resultat = await apply_plan(plan_de("a", "b"), client, dry_run=True)

    assert client.executed == []
    assert resultat.dry_run is True
    assert resultat.applied == 2
    assert all(o.detail == "simule" for o in resultat.outcomes)


async def test_application_reelle_envoie_dans_l_ordre() -> None:
    client = FauxClientEcriture()
    resultat = await apply_plan(plan_de("parent", "enfant"), client, dry_run=False)

    assert [a.name for a in client.executed] == ["parent", "enfant"]
    assert resultat.ok and resultat.applied == 2


# ------------------------------------------------------------- coupe-circuit
async def test_plan_trop_gros_refuse() -> None:
    """Un plan anormalement gros signale un etat desire mal calcule : mieux vaut
    s'arreter que de reecrire tout un PoP."""
    client = FauxClientEcriture()
    gros = plan_de(*[f"q{i}" for i in range(60)])

    resultat = await apply_plan(gros, client, dry_run=False, max_actions=50)

    assert client.executed == []
    assert resultat.ok is False
    assert "limite de securite" in (resultat.aborted_reason or "")


# ------------------------------------------------------------------ echecs
async def test_arret_au_premier_echec() -> None:
    """Les actions suivantes dependent peut-etre de celle qui a echoue."""
    client = FauxClientEcriture(echoue_sur={"b"})
    resultat = await apply_plan(plan_de("a", "b", "c"), client, dry_run=False)

    assert [a.name for a in client.executed] == ["a", "b"]
    assert resultat.applied == 1
    assert resultat.failed == 1
    assert resultat.ok is False
    assert "interrompu" in (resultat.aborted_reason or "")


async def test_poursuite_possible_apres_echec() -> None:
    client = FauxClientEcriture(echoue_sur={"b"})
    resultat = await apply_plan(plan_de("a", "b", "c"), client, dry_run=False, stop_on_error=False)

    assert [a.name for a in client.executed] == ["a", "b", "c"]
    assert resultat.applied == 2 and resultat.failed == 1


async def test_le_message_d_erreur_du_routeur_est_conserve() -> None:
    client = FauxClientEcriture(echoue_sur={"a"})
    resultat = await apply_plan(plan_de("a"), client, dry_run=False)

    echec = resultat.outcomes[0]
    assert echec.ok is False
    assert "already have such name" in echec.detail


async def test_compte_rendu_serialisable() -> None:
    client = FauxClientEcriture()
    resultat = await apply_plan(plan_de("a"), client, dry_run=True)
    donnees = resultat.to_dict()

    assert donnees["router"] == "pop-nord"
    assert donnees["dry_run"] is True
    assert donnees["results"][0]["command"].startswith("/queue/simple/add")


async def test_plan_vide() -> None:
    resultat = await apply_plan(Plan(router_name="pop"), FauxClientEcriture(), dry_run=False)
    assert resultat.ok and resultat.applied == 0

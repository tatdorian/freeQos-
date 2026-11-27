"""Files ORPHELINES : une ancienne file freeQoS qui masque celle du client.

Une fiche fusionnee (ou un client renomme) laissait sa file sur le routeur. La
reconciliation ne purge pas : cette file, placee plus haut, gardait tout le
trafic du client avec un debit fige ; la file du client ne mesurait plus rien
et le planificateur horaire n'avait plus d'effet. Elle part desormais d'elle-
meme -- et SEULEMENT elle.
"""

from __future__ import annotations

from typing import Any

from app.enforcement.planner import SubscriberTarget, build_plan, desired_state


def _session(login: str = "SESS-0042", adresse: str = "10.20.0.42") -> SubscriberTarget:
    return SubscriberTarget(
        login=login, interface="", address=adresse, plan_down_mbps=8, plan_up_mbps=4
    )


def _file(nom: str, cible: str, ident: str, **extra: Any) -> dict[str, Any]:
    return {
        ".id": ident,
        "name": nom,
        "target": cible,
        "max-limit": "8000000/8000000",
        "_freeqos": True,
        **extra,
    }


def _plan(abonnes: list[SubscriberTarget], actuelles: list[dict[str, Any]]):
    _types, files, _ = desired_state(links=[], subscribers=abonnes)
    voulue = files[0].routeros_fields()
    voulue.update({".id": "*2", "_freeqos": True})
    plan = build_plan(
        "pop-test",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[*actuelles, voulue],
        prune=False,
    )
    return files[0], plan


def test_l_orpheline_sur_la_cible_du_client_est_retiree() -> None:
    session = _session()
    cible = session.queue_target()
    orpheline = _file("ACME-SIEGE", cible, "*1")
    voulue, plan = _plan([session], [orpheline])

    retraits = [a for a in plan.actions if a.verb == "remove"]
    assert [a.name for a in retraits] == ["ACME-SIEGE"]
    assert retraits[0].target_id == "*1"
    assert "orphan" in retraits[0].reason
    # La file du client reste, et elle n'est plus signalee comme masquee.
    assert all(a.name != voulue.name for a in retraits)
    assert not plan.conflicts


def test_une_file_tierce_n_est_jamais_retiree() -> None:
    """Sans marque ni memoire freeQoS, la file est a l'exploitant : on n'y touche pas."""
    session = _session()
    tierce = {".id": "*1", "name": "posee-a-la-main", "target": session.queue_target()}
    _voulue, plan = _plan([session], [tierce])
    assert not [a for a in plan.actions if a.verb == "remove"]


def test_la_file_d_un_client_hors_ligne_ailleurs_reste() -> None:
    """Autre cible : c'est la file gardee d'un client absent, pas une orpheline."""
    hors_ligne = _file("ACME-ANNEXE", "10.20.0.99/32", "*1")
    _voulue, plan = _plan([_session()], [hors_ligne])
    assert not [a for a in plan.actions if a.verb == "remove"]


def test_sans_aucune_session_rien_n_est_orphelin() -> None:
    """Liste de sessions vide le temps d'une coupure : le PoP n'est pas vide."""
    plan = build_plan(
        "pop-test",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=[_file("ACME-SIEGE", "10.20.0.42/32", "*1")],
        prune=False,
    )
    assert plan.actions == []

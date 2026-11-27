"""La file d'une session PPPoE porte le NOM SAISI du client, pas son login.

L'exploitant a nomme ses clients (« ACME-SIEGE ») ; la file sur le routeur
s'appelait pourtant « SESS-0042 », le login PPPoE. Elle prend maintenant le
nom saisi, et la file existante est RENOMMEE en place (jamais doublee).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.enforcement.planner import SubscriberTarget, build_plan, desired_state
from app.models import KIND_STATIC
from app.services.shaping import ShapingService


def _session(login: str, ip: str, label: str | None = None) -> SubscriberTarget:
    return SubscriberTarget(
        login=login, interface="", address=ip, plan_down_mbps=8, plan_up_mbps=4, label=label
    )


class Fiches:
    def __init__(self, noms: dict[str, str]) -> None:
        self.noms = noms

    async def list_all(self) -> list[dict[str, Any]]:
        return [{"login": k, "display_name": v} for k, v in self.noms.items()]


def _service(noms: dict[str, str]) -> Any:
    faux = SimpleNamespace(client_profiles=Fiches(noms), queue_names={})
    faux._nommer_les_files = ShapingService._nommer_les_files.__get__(faux)
    faux.queue_name_for = ShapingService.queue_name_for.__get__(faux)
    return faux


async def test_la_file_prend_le_nom_saisi() -> None:
    service = _service({"SESS-0042": "ACME SIEGE"})
    abonnes = [_session("SESS-0042", "10.20.0.42"), _session("SESS-0043", "10.20.0.43")]
    await service._nommer_les_files(abonnes)
    assert [a.queue_name for a in abonnes] == ["ACME-SIEGE", "SESS-0043"]
    assert service.queue_name_for("SESS-0042") == "ACME-SIEGE"
    assert service.queue_name_for("SESS-0043") == "SESS-0043"


async def test_deux_clients_au_meme_nom_gardent_leur_login() -> None:
    """Un nom de file est une cle : il ne designe jamais deux clients."""
    service = _service({"SESS-0042": "ACME", "SESS-0043": "acme"})
    abonnes = [_session("SESS-0042", "10.20.0.42"), _session("SESS-0043", "10.20.0.43")]
    await service._nommer_les_files(abonnes)
    assert [a.queue_name for a in abonnes] == ["SESS-0042", "SESS-0043"]


async def test_un_nom_deja_pris_par_un_autre_client_n_est_pas_vole() -> None:
    service = _service({"SESS-0042": "ACME-SHOP"})
    statique = SubscriberTarget(
        login="ACME-SHOP",
        interface="",
        kind=KIND_STATIC,
        address="10.20.0.50",
        plan_down_mbps=8,
        plan_up_mbps=8,
    )
    abonnes = [_session("SESS-0042", "10.20.0.42"), statique]
    await service._nommer_les_files(abonnes)
    assert [a.queue_name for a in abonnes] == ["SESS-0042", "ACME-SHOP"]


def test_la_file_au_login_est_renommee_en_place() -> None:
    _types, files, _ = desired_state(
        links=[], subscribers=[_session("SESS-0042", "10.20.0.42", label="ACME-SIEGE")]
    )
    existante = {
        ".id": "*7",
        "name": "SESS-0042",
        "target": "10.20.0.42/32",
        "max-limit": "4000000/8000000",
        "_freeqos": True,
    }
    plan = build_plan(
        "pop-test",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[existante],
        prune=False,
    )
    assert [a.verb for a in plan.actions] == ["set"]
    action = plan.actions[0]
    assert action.target_id == "*7"
    assert action.fields["name"] == "ACME-SIEGE"
    assert action.changes["name"] == ("SESS-0042", "ACME-SIEGE")
    assert not plan.conflicts

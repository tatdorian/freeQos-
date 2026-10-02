"""Une restriction posee doit BLOQUER -- constate : "les restrictions d'IP ne
fonctionnent pas".

Deux causes : la regle de drop etait ajoutee en FIN de chaine forward, derriere
un "accept" de l'exploitant qui laissait passer le trafic avant elle ; et "pour
ce client seulement" ne visait que l'adresse saisie, alors que le client par
VLAN emet depuis une autre IP de sa VLAN.
"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace

from app.collectors import mikrotik
from app.enforcement.restrictions import (
    PATH_FILTER,
    RouterRestrictionState,
    RuleTarget,
    plan_restrictions,
    tag,
)
from app.services.restrictions import vlan_networks_for

ACCEPT = {".id": "*A", "chain": "forward", "action": "accept", "connection-state": "established"}


def test_le_drop_est_place_avant_les_accept_de_l_exploitant() -> None:
    etat = RouterRestrictionState(filters=[ACCEPT])
    plan = plan_restrictions(
        "nas", [RuleTarget(rule_id=1, name="b", destinations=("93.184.216.34/32",))], etat
    )
    drops = [a for a in plan.actions if a.path == PATH_FILTER and a.verb == "add"]
    assert len(drops) == 2
    assert all(a.fields.get("place-before") == "*A" for a in drops)


def test_un_drop_pose_trop_bas_est_remonte() -> None:
    cible = RuleTarget(rule_id=1, name="b", destinations=("93.184.216.34/32",))
    bas = {".id": "*D", "chain": "forward", "action": "drop", "comment": tag(1, "drop-up")}
    etat = RouterRestrictionState(filters=[ACCEPT, bas])
    plan = plan_restrictions("nas", [cible], etat)
    verbes = [
        (a.verb, a.target_id or a.fields.get("place-before"))
        for a in plan.actions
        if a.path == PATH_FILTER
    ]
    assert ("remove", "*D") in verbes
    assert ("add", "*A") in verbes


def test_pour_un_client_seul_sur_sa_vlan_tout_son_reseau_est_vise() -> None:
    mikrotik._RESEAUX_CONNECTES.clear()
    mikrotik._RESEAUX_CONNECTES["nas"] = [
        (ipaddress.ip_network("100.100.105.240/30"), "vlan2060-nestle-siege")
    ]
    shaping = SimpleNamespace(sole_vlan_interfaces={"nestle": "vlan2060-nestle-siege"})
    assert vlan_networks_for(shaping, ["nestle"]) == ["100.100.105.240/30"]
    assert vlan_networks_for(shaping, ["autre"]) == []
    mikrotik._RESEAUX_CONNECTES.clear()

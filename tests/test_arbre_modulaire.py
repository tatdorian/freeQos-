"""Arbre reseau modulaire : chaque client a une place, trouvee automatiquement
et corrigeable a la main -- et aucun ne disparait."""

from __future__ import annotations

from app.services.tree_placement import normalise, place_clients

ROUTEUR = {"key": "router:nas", "name": "PoP Plateau", "kind": "pop", "router_name": "nas"}
SECTEUR = {"key": "mac:01", "name": "AP-Nord", "kind": "radio"}
BOULANGERIE = {"key": "mac:02", "name": "Boulangerie-Fleuve", "kind": "unknown"}


def placer(
    clients,
    *,
    visible=None,
    hidden=(),
    links=(),
    attachments=None,
    sites=(),
    places=None,
    liens=None,
    noms=None,
):  # type: ignore[no-untyped-def]
    return place_clients(
        visible=visible if visible is not None else [ROUTEUR, SECTEUR, BOULANGERIE],
        hidden=hidden,
        links=links,
        clients=clients,
        attachments=attachments or {},
        pop_sites=sites,
        manual_places=places or {},
        manual_links=liens or {},
        names=noms or {},
    )


def test_les_noms_se_comparent_sans_casse_accents_ni_ponctuation() -> None:
    assert normalise("Nestlé SA") == normalise("nestle-sa") == normalise("NESTLE_SA")


def test_un_client_pend_sous_son_site() -> None:
    r = placer([{"login": "dupont", "pop_name": "PoP Plateau"}])
    assert r["placements"]["dupont"] == {"node": "router:nas", "how": "site"}


def test_un_equipement_qui_porte_le_nom_du_client_est_ce_client() -> None:
    """Le nom saisi « Boulangerie Fleuve » retrouve l'equipement decouvert
    « Boulangerie-Fleuve » : la case EST le client, sans doublon."""
    r = placer(
        [{"login": "boul01", "pop_name": "PoP Plateau"}],
        noms={"boul01": ["Boulangerie Fleuve"]},
    )
    assert r["client_nodes"] == {"mac:02": {"login": "boul01", "how": "name"}}
    assert r["placements"]["boul01"] == {"node": "mac:02", "how": "device"}


def test_jamais_de_lien_par_le_nom_avec_un_routeur_ni_un_nom_ambigu() -> None:
    double = {"key": "mac:03", "name": "boulangerie fleuve", "kind": "cpe"}
    r = placer(
        [{"login": "pop-plateau", "pop_name": "PoP Plateau"}, {"login": "boulangerie-fleuve"}],
        visible=[ROUTEUR, BOULANGERIE, double],
    )
    assert r["client_nodes"] == {}
    assert r["placements"]["pop-plateau"]["how"] == "site"


def test_un_lien_refuse_a_la_main_ne_revient_pas_par_le_nom() -> None:
    r = placer([{"login": "boulangerie-fleuve"}], liens={"mac:02": ""})
    assert r["client_nodes"] == {}


def test_un_lien_pose_a_la_main_prime() -> None:
    r = placer([{"login": "kader", "pop_name": "PoP Plateau"}], liens={"mac:01": "kader"})
    assert r["client_nodes"] == {"mac:01": {"login": "kader", "how": "manual"}}


def test_sa_propre_case_cachee_le_place_sous_son_secteur() -> None:
    """La case d'un client a IP fixe (ou d'un CPE reconnu) n'est pas servie,
    mais elle dit sous quoi il pend : il pend sous le meme."""
    cache = {
        "key": "static:acme",
        "name": "acme",
        "kind": "static",
        "attributes": {"reference": "acme"},
    }
    cpe = {"key": "mac:09", "name": "x", "kind": "cpe", "attributes": {"subscriber_cpe": "dupont"}}
    r = placer(
        [
            {"login": "acme", "pop_name": "PoP Plateau"},
            {"login": "dupont", "pop_name": "PoP Plateau"},
        ],
        hidden=[cache, cpe],
        links=[
            {"source_key": "static:acme", "target_key": "mac:01"},
            {"source_key": "mac:01", "target_key": "mac:09"},
        ],
    )
    assert r["placements"]["acme"] == {"node": "mac:01", "how": "graph"}
    assert r["placements"]["dupont"] == {"node": "mac:01", "how": "graph"}


def test_le_secteur_puis_le_site_de_vlan_puis_rien_n_est_perdu() -> None:
    r = placer(
        [
            {"login": "a", "pop_name": "PoP Plateau"},
            {"login": "b", "pop_name": "VLAN 2060"},
            {"login": "c", "pop_name": "Site inconnu"},
        ],
        attachments={"a": "mac:01"},
        sites=[{"name": "VLAN 2060", "router_name": "nas"}],
    )
    assert r["placements"]["a"] == {"node": "mac:01", "how": "sector"}
    assert r["placements"]["b"] == {"node": "router:nas", "how": "vlan-site", "site": "VLAN 2060"}
    assert r["placements"]["c"] == {"node": None, "how": "unplaced", "site": "Site inconnu"}


def test_le_placement_a_la_main_passe_avant_tout() -> None:
    r = placer(
        [{"login": "boulangerie-fleuve", "pop_name": "PoP Plateau"}],
        places={"boulangerie-fleuve": "mac:01"},
    )
    assert r["placements"]["boulangerie-fleuve"] == {"node": "mac:01", "how": "manual"}
    # Une case disparue : le placement a la main cede a l'automatique.
    r = placer([{"login": "x", "pop_name": "PoP Plateau"}], places={"x": "mac:disparu"})
    assert r["placements"]["x"]["how"] == "site"

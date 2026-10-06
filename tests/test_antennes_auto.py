"""Les radios decouvertes deviennent des antennes interrogees, sans saisie."""

from __future__ import annotations

from app.services.antenna_enroll import antennas_to_enroll

POPS = {"nas-sud": "PoP Sud"}


def _radio(**kw):  # type: ignore[no-untyped-def]
    return {
        "key": "mac:DC:9F:DB:11:22:33",
        "name": "NanoBeam-Sud",
        "kind": "radio",
        "mac": "DC:9F:DB:11:22:33",
        "address": "10.10.0.20",
        "router_name": "nas-sud",
        "attributes": {},
        **kw,
    }


def test_une_radio_decouverte_est_ajoutee_a_son_pop() -> None:
    [fiche] = antennas_to_enroll([_radio()], [], pop_of_router=POPS, username="ubnt")
    assert fiche == {
        "name": "NanoBeam-Sud",
        "pop_name": "PoP Sud",
        "host": "10.10.0.20",
        "username": "ubnt",
        "device_key": "DC:9F:DB:11:22:33",
        "verify_tls": False,
        "enabled": True,
    }


def test_une_antenne_deja_connue_n_est_pas_dupliquee() -> None:
    existantes = [{"name": "BH-Sud", "host": "10.10.0.99", "device_key": "dc-9f-db-11-22-33"}]
    assert antennas_to_enroll([_radio()], existantes, pop_of_router=POPS, username="u") == []
    par_hote = [{"name": "X", "host": "10.10.0.20"}]
    assert antennas_to_enroll([_radio()], par_hote, pop_of_router=POPS, username="u") == []


def test_ni_routeur_gere_ni_equipement_sans_adresse_ni_autre_nature() -> None:
    noeuds = [
        _radio(attributes={"managed": True}),
        _radio(address=None, key="b", mac=None),
        _radio(kind="switch", key="c", mac="AA:AA:AA:AA:AA:AA", address="10.0.0.9"),
        _radio(router_name="inconnu", key="d", mac="BB:BB:BB:BB:BB:BB", address="10.0.0.8"),
    ]
    assert antennas_to_enroll(noeuds, [], pop_of_router=POPS, username="u") == []


def test_un_homonyme_garde_son_adresse_dans_le_nom() -> None:
    existantes = [{"name": "NanoBeam-Sud", "host": "10.10.0.1"}]
    [fiche] = antennas_to_enroll([_radio()], existantes, pop_of_router=POPS, username="u")
    assert fiche["name"] == "NanoBeam-Sud (10.10.0.20)"

"""Droits reels du compte, lus sur le routeur.

Le controleur refusait d'ecrire tant qu'un compte distinct n'etait pas DECLARE.
C'etait verifier l'inventaire, pas la realite. Ces tests portent sur la lecture
effective des politiques et sur le refus de bloquer quand elle est impossible.
"""

from __future__ import annotations

import pytest

from app.enforcement.capability import (
    inspect_write_capability,
    permission_hint,
)

GROUPES = [
    {"name": "qos-ro", "policy": "read,api,test"},
    {"name": "qos-rw", "policy": "read,write,api,test"},
    {"name": "sans-api", "policy": "read,write,test"},
    {"name": "refus-explicite", "policy": "read,api,test,!write"},
]
COMPTES = [
    {"name": "qos-ro", "group": "qos-ro"},
    {"name": "qos-rw", "group": "qos-rw"},
    {"name": "admin", "group": "full"},
    {"name": "partiel", "group": "sans-api"},
    {"name": "refuse", "group": "refus-explicite"},
]


def test_compte_complet_peut_ecrire() -> None:
    """Le groupe integre 'full' porte toutes les politiques sans les enumerer."""
    verdict = inspect_write_capability("admin", COMPTES, GROUPES)
    assert verdict.can_write is True
    assert verdict.group == "full"


def test_compte_avec_write_et_api() -> None:
    verdict = inspect_write_capability("qos-rw", COMPTES, GROUPES)
    assert verdict.can_write is True
    assert set(verdict.policies) >= {"read", "write", "api"}


def test_compte_de_lecture_ne_peut_pas() -> None:
    verdict = inspect_write_capability("qos-ro", COMPTES, GROUPES)
    assert verdict.can_write is False
    assert verdict.missing == ["write"]
    assert "il manque write" in verdict.detail


def test_api_manquante_aussi_bloquante() -> None:
    """Sans 'api', le compte ne peut pas se connecter du tout par l'API binaire."""
    verdict = inspect_write_capability("partiel", COMPTES, GROUPES)
    assert verdict.can_write is False
    assert verdict.missing == ["api"]


def test_politique_refusee_explicitement() -> None:
    """RouterOS prefixe d'un '!' les politiques explicitement retirees."""
    verdict = inspect_write_capability("refuse", COMPTES, GROUPES)
    assert verdict.can_write is False
    assert "write" not in verdict.policies


def test_casse_ignoree() -> None:
    verdict = inspect_write_capability("QOS-RW", COMPTES, GROUPES)
    assert verdict.can_write is True


# ------------------------------------------- ce qui ne doit PAS bloquer
def test_compte_absent_ne_bloque_pas() -> None:
    """Authentification RADIUS ou /user partiellement lisible : on ne peut rien
    affirmer, donc on n'interdit rien."""
    verdict = inspect_write_capability("via-radius", COMPTES, GROUPES)
    assert verdict.can_write is None
    assert "verifies a l'execution" in verdict.detail


def test_groupe_introuvable_ne_bloque_pas() -> None:
    verdict = inspect_write_capability(
        "orphelin", [{"name": "orphelin", "group": "groupe-inconnu"}], GROUPES
    )
    assert verdict.can_write is None
    assert verdict.group == "groupe-inconnu"


def test_listes_vides_ne_bloquent_pas() -> None:
    assert inspect_write_capability("x", [], []).can_write is None


# ------------------------------------------------ traduction des refus
@pytest.mark.parametrize(
    "message",
    ["not enough permissions", "failure: not enough permissions (9)", "permission denied"],
)
def test_refus_de_routeros_traduit(message: str) -> None:
    """RouterOS dit 'not enough permissions' sans preciser laquelle manque."""
    indice = permission_hint(RuntimeError(message), "admin")

    assert indice is not None
    assert "admin" in indice
    assert "policy=read,write,api,test" in indice


def test_autre_erreur_non_traduite() -> None:
    """Ne pas confondre un probleme de droits avec autre chose."""
    assert permission_hint(TimeoutError("timed out"), "admin") is None
    assert permission_hint(RuntimeError("already have such name"), "admin") is None

"""Chiffrement des identifiants routeur."""

from __future__ import annotations

import pytest

from app.services.crypto import PREFIX, SecretBox, SecretUnavailableError, generate_key


def test_aller_retour() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt("motdepasse-du-routeur")
    assert box.decrypt(token) == "motdepasse-du-routeur"


def test_le_chiffre_ne_contient_pas_le_clair() -> None:
    box = SecretBox(generate_key())
    assert "motdepasse" not in box.encrypt("motdepasse")


def test_chiffrement_non_deterministe() -> None:
    """Deux routeurs partageant le meme mot de passe ne doivent pas etre
    reperables par comparaison des colonnes en base."""
    box = SecretBox(generate_key())
    assert box.encrypt("identique") != box.encrypt("identique")


def test_une_autre_cle_ne_dechiffre_pas() -> None:
    token = SecretBox(generate_key()).encrypt("secret")
    with pytest.raises(Exception):  # noqa: B017 - InvalidToken vient de cryptography
        SecretBox(generate_key()).decrypt(token)


def test_valeur_en_clair_refusee() -> None:
    """Une valeur non chiffree en base est une anomalie : on refuse de l'utiliser
    plutot que de la rattraper silencieusement."""
    box = SecretBox(generate_key())
    with pytest.raises(SecretUnavailableError, match="non chiffre"):
        box.decrypt("motdepasse-en-clair")


def test_sans_cle_indisponible_mais_pas_fatal() -> None:
    box = SecretBox(None)
    assert box.available is False
    assert "APP_SECRET_KEY" in (box.unavailable_reason or "")
    with pytest.raises(SecretUnavailableError):
        box.encrypt("secret")


def test_cle_invalide_signalee_clairement() -> None:
    box = SecretBox("pas-une-cle-fernet")
    assert box.available is False
    assert "invalide" in (box.unavailable_reason or "")


def test_prefixe_permet_de_reperer_les_valeurs_chiffrees() -> None:
    assert SecretBox(generate_key()).encrypt("x").startswith(PREFIX)

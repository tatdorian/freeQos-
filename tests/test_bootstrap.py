"""Demarrage sans rien preparer : cle et base creees automatiquement.

Ce sont les deux choses qu'un operateur ne devrait pas avoir a faire a la main,
et les deux dont la mauvaise gestion coute le plus cher.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.crypto import KeySource, SecretBox, load_or_create_key


# ------------------------------------------------------------ resolution
def test_env_prioritaire_sur_le_fichier(tmp_path: Path) -> None:
    """Un coffre a secrets doit pouvoir imposer la cle."""
    fichier = tmp_path / "secret.key"
    fichier.write_text("cle-du-fichier")

    cle, source, _ = load_or_create_key(env_key="cle-de-l-env", key_file=fichier)

    assert cle == "cle-de-l-env"
    assert source == KeySource.ENV


def test_generation_au_premier_demarrage(tmp_path: Path) -> None:
    fichier = tmp_path / "sous-dossier" / "secret.key"

    cle, source, chemin = load_or_create_key(env_key=None, key_file=fichier)

    assert source == KeySource.GENERATED
    assert chemin == fichier
    assert fichier.exists()
    # La cle doit etre utilisable immediatement.
    assert SecretBox(cle).decrypt(SecretBox(cle).encrypt("x")) == "x"


def test_la_cle_survit_au_redemarrage(tmp_path: Path) -> None:
    """Point critique : une cle regeneree a chaque demarrage rendrait illisibles
    tous les mots de passe deja stockes."""
    fichier = tmp_path / "secret.key"

    premiere, _, _ = load_or_create_key(env_key=None, key_file=fichier)
    seconde, source, _ = load_or_create_key(env_key=None, key_file=fichier)

    assert premiere == seconde
    assert source == KeySource.FILE


def test_la_cle_n_est_lisible_que_par_son_proprietaire(tmp_path: Path) -> None:
    fichier = tmp_path / "secret.key"
    load_or_create_key(env_key=None, key_file=fichier)

    assert os.stat(fichier).st_mode & 0o777 == 0o600


def test_fichier_vide_regenere(tmp_path: Path) -> None:
    fichier = tmp_path / "secret.key"
    fichier.write_text("   \n")

    cle, source, _ = load_or_create_key(env_key=None, key_file=fichier)

    assert source == KeySource.GENERATED
    assert cle and cle.strip()


def test_generation_desactivable(tmp_path: Path) -> None:
    cle, source, _ = load_or_create_key(
        env_key=None, key_file=tmp_path / "secret.key", autogenerate=False
    )
    assert cle is None
    assert source == KeySource.NONE


def test_sans_chemin_ni_env(tmp_path: Path) -> None:
    cle, source, _ = load_or_create_key(env_key=None, key_file=None)
    assert cle is None and source == KeySource.NONE


def test_ecriture_impossible(tmp_path: Path) -> None:
    """Volume en lecture seule, disque plein : on ne doit pas pretendre avoir une
    cle qui ne survivrait pas au redemarrage.

    Le chemin vise ici un repertoire existant, ce qui fait echouer l'ecriture de
    facon deterministe — contrairement a un chmod, que root ignore.
    """
    obstacle = tmp_path / "secret.key"
    obstacle.mkdir()

    cle, source, chemin = load_or_create_key(env_key=None, key_file=obstacle)

    assert cle is None
    assert source == KeySource.NONE
    assert chemin == obstacle


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignore les permissions de fichier")
def test_repertoire_en_lecture_seule(tmp_path: Path) -> None:
    interdit = tmp_path / "ro"
    interdit.mkdir()
    interdit.chmod(0o500)
    try:
        cle, source, _ = load_or_create_key(env_key=None, key_file=interdit / "secret.key")
        assert cle is None
        assert source == KeySource.NONE
    finally:
        interdit.chmod(0o700)


# --------------------------------------------- message de dechiffrement
def test_mauvaise_cle_donne_un_message_exploitable() -> None:
    """InvalidToken n'a pas de message : sans traduction, l'operateur lirait
    'secret illisible :' suivi de rien."""
    from app.services.crypto import SecretUnavailableError, generate_key

    chiffre = SecretBox(generate_key()).encrypt("motdepasse")

    with pytest.raises(SecretUnavailableError) as exc:
        SecretBox(generate_key()).decrypt(chiffre)

    message = str(exc.value)
    assert message.strip()
    assert "cle" in message.lower()
    assert "resaisissez" in message.lower()


# ----------------------------------------------------- indices de connexion
@pytest.mark.parametrize(
    ("erreur", "attendu"),
    [
        ('database "qos" does not exist', "CREATE DATABASE"),
        ("password authentication failed for user", "DATABASE_URL"),
        ("[Errno 111] Connect call failed", "localhost"),
        ("quelque chose d'inattendu", ""),
    ],
)
def test_indices_de_connexion(erreur: str, attendu: str) -> None:
    """Un echec de connexion doit dire quoi faire, pas seulement ce qui a rate."""
    from app.db.database import Database

    db = Database("postgresql://qos@localhost:5432/qos")
    indice = db._connect_hint(RuntimeError(erreur))

    assert attendu in indice


def test_nom_de_base_extrait_du_dsn() -> None:
    from app.db.database import Database

    assert Database("postgresql://u:p@h:5432/qos")._database_name() == "qos"
    assert Database("postgresql://u:p@h:5432/qos?sslmode=require")._database_name() == "qos"
    assert Database("postgresql://u:p@h:5432/")._database_name() is None

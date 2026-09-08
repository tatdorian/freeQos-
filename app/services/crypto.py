"""Chiffrement des identifiants routeur stockes en base.

Ajouter un PoP depuis l'interface implique de conserver son mot de passe cote
serveur : on ne peut plus se contenter d'une variable d'environnement. Le secret
est donc chiffre au repos (Fernet : AES-128-CBC + HMAC-SHA256) avec une cle qui,
elle, reste dans l'environnement.

Consequence assumee : la cle est le seul secret a proteger, et sans elle la base
ne livre rien d'exploitable. Les routeurs declares dans l'inventaire fichier
continuent d'utiliser des variables d'environnement et ne passent jamais par ici.

Generer une cle :

    python -m app.services.crypto
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "fernet:"

# Permissions attendues sur le fichier de cle : lisible par son seul proprietaire.
KEY_FILE_MODE = 0o600


class SecretUnavailableError(RuntimeError):
    """Aucune cle de chiffrement n'est configuree, ou elle est invalide."""


class SecretBox:
    """Chiffre et dechiffre les secrets destines a la base."""

    def __init__(self, key: str | None) -> None:
        self._fernet = None
        self._error: str | None = None

        if not key:
            self._error = (
                "Aucune cle de chiffrement disponible : impossible d'enregistrer "
                "un routeur depuis l'interface. Elle devrait etre generee "
                "automatiquement au premier demarrage ; verifiez que "
                "APP_SECRET_KEY_FILE pointe sur un chemin inscriptible, ou "
                "renseignez APP_SECRET_KEY."
            )
            return

        try:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001
            self._error = f"APP_SECRET_KEY invalide ({exc}). Attendu : une cle Fernet."

    @property
    def available(self) -> bool:
        return self._fernet is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._error

    def _require(self):
        if self._fernet is None:
            raise SecretUnavailableError(self._error or "chiffrement indisponible")
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        return PREFIX + self._require().encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        if not token.startswith(PREFIX):
            # Refus explicite : une valeur en clair en base est une anomalie, pas
            # un cas a rattraper silencieusement.
            raise SecretUnavailableError(
                "Secret non chiffre en base : refus de l'utiliser tel quel"
            )
        from cryptography.fernet import InvalidToken

        try:
            return self._require().decrypt(token[len(PREFIX) :].encode()).decode()
        except InvalidToken as exc:
            # InvalidToken n'a pas de message : sans cette traduction, l'operateur
            # lirait "secret illisible :" suivi de rien du tout.
            raise SecretUnavailableError(
                "dechiffrement impossible : la cle actuelle n'est pas celle qui a "
                "servi a chiffrer ce secret. Restaurez le fichier de cle d'origine "
                "ou resaisissez le mot de passe."
            ) from exc


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


class KeySource:
    """D'ou vient la cle. Utile pour dire a l'operateur ce qui s'est passe."""

    ENV = "env"
    FILE = "file"
    GENERATED = "generated"
    NONE = "none"


def load_or_create_key(
    *,
    env_key: str | None,
    key_file: Path | None,
    autogenerate: bool = True,
) -> tuple[str | None, str, Path | None]:
    """Resout la cle de chiffrement au demarrage.

    Ordre : variable d'environnement, puis fichier, puis generation.

    POURQUOI UN FICHIER, ET PAS UNE GENERATION EN MEMOIRE
    -----------------------------------------------------
    Une cle regeneree a chaque demarrage rendrait ILLISIBLES tous les mots de
    passe deja stockes. La cle doit survivre au processus, donc etre ecrite
    quelque part. Elle ne va pas en base : ce serait la ranger a cote de ce
    qu'elle protege.

    Retourne (cle, source, chemin du fichier).
    """
    if env_key:
        return env_key, KeySource.ENV, None

    if key_file is None:
        return None, KeySource.NONE, None

    key_file = Path(key_file)
    if key_file.is_file():
        try:
            cle = key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.error("Fichier de cle %s illisible : %s", key_file, exc)
            return None, KeySource.NONE, key_file
        if cle:
            _warn_if_readable_by_others(key_file)
            return cle, KeySource.FILE, key_file
        logger.warning("Fichier de cle %s vide : il sera regenere", key_file)
    elif key_file.exists():
        # Cas frequent avec Docker : un bind-mount vers un fichier inexistant
        # cree un REPERTOIRE a sa place.
        logger.error(
            "%s existe mais n'est pas un fichier. Si c'est un montage Docker, "
            "montez un volume sur le repertoire parent plutot que sur le fichier.",
            key_file,
        )
        return None, KeySource.NONE, key_file

    if not autogenerate:
        return None, KeySource.NONE, key_file

    cle = generate_key()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        # Creation en 0600 des l'origine : ne jamais laisser la cle lisible,
        # meme brievement, entre l'ecriture et le chmod.
        descripteur = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEY_FILE_MODE)
        with os.fdopen(descripteur, "w", encoding="utf-8") as fichier:
            fichier.write(cle + "\n")
        os.chmod(key_file, KEY_FILE_MODE)
    except OSError as exc:
        logger.error(
            "Cle de chiffrement non ecrite dans %s (%s). Elle ne survivrait pas a "
            "un redemarrage : l'ajout de PoP depuis l'interface reste desactive.",
            key_file,
            exc,
        )
        return None, KeySource.NONE, key_file

    logger.warning(
        "Cle de chiffrement generee dans %s. SAUVEGARDEZ CE FICHIER : sans lui, "
        "les mots de passe des routeurs enregistres depuis l'interface seront "
        "definitivement illisibles.",
        key_file,
    )
    return cle, KeySource.GENERATED, key_file


def _warn_if_readable_by_others(key_file: Path) -> None:
    try:
        mode = key_file.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "Le fichier de cle %s est lisible au-dela de son proprietaire. "
            "Corrigez avec : chmod 600 %s",
            key_file,
            key_file,
        )


if __name__ == "__main__":  # pragma: no cover - utilitaire en ligne de commande
    print(generate_key())

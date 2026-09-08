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

logger = logging.getLogger(__name__)

PREFIX = "fernet:"


class SecretUnavailableError(RuntimeError):
    """Aucune cle de chiffrement n'est configuree, ou elle est invalide."""


class SecretBox:
    """Chiffre et dechiffre les secrets destines a la base."""

    def __init__(self, key: str | None) -> None:
        self._fernet = None
        self._error: str | None = None

        if not key:
            self._error = (
                "APP_SECRET_KEY n'est pas defini : impossible d'enregistrer un "
                "routeur depuis l'interface. Generez une cle avec "
                "'python -m app.services.crypto'."
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
        return self._require().decrypt(token[len(PREFIX) :].encode()).decode()


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


if __name__ == "__main__":  # pragma: no cover - utilitaire en ligne de commande
    print(generate_key())

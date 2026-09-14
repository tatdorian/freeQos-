"""Verification TLS des connexions RouterOS, configurable PAR ROUTEUR.

Le probleme corrige : la verification TLS etait desactivee en dur
(``check_hostname = False`` + ``CERT_NONE``), et ce sur le module qui ECRIT sur
tous les PoP. Un pair non authentifie sur un canal d'ecriture, c'est une porte
grande ouverte a l'homme du milieu.

Trois postures, choisies par routeur (defaut : la plus sure) :

- ``strict``      : chaine de certification ET nom d'hote verifies. Defaut.
- ``fingerprint`` : le pair est epingle sur l'empreinte SHA-256 de son
                    certificat. Fait pour les certificats auto-signes des CHR,
                    sans renoncer a authentifier le pair.
- ``insecure``    : verification desactivee. Le transport reste chiffre mais le
                    pair n'est pas authentifie. C'est un choix ASSUME et VISIBLE
                    (``describe_tls``), plus un defaut cache.

Le meme wrapper sert la lecture (collecteur) et l'ecriture (enforcement) : la
posture ne depend plus du module qui ouvre la connexion.
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import RouterConfig

TLS_STRICT = "strict"
TLS_FINGERPRINT = "fingerprint"
TLS_INSECURE = "insecure"

_LABELS = {
    TLS_STRICT: "TLS verifie (chaine + nom d'hote)",
    TLS_FINGERPRINT: "TLS epingle (empreinte SHA-256)",
    TLS_INSECURE: "TLS NON verifie (chiffre, pair non authentifie)",
}


class TlsConfigurationError(RuntimeError):
    """Configuration TLS incoherente (empreinte absente ou mal formee)."""


def normalise_fingerprint(value: str) -> str:
    """Empreinte SHA-256 en hexadecimal, ``:`` et espaces tolerees a la saisie."""
    cleaned = value.strip().lower().replace(":", "").replace(" ", "")
    if len(cleaned) != 64 or any(c not in "0123456789abcdef" for c in cleaned):
        raise TlsConfigurationError(
            "empreinte TLS attendue : SHA-256 en hexadecimal (64 caracteres)"
        )
    return cleaned


def ssl_wrapper_for(config: RouterConfig) -> Callable[[Any], ssl.SSLSocket]:
    """Fabrique le wrapper de socket TLS attendu par librouteros, selon la
    posture declaree sur le routeur."""
    mode = config.tls_verify

    if mode == TLS_STRICT:
        context = ssl.create_default_context()
        host = config.host

        def wrap_strict(sock: Any) -> ssl.SSLSocket:
            # SNI + verification du nom d'hote : le pair doit presenter un
            # certificat valide pour cette adresse.
            return context.wrap_socket(sock, server_hostname=host)

        return wrap_strict

    if mode == TLS_INSECURE:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        def wrap_insecure(sock: Any) -> ssl.SSLSocket:
            return context.wrap_socket(sock)

        return wrap_insecure

    if mode == TLS_FINGERPRINT:
        if not config.tls_fingerprint:
            raise TlsConfigurationError(
                f"routeur '{config.name}' : tls_verify=fingerprint exige une empreinte "
                "(tls_fingerprint)"
            )
        expected = normalise_fingerprint(config.tls_fingerprint)
        context = ssl.create_default_context()
        # On n'utilise pas la chaine PKI : on epingle l'empreinte apres handshake.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        def wrap_pinned(sock: Any) -> ssl.SSLSocket:
            wrapped = context.wrap_socket(sock)
            der = wrapped.getpeercert(binary_form=True) or b""
            actual = hashlib.sha256(der).hexdigest()
            if not hmac.compare_digest(actual, expected):
                try:
                    wrapped.close()
                finally:
                    pass
                raise ssl.SSLError(
                    f"empreinte TLS du routeur '{config.name}' inattendue : "
                    f"{actual} (attendu {expected})"
                )
            return wrapped

        return wrap_pinned

    raise TlsConfigurationError(f"mode TLS inconnu : {mode!r}")


def describe_tls(config: RouterConfig) -> dict[str, Any]:
    """Posture TLS lisible par l'interface : c'est ce qui rend une desactivation
    ASSUMEE et visible, au lieu d'etre cachee dans le code."""
    if not config.use_ssl:
        return {"enabled": False, "mode": None, "secure": None, "label": "API binaire (sans TLS)"}
    mode = config.tls_verify
    return {
        "enabled": True,
        "mode": mode,
        "secure": mode != TLS_INSECURE,
        "label": _LABELS.get(mode, mode),
    }

"""Verification TLS configurable par routeur (P1-8).

On prouve, contre un vrai serveur TLS a certificat auto-signe, que :
  - ``strict`` REFUSE un pair non fiable (l'ancien defaut cache le laissait
    passer en silence) ;
  - ``insecure`` l'accepte, mais c'est un choix ASSUME ;
  - ``fingerprint`` epingle l'empreinte : bonne empreinte acceptee, mauvaise
    refusee.
"""

from __future__ import annotations

import datetime
import hashlib
import socket
import ssl
import threading
from pathlib import Path

import pytest

from app.config import RouterConfig
from app.services.tls import (
    TlsConfigurationError,
    describe_tls,
    normalise_fingerprint,
    ssl_wrapper_for,
)


def _self_signed(tmp: Path) -> tuple[Path, Path, str]:
    """Genere un certificat auto-signe pour 127.0.0.1 ; renvoie (cert, key, empreinte)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()

    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, fingerprint


def _try_handshake(config: RouterConfig, cert: Path, key: Path) -> Exception | None:
    """Ouvre un serveur TLS auto-signe, tente le handshake cote client avec le
    wrapper. Renvoie l'exception cote client, ou None si le handshake reussit."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            raw, _ = listener.accept()
            try:
                server_ctx.wrap_socket(raw, server_side=True).close()
            except Exception:  # noqa: BLE001 - un refus cote client fait echouer ici aussi
                pass
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    wrapper = ssl_wrapper_for(config)
    client_error: Exception | None = None
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        wrapper(sock).close()
    except Exception as exc:  # noqa: BLE001 - c'est le verdict qu'on mesure
        client_error = exc
    finally:
        sock.close()
        listener.close()
        thread.join(timeout=2)
    return client_error


def _config(mode: str, fingerprint: str | None = None) -> RouterConfig:
    return RouterConfig(
        name="pop",
        host="127.0.0.1",
        password="x",
        use_ssl=True,
        tls_verify=mode,  # type: ignore[arg-type]
        tls_fingerprint=fingerprint,
    )


def test_strict_refuse_un_certificat_auto_signe(tmp_path: Path) -> None:
    cert, key, _ = _self_signed(tmp_path)
    erreur = _try_handshake(_config("strict"), cert, key)
    assert isinstance(erreur, ssl.SSLError)


def test_insecure_accepte_mais_reste_un_choix_assume(tmp_path: Path) -> None:
    cert, key, _ = _self_signed(tmp_path)
    assert _try_handshake(_config("insecure"), cert, key) is None
    # ... et cette posture est visible, pas cachee.
    assert describe_tls(_config("insecure"))["secure"] is False


def test_fingerprint_epingle_le_pair(tmp_path: Path) -> None:
    cert, key, empreinte = _self_signed(tmp_path)
    # Bonne empreinte : accepte.
    assert _try_handshake(_config("fingerprint", empreinte), cert, key) is None
    # Mauvaise empreinte : refuse.
    mauvaise = "00" * 32
    erreur = _try_handshake(_config("fingerprint", mauvaise), cert, key)
    assert isinstance(erreur, ssl.SSLError)


def test_describe_tls_expose_la_posture() -> None:
    assert describe_tls(_config("strict"))["secure"] is True
    assert describe_tls(_config("insecure"))["secure"] is False
    sans_tls = RouterConfig(name="p", host="10.0.0.1", password="x", use_ssl=False)
    assert describe_tls(sans_tls)["enabled"] is False


def test_normalise_fingerprint() -> None:
    # 32 octets = 64 caracteres hex, avec des ':' et de la casse a nettoyer.
    brute = "AB:CD:" + ":".join(["00"] * 29) + ":EF"
    assert normalise_fingerprint(brute) == ("abcd" + "00" * 29 + "ef")
    with pytest.raises(TlsConfigurationError):
        normalise_fingerprint("trop-court")


def test_config_fingerprint_exige_une_empreinte() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        RouterConfig(
            name="p", host="10.0.0.1", password="x", use_ssl=True, tls_verify="fingerprint"
        )

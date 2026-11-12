"""Servir l'interface sous un NOM DE DOMAINE, en HTTPS, depuis l'interface.

Le proxy Caddy de la pile Docker recoit le web (80 / 443). Sans domaine, il
passe tout a l'application en HTTP (``http://<adresse du serveur>``). Avec un
domaine, on lui pousse une configuration complete par son API d'administration
(``POST /load``, reseau interne uniquement) :

- ``https://<domaine>`` -> l'application, certificat Let's Encrypt obtenu et
  renouvele par Caddy (defi HTTP sur le port 80) ;
- ``http://<domaine>`` -> redirection vers https ;
- ``http://<adresse IP>`` reste ouvert : l'acces de secours, le temps que le
  DNS se propage ou si le certificat n'a pas pu etre obtenu.

Le DNS se fait chez le registraire (OVH ou autre) : un enregistrement « A » du
domaine vers l'adresse publique du serveur. On le verifie avant d'appliquer.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UPSTREAM = "app:8000"
ADMIN_LISTEN = "0.0.0.0:2019"
ADMIN_ORIGINS = ["proxy:2019"]

_DOMAINE = re.compile(r"^(?=.{4,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidDomainError(ValueError):
    pass


def check_domain(value: str) -> str:
    """``QoS.Example.com.`` -> ``qos.example.com`` ; refuse une IP, un chemin."""
    domaine = str(value or "").strip().lower().rstrip(".")
    for prefixe in ("https://", "http://"):
        if domaine.startswith(prefixe):
            domaine = domaine[len(prefixe) :]
    domaine = domaine.split("/")[0]
    if not _DOMAINE.match(domaine):
        raise InvalidDomainError(
            f"'{value}' is not a domain name: give it like qos.example.com (no IP, no path)"
        )
    return domaine


def check_email(value: str | None) -> str | None:
    email = str(value or "").strip()
    if not email:
        return None
    if not _EMAIL.match(email):
        raise InvalidDomainError("the certificate email is not valid")
    return email


def _proxy() -> dict[str, Any]:
    return {"handler": "reverse_proxy", "upstreams": [{"dial": UPSTREAM}]}


def caddy_config(domain: str | None, email: str | None = None) -> dict[str, Any]:
    """La configuration complete de Caddy : HTTP seul, ou HTTPS pour un domaine."""
    admin = {"listen": ADMIN_LISTEN, "origins": list(ADMIN_ORIGINS)}
    if not domain:
        return {
            "admin": admin,
            "apps": {
                "http": {
                    "servers": {
                        "freeqos_http": {
                            "listen": [":80"],
                            "routes": [{"handle": [_proxy()]}],
                            "automatic_https": {"disable": True},
                        }
                    }
                }
            },
        }
    emetteur: dict[str, Any] = {"module": "acme"}
    if email:
        emetteur["email"] = email
    return {
        "admin": admin,
        "apps": {
            "http": {
                "servers": {
                    "freeqos_https": {
                        "listen": [":443"],
                        "routes": [
                            {"match": [{"host": [domain]}], "handle": [_proxy()], "terminal": True}
                        ],
                    },
                    "freeqos_http": {
                        "listen": [":80"],
                        "routes": [
                            {
                                "match": [{"host": [domain]}],
                                "handle": [
                                    {
                                        "handler": "static_response",
                                        "status_code": 308,
                                        "headers": {
                                            "Location": [
                                                "https://{http.request.host}{http.request.uri}"
                                            ]
                                        },
                                    }
                                ],
                                "terminal": True,
                            },
                            # L'adresse IP reste servie en HTTP : l'acces de secours.
                            {"handle": [_proxy()]},
                        ],
                        "automatic_https": {"disable_redirects": True},
                    },
                }
            },
            "tls": {"automation": {"policies": [{"subjects": [domain], "issuers": [emetteur]}]}},
        },
    }


async def push(admin_url: str, config: dict[str, Any], *, timeout_s: float = 10.0) -> None:
    """Remplace la configuration de Caddy. Leve une erreur dite en clair."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            reponse = await client.post(f"{admin_url.rstrip('/')}/load", json=config)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "the HTTPS proxy does not answer: is the 'proxy' container running "
            "(docker compose ps)? With an install without it, set the domain in your own "
            f"reverse proxy ({type(exc).__name__})"
        ) from exc
    if reponse.status_code >= 400:
        raise RuntimeError(f"the HTTPS proxy refused the configuration: {reponse.text[:300]}")


async def proxy_reachable(admin_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            return (await client.get(f"{admin_url.rstrip('/')}/config/")).status_code < 400
    except httpx.HTTPError:
        return False


async def resolve(domain: str) -> list[str]:
    """Les adresses IPv4 que le DNS donne pour ce domaine (vide = pas encore)."""
    boucle = asyncio.get_running_loop()
    try:
        infos = await boucle.getaddrinfo(
            domain, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        return []
    return sorted({str(i[4][0]) for i in infos})


async def https_ok(domain: str) -> tuple[bool, str]:
    """L'interface repond-elle en HTTPS, avec un certificat valide ?"""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            reponse = await client.get(f"https://{domain}/health")
        return reponse.status_code < 500, f"HTTPS answers ({reponse.status_code})"
    except httpx.ConnectError as exc:
        texte = str(exc)
        if "CERTIFICATE" in texte.upper():
            return False, "the certificate is not ready yet (it can take a minute)"
        return False, "no answer on port 443: is it open on the server's firewall?"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}"


class PublicAccessRepository:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT domain, email, updated_by, updated_at, applied_at, last_error "
                "FROM public_access WHERE id = 1"
            )
        return dict(row) if row else None

    async def save(self, domain: str, email: str | None, actor: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public_access (id, domain, email, updated_by) VALUES (1, $1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET domain = EXCLUDED.domain, email = EXCLUDED.email,
                    updated_by = EXCLUDED.updated_by, updated_at = now(),
                    applied_at = NULL, last_error = NULL
                """,
                domain,
                email,
                actor,
            )

    async def mark(self, *, ok: bool, error: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            if ok:
                await conn.execute(
                    "UPDATE public_access SET applied_at = now(), last_error = NULL WHERE id = 1"
                )
            else:
                await conn.execute(
                    "UPDATE public_access SET last_error = $1 WHERE id = 1", (error or "")[:500]
                )

    async def delete(self) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM public_access WHERE id = 1")
        return not resultat.endswith(" 0")


async def reapply_at_startup(repo: PublicAccessRepository, admin_url: str | None) -> None:
    """Au demarrage : repousse la config du domaine (le proxy a pu etre recree).

    Le proxy demarre parfois apres l'application : quelques essais espaces.
    Sans domaine enregistre, rien a faire (Caddy garde sa config HTTP).
    """
    if not admin_url:
        return
    try:
        fiche = await repo.get()
    except Exception:  # noqa: BLE001
        return
    if not fiche:
        return
    for attente in (0, 3, 10, 30):
        await asyncio.sleep(attente)
        try:
            await push(admin_url, caddy_config(fiche["domain"], fiche.get("email")))
            await repo.mark(ok=True)
            logger.info("Domaine %s applique au proxy HTTPS", fiche["domain"])
            return
        except Exception as exc:  # noqa: BLE001
            dernier = str(exc)
    logger.warning("Domaine %s non applique au proxy : %s", fiche["domain"], dernier)
    await repo.mark(ok=False, error=dernier)

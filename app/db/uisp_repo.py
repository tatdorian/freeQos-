"""Le controleur Ubiquiti UISP saisi depuis l'interface, et les antennes qu'on
rattache a un site.

Le jeton d'API est CHIFFRE en base (meme coffre que les mots de passe des
routeurs) et ne ressort jamais par l'API : on dit seulement qu'il existe.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import asyncpg

from app.services.crypto import SecretBox

logger = logging.getLogger(__name__)


class InvalidUispError(ValueError):
    pass


def check_base_url(value: str) -> str:
    """``https://uisp.example.net`` : schema et hote, sans le chemin d'API."""
    url = str(value or "").strip().rstrip("/")
    if url and "://" not in url:
        # « uisp.example.net » suffit : UISP se sert en HTTPS.
        url = "https://" + url
    for suffixe in ("/nms/api/v2.1", "/nms"):
        if url.endswith(suffixe):
            url = url[: -len(suffixe)]
    partie = urlparse(url)
    if partie.scheme not in ("http", "https") or not partie.netloc:
        raise InvalidUispError("give the UISP address, e.g. uisp.example.net")
    return url


class UispRepository:
    def __init__(self, pool: asyncpg.Pool, secrets: SecretBox) -> None:
        self._pool = pool
        self._secrets = secrets

    async def config(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT base_url, verify_tls, updated_by, updated_at, last_ok_at, last_error, "
                "device_count FROM uisp_controllers WHERE id = 1"
            )
        return dict(row) if row else None

    async def credentials(self) -> tuple[str, str, bool] | None:
        """(adresse, jeton en clair, verification TLS), ou None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT base_url, token_enc, verify_tls FROM uisp_controllers WHERE id = 1"
            )
        if row is None:
            return None
        try:
            jeton = self._secrets.decrypt(row["token_enc"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Jeton UISP illisible : %s", exc)
            return None
        return str(row["base_url"]), jeton, bool(row["verify_tls"])

    async def save(
        self, *, base_url: str, token: str | None, verify_tls: bool, actor: str | None
    ) -> dict[str, Any]:
        url = check_base_url(base_url)
        if not self._secrets.available:
            raise InvalidUispError(self._secrets.unavailable_reason or "encryption unavailable")
        async with self._pool.acquire() as conn:
            existe = await conn.fetchval("SELECT token_enc FROM uisp_controllers WHERE id = 1")
            if not token and not existe:
                raise InvalidUispError("give the UISP API token")
            jeton = self._secrets.encrypt(token) if token else existe
            await conn.execute(
                """
                INSERT INTO uisp_controllers (id, base_url, token_enc, verify_tls, updated_by)
                VALUES (1, $1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE SET base_url = EXCLUDED.base_url,
                    token_enc = EXCLUDED.token_enc, verify_tls = EXCLUDED.verify_tls,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                url,
                jeton,
                verify_tls,
                actor,
            )
        return await self.config() or {}

    async def delete(self) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM uisp_controllers WHERE id = 1")
        return not resultat.endswith(" 0")

    async def mark(self, *, ok: bool, error: str | None = None, count: int | None = None) -> None:
        async with self._pool.acquire() as conn:
            if ok:
                await conn.execute(
                    "UPDATE uisp_controllers SET last_ok_at = now(), last_error = NULL, "
                    "device_count = $1 WHERE id = 1",
                    count,
                )
            else:
                await conn.execute(
                    "UPDATE uisp_controllers SET last_error = $1 WHERE id = 1", (error or "")[:500]
                )

    async def links(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT device_id, name, pop_name, updated_by, updated_at FROM uisp_links "
                "ORDER BY lower(name)"
            )
        return [dict(r) for r in rows]

    async def set_link(
        self, device_id: str, *, name: str, pop_name: str, actor: str | None
    ) -> dict[str, Any]:
        if not device_id.strip() or not name.strip() or not pop_name.strip():
            raise InvalidUispError("a linked antenna needs its id, a name and a site")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO uisp_links (device_id, name, pop_name, updated_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (device_id) DO UPDATE SET name = EXCLUDED.name,
                    pop_name = EXCLUDED.pop_name, updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                RETURNING device_id, name, pop_name, updated_by, updated_at
                """,
                device_id.strip(),
                name.strip(),
                pop_name.strip(),
                actor,
            )
        return dict(row)

    async def delete_link(self, device_id: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM uisp_links WHERE device_id = $1", device_id)
        return not resultat.endswith(" 0")

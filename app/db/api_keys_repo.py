"""Depot des cles d'API.

Il ne rend JAMAIS un secret : les lignes sorties d'ici portent un prefixe, un
nom, des portees et des dates. Le secret n'existe qu'une fois, dans la reponse
de creation, et n'est plus jamais relisible -- y compris par l'exploitant qui
l'a creee. Une cle perdue se remplace, elle ne se retrouve pas.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.services.api_keys import (
    GeneratedKey,
    InvalidApiKeyError,
    extract_prefix,
    generate_key,
    matches,
    normalise_scopes,
)

logger = logging.getLogger(__name__)

COLUMNS = """
    id, name, prefix, scopes, enabled, note, created_by,
    expires_at, last_used_at, created_at, updated_at
"""


class ApiKeyNotFoundError(LookupError):
    pass


def _row(record: asyncpg.Record) -> dict[str, Any]:
    data = dict(record)
    data["scopes"] = list(data.get("scopes") or [])
    return data


class ApiKeysRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM api_keys ORDER BY created_at DESC"  # noqa: S608
            )
        return [_row(row) for row in rows]

    async def create(
        self,
        *,
        name: str,
        scopes: list[str] | None = None,
        note: str | None = None,
        created_by: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Cree une cle et rend (fiche, secret en clair).

        Le secret est le SEUL moment ou il circule. L'appelant le met dans sa
        reponse HTTP et l'oublie.
        """
        tiree: GeneratedKey = generate_key()
        retenues = normalise_scopes(scopes)
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                f"""
                INSERT INTO api_keys (name, prefix, key_hash, scopes, note,
                                      created_by, expires_at)
                VALUES ($1, $2, $3, $4::text[], $5, $6, $7)
                RETURNING {COLUMNS}
                """,  # noqa: S608
                name.strip(),
                tiree.prefix,
                tiree.key_hash,
                retenues,
                note,
                created_by,
                expires_at,
            )
        return _row(record), tiree.secret

    async def set_enabled(self, key_id: int, *, enabled: bool) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                f"""
                UPDATE api_keys SET enabled = $2, updated_at = now()
                WHERE id = $1 RETURNING {COLUMNS}
                """,  # noqa: S608
                key_id,
                enabled,
            )
        if record is None:
            raise ApiKeyNotFoundError(f"cle {key_id} inconnue")
        return _row(record)

    async def delete(self, key_id: int) -> None:
        async with self._pool.acquire() as conn:
            supprimees = await conn.execute("DELETE FROM api_keys WHERE id = $1", key_id)
        if supprimees.endswith(" 0"):
            raise ApiKeyNotFoundError(f"cle {key_id} inconnue")

    async def authenticate(self, secret: str) -> dict[str, Any] | None:
        """Rend la fiche de la cle presentee, ou None.

        None couvre TOUS les refus -- cle inconnue, empreinte fausse, cle
        desactivee, cle expiree -- volontairement. Distinguer "cette cle
        n'existe pas" de "cette cle est desactivee" dans une reponse HTTP
        renseigne gratuitement celui qui essaie des cles au hasard. Le journal,
        lui, dit ce qui s'est passe.
        """
        try:
            prefix = extract_prefix(secret)
        except InvalidApiKeyError:
            return None
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                f"SELECT {COLUMNS}, key_hash FROM api_keys WHERE prefix = $1",  # noqa: S608
                prefix,
            )
            if record is None or not matches(secret, record["key_hash"]):
                return None
            fiche = _row(record)
            fiche.pop("key_hash", None)
            if not fiche["enabled"]:
                logger.warning("Cle d'API %s refusee : desactivee", prefix)
                return None
            echeance = fiche.get("expires_at")
            if echeance is not None and echeance <= datetime.now(tz=UTC):
                logger.warning("Cle d'API %s refusee : expiree le %s", prefix, echeance)
                return None
            # Trace d'usage, volontairement sans await bloquant l'appel suivant :
            # une cle qui sert doit se voir dans l'interface, sinon personne ne
            # saura jamais laquelle retirer sans risque.
            await conn.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE id = $1", fiche["id"]
            )
        return fiche

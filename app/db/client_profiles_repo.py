"""Fiches saisies des clients (nom, contact, MAC du CPE, note).

A cote de la decouverte, jamais a sa place : la decouverte ecrit dans
``subscribers`` (adresse, site, derniere session), la saisie ici. Aucune des
deux n'efface l'autre.
"""

from __future__ import annotations

from typing import Any

import asyncpg

FIELDS: tuple[str, ...] = (
    "display_name",
    "cpe_mac",
    "phone",
    "email",
    "postal_address",
    "note",
)
_COLONNES = ", ".join((*FIELDS, "declared", "updated_by", "updated_at"))


class ClientProfilesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get(self, login: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT login, {_COLONNES} FROM client_profiles WHERE login = $1",  # noqa: S608
                login,
            )
        return dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT login, {_COLONNES} FROM client_profiles ORDER BY login"  # noqa: S608
            )
        return [dict(r) for r in rows]

    async def save(
        self,
        login: str,
        fields: dict[str, str | None],
        *,
        updated_by: str,
        declared: bool | None = None,
    ) -> dict[str, Any]:
        valeurs = [fields.get(f) for f in FIELDS]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO client_profiles (login, {", ".join(FIELDS)}, declared, updated_by)
                VALUES ($1, {", ".join(f"${i + 2}" for i in range(len(FIELDS)))},
                        COALESCE(${len(FIELDS) + 2}, FALSE), ${len(FIELDS) + 3})
                ON CONFLICT (login) DO UPDATE SET
                    {", ".join(f"{f} = EXCLUDED.{f}" for f in FIELDS)},
                    declared = COALESCE(${len(FIELDS) + 2}, client_profiles.declared),
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                RETURNING login, {_COLONNES}
                """,  # noqa: S608 - noms de colonnes fixes, valeurs parametrees
                login,
                *valeurs,
                declared,
                updated_by,
            )
        return dict(row)

    async def delete(self, login: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM client_profiles WHERE login = $1", login)
        return str(resultat).endswith(" 1")

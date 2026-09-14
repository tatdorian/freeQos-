"""Persistance des reglages d'exploitation (table ``runtime_settings``).

La valeur est stockee en JSONB : c'est le seul moyen de distinguer un reglage
volontairement VIDE (JSON ``null`` -- "ne pose pas ce champ CAKE") d'un reglage
simplement non surcharge (pas de ligne du tout).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg


class SettingsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self) -> dict[str, Any]:
        """Toutes les valeurs posees en base, pretes a surcharger les defauts."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, value FROM runtime_settings")
        valeurs: dict[str, Any] = {}
        for row in rows:
            brut = row["value"]
            valeurs[row["name"]] = json.loads(brut) if isinstance(brut, str) else brut
        return valeurs

    async def set(
        self, name: str, value: Any, *, updated_by: str | None = None, reason: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_settings (name, value, updated_by, reason)
                VALUES ($1, $2::jsonb, $3, $4)
                ON CONFLICT (name) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_by = EXCLUDED.updated_by,
                    reason     = EXCLUDED.reason,
                    updated_at = now()
                """,
                name,
                json.dumps(value),
                updated_by,
                reason,
            )

    async def delete(self, name: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM runtime_settings WHERE name = $1", name)
        return not resultat.endswith(" 0")

    async def history(self) -> list[dict[str, Any]]:
        """Qui a change quoi, et quand : le journal des reglages en vigueur."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, value, updated_by, reason, updated_at
                  FROM runtime_settings
                 ORDER BY updated_at DESC
                """
            )
        lignes: list[dict[str, Any]] = []
        for row in rows:
            ligne = dict(row)
            brut = ligne["value"]
            ligne["value"] = json.loads(brut) if isinstance(brut, str) else brut
            lignes.append(ligne)
        return lignes

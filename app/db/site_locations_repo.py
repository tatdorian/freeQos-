"""Position des sites sur la carte.

Une ligne par site, par son NOM (celui que portent les clients) : latitude,
longitude, qui l'a posee. Rien d'autre : ce que la carte montre de chaque site
(clients, connectes, alertes) se calcule a l'affichage.
"""

from __future__ import annotations

from typing import Any

import asyncpg


class InvalidLocationError(ValueError):
    pass


def check_location(latitude: Any, longitude: Any) -> tuple[float, float]:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise InvalidLocationError("latitude and longitude must be numbers") from exc
    if not -90 <= lat <= 90:
        raise InvalidLocationError("latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise InvalidLocationError("longitude must be between -180 and 180")
    return round(lat, 6), round(lon, 6)


class SiteLocationsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, latitude, longitude, updated_by, updated_at "
                "FROM site_locations ORDER BY lower(name)"
            )
        return [dict(r) for r in rows]

    async def put(
        self, name: str, latitude: float, longitude: float, *, actor: str | None
    ) -> dict[str, Any]:
        nom = name.strip()
        if not nom:
            raise InvalidLocationError("a site needs a name")
        lat, lon = check_location(latitude, longitude)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO site_locations (name, latitude, longitude, updated_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE
                   SET latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                       updated_by = EXCLUDED.updated_by, updated_at = now()
                RETURNING name, latitude, longitude, updated_by, updated_at
                """,
                nom,
                lat,
                lon,
                actor,
            )
        return dict(row)

    async def delete(self, name: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM site_locations WHERE name = $1", name)
        return not resultat.endswith(" 0")

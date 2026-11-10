"""Position des sites et des clients sur la carte.

Une ligne par site (son NOM, celui que portent les clients) ou par client (son
login) : latitude, longitude, qui l'a posee. Rien d'autre : ce que la carte
montre (clients, connectes, trafic) se calcule a l'affichage.
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


#: Les tables de positions : une liste blanche, le nom entre dans le SQL.
TABLES = ("site_locations", "client_locations")


class SiteLocationsRepository:
    def __init__(self, pool: asyncpg.Pool, table: str = "site_locations") -> None:
        if table not in TABLES:
            raise ValueError(f"unknown location table {table!r}")
        self._pool = pool
        self._table = table

    async def list(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, latitude, longitude, updated_by, updated_at "
                f"FROM {self._table} ORDER BY lower(name)"  # noqa: S608
            )
        return [dict(r) for r in rows]

    async def put(
        self, name: str, latitude: float, longitude: float, *, actor: str | None
    ) -> dict[str, Any]:
        nom = name.strip()
        if not nom:
            raise InvalidLocationError("a location needs a name")
        lat, lon = check_location(latitude, longitude)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._table} (name, latitude, longitude, updated_by)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE
                   SET latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                       updated_by = EXCLUDED.updated_by, updated_at = now()
                RETURNING name, latitude, longitude, updated_by, updated_at
                """,  # noqa: S608 - table issue de la liste blanche
                nom,
                lat,
                lon,
                actor,
            )
        return dict(row)

    async def delete(self, name: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                f"DELETE FROM {self._table} WHERE name = $1",  # noqa: S608
                name,
            )
        return not resultat.endswith(" 0")

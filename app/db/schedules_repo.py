"""Profils de debit horaires, tels qu'ils sont SAISIS.

Ce depot range des profils deja valides (``app.enforcement.schedules``) et les
rend. Il ne sait rien de l'heure qu'il est ni des routeurs : decider quelle
plage court, et pousser les files en consequence, se fait ailleurs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from app.enforcement.schedules import BandwidthSchedule, InvalidScheduleError

logger = logging.getLogger(__name__)

COLUMNS = "id, name, enabled, timezone, scope, targets, windows, note, created_at, updated_at"

CHAMPS = ("name", "enabled", "timezone", "scope", "targets", "windows", "note")
CHAMPS_JSON = ("targets", "windows")


class ScheduleNotFoundError(LookupError):
    pass


class ScheduleConflictError(ValueError):
    """Un profil porte deja ce nom."""


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for champ in CHAMPS_JSON:
        valeur = row.get(champ)
        if isinstance(valeur, str):
            try:
                row[champ] = json.loads(valeur)
            except ValueError:
                row[champ] = []
        elif valeur is None:
            row[champ] = []
    return row


def to_model(row: dict[str, Any]) -> BandwidthSchedule:
    """Une ligne -> un profil, revalide : la base n'est pas une source de confiance
    absolue (saisie SQL a la main, version anterieure)."""
    return BandwidthSchedule.build(
        id=int(row["id"]),
        name=str(row["name"]),
        windows=list(row.get("windows") or []),
        scope=str(row.get("scope") or "all"),
        targets=list(row.get("targets") or []),
        timezone=str(row.get("timezone") or "UTC"),
        enabled=bool(row.get("enabled", True)),
    )


class SchedulesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM bandwidth_schedules ORDER BY id"  # noqa: S608
            )
        return [_decode(dict(row)) for row in rows]

    async def get(self, schedule_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM bandwidth_schedules WHERE id = $1",  # noqa: S608
                schedule_id,
            )
        if row is None:
            raise ScheduleNotFoundError(f"unknown bandwidth schedule {schedule_id}")
        return _decode(dict(row))

    async def load_enabled(self) -> list[BandwidthSchedule]:
        """Les profils a appliquer. Un profil illisible est ECARTE et journalise :
        il ne doit pas empecher les autres de s'appliquer."""
        profils: list[BandwidthSchedule] = []
        for row in await self.list_all():
            if not row.get("enabled", True):
                continue
            try:
                profils.append(to_model(row))
            except (InvalidScheduleError, KeyError, TypeError, ValueError) as exc:
                logger.error("Profil horaire '%s' illisible, ignore : %s", row.get("name"), exc)
        return profils

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO bandwidth_schedules
                           (name, enabled, timezone, scope, targets, windows, note)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
                    RETURNING {COLUMNS}
                    """,  # noqa: S608
                    payload["name"],
                    bool(payload.get("enabled", True)),
                    payload.get("timezone") or "UTC",
                    payload.get("scope") or "all",
                    json.dumps(list(payload.get("targets") or [])),
                    json.dumps(list(payload.get("windows") or [])),
                    payload.get("note"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ScheduleConflictError(
                    f"a bandwidth schedule named '{payload['name']}' already exists"
                ) from exc
        return _decode(dict(row))

    async def update(self, schedule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        champs = {k: v for k, v in payload.items() if k in CHAMPS}
        if not champs:
            return await self.get(schedule_id)
        valeurs = [json.dumps(list(v or [])) if k in CHAMPS_JSON else v for k, v in champs.items()]
        affectations = ", ".join(
            f"{nom} = ${i + 2}" + ("::jsonb" if nom in CHAMPS_JSON else "")
            for i, nom in enumerate(champs)
        )
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE bandwidth_schedules SET {affectations}, updated_at = now()
                     WHERE id = $1
                    RETURNING {COLUMNS}
                    """,  # noqa: S608 - noms de colonnes issus d'une liste blanche
                    schedule_id,
                    *valeurs,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ScheduleConflictError(
                    f"a bandwidth schedule named '{payload.get('name')}' already exists"
                ) from exc
        if row is None:
            raise ScheduleNotFoundError(f"unknown bandwidth schedule {schedule_id}")
        return _decode(dict(row))

    async def delete(self, schedule_id: int) -> None:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM bandwidth_schedules WHERE id = $1", schedule_id
            )
        if resultat.endswith(" 0"):
            raise ScheduleNotFoundError(f"unknown bandwidth schedule {schedule_id}")

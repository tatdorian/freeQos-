"""Modes de debit : des debits nommes qu'on donne a un client d'un geste.

Un exploitant au telephone avec un client ne saisit pas "6 Mbps descendant,
6 Mbps montant" : il choisit "Backup" dans une liste. Ce depot range cette
liste. Le mode EN COURS d'un client, lui, vit dans sa politique de shaping
(``shaping_policies.mode_id``) : c'est elle que le planificateur lit deja.
"""

from __future__ import annotations

from typing import Any

import asyncpg

COLUMNS = (
    "id, name, rate_kind, down_value, up_value, disables_boost, position, note, "
    "created_at, updated_at"
)
CHAMPS = ("name", "rate_kind", "down_value", "up_value", "disables_boost", "position", "note")

RATE_FIXED = "fixed"
RATE_PERCENT = "percent"


class ModeNotFoundError(LookupError):
    pass


class ModeConflictError(ValueError):
    """Un mode porte deja ce nom, ou il est encore donne a des clients."""


class InvalidModeError(ValueError):
    pass


def mode_rates(
    mode: dict[str, Any], plan_down_mbps: float | None, plan_up_mbps: float | None
) -> tuple[float, float]:
    """Debit (descendant, montant) en Mbps que ce mode donne a ce client.

    Un mode en pourcentage a besoin du forfait du client : sans forfait connu,
    "50 % du forfait" ne veut rien dire, et inventer un debit serait pire.
    """
    bas, haut = float(mode["down_value"]), float(mode["up_value"])
    if mode.get("rate_kind") != RATE_PERCENT:
        return bas, haut
    if not plan_down_mbps or not plan_up_mbps:
        raise InvalidModeError(
            f"'{mode['name']}' is a share of the plan, and this client has no known plan: "
            "give it a plan first, or choose a mode with a fixed rate"
        )
    return round(plan_down_mbps * bas / 100, 3), round(plan_up_mbps * haut / 100, 3)


def check_mode(payload: dict[str, Any]) -> dict[str, Any]:
    nom = str(payload.get("name") or "").strip()
    if not nom:
        raise InvalidModeError("a mode needs a name")
    if nom.lower() == "automatic":
        raise InvalidModeError("'Automatic' is built in: it means no mode, plan and schedule")
    genre = payload.get("rate_kind") or RATE_FIXED
    if genre not in (RATE_FIXED, RATE_PERCENT):
        raise InvalidModeError("rate_kind must be 'fixed' (Mbps) or 'percent' (of the plan)")
    for champ in ("down_value", "up_value"):
        valeur = payload.get(champ)
        if valeur is None or float(valeur) <= 0:
            raise InvalidModeError(f"{champ} must be above 0")
        if genre == RATE_PERCENT and float(valeur) > 1000:
            raise InvalidModeError(f"{champ}: a share of the plan above 1000 % is a typo")
    return {**payload, "name": nom, "rate_kind": genre}


class ModesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM bandwidth_modes ORDER BY position, lower(name)"  # noqa: S608
            )
        return [dict(r) for r in rows]

    async def get(self, mode_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM bandwidth_modes WHERE id = $1",  # noqa: S608
                mode_id,
            )
        if row is None:
            raise ModeNotFoundError(f"no bandwidth mode {mode_id}")
        return dict(row)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        fiche = check_mode(payload)
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO bandwidth_modes
                           (name, rate_kind, down_value, up_value, disables_boost, position, note)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING {COLUMNS}
                    """,  # noqa: S608
                    fiche["name"],
                    fiche["rate_kind"],
                    float(fiche["down_value"]),
                    float(fiche["up_value"]),
                    bool(fiche.get("disables_boost", False)),
                    int(fiche.get("position") or 0),
                    fiche.get("note"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModeConflictError(f"a mode named '{fiche['name']}' already exists") from exc
        return dict(row)

    async def update(self, mode_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        actuel = await self.get(mode_id)
        champs = {k: v for k, v in payload.items() if k in CHAMPS and v is not None}
        fiche = check_mode({**actuel, **champs})
        affectations = ", ".join(f"{nom} = ${i + 2}" for i, nom in enumerate(CHAMPS))
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE bandwidth_modes SET {affectations}, updated_at = now()
                     WHERE id = $1 RETURNING {COLUMNS}
                    """,  # noqa: S608 - noms de colonnes issus d'une liste blanche
                    mode_id,
                    fiche["name"],
                    fiche["rate_kind"],
                    float(fiche["down_value"]),
                    float(fiche["up_value"]),
                    bool(fiche.get("disables_boost")),
                    int(fiche.get("position") or 0),
                    fiche.get("note"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModeConflictError(f"a mode named '{fiche['name']}' already exists") from exc
        if row is None:
            raise ModeNotFoundError(f"no bandwidth mode {mode_id}")
        return dict(row)

    async def delete(self, mode_id: int) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                en_cours = await conn.fetchval(
                    "SELECT count(*) FROM shaping_policies WHERE mode_id = $1", mode_id
                )
                if en_cours:
                    raise ModeConflictError(
                        f"{en_cours} client(s) are in this mode: put them back to "
                        "Automatic or in another mode first"
                    )
                resultat = await conn.execute("DELETE FROM bandwidth_modes WHERE id = $1", mode_id)
        if resultat.endswith(" 0"):
            raise ModeNotFoundError(f"no bandwidth mode {mode_id}")

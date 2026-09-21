"""Les restrictions de trafic, telles qu'elles sont SAISIES.

Ce depot ne connait ni RouterOS, ni catalogue de services, ni adresse
decouverte : il range une intention ("bloquer Netflix pour ces trois clients")
et la rend. Tout ce qui transforme cette intention en commandes vit dans
``enforcement/restrictions.py`` et ``services/restrictions.py``.

POURQUOI CETTE SEPARATION TIENT. Une regle n'est PAS une liste d'adresses. Elle
est un critere, et l'ensemble d'adresses qu'il designe change tout seul a mesure
que NetFlow decouvre de nouveaux serveurs. Figer les adresses ici reviendrait a
transformer une regle vivante en photo, et a obliger un humain a la reecrire
chaque fois qu'un fournisseur ajoute un serveur.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

COLUMNS = """
    id, name, action, limit_down_mbps, limit_up_mbps, services, categories,
    prefixes, protocol, ports, scope, logins, routers, enabled, note,
    last_applied_at, last_state, last_detail, created_at, updated_at
"""

#: Champs modifiables. ``name`` en fait partie : renommer une regle est legitime,
#: et le nom RouterOS derive de l'IDENTIFIANT, pas du libelle -- renommer ne
#: laisse donc pas d'orphelin sur les routeurs.
CHAMPS = (
    "name",
    "action",
    "limit_down_mbps",
    "limit_up_mbps",
    "services",
    "categories",
    "prefixes",
    "protocol",
    "ports",
    "scope",
    "logins",
    "routers",
    "enabled",
    "note",
)

CHAMPS_JSON = ("services", "categories", "prefixes", "logins", "routers")


class RuleNotFoundError(LookupError):
    pass


class RuleConflictError(ValueError):
    """Une regle porte deja ce nom."""


class TrafficRulesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {COLUMNS} FROM traffic_rules
                WHERE ($1::boolean IS NOT TRUE OR enabled)
                ORDER BY id
                """,  # noqa: S608
                enabled_only,
            )
        return [_decode(dict(row)) for row in rows]

    async def get(self, rule_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM traffic_rules WHERE id = $1",  # noqa: S608
                rule_id,
            )
        if row is None:
            raise RuleNotFoundError(f"regle {rule_id} inconnue")
        return _decode(dict(row))

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        valeurs = _encode(payload)
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO traffic_rules (name, action, limit_down_mbps, limit_up_mbps,
                                               services, categories, prefixes, protocol,
                                               ports, scope, logins, routers, enabled, note)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9, $10,
                            $11::jsonb, $12::jsonb, $13, $14)
                    RETURNING {COLUMNS}
                    """,  # noqa: S608
                    valeurs.get("name"),
                    valeurs.get("action", "block"),
                    valeurs.get("limit_down_mbps"),
                    valeurs.get("limit_up_mbps"),
                    valeurs.get("services", "[]"),
                    valeurs.get("categories", "[]"),
                    valeurs.get("prefixes", "[]"),
                    valeurs.get("protocol"),
                    valeurs.get("ports"),
                    valeurs.get("scope", "all"),
                    valeurs.get("logins", "[]"),
                    valeurs.get("routers", "[]"),
                    bool(valeurs.get("enabled", True)),
                    valeurs.get("note"),
                )
        except asyncpg.UniqueViolationError as exc:
            raise RuleConflictError(f"une regle s'appelle deja '{payload.get('name')}'") from exc
        return _decode(dict(row))

    async def update(self, rule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        champs = {k: v for k, v in _encode(payload).items() if k in CHAMPS}
        if not champs:
            return await self.get(rule_id)
        # Les colonnes JSONB doivent etre castees explicitement : asyncpg envoie
        # une chaine, et PostgreSQL refuse d'affecter un text a un jsonb.
        morceaux = [
            f"{nom} = ${i + 2}::jsonb" if nom in CHAMPS_JSON else f"{nom} = ${i + 2}"
            for i, nom in enumerate(champs)
        ]
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    UPDATE traffic_rules SET {", ".join(morceaux)}, updated_at = now()
                    WHERE id = $1 RETURNING {COLUMNS}
                    """,  # noqa: S608
                    rule_id,
                    *champs.values(),
                )
        except asyncpg.UniqueViolationError as exc:
            raise RuleConflictError(f"une regle s'appelle deja '{payload.get('name')}'") from exc
        if row is None:
            raise RuleNotFoundError(f"regle {rule_id} inconnue")
        return _decode(dict(row))

    async def delete(self, rule_id: int) -> None:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute("DELETE FROM traffic_rules WHERE id = $1", rule_id)
        if resultat.endswith(" 0"):
            raise RuleNotFoundError(f"regle {rule_id} inconnue")

    async def record_apply(self, rule_id: int, *, state: str, detail: str) -> None:
        """Garde la trace du dernier passage sur les routeurs.

        Sans cette trace, une regle enregistree et une regle REELLEMENT POSEE se
        ressemblent trait pour trait dans l'interface -- et c'est la confusion la
        plus couteuse du produit : croire qu'un trafic est bloque alors que
        l'enforcement etait coupe.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE traffic_rules
                   SET last_applied_at = now(), last_state = $2, last_detail = $3
                 WHERE id = $1
                """,
                rule_id,
                state,
                detail[:2000],
            )


def _encode(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialise les champs JSONB. Le reste passe tel quel."""
    sortie = dict(payload)
    for champ in CHAMPS_JSON:
        if champ in sortie:
            valeur = sortie[champ]
            sortie[champ] = json.dumps(list(valeur) if valeur is not None else [])
    return sortie


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for champ in CHAMPS_JSON:
        row[champ] = _as_list(row.get(champ))
    return row


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            charge = json.loads(value)
        except ValueError:
            return []
        return charge if isinstance(charge, list) else []
    return []

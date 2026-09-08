"""Inventaire dynamique des routeurs, stocke en base.

Complete l'inventaire fichier : les routeurs ajoutes depuis l'interface vivent
ici, ceux du YAML restent pilotes par l'environnement. Les deux sources sont
fusionnees par le RouterRegistry, le fichier ayant priorite en cas d'homonymie.

Aucune methode ne renvoie jamais un mot de passe, meme chiffre, vers l'API :
``to_public`` filtre explicitement, et ``load_config`` est le seul chemin qui
dechiffre — pour construire un collecteur, jamais pour repondre a une requete.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.config import RouterConfig, RouterRole
from app.services.crypto import SecretBox

logger = logging.getLogger(__name__)

# Colonnes renvoyees a l'interface. password_enc en est volontairement absente.
PUBLIC_COLUMNS = """
    id, name, host, port, username, role, pop_name, enabled, use_ssl, timeout_s,
    pppoe_interface_pattern, last_ok_at, last_error, identity, board_name,
    routeros_version, created_at, updated_at
"""


class RouterNotFoundError(LookupError):
    pass


class DuplicateRouterError(ValueError):
    pass


class RoutersRepository:
    def __init__(self, pool: asyncpg.Pool, secrets: SecretBox) -> None:
        self._pool = pool
        self._secrets = secrets

    # ------------------------------------------------------------ lectures
    async def list_public(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {PUBLIC_COLUMNS} FROM routers ORDER BY name"  # noqa: S608
            )
        return [dict(row) for row in rows]

    async def get_public(self, router_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {PUBLIC_COLUMNS} FROM routers WHERE id = $1",  # noqa: S608
                router_id,
            )
        if row is None:
            raise RouterNotFoundError(f"routeur {router_id} inconnu")
        return dict(row)

    async def load_configs(self, *, enabled_only: bool = True) -> list[RouterConfig]:
        """Traduit les lignes en RouterConfig utilisables par le collecteur.

        Une ligne dont le secret est indechiffrable (cle changee, valeur en clair)
        est ecartee avec un message clair plutot que de faire echouer tout le
        chargement : les autres PoPs doivent continuer a tourner.
        """
        query = "SELECT * FROM routers"
        if enabled_only:
            query += " WHERE enabled"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY name")

        configs: list[RouterConfig] = []
        for row in rows:
            try:
                password = self._secrets.decrypt(row["password_enc"])
            except Exception as exc:  # noqa: BLE001
                logger.error("Routeur '%s' ignore : secret illisible (%s)", row["name"], exc)
                await self.record_failure(row["id"], f"secret illisible : {exc}")
                continue
            configs.append(
                RouterConfig(
                    name=row["name"],
                    host=row["host"],
                    port=row["port"],
                    username=row["username"],
                    password=password,
                    role=RouterRole(row["role"]),
                    pop_name=row["pop_name"],
                    enabled=row["enabled"],
                    use_ssl=row["use_ssl"],
                    timeout_s=row["timeout_s"],
                    pppoe_interface_pattern=row["pppoe_interface_pattern"],
                )
            )
        return configs

    # ------------------------------------------------------------ ecritures
    async def create(self, payload: dict[str, Any], password: str) -> dict[str, Any]:
        password_enc = self._secrets.encrypt(password)
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO routers (name, host, port, username, password_enc, role,
                                         pop_name, enabled, use_ssl, timeout_s,
                                         pppoe_interface_pattern)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING {PUBLIC_COLUMNS}
                    """,  # noqa: S608
                    payload["name"],
                    payload["host"],
                    payload.get("port", 8728),
                    payload.get("username", "qos-ro"),
                    password_enc,
                    payload.get("role", "pop"),
                    payload.get("pop_name"),
                    payload.get("enabled", True),
                    payload.get("use_ssl", False),
                    payload.get("timeout_s", 5.0),
                    payload.get("pppoe_interface_pattern", "<pppoe-{login}>"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateRouterError(
                    f"un routeur nomme '{payload['name']}' existe deja"
                ) from exc
        return dict(row)

    async def update(
        self, router_id: int, payload: dict[str, Any], password: str | None = None
    ) -> dict[str, Any]:
        """Mise a jour partielle. Le mot de passe n'est reecrit que s'il est fourni :
        modifier l'IP d'un routeur ne doit pas obliger a resaisir son secret."""
        fields = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "name",
                "host",
                "port",
                "username",
                "role",
                "pop_name",
                "enabled",
                "use_ssl",
                "timeout_s",
                "pppoe_interface_pattern",
            }
            and value is not None
        }
        if password:
            fields["password_enc"] = self._secrets.encrypt(password)
        if not fields:
            return await self.get_public(router_id)

        assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(fields))
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE routers SET {assignments}, updated_at = now()
                     WHERE id = $1
                    RETURNING {PUBLIC_COLUMNS}
                    """,  # noqa: S608 - noms de colonnes issus d'une liste blanche
                    router_id,
                    *fields.values(),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateRouterError("ce nom de routeur est deja pris") from exc
        if row is None:
            raise RouterNotFoundError(f"routeur {router_id} inconnu")
        return dict(row)

    async def delete(self, router_id: int) -> None:
        async with self._pool.acquire() as conn:
            deleted = await conn.execute("DELETE FROM routers WHERE id = $1", router_id)
        if deleted.endswith(" 0"):
            raise RouterNotFoundError(f"routeur {router_id} inconnu")

    # --------------------------------------------------------- diagnostics
    async def record_success(self, router_id: int, info: dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE routers
                   SET last_ok_at = now(), last_error = NULL, identity = $2,
                       board_name = $3, routeros_version = $4, updated_at = now()
                 WHERE id = $1
                """,
                router_id,
                info.get("identity"),
                info.get("board_name"),
                info.get("version"),
            )

    async def record_failure(self, router_id: int, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE routers SET last_error = $2, updated_at = now() WHERE id = $1",
                router_id,
                error[:1000],
            )

    async def find_id_by_name(self, name: str) -> int | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM routers WHERE name = $1", name)

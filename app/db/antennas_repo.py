"""Inventaire des antennes Ubiquiti, ajoutees depuis l'interface.

Meme role que ``routers_repo`` mais pour les radios interrogees directement sur
leur API airOS locale : declarer une antenne ici suffit a la collecter, sans
aucune variable d'environnement. Le mot de passe est chiffre au repos et ne
ressort jamais vers l'API — ``list_public`` l'exclut, ``load_targets`` est le
seul chemin qui dechiffre, pour construire le client, jamais pour repondre.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.collectors.uisp import AirOsTarget
from app.config import BackhaulConfig
from app.services.crypto import SecretBox

logger = logging.getLogger(__name__)

# password_enc en est volontairement absente : un secret ne remonte jamais.
PUBLIC_COLUMNS = """
    id, name, pop_name, host, username, verify_tls, device_key,
    nominal_capacity_mbps, enabled, timeout_s, last_ok_at, last_error,
    last_capacity_mbps, created_at, updated_at
"""


class AntennaNotFoundError(LookupError):
    pass


class DuplicateAntennaError(ValueError):
    pass


class AntennasRepository:
    def __init__(self, pool: asyncpg.Pool, secrets: SecretBox) -> None:
        self._pool = pool
        self._secrets = secrets

    # ------------------------------------------------------------ lectures
    async def list_public(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {PUBLIC_COLUMNS} FROM airos_antennas ORDER BY name"  # noqa: S608
            )
        return [dict(row) for row in rows]

    async def get_public(self, antenna_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {PUBLIC_COLUMNS} FROM airos_antennas WHERE id = $1",  # noqa: S608
                antenna_id,
            )
        if row is None:
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")
        return dict(row)

    def _device_key(self, row: dict[str, Any]) -> str:
        return str(row.get("device_key") or row["name"])

    async def load_targets(self, *, enabled_only: bool = True) -> list[AirOsTarget]:
        """Cibles utilisables par le provider airOS. Dechiffre les secrets.

        Une antenne au secret indechiffrable (cle changee) est ecartee avec un
        message clair plutot que de faire tomber toute la collecte.
        """
        query = "SELECT * FROM airos_antennas"
        if enabled_only:
            query += " WHERE enabled"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY name")

        targets: list[AirOsTarget] = []
        for row in rows:
            password = ""
            if row["password_enc"]:
                try:
                    password = self._secrets.decrypt(row["password_enc"])
                except Exception as exc:  # noqa: BLE001
                    logger.error("Antenne '%s' ignoree : secret illisible (%s)", row["name"], exc)
                    await self.record_failure(row["id"], f"secret illisible : {exc}")
                    continue
            targets.append(
                AirOsTarget(
                    key=self._device_key(row),
                    host=row["host"],
                    username=row["username"] or "",
                    password=password,
                    verify_tls=row["verify_tls"],
                )
            )
        return targets

    async def backhaul_configs(self, *, enabled_only: bool = True) -> list[BackhaulConfig]:
        """Traduit chaque antenne en BackhaulConfig pour la collecte.

        C'est ce qui rattache la capacite lue a un PoP et fixe sa capacite
        nominale, exactement comme un backhaul declare dans le fichier.
        """
        query = "SELECT * FROM airos_antennas"
        if enabled_only:
            query += " WHERE enabled"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY name")
        return [
            BackhaulConfig(
                name=row["name"],
                pop_name=row["pop_name"],
                uisp_device_id=self._device_key(dict(row)),
                nominal_capacity_mbps=row["nominal_capacity_mbps"],
                enabled=row["enabled"],
                api_host=row["host"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------ ecritures
    async def create(self, payload: dict[str, Any], password: str | None) -> dict[str, Any]:
        password_enc = self._secrets.encrypt(password) if password else None
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO airos_antennas (name, pop_name, host, username,
                                                password_enc, verify_tls, device_key,
                                                nominal_capacity_mbps, enabled, timeout_s)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING {PUBLIC_COLUMNS}
                    """,  # noqa: S608
                    payload["name"],
                    payload["pop_name"],
                    payload["host"],
                    payload.get("username", "ubnt"),
                    password_enc,
                    payload.get("verify_tls", False),
                    payload.get("device_key"),
                    payload.get("nominal_capacity_mbps"),
                    payload.get("enabled", True),
                    payload.get("timeout_s", 10.0),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateAntennaError(
                    f"une antenne nommee '{payload['name']}' existe deja"
                ) from exc
        return dict(row)

    async def update(
        self, antenna_id: int, payload: dict[str, Any], password: str | None = None
    ) -> dict[str, Any]:
        """Mise a jour partielle. Le mot de passe n'est reecrit que s'il est fourni."""
        fields = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "name",
                "pop_name",
                "host",
                "username",
                "verify_tls",
                "device_key",
                "nominal_capacity_mbps",
                "enabled",
                "timeout_s",
            }
            and value is not None
        }
        if password:
            fields["password_enc"] = self._secrets.encrypt(password)
        if not fields:
            return await self.get_public(antenna_id)

        assignments = ", ".join(f"{name} = ${i + 2}" for i, name in enumerate(fields))
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE airos_antennas SET {assignments}, updated_at = now()
                     WHERE id = $1
                    RETURNING {PUBLIC_COLUMNS}
                    """,  # noqa: S608 - noms issus d'une liste blanche
                    antenna_id,
                    *fields.values(),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateAntennaError("ce nom d'antenne est deja pris") from exc
        if row is None:
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")
        return dict(row)

    async def delete(self, antenna_id: int) -> None:
        async with self._pool.acquire() as conn:
            deleted = await conn.execute("DELETE FROM airos_antennas WHERE id = $1", antenna_id)
        if deleted.endswith(" 0"):
            raise AntennaNotFoundError(f"antenne {antenna_id} inconnue")

    # --------------------------------------------------------- diagnostics
    async def record_success(self, antenna_id: int, capacity_mbps: float | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE airos_antennas
                   SET last_ok_at = now(), last_error = NULL,
                       last_capacity_mbps = $2, updated_at = now()
                 WHERE id = $1
                """,
                antenna_id,
                capacity_mbps,
            )

    async def record_failure(self, antenna_id: int, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE airos_antennas SET last_error = $2, updated_at = now() WHERE id = $1",
                antenna_id,
                error[:1000],
            )

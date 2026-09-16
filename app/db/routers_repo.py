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
from pydantic import SecretStr

from app.config import RouterConfig, RouterRole
from app.services.crypto import SecretBox

logger = logging.getLogger(__name__)

# Colonnes renvoyees a l'interface. password_enc en est volontairement absente.
PUBLIC_COLUMNS = """
    id, name, host, port, username, role, pop_name, enabled, use_ssl,
    tls_verify, tls_fingerprint, host(loopback) AS loopback, timeout_s,
    pppoe_interface_pattern, last_ok_at, last_error, identity, board_name,
    routeros_version, created_at, updated_at
"""


class RouterNotFoundError(LookupError):
    pass


class DuplicateRouterError(ValueError):
    pass


def _ecarte(row: Any, motif: str) -> dict[str, str]:
    """Fiche minimale d'un routeur ecarte, de quoi l'AFFICHER.

    On garde hote, PoP et role : sans eux, l'interface ne pourrait montrer
    qu'un nom, et l'arbre ne pourrait pas poser de case a sa place.
    """
    return {
        "name": str(row["name"]),
        "reason": motif,
        "source": "db",
        "host": str(row["host"] or ""),
        "pop_name": str(row["pop_name"] or ""),
        "role": str(row["role"] or "pop"),
    }


def _motif_court(exc: Exception) -> str:
    """Premiere ligne utile d'une erreur de validation, pour l'affichage."""
    texte = str(exc).strip()
    for ligne in texte.splitlines():
        propre = ligne.strip()
        if propre and not propre.startswith("For further information"):
            return propre[:160]
    return texte[:160] or type(exc).__name__


def _conflit(exc: asyncpg.UniqueViolationError, payload: dict[str, Any]) -> DuplicateRouterError:
    """Traduit la contrainte violee en message utile.

    Deux unicites coexistent sur cette table -- le nom et le loopback -- et
    dire "ce nom existe deja" quand c'est le loopback qui collisionne envoie
    l'operateur chercher au mauvais endroit. Or une collision de loopback est
    precisement ce qu'il doit corriger : elle casserait l'identification des
    routeurs dans l'arbre.
    """
    contrainte = str(getattr(exc, "constraint_name", "") or "")
    if "loopback" in contrainte:
        return DuplicateRouterError(
            f"le loopback '{payload.get('loopback')}' est deja utilise par un autre "
            f"routeur. Il identifie un routeur et un seul : verifiez lequel des deux "
            f"est mal declare."
        )
    return DuplicateRouterError(f"un routeur nomme '{payload.get('name')}' existe deja")


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
        """Les routeurs exploitables. Les ecartes sont perdus : voir ci-dessous."""
        configs, _ = await self.load_configs_with_report(enabled_only=enabled_only)
        return configs

    async def load_configs_with_report(
        self, *, enabled_only: bool = True
    ) -> tuple[list[RouterConfig], list[dict[str, str]]]:
        """Traduit les lignes en RouterConfig, ET rend la liste des ecartes.

        POURQUOI CETTE VARIANTE EXISTE. Une ligne dont le secret est
        indechiffrable (cle changee, volume perdu) est ecartee plutot que de
        faire echouer tout le chargement : les autres PoPs doivent continuer a
        tourner. C'est la bonne decision -- mais tant que ce depot se contentait
        d'un log serveur, le routeur disparaissait de la collecte, de l'arbre et
        de la detection ARP sans qu'AUCUN ecran ne dise pourquoi.

        Le rapport porte donc de quoi le reconstituer a l'ecran : son nom, son
        motif, et ce qu'on sait de lui (hote, PoP, role). Un PoP ecarte doit
        rester visible en tant qu'ecarte, jamais s'evaporer.
        """
        query = "SELECT * FROM routers"
        if enabled_only:
            query += " WHERE enabled"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY name")

        configs: list[RouterConfig] = []
        ecartes: list[dict[str, str]] = []
        for row in rows:
            try:
                password = self._secrets.decrypt(row["password_enc"])
            except Exception as exc:  # noqa: BLE001
                logger.error("Routeur '%s' ignore : secret illisible (%s)", row["name"], exc)
                await self.record_failure(row["id"], f"secret illisible : {exc}")
                ecartes.append(_ecarte(row, f"secret illisible : {exc}"))
                continue
            try:
                configs.append(
                    RouterConfig(
                        name=row["name"],
                        host=row["host"],
                        port=row["port"],
                        username=row["username"],
                        password=SecretStr(password),
                        role=RouterRole(row["role"]),
                        pop_name=row["pop_name"],
                        enabled=row["enabled"],
                        use_ssl=row["use_ssl"],
                        tls_verify=row["tls_verify"],
                        tls_fingerprint=row["tls_fingerprint"],
                        # La colonne est INET : asyncpg rend un objet, le
                        # validateur de RouterConfig ramene la forme /32 a
                        # l'adresse nue.
                        loopback=(str(row["loopback"]) if row["loopback"] is not None else None),
                        timeout_s=row["timeout_s"],
                        pppoe_interface_pattern=row["pppoe_interface_pattern"],
                    )
                )
            except (ValueError, TypeError, KeyError) as exc:
                # UNE fiche invalide (role inconnu, loopback mal saisi) faisait
                # remonter l'exception jusqu'au registre, dont le garde-fou
                # large la rattrapait -- et TOUT l'inventaire en base
                # disparaissait a cause d'une seule ligne. On n'en perd plus
                # qu'une, et on dit laquelle.
                logger.error("Routeur '%s' ignore : fiche invalide (%s)", row["name"], exc)
                await self.record_failure(row["id"], f"fiche invalide : {exc}")
                ecartes.append(_ecarte(row, f"fiche invalide : {_motif_court(exc)}"))
        return configs, ecartes

    # ------------------------------------------------------------ ecritures
    async def create(self, payload: dict[str, Any], password: str) -> dict[str, Any]:
        password_enc = self._secrets.encrypt(password)
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO routers (name, host, port, username, password_enc, role,
                                         pop_name, enabled, use_ssl, tls_verify,
                                         tls_fingerprint, loopback, timeout_s,
                                         pppoe_interface_pattern)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::inet, $13, $14)
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
                    payload.get("tls_verify", "strict"),
                    payload.get("tls_fingerprint"),
                    payload.get("loopback"),
                    payload.get("timeout_s", 5.0),
                    payload.get("pppoe_interface_pattern", "<pppoe-{login}>"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise _conflit(exc, payload) from exc
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
                "tls_verify",
                "tls_fingerprint",
                "loopback",
                "timeout_s",
                "pppoe_interface_pattern",
            }
            and value is not None
        }
        if password:
            fields["password_enc"] = self._secrets.encrypt(password)
        if not fields:
            return await self.get_public(router_id)

        assignments = ", ".join(
            f"{name} = ${i + 2}" + ("::inet" if name == "loopback" else "")
            for i, name in enumerate(fields)
        )
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
                raise _conflit(exc, {**fields, "name": fields.get("name", "")}) from exc
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
            value = await conn.fetchval("SELECT id FROM routers WHERE name = $1", name)
        return None if value is None else int(value)

    # --------------------------------------------- routeurs fichier masques
    async def hidden_file_routers(self) -> set[str]:
        """Noms de routeurs fichier a ecarter de l'inventaire."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT name FROM hidden_file_routers")
        return {row["name"] for row in rows}

    async def list_hidden_file_routers(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, reason, updated_at FROM hidden_file_routers ORDER BY name"
            )
        return [dict(row) for row in rows]

    async def hide_file_router(self, name: str, reason: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO hidden_file_routers (name, reason)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE SET reason = EXCLUDED.reason, updated_at = now()
                """,
                name,
                reason,
            )

    async def unhide_file_router(self, name: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM hidden_file_routers WHERE name = $1", name)
        return not result.endswith(" 0")

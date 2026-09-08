"""Pool asyncpg, migration du schema et politiques Timescale.

Choix : asyncpg nu plutot que SQLAlchemy. On manipule des series temporelles
(ecritures par lots, lectures agregees via date_bin) et des objets propres a
Timescale (hypertables, compression, retention) que l'ORM ne modelise pas.
Le referentiel est assez simple pour tenir en SQL direct.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        command_timeout: float = 15.0,
        auto_create: bool = True,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._auto_create = auto_create
        self._pool: asyncpg.Pool | None = None
        self.timescale_available: bool = False
        self.database_created: bool = False

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Le pool de connexions n'est pas initialise")
        return self._pool

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self, *, retries: int = 10, delay_s: float = 2.0) -> None:
        """Ouvre le pool, avec quelques tentatives.

        Au demarrage de la stack, l'application peut etre prete avant que
        PostgreSQL n'accepte les connexions. Quelques tentatives espacees evitent
        un crash-loop pour une course de quelques secondes.
        """
        if self._pool is not None:
            return
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=self._command_timeout,
                )
                logger.info(
                    "Pool PostgreSQL ouvert (min=%s max=%s)", self._min_size, self._max_size
                )
                return
            except asyncpg.InvalidCatalogNameError as exc:
                # La base n'existe pas encore : premier demarrage hors
                # docker-compose. On la cree plutot que de renvoyer l'operateur
                # vers un createdb manuel.
                last_error = exc
                if not self._auto_create or not await self._create_database():
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == retries:
                    break
                logger.warning("Base injoignable (tentative %d/%d) : %s", attempt, retries, exc)
                await asyncio.sleep(delay_s)

        indice = self._connect_hint(last_error)
        raise RuntimeError(
            f"Connexion a PostgreSQL impossible : {last_error}." + (f" {indice}" if indice else "")
        ) from last_error

    async def _create_database(self) -> bool:
        """Cree la base nommee dans le DSN, en se connectant a 'postgres'.

        Echoue proprement si le compte n'a pas le droit CREATEDB : ce n'est pas
        une raison de masquer le probleme derriere une trace asyncpg.
        """
        nom = self._database_name()
        if not nom:
            return False
        dsn_admin = self._dsn.rsplit("/", 1)[0] + "/postgres"
        try:
            conn = await asyncpg.connect(dsn=dsn_admin, timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Base '%s' absente, et la base d'administration est injoignable : %s", nom, exc
            )
            return False
        try:
            # Le nom vient du DSN fourni par l'operateur ; on le cite malgre tout.
            await conn.execute(f'CREATE DATABASE "{nom}"')
            self.database_created = True
            logger.warning("Base de donnees '%s' creee automatiquement", nom)
            return True
        except asyncpg.DuplicateDatabaseError:
            return True  # course avec une autre instance : tres bien
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Creation de la base '%s' impossible : %s. Creez-la a la main : "
                'CREATE DATABASE "%s";',
                nom,
                exc,
                nom,
            )
            return False
        finally:
            await conn.close()

    def _database_name(self) -> str | None:
        fin = self._dsn.rsplit("/", 1)
        if len(fin) != 2 or not fin[1]:
            return None
        return fin[1].split("?", 1)[0]

    def _connect_hint(self, error: Exception | None) -> str:
        """Traduit l'erreur de connexion en action concrete."""
        texte = str(error or "").lower()
        nom = self._database_name() or "qos"
        if "does not exist" in texte:
            return (
                f'Creez la base : CREATE DATABASE "{nom}"; ou laissez docker compose s\'en charger.'
            )
        if "password authentication" in texte or "role" in texte:
            return "Verifiez l'utilisateur et le mot de passe de DATABASE_URL."
        if "connect call failed" in texte or "refused" in texte:
            return (
                "Verifiez que PostgreSQL ecoute a cette adresse. Hors docker, "
                "DATABASE_URL doit pointer sur localhost et non sur l'hote "
                "'timescaledb', qui n'existe que dans le reseau docker."
            )
        return ""

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Pool PostgreSQL ferme")

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - le detail est remonte par /health/ready
            logger.exception("Echec du ping base de donnees")
            return False

    async def migrate(self) -> None:
        """Applique le schema. Idempotent : rejouable a chaque demarrage."""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            except Exception as exc:  # noqa: BLE001
                # PostgreSQL nu (CI, poste de dev) : on continue en tables simples.
                logger.warning("Extension timescaledb indisponible (%s)", exc)

            self.timescale_available = bool(
                await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            )
            await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            logger.info(
                "Schema applique (timescaledb=%s)",
                "actif" if self.timescale_available else "absent",
            )

    async def apply_policies(
        self,
        *,
        chunk_interval_hours: int = 24,
        compression_after_days: int = 7,
        retention_days: int = 90,
    ) -> None:
        """Politiques de compression et de retention, pilotees par la configuration.

        Elles vivent ici et non dans schema.sql parce que leurs valeurs viennent
        des variables d'environnement. Une valeur a 0 desactive la politique.
        """
        if not self.timescale_available:
            logger.info("Politiques Timescale ignorees : extension absente")
            return

        hypertables = ("subscriber_metrics", "backhaul_metrics", "qoe_scores")
        async with self.pool.acquire() as conn:
            for table in hypertables:
                is_hypertable = await conn.fetchval(
                    "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = $1",
                    table,
                )
                if not is_hypertable:
                    continue

                if chunk_interval_hours > 0:
                    await self._try(
                        conn,
                        "SELECT set_chunk_time_interval($1::regclass, $2::interval)",
                        table,
                        f"{chunk_interval_hours} hours",
                        label=f"chunk_interval({table})",
                    )
                if compression_after_days > 0:
                    await self._try(
                        conn,
                        "SELECT add_compression_policy($1::regclass, $2::interval, "
                        "if_not_exists => TRUE)",
                        table,
                        f"{compression_after_days} days",
                        label=f"compression_policy({table})",
                    )
                if retention_days > 0:
                    await self._try(
                        conn,
                        "SELECT add_retention_policy($1::regclass, $2::interval, "
                        "if_not_exists => TRUE)",
                        table,
                        f"{retention_days} days",
                        label=f"retention_policy({table})",
                    )

    @staticmethod
    async def _try(conn: asyncpg.Connection, sql: str, *args: object, label: str) -> None:
        try:
            await conn.execute(sql, *args)
            logger.info("Politique appliquee : %s", label)
        except Exception as exc:  # noqa: BLE001
            # Timescale Apache Edition n'a pas les politiques de fond : ce n'est pas
            # bloquant, l'ingestion fonctionne sans.
            logger.warning("Politique %s non appliquee : %s", label, exc)

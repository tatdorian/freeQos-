"""Inventaire vivant des routeurs.

Fusionne deux sources et maintient la liste des collecteurs a chaud, sans
redemarrage :

  1. l'inventaire FICHIER (YAML/env) : secrets en variables d'environnement,
     lecture seule depuis l'interface. C'est la posture d'origine, preservee ;
  2. la BASE : routeurs ajoutes depuis l'interface, secrets chiffres au repos.

En cas d'homonymie le fichier gagne : une declaration versionnee et revue doit
primer sur une saisie faite dans un formulaire.

Les collecteurs inchanges sont REUTILISES d'un rechargement a l'autre. Deux
raisons : ne pas rouvrir une session API a chaque modification, et surtout ne pas
perdre la continuite du calcul de debit (le RateTracker est indexe par nom de
routeur, un collecteur recree repartirait sans point de reference).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.collectors.mikrotik import MikrotikCollector, RouterOsReadClient
from app.config import MissingSecretError, RouterConfig, Settings
from app.db.routers_repo import RoutersRepository

logger = logging.getLogger(__name__)

SOURCE_FILE = "file"
SOURCE_DB = "db"


@dataclass(frozen=True)
class RouterEntry:
    config: RouterConfig
    source: str
    router_id: int | None = None


def _fingerprint(config: RouterConfig) -> tuple:
    """Ce qui, en changeant, impose de reconstruire le collecteur."""
    return (
        config.host,
        config.port,
        config.username,
        config.use_ssl,
        config.timeout_s,
        config.pppoe_interface_pattern,
        config.effective_pop_name,
    )


class RouterRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: RoutersRepository | None = None,
        client_factory: Callable[[RouterConfig], RouterOsReadClient] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._client_factory = client_factory
        self._collectors: dict[str, MikrotikCollector] = {}
        self._fingerprints: dict[str, tuple] = {}
        self._sources: dict[str, str] = {}
        self._ids: dict[str, int | None] = {}
        self.skipped: list[str] = []

    @property
    def collectors(self) -> list[MikrotikCollector]:
        return list(self._collectors.values())

    def source_of(self, name: str) -> str | None:
        return self._sources.get(name)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "source": self._sources.get(name),
                "router_id": self._ids.get(name),
                "host": collector.config.host,
                "port": collector.config.port,
                "role": collector.config.role.value,
                "pop": collector.config.effective_pop_name,
                "username": collector.config.username,
                "editable": self._sources.get(name) == SOURCE_DB,
            }
            for name, collector in self._collectors.items()
        ]

    # ------------------------------------------------------------------
    async def resolve_entries(self) -> list[RouterEntry]:
        entries: dict[str, RouterEntry] = {}
        skipped: list[str] = []

        if self._repository is not None:
            try:
                for config in await self._repository.load_configs():
                    router_id = await self._repository.find_id_by_name(config.name)
                    entries[config.name] = RouterEntry(config, SOURCE_DB, router_id)
            except Exception:  # noqa: BLE001
                # Une base momentanement indisponible ne doit pas vider l'inventaire
                # fichier : on garde ce qu'on peut.
                logger.exception("Chargement des routeurs en base impossible")

        for config in self._settings.enabled_routers:
            try:
                config.resolve_password()
            except MissingSecretError as exc:
                logger.error("Routeur ignore : %s", exc)
                skipped.append(str(exc))
                continue
            # Le fichier prime sur la base en cas d'homonymie.
            entries[config.name] = RouterEntry(config, SOURCE_FILE)

        self.skipped = skipped
        return list(entries.values())

    async def reload(self) -> list[MikrotikCollector]:
        entries = await self.resolve_entries()
        wanted = {entry.config.name: entry for entry in entries}

        for name in list(self._collectors):
            if name not in wanted:
                self._retire(name)

        for name, entry in wanted.items():
            fingerprint = _fingerprint(entry.config)
            existing = self._collectors.get(name)
            if existing is not None and self._fingerprints.get(name) == fingerprint:
                # Inchange : on garde la connexion et l'historique de debit.
                self._sources[name] = entry.source
                self._ids[name] = entry.router_id
                continue
            if existing is not None:
                self._retire(name)
            self._collectors[name] = self._build(entry.config)
            self._fingerprints[name] = fingerprint
            self._sources[name] = entry.source
            self._ids[name] = entry.router_id

        logger.info(
            "Inventaire recharge : %d routeur(s) actif(s)%s",
            len(self._collectors),
            f", {len(self.skipped)} ignore(s)" if self.skipped else "",
        )
        return self.collectors

    def _build(self, config: RouterConfig) -> MikrotikCollector:
        client = self._client_factory(config) if self._client_factory else None
        return MikrotikCollector(config, client=client)

    def _retire(self, name: str) -> None:
        collector = self._collectors.pop(name, None)
        self._fingerprints.pop(name, None)
        self._sources.pop(name, None)
        self._ids.pop(name, None)
        if collector is not None:
            try:
                collector.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info("Routeur '%s' retire de l'inventaire", name)

    def adopt(self, collectors: Sequence[MikrotikCollector], *, source: str = SOURCE_FILE) -> None:
        """Enregistre des collecteurs deja construits.

        Sert au demarrage sans base et aux tests : le registre doit connaitre
        les collecteurs pour que la topologie et le shaping sachent a qui parler,
        meme quand personne n'a appele reload().
        """
        for collector in collectors:
            nom = collector.name
            self._collectors[nom] = collector
            self._fingerprints[nom] = _fingerprint(collector.config)
            self._sources[nom] = source
            self._ids.setdefault(nom, None)

    def build_probe(self, config: RouterConfig) -> MikrotikCollector:
        """Collecteur jetable pour tester une configuration non encore enregistree."""
        return self._build(config)

    def close_all(self) -> None:
        for name in list(self._collectors):
            self._retire(name)


def collectors_from_settings(
    settings: Settings,
    client_factory: Callable[[RouterConfig], RouterOsReadClient] | None = None,
) -> Sequence[MikrotikCollector]:
    """Raccourci sans base, conserve pour les tests et le mode degrade."""
    registry = RouterRegistry(settings, client_factory=client_factory)
    collectors = []
    for config in settings.enabled_routers:
        try:
            config.resolve_password()
        except MissingSecretError as exc:
            logger.error("Routeur ignore : %s", exc)
            continue
        collectors.append(registry.build_probe(config))
    return collectors

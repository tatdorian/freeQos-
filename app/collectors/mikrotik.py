"""Collecteur RouterOS : sessions PPPoE actives et compteurs d'octets.

POINT TECHNIQUE IMPORTANT
-------------------------
``/ppp/active/print`` ne porte PAS les compteurs d'octets d'une session. Sur
RouterOS, chaque session PPPoE cree une interface dynamique nommee
``<pppoe-LOGIN>`` et ce sont les compteurs de cette interface qui contiennent
``rx-byte`` / ``tx-byte``. Le collecteur fait donc deux lectures et les correle :

    /ppp/active/print  -> login, adresse, uptime, session-id
    /interface/print   -> rx-byte, tx-byte de <pppoe-LOGIN>

Le motif de nommage est configurable par routeur (``pppoe_interface_pattern``)
car il depend du profil PPP.

HORS-BANDE
----------
Ce module n'expose aucune methode d'ecriture. Les identifiants utilises sont ceux
du compte lecture seule ``qos-ro``. Le push des files CAKE est la phase 2 et
vivra dans un module distinct.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Any, Protocol

from app.collectors.parsing import (
    parse_counter,
    parse_routeros_uptime,
    pppoe_interface_name,
)
from app.config import RouterConfig
from app.models import PppoeSession

logger = logging.getLogger(__name__)


class RouterOsReadClient(Protocol):
    """Interface de lecture minimale d'un routeur : rend le collecteur testable."""

    def ppp_active(self) -> list[dict[str, Any]]: ...

    def interfaces(self) -> list[dict[str, Any]]: ...

    def identity(self) -> str | None: ...

    def close(self) -> None: ...


class LibrouterosReadClient:
    """Client synchrone base sur librouteros (API binaire, port 8728).

    La connexion est persistante et reconstruite paresseusement : ouvrir une
    session API toutes les 10 secondes sur chaque PoP couterait cher en CPU
    routeur et polluerait les logs d'authentification. En cas d'erreur la
    connexion est fermee, le cycle suivant se reconnecte.

    Un verrou protege la connexion : l'appelant l'execute dans un thread pool et
    deux cycles ne doivent jamais se chevaucher sur la meme socket.
    """

    def __init__(self, config: RouterConfig) -> None:
        self._config = config
        self._api: Any = None
        self._lock = threading.Lock()

    # -- connexion ---------------------------------------------------------
    def _connect(self) -> Any:
        from librouteros import connect
        from librouteros.login import plain

        kwargs: dict[str, Any] = {
            "host": self._config.host,
            "port": self._config.port,
            "username": self._config.username,
            "password": self._config.resolve_password(),
            "timeout": self._config.timeout_s,
            "login_method": plain,
        }
        if self._config.use_ssl:
            import ssl

            context = ssl.create_default_context()
            # Les CHR de lab utilisent un certificat auto-signe ; la confidentialite
            # du transport reste assuree, l'authentification du pair non.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            kwargs["ssl_wrapper"] = context.wrap_socket
        return connect(**kwargs)

    def _ensure(self) -> Any:
        if self._api is None:
            self._api = self._connect()
            logger.info(
                "Connecte a %s (%s:%s) en lecture seule",
                self._config.name,
                self._config.host,
                self._config.port,
            )
        return self._api

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:  # noqa: BLE001 - la socket est peut-etre deja morte
                pass
            self._api = None

    # -- lectures ----------------------------------------------------------
    def _query(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            try:
                api = self._ensure()
                return [dict(row) for row in api.path(path)]
            except Exception:
                # Toute erreur invalide la connexion : le prochain cycle rouvrira.
                self._drop()
                raise

    def ppp_active(self) -> list[dict[str, Any]]:
        return self._query("/ppp/active")

    def interfaces(self) -> list[dict[str, Any]]:
        return self._query("/interface")

    def identity(self) -> str | None:
        try:
            rows = self._query("/system/identity")
        except Exception:  # noqa: BLE001 - purement informatif
            return None
        return rows[0].get("name") if rows else None


class MikrotikCollector:
    """Lit les sessions PPPoE d'un routeur et les normalise en PppoeSession."""

    def __init__(
        self,
        config: RouterConfig,
        client: RouterOsReadClient | None = None,
    ) -> None:
        self.config = config
        self._client = client or LibrouterosReadClient(config)

    @property
    def name(self) -> str:
        return self.config.name

    async def collect(self) -> list[PppoeSession]:
        """Execute la lecture bloquante dans un thread, avec garde-fou de duree.

        librouteros est synchrone. Sans ce passage par un thread, un routeur qui
        ne repond plus figerait la boucle asyncio et donc tous les autres PoPs.
        """
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.collect_sync), timeout=timeout)

    def collect_sync(self) -> list[PppoeSession]:
        active = self._client.ppp_active()
        interfaces = self._client.interfaces()
        return self._correlate(active, interfaces)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    def _correlate(
        self, active: list[dict[str, Any]], interfaces: list[dict[str, Any]]
    ) -> list[PppoeSession]:
        by_name = {str(row.get("name", "")): row for row in interfaces}
        pop_name = self.config.effective_pop_name
        sessions: list[PppoeSession] = []

        for row in active:
            login = row.get("name")
            if not login:
                continue
            login = str(login)
            iface_name = pppoe_interface_name(self.config.pppoe_interface_pattern, login)
            iface = by_name.get(iface_name)
            if iface is None:
                iface, iface_name = self._fuzzy_interface(by_name, login, iface_name)

            sessions.append(
                PppoeSession(
                    login=login,
                    router_name=self.config.name,
                    pop_name=pop_name,
                    address=_as_str(row.get("address")),
                    caller_id=_as_str(row.get("caller-id")),
                    service=_as_str(row.get("service")),
                    session_id=_as_str(row.get("session-id")),
                    uptime_s=parse_routeros_uptime(row.get("uptime")),
                    interface=iface_name if iface is not None else None,
                    rx_bytes=parse_counter(iface.get("rx-byte")) if iface else None,
                    tx_bytes=parse_counter(iface.get("tx-byte")) if iface else None,
                )
            )
        return sessions

    def _fuzzy_interface(
        self, by_name: dict[str, dict[str, Any]], login: str, expected: str
    ) -> tuple[dict[str, Any] | None, str]:
        """Repli si le motif de nommage ne correspond pas.

        Certains profils PPP renomment l'interface dynamique. On cherche alors une
        interface dont le nom contient le login, ce qui reste sans ambiguite tant
        que les logins ne sont pas prefixes les uns des autres. Si plusieurs
        candidats sortent, on n'en choisit aucun plutot que d'attribuer un debit
        au mauvais abonne.
        """
        candidates = [(name, row) for name, row in by_name.items() if login in name]
        if len(candidates) == 1:
            name, row = candidates[0]
            logger.debug(
                "%s: interface '%s' introuvable, correlation par nom sur '%s'",
                self.config.name,
                expected,
                name,
            )
            return row, name
        if len(candidates) > 1:
            logger.warning(
                "%s: correlation ambigue pour le login '%s' (%d interfaces)",
                self.config.name,
                login,
                len(candidates),
            )
        return None, expected


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def utcnow() -> datetime:
    return datetime.now(tz=UTC)

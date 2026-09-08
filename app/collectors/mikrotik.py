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
    parse_bitrate,
    parse_counter,
    parse_routeros_duration_ms,
    parse_routeros_uptime,
    pppoe_interface_name,
)
from app.collectors.topology import ethernet_capacity_mbps
from app.config import RouterConfig
from app.models import InterfaceSample, PppoeSession

logger = logging.getLogger(__name__)


# Interfaces DYNAMIQUES : RouterOS en cree une par session PPPoE, nommee
# <pppoe-LOGIN>. Elles sont deja suivies abonne par abonne dans
# subscriber_metrics ; les reprendre ici dupliquerait chaque serie dans une
# table censee ne porter que les liens physiques, et la ferait grossir au
# rythme du parc plutot qu'au rythme des ports.
DYNAMIC_INTERFACE_TYPES = ("pppoe-in", "pppoe-out", "ppp-in", "ppp-out")


def is_physical_interface(row: dict[str, Any]) -> bool:
    """Vrai pour un port qui porte un lien, faux pour une interface de session."""
    name = str(row.get("name") or "")
    if not name or name.startswith("<"):
        return False
    return not str(row.get("type") or "").startswith(DYNAMIC_INTERFACE_TYPES)


def parse_flag(value: object) -> bool | None:
    """RouterOS ecrit les booleens ``true``/``false`` en texte."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


class RouterOsReadClient(Protocol):
    """Interface de lecture minimale d'un routeur : rend le collecteur testable."""

    def ppp_active(self) -> list[dict[str, Any]]: ...

    def interfaces(self) -> list[dict[str, Any]]: ...

    def identity(self) -> str | None: ...

    def system_resource(self) -> dict[str, Any]: ...

    def ping(self, address: str, count: int = 1) -> list[dict[str, Any]]: ...

    # --- Topologie et etat du shaping (lecture seule) ---
    def neighbors(self) -> list[dict[str, Any]]: ...

    def ethernet(self) -> list[dict[str, Any]]: ...

    def monitor_traffic(self, interface: str) -> dict[str, Any]: ...

    def addresses(self) -> list[dict[str, Any]]: ...

    def simple_queues(self) -> list[dict[str, Any]]: ...

    def queue_types(self) -> list[dict[str, Any]]: ...

    def queue_trees(self) -> list[dict[str, Any]]: ...

    def users(self) -> list[dict[str, Any]]: ...

    def user_groups(self) -> list[dict[str, Any]]: ...

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

    def system_resource(self) -> dict[str, Any]:
        rows = self._query("/system/resource")
        return dict(rows[0]) if rows else {}

    # --- Topologie et etat du shaping (lecture seule) ---
    def neighbors(self) -> list[dict[str, Any]]:
        """Voisins MNDP / LLDP / CDP.

        Source maitresse de la topologie : pour chaque interface locale, elle
        nomme l'equipement d'en face. Aucun autre appel ne donne l'adjacence
        physique de facon aussi directe.
        """
        return self._query("/ip/neighbor")

    def ethernet(self) -> list[dict[str, Any]]:
        """Ports ethernet : le debit negocie est le plafond physique du lien."""
        return self._query("/interface/ethernet")

    def monitor_traffic(self, interface: str) -> dict[str, Any]:
        """Debit INSTANTANE d'une interface, mesure par le routeur lui-meme.

        Les compteurs cumulatifs ne donnent un debit qu'apres deux lectures :
        pour repondre "combien passe MAINTENANT sur ce lien", il faut demander
        au routeur, qui tient deja la mesure. ``once`` rend la commande
        ponctuelle au lieu de streamer.

        C'est une commande de LECTURE : elle ne modifie aucune configuration.
        """
        with self._lock:
            try:
                api = self._ensure()
                rows = [
                    dict(row)
                    for row in api(
                        "/interface/monitor-traffic",
                        **{"interface": interface, "once": True},
                    )
                ]
            except Exception:
                self._drop()
                raise
        return rows[0] if rows else {}

    def addresses(self) -> list[dict[str, Any]]:
        return self._query("/ip/address")

    def simple_queues(self) -> list[dict[str, Any]]:
        return self._query("/queue/simple")

    def queue_types(self) -> list[dict[str, Any]]:
        return self._query("/queue/type")

    def queue_trees(self) -> list[dict[str, Any]]:
        return self._query("/queue/tree")

    def users(self) -> list[dict[str, Any]]:
        """Comptes declares. Sert a savoir de quoi le notre est capable."""
        return self._query("/user")

    def user_groups(self) -> list[dict[str, Any]]:
        """Groupes et leurs politiques (read, write, api, test...)."""
        return self._query("/user/group")

    def ping(self, address: str, count: int = 1) -> list[dict[str, Any]]:
        """Sonde active depuis le routeur vers l'abonne.

        C'est une COMMANDE, pas une ecriture de configuration : elle ne modifie
        rien sur l'equipement. Elle exige la politique 'test' sur le compte, que
        le groupe qos-ro possede deja.
        """
        with self._lock:
            try:
                api = self._ensure()
                return [dict(row) for row in api("/ping", address=address, count=count)]
            except Exception:
                self._drop()
                raise


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
    # Debit des liens
    # ------------------------------------------------------------------
    async def collect_interfaces(self) -> list[InterfaceSample]:
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(
            asyncio.to_thread(self.collect_interfaces_sync), timeout=timeout
        )

    def collect_interfaces_sync(self) -> list[InterfaceSample]:
        """Compteurs de chaque port physique, SANS les debits.

        Le debit se derive de deux lectures successives, exactement comme pour
        les abonnes : c'est le service de collecte qui le calcule, parce que
        c'est lui qui garde l'etat entre deux cycles.
        """
        rows = self._client.interfaces()
        try:
            capacites = {
                str(row.get("name") or ""): ethernet_capacity_mbps(row)
                for row in self._client.ethernet()
            }
        except Exception:  # noqa: BLE001 - la capacite n'est qu'un plafond d'affichage
            # Un CHR sans port physique refuse cet appel. Les compteurs restent
            # exploitables ; seule la jauge de charge perd sa reference.
            logger.debug("Capacites ethernet indisponibles sur %s", self.name)
            capacites = {}

        ts = utcnow()
        echantillons: list[InterfaceSample] = []
        for row in rows:
            if not is_physical_interface(row):
                continue
            nom = str(row.get("name"))
            echantillons.append(
                InterfaceSample(
                    ts=ts,
                    router_name=self.name,
                    interface=nom,
                    kind=_as_str(row.get("type")),
                    running=parse_flag(row.get("running")),
                    capacity_mbps=capacites.get(nom),
                    rx_bytes=parse_counter(row.get("rx-byte")),
                    tx_bytes=parse_counter(row.get("tx-byte")),
                )
            )
        return echantillons

    async def measure_interface(self, interface: str) -> dict[str, Any]:
        """Debit instantane d'une interface, a la demande.

        Sert le "je veux voir le debit de ce lien MAINTENANT" : au lieu
        d'attendre le prochain cycle de compteurs, on demande au routeur la
        mesure qu'il tient deja.
        """
        timeout = max(self.config.timeout_s * 2, 4.0)
        row = await asyncio.wait_for(
            asyncio.to_thread(self._client.monitor_traffic, interface), timeout=timeout
        )
        return {
            "interface": interface,
            "rx_bps": parse_bitrate(row.get("rx-bits-per-second")),
            "tx_bps": parse_bitrate(row.get("tx-bits-per-second")),
            "rx_pps": parse_counter(row.get("rx-packets-per-second")),
            "tx_pps": parse_counter(row.get("tx-packets-per-second")),
        }

    async def ping(self, address: str, count: int = 1) -> float | None:
        """RTT du routeur vers l'abonne, en millisecondes.

        Retourne None si l'abonne ne repond pas : une absence de mesure vaut
        mieux qu'une valeur inventee.
        """
        timeout = max(self.config.timeout_s * 2, 4.0) + count
        rows = await asyncio.wait_for(
            asyncio.to_thread(self._client.ping, address, count), timeout=timeout
        )
        times = [
            parse_routeros_duration_ms(row.get("time"))
            for row in rows
            if row.get("time") is not None
        ]
        valides = [t for t in times if t is not None]
        return min(valides) if valides else None

    async def probe(self) -> dict[str, Any]:
        """Teste la connexion et renvoie de quoi identifier le routeur.

        Utilise par le bouton "Tester la connexion" de l'interface : un
        administrateur doit pouvoir verifier ses identifiants avant d'enregistrer
        un PoP, plutot que de decouvrir l'erreur dans les logs dix minutes apres.
        """
        timeout = max(self.config.timeout_s * 3, 5.0)
        return await asyncio.wait_for(asyncio.to_thread(self.probe_sync), timeout=timeout)

    def probe_sync(self) -> dict[str, Any]:
        resource = self._client.system_resource()
        sessions = self._client.ppp_active()
        interfaces = self._client.interfaces()

        # Droits reels du compte : c'est ce qui determine si l'enforcement
        # pourra fonctionner, pas ce qui est declare dans l'inventaire.
        from app.enforcement.capability import inspect_write_capability

        try:
            capacite = inspect_write_capability(
                self.config.rw_username or self.config.username,
                self._client.users(),
                self._client.user_groups(),
            ).to_dict()
        except Exception as exc:  # noqa: BLE001 - purement informatif
            capacite = {
                "username": self.config.rw_username or self.config.username,
                "can_write": None,
                "detail": f"droits non verifiables ({type(exc).__name__})",
            }

        return {
            "write_capability": capacite,
            "reachable": True,
            "identity": self._client.identity(),
            "version": _as_str(resource.get("version")),
            "board_name": _as_str(resource.get("board-name")),
            "uptime": _as_str(resource.get("uptime")),
            "cpu_load": resource.get("cpu-load"),
            "free_memory": resource.get("free-memory"),
            "ppp_active_sessions": len(sessions),
            "interfaces": len(interfaces),
            # Le point qui compte vraiment : sans correlation, pas de debit.
            "correlated_sessions": sum(
                1
                for session in self._correlate(sessions, interfaces)
                if session.rx_bytes is not None
            ),
        }

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

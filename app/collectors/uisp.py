"""Capacite des liens backhaul radio.

LECTURE SEULE, TOUJOURS. On ne pilote jamais la radio : on lit sa capacite reelle
pour en faire le debit parent du shaping (goulot numero 2). Le pilotage de la
radio appartient a l'equipement, pas au controleur.

Trois implementations derriere la meme abstraction :
  - UispProvider : API REST du CONTROLEUR UISP (un seul point, header X-Auth-Token) ;
  - AirOsProvider : API LOCALE de chaque antenne Ubiquiti (airOS /status.cgi),
    quand il n'y a pas de UISP -- on interroge directement la radio ;
  - MockBackhaulProvider : capacite simulee, variable dans le temps, deterministe.
    C'est elle qui permet de valider toute la logique en lab sans radio reelle.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.models import BackhaulSample

logger = logging.getLogger(__name__)


class BackhaulCapacityProvider(Protocol):
    """Contrat commun a l'API UISP et au simulateur."""

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]: ...

    async def raw_devices(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


def normalize_capacity_to_mbps(value: Any) -> float | None:
    """Ramene une capacite en Mbps quelle que soit l'unite renvoyee par UISP.

    Les versions d'UISP ne sont pas coherentes entre elles : selon les modeles et
    les firmwares, ``downlinkCapacity`` arrive en bps, en kbps ou deja en Mbps.
    L'ordre de grandeur leve l'ambiguite sans risque pour des capacites radio
    realistes (1 Mbps a 10 Gbps) :
        >= 1e6  -> bps    (500000000 -> 500)
        >= 1e3  -> kbps   (500000    -> 500)
        sinon   -> Mbps   (500       -> 500)
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return 0.0
    if number >= 1e6:
        return number / 1e6
    if number >= 1e3:
        return number / 1e3
    return number


def _pluck(payload: dict[str, Any], *paths: str) -> Any:
    """Premiere valeur non nulle parmi plusieurs chemins pointes.

    Le schema UISP varie selon la version et le type d'equipement ; on essaie
    plusieurs emplacements plutot que de casser a la premiere difference.
    """
    for path in paths:
        node: Any = payload
        for key in path.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is not None:
            return node
    return None


def parse_uisp_device(device: dict[str, Any], *, ts: datetime | None = None) -> BackhaulSample:
    """Traduit une fiche device UISP en echantillon, de facon defensive."""
    device_id = str(
        _pluck(device, "identification.id", "id", "deviceId", "identification.mac") or ""
    )
    down = normalize_capacity_to_mbps(
        _pluck(device, "overview.downlinkCapacity", "downlinkCapacity", "overview.downlink")
    )
    up = normalize_capacity_to_mbps(
        _pluck(device, "overview.uplinkCapacity", "uplinkCapacity", "overview.uplink")
    )
    status = _pluck(device, "overview.status", "status", "identification.status")
    online = str(status).lower() in {"active", "connected", "online"} if status else True

    capacity = None
    if down is not None and up is not None:
        # La capacite utile d'un lien PtP est bornee par son sens le plus faible.
        capacity = min(down, up) if min(down, up) > 0 else max(down, up)
    else:
        capacity = down if down is not None else up

    return BackhaulSample(
        ts=ts or datetime.now(tz=UTC),
        device_id=device_id,
        capacity_mbps=capacity,
        capacity_down_mbps=down,
        capacity_up_mbps=up,
        signal_dbm=_as_float(_pluck(device, "overview.signal", "signal", "overview.rssi")),
        airtime_pct=_as_float(
            _pluck(device, "overview.airTime", "airTime", "overview.transmitAirtime")
        ),
        mcs_down=_as_str(_pluck(device, "overview.downlinkMcs", "downlinkMcs")),
        mcs_up=_as_str(_pluck(device, "overview.uplinkMcs", "uplinkMcs")),
        online=online,
    )


class UispProvider:
    """Client REST UISP (lecture seule)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_tls: bool = True,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=f"{self._base_url}/nms/api/v2.1",
            headers={"X-Auth-Token": token, "Accept": "application/json"},
            verify=verify_tls,
            timeout=timeout_s,
        )

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]:
        """Un seul appel /devices puis filtrage local.

        UISP repond bien plus vite sur un listing global que sur N appels
        unitaires, et le nombre de backhauls d'un WISP tient largement en memoire.
        """
        wanted = {d for d in device_ids if d}
        response = await self._client.get("/devices")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("devices") or []

        ts = datetime.now(tz=UTC)
        result: dict[str, BackhaulSample] = {}
        for device in payload:
            if not isinstance(device, dict):
                continue
            sample = parse_uisp_device(device, ts=ts)
            if sample.device_id and (not wanted or sample.device_id in wanted):
                result[sample.device_id] = sample

        missing = wanted - result.keys()
        if missing:
            logger.warning("UISP : %d device(s) demandes absents de la reponse", len(missing))
        return result

    async def raw_devices(self) -> list[dict[str, Any]]:
        """Fiches completes, pour la decouverte de topologie.

        La capacite ne suffit pas : il faut la MAC (jointure avec les voisins
        MikroTik) et le rattachement station -> AP.
        """
        response = await self._client.get("/devices")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("devices") or []
        return [d for d in payload if isinstance(d, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class AirOsTarget:
    """Une antenne Ubiquiti interrogee directement sur son API locale.

    ``key`` est l'identifiant stable du backhaul (celui qui sert de cle en base
    et a la jointure de topologie) ; ``host`` est l'adresse de management de la
    radio. Les deux peuvent differer : on peut nommer un lien ``bh-nord`` et le
    joindre en 10.0.0.2.
    """

    key: str
    host: str
    username: str = ""
    password: str = ""
    verify_tls: bool = False


def parse_airos_status(
    status: dict[str, Any], *, key: str, ts: datetime | None = None
) -> BackhaulSample:
    """Traduit un ``/status.cgi`` airOS en echantillon, de facon defensive.

    airMAX expose ``wireless.txcapacity`` / ``wireless.rxcapacity`` en kbps : ce
    sont les capacites estimees du lien, pas le debit instantane. A defaut (vieux
    firmware), on retombe sur les debits PHY ``txrate`` / ``rxrate`` en Mbps. Les
    noms de champs varient selon la version : on essaie plusieurs emplacements.
    """
    wireless = status.get("wireless") if isinstance(status.get("wireless"), dict) else {}

    down = normalize_capacity_to_mbps(
        _pluck(status, "wireless.txcapacity", "wireless.txrate", "wireless.throughput.tx")
    )
    up = normalize_capacity_to_mbps(
        _pluck(status, "wireless.rxcapacity", "wireless.rxrate", "wireless.throughput.rx")
    )

    capacity = None
    if down is not None and up is not None:
        capacity = min(down, up) if min(down, up) > 0 else max(down, up)
    elif down is not None or up is not None:
        capacity = down if down is not None else up

    # La MAC sert a rattacher la radio aux voisins MikroTik : sans elle, la
    # capacite est lue mais le lien reste orphelin dans le graphe.
    mac = _pluck(status, "wireless.apmac", "host.hwaddr", "host.mac")

    signal = _as_float(_pluck(status, "wireless.signal", "wireless.rssi"))
    return BackhaulSample(
        ts=ts or datetime.now(tz=UTC),
        device_id=key,
        capacity_mbps=capacity,
        capacity_down_mbps=down,
        capacity_up_mbps=up,
        signal_dbm=signal,
        airtime_pct=_as_float(
            _pluck(status, "wireless.airmax.quality", "wireless.polling.use", "wireless.ccq")
        ),
        online=bool(wireless) and capacity not in (None, 0.0),
        raw={"mac": _as_str(mac)} if mac else {},
    )


class AirOsClient:
    """Client de l'API locale d'UNE antenne airOS (lecture seule).

    airOS protege ``/status.cgi`` par une session : on tente d'abord un GET
    direct, et seulement si l'equipement refuse on passe par ``/login.cgi``. Bien
    des parcs laissent un compte lecture sans mot de passe, ou une IP de
    management deja de confiance : inutile de s'authentifier pour rien.
    """

    def __init__(
        self,
        target: AirOsTarget,
        *,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._target = target
        self._client = client or httpx.AsyncClient(
            base_url=f"https://{target.host}",
            verify=target.verify_tls,
            timeout=timeout_s,
            follow_redirects=False,
        )

    async def fetch_status(self) -> dict[str, Any]:
        response = await self._client.get("/status.cgi")
        if response.status_code in (401, 403) or _looks_like_login(response):
            await self._login()
            response = await self._client.get("/status.cgi")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("reponse /status.cgi inattendue (pas un objet JSON)")
        return payload

    async def _login(self) -> None:
        # Un premier GET pose le cookie de session que /login.cgi attend en
        # retour ; certains firmwares refusent le POST sans lui.
        await self._client.get("/login.cgi")
        response = await self._client.post(
            "/login.cgi",
            data={
                "username": self._target.username,
                "password": self._target.password,
                "uri": "/status.cgi",
            },
        )
        # airOS repond 200 puis redirige ; l'echec d'auth renvoie a la mire.
        if response.status_code >= 400:
            response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def _looks_like_login(response: httpx.Response) -> bool:
    """Une mire de login se reconnait a sa redirection ou a son HTML."""
    if response.status_code in (301, 302, 303, 307, 308):
        return True
    kind = response.headers.get("content-type", "")
    return "text/html" in kind.lower()


class AirOsProvider:
    """Interroge directement N antennes Ubiquiti, sans UISP.

    Chaque radio est lue en parallele ; une antenne injoignable retire sa
    capacite du resultat mais ne fait pas echouer les autres, exactement comme
    un PoP absent lors de la decouverte.
    """

    def __init__(
        self,
        targets: Sequence[AirOsTarget],
        *,
        timeout_s: float = 10.0,
        client_factory: Callable[[AirOsTarget], AirOsClient] | None = None,
    ) -> None:
        self._targets = {t.key: t for t in targets if t.key}
        self._timeout_s = timeout_s
        self._factory = client_factory or (lambda target: AirOsClient(target, timeout_s=timeout_s))
        self._clients: dict[str, AirOsClient] = {}
        self._last_status: dict[str, dict[str, Any]] = {}

    def _client_for(self, target: AirOsTarget) -> AirOsClient:
        existing = self._clients.get(target.key)
        if existing is None:
            existing = self._factory(target)
            self._clients[target.key] = existing
        return existing

    async def set_targets(self, targets: Sequence[AirOsTarget]) -> None:
        """Remplace la liste des antennes (edition depuis l'interface).

        Un client dont la cible a change de host ou d'identifiants -- ou qui a
        disparu -- est ferme : la prochaine lecture le reconstruit avec les
        nouvelles valeurs.
        """
        nouveaux = {t.key: t for t in targets if t.key}
        for key, ancien in list(self._targets.items()):
            if nouveaux.get(key) != ancien:
                client = self._clients.pop(key, None)
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                self._last_status.pop(key, None)
        self._targets = nouveaux

    async def _read_one(self, target: AirOsTarget) -> tuple[str, BackhaulSample | None]:
        try:
            status = await self._client_for(target).fetch_status()
        except Exception as exc:  # noqa: BLE001 - une radio muette n'en coule pas d'autres
            logger.warning("airOS %s (%s) injoignable : %s", target.key, target.host, exc)
            return target.key, None
        self._last_status[target.key] = status
        return target.key, parse_airos_status(status, key=target.key)

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]:
        wanted = [self._targets[d] for d in device_ids if d in self._targets]
        if not wanted:
            return {}
        resultats = await asyncio.gather(*(self._read_one(t) for t in wanted))
        return {key: sample for key, sample in resultats if sample is not None}

    async def raw_devices(self) -> list[dict[str, Any]]:
        """Fiches brutes pour la decouverte de topologie.

        On refresh d'abord tous les status connus, puis on renormalise chacun
        vers la forme que ``attach_uisp_devices`` attend : un ``identification``
        portant l'id stable et la MAC.
        """
        if self._targets:
            await self.get_capacities(list(self._targets))
        devices: list[dict[str, Any]] = []
        for key, status in self._last_status.items():
            mac = _pluck(status, "wireless.apmac", "host.hwaddr", "host.mac")
            devices.append(
                {
                    "identification": {
                        "id": key,
                        "mac": _as_str(mac),
                        "name": _as_str(_pluck(status, "host.hostname", "host.devmodel")),
                    },
                    "airos": status,
                }
            )
        return devices

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()


class DbAirOsProvider(AirOsProvider):
    """Provider airOS dont les antennes viennent de la BASE, relues a chaque cycle.

    C'est ce qui rend l'ajout d'une antenne depuis l'interface immediat : aucune
    variable d'environnement, aucun redemarrage. On recharge la liste avant
    chaque lecture ; une base momentanement injoignable conserve la derniere
    liste connue plutot que de tout perdre.
    """

    def __init__(
        self,
        target_loader: Callable[[], Awaitable[Sequence[AirOsTarget]]],
        *,
        timeout_s: float = 10.0,
        client_factory: Callable[[AirOsTarget], AirOsClient] | None = None,
    ) -> None:
        super().__init__([], timeout_s=timeout_s, client_factory=client_factory)
        self._loader = target_loader

    async def _refresh(self) -> None:
        try:
            targets = await self._loader()
        except Exception as exc:  # noqa: BLE001 - on garde la liste precedente
            logger.warning("Antennes airOS non rechargees depuis la base : %s", exc)
            return
        await self.set_targets(targets)

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]:
        await self._refresh()
        return await super().get_capacities(device_ids)

    async def raw_devices(self) -> list[dict[str, Any]]:
        await self._refresh()
        return await super().raw_devices()


class MockBackhaulProvider:
    """Simulateur de capacite radio, deterministe et variable dans le temps.

    La capacite suit une sinusoide (cycle de charge / conditions de propagation)
    dephasee par device, plus un fade lent : c'est exactement le comportement que
    la boucle centrale doit savoir suivre, sans avoir besoin d'une radio.

    Le simulateur est deterministe pour un couple (seed, device_id, temps) : les
    tests peuvent figer l'horloge et verifier des valeurs exactes.
    """

    def __init__(
        self,
        *,
        base_capacity_mbps: float = 450.0,
        variation_pct: float = 35.0,
        period_s: float = 600.0,
        seed: int = 1337,
        clock: Callable[[], float] = time.time,
        nominal_by_device: dict[str, float] | None = None,
    ) -> None:
        self.base_capacity_mbps = base_capacity_mbps
        self.variation_pct = max(0.0, min(variation_pct, 95.0))
        self.period_s = max(period_s, 1.0)
        self.seed = seed
        self._clock = clock
        # Chaque lien oscille autour de SA capacite nominale : sans cela, un
        # backhaul declare a 300 Mbps afficherait 580 Mbps en lab, et le rapport
        # "capacite mesuree / nominal" affiche par l'interface n'aurait aucun sens.
        self._nominal = dict(nominal_by_device or {})
        self._overrides: dict[str, float] = {}
        self._devices: list[dict[str, Any]] = []

    def set_capacity(self, device_id: str, capacity_mbps: float) -> None:
        """Fige la capacite d'un device : utile pour rejouer un fade en test."""
        self._overrides[device_id] = capacity_mbps

    def clear_overrides(self) -> None:
        self._overrides.clear()

    def _phase(self, device_id: str) -> float:
        digest = hashlib.sha256(f"{self.seed}:{device_id}".encode()).digest()
        return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * 2 * math.pi

    def base_for(self, device_id: str) -> float:
        """Capacite nominale de ce lien, ou la valeur par defaut s'il est inconnu."""
        return self._nominal.get(device_id) or self.base_capacity_mbps

    def set_nominal(self, device_id: str, capacity_mbps: float) -> None:
        self._nominal[device_id] = capacity_mbps

    def sample_for(self, device_id: str, now: float | None = None) -> BackhaulSample:
        now = self._clock() if now is None else now
        ts = datetime.fromtimestamp(now, tz=UTC)
        base = self.base_for(device_id)

        if device_id in self._overrides:
            capacity = self._overrides[device_id]
        else:
            phase = self._phase(device_id)
            # Deux composantes : un cycle principal et un fade lent (periode x 7,3)
            # pour eviter un signal parfaitement periodique.
            main = math.sin(2 * math.pi * now / self.period_s + phase)
            slow = math.sin(2 * math.pi * now / (self.period_s * 7.3) + phase / 2)
            modulation = 1.0 + (self.variation_pct / 100.0) * (0.7 * main + 0.3 * slow)
            capacity = max(1.0, base * modulation)

        ratio = capacity / base if base else 1.0
        # Un signal plus faible accompagne une capacite plus faible : -45 dBm au
        # nominal, jusqu'a -80 dBm quand le lien s'effondre.
        signal = -45.0 - 35.0 * max(0.0, min(1.0, 1.0 - ratio))
        airtime = max(0.0, min(100.0, 90.0 - 45.0 * ratio))

        return BackhaulSample(
            ts=ts,
            device_id=device_id,
            capacity_mbps=round(capacity, 2),
            capacity_down_mbps=round(capacity * 0.75, 2),
            capacity_up_mbps=round(capacity * 0.25, 2),
            signal_dbm=round(signal, 1),
            airtime_pct=round(airtime, 1),
            mcs_down="MCS9" if ratio > 0.8 else "MCS5" if ratio > 0.5 else "MCS2",
            mcs_up="MCS7" if ratio > 0.8 else "MCS4" if ratio > 0.5 else "MCS1",
            online=capacity > 1.0,
            raw={"simulated": True},
        )

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]:
        now = self._clock()
        return {d: self.sample_for(d, now) for d in device_ids if d}

    def register_device(self, device: dict[str, Any]) -> None:
        """Declare une fiche simulee, pour tester la decouverte de topologie."""
        self._devices.append(device)

    async def raw_devices(self) -> list[dict[str, Any]]:
        return list(self._devices)

    async def aclose(self) -> None:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

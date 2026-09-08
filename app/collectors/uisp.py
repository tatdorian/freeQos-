"""Capacite des liens backhaul radio.

LECTURE SEULE, TOUJOURS. On ne pilote jamais la radio : on lit sa capacite reelle
pour en faire le debit parent du shaping (goulot numero 2). Le pilotage de la
radio appartient a l'equipement, pas au controleur.

Deux implementations derriere la meme abstraction :
  - UispProvider : API REST UISP (header X-Auth-Token) ;
  - MockBackhaulProvider : capacite simulee, variable dans le temps, deterministe.
    C'est elle qui permet de valider toute la logique en lab sans radio reelle.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.models import BackhaulSample

logger = logging.getLogger(__name__)


class BackhaulCapacityProvider(Protocol):
    """Contrat commun a l'API UISP et au simulateur."""

    async def get_capacities(self, device_ids: Sequence[str]) -> dict[str, BackhaulSample]: ...

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

    async def aclose(self) -> None:
        await self._client.aclose()


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

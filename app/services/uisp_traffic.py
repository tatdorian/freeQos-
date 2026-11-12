"""Le trafic des equipements UISP, releve chaque minute et integre au reseau.

UISP sait, pour chaque antenne et chaque equipement qu'il gere, ce que ses
ports transmettent, la capacite radio, le signal et les CPE accroches a une
AP. Jusqu'ici freeQoS n'en lisait que la capacite des liens rattaches a un
site. Ce releve :

- garde le DERNIER etat de chaque equipement (debit, capacite, signal, en ligne),
  et l'historise (``uisp_device_metrics``, 7 jours) ;
- sert ce debit aux liens de l'arbre qui pendent sur un equipement UISP, la ou
  aucun port de routeur ne le mesure ;
- donne au diagnostic radio d'un client la station UISP qui porte sa MAC ou son
  adresse ;
- signale (email) une AP ou un equipement rattache qui tombe.

Les champs UISP varient selon la version et le modele : tout est lu de facon
defensive (plusieurs emplacements essayes), et ce qui manque reste vide.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.collectors.uisp import UispProvider, _as_float, _as_str, _pluck, uisp_device_summary

logger = logging.getLogger(__name__)

JOB_UISP = "collect_uisp"
#: Au-dela, on ne lit pas les ports de chaque equipement a chaque minute.
MAX_DEVICES = 600
PARALLEL = 6
RETENTION_DAYS = 7

_SANS_FIL = ("ath", "wlan", "wifi", "wl", "air")


def _bps(valeur: Any) -> float | None:
    nombre = _as_float(valeur)
    return nombre if nombre is not None and nombre >= 0 else None


def interface_rates(
    interfaces: list[dict[str, Any]],
) -> tuple[float | None, float | None, str | None]:
    """(rx_bps, tx_bps, port) d'un equipement : son port RADIO s'il en a un,
    sinon celui qui porte le plus de trafic."""
    candidats: list[tuple[bool, float, float | None, float | None, str | None]] = []
    for iface in interfaces:
        stats = iface.get("statistics") or {}
        rx = _bps(_pluck(stats, "rxrate", "rxRate", "rx_rate", "rxBps"))
        tx = _bps(_pluck(stats, "txrate", "txRate", "tx_rate", "txBps"))
        if rx is None and tx is None:
            continue
        nom = _as_str(_pluck(iface, "identification.name", "name", "identification.displayName"))
        genre = str(_pluck(iface, "identification.type", "type") or "").lower()
        radio = genre in {"wlan", "wireless", "wifi", "air"} or (
            nom is not None and nom.lower().startswith(_SANS_FIL)
        )
        candidats.append((radio, (rx or 0) + (tx or 0), rx, tx, nom))
    if not candidats:
        return None, None, None
    candidats.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _radio, _total, rx, tx, nom = candidats[0]
    return rx, tx, nom


def parse_station(station: dict[str, Any]) -> dict[str, Any]:
    """Un CPE vu par son AP : de quoi le retrouver (MAC, IP, nom) et le juger."""
    return {
        "mac": _as_str(_pluck(station, "mac", "deviceIdentification.mac", "identification.mac")),
        "ip": _as_str(_pluck(station, "ipAddress", "ip", "deviceIdentification.ipAddress")),
        "name": _as_str(
            _pluck(station, "name", "deviceIdentification.name", "identification.name", "hostname")
        ),
        "model": _as_str(_pluck(station, "model", "deviceIdentification.model")),
        "signal_dbm": _as_float(_pluck(station, "rxSignal", "signal", "rssi", "signalLevel")),
        "rx_bps": _bps(_pluck(station, "rxRate", "statistics.rxrate", "rxBps")),
        "tx_bps": _bps(_pluck(station, "txRate", "statistics.txrate", "txBps")),
        "distance_m": _as_float(_pluck(station, "distance", "distanceMeters")),
        "uptime_s": _as_float(_pluck(station, "uptime", "statistics.uptime")),
    }


def is_access_point(resume: dict[str, Any]) -> bool:
    role = str(resume.get("role") or "").lower()
    return role in {"ap", "accesspoint", "access-point"}


class UispTraffic:
    def __init__(self, repo: Any, pool: Any | None = None, *, timeout_s: float = 10.0) -> None:
        self._repo = repo
        self._pool = pool
        self._timeout_s = timeout_s
        self.devices: dict[str, dict[str, Any]] = {}
        self.stations: dict[str, list[dict[str, Any]]] = {}
        self.collected_at: datetime | None = None
        self.error: str | None = None

    async def collect(self) -> dict[str, Any]:
        acces = await self._repo.credentials() if self._repo is not None else None
        if acces is None:
            self.devices, self.stations, self.error = {}, {}, None
            return {"devices": 0}
        url, jeton, verifier = acces
        fournisseur = UispProvider(url, jeton, verify_tls=verifier, timeout_s=self._timeout_s)
        stations: dict[str, list[dict[str, Any]]] = {}
        try:
            bruts = await fournisseur.raw_devices()
            limite = asyncio.Semaphore(PARALLEL)

            async def lire(resume: dict[str, Any]) -> None:
                ident = resume["id"]
                async with limite:
                    try:
                        rx, tx, port = interface_rates(await fournisseur.device_interfaces(ident))
                        resume.update(rx_bps=rx, tx_bps=tx, port=port)
                    except Exception as exc:  # noqa: BLE001 - un port illisible n'arrete rien
                        logger.debug("Ports UISP de %s illisibles : %s", ident, exc)
                    if is_access_point(resume):
                        try:
                            stations[ident] = [
                                parse_station(s) for s in await fournisseur.device_stations(ident)
                            ]
                            resume["stations_count"] = len(stations[ident])
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Stations UISP de %s illisibles : %s", ident, exc)

            resumes = []
            for brut in bruts[:MAX_DEVICES]:
                resume = uisp_device_summary(brut)
                if not resume.get("id"):
                    continue
                parent = _pluck(brut, "attributes.apDevice.id")
                resume["ap_id"] = str(parent) if parent else None
                resume.update(rx_bps=None, tx_bps=None, port=None, stations_count=None)
                resumes.append(resume)
            await asyncio.gather(*(lire(r) for r in resumes))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            if self._repo is not None:
                await self._repo.mark(ok=False, error=self.error)
            raise
        finally:
            await fournisseur.aclose()
        self.collected_at = datetime.now(tz=UTC)
        for resume in resumes:
            resume["ts"] = self.collected_at
        self.devices = {r["id"]: r for r in resumes}
        self.stations = stations
        self.error = None
        await self._repo.mark(ok=True, count=len(resumes))
        await self._historiser(resumes)
        return {"devices": len(resumes)}

    async def _historiser(self, resumes: list[dict[str, Any]]) -> None:
        if self._pool is None or not resumes:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO uisp_device_metrics (ts, device_id, rx_bps, tx_bps,
                           capacity_down_mbps, capacity_up_mbps, signal_dbm, online)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    [
                        (
                            r["ts"],
                            r["id"],
                            r.get("rx_bps"),
                            r.get("tx_bps"),
                            r.get("capacity_down_mbps"),
                            r.get("capacity_up_mbps"),
                            r.get("signal_dbm"),
                            bool(r.get("online")),
                        )
                        for r in resumes
                    ],
                )
                await conn.execute(
                    "DELETE FROM uisp_device_metrics WHERE ts < now() - make_interval(days => $1)",
                    RETENTION_DAYS,
                )
        except Exception:  # noqa: BLE001 - l'historique ne bloque jamais l'etat courant
            logger.exception("Historique UISP non ecrit")

    async def history(self, device_id: str, minutes: int) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, rx_bps, tx_bps, capacity_down_mbps, capacity_up_mbps, signal_dbm, online
                  FROM uisp_device_metrics
                 WHERE device_id = $1 AND ts >= now() - make_interval(mins => $2)
                 ORDER BY ts
                """,
                device_id,
                minutes,
            )
        return [dict(r) for r in rows]

    def radio_antennas(self) -> list[dict[str, Any]]:
        """Les AP UISP au format du diagnostic radio d'un client."""
        return [
            {
                "key": f"uisp:{ident}",
                "name": (self.devices.get(ident) or {}).get("name") or ident,
                "stations": stations,
            }
            for ident, stations in self.stations.items()
        ]

    async def problems(self) -> list[dict[str, Any]]:
        """Pour la surveillance : une AP ou un equipement rattache a un site,
        hors ligne."""
        if not self.devices:
            return []
        rattaches = set()
        if self._repo is not None:
            rattaches = {lien["device_id"] for lien in await self._repo.links()}
        sortie = []
        for ident, d in self.devices.items():
            if d.get("online") or not (is_access_point(d) or ident in rattaches):
                continue
            sortie.append(
                {
                    "key": f"uisp:{ident}",
                    "category": "uisp",
                    "level": "crit" if is_access_point(d) else "warn",
                    "title": f"UISP {d.get('name') or ident} offline",
                    "detail": " · ".join(
                        x for x in (d.get("model"), d.get("site"), d.get("status")) if x
                    )
                    or "reported offline by UISP",
                }
            )
        return sortie

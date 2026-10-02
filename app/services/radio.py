"""Sante radio d'une AP et de ses CPE : ce qui ne va pas, dit en clair.

Les seuils sont ceux qu'un technicien radio applique de tete ; les reunir ici
evite qu'une page les invente et qu'une autre les contredise.
"""

from __future__ import annotations

from typing import Any

#: Marge signal/bruit sous laquelle la modulation s'effondre.
SNR_FAIBLE_DB = 20.0
#: Signal sous lequel un CPE est mal aligne ou trop loin.
SIGNAL_FAIBLE_DBM = -75.0
#: Qualite de transmission (CCQ) sous laquelle les retransmissions coutent.
CCQ_FAIBLE_PCT = 75.0
#: Temps d'antenne occupe au-dela duquel l'AP est saturee (airMAX).
AIRTIME_SATURE_PCT = 70.0


def radio_issues(radio: dict[str, Any] | None) -> list[str]:
    if not radio:
        return []
    problemes: list[str] = []
    if radio.get("snr_db") is not None and radio["snr_db"] < SNR_FAIBLE_DB:
        problemes.append(f"low SNR {radio['snr_db']:.0f} dB (noise {radio.get('noise_dbm')} dBm)")
    if radio.get("airtime_pct") is not None and radio["airtime_pct"] >= AIRTIME_SATURE_PCT:
        problemes.append(f"airtime {radio['airtime_pct']:.0f}%: the AP is saturated")
    if radio.get("ccq_pct") is not None and radio["ccq_pct"] < CCQ_FAIBLE_PCT:
        problemes.append(f"CCQ {radio['ccq_pct']:.0f}%: many retransmissions")
    return problemes


def station_issues(station: dict[str, Any]) -> list[str]:
    problemes: list[str] = []
    signal = station.get("signal_dbm")
    if signal is not None and signal < SIGNAL_FAIBLE_DBM:
        problemes.append(f"weak signal {signal:.0f} dBm")
    if station.get("snr_db") is not None and station["snr_db"] < SNR_FAIBLE_DB:
        problemes.append(f"low SNR {station['snr_db']:.0f} dB")
    if station.get("ccq_pct") is not None and station["ccq_pct"] < CCQ_FAIBLE_PCT:
        problemes.append(f"CCQ {station['ccq_pct']:.0f}%")
    return problemes

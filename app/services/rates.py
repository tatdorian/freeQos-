"""Derivation des debits a partir de compteurs cumulatifs.

Les compteurs d'interface RouterOS sont monotones... jusqu'a la reconnexion :
une session PPPoE qui se relance recree l'interface dynamique et remet les
compteurs a zero. Sans detection, on ecrirait un debit negatif ou, pire, un pic
absurde apres un debordement.

Trois garde-fous :
  1. l'uptime qui recule est le signal le plus fiable d'une nouvelle session ;
  2. un compteur qui decroit signale un reset (ou un redemarrage du routeur) ;
  3. un debit superieur a un plafond configurable est rejete plutot qu'ecrit.

En cas de doute on n'ecrit PAS de debit (None) : un trou dans la serie se voit
et se comble, une valeur fausse pollue durablement les moyennes et le futur
score QoE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CounterSnapshot:
    ts: float
    rx_bytes: int | None
    tx_bytes: int | None
    uptime_s: int | None


@dataclass(slots=True)
class RateResult:
    rx_bps: float | None
    tx_bps: float | None
    reason: str | None = None  # renseigne uniquement quand aucun debit n'est produit


class RateTracker:
    """Convertit des compteurs cumulatifs en debits, par cle de serie."""

    def __init__(
        self,
        *,
        max_plausible_bps: float = 100_000_000_000.0,
        min_interval_s: float = 1.0,
    ) -> None:
        self.max_plausible_bps = max_plausible_bps
        self.min_interval_s = min_interval_s
        self._state: dict[str, CounterSnapshot] = {}
        self.resets_detected = 0

    def __len__(self) -> int:
        return len(self._state)

    def forget(self, key: str) -> None:
        self._state.pop(key, None)

    def prune(self, active_keys: set[str]) -> int:
        """Oublie les sessions disparues pour que l'etat ne croisse pas sans fin."""
        stale = self._state.keys() - active_keys
        for key in stale:
            del self._state[key]
        return len(stale)

    def update(
        self,
        key: str,
        *,
        ts: float,
        rx_bytes: int | None,
        tx_bytes: int | None,
        uptime_s: int | None = None,
    ) -> RateResult:
        previous = self._state.get(key)
        current = CounterSnapshot(ts=ts, rx_bytes=rx_bytes, tx_bytes=tx_bytes, uptime_s=uptime_s)

        if rx_bytes is None and tx_bytes is None:
            # Interface non correlee : on garde la session mais sans debit.
            self._state[key] = current
            return RateResult(None, None, "compteurs absents")

        if previous is None:
            self._state[key] = current
            return RateResult(None, None, "premiere mesure")

        if self._session_restarted(previous, current):
            self.resets_detected += 1
            self._state[key] = current
            logger.debug("Reset de compteurs detecte pour %s", key)
            return RateResult(None, None, "session redemarree")

        delta_t = ts - previous.ts
        if delta_t < self.min_interval_s:
            # Intervalle trop court : on conserve l'ancien point de reference
            # plutot que de diviser par un delta bruite.
            return RateResult(None, None, "intervalle trop court")

        rx_bps = _rate(previous.rx_bytes, rx_bytes, delta_t)
        tx_bps = _rate(previous.tx_bytes, tx_bytes, delta_t)
        self._state[key] = current

        if rx_bps is not None and rx_bps > self.max_plausible_bps:
            logger.warning("Debit rx aberrant pour %s (%.0f bps), echantillon rejete", key, rx_bps)
            rx_bps = None
        if tx_bps is not None and tx_bps > self.max_plausible_bps:
            logger.warning("Debit tx aberrant pour %s (%.0f bps), echantillon rejete", key, tx_bps)
            tx_bps = None

        if rx_bps is None and tx_bps is None:
            return RateResult(None, None, "aucun debit exploitable")
        return RateResult(rx_bps, tx_bps)

    @staticmethod
    def _session_restarted(previous: CounterSnapshot, current: CounterSnapshot) -> bool:
        if (
            previous.uptime_s is not None
            and current.uptime_s is not None
            and current.uptime_s < previous.uptime_s
        ):
            return True
        pairs = (
            (previous.rx_bytes, current.rx_bytes),
            (previous.tx_bytes, current.tx_bytes),
        )
        for old, new in pairs:
            if old is not None and new is not None and new < old:
                return True
        return False


def _rate(previous: int | None, current: int | None, delta_t: float) -> float | None:
    if previous is None or current is None or delta_t <= 0:
        return None
    return (current - previous) * 8.0 / delta_t

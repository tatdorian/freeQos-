"""Orchestration d'un cycle de collecte.

C'est la boucle CENTRALE LENTE du systeme : elle collecte, normalise, resout les
identifiants et ecrit. Elle ne reagit pas au temps reel.

La boucle LOCALE RAPIDE (reaction aux fades radio a la latence) vit sur le PoP et
n'est deliberement PAS implementee ici : cette application se contente de fixer et
de tenir a jour les baselines que cette boucle locale respectera.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.collectors.mikrotik import MikrotikCollector
from app.collectors.radius import PlanProvider
from app.collectors.uisp import BackhaulCapacityProvider
from app.config import BackhaulConfig, Settings
from app.db.directory import Directory
from app.db.writer import MetricsWriter
from app.models import BackhaulSample, Plan, PppoeSession, RunResult, SubscriberSample
from app.services.rates import RateTracker
from app.services.rtt import RttProber

logger = logging.getLogger(__name__)

JOB_SUBSCRIBERS = "collect_subscribers"
JOB_BACKHAULS = "collect_backhauls"
JOB_PLANS = "refresh_plans"
JOB_INVENTORY = "reload_inventory"
JOB_RTT = "probe_rtt"


class CollectionService:
    def __init__(
        self,
        settings: Settings,
        *,
        collectors: Sequence[MikrotikCollector],
        backhaul_provider: BackhaulCapacityProvider,
        plan_provider: PlanProvider,
        directory: Directory,
        writer: MetricsWriter,
        backhauls: Sequence[BackhaulConfig] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rtt_prober: RttProber | None = None,
    ) -> None:
        self.settings = settings
        self.collectors = list(collectors)
        self.backhaul_provider = backhaul_provider
        self.plan_provider = plan_provider
        self.directory = directory
        self.writer = writer
        self.backhauls = list(backhauls if backhauls is not None else settings.enabled_backhauls)
        # Horloge monotone injectable : elle sert a dater les intervalles entre
        # compteurs, jamais les enregistrements (qui portent un horodatage UTC).
        # L'injecter rend les tests de debit deterministes sans toucher au module time.
        self._clock = clock

        self.rates = RateTracker(
            max_plausible_bps=settings.max_plausible_bps,
            min_interval_s=settings.min_rate_interval_s,
        )
        self._known_logins: set[str] = set()
        self.last_results: dict[str, RunResult] = {}
        # Sonde de latence optionnelle. Sans elle, rtt_ms reste NULL : la colonne
        # existe depuis la phase 1, elle attendait juste une source.
        self.rtt_prober = rtt_prober
        # Cibles du prochain tour de sonde, rafraichies a chaque cycle.
        self._rtt_targets: list[tuple[int, str, MikrotikCollector]] = []

    def set_collectors(self, collectors: Sequence[MikrotikCollector]) -> None:
        """Remplace l'ensemble des collecteurs a chaud.

        Appele par le RouterRegistry apres un ajout ou une suppression de PoP
        depuis l'interface. Le RateTracker n'est PAS purge : ses cles sont
        prefixees par le nom du routeur, donc les series des routeurs conserves
        gardent leur point de reference, et celles des routeurs retires seront
        eliminees au prochain prune.
        """
        self.collectors = list(collectors)

    # ------------------------------------------------------------------
    # Abonnes
    # ------------------------------------------------------------------
    async def collect_subscribers(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []

        # Les routeurs sont interroges en parallele : un PoP injoignable ne doit
        # pas retarder ni annuler la collecte des autres.
        gathered = await asyncio.gather(
            *(collector.collect() for collector in self.collectors),
            return_exceptions=True,
        )

        sessions_by_router: list[tuple[MikrotikCollector, list[PppoeSession]]] = []
        for collector, outcome in zip(self.collectors, gathered, strict=True):
            if isinstance(outcome, BaseException):
                message = f"{collector.name}: {type(outcome).__name__}: {outcome}"
                errors.append(message)
                logger.error("Collecte impossible sur %s : %s", collector.name, outcome)
                continue
            sessions_by_router.append((collector, outcome))

        rows: list[tuple[int, SubscriberSample]] = []
        seen: dict[int, tuple[str | None, datetime]] = {}
        active_keys: set[str] = set()
        rtt_targets: list[tuple[int, str, MikrotikCollector]] = []

        for collector, sessions in sessions_by_router:
            try:
                pop_id = await self.directory.ensure_pop(
                    collector.config.effective_pop_name, collector.config.host
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{collector.name}: PoP non resolu: {exc}")
                logger.exception("Resolution du PoP impossible pour %s", collector.name)
                continue

            plans = await self._plans_for_new_logins([s.login for s in sessions])

            for session in sessions:
                key = f"{collector.name}/{session.login}"
                active_keys.add(key)
                try:
                    subscriber_id = await self.directory.ensure_subscriber(
                        session.login, pop_id=pop_id, plan=plans.get(session.login)
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{session.login}: abonne non resolu: {exc}")
                    continue
                self._known_logins.add(session.login)

                rate = self.rates.update(
                    key,
                    ts=monotonic,
                    rx_bytes=session.rx_bytes,
                    tx_bytes=session.tx_bytes,
                    uptime_s=session.uptime_s,
                )
                rows.append(
                    (
                        subscriber_id,
                        SubscriberSample(
                            ts=started_at,
                            login=session.login,
                            router_name=session.router_name,
                            pop_name=session.pop_name,
                            address=session.address,
                            uptime_s=session.uptime_s,
                            rx_bytes=session.rx_bytes,
                            tx_bytes=session.tx_bytes,
                            rx_bps=rate.rx_bps,
                            tx_bps=rate.tx_bps,
                            # Derniere mesure de latence si elle n'est pas perimee.
                            rtt_ms=(
                                self.rtt_prober.get(subscriber_id)
                                if self.rtt_prober is not None
                                else None
                            ),
                        ),
                    )
                )
                seen[subscriber_id] = (session.address, started_at)
                if session.address:
                    rtt_targets.append((subscriber_id, session.address, collector))

        self.rates.prune(active_keys)
        self._rtt_targets = rtt_targets
        if self.rtt_prober is not None:
            self.rtt_prober.forget_all_but({sid for sid, _, _ in rtt_targets})

        written = 0
        try:
            written = await self.writer.write_subscriber_metrics(rows)
            await self.directory.touch_subscribers(seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Ecriture des metriques abonnes impossible")

        result = RunResult(
            job=JOB_SUBSCRIBERS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=written,
            errors=errors,
        )
        await self._finalize(result)
        return result

    async def _plans_for_new_logins(self, logins: Sequence[str]) -> dict[str, Plan]:
        """Ne demande un plan que pour les logins jamais vus.

        Interroger RADIUS pour tous les abonnes a chaque cycle de 10 s serait
        inutile et couteux : les plans changent rarement, et le job dedie
        ``refresh_plans`` s'occupe de leur rafraichissement periodique.
        """
        unknown = [login for login in logins if login not in self._known_logins]
        if not unknown:
            return {}
        try:
            return dict(await self.plan_provider.get_plans(unknown))
        except Exception:  # noqa: BLE001
            # Un plan manquant ne doit pas empecher d'ecrire les metriques.
            logger.exception("Recuperation des plans impossible pour %d login(s)", len(unknown))
            return {}

    # ------------------------------------------------------------------
    # Backhauls
    # ------------------------------------------------------------------
    async def collect_backhauls(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        written = 0

        configured = [b for b in self.backhauls if b.uisp_device_id]
        if not configured:
            result = RunResult(JOB_BACKHAULS, started_at, self._clock() - monotonic, True, 0)
            await self._finalize(result)
            return result

        samples: dict[str, BackhaulSample] = {}
        try:
            samples = await self.backhaul_provider.get_capacities(
                [b.uisp_device_id for b in configured if b.uisp_device_id]
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fournisseur de capacite: {exc}")
            logger.exception("Lecture de la capacite backhaul impossible")

        rows: list[tuple[int, BackhaulSample]] = []
        for config in configured:
            sample = samples.get(config.uisp_device_id or "")
            if sample is None:
                continue
            try:
                pop_id = await self.directory.ensure_pop(config.pop_name)
                backhaul_id = await self.directory.ensure_backhaul(
                    config.name,
                    pop_id=pop_id,
                    uisp_device_id=config.uisp_device_id,
                    nominal_capacity_mbps=config.nominal_capacity_mbps,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{config.name}: backhaul non resolu: {exc}")
                continue
            rows.append((backhaul_id, sample))

        try:
            written = await self.writer.write_backhaul_metrics(rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ecriture: {exc}")
            logger.exception("Ecriture des metriques backhaul impossible")

        result = RunResult(
            job=JOB_BACKHAULS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=written,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    # Latence
    # ------------------------------------------------------------------
    async def probe_rtt(self) -> RunResult:
        """Sonde un lot d'abonnes. Les mesures sont rattachees au cycle suivant."""
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        answered = 0

        if self.rtt_prober is not None:
            try:
                answered = await self.rtt_prober.probe(self._rtt_targets)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                logger.exception("Sonde de latence impossible")

        result = RunResult(
            job=JOB_RTT,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=answered,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------
    async def refresh_plans(self) -> RunResult:
        started_at = _utcnow()
        monotonic = self._clock()
        errors: list[str] = []
        updated = 0

        try:
            logins = await self.directory.list_subscriber_logins()
            self._known_logins.update(logins)
            if logins:
                plans = await self.plan_provider.get_plans(list(logins))
                updated = await self.directory.update_plans(
                    {logins[login]: plan for login, plan in plans.items() if login in logins}
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            logger.exception("Rafraichissement des plans impossible")

        result = RunResult(
            job=JOB_PLANS,
            started_at=started_at,
            duration_s=self._clock() - monotonic,
            ok=not errors,
            items=updated,
            errors=errors,
        )
        await self._finalize(result)
        return result

    # ------------------------------------------------------------------
    async def _finalize(self, result: RunResult) -> None:
        self.last_results[result.job] = result
        try:
            await self.writer.record_run(result)
        except Exception:  # noqa: BLE001
            # L'historique d'execution est un confort d'exploitation, jamais un
            # motif d'echec du cycle.
            logger.warning("Historisation du run '%s' impossible", result.job)

    async def aclose(self) -> None:
        for collector in self.collectors:
            try:
                collector.close()
            except Exception:  # noqa: BLE001
                pass
        await self.backhaul_provider.aclose()
        await self.plan_provider.aclose()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)

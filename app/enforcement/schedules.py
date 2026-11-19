"""Profils de debit horaires : jour, soirée, nuit.

LE BESOIN. Un operateur vend a ses abonnes des debits qui changent avec
l'heure : un forfait bride en soiree, ouvert la nuit, normal le jour. Le
changement doit se faire TOUT SEUL, a l'heure dite, sans qu'un humain
reclique trois fois par jour.

CE QU'EST UN PROFIL. Un ensemble de PLAGES HORAIRES (« 18:00-23:00, lun-ven ») et,
pour chacune, le debit qui s'applique pendant la plage :

  - un debit ABSOLU (``down_mbps`` / ``up_mbps``) ;
  - ou un POURCENTAGE du forfait (``percent`` : 200 = le double la nuit, 70 =
    un resserrage en soiree), qui suit chaque forfait sans le recopier ;
  - ou un MODE de debit (``mode_id`` : « Backup », « Night »...), le meme que
    l'on donne a un client a la main. Le profil garde la REFERENCE du mode, pas
    son debit : modifier le mode change aussi les plages qui l'utilisent.

Hors de toute plage, c'est le forfait normal. Un profil s'adresse a TOUS les
abonnes, a des SITES, a des FORFAITS (par debit descendant souscrit) ou a des
abonnes nommes.

OU IL SE PLACE DANS LA HIERARCHIE. Entre la surcharge et le plan :

    boost > surcharge manuelle > profil horaire > forfait

Une surcharge est une decision prise sur CET abonne (bride pour impaye,
plafond de test) : un profil general ne doit jamais la defaire. Un boost reste
le geste ponctuel qui passe par-dessus tout, puis s'efface.

UN SEUL PROFIL PAR ABONNE. Quand plusieurs profils visent le meme abonne, le
plus PRECIS gouverne seul : abonnes nommes, puis forfaits, puis sites, puis
tous. A precision egale, le plus recent. Pas de composition entre profils :
l'exploitant doit pouvoir dire, abonne par abonne, d'ou vient son debit.

LES HEURES SONT LOCALES. Chaque profil porte son fuseau (IANA, ex.
``Africa/Abidjan``, ``Europe/Paris``) : « 18:00 » est l'heure des abonnes, pas
celle du serveur, et un changement d'heure d'ete ne decale rien.

Ce module est PUR : ni base, ni routeur. Il dit, pour un abonne et un instant,
quel debit horaire s'applique -- c'est tout ce dont le planificateur a besoin,
et ce qui le rend entierement testable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.vlan_sites import site_key

#: Jours, dans l'ordre de ``datetime.weekday()`` (lundi = 0).
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = frozenset(range(7))

SCOPE_ALL = "all"
SCOPE_POPS = "pops"
SCOPE_PLANS = "plans"
SCOPE_SUBSCRIBERS = "subscribers"
#: Les clients d'un ou plusieurs MODES (cibles : identifiants des modes).
SCOPE_MODES = "modes"
#: Les clients a leur PLAN, sans mode.
SCOPE_ON_PLAN = "on_plan"
SCOPES = (SCOPE_ALL, SCOPE_POPS, SCOPE_PLANS, SCOPE_SUBSCRIBERS, SCOPE_MODES, SCOPE_ON_PLAN)

#: Precision d'une portee : la plus petite gouverne.
_PRECISION = {
    SCOPE_SUBSCRIBERS: 0,
    SCOPE_MODES: 1,
    SCOPE_PLANS: 1,
    SCOPE_ON_PLAN: 2,
    SCOPE_POPS: 2,
    SCOPE_ALL: 3,
}

MINUTES_PAR_JOUR = 24 * 60
MINUTES_PAR_SEMAINE = 7 * MINUTES_PAR_JOUR
MAX_WINDOWS = 24
MAX_RATE_MBPS = 100_000.0
MAX_PERCENT = 1000.0

_HEURE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


class InvalidScheduleError(ValueError):
    """Un profil qu'on refuse d'enregistrer, avec la raison en clair."""


def parse_time(value: Any, *, allow_24: bool = False) -> int:
    """``"18:30"`` -> minutes depuis minuit. ``"24:00"`` seulement en fin de plage."""
    texte = str(value or "")
    trouve = _HEURE.match(texte)
    if not trouve:
        raise InvalidScheduleError(f"invalid time '{texte}': expected HH:MM")
    heures, minutes = int(trouve.group(1)), int(trouve.group(2))
    if minutes > 59 or heures > 24 or (heures == 24 and (minutes or not allow_24)):
        raise InvalidScheduleError(f"invalid time '{texte}'")
    return heures * 60 + minutes


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_days(value: Any) -> frozenset[int]:
    """``["mon", "fri"]`` -> {0, 4}. Vide ou absent : tous les jours."""
    if value is None or value == [] or value == "":
        return ALL_DAYS
    morceaux = value.replace(",", " ").split() if isinstance(value, str) else list(value)
    jours: set[int] = set()
    for morceau in morceaux:
        cle = str(morceau).strip().lower()[:3]
        if cle not in DAYS:
            raise InvalidScheduleError(f"unknown day '{morceau}': use {', '.join(DAYS)}")
        jours.add(DAYS.index(cle))
    return frozenset(jours) or ALL_DAYS


def validate_timezone(value: Any) -> str:
    nom = str(value or "").strip() or "UTC"
    try:
        ZoneInfo(nom)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidScheduleError(f"unknown time zone '{nom}' (IANA name expected)") from exc
    return nom


@dataclass(frozen=True, slots=True)
class Window:
    """Une plage horaire et le debit qui s'y applique.

    ``end_min <= start_min`` : la plage passe minuit (22:00-06:00) et finit le
    LENDEMAIN. ``days`` designe les jours ou la plage COMMENCE : une nuit du
    vendredi court jusqu'au samedi matin.
    """

    start_min: int
    end_min: int
    days: frozenset[int] = ALL_DAYS
    down_mbps: float | None = None
    up_mbps: float | None = None
    percent: float | None = None
    label: str | None = None
    #: Pourcentage du forfait MONTANT, quand il differe du descendant (un mode
    #: en pourcentage porte deux valeurs). None : ``percent`` vaut pour les deux.
    percent_up: float | None = None
    mode_id: int | None = None
    mode_name: str | None = None
    #: La plage donne un mode d'URGENCE : chaque client y recoit sa propre
    #: bande passante d'urgence quand il en a une.
    emergency: bool = False

    def minutes(self) -> list[tuple[int, int]]:
        """Les intervalles couverts dans la semaine, en minutes depuis lundi 00:00."""
        duree = (self.end_min - self.start_min) % MINUTES_PAR_JOUR or MINUTES_PAR_JOUR
        return [(jour * MINUTES_PAR_JOUR + self.start_min, duree) for jour in sorted(self.days)]

    def covers(self, jour: int, minute: int) -> bool:
        """La plage est-elle en cours ce jour-la, a cette minute (heure locale) ?"""
        instant = jour * MINUTES_PAR_JOUR + minute
        for debut, duree in self.minutes():
            if (instant - debut) % MINUTES_PAR_SEMAINE < duree:
                return True
        return False

    def rates(
        self, plan_down: float | None, plan_up: float | None
    ) -> tuple[float | None, float | None]:
        """Le debit de la plage pour CE forfait. None : ce sens garde son forfait."""
        if self.percent is not None:
            facteur = self.percent / 100.0
            facteur_haut = (self.percent_up if self.percent_up is not None else self.percent) / 100
            return (
                plan_down * facteur if plan_down else None,
                plan_up * facteur_haut if plan_up else None,
            )
        return self.down_mbps, self.up_mbps

    def describe(self) -> str:
        jours = (
            "every day" if self.days == ALL_DAYS else ",".join(DAYS[j] for j in sorted(self.days))
        )
        heures = f"{format_time(self.start_min)}-{format_time(self.end_min or MINUTES_PAR_JOUR)}"
        return f"{self.label + ' ' if self.label else ''}{heures} {jours}"

    def to_dict(self) -> dict[str, Any]:
        if self.mode_id is not None:
            # Une plage en mode s'enregistre par la REFERENCE du mode : son debit
            # est relu a chaque chargement, il suit les modifications du mode.
            return {
                "label": self.label,
                "start": format_time(self.start_min),
                "end": format_time(self.end_min or MINUTES_PAR_JOUR),
                "days": [DAYS[j] for j in sorted(self.days)],
                "down_mbps": None,
                "up_mbps": None,
                "percent": None,
                "mode_id": self.mode_id,
                "mode_name": self.mode_name,
            }
        return {
            "label": self.label,
            "start": format_time(self.start_min),
            # Minuit en FIN de plage s'ecrit 24:00 : c'est ainsi qu'on le saisit.
            "end": format_time(self.end_min or MINUTES_PAR_JOUR),
            "days": [DAYS[j] for j in sorted(self.days)],
            "down_mbps": self.down_mbps,
            "up_mbps": self.up_mbps,
            "percent": self.percent,
        }


def _taux(value: Any, nom: str, *, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        nombre = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidScheduleError(f"{nom}: not a number ({value!r})") from exc
    if not 0 < nombre <= maximum:
        raise InvalidScheduleError(f"{nom}: must be > 0 and <= {maximum:g}")
    return nombre


#: Les modes de debit connus, par identifiant : ``{id: {"name", "rate_kind",
#: "down_value", "up_value"}}``. Le module reste pur : c'est l'appelant qui les lit.
Modes = dict[int, dict[str, Any]]


def _plage_en_mode(raw: dict[str, Any], modes: Modes | None) -> dict[str, Any]:
    """Le debit d'une plage qui designe un mode, lu dans ce mode."""
    try:
        ident = int(raw["mode_id"])
    except (TypeError, ValueError) as exc:
        raise InvalidScheduleError(f"mode_id: not a mode id ({raw['mode_id']!r})") from exc
    if any(raw.get(c) not in (None, "") for c in ("percent", "down_mbps", "up_mbps")):
        raise InvalidScheduleError(
            "a time slot sets a mode OR a rate (percentage or Mbps), not both"
        )
    mode = (modes or {}).get(ident)
    if mode is None:
        raise InvalidScheduleError(f"unknown bandwidth mode {ident}: it was deleted?")
    bas, haut = float(mode["down_value"]), float(mode["up_value"])
    commun = {
        "mode_id": ident,
        "mode_name": str(mode.get("name") or f"mode {ident}"),
        "emergency": bool(mode.get("emergency")),
    }
    if mode.get("rate_kind") == "percent":
        return {**commun, "percent": bas, "percent_up": haut}
    return {**commun, "down_mbps": bas, "up_mbps": haut}


def parse_window(raw: dict[str, Any], modes: Modes | None = None) -> Window:
    """Une plage saisie -> ``Window``, ou une erreur qui dit quoi corriger."""
    if not isinstance(raw, dict):
        raise InvalidScheduleError("each time slot must be an object")
    debut = parse_time(raw.get("start"))
    fin = parse_time(raw.get("end"), allow_24=True)
    if fin == MINUTES_PAR_JOUR:
        fin = 0
    if debut == fin and not (debut == 0 and str(raw.get("end", "")).strip() == "24:00"):
        raise InvalidScheduleError(
            f"time slot {format_time(debut)}-{format_time(fin)} is empty: "
            "use 00:00-24:00 for a whole day"
        )
    libelle = str(raw.get("label") or "").strip()[:64] or None
    if raw.get("mode_id") not in (None, ""):
        return Window(
            start_min=debut,
            end_min=fin,
            days=parse_days(raw.get("days")),
            label=libelle,
            **_plage_en_mode(raw, modes),
        )
    pourcent = _taux(raw.get("percent"), "percent", maximum=MAX_PERCENT)
    bas = _taux(raw.get("down_mbps"), "down_mbps", maximum=MAX_RATE_MBPS)
    haut = _taux(raw.get("up_mbps"), "up_mbps", maximum=MAX_RATE_MBPS)
    if pourcent is not None and (bas is not None or haut is not None):
        raise InvalidScheduleError(
            "a time slot sets EITHER a percentage of the plan OR absolute rates, not both"
        )
    if pourcent is None and bas is None and haut is None:
        raise InvalidScheduleError(
            "a time slot needs a rate: a percentage of the plan, down/up Mbps, or a mode"
        )
    return Window(
        start_min=debut,
        end_min=fin,
        days=parse_days(raw.get("days")),
        down_mbps=bas,
        up_mbps=haut,
        percent=pourcent,
        label=libelle,
    )


def parse_windows(raws: Sequence[dict[str, Any]], modes: Modes | None = None) -> tuple[Window, ...]:
    """Toutes les plages d'un profil, sans recouvrement.

    DEUX PLAGES QUI SE CHEVAUCHENT SONT REFUSEES. Laquelle gagnerait a 19h si
    « soiree 18:00-23:00 » et « pointe 19:00-21:00 » coexistaient ? Toute regle
    de priorite serait une surprise ; le refus a la saisie n'en est pas une.
    """
    if not raws:
        raise InvalidScheduleError("a schedule needs at least one time slot")
    if len(raws) > MAX_WINDOWS:
        raise InvalidScheduleError(f"at most {MAX_WINDOWS} time slots per schedule")
    plages = tuple(parse_window(raw, modes) for raw in raws)
    occupation: dict[int, int] = {}
    for index, plage in enumerate(plages):
        for debut, duree in plage.minutes():
            for minute in range(debut, debut + duree):
                cle = minute % MINUTES_PAR_SEMAINE
                autre = occupation.get(cle)
                if autre is not None and autre != index:
                    jour, heure = divmod(cle, MINUTES_PAR_JOUR)
                    raise InvalidScheduleError(
                        f"time slots '{plages[autre].describe()}' and "
                        f"'{plage.describe()}' overlap ({DAYS[jour]} {format_time(heure)})"
                    )
                occupation[cle] = index
    return plages


def _cible_forfait(value: Any) -> str:
    try:
        nombre = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidScheduleError(
            f"plan '{value}': give the plan's download rate in Mbps (e.g. 20)"
        ) from exc
    if nombre <= 0:
        raise InvalidScheduleError(f"plan '{value}': must be > 0")
    return f"{nombre:g}"


def normalise_targets(scope: str, targets: Iterable[Any] | None) -> tuple[str, ...]:
    """Les cibles d'une portee, sous la forme ou elles seront comparees."""
    if scope not in SCOPES:
        raise InvalidScheduleError(f"unknown scope '{scope}': {', '.join(SCOPES)}")
    valeurs = [str(t).strip() for t in (targets or []) if str(t).strip()]
    if scope in (SCOPE_ALL, SCOPE_ON_PLAN):
        return ()
    if not valeurs:
        raise InvalidScheduleError(f"scope '{scope}' needs at least one target")
    if scope == SCOPE_PLANS:
        valeurs = [_cible_forfait(v) for v in valeurs]
    if scope == SCOPE_MODES:
        try:
            valeurs = [str(int(float(v))) for v in valeurs]
        except ValueError as exc:
            raise InvalidScheduleError("modes: give the identifiers of the modes") from exc
    vues: list[str] = []
    for valeur in valeurs:
        if valeur not in vues:
            vues.append(valeur)
    return tuple(vues)


@dataclass(frozen=True, slots=True)
class BandwidthSchedule:
    id: int
    name: str
    windows: tuple[Window, ...]
    scope: str = SCOPE_ALL
    targets: tuple[str, ...] = ()
    timezone: str = "UTC"
    enabled: bool = True
    # LANCE A LA MAIN : il s'applique tout de suite, quelle que soit l'heure,
    # jusqu'a ``manual_until`` (None : jusqu'a ce qu'on l'arrete).
    manual_on: bool = False
    manual_until: datetime | None = None
    _cles_sites: frozenset[str] = field(default=frozenset(), repr=False, compare=False)

    @classmethod
    def build(
        cls,
        *,
        id: int,  # noqa: A002 - nom de la colonne
        name: str,
        windows: Sequence[dict[str, Any]],
        scope: str = SCOPE_ALL,
        targets: Iterable[Any] | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        modes: Modes | None = None,
        manual_on: bool = False,
        manual_until: datetime | None = None,
    ) -> BandwidthSchedule:
        cibles = normalise_targets(scope, targets)
        sites = frozenset(site_key(c) for c in cibles) if scope == SCOPE_POPS else frozenset()
        return cls(
            id=id,
            name=name,
            windows=parse_windows(windows, modes),
            scope=scope,
            targets=cibles,
            timezone=validate_timezone(timezone),
            enabled=enabled,
            manual_on=bool(manual_on),
            manual_until=manual_until,
            _cles_sites=sites,
        )

    @property
    def precision(self) -> int:
        return _PRECISION.get(self.scope, 9)

    @property
    def personal(self) -> bool:
        """Un scheduler pour UN SEUL client (fait a la main pour lui)."""
        return self.scope == SCOPE_SUBSCRIBERS and len(self.targets) == 1

    def local(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.timezone))

    def manual_active(self, now: datetime | None = None) -> bool:
        """Lance a la main, et pas encore arrive a son terme."""
        if not self.manual_on:
            return False
        if self.manual_until is None:
            return True
        return (now or datetime.now(tz=UTC)) < self.manual_until

    @property
    def manual_window(self) -> Window:
        """La plage « lancee a la main » : le debit du scheduler, a toute heure."""
        from dataclasses import replace

        return replace(
            self.windows[0], start_min=0, end_min=0, days=ALL_DAYS, label="started by hand"
        )

    def window_at(self, now: datetime) -> Window | None:
        """La plage en cours a cet instant, dans le fuseau du profil."""
        if self.manual_active(now):
            return self.manual_window
        return self._scheduled_window(now)

    def _scheduled_window(self, now: datetime) -> Window | None:
        """La plage PREVUE a cet instant, sans tenir compte d'un lancement a la main."""
        local = self.local(now)
        minute = local.hour * 60 + local.minute
        return next((w for w in self.windows if w.covers(local.weekday(), minute)), None)

    def scheduled_end(self, now: datetime, *, horizon_days: int = 8) -> datetime | None:
        """La fin PREVUE de la plage en cours, ou de la prochaine (UTC), ou None.

        « Activer maintenant, avant l'heure » : le scheduler part tout de suite
        et s'arrete a l'heure de fin qu'il a toujours eue, puis tout revient a
        la normale. None : il ne s'arrete jamais (toute la semaine).
        """
        depart = now.replace(second=0, microsecond=0)
        commence = self._scheduled_window(depart) is not None
        for pas in range(1, horizon_days * MINUTES_PAR_JOUR + 1):
            instant = depart + timedelta(minutes=pas)
            if self._scheduled_window(instant) is not None:
                commence = True
            elif commence:
                return instant
        return None

    def next_change(self, now: datetime, *, horizon_days: int = 8) -> datetime | None:
        """Le prochain instant ou la plage en cours change (UTC), ou None.

        Cherche minute par minute : c'est ce qui rend le calcul juste a travers
        un changement d'heure, sans arithmetique de fuseaux a la main.
        """
        actuelle = self.window_at(now)
        depart = now.replace(second=0, microsecond=0)
        for pas in range(1, horizon_days * MINUTES_PAR_JOUR + 1):
            instant = depart + timedelta(minutes=pas)
            if self.window_at(instant) != actuelle:
                return instant
        return None

    def governs(
        self,
        *,
        login: str,
        sites: Iterable[str | None],
        plan_down: float | None,
        mode_id: int | None = None,
    ) -> bool:
        """Ce profil vise-t-il cet abonne ? (sans regarder l'heure)"""
        if not self.enabled:
            return False
        if self.scope == SCOPE_ALL:
            return True
        if self.scope == SCOPE_ON_PLAN:
            return mode_id is None
        if self.scope == SCOPE_MODES:
            return mode_id is not None and str(int(mode_id)) in self.targets
        if self.scope == SCOPE_SUBSCRIBERS:
            return login in self.targets
        if self.scope == SCOPE_POPS:
            return any(site and site_key(site) in self._cles_sites for site in sites)
        if self.scope == SCOPE_PLANS:
            if not plan_down:
                return False
            return any(abs(float(t) - float(plan_down)) < 1e-6 for t in self.targets)
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "scope": self.scope,
            "targets": list(self.targets),
            "windows": [w.to_dict() for w in self.windows],
            "manual_on": self.manual_on,
            "manual_until": self.manual_until.isoformat() if self.manual_until else None,
        }


@dataclass(frozen=True, slots=True)
class ScheduledRate:
    """Le debit qu'un profil impose a un abonne, maintenant."""

    down_mbps: float | None
    up_mbps: float | None
    schedule_id: int
    schedule_name: str
    window: str
    #: Calcule sur la vitesse du MODE du client : il passe devant le mode.
    over_mode: bool = False
    #: Le scheduler propre a ce client (et non un scheduler de groupe).
    personal: bool = False


class ScheduleBook:
    """Les profils actifs, gardes en memoire et relus regulierement.

    Le planificateur et l'affichage des limites lisent le MEME carnet : une
    interface qui calculerait le debit autrement que ce qui sera ecrit finirait
    par mentir.
    """

    def __init__(self, schedules: Iterable[BandwidthSchedule] = ()) -> None:
        self.schedules: tuple[BandwidthSchedule, ...] = tuple(schedules)

    def replace(self, schedules: Iterable[BandwidthSchedule]) -> None:
        self.schedules = tuple(schedules)

    def governing(
        self,
        *,
        login: str,
        sites: Iterable[str | None],
        plan_down: float | None,
        mode_id: int | None = None,
        now: datetime | None = None,
    ) -> BandwidthSchedule | None:
        """LE profil qui gouverne cet abonne, parmi ceux qui courent maintenant :

        1. le scheduler PROPRE au client (fait a la main pour lui) passe devant
           tout scheduler de groupe ;
        2. puis celui lance a la main (Start now) ;
        3. puis le plus precis, puis le plus recent."""
        sites = tuple(sites)
        instant = now or datetime.now(tz=UTC)
        candidats = [
            s
            for s in self.schedules
            if s.governs(login=login, sites=sites, plan_down=plan_down, mode_id=mode_id)
        ]
        if not candidats:
            return None
        return min(
            candidats,
            key=lambda s: (
                0 if s.window_at(instant) is not None else 1,
                0 if s.personal else 1,
                0 if s.manual_active(instant) else 1,
                s.precision,
                -s.id,
            ),
        )

    def rate_for(
        self,
        *,
        login: str,
        sites: Iterable[str | None],
        plan_down: float | None,
        plan_up: float | None,
        now: datetime | None = None,
        mode_id: int | None = None,
        mode_down: float | None = None,
        mode_up: float | None = None,
        emergency: tuple[float, float] | None = None,
    ) -> ScheduledRate | None:
        """Le debit horaire de cet abonne a cet instant, ou None (forfait normal).

        Le pourcentage s'applique a la VITESSE DU CLIENT : celle de son mode s'il
        en a un, sinon son plan."""
        if not self.schedules:
            return None
        instant = now or datetime.now(tz=UTC)
        profil = self.governing(
            login=login, sites=sites, plan_down=plan_down, mode_id=mode_id, now=instant
        )
        if profil is None:
            return None
        plage = profil.window_at(instant)
        if plage is None:
            return None
        en_mode = mode_id is not None and bool(mode_down or mode_up)
        base_bas, base_haut = (mode_down, mode_up) if en_mode else (plan_down, plan_up)
        bas, haut = plage.rates(base_bas, base_haut)
        if plage.emergency and emergency is not None:
            # Un mode d'urgence donne a CE client sa bande passante d'urgence.
            bas, haut = emergency
        if bas is None and haut is None:
            return None
        return ScheduledRate(
            down_mbps=bas,
            up_mbps=haut,
            schedule_id=profil.id,
            schedule_name=profil.name,
            window="started by hand" if profil.manual_active(instant) else plage.describe(),
            over_mode=en_mode,
            personal=profil.personal,
        )

    def signature(self, now: datetime | None = None) -> tuple[tuple[int, int | None], ...]:
        """Quelle plage court dans chaque profil. Elle change = il faut reecrire."""
        instant = now or datetime.now(tz=UTC)
        sortie: list[tuple[int, int | None]] = []
        for profil in self.schedules:
            if not profil.enabled:
                continue
            if profil.manual_active(instant):
                sortie.append((profil.id, -1))
                continue
            plage = profil.window_at(instant)
            sortie.append((profil.id, profil.windows.index(plage) if plage else None))
        return tuple(sortie)


#: Le carnet de l'application. Vide tant que rien n'est charge : sans profil,
#: rien ne change, ni dans le plan ni dans l'affichage.
BOOK = ScheduleBook()


# ---------------------------------------------------------------- chevauchements
#
# DEUX SCHEDULERS PEUVENT PARTAGER DES HEURES, PAS DES CLIENTS. « Soir » pour
# les clients a leur plan et « Soir » pour le mode Pro, aux memes heures : c'est
# permis, ils ne touchent pas les memes clients. Le meme client vise par deux
# schedulers a la meme minute : refuse a l'enregistrement, avec QUI et QUAND.


def _minutes_utc(profil: BandwidthSchedule, now: datetime) -> set[int]:
    """Les minutes de la semaine (UTC) couvertes par ses plages."""
    decalage = profil.local(now).utcoffset()
    ecart = int(decalage.total_seconds() // 60) if decalage else 0
    minutes: set[int] = set()
    for plage in profil.windows:
        for debut, duree in plage.minutes():
            minutes.update((m - ecart) % MINUTES_PAR_SEMAINE for m in range(debut, debut + duree))
    return minutes


def _memes_cibles_possibles(a: BandwidthSchedule, b: BandwidthSchedule) -> bool:
    """Peuvent-ils viser le meme client, quels que soient les clients d'aujourd'hui ?"""
    if SCOPE_ALL in (a.scope, b.scope):
        return True
    if a.scope != b.scope:
        return False
    if a.scope == SCOPE_ON_PLAN:
        return True
    if a.scope == SCOPE_POPS:
        return bool(a._cles_sites & b._cles_sites)
    return bool(set(a.targets) & set(b.targets))


def overlap_conflict(
    profil: BandwidthSchedule,
    autres: Iterable[BandwidthSchedule],
    clients: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    describe_client: Any = None,
) -> str | None:
    """Le message d'erreur si ``profil`` touche les memes clients qu'un autre aux
    memes heures, sinon None. ``clients`` : login, pop_name, plan_down_mbps,
    mode_id (et display_name pour le message)."""
    instant = now or datetime.now(tz=UTC)
    liste = list(clients)
    mes_minutes = _minutes_utc(profil, instant)
    for autre in autres:
        if autre.id == profil.id or not autre.enabled:
            continue
        # Un scheduler PROPRE a un client remplace le groupe pour lui : ils
        # peuvent partager des heures. Deux propres au meme client, non.
        if profil.personal != autre.personal:
            continue
        communes = mes_minutes & _minutes_utc(autre, instant)
        if not communes:
            continue
        touches = [
            c
            for c in liste
            if all(
                p.governs(
                    login=str(c.get("login")),
                    sites=(c.get("pop_name"),),
                    plan_down=c.get("plan_down_mbps"),
                    mode_id=c.get("mode_id"),
                )
                for p in (replace_enabled(profil), autre)
            )
        ]
        if not touches and not _memes_cibles_possibles(profil, autre):
            continue
        # L'heure dans le fuseau du scheduler, comme on l'a saisie.
        decalage = profil.local(instant).utcoffset()
        ecart = int(decalage.total_seconds() // 60) if decalage else 0
        locale = (min(communes) + ecart) % MINUTES_PAR_SEMAINE
        jour, heure = divmod(locale, MINUTES_PAR_JOUR)
        quand = f"{DAYS[jour].capitalize()} {format_time(heure)}"
        if touches:
            noms = [
                str(describe_client(c) if describe_client else c.get("login")) for c in touches[:5]
            ]
            qui = ", ".join(noms) + (f" and {len(touches) - 5} more" if len(touches) > 5 else "")
            return (
                f"'{profil.name}' and '{autre.name}' would both apply to {qui} at the same "
                f"time ({quand}). Change the hours, or choose other clients or modes."
            )
        return (
            f"'{profil.name}' and '{autre.name}' target the same clients at the same time "
            f"({quand}). Change the hours, or choose other clients or modes."
        )
    return None


def replace_enabled(profil: BandwidthSchedule) -> BandwidthSchedule:
    """Le meme profil, actif : on verifie un profil suspendu comme s'il tournait."""
    from dataclasses import replace

    return replace(profil, enabled=True)

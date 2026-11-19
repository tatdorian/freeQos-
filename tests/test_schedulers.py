"""Les SCHEDULERS : un pourcentage (ou un mode) pendant des heures, pour des clients.

CE QUI EST FIXE ICI
-------------------
1. Un scheduler vise les clients a leur PLAN, les clients d'un ou plusieurs
   MODES, des clients choisis, ou tout le monde.
2. Le pourcentage s'applique a la VITESSE du client : son mode s'il en a un,
   sinon son plan. Une limite manuelle (sans mode) reste au-dessus.
3. Deux schedulers peuvent partager des heures s'ils ne touchent pas les memes
   clients ; sinon l'enregistrement est refuse avec qui et quand.
4. Un scheduler se lance A LA MAIN : tout de suite, quelle que soit l'heure,
   pour une duree ou jusqu'a l'arret, et il passe devant les autres.
5. Un planning d'avant a plusieurs debits devient un scheduler par debit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.enforcement.planner import SOURCE_SCHEDULE, effective_rate
from app.enforcement.schedules import (
    BandwidthSchedule,
    InvalidScheduleError,
    ScheduleBook,
    overlap_conflict,
)
from app.services.schedule_upgrade import split_mixed_schedules

LUNDI_19H = datetime(2026, 3, 16, 19, 0, tzinfo=UTC)  # un lundi
LUNDI_12H = LUNDI_19H.replace(hour=12)
SOIR = [{"start": "18:00", "end": "23:00", "percent": 70}]


def _sched(ident: int, nom: str, scope: str, targets=(), windows=SOIR, **extra):  # type: ignore[no-untyped-def]
    return BandwidthSchedule.build(
        id=ident, name=nom, windows=list(windows), scope=scope, targets=list(targets), **extra
    )


# ------------------------------------------------------------------ cibles
def test_clients_a_leur_plan_seulement() -> None:
    s = _sched(1, "Evening", "on_plan")
    assert s.governs(login="a", sites=(), plan_down=20, mode_id=None)
    assert not s.governs(login="b", sites=(), plan_down=20, mode_id=3)


def test_les_clients_des_modes_choisis() -> None:
    s = _sched(1, "Evening Pro", "modes", targets=["3", "4"])
    assert s.governs(login="a", sites=(), plan_down=None, mode_id=3)
    assert not s.governs(login="a", sites=(), plan_down=None, mode_id=5)
    assert not s.governs(login="a", sites=(), plan_down=20, mode_id=None)


def test_un_mode_designe_par_un_nom_est_refuse() -> None:
    with pytest.raises(InvalidScheduleError, match="identifiers of the modes"):
        _sched(1, "x", "modes", targets=["Backup"])


# ------------------------------------------------------- pourcentage de quoi
def test_le_pourcentage_porte_sur_le_mode_ou_sur_le_plan() -> None:
    carnet = ScheduleBook([_sched(1, "Evening", "all")])
    sur_plan = carnet.rate_for(login="a", sites=(), plan_down=20, plan_up=10, now=LUNDI_19H)
    assert (sur_plan.down_mbps, sur_plan.up_mbps) == (14.0, 7.0)
    assert sur_plan.over_mode is False
    en_mode = carnet.rate_for(
        login="b",
        sites=(),
        plan_down=20,
        plan_up=10,
        now=LUNDI_19H,
        mode_id=3,
        mode_down=6,
        mode_up=6,
    )
    assert en_mode.down_mbps == pytest.approx(4.2)
    assert en_mode.over_mode is True


def test_le_debit_horaire_d_un_client_en_mode_passe_devant_le_mode() -> None:
    debit, source = effective_rate(
        plan_mbps=20,
        override_mbps=6,
        boost_mbps=None,
        boost_expires_at=None,
        schedule_mbps=4.2,
        schedule_over_override=True,
    )
    assert (debit, source) == (4.2, SOURCE_SCHEDULE)
    # Une limite manuelle (sans mode) reste au-dessus du scheduler.
    debit, _ = effective_rate(
        plan_mbps=20, override_mbps=1, boost_mbps=None, boost_expires_at=None, schedule_mbps=14
    )
    assert debit == 1


# ------------------------------------------------------------- lance a la main
def test_lance_a_la_main_il_s_applique_tout_de_suite() -> None:
    s = _sched(1, "Evening", "on_plan", manual_on=True, manual_until=LUNDI_12H + timedelta(hours=1))
    assert s.window_at(LUNDI_12H) is not None  # midi : hors de ses heures, et pourtant
    assert s.window_at(LUNDI_12H + timedelta(hours=2)) is None  # termine
    jusqu_a_l_arret = _sched(2, "Night", "on_plan", manual_on=True)
    assert jusqu_a_l_arret.window_at(LUNDI_12H + timedelta(days=3)) is not None


def test_entre_groupes_celui_lance_a_la_main_passe_devant() -> None:
    nuit = _sched(
        1,
        "Night",
        "on_plan",
        windows=[{"start": "00:00", "end": "24:00", "percent": 200}],
    )
    soir = _sched(2, "Evening", "all", manual_on=True)
    carnet = ScheduleBook([nuit, soir])
    taux = carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_12H)
    assert taux.schedule_name == "Evening"
    assert taux.down_mbps == 7.0


def test_le_scheduler_propre_au_client_passe_devant_le_groupe() -> None:
    """Ce qui est fait a la main POUR CE CLIENT l'emporte sur le groupe, meme
    lance a la main."""
    propre = _sched(
        1,
        "ACME a · own",
        "subscribers",
        targets=["a"],
        windows=[{"start": "18:00", "end": "23:00", "percent": 100}],
    )
    assert propre.personal
    groupe = _sched(2, "Evening", "all", manual_on=True)
    carnet = ScheduleBook([propre, groupe])
    soir = carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_19H)
    assert (soir.schedule_name, soir.down_mbps, soir.personal) == ("ACME a · own", 10.0, True)
    # Hors de ses heures, le groupe s'applique.
    midi = carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_12H)
    assert (midi.schedule_name, midi.personal) == ("Evening", False)
    # Les autres clients suivent le groupe.
    autre = carnet.rate_for(login="b", sites=(), plan_down=10, plan_up=10, now=LUNDI_19H)
    assert autre.schedule_name == "Evening"


def test_celui_dont_les_heures_courent_gouverne() -> None:
    """Deux schedulers pour les memes clients a des heures differentes : chacun
    s'applique a ses heures (le plus precis n'eclipse plus l'autre)."""
    soir = _sched(1, "Evening", "all")
    jour = _sched(2, "Day", "on_plan", windows=[{"start": "08:00", "end": "18:00", "percent": 50}])
    carnet = ScheduleBook([soir, jour])
    assert (
        carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_19H).schedule_name
        == "Evening"
    )
    assert (
        carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_12H).schedule_name
        == "Day"
    )


# ---------------------------------------------------------------- chevauchements
CLIENTS = [
    {"login": "a", "pop_name": "North", "plan_down_mbps": 20, "mode_id": None},
    {"login": "b", "pop_name": "North", "plan_down_mbps": 20, "mode_id": 3},
]


def test_memes_heures_autres_clients_c_est_permis() -> None:
    plan = _sched(1, "Evening plan", "on_plan")
    pro = _sched(2, "Evening Pro", "modes", targets=["3"])
    assert overlap_conflict(pro, [plan], CLIENTS, now=LUNDI_12H) is None


def test_memes_heures_memes_clients_refuse_avec_qui_et_quand() -> None:
    tous = _sched(1, "Evening", "all")
    choisis = _sched(2, "VIP", "subscribers", targets=["a", "b"])
    message = overlap_conflict(
        choisis, [tous], CLIENTS, now=LUNDI_12H, describe_client=lambda c: "ACME " + c["login"]
    )
    assert message is not None
    assert "'VIP' and 'Evening' would both apply to ACME a, ACME b" in message
    assert "(Mon 18:00)" in message


def test_un_scheduler_propre_peut_chevaucher_le_groupe_pas_un_autre_propre() -> None:
    tous = _sched(1, "Evening", "all")
    propre = _sched(2, "b · own", "subscribers", targets=["b"])
    assert overlap_conflict(propre, [tous], CLIENTS, now=LUNDI_12H) is None
    autre_propre = _sched(3, "b · other", "subscribers", targets=["b"])
    message = overlap_conflict(autre_propre, [tous, propre], CLIENTS, now=LUNDI_12H)
    assert message is not None and "'b · other' and 'b · own'" in message


def test_autres_heures_memes_clients_c_est_permis() -> None:
    soir = _sched(1, "Evening", "all")
    nuit = _sched(2, "Night", "all", windows=[{"start": "23:00", "end": "06:00", "percent": 200}])
    assert overlap_conflict(nuit, [soir], CLIENTS, now=LUNDI_12H) is None


def test_sans_client_aujourd_hui_le_meme_type_est_refuse() -> None:
    a = _sched(1, "A", "on_plan")
    b = _sched(2, "B", "on_plan")
    assert "target the same clients" in (overlap_conflict(b, [a], [], now=LUNDI_12H) or "")


# ------------------------------------------------------- planning d'avant
class DepotMemoire:
    def __init__(self, lignes: list[dict]) -> None:  # type: ignore[type-arg]
        self.lignes = lignes

    async def list_all(self) -> list[dict]:  # type: ignore[type-arg]
        return [dict(x) for x in self.lignes]

    async def create(self, payload: dict) -> dict:  # type: ignore[type-arg]
        ligne = {"id": len(self.lignes) + 1, **payload}
        self.lignes.append(ligne)
        return ligne

    async def update(self, ident: int, payload: dict) -> dict:  # type: ignore[type-arg]
        ligne = next(x for x in self.lignes if x["id"] == ident)
        ligne.update(payload)
        return ligne


async def test_un_planning_a_deux_debits_devient_deux_schedulers() -> None:
    depot = DepotMemoire(
        [
            {
                "id": 1,
                "name": "Day and night",
                "enabled": True,
                "timezone": "UTC",
                "scope": "all",
                "targets": [],
                "windows": [
                    {"label": "evening", "start": "18:00", "end": "23:00", "percent": 70},
                    {"label": "night", "start": "23:00", "end": "06:00", "percent": 200},
                ],
            }
        ]
    )
    assert await split_mixed_schedules(depot) == ["Day and night · night"]
    premier, second = depot.lignes
    assert [w["percent"] for w in premier["windows"]] == [70]
    assert [w["percent"] for w in second["windows"]] == [200]
    assert second["scope"] == "all"
    # Deja separe : rien de plus au demarrage suivant.
    assert await split_mixed_schedules(depot) == []


# ------------------------------------------------------- interface
def test_la_fiche_client_montre_le_scheduler_en_cours() -> None:
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "'<tr><td>Scheduler</td><td>'" in js
    assert "none right now" in js


def test_un_mode_ne_se_saisit_plus_en_pourcentage() -> None:
    from app.web.ui import STATIC_DIR

    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    debut = html.index('id="mode-unit"')
    unites = html[debut : html.index("</div>", debut)]
    assert 'data-u="kbps"' in unites
    assert 'data-u="mbps"' in unites
    assert "percent" not in unites


def test_le_scheduler_lance_a_la_main_le_dit() -> None:
    carnet = ScheduleBook([_sched(1, "Evening", "all", manual_on=True)])
    taux = carnet.rate_for(login="a", sites=(), plan_down=10, plan_up=10, now=LUNDI_12H)
    assert taux.window == "started by hand"


def test_la_page_schedulers_range_ceux_d_un_client_a_part() -> None:
    from app.web.ui import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "'<h3 style=\"margin:1.2rem 0 .3rem\">For one client</h3>'" in js
    assert "function carteSchedulerPropre(r, dis)" in js
    assert "'<b>its own</b> · '" in js

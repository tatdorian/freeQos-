"""Router health : le CPU en detail -- coeur par coeur, et le pic de l'heure.

La moyenne des coeurs cache le probleme : un coeur sature sur quatre
s'affiche 33 %, alors que le trafic qu'il traite prend du retard. Ce qui est
fixe ici : la lecture par coeur, l'historique d'une heure (releve toutes les
30 s), et l'alerte quand un coeur reste au-dessus de 80 % une minute.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.mikrotik import MikrotikCollector
from app.services.router_cpu import SEUIL_ALERTE, RouterCpu
from app.web.ui import STATIC_DIR
from tests.conftest import FakeRouterOsClient


class RouteurQuatreCoeurs(FakeRouterOsClient):
    def system_resource(self):  # type: ignore[no-untyped-def]
        return {**super().system_resource(), "cpu-load": "33", "cpu-count": "4"}

    def system_resource_cpu(self):  # type: ignore[no-untyped-def]
        return [
            {"cpu": "cpu0", "load": "100", "irq": "40"},
            {"cpu": "cpu1", "load": "12", "irq": "0"},
            {"cpu": "cpu2", "load": "10", "irq": "0"},
            {"cpu": "cpu3", "load": "9", "irq": "0"},
        ]


def test_la_sante_donne_chaque_coeur_et_le_plus_charge(router_config) -> None:  # type: ignore[no-untyped-def]
    sante = MikrotikCollector(router_config, client=RouteurQuatreCoeurs()).health_sync()
    assert sante["cpu_load_pct"] == 33
    assert sante["cpu_max_core_pct"] == 100
    assert [c["load_pct"] for c in sante["cores"]] == [100, 12, 10, 9]
    assert sante["cores"][0]["irq_pct"] == 40


def test_un_routeur_sans_detail_garde_la_moyenne(router_config) -> None:  # type: ignore[no-untyped-def]
    sante = MikrotikCollector(router_config, client=FakeRouterOsClient()).health_sync()
    assert sante["cpu_load_pct"] == 3
    assert sante["cores"] == []
    assert sante["cpu_max_core_pct"] is None


def _sante(coeur: int, moyenne: int = 30) -> dict:
    return {"reachable": True, "cpu_load_pct": moyenne, "cpu_max_core_pct": coeur}


def test_le_pic_de_l_heure() -> None:
    cpu = RouterCpu()
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
    cpu.record("nas", _sante(40), t0)
    cpu.record("nas", _sante(91), t0 + timedelta(minutes=10))
    cpu.record("nas", _sante(35), t0 + timedelta(minutes=20))
    r = cpu.summary("nas", t0 + timedelta(minutes=21))
    assert r["peak_core_pct"] == 91
    assert r["samples"] == 3
    assert r["minutes_over_threshold"] == 0.5
    # Plus d'une heure apres : le pic est sorti de la fenetre.
    cpu.record("nas", _sante(20), t0 + timedelta(minutes=75))
    assert cpu.summary("nas", t0 + timedelta(minutes=75))["peak_core_pct"] == 35


def test_alerte_seulement_quand_ca_dure() -> None:
    cpu = RouterCpu()
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
    cpu.record("nas", _sante(SEUIL_ALERTE + 5), t0)
    assert cpu.problems() == []  # un pic isole n'est pas une alerte
    cpu.record("nas", _sante(SEUIL_ALERTE + 5), t0 + timedelta(seconds=30))
    (p,) = cpu.problems()
    assert p["key"] == "cpu:nas"
    assert p["category"] == "router"
    assert p["level"] == "warn"
    cpu.record("nas", _sante(97), t0 + timedelta(seconds=60))
    assert cpu.problems()[0]["level"] == "crit"
    cpu.record("nas", _sante(30), t0 + timedelta(seconds=90))
    assert cpu.problems() == []


def test_un_routeur_injoignable_n_est_pas_releve() -> None:
    cpu = RouterCpu()
    cpu.record("nas", {"reachable": False})
    assert cpu.summary("nas") == {"samples": 0}


def test_le_releve_de_tous_les_routeurs(router_config) -> None:  # type: ignore[no-untyped-def]
    cpu = RouterCpu()
    collecteur = MikrotikCollector(router_config, client=RouteurQuatreCoeurs())
    assert asyncio.run(cpu.sample([collecteur])) == 1
    assert cpu.summary(collecteur.name)["peak_core_pct"] == 100


def test_l_interface_montre_le_detail() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function cpuDetail(r, badge)" in js
    assert "Peak, last hour" in js
    html = (STATIC_DIR.parent / "templates" / "index.html").read_text(encoding="utf-8")
    # « Router settings » vient juste apres « Connect a router ».
    assert html.index("<h2>Connect a router (PoP)</h2>") < html.index("<h2>Router settings</h2>")
    assert html.index("<h2>Router settings</h2>") < html.index("<h2>Ubiquiti UISP</h2>")
    assert "<h2>Inventory</h2>" not in html


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        (33, 33),
        ("33", 33),
        ("33%", 33),
        ("33 %", 33),
        ("12.5", 12),
        (12.6, 13),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_toutes_les_formes_de_charge_cpu_sont_lues(brut: object, attendu: int | None) -> None:
    """Selon la version de RouterOS et la bibliotheque, la charge arrive en nombre
    ou en texte, avec ou sans « % » : toutes donnent le meme chiffre."""
    from app.collectors.mikrotik import _pourcent

    assert _pourcent(brut) == attendu


def test_un_client_a_ip_fixe_sans_compteur_n_est_pas_connecte() -> None:
    """« Connected » a 0 pour un client a IP fixe que freeQoS ne mesure pas :
    faux. Sans compteur ni ping, il est « Not measured »."""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function sansMesure(r)" in js
    assert "return !sansMesure(r);" in js
    assert "Not measured · no counter yet" in js

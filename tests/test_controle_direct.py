"""« Le trafic est a 0 : est-ce la faute de freeQoS ? » -- le controle en direct.

Le routeur est lu deux fois, a 2 s d'intervalle, et compare a ce que freeQoS a
enregistre. Le verdict dit QUI est en cause : le client (au repos), le
routeur, ou la mesure de freeQoS. Il est sur la page du client.
"""

from __future__ import annotations

from app.api.metrics import live_verdict
from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _live(tx: float | None, rx: float | None) -> dict:
    return {
        "router": "nas-test",
        "session": {"address": "10.20.0.10"},
        "interface_found": True,
        "interface_tx_bps": tx,
        "interface_rx_bps": rx,
    }


def _stocke(tx: float, rx: float, age: float = 5.0) -> dict:
    return {"tx_bps": tx, "rx_bps": rx, "age_s": age}


def test_routeur_a_zero_le_client_est_au_repos() -> None:
    v = live_verdict("pppoe", _live(0, 0), None, _stocke(0, 0), [])
    assert v["level"] == "ok"
    assert "freeQoS shows 0 because there is 0" in v["text"]


def test_le_trafic_vient_de_s_arreter() -> None:
    v = live_verdict("pppoe", _live(0, 0), None, _stocke(4_000_000, 600_000), [])
    assert "the traffic has just stopped" in v["text"]


def test_routeur_compte_freeqos_a_zero_c_est_freeqos() -> None:
    v = live_verdict("pppoe", _live(8_000_000, 400_000), None, _stocke(0, 0), [])
    assert v["level"] == "crit"
    assert "the measurement in freeQoS is at fault" in v["text"]


def test_routeur_et_freeqos_d_accord() -> None:
    v = live_verdict("pppoe", _live(8_000_000, 400_000), None, _stocke(7_500_000, 380_000), [])
    assert v["level"] == "ok"
    assert "it is stored" in v["text"]


def test_mesure_en_panne() -> None:
    v = live_verdict("pppoe", _live(8_000_000, 0), None, _stocke(0, 0, age=600), [])
    assert v["level"] == "crit"
    assert "cycle is failing" in v["text"]


def test_le_controle_est_sur_la_page_du_client() -> None:
    assert 'id="cp-live-btn"' in JS
    assert "Check against the router" in JS
    assert "liveCheck(r.subscriber_id, 'cp-live', 'cp-live-btn')" in JS

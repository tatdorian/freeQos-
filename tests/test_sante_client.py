"""La sante d'un client, en cinq reponses : chaque verdict, et ce qui manque."""

from __future__ import annotations

from app.services import client_health as sante


def test_l_experience_se_lit_en_un_mot() -> None:
    bon = sante.experience(
        {"qoe_score": 94, "experience": "good", "median_ms": 18, "grade": "A", "reasons": []}
    )
    assert bon["level"] == "ok" and bon["title"] == "Good · 94/100"
    assert "under load: grade A" in bon["detail"]
    mauvais = sante.experience({"qoe_score": 41, "experience": "poor", "median_ms": 160})
    assert mauvais["level"] == "crit"
    assert sante.experience(None)["level"] == "unknown"


ANTENNES = [
    {
        "key": "ap-1",
        "name": "AP Plateau",
        "stations": [
            {"mac": "aa:bb:cc:00:11:22", "signal_dbm": -58, "snr_db": 35, "ccq_pct": 97},
            {"mac": "AA-BB-CC-00-11-33", "signal_dbm": -79, "ip": "10.40.0.20"},
        ],
    }
]


def test_le_signal_du_cpe_se_retrouve_par_sa_mac() -> None:
    bon = sante.radio("AABBCC001122", ANTENNES)
    assert bon["level"] == "ok" and bon["title"] == "Good · -58 dBm"
    assert "AP Plateau" in bon["detail"]
    faible = sante.radio("aa:bb:cc:00:11:33", ANTENNES)
    assert faible["level"] == "crit" and "alignment" in faible["detail"]
    # Sans MAC, l'adresse du CPE peut suffire.
    assert sante.radio(None, ANTENNES, ip="10.40.0.20")["level"] == "crit"
    assert sante.radio("00:00:00:00:00:01", ANTENNES)["level"] == "unknown"
    assert "No CPE MAC" in sante.radio(None, ANTENNES)["detail"]


def test_un_site_sature_se_dit_comme_tel() -> None:
    pop = {"tx_bps": 92e6, "backhauls": [{"capacity_mbps": 100}, {"capacity_mbps": 200}]}
    sature = sante.site("Plateau", pop)
    assert sature["level"] == "crit" and sature["title"] == "Plateau · Saturated · 92 %"
    assert "every client of the site" in sature["detail"]
    calme = sante.site("Plateau", {"tx_bps": 20e6, "backhauls": [{"capacity_mbps": 100}]})
    assert calme["level"] == "ok"
    inconnu = sante.site("Plateau", {"tx_bps": 5e6, "backhauls": []})
    assert inconnu["level"] == "unknown" and "capacity unknown" in inconnu["title"]


def test_une_limite_contournee_n_est_pas_tenue() -> None:
    tenue = sante.limite({"verdict": "en-vigueur", "seen": "10M/20M"}, True)
    assert tenue["level"] == "ok" and tenue["title"] == "Applied"
    contournee = sante.limite(
        {"verdict": "contourne", "detail": "fasttrack-connection active"}, True
    )
    assert contournee["level"] == "crit" and "FastTrack" in contournee["title"]
    assert sante.limite(None, False)["title"] == "Not checked yet"


def test_les_usages_se_classent_en_parts() -> None:
    u = sante.usage(
        1e9,
        2e8,
        3e10,
        5e9,
        [
            {"app": "streaming", "down_bytes": 600, "up_bytes": 0},
            {"app": "web", "down_bytes": 300, "up_bytes": 100},
        ],
    )
    assert u["day_down_bytes"] == 1_000_000_000
    assert [(a["app"], a["share"]) for a in u["top"]] == [("streaming", 60.0), ("web", 40.0)]

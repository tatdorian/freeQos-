"""Lien filaire ou radio, declare lien par lien.

- filaire : capacite fixe (ou vitesse du port), aucune mesure radio ;
- radio   : la capacite que l'antenne annonce EN DIRECT ;
- une antenne qui chute sous 70 % de sa nominale est signalee ;
- la capacite du NOEUD est celle de son lien montant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.capacity import hotspot_rows, node_uplinks, radio_alerts, radio_state

MAINTENANT = datetime.now(tz=UTC)


def _occupation(interface: str = "wlan1", port: float = 1000.0) -> list[dict]:
    return [
        {
            "router_name": "nas-sud",
            "interface": interface,
            "capacity_mbps": port,
            "peak_rx_bps": 120e6,
            "peak_tx_bps": 20e6,
            "samples": 30,
        }
    ]


def _antenne(capacite: float, *, nominale: float | None = 300.0, age_s: float = 30) -> dict:
    return {
        "name": "BH-Sud",
        "pop_name": "PoP Sud",
        "capacity_mbps": capacite,
        "nominal_capacity_mbps": nominale,
        "ts": MAINTENANT - timedelta(seconds=age_s),
        "online": True,
    }


def _ligne(**kw):  # type: ignore[no-untyped-def]
    return hotspot_rows(
        _occupation(),
        [],
        upstream={"nas-sud": ("10.0.0.1", "wlan1")},
        roles={"nas-sud": "pop"},
        **kw,
    )[0]


def test_sans_declaration_la_regle_automatique_reste() -> None:
    ligne = _ligne()
    assert ligne["medium"] is None
    assert (ligne["capacity_mbps"], ligne["capacity_source"]) == (1000.0, "port speed")


def test_un_lien_filaire_porte_sa_capacite_fixe() -> None:
    media = {("nas-sud", "wlan1"): {"medium": "wired", "capacity_mbps": 500.0}}
    ligne = _ligne(media=media, radios={"BH-Sud": _antenne(120.0)})
    assert (ligne["capacity_mbps"], ligne["capacity_source"]) == (500.0, "wired, declared")
    assert ligne["radio"] is None  # aucune mesure radio prise en compte


def test_un_lien_filaire_sans_chiffre_vaut_la_vitesse_du_port() -> None:
    ligne = _ligne(media={("nas-sud", "wlan1"): {"medium": "wired"}})
    assert ligne["capacity_source"] == "port speed"


def test_un_lien_radio_suit_l_antenne_en_direct() -> None:
    media = {("nas-sud", "wlan1"): {"medium": "radio", "backhaul_name": "BH-Sud"}}
    ligne = _ligne(media=media, radios={"BH-Sud": _antenne(150.0)})
    assert (ligne["capacity_mbps"], ligne["capacity_source"]) == (150.0, "radio, live")
    assert ligne["radio"]["state"] == "degraded"  # 150 / 300 = 50 %
    assert ligne["state"] == "busy"  # 120 Mbps sur 150 : 80 %


def test_une_antenne_muette_ne_fabrique_pas_de_capacite() -> None:
    media = {("nas-sud", "wlan1"): {"medium": "radio", "backhaul_name": "BH-Sud"}}
    ligne = _ligne(media=media, radios={"BH-Sud": _antenne(300.0, age_s=3600)})
    assert ligne["radio"]["state"] == "silent"
    assert ligne["capacity_source"] == "port speed"  # seule borne connue


def test_la_chute_radio_est_signalee_avec_la_meilleure_capacite_comme_reference() -> None:
    antennes = [_antenne(120.0, nominale=None), {**_antenne(290.0), "name": "BH-Nord"}]
    alertes = radio_alerts(antennes, maxima={"BH-Sud": 300.0}, now=MAINTENANT)
    assert [a["name"] for a in alertes] == ["BH-Sud"]
    assert alertes[0]["share_of_nominal"] == 0.4


def test_une_antenne_a_sa_nominale_va_bien() -> None:
    assert radio_state(_antenne(280.0), now=MAINTENANT)["state"] == "ok"


def test_la_capacite_du_noeud_est_celle_de_son_lien_montant() -> None:
    lignes = hotspot_rows(
        _occupation() + [{**_occupation("vlan2060")[0], "capacity_mbps": 100.0}],
        [],
        upstream={"nas-sud": ("10.0.0.1", "wlan1")},
        roles={"nas-sud": "pop"},
    )
    assert node_uplinks(lignes)["nas-sud"]["interface"] == "wlan1"

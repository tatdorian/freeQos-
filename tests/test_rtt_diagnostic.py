"""« Probe silent » : le diagnostic rend une CAUSE, pas seulement des chiffres."""

from __future__ import annotations

from app.services.rtt import rtt_verdict, suspicious_firewall_rules


def _essai(source: str | None, recus: int, statut: str = "timeout") -> dict[str, object]:
    return {
        "source": source,
        "stats": {"sent": 5, "received": recus},
        "raw": [{"seq": "0", "status": statut}],
    }


def test_un_drop_avant_l_accept_des_etablies_est_suspect() -> None:
    regles = [
        {"chain": "input", "action": "drop", "in-interface-list": "clients", "comment": "durci"},
        {"chain": "input", "action": "accept", "connection-state": "established,related"},
        {"chain": "input", "action": "drop"},
        {"chain": "forward", "action": "drop"},
        {"chain": "input", "action": "drop", "protocol": "tcp"},
    ]
    [suspecte] = suspicious_firewall_rules(regles)
    assert suspecte["position"] == 0 and suspecte["in_interface"] == "clients"


def test_un_accept_icmp_en_tete_blanchit_la_chaine() -> None:
    regles = [
        {"chain": "input", "action": "accept", "protocol": "icmp"},
        {"chain": "input", "action": "drop"},
    ]
    assert suspicious_firewall_rules(regles) == []


def test_une_regle_desactivee_ne_compte_pas() -> None:
    assert (
        suspicious_firewall_rules([{"chain": "input", "action": "drop", "disabled": "true"}]) == []
    )


def test_routeur_qui_pingue_sa_passerelle_mais_pas_le_client() -> None:
    v = rtt_verdict([_essai("11.11.11.75", 0), _essai(None, 0)], _essai(None, 5), [])
    assert v["code"] == "client_blocks_icmp"


def test_routeur_qui_ne_pingue_rien() -> None:
    v = rtt_verdict([_essai(None, 0)], _essai(None, 0), [])
    assert v["code"] == "router_cannot_ping"


def test_le_pare_feu_passe_avant_le_client() -> None:
    v = rtt_verdict([_essai(None, 0)], _essai(None, 5), [{"position": 0}])
    assert v["code"] == "firewall"


def test_reponse_sans_source_seulement() -> None:
    v = rtt_verdict([_essai("11.11.11.75", 0), _essai(None, 5)], None, [])
    assert v["code"] == "loopback_return_path"


def test_droit_test_manquant() -> None:
    v = rtt_verdict([{"source": None, "error": "TrapError: not enough permissions"}], None, [])
    assert v["code"] == "no_test_policy"


def test_hote_injoignable() -> None:
    v = rtt_verdict([_essai(None, 0, "host unreachable")], _essai(None, 5), [])
    assert v["code"] == "no_route"

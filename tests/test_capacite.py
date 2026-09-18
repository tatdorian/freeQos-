"""Capacite vendue, capacite reelle, et l'usage entre les deux.

CE QUE CES TESTS PROTEGENT
--------------------------
Ces chiffres servent a DIMENSIONNER : on achete un backhaul, on refuse une
vente, on rappelle un client sur la foi de ce qu'ils disent. Un chiffre faux ne
se voit pas ici comme une panne -- il se voit six mois plus tard, en facture.

D'ou l'insistance sur les cas ou l'on ne SAIT PAS : une capacite non mesuree ne
donne pas un taux de survente nul, elle donne un taux inconnu, et les deux
appellent des gestes opposes (ne rien faire, ou aller mesurer).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.services.capacity import (
    ETAT_CHARGE,
    ETAT_INCONNU,
    ETAT_LIBRE,
    ETAT_SATURE,
    VERDICT_CONFORTABLE,
    VERDICT_SANS_CAPACITE,
    VERDICT_SANS_VENTE,
    VERDICT_SURVEILLER,
    VERDICT_TENDU,
    a_renforcer,
    etat_du_lien,
    link_row,
    occupancy,
    oversubscription,
    pop_capacity_row,
    usage_row,
    verdict_survente,
)

NOW = datetime(2026, 9, 17, 21, 30, tzinfo=UTC)


@pytest.fixture
def client(settings):
    """L'API montee sur le conteneur factice du fichier voisin."""
    from fastapi import FastAPI

    from app.api.deps import get_container
    from app.main import register_routes
    from tests.test_api import build_container

    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: build_container(settings)
    return TestClient(app)


# =========================================================================
# 1. La survente : un rapport, et ce qu'on en dit
# =========================================================================


def test_le_taux_de_survente_est_le_vendu_sur_le_mesure() -> None:
    assert oversubscription(2000.0, 500.0) == 4.0


@pytest.mark.parametrize("capacite", [None, 0.0])
def test_sans_capacite_mesuree_le_taux_est_inconnu_pas_nul(capacite) -> None:
    """LE PIEGE. Rendre 0 ferait passer un PoP non mesure pour le plus sain du
    parc, et il serait le dernier qu'on irait regarder."""
    assert oversubscription(2000.0, capacite) is None
    assert verdict_survente(None, sold_mbps=2000.0) == VERDICT_SANS_CAPACITE


def test_un_pop_sans_rien_de_vendu_n_est_pas_un_pop_en_danger() -> None:
    assert verdict_survente(None, sold_mbps=0.0) == VERDICT_SANS_VENTE


@pytest.mark.parametrize(
    ("ratio", "attendu"),
    [
        (1.0, VERDICT_CONFORTABLE),
        (5.0, VERDICT_CONFORTABLE),
        (5.1, VERDICT_SURVEILLER),
        (20.0, VERDICT_SURVEILLER),
        (20.1, VERDICT_TENDU),
    ],
)
def test_les_seuils_de_verdict(ratio: float, attendu: str) -> None:
    """Un reseau d'acces SANS survente serait un reseau ou l'operateur a achete
    dix fois trop de transit : ces seuils disent quand le partage se voit, pas
    quand il y a panne."""
    assert verdict_survente(ratio, sold_mbps=100.0) == attendu


def test_la_ligne_de_pop_rapproche_le_vendu_et_la_pointe_reelle() -> None:
    """Vendre vingt fois la capacite ne se voit pas tant que la pointe reste au
    tiers du lien. Le taux seul serait un chiffre a sensation."""
    ligne = pop_capacity_row(
        {
            "pop_name": "PoP Nord",
            "subscribers": 40,
            "sold_down_mbps": 4000.0,
            "sold_up_mbps": 800.0,
            "capacity_mbps": 500.0,
            "peak_bps": 150_000_000.0,
        }
    )
    assert ligne["ratio"] == 8.0
    assert ligne["verdict"] == VERDICT_SURVEILLER
    assert ligne["peak_mbps"] == 150.0
    assert ligne["peak_share"] == 0.3
    assert ligne["peak_state"] == ETAT_LIBRE


def test_un_pop_sans_pointe_observee_ne_ment_pas() -> None:
    ligne = pop_capacity_row(
        {"pop_name": "P", "subscribers": 1, "sold_down_mbps": 100.0, "capacity_mbps": None}
    )
    assert ligne["ratio"] is None
    assert ligne["peak_mbps"] is None
    assert ligne["peak_state"] == ETAT_INCONNU


# =========================================================================
# 2. L'occupation d'un lien
# =========================================================================


def test_une_occupation_au_dela_de_cent_pour_cent_est_rendue_telle_quelle() -> None:
    """Ce n'est pas une aberration a masquer : c'est le signe que la capacite
    retenue est sous-estimee, et c'est exactement ce qu'il faut voir."""
    assert occupancy(1_200_000_000.0, 1000.0) == 1.2


@pytest.mark.parametrize(
    ("part", "attendu"),
    [(None, ETAT_INCONNU), (0.5, ETAT_LIBRE), (0.8, ETAT_CHARGE), (0.95, ETAT_SATURE)],
)
def test_les_paliers_d_occupation(part, attendu: str) -> None:
    assert etat_du_lien(part) == attendu


def test_la_pointe_d_un_lien_retient_la_direction_la_plus_chargee() -> None:
    """rx et tx n'ont pas de sens absolu : selon que le voisin soit en amont ou
    en aval, le meme tx est du descendant ou du montant. On ne devine pas, on
    NOMME la direction retenue."""
    ligne = link_row(
        {
            "router_name": "pop-test",
            "interface": "ether2",
            "link_name": "BH-Nord",
            "capacity_mbps": 1000.0,
            "peak_rx_bps": 120_000_000.0,
            "peak_tx_bps": 960_000_000.0,
            "avg_bps": 300_000_000.0,
            "peak_rx_at": NOW,
            "peak_tx_at": NOW,
        }
    )
    assert ligne["peak_direction"] == "tx"
    assert ligne["peak_mbps"] == 960.0
    assert ligne["share"] == 0.96
    assert ligne["state"] == ETAT_SATURE
    assert ligne["peak_at"] == NOW


def test_l_heure_de_pointe_suit_la_direction_retenue() -> None:
    tot = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    ligne = link_row(
        {
            "router_name": "r",
            "interface": "ether1",
            "capacity_mbps": 100.0,
            "peak_rx_bps": 90_000_000.0,
            "peak_tx_bps": 10_000_000.0,
            "peak_rx_at": tot,
            "peak_tx_at": NOW,
        }
    )
    assert (ligne["peak_direction"], ligne["peak_at"]) == ("rx", tot)


def test_un_port_sans_capacite_connue_reste_lisible() -> None:
    ligne = link_row(
        {"router_name": "r", "interface": "sfp1", "peak_rx_bps": 1.0, "peak_tx_bps": 2.0}
    )
    assert ligne["share"] is None
    assert ligne["state"] == ETAT_INCONNU


# =========================================================================
# 3. L'usage : le volume, et le temps passe au plafond
# =========================================================================


def test_le_volume_est_rendu_en_giga_octets() -> None:
    ligne = usage_row({"login": "dupont", "bytes": 42_000_000_000.0, "samples": 10})
    assert ligne["gigabytes"] == 42.0


def test_un_abonne_qui_vit_dans_son_plan_est_signale() -> None:
    """Il ne PROFITE plus de son plan, il vit dedans : candidat a une offre
    superieure, ou plan mal taille. Le dire vaut mieux que le pourcentage."""
    ligne = usage_row({"login": "dupont", "bytes": 1.0, "samples": 100, "capped_samples": 40})
    assert ligne["capped_share"] == 0.4
    assert ligne["at_plan_ceiling"] is True


def test_un_abonne_qui_touche_rarement_son_plafond_n_est_pas_signale() -> None:
    ligne = usage_row({"login": "x", "bytes": 1.0, "samples": 100, "capped_samples": 5})
    assert ligne["at_plan_ceiling"] is False


def test_sans_echantillon_la_part_au_plafond_est_inconnue_pas_nulle() -> None:
    ligne = usage_row({"login": "x", "bytes": 0.0, "samples": 0})
    assert ligne["capped_share"] is None
    assert ligne["at_plan_ceiling"] is False


# =========================================================================
# 4. L'API
# =========================================================================


# =========================================================================
# 5. A renforcer : ce qui n'a plus de marge EN MOYENNE
# =========================================================================


def _lien(**surcharges):
    base = {
        "router_name": "pop-test",
        "interface": "ether2",
        "link_name": "BH-Nord",
        "capacity_mbps": 1000.0,
        "peak_rx_bps": 120_000_000.0,
        "peak_tx_bps": 900_000_000.0,
        "avg_bps": 850_000_000.0,
        "samples": 120,
        "peak_rx_at": NOW,
        "peak_tx_at": NOW,
    }
    return link_row({**base, **surcharges})


def _abonne(**surcharges):
    base = {
        "login": "dupont",
        "plan_down_mbps": 100.0,
        "bytes": 1.0,
        "samples": 120,
        "avg_bps": 92_000_000.0,
        "peak_bps": 99_000_000.0,
        "capped_samples": 60,
    }
    return usage_row({**base, **surcharges})


def test_un_lien_qui_vit_a_85_pour_cent_est_a_renforcer() -> None:
    """Le critere est la MOYENNE : une pointe a 100 % est normale un soir de
    match, une moyenne a 85 % dit que la prochaine croissance se paiera en
    latence."""
    resultat = a_renforcer([_lien()], [])
    assert [ligne["link_name"] for ligne in resultat["links"]] == ["BH-Nord"]
    assert resultat["links"][0]["avg_share"] == 0.85


def test_une_pointe_haute_sur_un_lien_calme_ne_declenche_rien() -> None:
    """C'est exactement l'erreur a ne pas faire : acheter un backhaul parce
    qu'un soir a 100 % s'est produit une fois."""
    resultat = a_renforcer([_lien(avg_bps=200_000_000.0)], [])
    assert resultat["links"] == []


def test_une_moyenne_calculee_sur_trois_points_ne_justifie_rien() -> None:
    """Sans minimum d'echantillons, trois mesures prises pendant un pic
    feraient acheter un backhaul."""
    assert a_renforcer([_lien(samples=3)], [])["links"] == []


def test_un_lien_sans_capacite_connue_n_est_pas_classe() -> None:
    """On ne sait pas s'il a de la marge : le dire serait inventer."""
    assert a_renforcer([_lien(capacity_mbps=None)], [])["links"] == []


def test_un_abonne_qui_vit_dans_son_plan_est_une_vente_pas_un_renfort() -> None:
    """Les deux listes sont separees : un lien sature se renforce, un abonne
    sature se vend. Les melanger ferait passer une opportunite commerciale pour
    un probleme d'ingenierie."""
    resultat = a_renforcer([], [_abonne()])
    assert resultat["links"] == []
    assert [u["login"] for u in resultat["subscribers"]] == ["dupont"]
    assert resultat["subscribers"][0]["avg_share"] == 0.92


def test_le_classement_met_le_plus_charge_en_premier() -> None:
    """La premiere ligne est celle qui coute le plus cher a laisser en l'etat."""
    resultat = a_renforcer(
        [_lien(link_name="calme", avg_bps=810_000_000.0), _lien(link_name="pire")], []
    )
    assert [ligne["link_name"] for ligne in resultat["links"]] == ["pire", "calme"]


def test_api_rend_les_quatre_analyses(client: TestClient) -> None:
    corps = client.get("/api/v1/capacity?hours=6&usage_hours=24&silent_days=3").json()

    assert corps["window"] == {"hours": 6, "usage_hours": 24, "silent_days": 3}
    assert [p["pop_name"] for p in corps["pops"]] == ["PoP Test", "PoP Sans Radio"]
    assert corps["links"][0]["link_name"] == "BH-Nord"
    assert corps["usage"][0]["gigabytes"] == 42.0
    assert corps["silent"][0]["login"] == "ecole-dosso"


def test_api_compte_ce_qui_appelle_une_decision(client: TestClient) -> None:
    corps = client.get("/api/v1/capacity").json()
    assert corps["totals"]["subscribers_at_ceiling"] == 1
    assert corps["totals"]["silent"] == 1


def test_api_un_pop_sans_capacite_ne_fausse_pas_le_total(client: TestClient) -> None:
    """Le PoP sans radio pese dans le vendu, pas dans la capacite : le rapport
    global doit rester celui d'un reseau, pas une division par zero."""
    corps = client.get("/api/v1/capacity").json()
    sans_radio = next(p for p in corps["pops"] if p["pop_name"] == "PoP Sans Radio")
    assert sans_radio["verdict"] == VERDICT_SANS_CAPACITE
    assert corps["totals"]["capacity_mbps"] == 420.0
    assert corps["totals"]["sold_down_mbps"] == 500.0


def test_l_onglet_connexion_a_distance_n_existe_plus(client: TestClient) -> None:
    """Il relisait l'inventaire que l'onglet Equipements affiche deja, sans rien
    tester de plus : deux ecrans pour une seule verite finissent par se
    contredire."""
    assert client.get("/api/v1/remote/status").status_code == 404
    assert "/api/v1/remote/status" not in client.app.openapi()["paths"]

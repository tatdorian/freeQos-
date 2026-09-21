"""Rattachement des flux aux abonnes : la partie ou une erreur se paie en factures.

TROIS RISQUES, TROIS FAMILLES DE TESTS
--------------------------------------
1. ATTRIBUER UN OCTET AU MAUVAIS CLIENT. Un bloc /29 et un /32 a l'interieur
   designent deux abonnes differents ; c'est le plus precis qui gagne.
2. COMPTER DEUX FOIS LE MEME OCTET. Il traverse le PoP puis la sortie internet,
   et les deux l'exportent. Le point de mesure doit rester dans la cle.
3. PRENDRE UNE ADRESSE POUR UN CLIENT. Ce qui parle sans etre declare va dans
   une liste d'aide a la saisie, jamais dans l'inventaire.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collectors.netflow import Flow
from app.services.flows import (
    RESEAUX_CLIENTS_PAR_DEFAUT,
    FlowAggregator,
    PrefixIndex,
    classify,
)

MAINTENANT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def agregateur(**kwargs: object) -> FlowAggregator:
    parametres: dict[str, object] = {
        "index": PrefixIndex.build([("10.0.0.0/29", 1), ("10.0.0.5/32", 2)]),
        "customer_networks": FlowAggregator.parse_networks(list(RESEAUX_CLIENTS_PAR_DEFAUT)),
    }
    parametres.update(kwargs)
    return FlowAggregator(**parametres)  # type: ignore[arg-type]


def flux(src: str, dst: str, **kwargs: object) -> Flow:
    defauts: dict[str, object] = {
        "src_port": 51000,
        "dst_port": 443,
        "protocol": 6,
        "octets": 1000,
        "packets": 10,
    }
    defauts.update(kwargs)
    return Flow(src=src, dst=dst, **defauts)  # type: ignore[arg-type]


# =========================================================================
# 1. A QUI APPARTIENT CET OCTET
# =========================================================================


def test_le_prefixe_le_plus_precis_gagne() -> None:
    """UN /32 DANS UN /29 N'EST PAS UNE AMBIGUITE, C'EST UNE HIERARCHIE.

    Un professionnel se voit vendre un bloc, et une machine de ce bloc peut
    avoir son propre contrat. Rendre le /29 pour les deux facturerait le second
    au premier.
    """
    index = PrefixIndex.build([("10.0.0.0/29", 1), ("10.0.0.5/32", 2)])
    assert index.lookup("10.0.0.5") == 2
    assert index.lookup("10.0.0.2") == 1
    assert index.lookup("10.0.0.9") is None


def test_une_adresse_hors_de_tout_bloc_n_appartient_a_personne() -> None:
    index = PrefixIndex.build([("10.0.0.0/29", 1)])
    assert index.lookup("8.8.8.8") is None
    assert index.lookup("pas-une-adresse") is None


def test_ipv4_et_ipv6_ne_se_melangent_pas() -> None:
    index = PrefixIndex.build([("10.0.0.0/8", 1), ("2001:db8::/32", 2)])
    assert index.lookup("10.1.2.3") == 1
    assert index.lookup("2001:db8::1") == 2
    assert index.lookup("2001:db9::1") is None


def test_un_prefixe_illisible_est_ignore_sans_casser_l_index() -> None:
    """Une fiche mal saisie ne doit pas faire perdre le trafic de tous les
    autres."""
    index = PrefixIndex.build([("pas-un-reseau", 1), ("10.0.0.0/29", 2)])
    assert index.lookup("10.0.0.1") == 2


# =========================================================================
# 2. LE SENS SE DEDUIT DE L'ABONNE
# =========================================================================


def test_vers_l_abonne_est_du_descendant_depuis_lui_du_montant() -> None:
    agg = agregateur()
    agg.add(flux("1.1.1.1", "10.0.0.5"), vantage="edge")
    agg.add(flux("10.0.0.2", "1.1.1.1", octets=300, packets=4), vantage="edge")
    lot = agg.flush(MAINTENANT)

    par_abonne = {c.subscriber_id: c for c in lot.subscribers}
    assert par_abonne[2].down_bytes == 1000
    assert par_abonne[2].up_bytes == 0
    assert par_abonne[1].up_bytes == 300
    assert par_abonne[1].down_bytes == 0


def test_un_flux_entre_deux_abonnes_compte_des_deux_cotes() -> None:
    """Il EST du descendant pour l'un et du montant pour l'autre : ce n'est pas
    un double comptage, c'est deux faits differents."""
    agg = agregateur()
    agg.add(flux("10.0.0.2", "10.0.0.5", octets=500), vantage="pop")
    par_abonne = {c.subscriber_id: c for c in agg.flush(MAINTENANT).subscribers}
    assert par_abonne[1].up_bytes == 500
    assert par_abonne[2].down_bytes == 500


def test_l_echantillonnage_est_applique_aux_octets() -> None:
    """SANS LUI, UN ROUTEUR EN 1:1000 RAPPORTE UN MILLIEME DU TRAFIC.

    Et rien ne le montre : les chiffres restent plausibles, juste mille fois
    trop petits.
    """
    agg = agregateur()
    agg.add(
        flux("1.1.1.1", "10.0.0.5", octets=1000, packets=10), vantage="edge", sampling_rate=1000
    )
    compteurs = agg.flush(MAINTENANT).subscribers[0]
    assert compteurs.down_bytes == 1_000_000
    assert compteurs.down_packets == 10_000


# =========================================================================
# 3. LE MEME OCTET VU DEUX FOIS
# =========================================================================


def test_les_deux_points_de_mesure_restent_separes() -> None:
    """LE COEUR DU MONTAGE. Le controleur ecoute en amont du coeur ET au PoP :
    le meme flux est donc exporte deux fois. Fusionner les deux series
    doublerait la consommation de chaque abonne."""
    agg = agregateur()
    agg.add(flux("1.1.1.1", "10.0.0.5", octets=1000), vantage="edge")
    agg.add(flux("1.1.1.1", "10.0.0.5", octets=1000), vantage="pop")
    lot = agg.flush(MAINTENANT)

    assert len(lot.subscribers) == 2
    assert {c.vantage for c in lot.subscribers} == {"edge", "pop"}
    assert all(c.down_bytes == 1000 for c in lot.subscribers)


# =========================================================================
# 4. UNE ADRESSE QUI PARLE N'EST PAS UN CLIENT
# =========================================================================


def test_une_adresse_non_declaree_va_dans_l_aide_a_la_saisie() -> None:
    agg = agregateur()
    agg.add(
        flux("172.16.9.9", "8.8.8.8", octets=90, vlan=812),
        vantage="pop",
        exporter="10.10.0.1",
        pop_name="PoP Nord",
    )
    lot = agg.flush(MAINTENANT)

    assert lot.subscribers == []
    assert len(lot.hosts) == 1
    assert (lot.hosts[0].address, lot.hosts[0].vlan_id) == ("172.16.9.9", 812)
    assert lot.hosts[0].pop_name == "PoP Nord"


def test_l_autre_bout_d_une_conversation_internet_n_est_pas_retenu() -> None:
    """SANS CE FILTRE, LA LISTE DEVIENDRAIT UN ANNUAIRE D'INTERNET.

    Chaque serveur contacte par un abonne y apparaitrait comme un candidat.
    """
    agg = agregateur()
    agg.add(flux("203.0.113.7", "198.51.100.9"), vantage="edge")
    assert agg.flush(MAINTENANT).hosts == []


def test_un_abonne_declare_ne_figure_jamais_dans_les_candidats() -> None:
    agg = agregateur()
    agg.add(flux("1.1.1.1", "10.0.0.5"), vantage="edge")
    lot = agg.flush(MAINTENANT)
    assert lot.hosts == []
    assert agg.flows_matched == 1


def test_une_vlan_bavarde_ne_noie_pas_la_liste() -> None:
    agg = agregateur(host_limit=3)
    for i in range(50):
        agg.add(flux(f"172.16.0.{i}", "8.8.8.8", vlan=900), vantage="pop")
    assert len(agg.flush(MAINTENANT).hosts) == 3


def test_on_peut_couper_completement_le_suivi_des_hotes() -> None:
    agg = agregateur(track_hosts=False)
    agg.add(flux("172.16.9.9", "8.8.8.8", vlan=812), vantage="pop")
    assert agg.flush(MAINTENANT).hosts == []


# =========================================================================
# 5. FAMILLES D'USAGE
# =========================================================================


def test_le_port_de_service_est_le_plus_petit_des_deux() -> None:
    """Le port ephemere du client est tire au-dessus de 32768. Prendre le port
    source au hasard classerait la moitie du web en 'autre'."""
    assert classify(flux("10.0.0.5", "1.1.1.1", src_port=51000, dst_port=443)) == "web"
    assert classify(flux("1.1.1.1", "10.0.0.5", src_port=443, dst_port=51000)) == "web"


def test_les_familles_couvrent_les_usages_courants() -> None:
    assert classify(flux("a", "b", src_port=53, dst_port=40000, protocol=17)) == "dns"
    assert classify(flux("a", "b", src_port=5060, dst_port=40000)) == "voix / visio"
    assert classify(flux("a", "b", src_port=6881, dst_port=40000)) == "p2p"
    assert classify(flux("a", "b", src_port=0, dst_port=0, protocol=1)) == "diagnostic"
    assert classify(flux("a", "b", src_port=40001, dst_port=40002)) == "autre"


def test_la_repartition_par_usage_suit_le_sens() -> None:
    agg = agregateur()
    agg.add(flux("1.1.1.1", "10.0.0.5", src_port=443, dst_port=51000, octets=800), vantage="edge")
    agg.add(
        flux("10.0.0.5", "9.9.9.9", src_port=51001, dst_port=53, protocol=17, octets=60),
        vantage="edge",
    )
    par_usage = {(a.subscriber_id, a.app): a for a in agg.flush(MAINTENANT).apps}
    assert par_usage[(2, "web")].down_bytes == 800
    assert par_usage[(2, "dns")].up_bytes == 60


# =========================================================================
# 6. LA FENETRE
# =========================================================================


def test_vider_la_fenetre_remet_les_compteurs_a_zero() -> None:
    """Sans cela, chaque fenetre reecrirait le cumul depuis le demarrage : la
    consommation d'un abonne croitrait indefiniment."""
    agg = agregateur()
    agg.add(flux("1.1.1.1", "10.0.0.5"), vantage="edge")
    assert agg.flush(MAINTENANT).subscribers
    assert agg.flush(MAINTENANT).empty

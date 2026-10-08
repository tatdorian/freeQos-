"""Le trafic se mesure dans LES DEUX SENS.

CE QUI SE PERDAIT
-----------------
1. LE NAT. Une sortie internet qui masque ses clients voit le retour arriver
   sur SON adresse publique : le descendant n'etait rattache a personne, et
   l'on ne mesurait que le montant -- precisement au point qui sert au decompte.
2. LE CHOIX D'UN SEUL EXPORTEUR. Par point de vue, l'exporteur qui voyait le
   plus de trafic (les deux sens additionnes) etait seul retenu : le sens qu'un
   AUTRE routeur etait seul a porter (routage asymetrique) disparaissait.
3. UNE SOURCE PAR CONVERSATION. La liste "qui parle a qui" retenait un seul
   routeur par conversation, quel que soit le sens.
"""

from __future__ import annotations

import ipaddress
import struct
from datetime import UTC, datetime

from app.collectors.netflow import V9_HEADER, Flow, NetflowDecoder
from app.services.flows import FlowAggregator, PrefixIndex
from app.services.netflow_export import CHAMPS_NAT, PATH_IPFIX
from tests.conftest import FakeRouterOsClient
from tests.test_export_netflow import COLLECTEUR, COMMENTAIRE_CIBLE, configure, service

CLIENT = "10.0.0.5"
PUBLIQUE = "203.0.113.10"  # l'adresse de la sortie internet, apres NAT
SERVEUR = "93.184.216.34"


def _agregat() -> FlowAggregator:
    return FlowAggregator(index=PrefixIndex.build([(f"{CLIENT}/32", 7)]))


def _montant(octets: int = 100, *, port: int = 51000, **autres: object) -> Flow:
    return Flow(
        src=CLIENT,
        dst=SERVEUR,
        src_port=port,
        dst_port=443,
        protocol=6,
        octets=octets,
        packets=1,
        **autres,  # type: ignore[arg-type]
    )


def _retour(
    octets: int = 5000, *, vers: str = PUBLIQUE, port: int = 51000, **autres: object
) -> Flow:
    return Flow(
        src=SERVEUR,
        dst=vers,
        src_port=443,
        dst_port=port,
        protocol=6,
        octets=octets,
        packets=4,
        **autres,  # type: ignore[arg-type]
    )


def _compte(lot: object, point: str = "edge") -> tuple[int, int]:
    [c] = [c for c in lot.subscribers if c.vantage == point]  # type: ignore[attr-defined]
    return c.down_bytes, c.up_bytes


# ============================================================ decodage NAT


def test_les_adresses_traduites_sont_decodees() -> None:
    champs = [
        (8, 4),
        (12, 4),
        (7, 2),
        (11, 2),
        (4, 1),
        (1, 4),
        (2, 4),
        (225, 4),
        (226, 4),
        (227, 2),
        (228, 2),
    ]
    enregistrement = (
        ipaddress.IPv4Address(SERVEUR).packed
        + ipaddress.IPv4Address(PUBLIQUE).packed
        + struct.pack("!HHBII", 443, 62000, 6, 5000, 4)
        + ipaddress.IPv4Address(SERVEUR).packed
        + ipaddress.IPv4Address(CLIENT).packed
        + struct.pack("!HH", 443, 51000)
    )
    modele = struct.pack("!HH", 256, len(champs)) + b"".join(struct.pack("!HH", *c) for c in champs)
    corps = struct.pack("!HH", 0, 4 + len(modele)) + modele
    bourrage = (-len(enregistrement)) % 4
    corps += struct.pack("!HH", 256, 4 + len(enregistrement) + bourrage)
    corps += enregistrement + b"\x00" * bourrage
    paquet = V9_HEADER.pack(9, 1, 1000, 1_700_000_000, 1, 1) + corps

    [flux] = NetflowDecoder().decode(paquet, "192.0.2.1").flows
    assert (flux.dst, flux.post_dst, flux.post_dst_port) == (PUBLIQUE, CLIENT, 51000)
    assert flux.post_src == SERVEUR


# ======================================================= rattachement NAT


def test_le_retour_vers_l_adresse_publique_est_rattache_par_le_champ_nat() -> None:
    agregat = _agregat()
    agregat.add(_montant(post_src=PUBLIQUE), vantage="edge", exporter="ccr")
    agregat.add(_retour(post_dst=CLIENT), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")

    assert _compte(lot) == (5000, 100)
    assert agregat.nat_translated == 1
    # Et l'autre bout est connu dans les deux sens.
    [dest] = lot.destinations
    assert (dest.client, dest.address, dest.down_bytes, dest.up_bytes) == (
        CLIENT,
        SERVEUR,
        5000,
        100,
    )


def test_sans_champ_nat_le_retour_est_apparie_a_son_montant() -> None:
    """Le routeur n'exporte pas les adresses traduites : le retour revient du
    meme serveur, du meme port, vers le port du client. C'est le sien."""
    agregat = _agregat()
    agregat.add(_montant(), vantage="edge", exporter="ccr")
    agregat.add(_retour(), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")

    assert _compte(lot) == (5000, 100)
    assert agregat.nat_matched == 1
    assert lot.destinations[0].down_bytes == 5000


def test_un_retour_arrive_avant_son_montant_est_rejoue_au_flush() -> None:
    agregat = _agregat()
    agregat.add(_retour(), vantage="edge", exporter="ccr")
    agregat.add(_montant(), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")

    assert _compte(lot) == (5000, 100)
    assert agregat.nat_matched == 1
    assert agregat.nat_unmatched == 0


def test_la_correspondance_survit_a_la_fenetre() -> None:
    """Un telechargement long : le montant (des accuses de reception) a ete vu
    a la fenetre precedente, le retour arrive maintenant."""
    agregat = _agregat()
    agregat.add(_montant(), vantage="edge", exporter="ccr")
    agregat.flush(datetime.now(tz=UTC), vantage="edge")
    agregat.add(_retour(), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert _compte(lot) == (5000, 0)


def test_un_retour_sans_montant_n_est_credite_a_personne() -> None:
    agregat = _agregat()
    agregat.add(_retour(), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert lot.subscribers == []
    assert agregat.nat_unmatched == 1


def test_port_traduit_appariement_large_vers_une_adresse_nat_connue() -> None:
    """Le routeur a change le port du client. Sans port, on n'apparie que vers
    une adresse deja reconnue comme celle du NAT -- et que si un seul abonne
    parle a ce serveur."""
    agregat = _agregat()
    agregat.add(_montant(port=40000, post_src=PUBLIQUE), vantage="edge", exporter="ccr")
    agregat.add(_retour(port=62000), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert _compte(lot) == (5000, 100)


def test_une_machine_publique_inconnue_n_est_pas_creditee_a_un_abonne() -> None:
    """Meme serveur, meme port distant, mais vers une adresse qui n'a jamais
    ete vue comme celle du NAT : ce n'est pas un retour, on ne devine pas."""
    agregat = _agregat()
    agregat.add(_montant(port=40000), vantage="edge", exporter="ccr")
    agregat.add(_retour(vers="198.51.100.77", port=62000), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert _compte(lot) == (0, 100)


def test_deux_abonnes_sur_le_meme_serveur_ne_sont_pas_devines() -> None:
    agregat = FlowAggregator(index=PrefixIndex.build([(f"{CLIENT}/32", 7), ("10.0.0.6/32", 8)]))
    agregat.add(_montant(port=40000, post_src=PUBLIQUE), vantage="edge", exporter="ccr")
    agregat.add(
        Flow(src="10.0.0.6", dst=SERVEUR, src_port=40001, dst_port=443, protocol=6, octets=50),
        vantage="edge",
        exporter="ccr",
    )
    agregat.add(_retour(port=62000), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert sum(c.down_bytes for c in lot.subscribers) == 0
    assert agregat.nat_unmatched == 1


# ================================================= un exporteur par sens


def test_chaque_sens_vient_de_l_exporteur_qui_le_voit() -> None:
    """Routage asymetrique : le montant sort par un routeur, le descendant
    revient par un autre. Les deux sont du meme point de vue, aucun ne voit
    tout -- et garder le "meilleur" jetait l'autre sens."""
    agregat = _agregat()
    agregat.add(_montant(300), vantage="pop", exporter="nas-a")
    agregat.add(_retour(9000, vers=CLIENT), vantage="pop", exporter="nas-b")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="pop")

    assert _compte(lot, "pop") == (9000, 300)
    descendant = sum(a.down_bytes for a in lot.apps)
    montant = sum(a.up_bytes for a in lot.apps)
    assert (descendant, montant) == (9000, 300)


def test_le_meme_sens_vu_deux_fois_n_est_pas_additionne() -> None:
    agregat = _agregat()
    for nas in ("nas-a", "nas-b"):
        agregat.add(_montant(300), vantage="pop", exporter=nas)
        agregat.add(_retour(9000, vers=CLIENT), vantage="pop", exporter=nas)
    lot = agregat.flush(datetime.now(tz=UTC), vantage="pop")
    assert _compte(lot, "pop") == (9000, 300)


def test_en_auto_les_applications_suivent_le_meilleur_point_par_sens() -> None:
    """La bordure ne voit que le montant (NAT sans champs), le PoP voit tout :
    les applications prennent le descendant au PoP, sans rien doubler."""
    agregat = _agregat()
    agregat.add(_montant(300), vantage="edge", exporter="ccr")
    agregat.add(_montant(280), vantage="pop", exporter="nas")
    agregat.add(_retour(9000, vers=CLIENT), vantage="pop", exporter="nas")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="auto")

    assert sum(a.down_bytes for a in lot.apps) == 9000
    assert sum(a.up_bytes for a in lot.apps) == 300


def test_les_conversations_gardent_une_source_par_sens() -> None:
    """Le PoP n'exporte que le montant, la bordure voit le descendant : la
    conversation garde les deux, chacun d'une seule source."""
    agregat = _agregat()
    agregat.add(_montant(300), vantage="pop", exporter="nas")
    agregat.add(_retour(9000, vers=CLIENT), vantage="edge", exporter="ccr")
    agregat.add(_montant(300), vantage="edge", exporter="ccr")  # deja vu au PoP
    lot = agregat.flush(datetime.now(tz=UTC), vantage="auto")

    [dest] = lot.destinations
    assert (dest.down_bytes, dest.up_bytes) == (9000, 300)


def test_le_bilan_par_sens_est_tenu_par_point_de_vue() -> None:
    agregat = _agregat()
    agregat.add(_montant(300), vantage="edge", exporter="ccr")
    agregat.add(_retour(9000, vers=CLIENT), vantage="pop", exporter="nas")
    assert agregat.direction_bytes == {"edge": [0, 300], "pop": [9000, 0]}


# =================================================== reglage et routeurs


def test_le_decompte_auto_est_le_defaut() -> None:
    from app.config import Settings
    from app.services.netflow_service import NetflowService

    assert Settings(_env_file=None).netflow_accounting_vantage == "auto"  # type: ignore[call-arg]
    assert NetflowService(accounting_vantage="auto").effective_vantage == "auto"


async def test_les_champs_nat_coupes_sont_actives_sur_le_routeur() -> None:
    client = FakeRouterOsClient()
    client.traffic_flow_row = configure()
    client.traffic_flow_target_rows = [
        {
            ".id": "*1",
            "dst-address": COLLECTEUR,
            "port": "2055",
            "version": "9",
            "comment": COMMENTAIRE_CIBLE,
        }
    ]
    client.traffic_flow_ipfix_row = dict.fromkeys(CHAMPS_NAT, "false") | {"bytes": "true"}
    export, collector = service(client)
    etat = await export.state_of(collector)
    plan = export.plan_for(collector, etat)

    assert etat.state == "a poser"
    assert "NAT" in etat.reason
    [action] = plan.actions
    assert action.path == PATH_IPFIX
    assert action.fields == dict.fromkeys(CHAMPS_NAT, "yes")


async def test_des_champs_nat_deja_exportes_ne_coutent_aucune_ecriture() -> None:
    client = FakeRouterOsClient()
    client.traffic_flow_row = configure()
    client.traffic_flow_target_rows = [
        {
            ".id": "*1",
            "dst-address": COLLECTEUR,
            "port": "2055",
            "version": "9",
            "comment": COMMENTAIRE_CIBLE,
        }
    ]
    client.traffic_flow_ipfix_row = dict.fromkeys(CHAMPS_NAT, "true")
    export, collector = service(client)
    etat = await export.state_of(collector)
    assert etat.state == "pose"
    assert export.plan_for(collector, etat).is_empty


def test_une_table_nat_pleine_continue_d_apprendre() -> None:
    """Pleine, la table evince les correspondances les plus vieilles : le
    client qui vient d'ouvrir une connexion doit etre reconnu."""
    agregat = _agregat()
    agregat.nat_table_limit = 10
    for port in range(40000, 40030):
        agregat.add(_montant(port=port), vantage="edge", exporter="ccr")
    assert len(agregat._nat_exact) <= 10  # noqa: SLF001
    agregat.add(_retour(port=40029), vantage="edge", exporter="ccr")
    lot = agregat.flush(datetime.now(tz=UTC), vantage="edge")
    assert _compte(lot)[0] == 5000

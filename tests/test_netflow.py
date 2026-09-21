"""Decodage NetFlow : ce qu'on lit, et ce qu'on refuse de lire.

UNE ERREUR DE DECODAGE NE SE VOIT PAS. Un champ mal cadre ne fait pas planter le
collecteur : il produit des octets plausibles attribues au mauvais abonne. C'est
pour cela que ce fichier construit des datagrammes OCTET PAR OCTET plutot que de
faire confiance a une bibliotheque : le test doit echouer sur un decalage d'un
octet, pas sur une exception.
"""

from __future__ import annotations

import ipaddress
import struct

import pytest

from app.collectors.netflow import (
    IPFIX_HEADER,
    V5_HEADER,
    V5_RECORD,
    V9_HEADER,
    NetflowDecoder,
    NetflowParseError,
)

# (type, longueur) des champs du modele employe partout ici.
CHAMPS = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (58, 2), (1, 4), (2, 4)]


def enregistrement(
    src: str = "10.0.0.5",
    dst: str = "1.1.1.1",
    *,
    src_port: int = 51000,
    dst_port: int = 443,
    protocol: int = 6,
    vlan: int = 812,
    octets: int = 4096,
    paquets: int = 7,
) -> bytes:
    return (
        ipaddress.IPv4Address(src).packed
        + ipaddress.IPv4Address(dst).packed
        + struct.pack("!HHBHII", src_port, dst_port, protocol, vlan, octets, paquets)
    )


def paquet_v9(templates: list, datasets: list, *, source_id: int = 1, seq: int = 1) -> bytes:
    corps = b""
    for tid, champs in templates:
        modele = struct.pack("!HH", tid, len(champs)) + b"".join(
            struct.pack("!HH", *c) for c in champs
        )
        corps += struct.pack("!HH", 0, 4 + len(modele)) + modele
    for tid, enregistrements in datasets:
        charge = b"".join(enregistrements)
        bourrage = (-len(charge)) % 4
        corps += struct.pack("!HH", tid, 4 + len(charge) + bourrage) + charge + b"\x00" * bourrage
    return V9_HEADER.pack(9, 1, 1000, 1_700_000_000, seq, source_id) + corps


def paquet_ipfix(templates: list, datasets: list, *, domain: int = 7) -> bytes:
    corps = b""
    for tid, champs in templates:
        modele = struct.pack("!HH", tid, len(champs)) + b"".join(
            struct.pack("!HH", *c) for c in champs
        )
        corps += struct.pack("!HH", 2, 4 + len(modele)) + modele
    for tid, enregistrements in datasets:
        charge = b"".join(enregistrements)
        corps += struct.pack("!HH", tid, 4 + len(charge)) + charge
    return IPFIX_HEADER.pack(10, IPFIX_HEADER.size + len(corps), 1_700_000_000, 3, domain) + corps


# =========================================================================
# v5 : format fige, aucun modele a apprendre
# =========================================================================


def paquet_v5(nombre: int = 1, *, annonce: int | None = None, sampling: int = 0) -> bytes:
    entete = V5_HEADER.pack(
        5, annonce if annonce is not None else nombre, 1000, 1_700_000_000, 0, 1, 0, 0, sampling
    )
    corps = b"".join(
        V5_RECORD.pack(
            ipaddress.IPv4Address("8.8.8.8").packed,
            ipaddress.IPv4Address("10.0.0.5").packed,
            b"\x00" * 4,
            1,
            2,
            10,
            15_000,
            0,
            0,
            443,
            51_000,
            0,
            0x18,
            6,
            0,
            0,
            0,
            32,
            32,
            0,
        )
        for _ in range(nombre)
    )
    return entete + corps


def test_v5_se_decode_sans_modele() -> None:
    paquet = NetflowDecoder().decode(paquet_v5(2), "192.0.2.1")
    assert paquet.version == 5
    assert len(paquet.flows) == 2
    flux = paquet.flows[0]
    assert (flux.src, flux.dst) == ("8.8.8.8", "10.0.0.5")
    assert (flux.src_port, flux.dst_port, flux.protocol) == (443, 51_000, 6)
    assert (flux.octets, flux.packets) == (15_000, 10)


def test_v5_ne_lit_jamais_plus_que_le_datagramme() -> None:
    """UN COMPTEUR QUI MENT NE DOIT PAS FAIRE SORTIR DU TAMPON.

    Un datagramme tronque en vol annonce toujours son nombre d'origine. Lire
    jusqu'a ce nombre reviendrait a decoder de la memoire voisine comme si
    c'etaient des octets factures.
    """
    paquet = NetflowDecoder().decode(paquet_v5(1, annonce=25), "192.0.2.1")
    assert len(paquet.flows) == 1


def test_v5_rend_l_echantillonnage_de_son_entete() -> None:
    """Les 14 bits bas portent l'intervalle, les 2 hauts le mode."""
    paquet = NetflowDecoder().decode(paquet_v5(1, sampling=(1 << 14) | 1000), "192.0.2.1")
    assert paquet.sampling_interval == 1000


# =========================================================================
# v9 et IPFIX : rien n'est lisible sans le modele
# =========================================================================


def test_v9_apprend_un_modele_puis_decode() -> None:
    decodeur = NetflowDecoder()
    paquet = decodeur.decode(
        paquet_v9([(256, CHAMPS)], [(256, [enregistrement(), enregistrement()])]), "192.0.2.9"
    )
    assert paquet.templates_learned == 1
    assert len(paquet.flows) == 2
    flux = paquet.flows[0]
    assert (flux.src, flux.dst, flux.vlan) == ("10.0.0.5", "1.1.1.1", 812)
    assert (flux.octets, flux.packets) == (4096, 7)


def test_des_donnees_sans_modele_sont_comptees_et_jetees() -> None:
    """C'EST LE CAS NORMAL APRES UN REDEMARRAGE, PAS UNE PANNE.

    Le modele est reemis toutes les quelques minutes ; entre-temps on ne sait
    pas lire. Inventer des octets serait pire que d'en perdre -- mais se taire
    aussi : "je ne recois rien" et "je recois sans savoir lire" appellent deux
    gestes opposes.
    """
    decodeur = NetflowDecoder()
    avant = decodeur.decode(paquet_v9([], [(256, [enregistrement()])]), "192.0.2.9")
    assert avant.flows == ()
    assert decodeur.orphan_records == 1

    decodeur.decode(paquet_v9([(256, CHAMPS)], []), "192.0.2.9")
    apres = decodeur.decode(paquet_v9([], [(256, [enregistrement()])]), "192.0.2.9")
    assert len(apres.flows) == 1


def test_deux_exporteurs_ne_partagent_pas_leurs_modeles() -> None:
    """Le numero de modele est LOCAL a l'exporteur.

    Deux routeurs emploient tres bien le meme 256 pour des champs differents.
    Un cache indexe sur le seul numero lirait les octets de l'un avec le cadrage
    de l'autre -- et produirait des chiffres plausibles, donc invisibles.
    """
    decodeur = NetflowDecoder()
    decodeur.decode(paquet_v9([(256, CHAMPS)], []), "192.0.2.9")
    autre = decodeur.decode(paquet_v9([], [(256, [enregistrement()])]), "192.0.2.10")
    assert autre.flows == ()
    assert decodeur.orphan_records == 1


def test_le_domaine_separe_aussi_les_modeles() -> None:
    decodeur = NetflowDecoder()
    decodeur.decode(paquet_v9([(256, CHAMPS)], [], source_id=1), "192.0.2.9")
    croise = decodeur.decode(paquet_v9([], [(256, [enregistrement()])], source_id=2), "192.0.2.9")
    assert croise.flows == ()


def test_ipfix_se_decode_comme_v9_mais_avec_ses_propres_jeux() -> None:
    paquet = NetflowDecoder().decode(
        paquet_ipfix([(300, CHAMPS)], [(300, [enregistrement()])]), "192.0.2.10"
    )
    assert paquet.version == 10
    assert paquet.domain == 7
    assert len(paquet.flows) == 1


def test_un_modele_d_options_ne_produit_aucun_flux() -> None:
    """LEURS ENREGISTREMENTS NE SONT PAS DES FLUX.

    Un jeu d'options porte des compteurs d'exporteur ou une table
    d'interfaces. Sans connaitre sa longueur on les lirait comme des
    conversations : des octets inventes, attribues a de vraies fiches.
    """
    decodeur = NetflowDecoder()
    corps = struct.pack("!HHH", 400, 2, 1) + struct.pack("!HH", 34, 4) + struct.pack("!HH", 42, 4)
    jeu = struct.pack("!HH", 3, 4 + len(corps)) + corps
    donnees = struct.pack("!HH", 400, 4 + 8) + b"\x00" * 8
    entete = IPFIX_HEADER.pack(10, IPFIX_HEADER.size + len(jeu) + len(donnees), 1_700_000_000, 1, 7)
    paquet = decodeur.decode(entete + jeu + donnees, "192.0.2.10")
    assert paquet.flows == ()
    assert decodeur.orphan_records == 0


def test_un_champ_constructeur_est_saute_a_la_bonne_longueur() -> None:
    """Un champ prive (bit de poids fort a 1) porte 4 octets de plus dans le
    modele. Les ignorer decalerait TOUT le reste de l'enregistrement."""
    champs = [(8, 4), (12, 4), (0x8001, 2), (1, 4)]
    corps = struct.pack("!HH", 301, len(champs))
    for type_id, longueur in champs:
        corps += struct.pack("!HH", type_id, longueur)
        if type_id & 0x8000:
            corps += struct.pack("!I", 9)
    jeu = struct.pack("!HH", 2, 4 + len(corps)) + corps
    enreg = (
        ipaddress.IPv4Address("10.0.0.5").packed
        + ipaddress.IPv4Address("1.1.1.1").packed
        + struct.pack("!HI", 1234, 999)
    )
    donnees = struct.pack("!HH", 301, 4 + len(enreg)) + enreg
    entete = IPFIX_HEADER.pack(10, IPFIX_HEADER.size + len(jeu) + len(donnees), 1_700_000_000, 1, 7)
    paquet = NetflowDecoder().decode(entete + jeu + donnees, "192.0.2.10")
    assert len(paquet.flows) == 1
    assert paquet.flows[0].octets == 999


def test_ipv6_est_lu_sur_ses_seize_octets() -> None:
    champs = [(27, 16), (28, 16), (1, 4), (2, 4)]
    enreg = (
        ipaddress.IPv6Address("2001:db8::5").packed
        + ipaddress.IPv6Address("2001:4860::8888").packed
        + struct.pack("!II", 5000, 3)
    )
    paquet = NetflowDecoder().decode(paquet_v9([(260, champs)], [(260, [enreg])]), "192.0.2.9")
    assert paquet.flows[0].src == "2001:db8::5"
    assert paquet.flows[0].octets == 5000


# =========================================================================
# Ce qui doit etre REFUSE
# =========================================================================


@pytest.mark.parametrize(
    "donnees",
    [
        b"",
        b"\x00",
        struct.pack("!H", 3) + b"\x00" * 40,  # version inconnue
        struct.pack("!H", 9) + b"\x00" * 4,  # en-tete v9 tronque
    ],
)
def test_un_datagramme_illisible_est_refuse_proprement(donnees: bytes) -> None:
    """Refuser explicitement, jamais planter : un collecteur qui meurt sur un
    paquet malforme perd tout le trafic des autres exporteurs."""
    with pytest.raises(NetflowParseError):
        NetflowDecoder().decode(donnees, "192.0.2.1")


def test_une_longueur_de_jeu_incoherente_arrete_la_lecture() -> None:
    """Continuer reviendrait a lire du bruit comme des octets factures."""
    entete = V9_HEADER.pack(9, 1, 1000, 1_700_000_000, 1, 1)
    jeu_ment = struct.pack("!HH", 256, 9999)
    paquet = NetflowDecoder().decode(entete + jeu_ment + b"\x00" * 8, "192.0.2.9")
    assert paquet.flows == ()

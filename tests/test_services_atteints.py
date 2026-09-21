"""Qui se connecte a quoi, et ce qu'on a le droit d'en conclure.

QUATRE RISQUES, QUATRE FAMILLES DE TESTS
-----------------------------------------
1. NOMMER A TORT. Dire "Netflix" d'une adresse qui ne l'est pas amene a
   bloquer le trafic de quelqu'un d'autre. Le rattachement par suffixe de nom
   inverse doit tomber sur une frontiere de label, et un CDN ne doit jamais
   devenir "streaming" tout seul.
2. PRENDRE L'INFRASTRUCTURE POUR UNE DESTINATION. Deux abonnes qui se parlent,
   un DNS interne, la supervision du PoP : rien de tout cela n'est un service
   atteint, et chacun declencherait une requete de nom inverse pour rien.
3. POSER UNE REGLE QUI NE RENCONTRE JAMAIS UN PAQUET. Une restriction dont le
   sens est inverse s'affiche comme posee et ne bloque rien : c'est pire qu'une
   restriction absente.
4. LAISSER UNE RESTRICTION FIGEE. Une regle est un critere, pas une photo :
   une adresse nouvellement decouverte doit entrer dans la liste du routeur, et
   une adresse qui n'en releve plus doit en sortir.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.collectors.netflow import Flow
from app.enforcement.models import MANAGED_COMMENT
from app.enforcement.restrictions import (
    PATH_ADDRESS_LIST,
    PATH_FILTER,
    PATH_MANGLE,
    PATH_QUEUE_TREE,
    RouterRestrictionState,
    RuleTarget,
    dst_list_name,
    merge_addresses,
    normalize_prefixes,
    packet_mark,
    parse_tag,
    plan_restrictions,
    tag,
)
from app.services import ipfinder
from app.services.flows import (
    RESEAUX_CLIENTS_PAR_DEFAUT,
    FlowAggregator,
    PrefixIndex,
    service_port,
)
from app.services.restrictions import InvalidRuleError, validate

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
# 1. METTRE UN NOM SUR UNE ADRESSE
# =========================================================================


def test_un_bloc_publie_nomme_son_service() -> None:
    verdict = ipfinder.match_prefix("45.57.12.34")
    assert verdict.service == "netflix"
    assert verdict.category == ipfinder.CAT_STREAMING
    assert verdict.source == ipfinder.SOURCE_CATALOGUE
    # Le bloc qui a repondu est rendu : c'est ce qui permet a l'exploitant de
    # verifier le raisonnement plutot que de croire le verdict sur parole.
    assert verdict.matched_prefix == "45.57.0.0/17"


def test_une_adresse_hors_catalogue_n_est_nommee_par_personne() -> None:
    assert ipfinder.match_prefix("203.0.113.9").service is None
    assert ipfinder.match_prefix("pas-une-adresse").service is None


def test_le_suffixe_doit_tomber_sur_une_frontiere_de_label() -> None:
    """SANS CELA, N'IMPORTE QUI SE FAIT PASSER POUR N'IMPORTE QUI.

    Un domaine achete pour l'occasion (``pasnflxvideo.net``) suffirait a se
    faire nommer Netflix -- et donc a echapper a une restriction posee dessus,
    ou a s'y faire prendre sans raison.
    """
    assert ipfinder.match_hostname("ipv4-c001.1.oca.nflxvideo.net").service == "netflix"
    assert ipfinder.match_hostname("nflxvideo.net").service == "netflix"
    assert ipfinder.match_hostname("pasnflxvideo.net").service is None
    assert ipfinder.match_hostname("nflxvideo.net.exemple.fr").service is None


def test_le_nom_inverse_l_emporte_sur_le_bloc() -> None:
    """UN CACHE HEBERGE CHEZ L'OPERATEUR N'EST DANS AUCUN BLOC PUBLIE.

    C'est justement le serveur qui porte le plus de trafic : le manquer serait
    manquer l'essentiel de ce qu'on cherchait a mesurer.
    """
    verdict = ipfinder.identify("203.0.113.9", hostname="ipv4-c001-par001.1.oca.nflxvideo.net")
    assert verdict.service == "netflix"
    assert verdict.source == ipfinder.SOURCE_RDNS

    # Et une adresse de CDN dont le nom inverse nomme le service reel doit se
    # lire comme ce service, pas comme le CDN qui l'heberge.
    sur_cdn = ipfinder.identify("151.101.1.1", hostname="video-edge-1.fra01.ttvnw.net")
    assert sur_cdn.service == "twitch"


def test_youtube_se_distingue_de_google_par_le_nom_seulement() -> None:
    """LES DEUX PARTAGENT LES MEMES BLOCS. Restreindre le bloc de Google pour
    viser YouTube couperait la recherche et Gmail au passage."""
    assert ipfinder.match_hostname("rr1---sn-abc.googlevideo.com").service == "youtube"
    assert ipfinder.match_prefix("142.250.1.1").service == "google"
    youtube = next(s for s in ipfinder.CATALOGUE if s.key == "youtube")
    assert youtube.prefixes == ()


def test_un_cdn_n_est_jamais_classe_en_streaming() -> None:
    """UN CDN PORTE TOUT LE MONDE. Le classer en streaming ferait qu'une regle
    'streaming' couperait des sites qui n'ont rien a voir -- sans que personne
    ne l'ait demande."""
    for cle in ("cloudflare", "akamai", "fastly"):
        service = ipfinder.PAR_CLE[cle]
        assert service.category == ipfinder.CAT_CDN
        # Et l'avertissement doit etre ecrit, pas sous-entendu.
        assert service.note


def test_une_famille_rassemble_ses_services() -> None:
    streaming = ipfinder.services_in_categories({ipfinder.CAT_STREAMING})
    assert {"netflix", "youtube", "twitch"} <= streaming
    assert "cloudflare" not in streaming


def test_choisir_un_service_donne_ses_blocs_publies() -> None:
    """C'EST CE QUI REND UNE RESTRICTION UTILE DES SA POSE. Sans les blocs, la
    premiere connexion vers chaque nouveau serveur passerait avant que la regle
    ne le connaisse."""
    blocs = ipfinder.service_prefixes({"netflix"})
    assert "45.57.0.0/17" in blocs
    assert ipfinder.service_prefixes({"youtube"}) == []


def test_seules_les_adresses_d_internet_sont_enrichies() -> None:
    assert ipfinder.is_routable("45.57.12.34")
    for interne in ("10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.1.1", "224.0.0.1"):
        assert not ipfinder.is_routable(interne), interne


# =========================================================================
# 2. CE QU'UN ABONNE ATTEINT
# =========================================================================


def test_le_sens_de_la_destination_suit_l_abonne() -> None:
    """Un flux qui ARRIVE chez le client vient de la destination (descendant) ;
    un flux qui en PART y va (montant). Inverser les deux ferait lire un
    telechargement comme un envoi."""
    agg = agregateur()
    agg.add(flux("45.57.12.34", "10.0.0.2", octets=5000), vantage="edge")
    agg.add(flux("10.0.0.2", "45.57.12.34", octets=300), vantage="edge")

    lot = agg.flush(MAINTENANT)
    assert len(lot.destinations) == 1
    destination = lot.destinations[0]
    assert destination.subscriber_id == 1
    assert destination.address == "45.57.12.34"
    assert destination.down_bytes == 5000
    assert destination.up_bytes == 300


def test_deux_abonnes_qui_se_parlent_ne_sont_une_destination_pour_personne() -> None:
    """L'AUTRE BOUT DOIT ETRE SUR INTERNET. Sinon la liste des services
    atteints se remplirait de l'infrastructure de l'exploitant, et chaque
    adresse interne declencherait une requete de nom inverse."""
    agg = agregateur()
    agg.add(flux("10.0.0.2", "10.0.0.5", octets=9000), vantage="pop")
    lot = agg.flush(MAINTENANT)
    assert lot.destinations == []
    # Le VOLUME, lui, est bien compte pour les deux : seule la destination est
    # ecartee, pas la mesure.
    assert len(lot.subscribers) == 2


def test_le_port_retenu_est_celui_du_service() -> None:
    agg = agregateur()
    agg.add(flux("45.57.12.34", "10.0.0.2", src_port=443, dst_port=51000), vantage="edge")
    destination = agg.flush(MAINTENANT).destinations[0]
    assert destination.port == 443
    assert service_port(flux("1.1.1.1", "10.0.0.2", src_port=53, dst_port=60000)) == 53


def test_une_fenetre_bavarde_est_plafonnee_et_le_dit() -> None:
    """UN ABONNE EN P2P TOUCHE DES MILLIERS D'ADRESSES PAR MINUTE. Sans plafond,
    une fenetre de collecte deviendrait une rafale d'ecritures. Le compteur
    d'ecartees existe pour qu'un plafond trop bas se voie, au lieu de laisser
    croire que ces abonnes n'atteignent rien."""
    agg = agregateur(destination_limit=2)
    for i in range(10):
        # Des adresses PUBLIQUES : les plages de documentation (203.0.113.0/24
        # et compagnie) sont classees privees par la bibliotheque standard, donc
        # ecartees avant meme d'etre comptees -- ce test ne mesurerait rien.
        agg.add(flux(f"93.184.216.{i}", "10.0.0.2"), vantage="edge")
    lot = agg.flush(MAINTENANT)
    assert len(lot.destinations) == 2
    assert agg.destinations_dropped == 8


def test_le_suivi_des_destinations_se_coupe_sans_toucher_au_volume() -> None:
    agg = agregateur(track_destinations=False)
    agg.add(flux("45.57.12.34", "10.0.0.2", octets=4000), vantage="edge")
    lot = agg.flush(MAINTENANT)
    assert lot.destinations == []
    assert lot.subscribers[0].down_bytes == 4000


def test_la_fenetre_en_cours_se_lit_sans_etre_videe() -> None:
    """C'EST LA SEULE VUE EN DIRECT DU PRODUIT. La lire ne doit pas consommer
    la fenetre, sinon l'ecriture suivante perdrait ce qui a ete affiche."""
    agg = agregateur()
    agg.add(flux("45.57.12.34", "10.0.0.2", octets=4000), vantage="edge")
    assert len(agg.live_destinations()) == 1
    assert agg.destinations_in_window == 1
    assert len(agg.flush(MAINTENANT).destinations) == 1


# =========================================================================
# 3. CE QU'UNE RESTRICTION POSE SUR LE ROUTEUR
# =========================================================================


def cible(**kwargs: Any) -> RuleTarget:
    defauts: dict[str, Any] = {
        "rule_id": 7,
        "name": "Pas de Netflix",
        "action": "block",
        "destinations": ("45.57.0.0/17",),
    }
    defauts.update(kwargs)
    return RuleTarget(**defauts)


def actions(plan: Any, chemin: str) -> list[Any]:
    return [a for a in plan.actions if a.path == chemin]


def test_un_blocage_pose_une_liste_et_deux_regles_une_par_sens() -> None:
    """UNE SEULE REGLE LAISSERAIT PASSER LE RETOUR. Pour du streaming, cela
    revient a ne rien bloquer : la liste est en destination quand le client
    emet, en source quand le service repond."""
    plan = plan_restrictions("pop-nord", [cible()], RouterRestrictionState())

    adresses = actions(plan, PATH_ADDRESS_LIST)
    assert len(adresses) == 1
    assert adresses[0].fields["list"] == dst_list_name(7)
    assert adresses[0].fields["address"] == "45.57.0.0/17"

    filtres = actions(plan, PATH_FILTER)
    assert len(filtres) == 2
    sens = {
        f.fields.get("src-address-list") or f.fields.get("dst-address-list"): f.fields
        for f in filtres
    }
    assert all(champs["action"] == "drop" for champs in sens.values())
    montant = next(f.fields for f in filtres if "dst-address-list" in f.fields)
    descendant = next(f.fields for f in filtres if "src-address-list" in f.fields)
    assert montant["dst-address-list"] == dst_list_name(7)
    assert descendant["src-address-list"] == dst_list_name(7)


def test_un_plafond_pose_un_marquage_et_une_file_par_sens() -> None:
    plan = plan_restrictions(
        "pop-nord",
        [cible(action="limit", limit_down_mbps=5.0, limit_up_mbps=1.0)],
        RouterRestrictionState(),
    )
    marquages = actions(plan, PATH_MANGLE)
    files = actions(plan, PATH_QUEUE_TREE)
    assert len(marquages) == 2
    assert len(files) == 2
    # passthrough=no : sans cela, une regle suivante pourrait remarquer le
    # paquet et le faire compter dans deux files a la fois.
    assert all(m.fields["passthrough"] == "no" for m in marquages)
    descendante = next(
        f for f in files if f.fields["packet-mark"] == packet_mark(7, descendant=True)
    )
    assert descendante.fields["max-limit"] == "5000000"
    assert descendante.fields["parent"] == "global"


def test_un_sens_sans_plafond_ne_pose_rien_de_ce_cote() -> None:
    """Marquer sans plafonner couterait du CPU routeur pour rien."""
    plan = plan_restrictions(
        "pop-nord",
        [cible(action="limit", limit_down_mbps=5.0)],
        RouterRestrictionState(),
    )
    assert len(actions(plan, PATH_MANGLE)) == 1
    assert len(actions(plan, PATH_QUEUE_TREE)) == 1


def test_une_regle_bornee_a_des_clients_pose_leur_liste() -> None:
    plan = plan_restrictions(
        "pop-nord",
        [cible(clients=("10.0.0.5/32",))],
        RouterRestrictionState(),
    )
    listes = {a.fields["list"] for a in actions(plan, PATH_ADDRESS_LIST)}
    assert listes == {dst_list_name(7), "freeqos-r7-src"}
    montant = next(f.fields for f in actions(plan, PATH_FILTER) if "dst-address-list" in f.fields)
    assert montant["src-address-list"] == "freeqos-r7-src"


def test_une_regle_pour_tous_ne_borne_pas_le_cote_client() -> None:
    plan = plan_restrictions("pop-nord", [cible()], RouterRestrictionState())
    montant = next(f.fields for f in actions(plan, PATH_FILTER) if f.fields.get("dst-address-list"))
    assert "src-address-list" not in montant


def test_l_ipv6_est_ecarte_et_le_plan_le_dit() -> None:
    """LES LISTES DE /ip/firewall SONT IPv4. Poser silencieusement la moitie
    d'une regle laisserait croire a une protection complete."""
    plan = plan_restrictions(
        "pop-nord",
        [cible(destinations=("45.57.0.0/17", "2a00:86c0::/32"))],
        RouterRestrictionState(),
    )
    assert len(actions(plan, PATH_ADDRESS_LIST)) == 1
    assert any("IPv6" in s.reason for s in plan.skipped)


def test_une_regle_sans_aucune_adresse_est_ecartee_avec_son_motif() -> None:
    """Un service sans bloc publie et jamais rencontre ne vise rien encore. Le
    dire evite de chercher pourquoi la regle 'ne marche pas'."""
    plan = plan_restrictions("pop-nord", [cible(destinations=())], RouterRestrictionState())
    assert plan.is_empty
    assert plan.skipped and "aucune adresse" in plan.skipped[0].reason


def test_une_liste_demesuree_est_refusee_plutot_que_posee() -> None:
    trop = tuple(f"203.0.113.{i}" for i in range(20))
    plan = plan_restrictions(
        "pop-nord", [cible(destinations=trop)], RouterRestrictionState(), address_limit=5
    )
    assert plan.is_empty
    assert plan.conflicts and "limite de securite" in plan.conflicts[0].detail


# =========================================================================
# 4. LA REGLE EST VIVANTE : RECONCILIATION
# =========================================================================


def etat_pose(cible_posee: RuleTarget, adresses: list[str]) -> RouterRestrictionState:
    """Le routeur tel qu'il serait apres une pose complete de cette regle."""
    plan = plan_restrictions("pop-nord", [cible_posee], RouterRestrictionState())
    liste = [
        {
            ".id": f"*{i}",
            "list": a.fields["list"],
            "address": a.fields["address"],
            "comment": a.fields["comment"],
        }
        for i, a in enumerate(actions(plan, PATH_ADDRESS_LIST))
        if a.fields["address"] in adresses
    ]
    filtres = [{".id": f"*F{i}", **a.fields} for i, a in enumerate(actions(plan, PATH_FILTER))]
    return RouterRestrictionState(address_list=liste, filters=filtres)


def test_rien_ne_bouge_quand_rien_n_a_change() -> None:
    """LE CAS LE PLUS FREQUENT. Un plan qui reecrirait tout a chaque passage
    ferait autant d'ecritures inutiles sur des equipements de production."""
    depart = cible()
    etat = etat_pose(depart, ["45.57.0.0/17"])
    plan = plan_restrictions("pop-nord", [depart], etat)
    assert plan.is_empty
    assert plan.unchanged == 2


def test_une_adresse_nouvellement_decouverte_rejoint_la_liste() -> None:
    """C'EST TOUTE LA PROMESSE DU DYNAMIQUE. NetFlow voit un serveur hors des
    blocs publies, l'enrichissement le nomme, et la reconciliation l'ajoute --
    sans que personne ne reecrive la regle."""
    etat = etat_pose(cible(), ["45.57.0.0/17"])
    enrichie = cible(destinations=("45.57.0.0/17", "203.0.113.9"))
    plan = plan_restrictions("pop-nord", [enrichie], etat)
    ajouts = [a for a in actions(plan, PATH_ADDRESS_LIST) if a.verb == "add"]
    assert len(ajouts) == 1
    assert ajouts[0].fields["address"] == "203.0.113.9"


def test_une_adresse_qui_ne_releve_plus_du_service_sort_de_la_liste() -> None:
    """SANS CE RETRAIT, une adresse ajoutee un jour resterait bloquee pour
    toujours. La liste doit etre vivante dans les DEUX sens."""
    etat = etat_pose(
        cible(destinations=("45.57.0.0/17", "203.0.113.9")), ["45.57.0.0/17", "203.0.113.9"]
    )
    plan = plan_restrictions("pop-nord", [cible()], etat)
    retraits = [a for a in actions(plan, PATH_ADDRESS_LIST) if a.verb == "remove"]
    assert len(retraits) == 1
    assert "203.0.113.9" in retraits[0].name


def test_une_regle_retiree_emporte_ce_qu_elle_avait_pose() -> None:
    """Desactiver ou supprimer une regle doit LEVER la restriction. Une regle
    absente de l'interface mais toujours posee sur le routeur est le pire des
    etats : plus rien ne l'explique."""
    etat = etat_pose(cible(), ["45.57.0.0/17"])
    plan = plan_restrictions("pop-nord", [], etat)
    assert all(a.verb == "remove" for a in plan.actions)
    assert len(actions(plan, PATH_FILTER)) == 2
    assert len(actions(plan, PATH_ADDRESS_LIST)) == 1


def test_une_ligne_desactivee_a_la_main_est_reactivee() -> None:
    """Une regle desactivee ne restreint plus rien, alors que l'interface la
    montrerait comme posee. Meme logique que pour les files."""
    etat = etat_pose(cible(), ["45.57.0.0/17"])
    etat.filters[0]["disabled"] = "true"
    plan = plan_restrictions("pop-nord", [cible()], etat)
    corrections = [a for a in plan.actions if a.verb == "set"]
    assert len(corrections) == 1
    assert corrections[0].fields["disabled"] == "no"


def test_ce_qui_ne_porte_pas_notre_marque_n_est_jamais_touche() -> None:
    """LA REGLE ABSOLUE DU PRODUIT. Une regle de pare-feu de l'exploitant, une
    liste utilisee par son routage : on ne les lit meme pas."""
    etat = RouterRestrictionState(
        address_list=[{".id": "*9", "list": "clients-vip", "address": "10.9.0.0/24"}],
        filters=[{".id": "*A", "chain": "forward", "action": "drop", "comment": "a moi"}],
    )
    plan = plan_restrictions("pop-nord", [], etat)
    assert plan.is_empty


def test_la_marque_porte_la_regle_et_le_role() -> None:
    assert parse_tag(tag(7, "drop-up")) == ("r7", "drop-up")
    assert parse_tag("commentaire de l'exploitant") is None
    # La marque de propriete seule, sans regle nommee, n'est pas la notre non
    # plus : une file d'abonne porte 'freeqos:managed' et n'a rien a voir ici.
    assert parse_tag(MANAGED_COMMENT) is None


def test_les_prefixes_sont_ranges_sous_forme_canonique() -> None:
    """SINON UNE REECRITURE PAR CYCLE. RouterOS relit '10.0.0.0/8' la ou on
    aurait ecrit '10.0.0.1/8' : comparer des chaines brutes ferait voir un
    ecart a chaque passage."""
    retenus, ecartes = normalize_prefixes(
        ["10.0.0.1/8", "45.57.12.34/32", "45.57.12.34", "2a00::/32", "n'importe quoi"]
    )
    assert retenus == ["10.0.0.0/8", "45.57.12.34"]
    assert ecartes == ["2a00::/32", "n'importe quoi"]


def test_une_adresse_deja_couverte_par_un_bloc_n_est_pas_ajoutee() -> None:
    """Elle ne changerait rien au filtrage et ferait grossir une liste que le
    routeur parcourt a chaque paquet."""
    fusion = merge_addresses(["45.57.0.0/17"], ["45.57.12.34", "203.0.113.9"])
    assert fusion == ["45.57.0.0/17", "203.0.113.9"]


# =========================================================================
# 5. CE QU'UNE REGLE N'A PAS LE DROIT D'ETRE
# =========================================================================


def test_une_regle_sans_critere_est_refusee() -> None:
    """ELLE VISERAIT TOUT INTERNET. Sur un routeur de sortie, l'appliquer
    couperait le reseau entier -- et la regle aurait l'air normale dans la
    liste."""
    with pytest.raises(InvalidRuleError, match="designer du trafic"):
        validate({"name": "vide", "services": [], "categories": [], "prefixes": []})


def test_un_service_inconnu_du_catalogue_est_refuse() -> None:
    with pytest.raises(InvalidRuleError, match="inconnu"):
        validate({"name": "x", "services": ["netflixx"]})


def test_un_plafond_sans_debit_est_refuse() -> None:
    with pytest.raises(InvalidRuleError, match="plafond sans debit"):
        validate({"name": "x", "services": ["netflix"], "action": "limit"})


def test_une_portee_par_abonne_sans_abonne_est_refusee() -> None:
    with pytest.raises(InvalidRuleError, match="ne viserait personne"):
        validate({"name": "x", "services": ["netflix"], "scope": "subscribers", "logins": []})


def test_une_regle_complete_passe() -> None:
    validate(
        {
            "name": "Pas de streaming",
            "categories": [ipfinder.CAT_STREAMING],
            "action": "limit",
            "limit_down_mbps": 3.0,
            "scope": "subscribers",
            "logins": ["dupont"],
        }
    )


# =========================================================================
# 6. CE QUI N'EST PAS UN ABONNE DECLARE COMPTE AUSSI
# =========================================================================


def test_une_machine_non_declaree_atteint_quand_meme_des_destinations() -> None:
    """L'ANGLE MORT QUI SE VOYAIT DES LE PREMIER ESSAI.

    Un ping lance depuis un poste de supervision, une camera, un routeur --
    n'importe quoi qui n'est pas une fiche d'abonne -- ne laissait AUCUNE trace,
    alors que le flux traversait bien le reseau et que le collecteur le voyait
    passer. L'observation est "cette adresse a joint celle-la" ; le rattachement
    a un abonne est une interpretation, pas une condition.
    """
    agg = agregateur()
    # 10.9.9.9 n'est declare nulle part, mais il est dans l'espace client.
    agg.add(flux("10.9.9.9", "45.57.12.34", octets=84, protocol=1), vantage="edge")

    lot = agg.flush(MAINTENANT)
    assert len(lot.destinations) == 1
    destination = lot.destinations[0]
    assert destination.client == "10.9.9.9"
    assert destination.address == "45.57.12.34"
    assert destination.subscriber_id is None
    assert destination.up_bytes == 84


def test_un_abonne_declare_reste_rattache_a_sa_fiche() -> None:
    """Le rattachement n'est pas perdu au passage : il devient une donnee de
    plus, pas la condition d'existence de la ligne."""
    agg = agregateur()
    agg.add(flux("45.57.12.34", "10.0.0.2", octets=5000), vantage="edge")

    destination = agg.flush(MAINTENANT).destinations[0]
    assert destination.client == "10.0.0.2"
    assert destination.subscriber_id == 1


def test_le_rattachement_arrive_apres_coup_sans_perdre_la_mesure() -> None:
    """Une session PPPoE qui s'ouvre, une fiche saisie dans la minute : le
    rattachement apparait APRES la premiere vue. Il est pris des qu'il existe,
    et jamais efface par un flux ou il manquait."""
    agg = agregateur()
    agg.add(flux("10.0.0.2", "45.57.12.34", octets=100), vantage="edge")
    # Le meme couple, vu sans index (par exemple un flux ou seul l'autre sens
    # est rattache) : la ligne garde son abonne.
    agg._dests[("10.0.0.2", "45.57.12.34")].subscriber_id = 1
    agg.add(flux("10.0.0.2", "45.57.12.34", octets=100), vantage="edge")

    assert agg.flush(MAINTENANT).destinations[0].subscriber_id == 1


def test_une_machine_hors_de_l_espace_client_n_est_pas_suivie() -> None:
    """SANS CE FILTRE, le transit ferait de ce tableau un annuaire d'internet :
    chaque conversation entre deux adresses publiques qui traverse le reseau y
    entrerait, et aucune ne concerne un client."""
    agg = agregateur()
    agg.add(flux("198.51.100.7", "45.57.12.34"), vantage="edge")
    assert agg.flush(MAINTENANT).destinations == []


def test_un_ping_icmp_est_retenu_comme_le_reste() -> None:
    """Un ping n'a pas de port et pese quelques octets : rien de tout cela ne
    doit le faire disparaitre. C'est le premier geste de verification de
    n'importe quel exploitant."""
    agg = agregateur()
    agg.add(
        flux("10.0.0.2", "45.57.12.34", protocol=1, src_port=0, dst_port=0, octets=84, packets=1),
        vantage="edge",
    )
    destination = agg.flush(MAINTENANT).destinations[0]
    assert destination.address == "45.57.12.34"
    assert destination.app == "diagnostic"
    assert destination.port == 0


# =========================================================================
# 7. CE QUI EST DE L'EXPLOITATION N'EST PAS UNE CONVERSATION DE CLIENT
# =========================================================================


def agregateur_operateur(**kwargs: object) -> FlowAggregator:
    """Un agregateur qui connait son reseau : espace client et exploitation."""
    parametres: dict[str, object] = {
        "index": PrefixIndex.build([("100.100.101.115/32", 1)]),
        "customer_networks": FlowAggregator.parse_networks(["100.64.0.0/10", "172.16.0.0/12"]),
        "infrastructure_networks": FlowAggregator.parse_networks(["11.11.11.0/24"]),
    }
    parametres.update(kwargs)
    return FlowAggregator(**parametres)  # type: ignore[arg-type]


def test_deux_clients_qui_se_parlent_ne_sont_pas_une_destination() -> None:
    """LA CGNAT ECHAPPAIT AU FILTRE.

    La bibliotheque standard dit 100.64.0.0/10 privee, mais un operateur y met
    ses clients : ils tombaient donc dans "qui parle a qui", et la meme
    conversation y apparaissait DEUX FOIS -- une par sens, puisque chaque bout
    voyait l'autre comme sa destination.
    """
    agg = agregateur_operateur()
    agg.add(flux("100.100.101.115", "100.100.101.113", protocol=1), vantage="pop")
    agg.add(flux("100.100.101.113", "100.100.101.115", protocol=1), vantage="pop")

    assert agg.flush(MAINTENANT).destinations == []


def test_l_interrogation_des_routeurs_n_est_pas_du_trafic_client() -> None:
    """Le controleur interroge les routeurs en 8728 et recoit leurs flux en
    2055 : c'est le trafic le plus regulier du reseau, et il noyait le ping
    d'un client vers un site."""
    agg = agregateur_operateur()
    agg.add(flux("100.100.101.115", "11.11.11.81", src_port=51000, dst_port=8728), vantage="pop")
    agg.add(flux("100.100.101.115", "11.11.11.75", src_port=51000, dst_port=2055), vantage="pop")

    assert agg.flush(MAINTENANT).destinations == []
    assert agg.destinations_infra == 2


def test_le_bfd_entre_routeurs_est_ecarte() -> None:
    """Port 3784 : les routeurs se surveillent mutuellement. C'est du reseau
    qui s'administre, pas un client qui consomme."""
    agg = agregateur_operateur()
    agg.add(flux("100.100.101.116", "8.8.4.4", src_port=3784, dst_port=3784), vantage="pop")
    assert agg.flush(MAINTENANT).destinations == []


def test_une_adresse_d_exploitation_n_est_jamais_un_client() -> None:
    """Le conteneur du controleur parle aux routeurs : ce n'est pas un abonne,
    et ses conversations n'ont rien a faire dans la liste."""
    agg = agregateur_operateur(
        customer_networks=FlowAggregator.parse_networks(["172.16.0.0/12"]),
        infrastructure_networks=FlowAggregator.parse_networks(["172.18.0.0/16", "11.11.11.0/24"]),
    )
    agg.add(flux("172.18.0.3", "11.11.11.81", src_port=42765, dst_port=443), vantage="pop")
    assert agg.flush(MAINTENANT).destinations == []


def test_le_vrai_trafic_client_passe_toujours() -> None:
    """LE TEST QUI COMPTE. Tout ce filtrage n'a de valeur que s'il laisse
    passer ce qu'on venait chercher : un client qui joint un site."""
    agg = agregateur_operateur()
    agg.add(flux("100.100.101.115", "188.114.97.2", protocol=1, octets=132), vantage="pop")

    destinations = agg.flush(MAINTENANT).destinations
    assert len(destinations) == 1
    assert destinations[0].address == "188.114.97.2"
    assert destinations[0].client == "100.100.101.115"
    assert agg.destinations_infra == 0


def test_un_client_a_le_droit_d_utiliser_ssh() -> None:
    """SSH N'EST PAS DANS LA LISTE DE GESTION, et c'est volontaire : un client
    s'en sert legitimement, et l'ecarter masquerait son trafic."""
    agg = agregateur_operateur()
    agg.add(flux("100.100.101.115", "188.114.97.2", src_port=51000, dst_port=22), vantage="pop")
    assert len(agg.flush(MAINTENANT).destinations) == 1


def test_sans_reseaux_declares_rien_n_est_pris_pour_de_l_exploitation() -> None:
    """Le filtre par adresse ne s'applique que si on lui a dit quoi ecarter :
    une installation neuve ne doit pas perdre de trafic en silence."""
    agg = agregateur(infrastructure_networks=())
    agg.add(flux("10.0.0.2", "45.57.12.34"), vantage="edge")
    assert len(agg.flush(MAINTENANT).destinations) == 1


# =========================================================================
# 8. METTRE UN NOM LISIBLE SUR UNE ADRESSE
# =========================================================================


def test_le_domaine_enregistrable_est_extrait_du_nom_inverse() -> None:
    """'lfbn-lyo-1-878-160.w86-194.abo.wanadoo.fr' ne dit rien a personne ;
    'wanadoo.fr' dit Orange. C'est la forme qu'on reconnait d'un coup d'oeil."""
    assert ipfinder.registrable_domain("lfbn-lyo-1-878-160.w86-194.abo.wanadoo.fr") == "wanadoo.fr"
    assert ipfinder.registrable_domain("vip-rdefy-prod-k8s.s0.fti.net") == "fti.net"
    assert ipfinder.registrable_domain("ipv4-c001.1.oca.nflxvideo.net") == "nflxvideo.net"


def test_un_suffixe_compose_compte_pour_un() -> None:
    """Sans cette precaution, 'bbc.co.uk' deviendrait 'co.uk' -- le nom du
    registre, pas celui de l'organisation."""
    assert ipfinder.registrable_domain("www.bbc.co.uk") == "bbc.co.uk"
    assert ipfinder.registrable_domain("a.b.example.com.au") == "example.com.au"


def test_un_nom_inexploitable_ne_rend_pas_de_domaine() -> None:
    assert ipfinder.registrable_domain("localhost") is None
    assert ipfinder.registrable_domain(None) is None
    assert ipfinder.registrable_domain("") is None


def test_les_criteres_de_purge_couvrent_les_memes_cas_que_le_filtre() -> None:
    """LE FILTRE A L'ECRITURE NE SUFFIT PAS.

    Il empeche les nouvelles lignes, mais celles deja ecrites restent jusqu'a
    expiration de la retention -- une semaine pendant laquelle la liste continue
    d'afficher le BFD entre routeurs. Les deux doivent nommer les memes motifs,
    sinon l'historique et le direct se contredisent.
    """
    from app.services.flows import PORTS_INFRASTRUCTURE

    # Les ports que le filtre ecarte a l'ecriture sont exactement ceux que la
    # purge recoit : c'est la meme constante, pas une liste recopiee.
    assert 8728 in PORTS_INFRASTRUCTURE  # API RouterOS
    assert 2055 in PORTS_INFRASTRUCTURE  # NetFlow
    assert 3784 in PORTS_INFRASTRUCTURE  # BFD
    assert 22 not in PORTS_INFRASTRUCTURE  # SSH : usage client legitime

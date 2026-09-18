"""Capacite vendue, capacite reelle, et ce qui se passe entre les deux.

LES TROIS CHIFFRES QUI MANQUAIENT
---------------------------------
Le controleur mesurait deja le debit instantane, la latence et la QoE. Trois
questions d'exploitation restaient pourtant sans reponse, alors que la donnee
etait deja en base :

1. COMBIEN AI-JE VENDU SUR CE POP, ET QU'EST-CE QUI LE PORTE ? La somme des
   plans souscrits rapportee a la capacite mesuree du site. Tout WISP surveille
   ce rapport ; aucun ecran ne le donnait. Un PoP a 12:1 n'est pas en panne --
   il le devient le jour ou les abonnes s'en servent en meme temps.

2. QUAND MON LIEN SATURE-T-IL ? Pas "combien passe maintenant", mais l'heure de
   pointe et le taux d'occupation atteint. C'est ce qui decide d'un
   investissement, et cela ne se lit pas sur une courbe temps reel.

3. QUI CONSOMME, EN VOLUME ? Le top des debits instantanes designe celui qui
   telecharge a cet instant. Le volume sur une semaine designe celui qui pese
   sur le reseau. Ce ne sont presque jamais les memes abonnes.

CE MODULE NE FAIT AUCUNE ENTREE-SORTIE. Il recoit des lignes deja agregees et
rend des nombres et des verdicts, ce qui le rend testable sans base -- et
surtout, ce qui met les SEUILS au meme endroit que leur justification.
"""

from __future__ import annotations

from typing import Any

# -----------------------------------------------------------------------------
# Seuils de survente. Ils ne decrivent pas une panne : un reseau d'acces SANS
# survente serait un reseau ou l'operateur a achete dix fois trop de transit.
# Ils disent a partir de quand le partage commence a se voir.
#
# Les valeurs retenues sont celles qu'un WISP reconnait : jusqu'a 5:1 personne
# ne s'en apercoit, au-dela de 20:1 une heure de pointe se sent sur chaque
# ligne. Elles restent des reperes, pas une verite -- d'ou le verdict en toutes
# lettres plutot qu'un code couleur seul.
# -----------------------------------------------------------------------------
SURVENTE_CONFORTABLE = 5.0
SURVENTE_TENDUE = 20.0

VERDICT_SANS_CAPACITE = "capacite inconnue"
VERDICT_SANS_VENTE = "rien de vendu"
VERDICT_CONFORTABLE = "confortable"
VERDICT_SURVEILLER = "a surveiller"
VERDICT_TENDU = "tendu"

# Part de la capacite au-dela de laquelle un lien ne peut plus absorber une
# pointe : a 80 % la file commence a se former, a 95 % elle est deja formee.
OCCUPATION_CHARGEE = 0.80
OCCUPATION_SATUREE = 0.95

ETAT_LIBRE = "libre"
ETAT_CHARGE = "charge"
ETAT_SATURE = "sature"
ETAT_INCONNU = "capacite inconnue"

# A RENFORCER : le critere est la MOYENNE, pas la pointe.
#
# Une pointe a 100 % ne prouve rien -- c'est meme ce qu'on attend d'un lien
# correctement dimensionne un soir de match. Une MOYENNE au-dessus de 80 % sur
# la periode dit autre chose : le lien n'a plus de marge, et la prochaine
# croissance se paiera en latence pour tout le monde.
#
# Le seuil de 80 % et le minimum d'echantillons sont ceux qu'emploient les
# outils du domaine ; ils sont repris ici parce qu'ils sont defendables, pas
# parce qu'ils sont ecrits ailleurs. Sans minimum d'echantillons, trois mesures
# prises pendant un pic feraient acheter un backhaul.
SEUIL_RENFORT = 0.80
ECHANTILLONS_MINIMUM = 10

# Au-dela de cette part d'echantillons passes a plus de 90 % du plan, l'abonne
# ne "profite" plus de son plan : il vit dedans. C'est un candidat a une offre
# superieure -- ou le signe que son plan est mal taille.
PART_AU_PLAFOND = 0.20
SEUIL_PLAFOND = 0.90


def oversubscription(sold_mbps: float | None, capacity_mbps: float | None) -> float | None:
    """Rapport entre ce qui est VENDU et ce qui est MESURE. None si indecidable.

    Rendre None plutot que 0 ou l'infini est deliberé : un PoP dont la capacite
    n'est pas mesuree n'a pas un taux de survente nul, il a un taux INCONNU, et
    les deux appellent des gestes opposes (ne rien faire, ou aller mesurer).
    """
    if not sold_mbps or not capacity_mbps or capacity_mbps <= 0:
        return None
    return round(sold_mbps / capacity_mbps, 2)


def verdict_survente(ratio: float | None, *, sold_mbps: float | None) -> str:
    """Le rapport en toutes lettres. Un chiffre seul ne se discute pas."""
    if not sold_mbps:
        return VERDICT_SANS_VENTE
    if ratio is None:
        return VERDICT_SANS_CAPACITE
    if ratio <= SURVENTE_CONFORTABLE:
        return VERDICT_CONFORTABLE
    if ratio <= SURVENTE_TENDUE:
        return VERDICT_SURVEILLER
    return VERDICT_TENDU


def occupancy(peak_bps: float | None, capacity_mbps: float | None) -> float | None:
    """Part de la capacite du lien atteinte a la pointe, entre 0 et 1+.

    Peut depasser 1 : la capacite d'un port est son debit negocie, celle d'une
    radio une mesure du moment. Un depassement n'est pas une aberration a
    masquer, c'est le signe que la capacite retenue est sous-estimee -- et c'est
    exactement ce qu'il faut voir.
    """
    if peak_bps is None or not capacity_mbps or capacity_mbps <= 0:
        return None
    return round(peak_bps / (capacity_mbps * 1_000_000), 3)


def etat_du_lien(part: float | None) -> str:
    if part is None:
        return ETAT_INCONNU
    if part >= OCCUPATION_SATUREE:
        return ETAT_SATURE
    if part >= OCCUPATION_CHARGEE:
        return ETAT_CHARGE
    return ETAT_LIBRE


def pop_capacity_row(row: dict[str, Any]) -> dict[str, Any]:
    """Une ligne de survente, prete a lire.

    ``peak_bps`` est la pointe REELLEMENT observee, tous abonnes du PoP
    additionnes. C'est elle qui donne son sens au rapport : vendre 20 fois la
    capacite ne se voit pas tant que la pointe reste au tiers du lien, et se
    voit tres bien quand elle la frole.
    """
    vendu_down = float(row.get("sold_down_mbps") or 0.0)
    capacite = row.get("capacity_mbps")
    capacite = float(capacite) if capacite is not None else None
    ratio = oversubscription(vendu_down, capacite)
    pointe = row.get("peak_bps")
    pointe = float(pointe) if pointe is not None else None
    part = occupancy(pointe, capacite)
    return {
        "pop_name": row.get("pop_name"),
        "subscribers": int(row.get("subscribers") or 0),
        "sold_down_mbps": round(vendu_down, 1),
        "sold_up_mbps": round(float(row.get("sold_up_mbps") or 0.0), 1),
        "capacity_mbps": round(capacite, 1) if capacite is not None else None,
        "ratio": ratio,
        "verdict": verdict_survente(ratio, sold_mbps=vendu_down),
        "peak_mbps": round(pointe / 1_000_000, 1) if pointe is not None else None,
        "peak_share": part,
        "peak_state": etat_du_lien(part),
    }


def link_row(row: dict[str, Any]) -> dict[str, Any]:
    """Une ligne d'occupation de lien, avec SON heure de pointe.

    Le sens des compteurs suit la convention du projet : ``rx`` est ce que le
    routeur RECOIT du voisin, ``tx`` ce qu'il lui ENVOIE. Selon que le voisin
    soit en amont ou en aval, le meme ``tx`` est du descendant ou du montant :
    on ne devine pas, on retient la direction la plus chargee et on la NOMME.
    """
    capacite = row.get("capacity_mbps")
    capacite = float(capacite) if capacite is not None else None
    pic_rx = float(row["peak_rx_bps"]) if row.get("peak_rx_bps") is not None else None
    pic_tx = float(row["peak_tx_bps"]) if row.get("peak_tx_bps") is not None else None
    if (pic_tx or 0) >= (pic_rx or 0):
        pointe, sens, quand = pic_tx, "tx", row.get("peak_tx_at")
    else:
        pointe, sens, quand = pic_rx, "rx", row.get("peak_rx_at")
    part = occupancy(pointe, capacite)
    return {
        "router_name": row.get("router_name"),
        "interface": row.get("interface"),
        "link_name": row.get("link_name"),
        "capacity_mbps": round(capacite, 1) if capacite is not None else None,
        "peak_mbps": round(pointe / 1_000_000, 2) if pointe is not None else None,
        "peak_direction": sens,
        "peak_at": quand,
        "avg_mbps": (
            round(float(row["avg_bps"]) / 1_000_000, 2) if row.get("avg_bps") is not None else None
        ),
        "avg_share": occupancy(
            float(row["avg_bps"]) if row.get("avg_bps") is not None else None, capacite
        ),
        "samples": int(row.get("samples") or 0),
        "share": part,
        "state": etat_du_lien(part),
    }


def usage_row(row: dict[str, Any]) -> dict[str, Any]:
    """Un abonne vu par son VOLUME, pas par son debit de l'instant.

    ``bytes`` est une integration du debit mesure sur la periode, pas un
    compteur releve : les compteurs d'une session PPPoE repartent de zero a
    chaque reconnexion, et les additionner produirait des volumes fantaisistes.
    L'integration, elle, encaisse les reconnexions sans broncher.
    """
    octets = float(row.get("bytes") or 0.0)
    echantillons = int(row.get("samples") or 0)
    au_plafond = int(row.get("capped_samples") or 0)
    part = round(au_plafond / echantillons, 3) if echantillons else None
    return {
        "subscriber_id": row.get("subscriber_id"),
        "login": row.get("login"),
        "kind": row.get("kind"),
        "pop_name": row.get("pop_name"),
        "plan_down_mbps": row.get("plan_down_mbps"),
        "gigabytes": round(octets / 1_000_000_000, 2),
        "peak_mbps": (
            round(float(row["peak_bps"]) / 1_000_000, 2)
            if row.get("peak_bps") is not None
            else None
        ),
        "avg_mbps": (
            round(float(row["avg_bps"]) / 1_000_000, 2) if row.get("avg_bps") is not None else None
        ),
        "avg_share": occupancy(
            float(row["avg_bps"]) if row.get("avg_bps") is not None else None,
            row.get("plan_down_mbps"),
        ),
        "samples": echantillons,
        "capped_share": part,
        # Vit dans son plan plutot qu'il n'en profite : candidat a une offre
        # superieure, ou plan mal taille. Le dire est plus utile que le chiffre.
        "at_plan_ceiling": bool(part is not None and part >= PART_AU_PLAFOND),
        "last_traffic_at": row.get("last_traffic_at"),
    }


def a_renforcer(
    liens: list[dict[str, Any]], abonnes: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Ce qui n'a plus de marge : liens et abonnes au-dessus du seuil EN MOYENNE.

    DEUX POPULATIONS, DEUX GESTES. Un LIEN sans marge se renforce -- on achete
    de la capacite, on reequilibre des secteurs. Un ABONNE sans marge se vend --
    il consomme ce qu'il a paye et en voudrait davantage. Les melanger dans un
    seul classement ferait passer une opportunite commerciale pour un probleme
    d'ingenierie.

    Les deux listes sont ordonnees par occupation decroissante : la premiere
    ligne est celle qui coute le plus cher a laisser en l'etat.
    """
    liens_charges = [
        ligne
        for ligne in liens
        if ligne.get("avg_share") is not None
        and ligne["avg_share"] >= SEUIL_RENFORT
        and ligne.get("samples", 0) >= ECHANTILLONS_MINIMUM
    ]
    abonnes_charges = [
        ligne
        for ligne in abonnes
        if ligne.get("avg_share") is not None
        and ligne["avg_share"] >= SEUIL_RENFORT
        and ligne.get("samples", 0) >= ECHANTILLONS_MINIMUM
    ]
    liens_charges.sort(key=lambda ligne: -float(ligne["avg_share"]))
    abonnes_charges.sort(key=lambda ligne: -float(ligne["avg_share"]))
    return {"links": liens_charges, "subscribers": abonnes_charges}

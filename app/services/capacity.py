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


# =========================================================================
# Points de saturation : ou la marge manque, cote PoP comme cote internet
# =========================================================================

#: Memes seuils que les jauges de l'interface : a 70 % un lien se surveille,
#: a 90 % il ne tient plus une pointe de plus.
RISQUE_SURVEILLER = 0.70
RISQUE_SATURE = 0.90

COTE_INTERNET = "internet"  # le lien amont d'une passerelle : le transit
COTE_AMONT = "upstream"  # le lien amont d'un PoP ou du coeur : vers le coeur
COTE_POP = "pop"  # un lien aval : vers les abonnes, un VLAN, un relais


def _positif(valeur: Any) -> float | None:
    try:
        v = float(valeur)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def hotspot_rows(
    occupancy: list[dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    upstream: dict[str, tuple[str | None, str | None]],
    roles: dict[str, str],
) -> list[dict[str, Any]]:
    """Chaque port mesure, avec ce qui y passe, ce qu'il peut porter, et la marge.

    LA CAPACITE RETENUE EST LA PLUS PETITE DE CELLES QU'ON CONNAIT : le debit
    pose a la main sur le lien (bouton Bandwidth), la capacite mesuree du lien
    (une radio), le debit negocie du port. C'est la plus petite qui sature en
    premier ; retenir celle du port ferait croire a de la marge sur un
    backhaul radio qui n'en a plus.

    LE SENS SE DEDUIT DU COTE. Sur le lien amont d'un routeur, ce qu'il RECOIT
    est le descendant des abonnes ; sur un lien aval, c'est ce qu'il EMET. Le
    cote vient de la route par defaut lue a la decouverte, pas d'un nom.
    """
    par_port: dict[tuple[str, str], dict[str, Any]] = {}
    for lien in links:
        routeur = str(lien.get("discovered_by") or "")
        interface = str(lien.get("interface") or "")
        if routeur and interface:
            # Le lien le plus renseigne gagne quand plusieurs voisins partagent le port.
            actuel = par_port.get((routeur, interface))
            if actuel is None or (lien.get("max_down_mbps") and not actuel.get("max_down_mbps")):
                par_port[(routeur, interface)] = lien

    lignes: list[dict[str, Any]] = []
    for mesure in occupancy:
        routeur = str(mesure.get("router_name") or "")
        interface = str(mesure.get("interface") or "")
        lien = par_port.get((routeur, interface), {})
        _passerelle, sortie = upstream.get(routeur, (None, None))
        amont = bool(sortie) and sortie == interface
        role = roles.get(routeur, "pop")
        cote = (COTE_INTERNET if role == "gateway" else COTE_AMONT) if amont else COTE_POP

        candidats = [
            ("set on the link", _positif(lien.get("max_down_mbps"))),
            ("measured link capacity", _positif(lien.get("capacity_mbps"))),
            ("port speed", _positif(mesure.get("capacity_mbps"))),
        ]
        connus = [(source, v) for source, v in candidats if v is not None]
        capacite, source = (None, None)
        if connus:
            source, capacite = min(connus, key=lambda c: c[1])

        # Descendant / montant vus des abonnes, selon le cote.
        rx_now, tx_now = lien.get("rx_bps"), lien.get("tx_bps")
        if not lien.get("measure_fresh", True):
            rx_now = tx_now = None
        down_now, up_now = (rx_now, tx_now) if amont else (tx_now, rx_now)
        pic_rx, pic_tx = mesure.get("peak_rx_bps"), mesure.get("peak_tx_bps")
        down_peak, up_peak = (pic_rx, pic_tx) if amont else (pic_tx, pic_rx)
        quand_rx, quand_tx = mesure.get("peak_rx_at"), mesure.get("peak_tx_at")
        down_at, up_at = (quand_rx, quand_tx) if amont else (quand_tx, quand_rx)

        def mbps(v: Any) -> float | None:
            return round(float(v) / 1e6, 2) if v is not None else None

        pic = max(float(down_peak or 0), float(up_peak or 0))
        maintenant = (
            max(float(down_now or 0), float(up_now or 0))
            if down_now is not None or up_now is not None
            else None
        )
        part_pic = pic / (capacite * 1e6) if capacite else None
        part_now = maintenant / (capacite * 1e6) if capacite and maintenant is not None else None
        risque = max(part_pic or 0, part_now or 0) if capacite else None
        if risque is None:
            etat = "unknown"
        elif risque >= RISQUE_SATURE:
            etat = "saturated"
        elif risque >= RISQUE_SURVEILLER:
            etat = "busy"
        else:
            etat = "ok"
        if not pic and not maintenant and capacite is None:
            continue  # port muet et sans capacite : rien a dire
        lignes.append(
            {
                "router": routeur,
                "interface": interface,
                "name": lien.get("target_name") or mesure.get("link_name") or interface,
                "side": cote,
                "capacity_mbps": round(capacite, 1) if capacite else None,
                "capacity_source": source,
                "now_down_mbps": mbps(down_now),
                "now_up_mbps": mbps(up_now),
                "peak_down_mbps": mbps(down_peak),
                "peak_up_mbps": mbps(up_peak),
                "peak_down_at": down_at,
                "peak_up_at": up_at,
                "avg_mbps": mbps(mesure.get("avg_bps")),
                "now_share": round(part_now, 3) if part_now is not None else None,
                "peak_share": round(part_pic, 3) if part_pic is not None else None,
                "headroom_mbps": round(capacite - pic / 1e6, 1) if capacite else None,
                "samples": int(mesure.get("samples") or 0),
                "state": etat,
            }
        )
    ordre = {"saturated": 0, "busy": 1, "ok": 2, "unknown": 3}
    lignes.sort(
        key=lambda r: (
            ordre[r["state"]],
            -(r["peak_share"] or 0),
            -max(r["peak_down_mbps"] or 0, r["peak_up_mbps"] or 0),
        )
    )
    return lignes

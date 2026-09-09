"""Planificateur d'enforcement.

Il est PUR : ces tests couvrent l'integralite de la logique qui decide ce qui
sera envoye a un routeur, sans qu'aucune connexion soit ouverte.
"""

from __future__ import annotations

from app.enforcement.models import MANAGED_COMMENT, format_rate, slugify
from app.enforcement.planner import (
    QUEUE_TYPE_DOWN,
    QUEUE_TYPE_UP,
    LinkTarget,
    SubscriberTarget,
    build_plan,
    desired_queue_types,
    desired_state,
    shaped_capacity,
)


def abonne(login="dupont", down=100.0, up=20.0, **kwargs) -> SubscriberTarget:
    kwargs.setdefault("interface", f"<pppoe-{login}>")
    # Une session ouverte, donc une adresse : c'est elle qui porte la file.
    kwargs.setdefault("address", "10.20.0.10")
    return SubscriberTarget(login=login, plan_down_mbps=down, plan_up_mbps=up, **kwargs)


def lien(name="bh-nord", capacity=500.0, **kwargs) -> LinkTarget:
    kwargs.setdefault("interface", "ether1")
    return LinkTarget(name=name, measured_capacity_mbps=capacity, **kwargs)


# ------------------------------------------------------------------- helpers
def test_slugify_protege_les_noms_de_file() -> None:
    assert slugify("jean.dupont@fai.fr") == "jean-dupont-fai-fr"
    assert slugify("  ") == "sans-nom"
    assert len(slugify("x" * 200)) <= 48


def test_format_rate_en_bits_entiers() -> None:
    """RouterOS n'accepte pas toujours les decimales : on sort des entiers."""
    assert format_rate(100) == "100000000"
    assert format_rate(12.5) == "12500000"
    assert format_rate(None) == "0"
    assert format_rate(0) == "0"


# ------------------------------------------------------- capacite a appliquer
def test_facteur_de_securite() -> None:
    """On shape SOUS la capacite reelle pour que la file se forme dans CAKE,
    pas dans le buffer de la radio."""
    assert shaped_capacity(500, safety_factor=0.9, floor_mbps=5) == 450.0


def test_plancher_en_cas_de_fade_profond() -> None:
    assert shaped_capacity(2, safety_factor=0.9, floor_mbps=5) == 5.0


def test_surcharge_manuelle_prioritaire() -> None:
    """L'operateur qui fixe un plafond sait ce qu'il fait."""
    assert shaped_capacity(500, safety_factor=0.9, floor_mbps=5, override_mbps=300) == 300.0


def test_capacite_inconnue() -> None:
    assert shaped_capacity(None, safety_factor=0.9, floor_mbps=5) is None


# ---------------------------------------------------------------- etat desire
def test_etat_desire_complet() -> None:
    types, files, _ = desired_state(
        links=[lien()], subscribers=[abonne(parent="freeqos-parent-bh-nord")]
    )

    assert [t.name for t in types] == [QUEUE_TYPE_UP, QUEUE_TYPE_DOWN]
    assert [f.name for f in files] == ["freeqos-parent-bh-nord", "freeqos-dupont"]

    parent, enfant = files
    assert parent.max_down_mbps == 450.0  # 500 x 0,9
    assert enfant.parent == "freeqos-parent-bh-nord"
    # max-limit = upload/download vu du routeur, comme RouterOS l'attend.
    assert enfant.max_limit == "20000000/100000000"
    assert enfant.queue == f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}"


def test_lien_decouvert_recoit_une_file_illimitee() -> None:
    """Un lien decouvert doit avoir sa file tout de suite, meme sans mesure.

    ``0/0`` ne bride rien : la file existe, elle porte les abonnes du lien, et
    l'exploitant n'a plus qu'a lui fixer un debit. Ne rien creer laisserait au
    contraire un lien sans aucune prise dans l'interface."""
    _, files, _ = desired_state(links=[lien(capacity=None)], subscribers=[abonne()])

    assert [f.name for f in files] == ["freeqos-parent-bh-nord", "freeqos-dupont"]
    assert files[0].max_limit == "0/0"


def test_lien_sans_capacite_ignorable() -> None:
    """Qui prefere l'ancien comportement le garde."""
    _, files, _ = desired_state(
        links=[lien(capacity=None)], subscribers=[abonne()], queue_unmeasured_links=False
    )
    assert [f.name for f in files] == ["freeqos-dupont"]


def test_la_file_d_un_lien_vise_son_segment_l3() -> None:
    """Une file posee sur un NOM d'interface ne peut pas etre le parent d'une
    file d'abonne, qui vise une adresse. Viser le segment donne la hierarchie
    attendue -- celle que tout le monde ecrit a la main."""
    _, files, _ = desired_state(
        links=[lien(subnet="172.16.38.1/23")],
        subscribers=[abonne(address="172.16.39.253")],
    )

    parent, enfant = files
    # L'adresse du routeur devient le RESEAU : c'est ce que RouterOS relira.
    assert parent.target == "172.16.38.0/23"
    # Et l'abonne est rattache par son adresse, sans avoir besoin d'UISP.
    assert enfant.parent == "freeqos-parent-bh-nord"


def test_lien_sans_adresse_retombe_sur_l_interface() -> None:
    """Un lien purement L2 n'a pas de segment : mieux vaut un plafond de port
    qu'aucun plafond."""
    _, files, _ = desired_state(links=[lien()], subscribers=[])
    assert files[0].target == "ether1"


def test_le_parent_le_plus_specifique_l_emporte() -> None:
    """Un abonne tient souvent dans plusieurs segments emboites : le goulot
    utile est le plus proche de lui."""
    _, files, _ = desired_state(
        links=[
            lien(name="pop", subnet="172.16.38.1/23"),
            lien(name="secteur", subnet="172.16.39.1/27", interface="ether2"),
        ],
        subscribers=[abonne(address="172.16.39.10")],
    )

    assert files[-1].parent == "freeqos-parent-secteur"


def test_deux_liens_sur_la_meme_cible_ne_font_qu_une_file() -> None:
    """Deux files de meme cible se masqueraient : RouterOS n'applique que la
    premiere."""
    _, files, _ = desired_state(
        links=[
            lien(name="voisin-a", subnet="172.16.38.1/23"),
            lien(name="voisin-b", subnet="172.16.38.1/23", interface="ether2"),
        ],
        subscribers=[],
    )

    assert [f.name for f in files] == ["freeqos-parent-voisin-a"]


def test_parent_inconnu_est_ignore() -> None:
    """Referencer un parent inexistant ferait echouer la commande RouterOS."""
    _, files, _ = desired_state(links=[], subscribers=[abonne(parent="freeqos-parent-absent")])
    assert files[0].parent is None


def test_abonne_sans_plan_ni_surcharge_ignore() -> None:
    _, files, _ = desired_state(links=[], subscribers=[abonne(down=None, up=None)])
    assert files == []


def test_surcharge_abonne_prime_sur_le_plan() -> None:
    _, files, _ = desired_state(
        links=[], subscribers=[abonne(down=100, up=20, override_down_mbps=250)]
    )
    assert files[0].max_down_mbps == 250
    assert files[0].max_up_mbps == 20  # non surcharge : le plan reste


def test_abonne_desactive_ignore() -> None:
    _, files, _ = desired_state(links=[], subscribers=[abonne(enabled=False)])
    assert files == []


# ----------------------------------------------------------------------- plan
def test_plan_sur_routeur_vierge() -> None:
    types, files, _ = desired_state(links=[lien()], subscribers=[abonne()])
    plan = build_plan(
        "pop-nord", desired_types=types, desired_queues=files, actual_types=[], actual_queues=[]
    )

    assert plan.counts() == {"add": 4, "set": 0, "remove": 0}  # 2 types + 2 files
    # Les parents passent avant leurs enfants : RouterOS refuse l'inverse.
    noms = [a.fields.get("name") for a in plan.actions if a.path == "/queue/simple"]
    assert noms == ["freeqos-parent-bh-nord", "freeqos-dupont"]


def test_la_commande_est_lisible_avant_envoi() -> None:
    """C'est ce que l'operateur voit dans l'interface avant d'appliquer."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[]
    )

    commande = plan.actions[0].command
    assert commande.startswith("/queue/simple/add ")
    assert "name=freeqos-dupont" in commande
    # La cible est l'ADRESSE de la session, sous sa forme canonique : c'est
    # elle que RouterOS relira, donc la seule qui ne produise pas un faux ecart.
    assert "target=10.20.0.10/32" in commande
    assert "max-limit=20000000/100000000" in commande
    # Pas de guillemets superflus : freeqos:managed ne contient aucun caractere
    # ambigu pour le shell RouterOS.
    assert f"comment={MANAGED_COMMENT}" in commande


def test_etat_deja_conforme_ne_produit_rien() -> None:
    types, files, _ = desired_state(links=[], subscribers=[abonne()])
    existante = {
        ".id": "*1",
        "name": "freeqos-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "20000000/100000000",
        "queue": f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}",
        "comment": MANAGED_COMMENT,
    }
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[existante]
    )

    assert plan.is_empty
    assert plan.unchanged == 1


def test_ecriture_routeros_comparee_sans_se_tromper_de_forme() -> None:
    """RouterOS relit '20M/100M' la ou on a ecrit des bits : ce n'est pas un
    changement, et le confondre provoquerait une reecriture a chaque cycle."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    existante = {
        ".id": "*1",
        "name": "freeqos-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "20M/100M",
        "queue": f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}",
        "comment": MANAGED_COMMENT,
    }
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[existante]
    )
    assert plan.is_empty


def test_changement_de_debit_produit_un_set() -> None:
    _, files, _ = desired_state(links=[], subscribers=[abonne(down=300)])
    existante = {
        ".id": "*7",
        "name": "freeqos-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "20M/100M",
        "queue": f"{QUEUE_TYPE_UP}/{QUEUE_TYPE_DOWN}",
        "comment": MANAGED_COMMENT,
    }
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[existante]
    )

    assert plan.counts() == {"add": 0, "set": 1, "remove": 0}
    action = plan.actions[0]
    assert action.target_id == "*7"
    assert action.changes["max-limit"] == ("20M/100M", "20000000/300000000")
    assert action.summary() == (
        "modifier freeqos-dupont (max-limit 20M/100M -> 20000000/300000000)"
    )


# ----------------------------------------------------- SURETE : propriete
def test_une_file_non_marquee_n_est_jamais_modifiee() -> None:
    """Regle absolue : ce qui n'a pas notre marqueur appartient a l'operateur
    ou a RADIUS. On signale le conflit, on ne touche a rien."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    manuelle = {
        ".id": "*1",
        "name": "freeqos-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "1M/1M",
        "comment": "pose a la main par l'exploitant",
    }
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[manuelle]
    )

    assert plan.is_empty
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].name == "freeqos-dupont"
    assert MANAGED_COMMENT in plan.conflicts[0].detail


def test_une_file_tierce_n_est_jamais_supprimee() -> None:
    """Le nettoyage ne doit emporter que nos propres files."""
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=[
            {".id": "*1", "name": "queue-radius-jean", "comment": ""},
            {".id": "*2", "name": "shaping-exploitant", "comment": "ne pas toucher"},
            {".id": "*3", "name": "freeqos-parti", "comment": MANAGED_COMMENT},
        ],
    )

    assert plan.counts() == {"add": 0, "set": 0, "remove": 1}
    assert plan.actions[0].fields["name"] == "freeqos-parti"


def _tierce(**extra) -> dict:
    """Une file heritee : posee a la main ou par un ancien outil."""
    return {
        ".id": "*1",
        "name": "sub-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "5M/20M",
        "comment": "dupont",
        **extra,
    }


def test_le_debit_d_une_file_tierce_est_aligne_sur_la_cible() -> None:
    """RouterOS n'applique que la premiere file d'une meme cible, en silence.

    Ajouter la notre derriere une file heritee ne briderait donc rien : on
    envoie un set sur la file en place, exactement le debit et rien d'autre."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[_tierce()]
    )

    assert plan.counts() == {"add": 0, "set": 1, "remove": 0}
    action = plan.actions[0]
    assert action.target_id == "*1"
    assert action.command == "/queue/simple/set .id=*1 max-limit=20000000/100000000"
    # Ni renommee, ni reparentee, ni marquee : elle reste la file de l'exploitant.
    assert set(action.fields) == {"max-limit"}
    assert action.changes["max-limit"] == ("5M/20M", "20000000/100000000")


def test_file_tierce_deja_au_bon_debit_ne_produit_rien() -> None:
    """Le debit voulu est deja en place : il n'y a plus rien a envoyer."""
    _, files, _ = desired_state(links=[], subscribers=[abonne(down=100, up=20)])
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[_tierce(**{"max-limit": "20M/100M"})],
    )

    assert plan.is_empty
    assert plan.unchanged == 1


def test_file_tierce_desactivee_ne_masque_rien() -> None:
    """Une file desactivee ne shape rien, donc elle ne cache pas la notre :
    c'est bien une creation qu'il faut, pas une reprise."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[_tierce(disabled="true")],
    )

    assert plan.counts() == {"add": 1, "set": 0, "remove": 0}
    assert plan.actions[0].fields["name"] == "freeqos-dupont"


def test_plusieurs_files_tierces_sur_la_meme_cible_ne_sont_pas_touchees() -> None:
    """Laquelle shape reellement ? On ne peut pas le deviner : on ne touche a
    rien plutot que de modifier la mauvaise."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[_tierce(), _tierce(**{".id": "*2", "name": "vieux-dupont"})],
    )

    assert plan.is_empty
    assert len(plan.conflicts) == 1
    assert "sub-dupont, vieux-dupont" in plan.conflicts[0].detail


def test_reprise_desactivable() -> None:
    """Qui prefere ne rien ecrire hors de son perimetre garde le signalement."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()])
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[_tierce()],
        adopt=False,
    )

    assert plan.is_empty
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].name == "freeqos-dupont"
    assert "sub-dupont" in plan.conflicts[0].detail
    assert "10.20.0.10/32" in plan.conflicts[0].detail


def test_prune_desactivable() -> None:
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=[{".id": "*3", "name": "freeqos-parti", "comment": MANAGED_COMMENT}],
        prune=False,
    )
    assert plan.is_empty


def test_file_marquee_mais_hors_prefixe_conservee() -> None:
    """Ceinture et bretelles : marqueur ET prefixe sont exiges pour supprimer."""
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=[],
        actual_types=[],
        actual_queues=[{".id": "*4", "name": "autre-chose", "comment": MANAGED_COMMENT}],
    )
    assert plan.is_empty


# -------------------------------------------------------------- types CAKE
def test_type_cake_avec_overhead_pppoe() -> None:
    types = desired_queue_types(overhead=22, rtt_ms=50)
    champs = types[0].routeros_fields()

    assert champs["kind"] == "cake"
    assert champs["cake-overhead"] == "22"
    assert champs["cake-rtt"] == "50ms"


def test_type_cake_existant_mais_different() -> None:
    types = desired_queue_types(overhead=22)
    plan = build_plan(
        "pop",
        desired_types=types,
        desired_queues=[],
        actual_types=[
            {".id": "*a", "name": QUEUE_TYPE_UP, "kind": "cake", "cake-overhead": "0"},
            {".id": "*b", "name": QUEUE_TYPE_DOWN, "kind": "cake", "cake-overhead": "22"},
        ],
        actual_queues=[],
    )

    assert plan.counts() == {"add": 0, "set": 1, "remove": 0}
    assert plan.unchanged == 1
    assert plan.actions[0].changes["cake-overhead"] == ("0", "22")


def test_serialisation_du_plan() -> None:
    types, files, _ = desired_state(links=[lien()], subscribers=[abonne()])
    plan = build_plan(
        "pop-nord", desired_types=types, desired_queues=files, actual_types=[], actual_queues=[]
    )
    donnees = plan.to_dict()

    assert donnees["router"] == "pop-nord"
    assert donnees["counts"]["add"] == 4
    assert all("command" in a and "summary" in a for a in donnees["actions"])

"""Ce que vise une file d'abonne, et pourquoi c'est son adresse.

La file etait accrochee a l'interface PPPoE dynamique. Trois defauts, dont deux
silencieux :

  1. l'interface est recreee a chaque reconnexion, la file reste accrochee a un
     objet disparu ;
  2. RouterOS INVERSE alors le sens des deux limites, donc le plan vendu est
     applique a l'envers ;
  3. les chevrons du nom dynamique ne passent pas l'API RouterOS 7.

Ces tests fixent le comportement corrige : la cible est l'adresse de la session
en cours, sous sa forme canonique, et un abonne sans session n'a pas de file.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime

import pytest

from app.enforcement.models import MANAGED_COMMENT, address_target
from app.enforcement.planner import (
    QUEUE_TYPE_DOWN,
    QUEUE_TYPE_UP,
    TARGET_INTERFACE,
    SubscriberTarget,
    build_plan,
    desired_state,
)

MAINTENANT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def abonne(login="dupont", **kwargs) -> SubscriberTarget:
    kwargs.setdefault("interface", f"<pppoe-{login}>")
    kwargs.setdefault("address", "10.20.0.10")
    kwargs.setdefault("plan_down_mbps", 100.0)
    kwargs.setdefault("plan_up_mbps", 20.0)
    return SubscriberTarget(login=login, **kwargs)


# ------------------------------------------------------- forme de l'adresse
@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("10.20.0.10", "10.20.0.10/32"),
        ("10.20.0.10/32", "10.20.0.10/32"),
        ("  10.20.0.10  ", "10.20.0.10/32"),
        ("2001:db8::1", "2001:db8::1/128"),
    ],
)
def test_l_adresse_est_ramenee_a_sa_forme_canonique(brut: str, attendu: str) -> None:
    """RouterOS reecrit toujours la cible avec son prefixe. Envoyer l'adresse
    nue puis relire '/32' ferait croire a un changement a CHAQUE cycle, donc un
    'set' perpetuel."""
    assert address_target(brut) == attendu


def test_un_prefixe_trop_large_est_ramene_a_l_hote() -> None:
    """Un /24 saisi par erreur briderait tous les voisins de l'abonne avec son
    propre plan : on ne garde que l'hote."""
    assert address_target("10.20.0.10/24") == "10.20.0.10/32"


def test_les_objets_ipaddress_sont_acceptes() -> None:
    """La colonne last_ip est de type INET : asyncpg rend un objet, pas du texte."""
    assert address_target(ipaddress.ip_address("10.20.0.10")) == "10.20.0.10/32"
    assert address_target(ipaddress.ip_interface("10.20.0.10/24")) == "10.20.0.10/32"


@pytest.mark.parametrize("brut", [None, "", "   ", "pas-une-ip", "0.0.0.0", "127.0.0.1"])
def test_une_adresse_inexploitable_ne_donne_pas_de_cible(brut: object) -> None:
    assert address_target(brut) is None


# ------------------------------------------------------------ etat desire
def test_la_file_vise_l_adresse_de_la_session() -> None:
    _, files, ecartes = desired_state(links=[], subscribers=[abonne()])

    assert len(files) == 1
    assert files[0].target == "10.20.0.10/32"
    assert ecartes == []


def test_le_sens_des_limites_est_celui_du_client() -> None:
    """Avec une cible ADRESSE, RouterOS lit max-limit=montant/descendant, le
    montant etant ce qui VIENT de la cible. Un plan 100 down / 20 up s'ecrit
    donc 20M/100M -- et c'est precisement ce que la cible interface inversait."""
    _, files, _ = desired_state(links=[], subscribers=[abonne(plan_down_mbps=100, plan_up_mbps=20)])
    assert files[0].max_limit == "20000000/100000000"


def test_un_abonne_hors_ligne_n_a_pas_de_file() -> None:
    """Ecrire une file sur sa DERNIERE adresse connue serait dangereux : le pool
    a pu la reattribuer, et on briderait un autre client."""
    _, files, ecartes = desired_state(links=[], subscribers=[abonne(address=None)])

    assert files == []
    assert [(s.login, "hors ligne" in s.reason) for s in ecartes] == [("dupont", True)]


def test_le_nom_de_la_file_ne_depend_pas_de_l_adresse() -> None:
    """C'est la cle de reconciliation : si elle bougeait avec l'IP, chaque
    renouvellement de bail provoquerait une suppression puis une creation."""
    assert abonne(address="10.20.0.10").queue_name == abonne(address="10.20.0.99").queue_name


def test_changement_d_adresse_produit_un_set_pas_un_remplacement() -> None:
    """L'abonne s'est reconnecte avec une autre IP : la file existante est
    recablee, elle n'est ni supprimee ni recreee."""
    _, files, _ = desired_state(links=[], subscribers=[abonne(address="10.20.0.77")])
    existante = {
        ".id": "*3",
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
    assert action.target_id == "*3"
    assert action.changes["target"] == ("10.20.0.10/32", "10.20.0.77/32")


def test_adresse_inchangee_ne_produit_rien() -> None:
    """Le piege de l'idempotence : la forme ecrite doit etre celle relue."""
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
    assert plan.unchanged == 1


# --------------------------------------------------- ce qui est mis de cote
def test_chaque_abonne_ecarte_est_justifie() -> None:
    """Un abonne absent du plan sans explication est indiscernable d'un abonne
    correctement shape : l'exploitant chercherait la panne ailleurs."""
    _, files, ecartes = desired_state(
        links=[],
        subscribers=[
            abonne("en-ligne"),
            abonne("hors-ligne", address=None),
            abonne("sans-plan", plan_down_mbps=None, plan_up_mbps=None),
            abonne("desactive", enabled=False),
        ],
    )

    assert [f.name for f in files] == ["freeqos-en-ligne"]
    motifs = {s.login: s.reason for s in ecartes}
    assert set(motifs) == {"hors-ligne", "sans-plan", "desactive"}
    assert "hors ligne" in motifs["hors-ligne"]
    assert "aucun debit" in motifs["sans-plan"]
    assert "desactive" in motifs["desactive"]


def test_les_ecarts_sont_serialises_pour_l_interface() -> None:
    _, files, ecartes = desired_state(links=[], subscribers=[abonne(address=None)])
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[]
    )
    plan.skipped = ecartes

    corps = plan.to_dict()
    assert corps["skipped"][0]["login"] == "dupont"
    assert "hors ligne" in corps["skipped"][0]["reason"]


# ------------------------------------------------------ mode historique
def test_le_mode_interface_reste_disponible() -> None:
    """Conserve pour un parc qui en depend deja, mais ce n'est pas le defaut."""
    _, files, _ = desired_state(links=[], subscribers=[abonne()], target_mode=TARGET_INTERFACE)
    assert files[0].target == "<pppoe-dupont>"


def test_le_mode_interface_n_exige_pas_d_adresse() -> None:
    _, files, ecartes = desired_state(
        links=[], subscribers=[abonne(address=None)], target_mode=TARGET_INTERFACE
    )
    assert files[0].target == "<pppoe-dupont>"
    assert ecartes == []


# ------------------------------------------------- deux abonnes, une adresse
def test_deux_abonnes_sur_la_meme_adresse_ne_sont_pas_shapes() -> None:
    """L'un des deux est perime : session fermee dont l'IP a ete reattribuee.
    On ne peut pas savoir lequel, et RouterOS n'appliquerait que la premiere
    file, en silence. Brider le mauvais client est pire que ne rien faire."""
    _, files, ecartes = desired_state(
        links=[],
        subscribers=[
            abonne("ancien", address="10.20.0.10"),
            abonne("nouveau", address="10.20.0.10"),
            abonne("tranquille", address="10.20.0.11"),
        ],
    )

    assert [f.name for f in files] == ["freeqos-tranquille"]
    motifs = {s.login: s.reason for s in ecartes}
    assert set(motifs) == {"ancien", "nouveau"}
    assert "revendiquee aussi par nouveau" in motifs["ancien"]
    assert "revendiquee aussi par ancien" in motifs["nouveau"]


def test_un_abonne_sans_debit_ne_bloque_pas_l_adresse_d_un_autre() -> None:
    """Il ne produirait aucune file : il ne prend la place de personne."""
    _, files, _ = desired_state(
        links=[],
        subscribers=[
            abonne("fantome", address="10.20.0.10", plan_down_mbps=None, plan_up_mbps=None),
            abonne("reel", address="10.20.0.10"),
        ],
    )
    assert [f.name for f in files] == ["freeqos-reel"]

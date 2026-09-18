"""Un plafond pose est-il REELLEMENT tenu par le reseau ?

Ces tests partent d'un cas reel : un abonne plafonne a 100 kbps dans
l'interface, mesure a 497 kbps sur le terrain, avec une file bien presente sur
le routeur. Chaque cause silencieuse de ce genre d'ecart a son test.
"""

from __future__ import annotations

from app.enforcement.models import MANAGED_COMMENT, QueueSpec
from app.enforcement.planner import masked_queues, targets_overlap
from app.services.limit_audit import (
    VERDICT_ABSENTE,
    VERDICT_CONTOURNE,
    VERDICT_DESACTIVEE,
    VERDICT_ECART,
    VERDICT_EN_VIGUEUR,
    VERDICT_MASQUEE,
    VERDICT_SANS_PLAFOND,
    audit_router,
    fasttrack_rules,
    fasttrack_verdict,
    limit_state,
)


def file(nom: str, cible: str, limite: str, **extra: object) -> dict[str, object]:
    ligne: dict[str, object] = {
        "name": nom,
        "target": cible,
        "max-limit": limite,
        "comment": MANAGED_COMMENT,
    }
    ligne.update(extra)
    return ligne


def voulue(nom: str, cible: str, up: float | None, down: float | None) -> QueueSpec:
    return QueueSpec(name=nom, target=cible, max_up_mbps=up, max_down_mbps=down)


# ------------------------------------------------------------- fasttrack
def test_fasttrack_actif_est_detecte() -> None:
    regles = fasttrack_rules(
        [
            {"chain": "forward", "action": "accept"},
            {"chain": "forward", "action": "fasttrack-connection", "comment": "defconf"},
        ]
    )
    assert len(regles) == 1
    assert regles[0].chain == "forward"


def test_fasttrack_desactive_n_alarme_pas() -> None:
    """Une regle deja coupee ne contourne rien : alarmer dessus ferait chercher
    une panne deja corrigee."""
    regles = fasttrack_rules(
        [{"chain": "forward", "action": "fasttrack-connection", "disabled": "true"}]
    )
    assert regles == []
    assert fasttrack_verdict(regles)["active"] is False


def test_pare_feu_illisible_ne_se_lit_pas_comme_propre() -> None:
    """'Je n'ai pas pu lire' et 'il n'y a rien' sont deux reponses differentes."""
    verdict = fasttrack_verdict(None)
    assert verdict["active"] is None
    assert "illisible" in verdict["detail"]


def test_le_fasttrack_donne_la_commande_qui_le_corrige() -> None:
    verdict = fasttrack_verdict(
        fasttrack_rules([{"chain": "forward", "action": "fasttrack-connection"}])
    )
    assert verdict["active"] is True
    assert "fasttrack-connection" in verdict["remedy"]


def test_une_file_parfaite_derriere_un_fasttrack_n_est_pas_en_vigueur() -> None:
    """LE cas qui fait perdre des journees : la file est juste, et ne bride rien."""
    rows = [file("freeqos-test-ba", "172.16.25.253/32", "100000/100000")]
    etat = limit_state(
        voulue("freeqos-test-ba", "172.16.25.253/32", 0.1, 0.1),
        rows=rows,
        masquees={},
        fasttrack=True,
    )
    assert etat.verdict == VERDICT_CONTOURNE
    assert etat.enforced is False


# -------------------------------------------------------------- masquage
def test_deux_files_sur_la_meme_cible_la_seconde_ne_bride_rien() -> None:
    """Cas releve sur NAS-BASSORA : deux parents sur 100.100.101.112/29."""
    rows = [
        file("freeqos-parent-DS-CCR", "100.100.101.112/29", "0/0"),
        file("freeqos-parent-NAS-TALLADJE", "100.100.101.112/29", "0/0"),
    ]
    masquees = masked_queues(rows)
    assert "freeqos-parent-NAS-TALLADJE" in masquees
    assert masquees["freeqos-parent-NAS-TALLADJE"]["by"] == "freeqos-parent-DS-CCR"
    # La premiere, elle, s'applique normalement.
    assert "freeqos-parent-DS-CCR" not in masquees


def test_un_reseau_plus_large_place_avant_masque_l_abonne() -> None:
    """Un /29 pose plus haut prend le trafic du /32 qui le suit."""
    rows = [
        file("bloc-pro", "100.100.101.112/29", "50000000/50000000"),
        file("freeqos-client", "100.100.101.114/32", "100000/100000"),
    ]
    assert "freeqos-client" in masked_queues(rows)


def test_un_parent_ne_masque_pas_son_enfant() -> None:
    """La hierarchie est VOULUE : la compter comme un masquage signalerait une
    panne a chaque abonne correctement rattache."""
    rows = [
        file("freeqos-parent-nord", "10.0.0.0/24", "100000000/100000000"),
        file("freeqos-alice", "10.0.0.5/32", "10000000/10000000", parent="freeqos-parent-nord"),
    ]
    assert masked_queues(rows) == {}


def test_une_file_desactivee_ne_masque_personne() -> None:
    rows = [
        file("vieille", "10.0.0.5/32", "1000000/1000000", disabled="true"),
        file("freeqos-alice", "10.0.0.5/32", "10000000/10000000"),
    ]
    assert masked_queues(rows) == {}


def test_deux_interfaces_identiques_se_masquent() -> None:
    rows = [
        file("a", "ether2,lan-bridge", "0/0"),
        file("b", "lan-bridge,ether2", "0/0"),
    ]
    assert "b" in masked_queues(rows)


def test_cibles_sans_rapport_ne_se_masquent_pas() -> None:
    assert targets_overlap("10.0.0.0/24", "192.168.1.0/24") is False
    assert targets_overlap("ether2", "ether3") is False
    assert targets_overlap("", "10.0.0.1/32") is False


# --------------------------------------------------------------- verdicts
def test_file_absente_dit_que_rien_ne_bride() -> None:
    etat = limit_state(
        voulue("freeqos-alice", "10.0.0.5/32", 1, 10), rows=[], masquees={}, fasttrack=False
    )
    assert etat.verdict == VERDICT_ABSENTE


def test_file_desactivee_a_la_main_est_signalee() -> None:
    rows = [file("freeqos-alice", "10.0.0.5/32", "1000000/10000000", disabled="true")]
    etat = limit_state(
        voulue("freeqos-alice", "10.0.0.5/32", 1, 10),
        rows=rows,
        masquees={},
        fasttrack=False,
    )
    assert etat.verdict == VERDICT_DESACTIVEE


def test_ecart_de_debit_nomme_les_deux_valeurs() -> None:
    """Le cas du terrain : 100 kbps decides, 100M/500M sur le routeur."""
    rows = [file("freeqos-test-ba", "172.16.25.253/32", "100M/500M")]
    etat = limit_state(
        voulue("freeqos-test-ba", "172.16.25.253/32", 0.1, 0.1),
        rows=rows,
        masquees={},
        fasttrack=False,
    )
    assert etat.verdict == VERDICT_ECART
    assert "100M/500M" in etat.detail
    assert "100000/100000" in etat.detail


def test_meme_debit_ecrit_autrement_reste_en_vigueur() -> None:
    """RouterOS rend '100000/100000' la ou on a ecrit '100k/100k' : ce n'est pas
    un ecart, et le signaler userait la confiance dans l'audit."""
    rows = [file("freeqos-alice", "10.0.0.5/32", "100k/100k")]
    etat = limit_state(
        voulue("freeqos-alice", "10.0.0.5/32", 0.1, 0.1),
        rows=rows,
        masquees={},
        fasttrack=False,
    )
    assert etat.verdict == VERDICT_EN_VIGUEUR
    assert etat.enforced is True


def test_le_masquage_prime_sur_le_debit_juste() -> None:
    rows = [
        file("bloc-pro", "10.0.0.0/24", "50000000/50000000"),
        file("freeqos-alice", "10.0.0.5/32", "1000000/10000000"),
    ]
    rapport = audit_router(
        router_name="pop",
        desired=[voulue("freeqos-alice", "10.0.0.5/32", 1, 10)],
        rows=rows,
        firewall=[],
    )
    (ligne,) = rapport["queues"]
    assert ligne["verdict"] == VERDICT_MASQUEE
    assert ligne["masked_by"] == "bloc-pro"


# ----------------------------------------------------------------- rapport
def test_le_rapport_compte_ce_qui_fuit() -> None:
    rows = [
        file("freeqos-alice", "10.0.0.5/32", "1000000/10000000"),
        file("freeqos-bob", "10.0.0.6/32", "999/999"),
    ]
    rapport = audit_router(
        router_name="pop",
        desired=[
            voulue("freeqos-alice", "10.0.0.5/32", 1, 10),
            voulue("freeqos-bob", "10.0.0.6/32", 1, 10),
            voulue("freeqos-carol", "10.0.0.7/32", 1, 10),
        ],
        rows=rows,
        firewall=[],
    )
    assert rapport["enforced"] == 1
    assert rapport["leaking"] == 2
    # Le plus grave d'abord : l'exploitant lit la premiere ligne, pas la dixieme.
    assert rapport["queues"][0]["verdict"] == VERDICT_ABSENTE


def test_le_fasttrack_condamne_tout_le_routeur() -> None:
    """Une seule regle suffit : aucun plafond du routeur n'est tenu."""
    rows = [file("freeqos-alice", "10.0.0.5/32", "1000000/10000000")]
    rapport = audit_router(
        router_name="pop",
        desired=[voulue("freeqos-alice", "10.0.0.5/32", 1, 10)],
        rows=rows,
        firewall=[{"chain": "forward", "action": "fasttrack-connection"}],
    )
    assert rapport["fasttrack"]["active"] is True
    assert rapport["leaking"] == 1
    assert rapport["queues"][0]["verdict"] == VERDICT_CONTOURNE


# =========================================================================
# De bout en bout : un plafond saisi PART sur le routeur
# =========================================================================
#
# LE DEFAUT D'ORIGINE. Fixer un debit n'ecrivait qu'une ligne en base. Il
# fallait ensuite demander un plan, puis l'appliquer a la main -- ou attendre la
# reconciliation, qui ne passe que si l'enforcement est actif. Pendant ce
# temps-la, l'interface affichait "100 kbps impose" sur un abonne qui passait
# 497 kbps : une INTENTION presentee comme un FAIT.

import pytest  # noqa: E402

from app.config import Settings  # noqa: E402
from app.services.shaping import ShapingService  # noqa: E402
from tests.conftest import FakeRouterOsClient  # noqa: E402
from tests.test_pose_immediate import RouteurQuiSeSouvient  # noqa: E402
from tests.test_shaping_service import (  # noqa: E402
    DepotBoosts,
    MetriquesMinimales,
    make_service,
)


class DepotAvecSurcharge(DepotBoosts):
    """Depot minimal qui porte UNE surcharge d'abonne, comme apres une saisie."""

    def __init__(self, surcharges: dict[str, dict] | None = None) -> None:
        super().__init__()
        self.surcharges = surcharges or {}

    async def policy_map(self, scope):  # type: ignore[no-untyped-def]
        return self.surcharges if scope == "subscriber" else {}


def _abonne(login: str = "test-ba") -> dict[str, object]:
    return {
        "login": login,
        "pop_name": "PoP Test",
        "plan_down_mbps": 500.0,
        "plan_up_mbps": 100.0,
        "kind": "pppoe",
    }


def _service(
    settings: Settings, routeur: FakeRouterOsClient, ecriture, surcharges: dict[str, dict]
) -> ShapingService:
    return make_service(
        settings,
        routeur,
        repository=DepotAvecSurcharge(surcharges),
        metrics=MetriquesMinimales([_abonne()]),
        write_client_factory=lambda config: ecriture,
    )


@pytest.fixture
def routeur_lecture() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.add_session("test-ba", rx_byte=0, tx_byte=0)
    return client


async def test_un_plafond_de_100_kbps_part_sur_le_routeur(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """LE CAS DU TERRAIN. 100 kbps saisis, 100 kbps ecrits -- pas le plan."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = RouteurQuiSeSouvient(routeur_lecture)
    service = _service(
        settings,
        routeur_lecture,
        ecriture,
        {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}},
    )
    await service.registry.reload()

    rapport = await service.enforce_subscriber(login="test-ba", author="test")

    assert rapport["state"] == "file-posee"
    assert rapport["applied"] >= 1
    commandes = [a.command for a in ecriture.executed if a.path == "/queue/simple"]
    # 0,1 Mbps = 100 000 bits/s, dans les deux sens. Le plan souscrit
    # (100M/500M) ne doit PAS ressortir.
    assert any("max-limit=100000/100000" in c for c in commandes)
    assert not any("500000000" in c for c in commandes)


async def test_le_plafond_pose_est_ensuite_declare_en_vigueur(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Apres l'ecriture, l'audit doit confirmer -- sur le routeur, pas en base."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = RouteurQuiSeSouvient(routeur_lecture)
    service = _service(
        settings,
        routeur_lecture,
        ecriture,
        {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}},
    )
    await service.registry.reload()
    await service.enforce_subscriber(login="test-ba", author="test")

    audit = await service.limit_audit()

    (routeur,) = audit["routers"]
    ligne = next(q for q in routeur["queues"] if q["name"] == "freeqos-test-ba")
    assert ligne["verdict"] == VERDICT_EN_VIGUEUR
    assert ligne["login"] == "test-ba"
    assert audit["leaking"] == 0


async def test_l_audit_denonce_le_plafond_reste_en_base(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """La file porte le plan, la base porte le plafond : c'est exactement l'ecart
    qu'on mesurait sur le terrain, et il doit se VOIR."""
    settings.enforcement_enabled = False
    routeur_lecture.simple_queue_rows = [
        {
            ".id": "*1",
            "name": "freeqos-test-ba",
            "target": "10.20.0.1/32",
            "max-limit": "100M/500M",
            "comment": MANAGED_COMMENT,
        }
    ]
    service = _service(
        settings,
        routeur_lecture,
        RouteurQuiSeSouvient(routeur_lecture),
        {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}},
    )
    await service.registry.reload()

    audit = await service.limit_audit()

    ligne = next(q for q in audit["routers"][0]["queues"] if q["name"] == "freeqos-test-ba")
    assert ligne["verdict"] == VERDICT_ECART
    assert audit["leaking"] == 1


async def test_enforcement_coupe_le_dit_au_lieu_de_laisser_croire(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """En lecture seule, la saisie reussit -- et le rapport nomme l'obstacle."""
    settings.enforcement_enabled = False
    ecriture = RouteurQuiSeSouvient(routeur_lecture)
    service = _service(
        settings,
        routeur_lecture,
        ecriture,
        {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}},
    )
    await service.registry.reload()

    rapport = await service.enforce_subscriber(login="test-ba", author="test")

    assert rapport["state"] == "file-a-poser"
    assert "enforcement" in rapport["reason"]
    assert ecriture.executed == []


async def test_un_abonne_sans_routeur_est_nomme_comme_tel(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Un plafond sur un abonne qu'aucun PoP ne porte ne doit pas se taire."""
    service = _service(settings, routeur_lecture, RouteurQuiSeSouvient(routeur_lecture), {})
    await service.registry.reload()

    rapport = await service.enforce_subscriber(login="inconnu-au-bataillon", author="test")

    assert rapport["state"] == "sans-routeur"
    assert "aucun routeur" in rapport["reason"]


async def test_retirer_le_plafond_repousse_le_plan_souscrit(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Retirer une surcharge n'est pas supprimer la file : l'abonne retombe sur
    son plan, et c'est CE debit-la qu'il faut ecrire. Le laisser bride serait
    une panne d'autant plus dure a voir que l'interface afficherait deja le bon
    chiffre."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = RouteurQuiSeSouvient(routeur_lecture)
    surcharges = {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}}
    service = _service(settings, routeur_lecture, ecriture, surcharges)
    await service.registry.reload()
    await service.enforce_subscriber(login="test-ba", author="test")

    surcharges.clear()  # la surcharge vient d'etre supprimee en base
    rapport = await service.enforce_subscriber(login="test-ba", author="test", removing=True)

    assert rapport["state"] == "file-posee"
    commandes = [a.command for a in ecriture.executed if a.path == "/queue/simple"]
    assert any("max-limit=100000000/500000000" in c for c in commandes)


async def test_la_chaine_des_unites_va_du_kbps_aux_bits_par_seconde(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Un plafond saisi en kbps arrive en bits/s sur le routeur, sans derapage.

    Ce test verrouille la conversion de bout en bout parce que le symptome
    observe -- 100 kbps affiches, 100M/500M sur le routeur -- ressemblait a un
    facteur 1000 egare. Ce n'en etait pas un : la file portait le PLAN, jamais
    remplace par la surcharge. Le test le prouve dans les deux sens.
    """
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    ecriture = RouteurQuiSeSouvient(routeur_lecture)
    for kbps, attendu in ((100.0, "100000"), (512.0, "512000"), (2048.0, "2048000")):
        routeur_lecture.simple_queue_rows = []
        ecriture.executed.clear()
        service = _service(
            settings,
            routeur_lecture,
            ecriture,
            {
                "test-ba": {
                    "max_down_mbps": kbps / 1000.0,
                    "max_up_mbps": kbps / 1000.0,
                    "enabled": True,
                }
            },
        )
        await service.registry.reload()

        await service.enforce_subscriber(login="test-ba", author="test")

        commandes = [a.command for a in ecriture.executed if a.path == "/queue/simple"]
        assert any(f"max-limit={attendu}/{attendu}" in c for c in commandes), (kbps, commandes)


# =========================================================================
# L'audit ne doit JAMAIS retenir l'interface
# =========================================================================
#
# LE DEFAUT SHIPPE, ET SA LECON. La verification des plafonds a d'abord ete
# placee dans le chargement de la liste des abonnes, attendue avec le reste :
# la page est restee vide. Une page vide n'est pas un affichage plus honnete
# qu'un affichage incomplet -- c'est l'absence de reponse. Ce qui lit les
# routeurs ne bloque donc plus rien, et ne peut plus pendre.


class RouteurQuiTraine(FakeRouterOsClient):
    """Un routeur qui ne repond jamais. Il y en a un dans chaque parc."""

    # Une seconde suffit a depasser la minuterie du test ; plus longtemps
    # couterait ce temps a la suite entiere, car le fil continue de dormir
    # apres l'expiration.
    def simple_queues(self):  # type: ignore[no-untyped-def]
        import time as _t

        _t.sleep(1.0)
        return []


async def test_un_routeur_muet_coute_une_ligne_pas_la_page(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Le rapport arrive quand meme, et NOMME le routeur qu'il n'a pas pu lire."""
    service = _service(settings, RouteurQuiTraine(), RouteurQuiSeSouvient(routeur_lecture), {})
    service.AUDIT_TIMEOUT_S = 0.05  # type: ignore[misc]
    await service.registry.reload()

    audit = await service.limit_audit()

    (routeur,) = audit["routers"]
    assert "trop lent" in routeur["error"]
    # Surtout pas "0 plafond qui fuit" : on n'a rien verifie du tout.
    assert routeur["fasttrack"]["active"] is None
    assert routeur["queues"] == []


async def test_l_audit_ne_relit_pas_les_routeurs_a_chaque_appel(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """L'interface se rafraichit toutes les dix secondes ; la reconciliation
    passe toutes les deux minutes. Relire le parc a chaque coup serait le
    marteler pour une reponse qui n'a pas bouge."""
    service = _service(settings, routeur_lecture, RouteurQuiSeSouvient(routeur_lecture), {})
    await service.registry.reload()
    appels: list[int] = []
    vrai = service._audit_un_routeur  # noqa: SLF001

    async def compter(nom: str) -> dict:
        appels.append(1)
        return await vrai(nom)

    service._audit_un_routeur = compter  # type: ignore[assignment, method-assign]

    await service.limit_audit()
    await service.limit_audit()
    await service.limit_audit()

    assert len(appels) == 1


async def test_une_ecriture_perime_l_audit_en_cache(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    """Sinon on reafficherait une minute durant le defaut qu'on vient de
    corriger, et c'est la correction qu'on soupconnerait, pas le cache."""
    settings.enforcement_enabled = True
    settings.routers[0].rw_username = "qos-rw"
    service = _service(
        settings,
        routeur_lecture,
        RouteurQuiSeSouvient(routeur_lecture),
        {"test-ba": {"max_down_mbps": 0.1, "max_up_mbps": 0.1, "enabled": True}},
    )
    await service.registry.reload()

    premier = await service.limit_audit()
    ligne = next(q for q in premier["routers"][0]["queues"] if q["name"] == "freeqos-test-ba")
    assert ligne["verdict"] == VERDICT_ABSENTE

    await service.enforce_subscriber(login="test-ba", author="test")
    second = await service.limit_audit()

    ligne = next(q for q in second["routers"][0]["queues"] if q["name"] == "freeqos-test-ba")
    assert ligne["verdict"] == VERDICT_EN_VIGUEUR


# =========================================================================
# Un PoP qu'on vient d'ajouter n'est pas un PoP en panne
# =========================================================================
#
# CE QUE L'AUDIT ANNONCAIT A TORT. Sur un site neuf, il affichait "3 plafonds
# sur 3 ne sont PAS tenus" en rouge. Deux erreurs s'additionnaient :
#
# 1. il comptait comme "plafonds" des files parentes a 0/0 -- illimitees faute
#    de capacite connue sur le lien. Il n'y a rien a tenir dans une file qui ne
#    borne rien ;
# 2. il disait "rien ne bride cet abonne" sans dire POURQUOI la file n'est pas
#    la, alors que la cause est connue : enforcement coupe, ou reconciliation
#    pas encore passee.
#
# Resultat : une alarme rouge sur un site qui fonctionne, et la seule ligne qui
# comptait vraiment noyee au milieu.


def test_une_file_sans_plafond_n_est_pas_un_plafond_non_tenu() -> None:
    """Une file parente a 0/0 ne borne rien : il n'y a rien a tenir."""
    etat = limit_state(
        voulue("freeqos-parent-MikroTik", "172.16.38.0/23", None, None),
        rows=[],
        masquees={},
        fasttrack=False,
    )

    assert etat.verdict == VERDICT_SANS_PLAFOND
    assert etat.enforced is False
    assert etat.is_cap is False
    assert "capacite du lien inconnue" in etat.detail


def test_les_files_sans_plafond_ne_gonflent_pas_l_alarme() -> None:
    """Le cas exact du terrain : deux parents illimites et UN vrai plafond."""
    rapport = audit_router(
        router_name="NAS-Tailladje",
        desired=[
            voulue("freeqos-parent-MikroTik", "172.16.38.0/23", None, None),
            voulue("freeqos-parent-NAS-FRANCOPHONIE", "100.100.101.112/29", None, None),
            voulue("freeqos-test-ta", "172.16.39.253/32", 50, 300),
        ],
        rows=[],
        firewall=[],
    )

    # Un seul plafond en jeu, pas trois.
    assert rapport["leaking"] == 1
    assert rapport["enforced"] == 0
    assert rapport["uncapped"] == 2


def test_enforcement_coupe_explique_la_file_absente() -> None:
    """'Rien ne bride cet abonne' est exact et inutile : ce qui manque, c'est
    l'autorisation d'ecrire, et c'est cela qu'il faut lire."""
    rapport = audit_router(
        router_name="NAS-Tailladje",
        desired=[voulue("freeqos-test-ta", "172.16.39.253/32", 50, 300)],
        rows=[],
        firewall=[],
        enforcement_enabled=False,
    )

    (ligne,) = rapport["queues"]
    assert ligne["verdict"] == VERDICT_ABSENTE
    assert "enforcement est coupe" in ligne["detail"]


def test_un_site_neuf_dit_que_la_reconciliation_n_est_pas_passee() -> None:
    """Sur un PoP qu'on vient d'ajouter, la file part au prochain tour. Le dire
    evite de lire une panne la ou il n'y a qu'une attente."""
    rapport = audit_router(
        router_name="NAS-Tailladje",
        desired=[voulue("freeqos-test-ta", "172.16.39.253/32", 50, 300)],
        rows=[],
        firewall=[],
        enforcement_enabled=True,
        deja_reconcilie=False,
    )

    (ligne,) = rapport["queues"]
    assert "reconciliation n'est pas encore passee" in ligne["detail"]
    assert "site qu'on vient d'ajouter" in ligne["detail"]


def test_une_file_vraiment_manquante_reste_un_defaut() -> None:
    """La tolerance ne doit pas tout excuser : ecriture permise, reconciliation
    passee, et toujours pas de file -- c'est un defaut, et il se dit."""
    rapport = audit_router(
        router_name="NAS-Tailladje",
        desired=[voulue("freeqos-test-ta", "172.16.39.253/32", 50, 300)],
        rows=[],
        firewall=[],
        enforcement_enabled=True,
        deja_reconcilie=True,
    )

    (ligne,) = rapport["queues"]
    assert ligne["detail"] == "aucune file de ce nom sur le routeur : rien ne bride cet abonne"


async def test_le_service_dit_si_la_reconciliation_a_touche_ce_routeur(
    settings: Settings, routeur_lecture: FakeRouterOsClient
) -> None:
    service = _service(settings, routeur_lecture, RouteurQuiSeSouvient(routeur_lecture), {})
    await service.registry.reload()

    # Aucune passe : c'est "pas encore", pas "on ne sait pas".
    assert service._deja_reconcilie("pop-test") is False  # noqa: SLF001

    service.last_reconcile = {"at": "2026-09-18T12:00:00Z", "routers": ["pop-test"]}
    assert service._deja_reconcilie("pop-test") is True  # noqa: SLF001
    assert service._deja_reconcilie("un-autre") is False  # noqa: SLF001

    # Trace sans liste de routeurs : on ne conclut rien plutot que d'affirmer.
    service.last_reconcile = {"at": "2026-09-18T12:00:00Z"}
    assert service._deja_reconcilie("pop-test") is None  # noqa: SLF001

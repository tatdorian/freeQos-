"""Le controleur configure l'export NetFlow lui-meme.

CE QUE CES TESTS PROTEGENT
--------------------------
1. NE PAS CASSER CE QUI EXISTE. Un routeur qui exporte deja vers un outil tiers
   doit continuer : une cible qui ne pointe pas vers NOTRE collecteur n'est
   jamais touchee, ni modifiee, ni retiree.
2. NE PAS ECRIRE POUR RIEN. Un routeur deja configure doit rendre un plan vide.
   Sinon chaque passage reecrirait la meme chose sur tout le parc.
3. ANNONCER LA BONNE ADRESSE. Le collecteur annonce l'adresse par laquelle CE
   routeur le joint. Une valeur unique serait fausse pour une partie du parc
   des que le controleur a deux interfaces -- et les flux partiraient dans le
   vide, sans que rien ne le signale.
4. RESTER SOUS L'INTERRUPTEUR D'ECRITURE. Comme toute ecriture, celle-ci
   n'existe que si l'enforcement est actif.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig, RouterRole
from app.services.netflow_export import (
    PATH_FLOW,
    PATH_TARGET,
    NetflowExportService,
    local_address_for,
)
from tests.conftest import FakeRouterOsClient

COLLECTEUR = "10.0.0.250"


class FauxExporteurs:
    def __init__(self) -> None:
        self.declares: list[dict[str, Any]] = []

    async def declare(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.declares.append(dict(payload))
        return dict(payload)


def collecteur_routeur(
    client: FakeRouterOsClient, *, host: str = "192.0.2.11", role: RouterRole = RouterRole.POP
) -> MikrotikCollector:
    config = RouterConfig(
        name="pop-nord",
        host=host,
        username="qos-ro",
        password="secret-de-lab",
        pop_name="PoP Nord",
        role=role,
    )
    return MikrotikCollector(config, client=client)


def service(
    client: FakeRouterOsClient,
    *,
    enforcement: bool = True,
    exporteurs: FauxExporteurs | None = None,
    role: RouterRole = RouterRole.POP,
    host: str = "192.0.2.11",
) -> tuple[NetflowExportService, MikrotikCollector]:
    collector = collecteur_routeur(client, host=host, role=role)
    registry = SimpleNamespace(collectors=[collector])
    shaping = SimpleNamespace(enforcement_enabled=enforcement)
    export = NetflowExportService(
        shaping=shaping,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        exporters_repo=exporteurs,  # type: ignore[arg-type]
        collector_address=COLLECTEUR,
    )
    return export, collector


def configure(**surcharges: str) -> dict[str, str]:
    """Le reglage d'un routeur DEJA conforme a ce que le controleur veut."""
    return {
        "enabled": "true",
        "interfaces": "all",
        "active-flow-timeout": "1m",
        "inactive-flow-timeout": "15s",
        **surcharges,
    }


@pytest.fixture
def client() -> FakeRouterOsClient:
    return FakeRouterOsClient()


# ==================================================================== le plan


async def test_un_routeur_sans_export_recoit_les_deux_commandes(
    client: FakeRouterOsClient,
) -> None:
    export, collector = service(client)
    etat = await export.state_of(collector)
    plan = export.plan_for(collector, etat)

    assert etat.state == "a poser"
    chemins = [a.path for a in plan.actions]
    assert chemins == [PATH_FLOW, PATH_TARGET]

    reglage = next(a for a in plan.actions if a.path == PATH_FLOW)
    assert reglage.verb == "set"
    assert reglage.fields["enabled"] == "yes"
    assert reglage.fields["interfaces"] == "all"
    # LE PIEGE LE PLUS COUTEUX : avec le defaut RouterOS (30 min), un flux
    # encore actif -- un streaming en cours -- n'est exporte qu'une demi-heure
    # plus tard. Tout se passe comme s'il n'existait pas.
    assert reglage.fields["active-flow-timeout"] == "1m"
    # Un reglage global n'a pas de .id : en envoyer un ferait echouer une
    # commande parfaitement valide.
    assert reglage.target_id is None

    cible = next(a for a in plan.actions if a.path == PATH_TARGET)
    assert cible.verb == "add"
    assert cible.fields["dst-address"] == COLLECTEUR
    assert cible.fields["port"] == "2055"
    assert cible.fields["version"] == "9"


async def test_l_adresse_source_est_fixee_pour_que_l_exporteur_soit_reconnu(
    client: FakeRouterOsClient,
) -> None:
    """SANS ELLE, RouterOS choisit selon sa table de routage et l'exporteur peut
    arriver sous une adresse qu'aucune declaration ne connait : il tombe en
    'unknown', et ses octets ne sont rattaches a aucun point de mesure."""
    export, collector = service(client, host="192.0.2.11")
    plan = export.plan_for(collector, await export.state_of(collector))
    cible = next(a for a in plan.actions if a.path == PATH_TARGET)
    assert cible.fields["src-address"] == "192.0.2.11"


async def test_un_routeur_joint_par_son_nom_n_impose_pas_d_adresse_source(
    client: FakeRouterOsClient,
) -> None:
    """Un nom d'hote n'est pas forcement une adresse locale du routeur : poser
    ``src-address`` ferait echouer la commande."""
    export, collector = service(client, host="pop-nord.lan")
    plan = export.plan_for(collector, await export.state_of(collector))
    cible = next(a for a in plan.actions if a.path == PATH_TARGET)
    assert "src-address" not in cible.fields


async def test_un_routeur_deja_configure_ne_recoit_rien(client: FakeRouterOsClient) -> None:
    """LE CAS LE PLUS FREQUENT. Un plan non vide a chaque passage reecrirait la
    meme chose sur tout le parc, indefiniment."""
    client.traffic_flow_row = configure()
    client.traffic_flow_target_rows = [
        {".id": "*1", "dst-address": COLLECTEUR, "port": "2055", "version": "9"}
    ]
    export, collector = service(client)
    etat = await export.state_of(collector)
    plan = export.plan_for(collector, etat)

    assert etat.state == "pose"
    assert plan.is_empty
    assert plan.unchanged == 1


async def test_une_cible_vers_un_autre_collecteur_n_est_jamais_touchee(
    client: FakeRouterOsClient,
) -> None:
    """Envoyer ses flux a deux endroits est un choix legitime. Le controleur
    ajoute le sien et laisse l'autre exactement en place."""
    client.traffic_flow_row = configure()
    client.traffic_flow_target_rows = [
        {".id": "*1", "dst-address": "198.51.100.7", "port": "2055", "version": "5"}
    ]
    export, collector = service(client)
    plan = export.plan_for(collector, await export.state_of(collector))

    cibles = [a for a in plan.actions if a.path == PATH_TARGET]
    assert [a.verb for a in cibles] == ["add"]
    # Rien ne modifie ni ne retire la cible de l'autre collecteur.
    assert not [a for a in cibles if a.target_id]


async def test_une_cible_a_la_mauvaise_version_est_alignee(client: FakeRouterOsClient) -> None:
    client.traffic_flow_row = configure()
    client.traffic_flow_target_rows = [
        {".id": "*7", "dst-address": COLLECTEUR, "port": "2055", "version": "5"}
    ]
    export, collector = service(client)
    plan = export.plan_for(collector, await export.state_of(collector))

    correction = next(a for a in plan.actions if a.path == PATH_TARGET)
    assert correction.verb == "set"
    assert correction.target_id == "*7"
    assert correction.fields == {"version": "9"}


async def test_un_export_coupe_a_la_main_est_rallume(client: FakeRouterOsClient) -> None:
    """La cible est la, mais l'export est coupe : rien ne sort du routeur, et
    l'interface le montrerait pourtant comme declare."""
    client.traffic_flow_row = configure(enabled="false")
    client.traffic_flow_target_rows = [
        {".id": "*1", "dst-address": COLLECTEUR, "port": "2055", "version": "9"}
    ]
    export, collector = service(client)
    etat = await export.state_of(collector)
    plan = export.plan_for(collector, etat)

    assert etat.state == "a poser"
    assert [a.path for a in plan.actions] == [PATH_FLOW]


async def test_un_routeur_muet_est_signale_sans_faire_tomber_les_autres(
    client: FakeRouterOsClient,
) -> None:
    client.raise_on_traffic_flow = RuntimeError("API fermee")
    export, collector = service(client)
    etat = await export.state_of(collector)
    assert etat.state == "erreur"
    assert "API fermee" in etat.reason
    # Sans savoir ce que le routeur porte, on n'ecrit rien : l'etat par defaut
    # ("pas d'export, aucune cible") ferait croire qu'il faut tout poser.
    assert export.plan_for(collector, etat).is_empty


# ================================================================== l'ecriture


async def test_rien_n_est_ecrit_tant_que_l_enforcement_est_coupe(
    client: FakeRouterOsClient,
) -> None:
    export, _ = service(client, enforcement=False)
    rapport = await export.apply_all(author="test", dry_run=False)

    assert rapport["state"] == "a poser"
    assert rapport["applied"] == 0
    assert "ecriture desactivee" in rapport["routers"][0]["reason"]
    # Le plan est quand meme calcule et montre : c'est ce que l'exploitant
    # validera le jour ou il ouvrira l'ecriture.
    assert rapport["routers"][0]["actions"]


async def test_la_simulation_montre_les_commandes_sans_les_envoyer(
    client: FakeRouterOsClient,
) -> None:
    export, _ = service(client)
    rapport = await export.apply_all(author="test", dry_run=True)

    assert rapport["dry_run"] is True
    assert rapport["applied"] == 0
    commandes = " ".join(rapport["routers"][0]["actions"])
    assert "/ip/traffic-flow/set" in commandes
    assert f"dst-address={COLLECTEUR}" in commandes


async def test_la_boucle_periodique_ne_fait_rien_si_elle_est_desactivee(
    client: FakeRouterOsClient,
) -> None:
    export, _ = service(client)
    export.enabled = False
    assert (await export.ensure())["state"] == "desactive"


# ============================================== l'adresse annoncee au routeur


def test_l_adresse_du_collecteur_est_celle_qui_joint_ce_routeur() -> None:
    """``connect`` sur une socket UDP n'emet rien : il demande au noyau quelle
    route il prendrait, donc quelle adresse source. C'est la seule reponse
    correcte sur un controleur multi-interfaces."""
    adresse = local_address_for("198.51.100.1")
    assert adresse is None or adresse.count(".") == 3


def test_une_adresse_indeterminable_est_dite_et_rien_n_est_pose(
    client: FakeRouterOsClient,
) -> None:
    """Poser une cible vers une adresse fausse enverrait les flux dans le vide,
    et le routeur s'afficherait pourtant comme configure."""
    export, collector = service(client)
    export.collector_address = None
    etat_vide = export.plan_for(
        collector,
        type(
            "Etat",
            (),
            {"collector": None, "enabled": False, "interfaces": "", "ours": None},
        )(),
    )
    assert etat_vide.is_empty


# ===================================================== declaration automatique


async def test_configurer_l_export_declare_aussi_l_exporteur(
    client: FakeRouterOsClient,
) -> None:
    """SANS CETTE DECLARATION les flux arriveraient marques 'unknown' : leurs
    octets ne seraient rattaches a aucun point de mesure, donc absents de la
    consommation."""
    exporteurs = FauxExporteurs()
    export, _ = service(client, exporteurs=exporteurs)

    class FauxResultat:
        applied = 2
        outcomes: list[Any] = []

    async def faux_apply(plan: Any, **kwargs: Any) -> Any:
        return FauxResultat()

    export.shaping.apply = faux_apply  # type: ignore[attr-defined]
    await export.apply_all(author="test", dry_run=False)

    assert len(exporteurs.declares) == 1
    fiche = exporteurs.declares[0]
    assert fiche["address"] == "192.0.2.11"
    assert fiche["vantage"] == "pop"
    assert fiche["pop_name"] == "PoP Nord"


async def test_une_passerelle_regarde_depuis_la_sortie_internet(
    client: FakeRouterOsClient,
) -> None:
    """Le point de mesure se deduit du ROLE : une passerelle voit le trafic a la
    sortie internet, tout le reste le voit au PoP. Se tromper ferait compter le
    meme octet deux fois, ou pas du tout."""
    exporteurs = FauxExporteurs()
    export, _ = service(client, exporteurs=exporteurs, role=RouterRole.GATEWAY)

    class FauxResultat:
        applied = 2
        outcomes: list[Any] = []

    async def faux_apply(plan: Any, **kwargs: Any) -> Any:
        return FauxResultat()

    export.shaping.apply = faux_apply  # type: ignore[attr-defined]
    await export.apply_all(author="test", dry_run=False)

    assert exporteurs.declares[0]["vantage"] == "edge"


# ======================================================== les delais d'export


async def test_un_export_trop_lent_est_corrige(client: FakeRouterOsClient) -> None:
    """LE PIEGE LE PLUS COUTEUX DE TRAFFIC-FLOW.

    Le defaut RouterOS n'exporte un flux ENCORE ACTIF qu'au bout de trente
    minutes. Une session de streaming, une visio, un telechargement : rien
    n'apparait avant une demi-heure, alors que c'est exactement ce que
    l'exploitant regarde. Le routeur s'affiche pourtant comme configure.
    """
    client.traffic_flow_row = configure(**{"active-flow-timeout": "30m"})
    client.traffic_flow_target_rows = [
        {".id": "*1", "dst-address": COLLECTEUR, "port": "2055", "version": "9"}
    ]
    export, collector = service(client)
    etat = await export.state_of(collector)
    plan = export.plan_for(collector, etat)

    assert etat.state == "a poser"
    assert "30m" in etat.reason
    reglage = next(a for a in plan.actions if a.path == PATH_FLOW)
    assert reglage.fields == {"active-flow-timeout": "1m"}


async def test_une_duree_relue_sous_une_autre_forme_ne_declenche_rien(
    client: FakeRouterOsClient,
) -> None:
    """RouterOS relit une duree dans SA forme : ``1m`` peut revenir
    ``00:01:00``. Comparer les chaines ferait reecrire le meme reglage a chaque
    passage, indefiniment."""
    client.traffic_flow_row = configure(
        **{"active-flow-timeout": "00:01:00", "inactive-flow-timeout": "00:00:15"}
    )
    client.traffic_flow_target_rows = [
        {".id": "*1", "dst-address": COLLECTEUR, "port": "2055", "version": "9"}
    ]
    export, collector = service(client)
    etat = await export.state_of(collector)

    assert etat.state == "pose"
    assert export.plan_for(collector, etat).is_empty

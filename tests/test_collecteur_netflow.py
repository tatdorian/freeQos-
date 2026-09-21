"""Le collecteur NetFlow en service : declarations, fenetres, diagnostic.

CE QUI EST TESTE ICI EST LE CABLAGE, pas le decodage (cf. test_netflow.py) ni
le comptage (cf. test_flux_trafic.py) : ce qui arrive a un datagramme entre la
socket et la base.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from app.collectors.netflow import V5_HEADER, V5_RECORD
from app.services.flows import FlushBatch, PrefixIndex
from app.services.netflow_service import ExporterInfo, NetflowService


class DepotFlux:
    def __init__(self, prefixes: list[tuple[str, int]] | None = None) -> None:
        self.lots: list[FlushBatch] = []
        self.prefixes = prefixes if prefixes is not None else [("10.0.0.5/32", 7)]
        self.purges: list[float] = []

    async def write_batch(self, batch: FlushBatch) -> int:
        self.lots.append(batch)
        return len(batch.subscribers)

    async def subscriber_prefixes(self) -> list[tuple[str, int]]:
        return list(self.prefixes)

    async def prune_hosts(self, *, older_than_s: float) -> int:
        self.purges.append(older_than_s)
        return 0


class DepotExporteurs:
    def __init__(self, lignes: list[dict[str, Any]] | None = None) -> None:
        self.lignes = lignes or []
        self.activite: list[dict[str, Any]] = []

    async def list_all(self) -> list[dict[str, Any]]:
        return list(self.lignes)

    async def record_activity(self, stats: dict[str, dict[str, Any]]) -> None:
        self.activite.append(stats)


def exporteur(
    address: str, vantage: str = "pop", *, sampling: int = 1, enabled: bool = True
) -> dict[str, Any]:
    return {
        "address": address,
        "vantage": vantage,
        "pop_name": "PoP Nord",
        "sampling_rate": sampling,
        "enabled": enabled,
    }


def datagramme_v5(octets: int = 15_000, *, dst: str = "10.0.0.5") -> bytes:
    entete = V5_HEADER.pack(5, 1, 1000, 1_700_000_000, 0, 1, 0, 0, 0)
    corps = V5_RECORD.pack(
        ipaddress.IPv4Address("8.8.8.8").packed,
        ipaddress.IPv4Address(dst).packed,
        b"\x00" * 4,
        1,
        2,
        10,
        octets,
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
    return entete + corps


def service(**kwargs: Any) -> NetflowService:
    parametres: dict[str, Any] = {
        "flows_repo": DepotFlux(),
        "exporters_repo": DepotExporteurs(),
        "enabled": True,
        "customer_networks": ("10.0.0.0/8", "172.16.0.0/12"),
    }
    parametres.update(kwargs)
    svc = NetflowService(**parametres)
    svc.aggregator.set_index(PrefixIndex.build([("10.0.0.5/32", 7)]))
    return svc


async def test_un_datagramme_est_compte_sur_le_point_de_mesure_declare() -> None:
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    svc.exporters = {"10.10.0.1": ExporterInfo(vantage="edge", pop_name="Sortie")}

    svc.handle_datagram(datagramme_v5(), "10.10.0.1")
    await svc.flush()

    compteurs = depot.lots[0].subscribers[0]
    assert compteurs.vantage == "edge"
    assert compteurs.down_bytes == 15_000


async def test_un_exporteur_non_declare_est_compte_mais_marque() -> None:
    """IL DOIT SE VOIR, PAS DISPARAITRE.

    Un PoP qui exporte vers un collecteur qui l'ignore en silence reste
    invisible pendant des semaines, et personne ne comprend pourquoi ses
    chiffres manquent. On le compte en 'unknown' : les lectures de
    consommation ne le prennent pas (elles nomment leur point de mesure), mais
    l'exploitant le voit dans la liste.
    """
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    svc.handle_datagram(datagramme_v5(), "192.0.2.77")
    await svc.flush()

    assert depot.lots[0].subscribers[0].vantage == "unknown"


async def test_un_exporteur_desactive_est_ignore() -> None:
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    svc.exporters = {"10.10.0.1": ExporterInfo(vantage="pop", enabled=False)}
    svc.handle_datagram(datagramme_v5(), "10.10.0.1")
    await svc.flush()
    assert depot.lots == []


async def test_l_echantillonnage_de_l_entete_prime_sur_la_declaration() -> None:
    """Il vient de l'equipement ; la declaration n'est qu'un repli. Faire
    l'inverse laisserait une saisie obsolete fausser tous les volumes."""
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    svc.exporters = {"10.10.0.1": ExporterInfo(vantage="pop", sampling_rate=2)}

    entete = V5_HEADER.pack(5, 1, 1000, 1_700_000_000, 0, 1, 0, 0, 100)
    svc.handle_datagram(entete + datagramme_v5()[V5_HEADER.size :], "10.10.0.1")
    await svc.flush()

    assert depot.lots[0].subscribers[0].down_bytes == 15_000 * 100


async def test_un_datagramme_illisible_est_compte_et_ne_tue_pas_le_collecteur() -> None:
    """Un collecteur qui meurt sur un paquet malforme perd le trafic de TOUS
    les autres exporteurs."""
    svc = service()
    svc.handle_datagram(b"\x00\x03" + b"\xff" * 40, "10.10.0.1")
    svc.handle_datagram(datagramme_v5(), "10.10.0.1")

    assert svc.packets_received == 2
    assert svc.packets_rejected == 1
    assert svc.aggregator.flows_seen == 1


async def test_la_fenetre_est_ecrite_puis_les_declarations_relues() -> None:
    depot = DepotFlux(prefixes=[("10.0.0.5/32", 7), ("10.0.0.6/32", 8)])
    exporteurs = DepotExporteurs([exporteur("10.10.0.1", "edge")])
    svc = service(flows_repo=depot, exporters_repo=exporteurs)

    svc.handle_datagram(datagramme_v5(), "10.10.0.1")
    await svc.flush()

    assert exporteurs.activite[0]["10.10.0.1"]["flows"] == 1
    assert exporteurs.activite[0]["10.10.0.1"]["version"] == "v5"
    # Les declarations relues valent pour la fenetre SUIVANTE : un client saisi
    # doit compter des la minute d'apres, pas au prochain redemarrage.
    assert len(svc.aggregator.index) == 2
    assert svc.exporters["10.10.0.1"].vantage == "edge"


async def test_une_fenetre_vide_n_ecrit_rien() -> None:
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    assert await svc.flush() == 0
    assert depot.lots == []


async def test_les_hotes_trop_vieux_sont_purges() -> None:
    depot = DepotFlux()
    svc = service(flows_repo=depot, host_retention_s=3600.0)
    await svc.flush()
    assert depot.purges == [3600.0]


async def test_l_arret_ecrit_une_derniere_fenetre() -> None:
    """Sans cela, un redemarrage quotidien perd une minute de trafic par jour."""
    depot = DepotFlux()
    svc = service(flows_repo=depot)
    svc.handle_datagram(datagramme_v5(), "10.10.0.1")
    await svc.stop()
    assert depot.lots[0].subscribers


async def test_l_etat_dit_ce_qui_empeche_de_mesurer() -> None:
    """UN TABLEAU VIDE A TROIS CAUSES OPPOSEES : collecteur coupe, rien qui
    parle, ou des flux dont on ne sait pas lire le modele. Elles n'appellent pas
    le meme geste, et un ecran vide les confond toutes les trois."""
    svc = service()
    svc.handle_datagram(datagramme_v5(dst="203.0.113.9"), "10.10.0.1")
    etat = svc.status()

    assert etat["enabled"] is True
    assert etat["packets_received"] == 1
    assert etat["flows_seen"] == 1
    assert etat["flows_matched"] == 0
    assert etat["declared_prefixes"] == 1


async def test_un_collecteur_coupe_n_ecoute_pas() -> None:
    svc = service(enabled=False)
    await svc.start()
    assert svc.listening is False
    assert svc.status()["listening"] is False


async def test_un_port_deja_pris_se_dit_sans_empecher_le_demarrage() -> None:
    """LE RESTE DU CONTROLEUR DOIT TOURNER. Une collecte de trafic impossible
    n'est pas une raison de priver l'exploitant de sa supervision."""
    import socket

    prise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    prise.bind(("127.0.0.1", 0))
    port = prise.getsockname()[1]
    try:
        svc = service(bind="127.0.0.1", port=port)
        await svc.start()
        assert svc.listening is False
        assert svc.last_error is not None and str(port) in svc.last_error
    finally:
        prise.close()


async def test_le_collecteur_recoit_vraiment_sur_sa_socket() -> None:
    """Le seul test qui traverse la pile UDP : le reste est teste sans socket."""
    import asyncio
    import socket

    depot = DepotFlux()
    svc = service(flows_repo=depot, bind="127.0.0.1", port=0)
    await svc.start()
    assert svc.listening

    try:
        adresse = svc._transport.get_extra_info("sockname")  # noqa: SLF001
        emetteur = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        emetteur.sendto(datagramme_v5(), ("127.0.0.1", adresse[1]))
        emetteur.close()
        for _ in range(50):
            if svc.packets_received:
                break
            await asyncio.sleep(0.02)
    finally:
        await svc.stop()

    assert svc.packets_received == 1
    assert depot.lots[0].subscribers[0].down_bytes == 15_000


@pytest.mark.parametrize("vantage", ["edge", "pop"])
async def test_le_point_de_comptage_est_celui_qu_on_a_choisi(vantage: str) -> None:
    svc = service(accounting_vantage=vantage)
    assert svc.status()["accounting_vantage"] == vantage


async def test_les_reglages_de_trafic_se_changent_a_chaud() -> None:
    """Ils vivent en base comme les autres reglages : la fenetre suivante doit
    les prendre, sans redemarrage. L'ECOUTE, elle, reste dans l'environnement --
    ouvrir une socket n'est pas un reglage qu'on bascule depuis une page web."""
    svc = service(accounting_vantage="edge", host_limit=500)
    svc.apply_runtime(
        accounting_vantage="pop", track_hosts=False, host_limit=10, host_retention_s=600.0
    )
    assert svc.accounting_vantage == "pop"
    assert svc.aggregator.track_hosts is False
    assert svc.aggregator.host_limit == 10
    assert svc.status()["accounting_vantage"] == "pop"


async def test_un_collecteur_coupe_n_interroge_pas_la_base() -> None:
    """Le job de fenetre est planifie MEME collecteur coupe, pour que la cadence
    declaree dans les reglages pilote un job qui existe. Il doit donc rendre la
    main tout de suite plutot que d'interroger la base chaque minute pour une
    fenetre qui ne peut contenir que du vide."""
    depot = DepotFlux()
    exporteurs = DepotExporteurs([exporteur("10.10.0.1")])
    svc = service(enabled=False, flows_repo=depot, exporters_repo=exporteurs)

    assert await svc.flush() == 0
    assert depot.lots == []
    assert depot.purges == []
    assert exporteurs.activite == []

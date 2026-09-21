"""Collecteur NetFlow : ecoute UDP, agregation, ecriture periodique.

OU CE SERVICE SE PLACE DANS LE RESEAU
-------------------------------------
Il ecoute. Rien d'autre. Les routeurs qui portent deja le trafic lui envoient
un resume ; il ne demande rien, ne sonde rien, ne duplique aucun paquet.

Les exporteurs sont declares AUX DEUX EXTREMITES du reseau et jamais au milieu :

  - en amont du coeur, a la sortie internet (``vantage='edge'``). C'est la
    mesure de reference de ce qu'un abonne a consomme : tout ce qui vient
    d'internet et tout ce qui y va passe par la, une seule fois.
  - au PoP (``vantage='pop'``). Meme trafic, mais vu la ou le dernier kilometre
    commence : c'est le seul endroit ou l'etiquette VLAN et le secteur existent
    encore.

Le coeur, entre les deux, n'exporte rien et n'est pas interroge. C'est tout
l'interet de ce montage : la mesure ne lui ajoute aucune charge, ni a l'aller
ni au retour.

LE MEME OCTET EST DONC VU DEUX FOIS, et c'est voulu. Les additionner doublerait
la consommation de chacun ; le point de mesure est enregistre avec la mesure, et
la lecture en choisit un seul (``NETFLOW_ACCOUNTING_VANTAGE``).

CE QUI SE PASSE QUAND ON NE SUIT PAS LE RYTHME
----------------------------------------------
Un datagramme UDP perdu est un datagramme perdu : personne ne le retransmet.
Le decodage se fait donc dans la reception, sans allocation superflue, et
l'ecriture en base est REPORTEE a la fin de fenetre. C'est la raison d'etre de
l'agregation : une ligne par abonne et par minute plutot qu'une par flux.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.collectors.netflow import NetflowDecoder, NetflowParseError
from app.db.flows_repo import FlowsRepository, NetflowExportersRepository
from app.services.flows import FlowAggregator, PrefixIndex

logger = logging.getLogger(__name__)

JOB_NETFLOW = "netflow_flush"


@dataclass
class ExporterInfo:
    """Ce qu'on sait d'une machine qui exporte."""

    vantage: str = "unknown"
    sampling_rate: int = 1
    pop_name: str | None = None
    enabled: bool = True


@dataclass
class _Activity:
    version: str = ""
    packets: int = 0
    flows: int = 0


class NetflowProtocol(asyncio.DatagramProtocol):
    """Colle entre asyncio et le service. Volontairement minuscule."""

    def __init__(self, service: NetflowService) -> None:
        self._service = service

    def datagram_received(self, data: bytes, addr: tuple[str | Any, ...]) -> None:
        self._service.handle_datagram(data, str(addr[0]))

    def error_received(self, exc: Exception) -> None:
        logger.warning("Erreur sur la socket NetFlow : %s", exc)


@dataclass
class NetflowService:
    flows_repo: FlowsRepository | None = None
    exporters_repo: NetflowExportersRepository | None = None
    bind: str = "0.0.0.0"  # noqa: S104 - un collecteur ecoute sur tous les liens
    port: int = 2055
    enabled: bool = False
    accounting_vantage: str = "edge"
    customer_networks: tuple[str, ...] = ()
    host_limit: int = 500
    track_hosts: bool = True
    host_retention_s: float = 86_400.0

    decoder: NetflowDecoder = field(default_factory=NetflowDecoder)
    aggregator: FlowAggregator = field(default_factory=FlowAggregator)
    exporters: dict[str, ExporterInfo] = field(default_factory=dict)
    activity: dict[str, _Activity] = field(default_factory=dict)

    packets_received: int = 0
    packets_rejected: int = 0
    started_at: datetime | None = None
    last_flush_at: datetime | None = None
    last_error: str | None = None
    _transport: asyncio.DatagramTransport | None = None

    def __post_init__(self) -> None:
        self.aggregator.customer_networks = FlowAggregator.parse_networks(
            list(self.customer_networks)
        )
        self.aggregator.host_limit = self.host_limit
        self.aggregator.track_hosts = self.track_hosts

    def apply_runtime(
        self,
        *,
        accounting_vantage: str,
        track_hosts: bool,
        host_limit: int,
        host_retention_s: float,
    ) -> None:
        """Reprend les reglages pilotables a chaud depuis l'interface.

        L'ECOUTE, ELLE, NE SE BASCULE PAS. Ouvrir ou fermer une socket sur un
        port privilegie n'est pas un reglage qu'on change depuis une page web ;
        ``NETFLOW_ENABLED``, l'adresse et le port restent dans l'environnement,
        et l'interface ne pretend pas le contraire.
        """
        self.accounting_vantage = accounting_vantage
        self.track_hosts = track_hosts
        self.host_limit = host_limit
        self.host_retention_s = host_retention_s
        self.aggregator.track_hosts = track_hosts
        self.aggregator.host_limit = host_limit

    # ------------------------------------------------------------ cycle de vie
    async def start(self) -> None:
        if not self.enabled or self._transport is not None:
            return
        boucle = asyncio.get_running_loop()
        try:
            transport, _protocol = await boucle.create_datagram_endpoint(
                lambda: NetflowProtocol(self),
                local_addr=(self.bind, self.port),
            )
        except OSError as exc:
            # Port occupe, droits insuffisants sur un port < 1024 : ca doit se
            # dire fort et ne PAS empecher le reste du controleur de tourner.
            self.last_error = f"ecoute impossible sur {self.bind}:{self.port} : {exc}"
            logger.error("NetFlow : %s", self.last_error)
            return
        self._transport = transport
        self.started_at = datetime.now(tz=UTC)
        self.last_error = None
        logger.info(
            "NetFlow a l'ecoute sur %s:%d (v5, v9, IPFIX) -- comptage sur le point de mesure '%s'",
            self.bind,
            self.port,
            self.accounting_vantage,
        )
        await self.refresh_exporters()
        await self.refresh_index()

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        # Une derniere fenetre : ce qui a ete mesure doit etre ecrit, meme a
        # l'arret. Sinon un redemarrage quotidien perd une minute par jour.
        await self.flush()

    @property
    def listening(self) -> bool:
        return self._transport is not None

    # ------------------------------------------------------------- reception
    def handle_datagram(self, data: bytes, source: str) -> None:
        self.packets_received += 1
        try:
            paquet = self.decoder.decode(data, source)
        except NetflowParseError as exc:
            self.packets_rejected += 1
            self.last_error = f"{source} : {exc}"
            logger.debug("Datagramme NetFlow rejete depuis %s : %s", source, exc)
            return

        info = self.exporters.get(source) or ExporterInfo()
        if not info.enabled:
            return
        # L'echantillonnage annonce dans l'en-tete v5 fait foi s'il est present :
        # il vient de l'equipement, la declaration n'est qu'un repli.
        taux = paquet.sampling_interval or info.sampling_rate

        suivi = self.activity.setdefault(source, _Activity())
        suivi.version = f"v{paquet.version}"
        suivi.packets += 1
        suivi.flows += len(paquet.flows)

        for flux in paquet.flows:
            self.aggregator.add(
                flux,
                vantage=info.vantage,
                sampling_rate=taux,
                exporter=source,
                pop_name=info.pop_name,
            )

    # --------------------------------------------------------------- fenetres
    async def refresh_index(self) -> None:
        """Relit les blocs declares. Un client saisi doit compter TOUT DE SUITE."""
        if self.flows_repo is None:
            return
        try:
            entrees = await self.flows_repo.subscriber_prefixes()
        except Exception as exc:  # noqa: BLE001 - la collecte ne doit pas s'arreter
            logger.warning("Index des abonnes non relu : %s", exc)
            return
        self.aggregator.set_index(PrefixIndex.build(entrees))

    async def refresh_exporters(self) -> None:
        if self.exporters_repo is None:
            return
        try:
            lignes = await self.exporters_repo.list_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exporteurs NetFlow non relus : %s", exc)
            return
        self.exporters = {
            str(ligne["address"]): ExporterInfo(
                vantage=str(ligne["vantage"]),
                sampling_rate=int(ligne["sampling_rate"] or 1),
                pop_name=ligne["pop_name"],
                enabled=bool(ligne["enabled"]),
            )
            for ligne in lignes
        }

    async def flush(self) -> int:
        """Ecrit la fenetre courante, puis reprend les declarations.

        Ordre voulu : on vide D'ABORD l'agregat (la mesure est datee de
        maintenant, pas de la fin d'un rechargement qui peut prendre du temps),
        on ecrit, puis on relit index et exporteurs pour la fenetre suivante.
        """
        if not self.enabled:
            # Le job est planifie meme collecteur coupe (cf. container) : rendre
            # la main tout de suite evite d'interroger la base toutes les
            # minutes pour une fenetre qui ne peut contenir que du vide.
            return 0
        lot = self.aggregator.flush(datetime.now(tz=UTC))
        ecrites = 0
        if self.flows_repo is not None and not lot.empty:
            try:
                ecrites = await self.flows_repo.write_batch(lot)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"ecriture impossible : {exc}"
                logger.exception("Fenetre NetFlow non ecrite")
        if self.exporters_repo is not None and self.activity:
            instantane = {
                adresse: {"version": a.version, "packets": a.packets, "flows": a.flows}
                for adresse, a in self.activity.items()
            }
            self.activity = {}
            try:
                await self.exporters_repo.record_activity(instantane)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Activite des exporteurs non enregistree : %s", exc)
        self.last_flush_at = datetime.now(tz=UTC)
        await self.refresh_exporters()
        await self.refresh_index()
        if self.flows_repo is not None and self.host_retention_s > 0:
            try:
                await self.flows_repo.prune_hosts(older_than_s=self.host_retention_s)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Purge des hotes vus impossible : %s", exc)
        return ecrites

    # ----------------------------------------------------------------- etat
    def status(self) -> dict[str, Any]:
        """Ce qu'il faut pour diagnostiquer sans ouvrir un terminal.

        ``orphan_records`` merite une explication : en v9 et en IPFIX, les
        donnees sont illisibles sans le modele qui les decrit, et ce modele
        arrive dans un datagramme separe toutes les quelques minutes. Un nombre
        qui monte puis se stabilise est normal apres un demarrage ; un nombre qui
        monte SANS CESSE veut dire que l'exporteur n'envoie jamais ses modeles.
        """
        return {
            "enabled": self.enabled,
            "listening": self.listening,
            "bind": f"{self.bind}:{self.port}",
            "accounting_vantage": self.accounting_vantage,
            "started_at": self.started_at,
            "last_flush_at": self.last_flush_at,
            "packets_received": self.packets_received,
            "packets_rejected": self.packets_rejected,
            "flows_seen": self.aggregator.flows_seen,
            "flows_matched": self.aggregator.flows_matched,
            "orphan_records": self.decoder.orphan_records,
            "templates_known": len(self.decoder.templates),
            "declared_prefixes": len(self.aggregator.index),
            "exporters_known": len(self.exporters),
            "track_hosts": self.track_hosts,
            "last_error": self.last_error,
        }

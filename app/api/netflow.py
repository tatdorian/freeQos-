"""Trafic mesure par NetFlow, et declaration des exporteurs.

Ces routes servent l'interface d'exploitation (meme origine). L'API EXTERNE,
celle qu'un systeme tiers appelle, vit sous ``/model/v1`` et ``/usage/v1`` et
demande une cle.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import ContainerDep, TimeRangeDep
from app.db.destinations_repo import DestinationsRepository
from app.db.flows_repo import (
    ExporterNotFoundError,
    FlowsRepository,
    NetflowExportersRepository,
)
from app.services import ipfinder
from app.services.intel import IntelService
from app.services.netflow_export import NetflowExportService
from app.services.netflow_service import NetflowService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trafic (netflow)"])

Vantage = Literal["edge", "pop", "unknown"]


class ExporterInput(BaseModel):
    address: str = Field(
        min_length=1,
        max_length=64,
        description="Adresse IP depuis laquelle l'equipement exporte ses flux",
    )
    name: str | None = Field(default=None, max_length=128)
    vantage: Vantage = Field(
        default="pop",
        description=(
            "'edge' = en amont du coeur, a la sortie internet ; 'pop' = au PoP. "
            "Le meme octet est vu aux deux endroits : le comptage n'en retient "
            "qu'un seul."
        ),
    )
    pop_name: str | None = Field(default=None, max_length=128)
    sampling_rate: int = Field(
        default=1,
        ge=1,
        le=100_000,
        description=(
            "Taux d'echantillonnage configure sur l'equipement (1 = tout). Les "
            "octets lus sont multiplies par ce facteur."
        ),
    )
    enabled: bool = True
    note: str | None = Field(default=None, max_length=512)

    @field_validator("address")
    @classmethod
    def _valide(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError(f"adresse invalide : {value}") from exc


class ExporterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    vantage: Vantage | None = None
    pop_name: str | None = Field(default=None, max_length=128)
    sampling_rate: int | None = Field(default=None, ge=1, le=100_000)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=512)


def _service(container: ContainerDep) -> NetflowService:
    if container.netflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Collecteur NetFlow indisponible",
        )
    return container.netflow


def _flows(container: ContainerDep) -> FlowsRepository:
    if container.flows_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mesures de trafic indisponibles (base non initialisee)",
        )
    return container.flows_repo


def _exporters(container: ContainerDep) -> NetflowExportersRepository:
    if container.exporters_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Declaration des exporteurs indisponible (base non initialisee)",
        )
    return container.exporters_repo


def _destinations(container: ContainerDep) -> DestinationsRepository:
    if container.destinations_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Destinations indisponibles (base non initialisee)",
        )
    return container.destinations_repo


def _intel(container: ContainerDep) -> IntelService:
    if container.intel is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Enrichissement des adresses indisponible",
        )
    return container.intel


def _export(container: ContainerDep) -> NetflowExportService:
    if container.netflow_export is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configuration de l'export indisponible",
        )
    return container.netflow_export


def _valide_adresse(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"adresse invalide : {value}",
        ) from exc


@router.get("/netflow/status", summary="Etat du collecteur NetFlow")
async def netflow_status(container: ContainerDep) -> dict[str, Any]:
    return _service(container).status()


@router.get("/netflow/exporters", summary="Equipements qui exportent des flux")
async def list_exporters(container: ContainerDep) -> list[dict[str, Any]]:
    return list(await _exporters(container).list_all())


@router.post(
    "/netflow/exporters",
    status_code=status.HTTP_201_CREATED,
    summary="Declarer un exporteur (ou corriger sa declaration)",
)
async def declare_exporter(payload: ExporterInput, container: ContainerDep) -> dict[str, Any]:
    fiche = await _exporters(container).declare(payload.model_dump())
    # Sans ce rechargement, la declaration n'aurait d'effet qu'au prochain
    # flush : les flux de la minute en cours resteraient comptes en 'unknown'.
    await _service(container).refresh_exporters()
    return dict(fiche)


@router.patch("/netflow/exporters/{exporter_id}", summary="Modifier un exporteur")
async def update_exporter(
    exporter_id: Annotated[int, Path(ge=1)],
    payload: ExporterUpdate,
    container: ContainerDep,
) -> dict[str, Any]:
    champs = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        fiche = await _exporters(container).update(exporter_id, champs)
    except ExporterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _service(container).refresh_exporters()
    return dict(fiche)


@router.delete(
    "/netflow/exporters/{exporter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un exporteur de la liste",
)
async def delete_exporter(exporter_id: Annotated[int, Path(ge=1)], container: ContainerDep) -> None:
    try:
        await _exporters(container).delete(exporter_id)
    except ExporterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _service(container).refresh_exporters()


@router.get("/netflow/top", summary="Qui consomme, et combien")
async def top_talkers(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    vantage: Annotated[Vantage | None, Query()] = None,
) -> dict[str, Any]:
    service = _service(container)
    point = vantage or service.accounting_vantage
    repo = _flows(container)
    return {
        "vantage": point,
        "minutes": minutes,
        "totals": await repo.totals(minutes=minutes, vantage=point),
        "subscribers": await repo.top_subscribers(minutes=minutes, vantage=point, limit=limit),
    }


@router.get("/netflow/applications", summary="Repartition du trafic par usage")
async def applications(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    subscriber_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
) -> list[dict[str, Any]]:
    return await _flows(container).applications(
        minutes=minutes, subscriber_id=subscriber_id, limit=limit
    )


@router.get(
    "/netflow/subscribers/{subscriber_id}/series",
    summary="Volume dans le temps pour un abonne",
)
async def subscriber_series(
    subscriber_id: Annotated[int, Path(ge=1)],
    container: ContainerDep,
    window: TimeRangeDep,
    vantage: Annotated[Vantage | None, Query()] = None,
) -> list[dict[str, Any]]:
    point = vantage or _service(container).accounting_vantage
    return await _flows(container).subscriber_series(
        subscriber_id=subscriber_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
        vantage=point,
    )


@router.get(
    "/netflow/hosts",
    summary="Adresses vues dans les flux et rattachees a aucune fiche",
)
async def unmatched_hosts(
    container: ContainerDep,
    vlan: Annotated[int | None, Query(ge=1, le=4094)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    max_age_s: Annotated[float, Query(ge=60, le=2_592_000)] = 86_400.0,
) -> dict[str, Any]:
    """AIDE A LA SAISIE, ET RIEN D'AUTRE.

    Une adresse qui parle n'est pas un client. Une imprimante, une camera, un
    equipement d'un autre operateur laissent exactement la meme trace, et rien
    dans un flux ne dit quel debit a ete vendu. Aucune ligne d'ici ne devient
    une fiche toute seule : un humain la declare dans ``/static-clients``, ou
    elle expire.
    """
    repo = _flows(container)
    return {
        "hosts": await repo.hosts(vlan_id=vlan, limit=limit, max_age_s=max_age_s),
        "vlans": await repo.vlans(max_age_s=max_age_s),
    }


@router.post("/netflow/flush", summary="Ecrire la fenetre en cours tout de suite")
async def flush_now(container: ContainerDep) -> dict[str, Any]:
    service = _service(container)
    ecrites = await service.flush()
    return {"written": ecrites, "status": service.status()}


# ---------------------------------------------------------------------------
# CE QUE LES CLIENTS ATTEIGNENT
# ---------------------------------------------------------------------------
#
# NetFlow dit "cet abonne a echange 4 Go avec 45.57.12.34". Ces routes mettent
# un nom sur l'autre bout, parce qu'une adresse brute ne repond a aucune
# question d'exploitation. L'identification se fait sur l'ADRESSE, son nom
# inverse et, si l'exploitant l'autorise, le registre -- jamais sur le contenu :
# le trafic est chiffre, il le reste.


@router.get("/netflow/destinations", summary="Adresses atteintes par les clients")
async def destinations(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    subscriber_id: Annotated[int | None, Query(ge=1)] = None,
    client: Annotated[str | None, Query(max_length=64, description="Adresse du client")] = None,
    service: Annotated[str | None, Query(max_length=64)] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[
        str | None, Query(max_length=128, description="Adresse, nom ou organisation")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    repo = _destinations(container)
    return {
        "minutes": minutes,
        "destinations": await repo.top(
            minutes=minutes,
            subscriber_id=subscriber_id,
            client=_valide_adresse(client) if client else None,
            service=service,
            category=category,
            search=q,
            limit=limit,
        ),
        "services": await repo.by_service(minutes=minutes),
    }


@router.get(
    "/netflow/destinations/{address}",
    summary="Fiche detaillee d'une adresse atteinte",
)
async def destination_detail(
    address: str,
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 1440,
) -> dict[str, Any]:
    """Tout ce qu'on sait de cette adresse, et QUI la joint.

    C'est la fiche qu'on ouvre avant de decider d'une restriction : le nom
    inverse, le service reconnu et a quel titre, l'organisation, l'AS, le pays,
    depuis quand elle est vue, et la liste nominative des abonnes concernes.
    """
    adresse = _valide_adresse(address)
    fiche = await _destinations(container).detail(adresse, minutes=minutes)
    # Le verdict du CATALOGUE est recalcule a la volee, meme si la base n'a rien
    # retenu : il ne coute rien, et une adresse vue il y a trois secondes doit
    # deja s'afficher comme "Netflix" sans attendre le passage de la boucle.
    fiche["catalogue"] = ipfinder.match_prefix(adresse).to_dict()
    return fiche


@router.post(
    "/netflow/destinations/{address}/resolve",
    summary="Relancer l'analyse d'une adresse",
)
async def resolve_destination(address: str, container: ContainerDep) -> dict[str, Any]:
    """Redemande le nom inverse et le registre pour cette adresse.

    Sert quand un service change de nom inverse, ou apres une mise a jour du
    catalogue : sans cela, le verdict deja pose resterait tel quel, puisque le
    but de la file d'attente est justement de ne pas redemander sans fin.
    """
    return await _intel(container).resolve_now(_valide_adresse(address))


@router.get("/netflow/connections", summary="Connexions clients en cours")
async def connections(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    app: Annotated[str | None, Query(max_length=64, description="Famille d'usage")] = None,
) -> dict[str, Any]:
    """Ce qui se passe DANS LA FENETRE EN COURS, avant meme son ecriture.

    Les autres routes lisent la base, donc au mieux la minute precedente. Celle
    -ci lit l'agregat en memoire du collecteur : c'est la seule qui reponde a
    "qu'est-ce que ce client fait la, maintenant". Elle est donc vide juste
    apres un flush, et se remplit au fil de la fenetre -- ce n'est pas une
    panne, et l'interface le dit.
    """
    service = _service(container)
    # Le filtre est applique AVANT la limite : sinon demander "autre" sur une
    # fenetre dominee par le web rendrait cent lignes de web et zero de ce
    # qu'on a demande.
    lignes = [
        ligne
        for ligne in service.live_connections(100_000)
        if app is None or ligne.get("app") == app
    ][:limit]
    ids = [int(ligne["subscriber_id"]) for ligne in lignes if ligne.get("subscriber_id")]
    adresses = [str(ligne["address"]) for ligne in lignes]

    abonnes: dict[int, dict[str, Any]] = {}
    if container.flows_repo is not None:
        abonnes = await container.flows_repo.subscribers_by_id(ids)
    connaissances: dict[str, dict[str, Any]] = {}
    if container.destinations_repo is not None:
        connaissances = await container.destinations_repo.intel_for(adresses)

    for ligne in lignes:
        # UNE MACHINE SANS FICHE RESTE AFFICHEE, sous son adresse. Elle n'a pas
        # d'abonne -- un poste de supervision, une camera, un routeur -- mais
        # elle joint bien quelque chose, et c'est la question posee.
        fiche = abonnes.get(int(ligne["subscriber_id"] or 0)) or {}
        ligne["login"] = fiche.get("login")
        ligne["kind"] = fiche.get("kind")
        ligne["pop_name"] = fiche.get("pop_name")
        ligne["plan_down_mbps"] = fiche.get("plan_down_mbps")
        connu = connaissances.get(str(ligne["address"]))
        if connu is None:
            # Pas encore enrichie : le catalogue repond quand meme, et tout de
            # suite. Une adresse vue il y a deux secondes ne doit pas s'afficher
            # "inconnue" alors qu'elle est dans un bloc publie.
            connu = ipfinder.match_prefix(str(ligne["address"])).to_dict()
            ligne["hostname"] = None
            ligne["service"] = connu.get("service")
            ligne["category"] = connu.get("category")
            ligne["source"] = connu.get("source")
            ligne["pending"] = True
        else:
            ligne["hostname"] = connu.get("hostname")
            ligne["service"] = connu.get("service")
            ligne["category"] = connu.get("category")
            ligne["source"] = connu.get("source")
            ligne["org"] = connu.get("org")
            ligne["pending"] = connu.get("resolved_at") is None
    return {
        "window_open": service.listening,
        "tracked": service.track_destinations,
        # La duree d'accumulation : des octets sans duree ne sont qu'un volume,
        # et l'interface a besoin d'un debit.
        "window_seconds": round(service.window_seconds, 1),
        "connections": lignes,
    }


@router.get("/netflow/pairs", summary="Qui parle a qui : client et adresse atteinte")
async def pairs(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    app: Annotated[str | None, Query(max_length=64, description="Famille d'usage")] = None,
    client: Annotated[str | None, Query(max_length=64, description="Adresse du client")] = None,
    service: Annotated[str | None, Query(max_length=64)] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    pop: Annotated[str | None, Query(max_length=128)] = None,
    q: Annotated[
        str | None, Query(max_length=128, description="Client, login, adresse, nom")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Une ligne par CONVERSATION, sur la periode, et ce qui se passe maintenant.

    C'est la reponse a "cette famille d'usage pese 3 Kio -- mais avec qui ?".
    Le tableau par usage agrege justement ce detail ; celui par adresse fond
    tous les clients ensemble. Le couple est la seule forme qui montre la
    conversation.

    ``live`` porte la meme liste lue dans la fenetre EN COURS : l'interface
    marque les conversations qui se tiennent a cet instant.
    """
    repo = _destinations(container)
    lignes = await repo.pairs(
        minutes=minutes,
        app=app,
        client=_valide_adresse(client) if client else None,
        service=service,
        category=category,
        pop=pop,
        search=q,
        limit=limit,
    )
    collecteur = container.netflow
    directes = (
        [
            ligne
            for ligne in collecteur.live_connections(100_000)
            if app is None or ligne.get("app") == app
        ]
        if collecteur is not None
        else []
    )
    return {
        "minutes": minutes,
        "app": app,
        "pairs": lignes,
        "live": [(str(d["client"]), str(d["address"])) for d in directes],
        # Les valeurs REELLEMENT presentes : proposer un filtre qui ne rend rien
        # est pire que ne pas le proposer.
        "facets": await repo.pairs_facets(minutes=minutes),
        "live_bytes": {
            f"{d['client']}|{d['address']}": int(d["down_bytes"]) + int(d["up_bytes"])
            for d in directes
        },
        "window_seconds": round(collecteur.window_seconds, 1) if collecteur is not None else 0.0,
    }


@router.get("/netflow/catalogue", summary="Services que le controleur sait reconnaitre")
async def catalogue() -> dict[str, Any]:
    """De quoi ecrire une restriction : les services connus et leurs familles.

    Le nombre de blocs publies est rendu avec chaque service, et la note dit ce
    qu'il faut savoir avant de restreindre -- notamment qu'un CDN porte tout le
    monde, et qu'un service sans bloc publie n'est reconnu qu'au nom inverse.
    """
    return {
        "services": ipfinder.describe_catalogue(),
        "categories": list(ipfinder.CATEGORIES),
    }


@router.get("/netflow/intel", summary="Etat de l'enrichissement des adresses")
async def intel_status(container: ContainerDep) -> dict[str, Any]:
    return await _intel(container).status()


@router.post("/netflow/intel/run", summary="Nommer tout de suite les adresses en attente")
async def intel_run(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    service = _intel(container)
    traites = await service.resolve_pending(limit)
    return {"resolved": traites, "status": await service.status()}


@router.get("/netflow/export", summary="Export NetFlow configure sur les routeurs")
async def export_status(container: ContainerDep) -> dict[str, Any]:
    """Qui exporte deja vers ce collecteur, et qui ne le fait pas encore.

    L'adresse annoncee est calculee PAR ROUTEUR : c'est celle que le systeme
    utiliserait pour le joindre. Sur un controleur multi-interfaces, une valeur
    unique serait fausse pour une partie du parc.
    """
    return await _export(container).status()


@router.post("/netflow/export/apply", summary="Poser l'export NetFlow sur les routeurs")
async def export_apply(
    container: ContainerDep,
    dry_run: Annotated[bool, Query(description="Simulation : rien n'est ecrit")] = True,
    router: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:
    """Ecrit ``/ip/traffic-flow`` et sa cible sur les routeurs qui en manquent.

    Une cible qui pointe vers un AUTRE collecteur n'est jamais touchee :
    envoyer ses flux a deux endroits est un choix legitime.
    """
    return await _export(container).apply_all(
        author="ui:netflow-export", dry_run=dry_run, router_name=router
    )

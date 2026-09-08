"""API de topologie et d'enforcement (phase 2).

Le parcours est volontairement en trois temps :

  GET  /topology            ce que le controleur comprend du reseau
  GET  /shaping/state       ce qui est DEJA configure sur les routeurs
  POST /shaping/plan        ce qu'il faudrait changer, avec les commandes exactes
  POST /shaping/apply       execution, uniquement sur ordre explicite

Aucun endpoint de lecture n'ecrit sur un equipement. Le seul qui le fasse exige
``dry_run=false`` ET ``ENFORCEMENT_ENABLED=true``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, RepositoryDep
from app.enforcement.planner import LinkTarget, SubscriberTarget
from app.enforcement.routeros import MissingWriteCredentialsError
from app.services.shaping import EnforcementDisabledError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shaping"])


def _require_topology(container: ContainerDep):
    if container.topology_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Topologie indisponible (base non initialisee)",
        )
    return container.topology_repo


# --------------------------------------------------------------- topologie
@router.get("/topology", summary="Graphe du reseau tel que le controleur le comprend")
async def topology(container: ContainerDep) -> dict[str, Any]:
    repo = _require_topology(container)
    noeuds = await repo.nodes()
    liens = await repo.links()
    return {
        "nodes": noeuds,
        "links": liens,
        "counts": {"nodes": len(noeuds), "links": len(liens)},
        "sources": {
            "neighbors": "/ip/neighbor (MNDP, LLDP, CDP) - adjacence physique",
            "ethernet": "/interface/ethernet - debit negocie du port",
            "addresses": "/ip/address - segment L3 du lien",
            "uisp": "UISP /devices - liens radio et capacite du moment",
            "pppoe": "/ppp/active caller-id - MAC du CPE, rattache l'abonne au secteur",
        },
    }


@router.post("/topology/discover", summary="Relance la decouverte de topologie")
async def discover(container: ContainerDep) -> dict[str, Any]:
    """Lecture seule sur tous les PoPs, puis persistance du graphe."""
    devices: list[dict[str, Any]] = []
    fournisseur = container.backhaul_provider
    if hasattr(fournisseur, "raw_devices"):
        devices = await fournisseur.raw_devices()  # type: ignore[attr-defined]

    snapshot = await container.shaping.discover(uisp_devices=devices)
    return {
        "nodes": len(snapshot.nodes),
        "links": len(snapshot.links),
        "warnings": snapshot.warnings,
    }


@router.patch("/topology/nodes/{key:path}", summary="Corriger le role d'un equipement")
async def set_node_kind(
    key: str,
    container: ContainerDep,
    kind: Annotated[
        Literal["gateway", "core", "pop", "radio", "sector", "cpe", "unknown"] | None,
        Query(description="Role force ; omettre pour revenir a la detection"),
    ] = None,
) -> dict[str, Any]:
    """La classification automatique est une heuristique : l'operateur tranche."""
    repo = _require_topology(container)
    await repo.set_node_kind(key, kind)
    return {"key": key, "kind_override": kind}


# ------------------------------------------------------- etat du shaping
@router.get("/shaping/state", summary="Ce qui est deja configure sur les routeurs")
async def shaping_state(
    container: ContainerDep,
    router_name: Annotated[str | None, Query(alias="router")] = None,
) -> list[dict[str, Any]]:
    """Analyse de l'existant, sans rien modifier.

    Distingue explicitement ce qui appartient au controleur de ce qui a ete pose
    par l'operateur ou par RADIUS.
    """
    etats = await container.shaping.inspect(router_name)
    return [etat.to_dict() for etat in etats]


# ---------------------------------------------------------------- politique
class PolicyInput(BaseModel):
    scope: Literal["link", "subscriber"]
    target_key: str = Field(min_length=1, max_length=256)
    max_down_mbps: float | None = Field(default=None, ge=0, le=100_000)
    max_up_mbps: float | None = Field(default=None, ge=0, le=100_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


@router.get("/shaping/policies", summary="Surcharges de debit posees a la main")
async def list_policies(
    container: ContainerDep,
    scope: Annotated[Literal["link", "subscriber"] | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await _require_topology(container).policies(scope)


@router.put("/shaping/policies", summary="Fixer le debit d'un lien ou d'un abonne")
async def set_policy(payload: PolicyInput, container: ContainerDep) -> dict[str, Any]:
    """Enregistre la surcharge. N'ecrit RIEN sur le routeur : il faut ensuite
    demander un plan puis l'appliquer."""
    repo = _require_topology(container)
    enregistre = await repo.upsert_policy(
        scope=payload.scope,
        target_key=payload.target_key,
        max_down_mbps=payload.max_down_mbps,
        max_up_mbps=payload.max_up_mbps,
        enabled=payload.enabled,
        note=payload.note,
        updated_by="ui",
    )
    return {
        "policy": enregistre,
        "next_step": "POST /shaping/plan pour voir les commandes qui en decoulent",
    }


@router.delete("/shaping/policies/{scope}/{target_key:path}", summary="Retirer une surcharge")
async def delete_policy(
    scope: Literal["link", "subscriber"], target_key: str, container: ContainerDep
) -> dict[str, Any]:
    supprime = await _require_topology(container).delete_policy(scope, target_key)
    if not supprime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surcharge inconnue")
    return {"deleted": True}


# --------------------------------------------------------------------- plan
class PlanRequest(BaseModel):
    router: str = Field(min_length=1)


@router.post("/shaping/plan", summary="Calculer les commandes, sans rien envoyer")
async def build_shaping_plan(
    payload: PlanRequest, container: ContainerDep, metrics: RepositoryDep
) -> dict[str, Any]:
    """Produit le plan : la liste exacte des commandes RouterOS qui seraient
    envoyees, avec pour chacune la raison et ce qui change."""
    try:
        liens, abonnes = await _targets_for(container, metrics, payload.router)
        plan = await container.shaping.plan(payload.router, links=liens, subscribers=abonnes)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Lecture du routeur impossible : {type(exc).__name__}: {exc}",
        ) from exc

    orphelins = [a.login for a in abonnes if a.parent is None]
    donnees = plan.to_dict()
    donnees["unparented_subscribers"] = len(orphelins)
    if orphelins:
        donnees["notes"] = [
            f"{len(orphelins)} abonne(s) sans backhaul identifie : leur file est "
            "creee sans parent. Le dernier km est bien shape, mais la contention "
            "sur le backhaul ne l'est pas. Le rattachement vient de la jointure "
            "entre le caller-id PPPoE et les stations UISP."
        ]
    return donnees


class ApplyRequest(BaseModel):
    router: str = Field(min_length=1)
    # Defaut volontairement sur : appliquer pour de vrai doit etre un choix.
    dry_run: bool = True
    confirm: bool = False


@router.post("/shaping/apply", summary="Appliquer un plan (ecriture sur le routeur)")
async def apply_shaping(
    payload: ApplyRequest, container: ContainerDep, metrics: RepositoryDep
) -> dict[str, Any]:
    """Recalcule le plan puis l'execute.

    Le plan est RECALCULE juste avant d'appliquer, volontairement : appliquer un
    plan calcule il y a dix minutes reviendrait a ecrire sur un etat qui a pu
    changer entre-temps.
    """
    if not payload.dry_run and not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Application reelle : 'confirm' doit valoir true",
        )
    try:
        liens, abonnes = await _targets_for(container, metrics, payload.router)
        plan = await container.shaping.plan(payload.router, links=liens, subscribers=abonnes)
        resultat = await container.shaping.apply(plan, dry_run=payload.dry_run)
    except EnforcementDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MissingWriteCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{exc}. Declarez rw_username et rw_password_env pour ce routeur, "
                "et creez le compte qos-rw avec policy=read,write,api,test."
            ),
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"plan": plan.to_dict(), "result": resultat.to_dict()}


@router.get("/shaping/audit", summary="Journal des commandes envoyees")
async def audit(
    container: ContainerDep, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[dict[str, Any]]:
    return await _require_topology(container).audit(limit=limit)


# ------------------------------------------------------------------ interne
async def _targets_for(
    container: ContainerDep, metrics: RepositoryDep, router_name: str
) -> tuple[list[LinkTarget], list[SubscriberTarget]]:
    """Assemble l'etat desire a partir de la base et des surcharges.

    Les liens viennent de la topologie (capacite physique) et des backhauls
    (capacite radio mesuree) ; les abonnes de leur plan RADIUS. Les surcharges
    posees dans l'interface priment sur les deux.
    """
    repo = _require_topology(container)
    surcharges_liens = await repo.policy_map("link")
    surcharges_abonnes = await repo.policy_map("subscriber")

    collector = container.shaping._collector(router_name)  # noqa: SLF001
    pop_name = collector.config.effective_pop_name

    liens: list[LinkTarget] = []
    # cle du noeud d'en face -> file parent, pour rattacher chaque abonne au
    # lien qu'il traverse REELLEMENT et non a un lien pris au hasard.
    parent_par_noeud: dict[str, str] = {}
    for lien in await repo.links():
        if lien.get("discovered_by") != router_name or not lien.get("interface"):
            continue
        surcharge = surcharges_liens.get(lien["key"], {})
        if surcharge and not surcharge.get("enabled", True):
            continue
        cible = LinkTarget(
            name=str(lien.get("target_name") or lien["interface"]),
            interface=str(lien["interface"]),
            measured_capacity_mbps=lien.get("capacity_mbps"),
            override_down_mbps=surcharge.get("max_down_mbps"),
            override_up_mbps=surcharge.get("max_up_mbps"),
        )
        liens.append(cible)
        if lien.get("target_key"):
            parent_par_noeud[str(lien["target_key"])] = cible.queue_name

    # Capacite radio mesuree : elle prime sur le debit negocie du port ethernet,
    # car c'est elle le vrai goulot d'un backhaul sans fil.
    for backhaul in await metrics.backhaul_latest():
        if backhaul.get("pop_name") != pop_name or not backhaul.get("capacity_mbps"):
            continue
        for lien in liens:
            if lien.name == backhaul["name"]:
                lien.measured_capacity_mbps = backhaul["capacity_mbps"]

    # Rattachement issu de la jointure caller-id PPPoE <-> station UISP. C'est
    # le SEUL moyen de savoir par quel secteur passe un abonne. Sans lui, on ne
    # devine pas : la file abonne reste sans parent, ce qui shape correctement le
    # dernier km mais ne gere pas la contention sur le backhaul.
    rattachements = await repo.attachments()

    abonnes: list[SubscriberTarget] = []
    for ligne in await metrics.subscriber_latest(limit=5000, order_by="login"):
        if ligne.get("pop_name") != pop_name:
            continue
        login = str(ligne["pppoe_login"])
        surcharge = surcharges_abonnes.get(login, {})
        secteur = rattachements.get(login)
        abonnes.append(
            SubscriberTarget(
                login=login,
                interface=collector.config.pppoe_interface_pattern.format(
                    login=login, name=login, user=login
                ),
                plan_down_mbps=ligne.get("plan_down_mbps"),
                plan_up_mbps=ligne.get("plan_up_mbps"),
                override_down_mbps=surcharge.get("max_down_mbps"),
                override_up_mbps=surcharge.get("max_up_mbps"),
                enabled=surcharge.get("enabled", True),
                parent=parent_par_noeud.get(secteur) if secteur else None,
            )
        )

    return liens, abonnes

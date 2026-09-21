"""Restrictions de trafic : saisie, plan, application.

DEUX GESTES DISTINCTS, ET LA DISTINCTION EST TOUT L'INTERET :

  - ``POST/PATCH/DELETE /traffic-rules`` enregistre une INTENTION. Rien n'est
    ecrit sur un routeur. Une regle peut etre saisie, relue, corrigee sans
    qu'aucun paquet ne soit touche.
  - ``POST /traffic-rules/apply`` ECRIT -- et seulement si l'enforcement est
    actif. Le meme appel en simulation (``dry_run``, le defaut) rend les
    commandes exactes qui seraient envoyees.

C'est la meme separation que pour les files, et pour la meme raison : croire
qu'un trafic est bloque alors qu'il ne l'est pas est l'erreur la plus couteuse
que ce produit puisse induire.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import ContainerDep
from app.db.traffic_rules_repo import (
    RuleConflictError,
    RuleNotFoundError,
    TrafficRulesRepository,
)
from app.services.restrictions import InvalidRuleError, RestrictionService, validate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["traffic restrictions"])

Action = Literal["block", "limit"]
Scope = Literal["all", "subscribers"]
Protocol = Literal["tcp", "udp", "icmp"]


class RuleInput(BaseModel):
    """Une restriction telle qu'on la saisit.

    LES CRITERES SONT DES NOMS, PAS DES ADRESSES. On ecrit ``services=["netflix"]``
    et non une liste de blocs : c'est ce qui permet a la regle de suivre le
    service quand il ajoute des serveurs, sans que personne ne la reecrive.
    """

    name: str = Field(min_length=1, max_length=128)
    action: Action = "block"
    limit_down_mbps: float | None = Field(default=None, ge=0.01, le=100_000)
    limit_up_mbps: float | None = Field(default=None, ge=0.01, le=100_000)
    services: list[str] = Field(default_factory=list, max_length=50)
    categories: list[str] = Field(default_factory=list, max_length=20)
    prefixes: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Hand-entered prefixes, on top of the chosen services",
    )
    protocol: Protocol | None = None
    ports: str | None = Field(
        default=None,
        max_length=64,
        description="Port or range on the service side ('443', '6881-6999'). Requires a protocol.",
    )
    scope: Scope = "all"
    logins: list[str] = Field(default_factory=list, max_length=500)
    routers: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Target routers. Empty = every router of the active inventory.",
    )
    enabled: bool = True
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("ports")
    @classmethod
    def _valide_ports(cls, value: str | None) -> str | None:
        if value is None:
            return None
        texte = value.strip()
        if not texte:
            return None
        # RouterOS accepte '443', '80,443' et '6881-6999'. On refuse tout le
        # reste ici plutot que de laisser le routeur rejeter la commande a
        # l'application : l'erreur serait alors lue une heure plus tard.
        autorises = set("0123456789,-")
        if not texte or set(texte) - autorises:
            raise ValueError("ports invalides : chiffres, virgules et tirets seulement")
        return texte


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    action: Action | None = None
    limit_down_mbps: float | None = Field(default=None, ge=0.01, le=100_000)
    limit_up_mbps: float | None = Field(default=None, ge=0.01, le=100_000)
    services: list[str] | None = Field(default=None, max_length=50)
    categories: list[str] | None = Field(default=None, max_length=20)
    prefixes: list[str] | None = Field(default=None, max_length=200)
    protocol: Protocol | None = None
    ports: str | None = Field(default=None, max_length=64)
    scope: Scope | None = None
    logins: list[str] | None = Field(default=None, max_length=500)
    routers: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=1000)


def _repo(container: ContainerDep) -> TrafficRulesRepository:
    if container.traffic_rules_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restrictions unavailable (database not initialised)",
        )
    return container.traffic_rules_repo


def _service(container: ContainerDep) -> RestrictionService:
    if container.restrictions is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restriction service unavailable",
        )
    return container.restrictions


def _valide(payload: dict[str, Any]) -> None:
    try:
        validate(payload)
    except InvalidRuleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/traffic-rules", summary="The saved restrictions")
async def list_rules(container: ContainerDep) -> dict[str, Any]:
    """Les regles, et l'etat de la derniere pose.

    ``last_state`` est la seule chose qui distingue une regle SAISIE d'une regle
    POSEE. Sans lui, les deux se ressemblent trait pour trait dans la liste.
    """
    regles = await _repo(container).list_all()
    service = container.restrictions
    return {
        "rules": regles,
        "status": service.status() if service is not None else None,
    }


@router.post(
    "/traffic-rules",
    status_code=status.HTTP_201_CREATED,
    summary="Save a restriction (without writing anything to the routers)",
)
async def create_rule(payload: RuleInput, container: ContainerDep) -> dict[str, Any]:
    donnees = payload.model_dump()
    _valide(donnees)
    try:
        return await _repo(container).create(donnees)
    except RuleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/traffic-rules/{rule_id}", summary="Edit a restriction")
async def update_rule(
    rule_id: Annotated[int, Path(ge=1)],
    payload: RuleUpdate,
    container: ContainerDep,
) -> dict[str, Any]:
    repo = _repo(container)
    champs = payload.model_dump(exclude_unset=True)
    try:
        actuelle = await repo.get(rule_id)
    except RuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # La validation porte sur la regle TELLE QU'ELLE SERA, pas sur le fragment
    # envoye : retirer le dernier service d'une regle la rendrait sans critere,
    # et une regle sans critere vise tout internet.
    _valide({**actuelle, **champs})
    try:
        return await repo.update(rule_id, champs)
    except RuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/traffic-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a restriction",
)
async def delete_rule(rule_id: Annotated[int, Path(ge=1)], container: ContainerDep) -> None:
    """Retire la regle de la base.

    CE QUI EST POSE SUR LES ROUTEURS N'EST PAS RETIRE ICI. La reconciliation
    s'en charge au passage suivant, ou tout de suite via ``/traffic-rules/apply``
    -- et c'est deliberé : supprimer une fiche ne doit pas declencher une
    ecriture sur des equipements de production sans que personne ne l'ait
    demande.
    """
    try:
        await _repo(container).delete(rule_id)
    except RuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/traffic-rules/{rule_id}/preview", summary="What this rule targets today")
async def preview_rule(
    rule_id: Annotated[int, Path(ge=1)],
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Les adresses que la regle designe A CET INSTANT, et d'ou elles viennent.

    Une regle est un critere, pas une liste : elle grossit toute seule a mesure
    que NetFlow decouvre des serveurs. Avant de l'appliquer, il faut donc
    pouvoir regarder ce qu'elle couvre reellement -- surtout quand le critere
    est une famille entiere ou un CDN.
    """
    repo = _repo(container)
    try:
        regle = await repo.get(rule_id)
    except RuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    cible = await _service(container).resolve(regle)
    return {
        "rule": regle,
        "addresses": list(cible.destinations[:limit]),
        "address_count": len(cible.destinations),
        "clients": list(cible.clients[:limit]),
        "client_count": len(cible.clients),
        "routers": _service(container).routers_for(regle),
    }


@router.post("/traffic-rules/apply", summary="Apply the restrictions on the routers")
async def apply_rules(
    container: ContainerDep,
    dry_run: Annotated[bool, Query(description="Dry run: nothing is written")] = True,
    router: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, Any]:
    """Calcule le plan de chaque routeur, et l'applique si on le demande.

    ``dry_run=true`` par defaut : on montre d'abord. Meme a false, le drapeau
    ``ENFORCEMENT_ENABLED`` reste le dernier mot -- l'appel rend alors l'etat
    "a poser" avec la raison, plutot que d'echouer.
    """
    return await _service(container).apply_all(
        author="ui:restrictions", dry_run=dry_run, router_name=router
    )

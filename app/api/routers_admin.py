"""Gestion des PoPs depuis l'interface.

Ces endpoints ecrivent en base, jamais sur un equipement : ils declarent quels
routeurs INTERROGER. Le controleur reste hors-bande et en lecture seule vis-a-vis
du reseau.

Le mot de passe entre par ces routes et n'en ressort jamais : il est chiffre a
l'ecriture et aucune reponse ne le contient, meme chiffre.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import ContainerDep
from app.config import RouterConfig, RouterRole
from app.db.routers_repo import DuplicateRouterError, RouterNotFoundError
from app.services.crypto import SecretUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pops"])


class RouterInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="Identifiant unique du PoP")
    host: str = Field(min_length=1, max_length=255, description="IP ou nom d'hote de management")
    password: SecretStr = Field(min_length=1)
    port: int = Field(default=8728, ge=1, le=65535)
    username: str = Field(default="qos-ro", min_length=1, max_length=64)
    role: Literal["pop", "core", "gateway"] = "pop"
    pop_name: str | None = Field(default=None, max_length=128)
    enabled: bool = True
    use_ssl: bool = False
    timeout_s: float = Field(default=5.0, ge=0.5, le=60.0)
    pppoe_interface_pattern: str = "<pppoe-{login}>"


class RouterUpdate(BaseModel):
    """Mise a jour partielle : omettre le mot de passe le laisse inchange."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    password: SecretStr | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    role: Literal["pop", "core", "gateway"] | None = None
    pop_name: str | None = None
    enabled: bool | None = None
    use_ssl: bool | None = None
    timeout_s: float | None = Field(default=None, ge=0.5, le=60.0)
    pppoe_interface_pattern: str | None = None


def _require_repository(container: ContainerDep):
    if container.routers_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inventaire dynamique indisponible (base non initialisee)",
        )
    return container.routers_repo


def _guard_secrets(container: ContainerDep) -> None:
    """Refuse d'enregistrer un secret s'il ne peut pas etre chiffre.

    Mieux vaut un refus explicite qu'un mot de passe de routeur ecrit en clair
    dans PostgreSQL.
    """
    if not container.secrets.available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=container.secrets.unavailable_reason,
        )


@router.get("/pops/routers", summary="Inventaire des routeurs (fichier + base)")
async def list_routers(container: ContainerDep) -> dict[str, Any]:
    stored: list[dict[str, Any]] = []
    if container.routers_repo is not None:
        stored = await container.routers_repo.list_public()

    registry = container.registry
    actifs = {entry["name"]: entry for entry in registry.describe()}

    # Les routeurs de l'inventaire fichier n'ont pas de ligne en base : on les
    # expose quand meme, marques non modifiables.
    depuis_fichier = [
        {
            "id": None,
            "name": entry["name"],
            "host": entry["host"],
            "port": entry["port"],
            "username": entry["username"],
            "role": entry["role"],
            "pop_name": entry["pop"],
            "enabled": True,
            "source": "file",
            "editable": False,
            "active": True,
        }
        for entry in registry.describe()
        if entry["source"] == "file"
    ]
    depuis_base = [
        {**row, "source": "db", "editable": True, "active": row["name"] in actifs} for row in stored
    ]
    hidden: list[dict[str, Any]] = []
    if container.routers_repo is not None:
        try:
            hidden = await container.routers_repo.list_hidden_file_routers()
        except Exception:  # noqa: BLE001 - une base cassee ne doit pas vider la liste
            hidden = []
    return {
        "routers": depuis_fichier + depuis_base,
        "secrets_available": container.secrets.available,
        "secrets_reason": container.secrets.unavailable_reason,
        "skipped": registry.skipped,
        "hidden": hidden,
    }


@router.delete(
    "/pops/routers/file/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un routeur de l'inventaire fichier",
)
async def hide_file_router(container: ContainerDep, name: str) -> None:
    """Ecarte un routeur declare dans le fichier (ou ignore faute de secret).

    Le fichier reste la source de verite, mais ce nom est desormais ignore par
    le registre — sans editer le YAML ni redemarrer. Reversible via /restore.
    """
    repository = _require_repository(container)
    await repository.hide_file_router(name, reason="retire depuis l'interface")
    await _apply(container)


@router.post(
    "/pops/routers/file/{name}/restore",
    summary="Reafficher un routeur fichier precedemment retire",
)
async def restore_file_router(container: ContainerDep, name: str) -> dict[str, Any]:
    repository = _require_repository(container)
    restored = await repository.unhide_file_router(name)
    await _apply(container)
    return {"name": name, "restored": restored}


@router.post("/pops/routers/test", summary="Tester une connexion sans l'enregistrer")
async def test_connection(payload: RouterInput, container: ContainerDep) -> dict[str, Any]:
    """Ouvre une session API en lecture seule et renvoie l'identite du routeur.

    Permet de valider IP, port et identifiants avant d'enregistrer quoi que ce
    soit. La connexion est refermee immediatement.
    """
    config = RouterConfig(
        name=payload.name,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password=payload.password,
        role=RouterRole(payload.role),
        pop_name=payload.pop_name,
        use_ssl=payload.use_ssl,
        timeout_s=payload.timeout_s,
        pppoe_interface_pattern=payload.pppoe_interface_pattern,
    )
    probe = container.registry.build_probe(config)
    try:
        result = await probe.probe()
    except Exception as exc:  # noqa: BLE001 - le diagnostic est la reponse
        return {
            "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": _hint_for(exc, config),
        }
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass
    return result


def _hint_for(exc: Exception, config: RouterConfig) -> str:
    """Traduit les erreurs les plus frequentes en action concrete."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return (
            f"Aucune reponse sur {config.host}:{config.port}. Verifiez la route depuis "
            f"cette machine (nc -zv {config.host} {config.port}) et que le service API "
            "est actif : /ip service set api disabled=no"
        )
    if "refused" in text:
        return (
            f"Connexion refusee sur le port {config.port}. Le service API est "
            "probablement desactive, ou restreint a d'autres adresses "
            "(/ip service print)."
        )
    if "trap" in text or "cannot log in" in text or "invalid user" in text:
        return (
            "Identifiants refuses. Verifiez le compte et que son groupe possede "
            "les politiques 'api' et 'read' (policy=read,api,test)."
        )
    if "ssl" in text or "certificate" in text:
        return "Erreur TLS : verifiez que le service api-ssl est actif sur le port 8729."
    return "Consultez les logs du controleur pour le detail."


@router.post(
    "/pops/routers",
    status_code=status.HTTP_201_CREATED,
    summary="Enregistrer un routeur",
)
async def create_router(payload: RouterInput, container: ContainerDep) -> dict[str, Any]:
    repository = _require_repository(container)
    _guard_secrets(container)
    try:
        created = await repository.create(
            payload.model_dump(exclude={"password"}),
            payload.password.get_secret_value(),
        )
    except DuplicateRouterError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SecretUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await _apply(container)
    logger.info("Routeur '%s' enregistre depuis l'interface", created["name"])
    return created


@router.patch("/pops/routers/{router_id}", summary="Modifier un routeur")
async def update_router(
    payload: RouterUpdate,
    container: ContainerDep,
    router_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    repository = _require_repository(container)
    password = payload.password.get_secret_value() if payload.password else None
    if password:
        _guard_secrets(container)
    try:
        updated = await repository.update(
            router_id, payload.model_dump(exclude={"password"}), password
        )
    except RouterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DuplicateRouterError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await _apply(container)
    return updated


@router.delete(
    "/pops/routers/{router_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un routeur de l'inventaire",
)
async def delete_router(
    container: ContainerDep,
    router_id: Annotated[int, Path(ge=1)],
) -> None:
    repository = _require_repository(container)
    try:
        await repository.delete(router_id)
    except RouterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _apply(container)


@router.post("/pops/routers/{router_id}/probe", summary="Tester un routeur enregistre")
async def probe_router(
    container: ContainerDep,
    router_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    repository = _require_repository(container)
    try:
        stored = await repository.get_public(router_id)
    except RouterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    collector = next((c for c in container.registry.collectors if c.name == stored["name"]), None)
    if collector is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Routeur non charge (desactive, ou secret illisible)",
        )
    try:
        result = await collector.probe()
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        await repository.record_failure(router_id, message)
        return {"reachable": False, "error": message, "hint": _hint_for(exc, collector.config)}

    await repository.record_success(router_id, result)
    return result


async def _apply(container: ContainerDep) -> None:
    """Recharge l'inventaire et remet les collecteurs a jour, sans redemarrage."""
    collectors = await container.registry.reload()
    container.collection.set_collectors(collectors)

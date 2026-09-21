"""Gestion des antennes Ubiquiti (airOS) depuis l'interface.

Meme esprit que la gestion des PoPs : ces routes ecrivent en base quelles radios
INTERROGER sur leur API locale, jamais sur l'equipement. Declarer une antenne ici
suffit a la collecter -- aucune variable d'environnement, aucun redemarrage. Le
mot de passe entre par ces routes et n'en ressort jamais, meme chiffre.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field, SecretStr

from app.api.deps import ContainerDep
from app.collectors.uisp import AirOsClient, AirOsTarget, parse_airos_status
from app.db.antennas_repo import (
    AntennaNotFoundError,
    AntennasRepository,
    DuplicateAntennaError,
)
from app.services.crypto import SecretUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["antennas"])


class AntennaInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="Unique identifier of the antenna")
    pop_name: str = Field(min_length=1, max_length=128, description="PoP to attach the link to")
    host: str = Field(min_length=1, max_length=255, description="Management IP of the radio")
    username: str = Field(default="ubnt", max_length=64)
    # Optionnel : bien des parcs laissent /status.cgi accessible en lecture.
    password: SecretStr | None = None
    verify_tls: bool = False
    device_key: str | None = Field(default=None, max_length=128, description="Stable key (MAC)")
    nominal_capacity_mbps: float | None = Field(default=None, ge=0, le=100_000)
    enabled: bool = True
    timeout_s: float = Field(default=10.0, ge=0.5, le=60.0)


class AntennaUpdate(BaseModel):
    """Mise a jour partielle : omettre le mot de passe le laisse inchange."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    pop_name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=64)
    password: SecretStr | None = None
    verify_tls: bool | None = None
    device_key: str | None = Field(default=None, max_length=128)
    nominal_capacity_mbps: float | None = Field(default=None, ge=0, le=100_000)
    enabled: bool | None = None
    timeout_s: float | None = Field(default=None, ge=0.5, le=60.0)


def _require_repository(container: ContainerDep) -> AntennasRepository:
    if container.antennas_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Antenna inventory unavailable (database not initialised)",
        )
    return container.antennas_repo


def _guard_secrets(container: ContainerDep) -> None:
    if not container.secrets.available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=container.secrets.unavailable_reason,
        )


@router.get("/pops/antennas", summary="Ubiquiti antennas polled live")
async def list_antennas(container: ContainerDep) -> dict[str, Any]:
    stored: list[dict[str, Any]] = []
    if container.antennas_repo is not None:
        stored = await container.antennas_repo.list_public()
    return {
        "antennas": stored,
        "secrets_available": container.secrets.available,
        "secrets_reason": container.secrets.unavailable_reason,
    }


async def _probe_target(target: AirOsTarget, timeout_s: float) -> dict[str, Any]:
    client = AirOsClient(target, timeout_s=timeout_s)
    try:
        statut = await client.fetch_status()
    except Exception as exc:  # noqa: BLE001 - le diagnostic est la reponse
        return {
            "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": _hint_for(exc, target),
        }
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    sample = parse_airos_status(statut, key=target.key)
    return {
        "reachable": True,
        "capacity_mbps": sample.capacity_mbps,
        "capacity_down_mbps": sample.capacity_down_mbps,
        "capacity_up_mbps": sample.capacity_up_mbps,
        "signal_dbm": sample.signal_dbm,
        "mac": sample.raw.get("mac"),
    }


def _hint_for(exc: Exception, target: AirOsTarget) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return (
            f"No answer from {target.host}. Check the route from this machine "
            "and that the antenna web interface is reachable over HTTPS."
        )
    if "refused" in text:
        return f"Connection refused by {target.host}: HTTPS disabled, or port filtered."
    if "401" in text or "403" in text or "login" in text:
        return "Credentials refused: check the airOS account and its password."
    if "ssl" in text or "certificate" in text:
        return (
            "TLS error: the antenna has a self-signed certificate. Leave 'verify_tls' "
            "unticked for these radios."
        )
    return "See the controller logs for the details."


@router.post("/pops/antennas/test", summary="Test an antenna without saving it")
async def test_antenna(payload: AntennaInput, container: ContainerDep) -> dict[str, Any]:
    target = AirOsTarget(
        key=payload.device_key or payload.name,
        host=payload.host,
        username=payload.username,
        password=payload.password.get_secret_value() if payload.password else "",
        verify_tls=payload.verify_tls,
    )
    return await _probe_target(target, payload.timeout_s)


@router.post(
    "/pops/antennas",
    status_code=status.HTTP_201_CREATED,
    summary="Save an antenna",
)
async def create_antenna(payload: AntennaInput, container: ContainerDep) -> dict[str, Any]:
    repository = _require_repository(container)
    password = payload.password.get_secret_value() if payload.password else None
    if password:
        _guard_secrets(container)
    try:
        created = await repository.create(payload.model_dump(exclude={"password"}), password)
    except DuplicateAntennaError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SecretUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("Antenne '%s' enregistree depuis l'interface", created["name"])
    return created


@router.patch("/pops/antennas/{antenna_id}", summary="Edit an antenna")
async def update_antenna(
    payload: AntennaUpdate,
    container: ContainerDep,
    antenna_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    repository = _require_repository(container)
    password = payload.password.get_secret_value() if payload.password else None
    if password:
        _guard_secrets(container)
    try:
        updated = await repository.update(
            antenna_id, payload.model_dump(exclude={"password"}), password
        )
    except AntennaNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DuplicateAntennaError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return updated


@router.delete(
    "/pops/antennas/{antenna_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an antenna",
)
async def delete_antenna(
    container: ContainerDep,
    antenna_id: Annotated[int, Path(ge=1)],
) -> None:
    repository = _require_repository(container)
    try:
        await repository.delete(antenna_id)
    except AntennaNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/pops/antennas/{antenna_id}/probe", summary="Test a saved antenna")
async def probe_antenna(
    container: ContainerDep,
    antenna_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    repository = _require_repository(container)
    try:
        stored = await repository.get_public(antenna_id)
    except AntennaNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    targets = await repository.load_targets(enabled_only=False)
    key = stored.get("device_key") or stored["name"]
    target = next((t for t in targets if t.key == key), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Antenna cannot be loaded (unreadable secret?)",
        )
    result = await _probe_target(target, stored.get("timeout_s") or 10.0)
    if result.get("reachable"):
        await repository.record_success(antenna_id, result.get("capacity_mbps"))
    else:
        await repository.record_failure(antenna_id, result.get("error") or "echec")
    return result

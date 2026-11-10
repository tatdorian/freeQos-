"""Brancher le controleur Ubiquiti UISP depuis l'interface, et voir ses antennes.

UISP (ex-UNMS) connait toutes les antennes airMAX / LTU d'un WISP : leur
capacite radio, leur signal, leur temps d'antenne. On le relie une fois (son
adresse et un jeton d'API, en lecture), on voit le potentiel de chaque antenne,
et celles qu'on rattache a un site donnent leur capacite au shaping.

Lire est ouvert a tous les niveaux ; brancher, tester, rattacher sont des
gestes d'Administrator (la garde commune laisse les ecritures a ce niveau).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur
from app.collectors.uisp import UispProvider, uisp_device_summary
from app.db.uisp_repo import InvalidUispError, UispRepository, check_base_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ubiquiti"])


class UispInput(BaseModel):
    base_url: str = Field(min_length=8, max_length=300, examples=["https://uisp.example.net"])
    token: str | None = Field(
        default=None,
        max_length=500,
        description="UISP API token (Settings > Users > API tokens, read access). "
        "Leave empty to keep the stored one.",
    )
    verify_tls: bool = True


class UispTest(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=500)
    verify_tls: bool | None = None


class LinkInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pop_name: str = Field(min_length=1, max_length=128)


def _repo(container: ContainerDep) -> UispRepository:
    depot = getattr(container, "uisp_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ubiquiti UISP unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


def _message(exc: Exception) -> str:
    """Une erreur de connexion dite en clair."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"UISP refused the token ({code}): check it, and that it has read access"
        if code == 404:
            return "no UISP API at this address (404): give the UISP address, e.g. https://uisp.example.net"
        return f"UISP answered {code}"
    if isinstance(exc, httpx.ConnectError):
        return "cannot reach this address: check it, and that this server can reach UISP"
    if isinstance(exc, httpx.TimeoutException):
        return "UISP did not answer in time"
    if "CERTIFICATE" in str(exc).upper():
        return "the TLS certificate of UISP is not trusted: untick 'Verify the certificate'"
    return f"{type(exc).__name__}: {exc}"


async def _appareils(url: str, jeton: str, verifier: bool) -> list[dict[str, Any]]:
    fournisseur = UispProvider(url, jeton, verify_tls=verifier, timeout_s=15.0)
    try:
        return await fournisseur.raw_devices()
    finally:
        await fournisseur.aclose()


@router.get("/uisp", summary="The Ubiquiti UISP controller connected, if any")
async def get_uisp(container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    config = await depot.config()
    return {"configured": config is not None, **(config or {}), "links": await depot.links()}


@router.put("/uisp", summary="Connect the Ubiquiti UISP controller")
async def put_uisp(payload: UispInput, request: Request, container: ContainerDep) -> dict[str, Any]:
    try:
        return await _repo(container).save(
            base_url=payload.base_url,
            token=payload.token,
            verify_tls=payload.verify_tls,
            actor=acteur(request),
        )
    except InvalidUispError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/uisp", summary="Disconnect the Ubiquiti UISP controller")
async def delete_uisp(container: ContainerDep) -> dict[str, Any]:
    if not await _repo(container).delete():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No UISP connected")
    return {"deleted": True}


@router.post("/uisp/test", summary="Test the connection to UISP (stored or given)")
async def test_uisp(payload: UispTest, container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    stocke = await depot.credentials()
    try:
        url = check_base_url(payload.base_url or (stocke[0] if stocke else ""))
    except InvalidUispError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    jeton = payload.token or (stocke[1] if stocke else None)
    if not jeton:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="give the UISP API token"
        )
    verifier = (
        payload.verify_tls if payload.verify_tls is not None else (stocke[2] if stocke else True)
    )
    try:
        appareils = await _appareils(url, jeton, verifier)
    except Exception as exc:  # noqa: BLE001 - la raison est rendue en clair
        return {"ok": False, "detail": _message(exc)}
    radios = [uisp_device_summary(d) for d in appareils]
    return {
        "ok": True,
        "devices": len(radios),
        "with_capacity": sum(1 for r in radios if r["capacity_down_mbps"]),
    }


@router.get("/uisp/devices", summary="Antennas known to UISP, with their radio capacity")
async def uisp_devices(container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    acces = await depot.credentials()
    if acces is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No UISP connected")
    try:
        appareils = await _appareils(*acces)
    except Exception as exc:  # noqa: BLE001
        message = _message(exc)
        await depot.mark(ok=False, error=message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message) from exc
    liens = {lien["device_id"]: lien for lien in await depot.links()}
    radios = []
    for appareil in appareils:
        fiche = uisp_device_summary(appareil)
        lien = liens.get(fiche["id"] or "")
        fiche["linked_pop"] = lien["pop_name"] if lien else None
        radios.append(fiche)
    radios.sort(key=lambda r: (r["site"] or "~", r["name"] or ""))
    await depot.mark(ok=True, count=len(radios))
    return {"devices": radios}


@router.put("/uisp/links/{device_id}", summary="Attach a UISP antenna to a site")
async def put_link(
    payload: LinkInput,
    request: Request,
    container: ContainerDep,
    device_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> dict[str, Any]:
    try:
        return await _repo(container).set_link(
            device_id, name=payload.name, pop_name=payload.pop_name, actor=acteur(request)
        )
    except InvalidUispError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/uisp/links/{device_id}", summary="Detach a UISP antenna from its site")
async def delete_link(
    container: ContainerDep, device_id: Annotated[str, Path(min_length=1, max_length=128)]
) -> dict[str, Any]:
    if not await _repo(container).delete_link(device_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not attached")
    return {"deleted": device_id}

"""Nom de domaine et HTTPS de l'interface, regles depuis Settings.

Lire est ouvert a tous les niveaux ; changer le domaine est un geste
d'Administrator (la garde commune laisse les ecritures a ce niveau).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur
from app.services import public_access as acces

router = APIRouter(tags=["public access"])


class DomainInput(BaseModel):
    domain: str = Field(min_length=4, max_length=253, examples=["qos.example.com"])
    email: str | None = Field(
        default=None,
        max_length=254,
        description="Optional: Let's Encrypt writes here before a certificate expires.",
    )


def _repo(container: ContainerDep) -> acces.PublicAccessRepository:
    depot = getattr(container, "public_access_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Public access unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


def _admin_url(container: ContainerDep) -> str | None:
    return container.settings.proxy_admin_url or None


@router.get("/public-access", summary="Domain name and HTTPS of the interface")
async def get_public_access(container: ContainerDep) -> dict[str, Any]:
    url = _admin_url(container)
    fiche = await _repo(container).get()
    return {
        "proxy": url is not None,
        "proxy_reachable": await acces.proxy_reachable(url) if url else False,
        "domain": fiche["domain"] if fiche else None,
        "url": f"https://{fiche['domain']}/" if fiche else None,
        "email": fiche.get("email") if fiche else None,
        "applied_at": fiche.get("applied_at") if fiche else None,
        "last_error": fiche.get("last_error") if fiche else None,
        "dns": await acces.resolve(fiche["domain"]) if fiche else [],
    }


@router.put("/public-access", summary="Serve the interface under a domain name, in HTTPS")
async def put_public_access(
    payload: DomainInput, request: Request, container: ContainerDep
) -> dict[str, Any]:
    try:
        domaine = acces.check_domain(payload.domain)
        email = acces.check_email(payload.email)
    except acces.InvalidDomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    url = _admin_url(container)
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no HTTPS proxy is managed by freeQoS here (PROXY_ADMIN_URL is empty): "
            "set the domain in your own reverse proxy",
        )
    depot = _repo(container)
    await depot.save(domaine, email, acteur(request))
    try:
        await acces.push(url, acces.caddy_config(domaine, email))
    except RuntimeError as exc:
        await depot.mark(ok=False, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await depot.mark(ok=True)
    return {"domain": domaine, "applied": True, "dns": await acces.resolve(domaine)}


@router.delete("/public-access", summary="Stop serving the interface under a domain name")
async def delete_public_access(container: ContainerDep) -> dict[str, Any]:
    url = _admin_url(container)
    if url is not None:
        try:
            await acces.push(url, acces.caddy_config(None))
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await _repo(container).delete()
    return {"deleted": True}


@router.post("/public-access/check", summary="Check the DNS and the HTTPS of the domain")
async def check_public_access(container: ContainerDep) -> dict[str, Any]:
    fiche = await _repo(container).get()
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No domain set")
    dns = await acces.resolve(fiche["domain"])
    ok, detail = (
        await acces.https_ok(fiche["domain"]) if dns else (False, "the domain does not resolve yet")
    )
    return {"domain": fiche["domain"], "dns": dns, "https": ok, "detail": detail}

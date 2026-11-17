"""Les dernieres erreurs inattendues du serveur, pour un Administrator."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(tags=["status"])


def _admin(request: Request) -> None:
    user = getattr(request.state, "user", None) or {}
    if user.get("role") != "edit":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Server errors are for Administrators"
        )


@router.get("/status/errors", summary="Last unexpected server errors (Administrator)")
async def list_errors(request: Request) -> dict[str, Any]:
    _admin(request)
    erreurs = request.app.state.server_errors.list()
    return {"count": len(erreurs), "errors": erreurs}


@router.delete("/status/errors", summary="Clear the list of server errors")
async def clear_errors(request: Request) -> dict[str, Any]:
    _admin(request)
    request.app.state.server_errors.clear()
    return {"cleared": True}

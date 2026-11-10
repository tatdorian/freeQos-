"""La carte : ou sont les sites (PoPs), pour y retrouver ses clients.

Lire la carte est ouvert a tous les niveaux. PLACER un site est un geste
d'Administrator (la garde commune laisse les ecritures a ce niveau).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur
from app.db.site_locations_repo import InvalidLocationError, SiteLocationsRepository

router = APIRouter(tags=["map"])


class LocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90, examples=[13.5137])
    longitude: float = Field(ge=-180, le=180, examples=[2.1098])


def _repo(container: ContainerDep) -> SiteLocationsRepository:
    depot = getattr(container, "site_locations_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Map unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


@router.get("/site-locations", summary="Where the sites (PoPs) are, for the map")
async def list_locations(container: ContainerDep) -> dict[str, Any]:
    return {"locations": await _repo(container).list()}


@router.put("/site-locations/{name}", summary="Place a site on the map")
async def put_location(
    payload: LocationInput,
    request: Request,
    container: ContainerDep,
    name: Annotated[str, Path(min_length=1, max_length=128)],
) -> dict[str, Any]:
    try:
        return await _repo(container).put(
            name, payload.latitude, payload.longitude, actor=acteur(request)
        )
    except InvalidLocationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/site-locations/{name}", summary="Remove a site from the map")
async def delete_location(
    container: ContainerDep, name: Annotated[str, Path(min_length=1, max_length=128)]
) -> dict[str, Any]:
    if not await _repo(container).delete(name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"'{name}' is not placed")
    return {"deleted": name}

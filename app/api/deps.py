"""Dependances FastAPI.

Le conteneur est porte par ``app.state`` : les tests le remplacent par un
conteneur factice sans base ni routeur, via ``dependency_overrides``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, status

from app.container import Container
from app.db.repository import MetricsRepository
from app.scheduler import Scheduler
from app.services.collection import CollectionService


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application non initialisee",
        )
    return container


def get_repository(container: Annotated[Container, Depends(get_container)]) -> MetricsRepository:
    return container.repository


def get_scheduler(container: Annotated[Container, Depends(get_container)]) -> Scheduler:
    return container.scheduler


def get_collection(container: Annotated[Container, Depends(get_container)]) -> CollectionService:
    return container.collection


class TimeRange:
    """Fenetre temporelle normalisee, commune a tous les endpoints de series."""

    def __init__(self, start: datetime, end: datetime, bucket_seconds: int) -> None:
        self.start = start
        self.end = end
        self.bucket_seconds = bucket_seconds


def time_range(
    start: Annotated[datetime | None, Query(description="Debut (ISO 8601, UTC par defaut)")] = None,
    end: Annotated[datetime | None, Query(description="Fin (ISO 8601, UTC par defaut)")] = None,
    minutes: Annotated[
        int, Query(ge=1, le=60 * 24 * 31, description="Fenetre glissante si start absent")
    ] = 60,
    bucket_seconds: Annotated[
        int, Query(ge=1, le=86_400, description="Taille d'agregation en secondes")
    ] = 60,
) -> TimeRange:
    now = datetime.now(tz=UTC)
    end = end or now
    start = start or (end - timedelta(minutes=minutes))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start >= end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'start' doit preceder 'end'",
        )
    return TimeRange(start, end, bucket_seconds)


ContainerDep = Annotated[Container, Depends(get_container)]
RepositoryDep = Annotated[MetricsRepository, Depends(get_repository)]
SchedulerDep = Annotated[Scheduler, Depends(get_scheduler)]
CollectionDep = Annotated[CollectionService, Depends(get_collection)]
TimeRangeDep = Annotated[TimeRange, Depends(time_range)]

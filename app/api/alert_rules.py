"""Page Alerts : SES alertes, envoyees a SON adresse par le compte SMTP de Settings.

Chaque compte cree ses regles (« un routeur ne repond plus », « ces clients
hors ligne plus de 10 minutes », « CPU au-dessus de 85 % ») ; elles partent a
l'adresse email de ce compte. Un compte voit et modifie les siennes ; un
Administrator voit toutes celles de l'instance.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.alert_rules_repo import AlertRuleNotFoundError, AlertRulesRepository
from app.services import notifier
from app.services.accounts import ROLE_EDIT
from app.services.alert_rules import (
    CATEGORIES,
    InvalidAlertRuleError,
    check,
    send_to,
    smtp_ready,
)

router = APIRouter(tags=["alerts"])


class AlertRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=80, examples=["My routers"])
    kind: Literal["events", "client_offline", "router_cpu"] = "events"
    categories: list[str] = Field(
        default_factory=list,
        description="events: " + ", ".join(CATEGORIES),
    )
    sites: list[str] = Field(default_factory=list, max_length=500)
    targets: list[str] = Field(
        default_factory=list,
        max_length=5000,
        description="client_offline: the clients; router_cpu: the routers (empty = all)",
    )
    threshold: float | None = Field(default=None, description="router_cpu: busiest core, in %")
    delay_minutes: int = Field(default=3, ge=0, le=1440)
    notify_resolved: bool = True
    enabled: bool = True


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: Literal["events", "client_offline", "router_cpu"] | None = None
    categories: list[str] | None = None
    sites: list[str] | None = Field(default=None, max_length=500)
    targets: list[str] | None = Field(default=None, max_length=5000)
    threshold: float | None = None
    delay_minutes: int | None = Field(default=None, ge=0, le=1440)
    notify_resolved: bool | None = None
    enabled: bool | None = None


def _repo(container: Any) -> AlertRulesRepository:
    depot = getattr(container, "alert_rules_repo", None)
    if depot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Alerts unavailable (database not initialised)",
        )
    return depot  # type: ignore[no-any-return]


def _compte(request: Request) -> dict[str, Any]:
    return dict(getattr(request.state, "user", None) or {})


def _adresse(request: Request) -> str:
    """L'adresse email du compte : c'est la que partent ses alertes."""
    email = str(_compte(request).get("email") or "")
    if "@" not in email or email.startswith("api:"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email alerts belong to a user account: sign in with your account "
            "(an API key has no email address)",
        )
    return email


def _admin(request: Request) -> bool:
    return _compte(request).get("role") == ROLE_EDIT


async def _sienne(request: Request, container: Any, ident: int) -> dict[str, Any]:
    try:
        regle = await _repo(container).get(ident)
    except AlertRuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    email = str(_compte(request).get("email") or "")
    if regle["owner_email"].lower() != email.lower() and not _admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This alert belongs to another account",
        )
    return regle


def _sortie(regle: dict[str, Any], ouverts: dict[int, int]) -> dict[str, Any]:
    return {
        **{k: v for k, v in regle.items() if k not in ("created_at", "updated_at", "last_sent_at")},
        "last_sent_at": regle["last_sent_at"].isoformat() if regle.get("last_sent_at") else None,
        "open": ouverts.get(int(regle["id"]), 0),
    }


@router.get("/alert-rules", summary="My email alerts (all of them for an Administrator)")
async def list_rules(request: Request, container: ContainerDep) -> dict[str, Any]:
    depot = _repo(container)
    email = str(_compte(request).get("email") or "")
    regles = await depot.list(None if _admin(request) else email)
    ouverts = await depot.open_counts()
    config, motif = await smtp_ready(container)
    return {
        "me": email,
        "smtp": {"ready": config is not None, "reason": motif},
        "categories": list(CATEGORIES),
        "rules": [_sortie(r, ouverts) for r in regles],
    }


@router.post(
    "/alert-rules", status_code=status.HTTP_201_CREATED, summary="Create an email alert for me"
)
async def create_rule(
    payload: AlertRuleInput, request: Request, container: ContainerDep
) -> dict[str, Any]:
    email = _adresse(request)
    try:
        valeurs = check(payload.model_dump())
    except InvalidAlertRuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    regle = await _repo(container).create(email, valeurs)
    return _sortie(regle, {})


@router.patch("/alert-rules/{ident}", summary="Edit one of my email alerts")
async def update_rule(
    payload: AlertRuleUpdate,
    request: Request,
    container: ContainerDep,
    ident: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    actuelle = await _sienne(request, container, ident)
    try:
        valeurs = check({**actuelle, **payload.model_dump(exclude_unset=True)})
    except InvalidAlertRuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    regle = await _repo(container).update(ident, valeurs)
    return _sortie(regle, {})


@router.delete("/alert-rules/{ident}", summary="Delete one of my email alerts")
async def delete_rule(
    request: Request, container: ContainerDep, ident: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    await _sienne(request, container, ident)
    await _repo(container).delete(ident)
    return {"deleted": ident}


@router.post("/alert-rules/{ident}/test", summary="Send a test email for this alert")
async def test_rule(
    request: Request, container: ContainerDep, ident: Annotated[int, Path(ge=1)]
) -> dict[str, Any]:
    regle = await _sienne(request, container, ident)
    exemple = {
        "key": "test",
        "level": "warn",
        "title": f"Test of your alert '{regle['name']}'",
        "detail": "This is a test: your alerts reach this address.",
    }
    try:
        await send_to(
            container,
            str(regle["owner_email"]),
            rule_name=str(regle["name"]),
            nouveaux=[exemple],
            resolus=[],
        )
    except notifier.MailError as exc:
        await _repo(container).mark(ident, ok=False, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await _repo(container).mark(ident, ok=True)
    return {"sent": True, "to": regle["owner_email"]}

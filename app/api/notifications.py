"""Notifications par email (Administrator) : compte SMTP, test, historique.

Le mot de passe SMTP entre, il ne ressort jamais : on dit seulement qu'il est
enregistre.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur
from app.db.notifications_repo import CATEGORIES, NotificationsRepository
from app.services import notifier

router = APIRouter(tags=["notifications"])
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_HOTE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class SmtpInput(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = Field(default=None, max_length=254)
    #: Absent : garde le mot de passe enregistre. "" : l'efface.
    password: str | None = Field(default=None, max_length=512)
    from_addr: str = Field(min_length=3, max_length=254)
    recipients: list[str] = Field(min_length=1, max_length=20)
    events: list[str] = Field(default_factory=lambda: ["router", "site", "measurement", "uisp"])
    grace_minutes: int = Field(default=3, ge=0, le=120)
    enabled: bool = True


def _repo(container: ContainerDep) -> NotificationsRepository:
    repo = getattr(container, "notifications_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not initialised"
        )
    return repo  # type: ignore[no-any-return]


def _valider(p: SmtpInput) -> dict[str, Any]:
    hote = p.host.strip()
    if not _HOTE.match(hote):
        raise HTTPException(status_code=400, detail="SMTP host: a name like smtp.example.com")
    expediteur = p.from_addr.strip()
    if not _EMAIL.match(expediteur):
        raise HTTPException(
            status_code=400, detail=f"sender '{expediteur}' is not an email address"
        )
    destinataires = [d.strip() for d in p.recipients if d.strip()]
    mauvais = [d for d in destinataires if not _EMAIL.match(d)]
    if mauvais or not destinataires:
        raise HTTPException(
            status_code=400,
            detail="recipient(s) not valid: " + ", ".join(mauvais or ["none given"]),
        )
    inconnus = [e for e in p.events if e not in CATEGORIES]
    if inconnus:
        raise HTTPException(status_code=400, detail="unknown event(s): " + ", ".join(inconnus))
    return {
        "host": hote,
        "port": p.port,
        "security": p.security,
        "username": (p.username or "").strip() or None,
        "from_addr": expediteur,
        "recipients": list(dict.fromkeys(destinataires)),
        "events": list(dict.fromkeys(p.events)),
        "grace_minutes": p.grace_minutes,
        "enabled": p.enabled,
    }


@router.get("/notifications/smtp", summary="The SMTP account (never its password)")
async def get_smtp(container: ContainerDep) -> dict[str, Any]:
    repo = _repo(container)
    return {"config": await repo.config(), "categories": list(CATEGORIES)}


@router.put("/notifications/smtp", summary="Save the SMTP account for alert emails")
async def put_smtp(payload: SmtpInput, request: Request, container: ContainerDep) -> dict[str, Any]:
    repo = _repo(container)
    valeurs = _valider(payload)
    if payload.password:
        possible, raison = repo.can_encrypt
        if not possible:
            raise HTTPException(status_code=409, detail=raison or "encryption unavailable")
    await repo.save(valeurs, password=payload.password, actor=acteur(request))
    return {"config": await repo.config()}


@router.delete("/notifications/smtp", summary="Remove the SMTP account (no more emails)")
async def delete_smtp(container: ContainerDep) -> dict[str, Any]:
    await _repo(container).delete()
    return {"removed": True}


@router.post("/notifications/test", summary="Send a test email now")
async def test_smtp(container: ContainerDep) -> dict[str, Any]:
    repo = _repo(container)
    config = await repo.config()
    if not config:
        raise HTTPException(status_code=409, detail="save the SMTP account first")
    message = notifier.build_message(
        from_addr=config["from_addr"],
        recipients=config["recipients"],
        subject="[freeQoS] Test email",
        text="This is a test from freeQoS: alert emails will reach this address.\n",
        html_body=(
            '<div style="font-family:sans-serif">This is a test from <b>freeQoS</b>: '
            "alert emails will reach this address.</div>"
        ),
    )
    try:
        await notifier.send(config, await repo.password(), message)
    except notifier.MailError as exc:
        await repo.mark(ok=False, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await repo.mark(ok=True)
    return {"sent": True, "recipients": config["recipients"]}


@router.get("/notifications/history", summary="Problems detected, and whether they were emailed")
async def history(container: ContainerDep) -> dict[str, Any]:
    return {"events": await _repo(container).history()}

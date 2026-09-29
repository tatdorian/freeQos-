"""Assistant de support : question en clair -> diagnostic tire des mesures.

Ouvert a TOUS les comptes, lecture seule compris : poser une question ne
modifie rien sur le reseau. Seule garde en plus de la session : la requete doit
venir de la page elle-meme (meme origine), comme toute requete POST.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api import accounts, antennas_admin, metrics
from app.api.deps import ContainerDep
from app.services import assistant as svc
from app.services.accounts import SAFE_METHODES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant"])

LECTURE_DIRECTE_S = 10.0


async def _meme_origine(request: Request) -> None:
    if request.method.upper() not in SAFE_METHODES and not accounts._meme_origine(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request")


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    subscriber_id: int | None = Field(default=None, ge=1)
    # Lire l'abonne en direct sur le routeur (deux secondes) avant de repondre.
    live: bool = True


@router.get("/assistant/status", summary="Is the AI support assistant configured")
async def assistant_status(container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    return {"enabled": svc.enabled(settings), "model": settings.assistant_model}


def _court(x: dict[str, Any], cles: tuple[str, ...]) -> dict[str, Any]:
    return {k: x.get(k) for k in cles if x.get(k) is not None}


async def _essaie(erreurs: dict[str, str], nom: str, coro: Any) -> Any:
    """Une piece manquante n'empeche pas les autres : le modele est prevenu."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        erreurs[nom] = f"{type(exc).__name__}: {exc}"[:200]
        return None


async def gather_context(container: Any, subscriber_id: int | None, live: bool) -> dict[str, Any]:
    repo = container.repository
    collection = container.collection
    erreurs: dict[str, str] = {}
    ctx: dict[str, Any] = {
        "rtt_probe_enabled": bool(getattr(collection, "rtt_enabled", False)),
    }

    cycles = await _essaie(erreurs, "cycles", metrics.collection_cycles(collection))
    if cycles:
        ctx["measurement_cycles"] = cycles["cycles"]

    if subscriber_id is not None:
        fiche = await _essaie(erreurs, "subscriber", repo.get_subscriber(subscriber_id))
        if fiche is None and "subscriber" not in erreurs:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
        abonne: dict[str, Any] = {"record": fiche}
        login = (fiche or {}).get("login")
        if login:
            lignes = await _essaie(
                erreurs,
                "last_sample",
                repo.subscriber_latest(search=str(login), limit=10, include_unmeasured=True),
            )
            for ligne in lignes or []:
                if int(ligne.get("subscriber_id") or 0) == subscriber_id:
                    abonne["last_sample"] = ligne
        sonde = getattr(collection, "rtt_prober", None)
        if sonde is not None:
            abonne["latency"] = sonde.detail(subscriber_id)
        bloat = await _essaie(
            erreurs, "bufferbloat", repo.bufferbloat(minutes=60, subscriber_id=subscriber_id)
        )
        if bloat and bloat.get("subscribers"):
            abonne["bufferbloat_last_hour"] = bloat["subscribers"][0]
        tendances = await _essaie(erreurs, "insights", repo.subscriber_trends(days=7))
        for t in tendances or []:
            if int(t.get("subscriber_id") or 0) == subscriber_id:
                from app.services.insights import classify

                qoe = (abonne.get("bufferbloat_last_hour") or {}).get("qoe")
                abonne["seven_day_insight"] = classify(t, qoe)
        if live:
            direct = await _essaie(
                erreurs,
                "live_check",
                asyncio.wait_for(
                    metrics.subscriber_live(repo, container, collection, subscriber_id),
                    LECTURE_DIRECTE_S,
                ),
            )
            if direct is not None:
                abonne["live_check_on_router"] = direct
        ctx["subscriber"] = abonne

    reseau = await _essaie(erreurs, "network_bufferbloat", repo.bufferbloat(minutes=60))
    if reseau:
        notes = [b for b in reseau.get("subscribers") or [] if b.get("qoe")]
        notes.sort(key=lambda b: float(b["qoe"].get("score") or 100))
        ctx["worst_experience_last_hour"] = [
            _court(b, ("login", "pop_name", "grade", "qoe", "idle_rtt_ms", "loaded_rtt_ms"))
            for b in notes[:10]
        ]

    sites = await _essaie(erreurs, "capacity", metrics.capacity_insights(repo, hours=24))
    if sites:
        ctx["site_capacity_24h"] = sites["sites"][:20]

    radios = await _essaie(erreurs, "radios", antennas_admin.radios(container))
    if radios:
        ctx["radios"] = [
            {
                **_court(a, ("name", "pop_name", "issues", "stations_with_issues", "error")),
                "radio": a.get("radio"),
                "stations_with_problems": [s for s in a.get("stations") or [] if s.get("issues")][
                    :15
                ],
            }
            for a in radios["antennas"]
        ]

    if erreurs:
        ctx["unavailable_data"] = erreurs
    return ctx


@router.post(
    "/assistant", summary="Ask the AI support assistant", dependencies=[Depends(_meme_origine)]
)
async def ask_assistant(payload: Question, container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    if not svc.enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The AI assistant is off: set ANTHROPIC_API_KEY in .env "
                "(key from console.anthropic.com) and restart."
            ),
        )
    contexte = await gather_context(container, payload.subscriber_id, payload.live)
    try:
        resultat = await svc.ask(
            svc.make_client(settings),
            model=settings.assistant_model,
            question=payload.question,
            context=contexte,
        )
    except anthropic.AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The Anthropic API rejected the key (ANTHROPIC_API_KEY).",
        ) from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The AI assistant is rate limited: try again in a minute.",
        ) from exc
    except anthropic.APIStatusError as exc:
        logger.warning("Assistant : erreur API %s", exc.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The Anthropic API answered {exc.status_code}: {exc.message}"[:300],
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cannot reach api.anthropic.com from the controller (network or proxy).",
        ) from exc
    resultat["context"] = contexte
    return resultat

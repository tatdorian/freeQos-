"""Reglages d'exploitation, pilotes depuis l'interface et stockes en BASE.

L'environnement ne sert plus qu'a fournir un defaut au premier demarrage : des
qu'une valeur est posee ici, c'est elle qui fait foi, et elle prend effet sans
redemarrage (les options de shaping/CAKE au prochain plan, les cadences au
prochain tour de boucle).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.settings_repo import SettingsRepository
from app.services.runtime_config import (
    ReglageInconnuError,
    RuntimeConfig,
    ValeurInvalideError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

# Ce qui ne peut PAS venir de la base, et pourquoi : il faut ces valeurs AVANT
# de pouvoir ouvrir la base. Les y chercher serait circulaire.
AMORCAGE = [
    {"name": "DATABASE_URL", "why": "this value is needed to open the database itself"},
    {
        "name": "APP_SECRET_KEY / APP_SECRET_KEY_FILE",
        "why": "it decrypts the secrets stored in the database",
    },
    {"name": "ROUTERS_FILE / ROUTERS", "why": "file inventory, read at startup"},
    {"name": "APP_ENV / LOG_LEVEL / API_PREFIX", "why": "they fix how the process starts"},
]


async def _apres_changement(container: ContainerDep, name: str, avant: Any, apres: Any) -> None:
    """Le geste qui accompagne une bascule, fait AU MOMENT du changement.

    Boucle QoE (``qoe_loop_enabled``) : ses resserrages encore en base sont
    rendus des que le reglage change, dans un sens comme dans l'autre. Attendre
    le prochain tour de la boucle ne suffit pas : il ne vient jamais si sa
    cadence est a 0, et une remise en marche avant son passage reappliquerait
    d'un coup des decisions anciennes. Rendre aussi a la remise en marche
    couvre le cas ou la boucle etait deja coupee au demarrage.
    """
    if name == "queue_kind" and avant != apres:
        await _bascule_type_de_file(container, avant, apres)
        return
    if name != "qoe_loop_enabled" or avant == apres:
        return
    liberer = getattr(container.topology_repo, "release_qoe_trims", None)
    if not callable(liberer):
        return
    motif = (
        "closed QoE loop turned back on in Settings: starting from scratch"
        if apres
        else "closed QoE loop turned off in Settings"
    )
    try:
        liberes = await liberer(motif)
    except Exception:  # noqa: BLE001 - le reglage est deja applique et persiste
        logger.exception("Resserrages QoE non rendus apres la bascule de '%s'", name)
        return
    if liberes:
        logger.info("Resserrages QoE rendus (%s) : %s", motif, ", ".join(liberes))


async def _bascule_type_de_file(container: ContainerDep, avant: Any, apres: Any) -> None:
    """CAKE <-> FQ-CoDel : les files freeQoS sont repointees TOUT DE SUITE, en
    arriere-plan -- seuls leurs types changent, pas leurs debits."""
    shaping = getattr(container, "shaping", None)
    if shaping is None:
        return
    logger.info("Type des files : %s -> %s", avant, apres)

    async def repointer() -> None:
        try:
            refus = dict(getattr(shaping, "fq_codel_refused", {}))
            await shaping.reconcile()
            # Un routeur qui vient de refuser FQ-CoDel repasse en CAKE tout de suite.
            if dict(getattr(shaping, "fq_codel_refused", {})) != refus:
                await shaping.reconcile()
        except Exception:  # noqa: BLE001 - la reconciliation suivante rattrapera
            logger.exception("Repointage des files apres bascule impossible")

    tache = asyncio.create_task(repointer())
    _TACHES.add(tache)
    tache.add_done_callback(_TACHES.discard)


_TACHES: set[Any] = set()


def _config(container: ContainerDep) -> RuntimeConfig:
    config = container.runtime_config
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings unavailable (database not initialised)",
        )
    return config


def _repo(container: ContainerDep) -> SettingsRepository:
    repo = container.settings_repo
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings cannot be persisted (database not initialised)",
        )
    return repo


class ReglageInput(BaseModel):
    """Une valeur nulle est LEGITIME pour les options CAKE : elle signifie
    'ne pose pas ce champ' et laisse le defaut de RouterOS."""

    value: Any = None
    reason: str | None = Field(default=None, max_length=300)


@router.get("/settings", summary="Operational settings in force")
async def list_settings(container: ContainerDep) -> dict[str, Any]:
    """Chaque reglage avec sa valeur, son defaut, et d'ou vient la valeur
    ('db' = pose depuis l'interface, 'defaut' = valeur d'origine)."""
    config = _config(container)
    entrees = config.describe()
    groupes: dict[str, list[dict[str, Any]]] = {}
    for entree in entrees:
        groupes.setdefault(entree["group"], []).append(entree)
    shaping = getattr(container, "shaping", None)
    return {
        "settings": entrees,
        "groups": groupes,
        # Routeurs qui ont refuse FQ-CoDel : ils restent en CAKE.
        "fq_codel_refused": dict(getattr(shaping, "fq_codel_refused", {}) or {}),
        "from_db": sorted(config.overrides),
        # Dit noir sur blanc ce qui reste hors de portee de l'interface.
        "bootstrap_only": AMORCAGE,
    }


@router.get("/settings/history", summary="Who changed which setting, and when")
async def settings_history(container: ContainerDep) -> list[dict[str, Any]]:
    return await _repo(container).history()


@router.put("/settings/{name}", summary="Set a setting (stored in the database)")
async def set_setting(
    payload: ReglageInput,
    container: ContainerDep,
    name: Annotated[str, Path(description="Setting name, see GET /settings")],
) -> dict[str, Any]:
    """Valide, applique a chaud, puis persiste. L'ordre compte : on n'ecrit en
    base que ce qu'on a su appliquer."""
    config = _config(container)
    repo = _repo(container)
    try:
        avant = config.value(name)
        valeur = config.set(name, payload.value)
    except ReglageInconnuError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValeurInvalideError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await repo.set(name, valeur, updated_by="ui", reason=payload.reason)
    logger.info("Reglage '%s' fixe a %r depuis l'interface", name, valeur)
    await _apres_changement(container, name, avant, valeur)
    return {"name": name, "value": valeur, "source": "db", "applied": True}


@router.delete("/settings/{name}", summary="Reset a setting to its default")
async def clear_setting(
    container: ContainerDep,
    name: Annotated[str, Path(description="Setting name, see GET /settings")],
) -> dict[str, Any]:
    """Efface la valeur en base : le reglage retombe sur le defaut d'origine,
    immediatement."""
    config = _config(container)
    repo = _repo(container)
    try:
        avant = config.value(name)
        valeur = config.clear(name)
    except ReglageInconnuError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await repo.delete(name)
    logger.info("Reglage '%s' remis a son defaut (%r)", name, valeur)
    await _apres_changement(container, name, avant, valeur)
    return {"name": name, "value": valeur, "source": "defaut", "applied": True}


# ------------------------------------------------ type de file, par routeur
class QueueKindChoice(BaseModel):
    kind: Literal["cake", "fq-codel"] | None = Field(
        default=None, description="null: this router follows the default type"
    )


@router.get(
    "/shaping/queue-kinds",
    summary="Queue type of each router (CAKE or FQ-CoDel), and the default",
)
async def queue_kinds(container: ContainerDep) -> dict[str, Any]:
    shaping = container.shaping
    choix = await shaping.load_queue_kinds()
    refus = dict(getattr(shaping, "fq_codel_refused", {}) or {})
    return {
        "default": shaping.queue_kind_default(),
        "routers": [
            {
                "router": c.name,
                "pop_name": c.config.effective_pop_name,
                "role": str(getattr(c.config.role, "value", c.config.role) or ""),
                "choice": choix.get(c.name),
                "kind": shaping.queue_kind_for(c.name),
                "refused": refus.get(c.name),
                "write_enabled": bool(getattr(c.config, "write_enabled", True)),
            }
            for c in container.registry.collectors
        ],
    }


@router.put(
    "/shaping/queue-kinds/{name}",
    summary="Choose the queue type of one router (null: the default)",
)
async def set_queue_kind(
    payload: QueueKindChoice,
    request: Request,
    container: ContainerDep,
    name: Annotated[str, Path(min_length=1, max_length=128)],
) -> dict[str, Any]:
    """Seuls les TYPES des files freeQoS de ce routeur changent, tout de suite
    (en arriere-plan) : leurs debits ne bougent pas."""
    from app.api.deps import acteur

    shaping = container.shaping
    if not any(c.name == name for c in container.registry.collectors):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown router")
    pool = getattr(shaping, "owned_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database not initialised"
        )
    avant = shaping.queue_kind_for(name)
    async with pool.acquire() as conn:
        if payload.kind is None:
            await conn.execute("DELETE FROM router_queue_kinds WHERE router = $1", name)
        else:
            await conn.execute(
                """
                INSERT INTO router_queue_kinds (router, kind, updated_by) VALUES ($1, $2, $3)
                ON CONFLICT (router) DO UPDATE SET kind = EXCLUDED.kind,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                name,
                payload.kind,
                acteur(request),
            )
    if payload.kind == "fq-codel":
        # Un nouvel essai : le refus d'avant ne tient plus.
        shaping.fq_codel_refused.pop(name, None)
    await shaping.load_queue_kinds()
    apres = shaping.queue_kind_for(name)
    if avant != apres:
        await _bascule_type_de_file(container, avant, apres)
    return {"router": name, "choice": payload.kind, "kind": apres, "switching": avant != apres}

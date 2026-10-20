"""Reglages d'exploitation, pilotes depuis l'interface et stockes en BASE.

L'environnement ne sert plus qu'a fournir un defaut au premier demarrage : des
qu'une valeur est posee ici, c'est elle qui fait foi, et elle prend effet sans
redemarrage (les options de shaping/CAKE au prochain plan, les cadences au
prochain tour de boucle).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
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
    return {
        "settings": entrees,
        "groups": groupes,
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

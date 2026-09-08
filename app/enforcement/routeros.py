"""Execution des commandes sur RouterOS.

SEUL module du projet qui ecrit sur un equipement.

Separation des comptes : ce client utilise ``qos-rw``, jamais ``qos-ro``. Un
routeur sans identifiants d'ecriture ne peut simplement pas etre modifie, ce qui
donne un moyen sur de mettre un PoP hors de portee de l'enforcement.

Les commandes ne sont jamais construites ici : elles arrivent deja formees dans
le plan, ce qui garantit que ce qui est execute est exactement ce que
l'operateur a vu.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import MissingSecretError, RouterConfig
from app.enforcement.models import Plan, PlanAction

logger = logging.getLogger(__name__)


class RouterOsWriteClient(Protocol):
    """Contrat d'ecriture, volontairement minimal."""

    def execute(self, action: PlanAction) -> dict[str, Any]: ...

    def close(self) -> None: ...


class MissingWriteCredentialsError(RuntimeError):
    """Le routeur n'a pas d'identifiants d'ecriture : il est hors de portee."""


def write_config(config: RouterConfig) -> RouterConfig:
    """Derive la configuration d'ecriture d'un routeur a partir de sa config.

    Refuse explicitement de retomber sur le compte de lecture : melanger les
    deux annulerait la separation des privileges.
    """
    if not config.rw_username:
        raise MissingWriteCredentialsError(
            f"routeur '{config.name}' : aucun compte d'ecriture (rw_username) declare"
        )
    if not config.rw_password_env:
        raise MissingWriteCredentialsError(f"routeur '{config.name}' : rw_password_env manquant")
    clone = config.model_copy(
        update={
            "username": config.rw_username,
            "password": None,
            "password_env": config.rw_password_env,
        }
    )
    try:
        clone.resolve_password()
    except MissingSecretError as exc:
        raise MissingWriteCredentialsError(str(exc)) from exc
    return clone


class LibrouterosWriteClient:
    """Client d'ecriture reel (librouteros, API binaire)."""

    def __init__(self, config: RouterConfig) -> None:
        self._config = write_config(config)
        self._api: Any = None
        self._lock = threading.Lock()

    def _ensure(self) -> Any:
        if self._api is None:
            from librouteros import connect
            from librouteros.login import plain

            kwargs: dict[str, Any] = {
                "host": self._config.host,
                "port": self._config.port,
                "username": self._config.username,
                "password": self._config.resolve_password(),
                "timeout": self._config.timeout_s,
                "login_method": plain,
            }
            if self._config.use_ssl:
                import ssl

                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                kwargs["ssl_wrapper"] = context.wrap_socket
            self._api = connect(**kwargs)
            logger.warning(
                "Connexion en ECRITURE ouverte sur %s (%s) avec le compte %s",
                self._config.name,
                self._config.host,
                self._config.username,
            )
        return self._api

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._api is not None:
            try:
                self._api.close()
            except Exception:  # noqa: BLE001
                pass
            self._api = None

    def execute(self, action: PlanAction) -> dict[str, Any]:
        with self._lock:
            try:
                api = self._ensure()
                chemin = api.path(*[p for p in action.path.split("/") if p])
                if action.verb == "add":
                    resultat = chemin.add(**action.fields)
                    return {"id": str(resultat)}
                if action.verb == "set":
                    chemin.update(**{".id": action.target_id, **action.fields})
                    return {"id": action.target_id}
                if action.verb == "remove":
                    chemin.remove(action.target_id)
                    return {"id": action.target_id}
                raise ValueError(f"verbe inconnu : {action.verb}")
            except Exception:
                self._drop()
                raise


@dataclass(slots=True)
class ActionOutcome:
    action: PlanAction
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class ApplyResult:
    """Compte rendu d'une application, pour l'audit et l'interface."""

    router_name: str
    dry_run: bool
    outcomes: list[ActionOutcome] = field(default_factory=list)
    aborted_reason: str | None = None

    @property
    def applied(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.aborted_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "router": self.router_name,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "applied": self.applied,
            "failed": self.failed,
            "aborted_reason": self.aborted_reason,
            "results": [
                {
                    "command": o.action.command,
                    "summary": o.action.summary(),
                    "ok": o.ok,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


async def apply_plan(
    plan: Plan,
    client: RouterOsWriteClient,
    *,
    dry_run: bool = True,
    stop_on_error: bool = True,
    max_actions: int = 500,
) -> ApplyResult:
    """Execute un plan, action par action, dans l'ordre.

    ``dry_run`` est le defaut : rien n'est envoye tant qu'on ne demande pas
    explicitement le contraire.

    ``max_actions`` est un coupe-circuit : un plan anormalement gros signale
    presque toujours un etat desire mal calcule (inventaire vide, capacite
    perdue), et il vaut mieux s'arreter que de reecrire tout un PoP.
    """
    resultat = ApplyResult(router_name=plan.router_name, dry_run=dry_run)

    if len(plan.actions) > max_actions:
        resultat.aborted_reason = (
            f"{len(plan.actions)} actions depassent la limite de securite "
            f"({max_actions}). Verifiez l'etat desire avant de forcer."
        )
        logger.error("Plan refuse sur %s : %s", plan.router_name, resultat.aborted_reason)
        return resultat

    for action in plan.actions:
        if dry_run:
            resultat.outcomes.append(ActionOutcome(action=action, ok=True, detail="simule"))
            continue
        try:
            retour = await asyncio.to_thread(client.execute, action)
            resultat.outcomes.append(
                ActionOutcome(action=action, ok=True, detail=str(retour.get("id") or ""))
            )
            logger.info("[%s] %s", plan.router_name, action.command)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            resultat.outcomes.append(ActionOutcome(action=action, ok=False, detail=message))
            logger.error("[%s] ECHEC %s -> %s", plan.router_name, action.command, message)
            if stop_on_error:
                resultat.aborted_reason = (
                    "interrompu apres un echec : les actions suivantes dependent "
                    "peut-etre de celle-ci"
                )
                break
    return resultat

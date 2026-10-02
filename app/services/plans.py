"""Plan par client : la facturation (API), la page Plans, sinon le plan par defaut.

LE PLAN EST UNE PROPRIETE DU CLIENT. Un PoP n'est qu'un point de connexion : il
n'a pas de plan, il a une capacite. Trois sources, par ordre :

1. le plan ecrit pour ce client (``client_plans``) -- pousse par l'API
   Preseem ou saisi dans la page Plans ; le dernier ecrit gagne ;
2. a defaut, le plan par defaut (DEFAULT_PLAN_DOWN/UP_MBPS), le meme pour
   tout nouveau client detecte.

Remplace l'ancien fournisseur de demonstration, qui INVENTAIT un plan par login
(500/100, 100/20...) -- d'ou des plans affiches que personne n'avait vendus.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.models import Plan

SOURCE_API = "api"
SOURCE_UI = "ui"
SOURCE_DEFAUT = "default"


def default_plan(settings: Any) -> Plan | None:
    bas = float(getattr(settings, "default_plan_down_mbps", 0) or 0) or None
    haut = float(getattr(settings, "default_plan_up_mbps", 0) or 0) or None
    if bas is None and haut is None:
        return None
    return Plan(down_mbps=bas, up_mbps=haut, source=SOURCE_DEFAUT)


def plan_from_row(row: dict[str, Any], settings: Any) -> Plan | None:
    """Le plan d'une ligne ``client_plans`` ; debits vides = plan par defaut."""
    if row.get("down_mbps") is None and row.get("up_mbps") is None:
        defaut = default_plan(settings)
        if defaut is None:
            return None
        return Plan(defaut.down_mbps, defaut.up_mbps, source=f"{row['source']}:default")
    source = str(row["source"])
    if row.get("service_id"):
        source += ":" + str(row["service_id"])
    return Plan(down_mbps=row.get("down_mbps"), up_mbps=row.get("up_mbps"), source=source)


class ClientPlanProvider:
    """Fournisseur de plans : ce qui a ete ecrit pour le client, sinon le defaut."""

    def __init__(self, repository: Any, settings: Any) -> None:
        self.repository = repository
        self.settings = settings

    async def get_plan(self, login: str) -> Plan | None:
        return (await self.get_plans([login])).get(login)

    async def get_plans(self, logins: Sequence[str]) -> dict[str, Plan]:
        lignes = await self.repository.get_many(list(logins)) if self.repository else {}
        defaut = default_plan(self.settings)
        sortie: dict[str, Plan] = {}
        for login in logins:
            ligne = lignes.get(login)
            plan = plan_from_row(ligne, self.settings) if ligne else defaut
            if plan is not None:
                sortie[login] = plan
        return sortie

    async def aclose(self) -> None:
        return None


async def apply_now(container: Any, login: str, *, author: str) -> dict[str, Any] | None:
    """Le nouveau plan d'un client s'applique TOUT DE SUITE : fiche et file."""
    fournisseur = getattr(container, "plan_provider", None)
    directory = getattr(container, "directory", None)
    if fournisseur is not None and directory is not None:
        plan = await fournisseur.get_plan(login)
        ids = await directory.list_subscriber_logins()
        if plan is not None and login in ids:
            await directory.update_plans({ids[login]: plan})
    try:
        rapport: dict[str, Any] = await container.shaping.enforce_subscriber(
            login=login, author=author
        )
        return rapport
    except Exception as exc:  # noqa: BLE001 - le plan est enregistre quoi qu'il arrive
        return {"state": "erreur", "reason": f"{type(exc).__name__}: {exc}"}

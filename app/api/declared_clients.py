"""Clients DECLARES seulement : son etat, et les sessions non declarees.

Le reglage ``declared_clients_only`` (UN interrupteur, Settings > Clients) fait
qu'un client n'existe que s'il est declare (a la main ou par l'API). Ici :

- ``GET /clients/registry`` : l'etat du mode et ce qu'il change, SANS RIEN
  ECRIRE -- les clients seulement detectes qui ne sont (ou ne seraient) plus
  des clients, et les doublons entre clients declares (meme IP) ;
- ``GET /clients/unlisted-sessions`` : les sessions vues sur les routeurs et
  non declarees (login, IP, MAC, routeur), et si la limite par defaut leur
  est posee.

Allume, le mode vide aussi la liste « Removed » : elle ne sert plus a rien.
Les donnees des clients seulement detectes restent en base, ignorees :
l'eteindre remet tout comme avant.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ContainerDep
from app.services import declared as mode_declares

logger = logging.getLogger(__name__)

router = APIRouter(tags=["clients"])


def _pool(container: Any) -> Any:
    base = getattr(container, "database", None)
    if base is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available"
        )
    return base.pool


async def _seulement_detectes(container: Any) -> list[dict[str, Any]]:
    """Les clients (hors IP fixe) qu'aucune declaration ne couvre."""
    async with _pool(container).acquire() as conn:
        lignes = await conn.fetch(
            f"""
            SELECT s.id, s.login, pop.name AS pop_name, s.last_seen, host(s.last_ip) AS address
              FROM subscribers s
              LEFT JOIN pops pop ON pop.id = s.pop_id
             WHERE NOT {mode_declares.SQL_ABONNE_DECLARE}
             ORDER BY s.login
            """  # noqa: S608 - fragment constant
        )
    return [
        {
            "id": int(r["id"]),
            "login": str(r["login"]),
            "pop_name": r["pop_name"],
            "address": r["address"],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        }
        for r in lignes
    ]


@router.get(
    "/clients/registry",
    summary="Declared clients only: its state, and what turning it on changes (nothing written)",
)
async def declared_only_preview(container: ContainerDep) -> dict[str, Any]:
    detectes = await _seulement_detectes(container)
    retires = getattr(container, "exclusions", None)
    statiques = getattr(container, "static_clients_repo", None)
    fiches = await statiques.list_all() if statiques is not None else []
    declares = await mode_declares.load(_pool(container))
    return {
        "enabled": mode_declares.enabled(container.settings),
        "declared": len(declares.logins),
        # Ne sont (ou ne seraient) plus des clients : ignores, gardes en base.
        "detected": [{k: v for k, v in d.items() if k != "id"} for d in detectes],
        "removed": len(retires.logins) if retires is not None else 0,
        # Le meme client declare deux fois : a corriger avant d'allumer.
        "conflicts": mode_declares.conflicts(fiches),
    }


@router.get(
    "/clients/unlisted-sessions",
    summary="Sessions seen on the routers and not declared; does the default plan limit them?",
)
async def undeclared_sessions(container: ContainerDep) -> dict[str, Any]:
    collection = getattr(container, "collection", None)
    sessions = dict(getattr(collection, "undeclared_sessions", {}) or {})
    settings = container.settings
    bas = float(getattr(settings, "default_plan_down_mbps", 0) or 0)
    haut = float(getattr(settings, "default_plan_up_mbps", 0) or 0)
    limite = (
        mode_declares.enabled(settings)
        and bool(getattr(settings, "default_plan_for_detected_clients", False))
        and bool(bas or haut)
    )
    lignes = []
    for s in sorted(sessions.values(), key=lambda x: str(x.get("login"))):
        porteur = s.get("declared_client")
        lignes.append(
            {
                **s,
                # Elle porte l'IP d'un client declare : c'est lui, sa file a lui.
                "limited": limite and not porteur,
            }
        )
    return {
        "enabled": mode_declares.enabled(settings),
        "limit": {"down_mbps": bas or None, "up_mbps": haut or None} if limite else None,
        "sessions": lignes,
    }

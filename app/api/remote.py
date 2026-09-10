"""Connexion a distance : l'etat, en un coup d'oeil, de toutes les integrations
par lesquelles le controleur parle aux equipements.

Meme esprit que la page de connexion de LibreQoS : une vue unique qui dit, par
famille d'API (RouterOS, airOS Ubiquiti, UISP, RADIUS), ce qui est joignable,
ce qui ne l'est pas, et avec quel protocole. Tout est en LECTURE : cette page
n'ouvre aucune session d'ecriture, elle expose le diagnostic deja tenu par
l'inventaire.

Elle est faite pour grossir : ajouter demain une API d'antenne d'un autre
fabricant, c'est ajouter une famille ici, pas refondre la page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api.deps import ContainerDep

router = APIRouter(tags=["remote"])


def _router_status(row: dict[str, Any]) -> str:
    if row.get("enabled") is False:
        return "disabled"
    if row.get("last_error"):
        return "error"
    if row.get("last_ok_at") or row.get("source") == "file":
        return "ok"
    return "unknown"


async def _routers(container: ContainerDep) -> list[dict[str, Any]]:
    """Routeurs RouterOS : inventaire fichier (registre) + base."""
    devices: list[dict[str, Any]] = []
    registry = container.registry
    for entry in registry.describe():
        if entry["source"] != "file":
            continue
        devices.append(
            {
                "name": entry["name"],
                "host": f"{entry['host']}:{entry['port']}",
                "source": "file",
                "enabled": True,
                "status": "ok",
                "detail": "inventaire fichier (secrets en variables d'environnement)",
                "last_ok_at": None,
            }
        )
    if container.routers_repo is not None:
        for row in await container.routers_repo.list_public():
            modele = row.get("board_name") or ""
            if row.get("routeros_version"):
                modele = f"{modele} · RouterOS {row['routeros_version']}".strip(" ·")
            devices.append(
                {
                    "name": row["name"],
                    "host": f"{row['host']}:{row['port']}",
                    "source": "db",
                    "enabled": row.get("enabled", True),
                    "status": _router_status({**row, "source": "db"}),
                    "detail": row.get("last_error") or modele,
                    "last_ok_at": row.get("last_ok_at"),
                }
            )
    return devices


async def _antennas(container: ContainerDep) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    if container.antennas_repo is None:
        return devices
    for row in await container.antennas_repo.list_public():
        if row.get("enabled") is False:
            status = "disabled"
        elif row.get("last_error"):
            status = "error"
        elif row.get("last_ok_at"):
            status = "ok"
        else:
            status = "unknown"
        capacite = row.get("last_capacity_mbps")
        devices.append(
            {
                "name": row["name"],
                "host": row["host"],
                "source": "db",
                "enabled": row.get("enabled", True),
                "status": status,
                "detail": row.get("last_error")
                or (f"capacite lue {capacite:.0f} Mbps" if capacite is not None else "jamais lue"),
                "last_ok_at": row.get("last_ok_at"),
            }
        )
    return devices


def _summary(devices: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(devices),
        "ok": sum(1 for d in devices if d["status"] == "ok"),
        "error": sum(1 for d in devices if d["status"] == "error"),
    }


@router.get("/remote/status", summary="Etat des connexions distantes (integrations)")
async def remote_status(container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    routeurs = await _routers(container)
    antennes = await _antennas(container)

    integrations: list[dict[str, Any]] = [
        {
            "kind": "routeros",
            "label": "MikroTik RouterOS",
            "transport": "API binaire (8728) / api-ssl (8729), lecture seule",
            "configured": True,
            "summary": _summary(routeurs),
            "devices": routeurs,
        },
        {
            "kind": "airos",
            "label": "Ubiquiti airOS",
            "transport": "HTTP local (status.cgi), direct sur l'antenne",
            "configured": bool(antennes) or settings.backhaul_provider == "airos",
            "summary": _summary(antennes),
            "devices": antennes,
        },
        {
            "kind": "uisp",
            "label": "UISP (capacite backhaul)",
            "transport": "API UISP (HTTPS)",
            "configured": settings.backhaul_provider == "uisp",
            "provider": settings.backhaul_provider,
            "endpoint": settings.uisp_base_url,
            "summary": {"total": 0, "ok": 0, "error": 0},
            "devices": [],
            "note": (
                "Capacite radio lue chez UISP."
                if settings.backhaul_provider == "uisp"
                else f"Fournisseur actuel : {settings.backhaul_provider} "
                "(passez BACKHAUL_PROVIDER=uisp pour lire chez UISP)."
            ),
        },
        {
            "kind": "radius",
            "label": "FreeRADIUS (plans)",
            "transport": "SQL",
            "configured": settings.plan_provider == "freeradius_sql",
            "provider": settings.plan_provider,
            "endpoint": "dsn configure" if settings.radius_dsn else None,
            "summary": {"total": 0, "ok": 0, "error": 0},
            "devices": [],
            "note": (
                "Plans lus dans FreeRADIUS."
                if settings.plan_provider == "freeradius_sql"
                else f"Fournisseur actuel : {settings.plan_provider}."
            ),
        },
    ]
    return {
        "generated_at": datetime.now(tz=UTC),
        "mode": "out-of-band (lecture seule)",
        "integrations": integrations,
    }

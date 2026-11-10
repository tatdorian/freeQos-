"""La sante d'un client, pour sa fiche : une route, cinq reponses simples.

Lecture seule, ouverte a tous les niveaux. Chaque source peut manquer (pas
d'antenne lue, pas de NetFlow, sonde coupee) : sa reponse dit alors
« inconnu » et pourquoi, les autres restent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep
from app.services import client_health as sante

logger = logging.getLogger(__name__)
router = APIRouter(tags=["clients"])


async def _volumes(repo: Any, sid: int, heures: int, pas: int) -> tuple[float, float]:
    """Octets descendus / montes sur la periode, d'apres les debits moyens."""
    fin = datetime.now(tz=UTC)
    points = await repo.subscriber_metrics(
        sid, start=fin - timedelta(hours=heures), end=fin, bucket_seconds=pas
    )
    bas = sum(float(p.get("tx_bps_avg") or 0) * pas / 8 for p in points)
    haut = sum(float(p.get("rx_bps_avg") or 0) * pas / 8 for p in points)
    return bas, haut


async def _cpe_mac(repo: Any, container: Any, sid: int, login: str) -> str | None:
    pool = getattr(repo, "_pool", None)
    if pool is not None:
        async with pool.acquire() as conn:
            mac = await conn.fetchval(
                "SELECT cpe_mac FROM subscriber_attachments WHERE subscriber_id = $1", sid
            )
            if mac:
                return str(mac)
            mac = await conn.fetchval(
                "SELECT cpe_mac FROM static_clients WHERE reference = $1", login
            )
            if mac:
                return str(mac)
    return None


@router.get(
    "/clients/{login}/health", summary="A client's health: experience, radio, site, limit, usage"
)
async def client_health(
    container: ContainerDep,
    collection: CollectionDep,
    repo: RepositoryDep,
    login: Annotated[str, Path(min_length=1, max_length=128)],
) -> dict[str, Any]:
    fiche = await repo.get_subscriber_by_login(login)
    if fiche is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown client '{login}'"
        )
    sid = int(fiche["id"])
    ligne = next(
        (
            r
            for r in await repo.subscriber_latest(search=login, limit=50, include_unmeasured=True)
            if r["login"] == login
        ),
        {},
    )
    sortie: dict[str, Any] = {"login": login}

    # 1. Experience.
    try:
        from app.services.latency_clients import build_rows

        # La derniere heure ; a defaut (sonde espacee), la derniere journee.
        latences: list[dict[str, Any]] = []
        fenetre = 60
        for fenetre in (60, 1440):
            latences = [
                x
                for x in await repo.latency_by_subscriber(minutes=fenetre)
                if int(x["subscriber_id"]) == sid
            ]
            if latences:
                break
        bloat = await repo.bufferbloat(minutes=fenetre, subscriber_id=sid)
        charge = {int(b["subscriber_id"]): b for b in (bloat.get("subscribers") or [])}
        sonde = collection.rtt_prober
        series: dict[int, dict[str, Any]] = {}
        detail = sonde.detail(sid) if sonde is not None else None
        if detail:
            series[sid] = detail
        lignes = build_rows(latences, charge, series)
        sortie["experience"] = sante.experience(lignes[0] if lignes else None)
    except Exception as exc:  # noqa: BLE001 - une source qui manque n'empeche pas les autres
        logger.debug("Experience de %s illisible : %s", login, exc)
        sortie["experience"] = sante.experience(None)

    # 2. Radio.
    try:
        fournisseur = getattr(collection, "antennas_provider", None)
        lecteur = getattr(fournisseur, "radio_snapshot", None)
        antennes = lecteur() if callable(lecteur) else []
        if container.antennas_repo is not None:
            noms = {
                str(f.get("device_key") or f.get("name")): f.get("name")
                for f in await container.antennas_repo.list_public()
            }
            antennes = [{**a, "name": noms.get(a.get("key")) or a.get("key")} for a in antennes]
        sortie["radio"] = sante.radio(
            await _cpe_mac(repo, container, sid, login), antennes, ligne.get("last_ip")
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Radio de %s illisible : %s", login, exc)
        sortie["radio"] = sante.radio(None, [])

    # 3. Site.
    try:
        nom = ligne.get("pop_name")
        pop = (
            next((p for p in await repo.network_tree() if p.get("name") == nom), None)
            if nom
            else None
        )
        sortie["site"] = sante.site(nom, pop)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Site de %s illisible : %s", login, exc)
        sortie["site"] = sante.site(None, None)

    # 4. Limite tenue ?
    try:
        audit = await container.shaping.limit_audit()
        nom_file = container.shaping.queue_name_for(login)
        file = None
        fait = False
        for routeur in audit.get("routers") or []:
            if not routeur.get("pending"):
                fait = True
            for q in routeur.get("queues") or []:
                if q.get("name") == nom_file:
                    file = q
        sortie["limit"] = sante.limite(file, fait)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Limite de %s illisible : %s", login, exc)
        sortie["limit"] = sante.limite(None, False)

    # 5. Usage.
    try:
        jb, jh = await _volumes(repo, sid, 24, 300)
        mb, mh = await _volumes(repo, sid, 24 * 30, 3600)
        apps: list[dict[str, Any]] = []
        flux = getattr(container, "flows_repo", None)
        if flux is not None:
            apps = await flux.applications(minutes=7 * 24 * 60, subscriber_id=sid, limit=6)
        sortie["usage"] = sante.usage(jb, jh, mb, mh, apps)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Usage de %s illisible : %s", login, exc)
        sortie["usage"] = sante.usage(0, 0, 0, 0, [])
    return sortie

"""API de topologie et d'enforcement (phase 2).

Le parcours est volontairement en trois temps :

  GET  /topology            ce que le controleur comprend du reseau
  GET  /shaping/state       ce qui est DEJA configure sur les routeurs
  POST /shaping/plan        ce qu'il faudrait changer, avec les commandes exactes
  POST /shaping/apply       execution, uniquement sur ordre explicite

Aucun endpoint de lecture n'ecrit sur un equipement. Le seul qui le fasse exige
``dry_run=false`` ET ``ENFORCEMENT_ENABLED=true``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import ContainerDep, RepositoryDep
from app.enforcement.routeros import MissingWriteCredentialsError
from app.services.shaping import EnforcementDisabledError, EnforcementLockedError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shaping"])


def _require_topology(container: ContainerDep):
    if container.topology_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Topologie indisponible (base non initialisee)",
        )
    return container.topology_repo


# --------------------------------------------------------------- topologie
@router.get("/topology", summary="Graphe du reseau tel que le controleur le comprend")
async def topology(container: ContainerDep) -> dict[str, Any]:
    repo = _require_topology(container)
    noeuds = await repo.nodes()
    liens = await repo.links()
    return {
        "nodes": noeuds,
        "links": liens,
        "counts": {"nodes": len(noeuds), "links": len(liens)},
        "sources": {
            "neighbors": "/ip/neighbor (MNDP, LLDP, CDP) - adjacence physique",
            "ethernet": "/interface/ethernet - debit negocie du port",
            "addresses": "/ip/address - segment L3 du lien",
            "uisp": "UISP /devices - liens radio et capacite du moment",
            "pppoe": "/ppp/active caller-id - MAC du CPE, rattache l'abonne au secteur",
            "counters": "/interface rx-byte,tx-byte - debit mesure du port qui porte le lien",
        },
    }


@router.post("/topology/discover", summary="Relance la decouverte de topologie")
async def discover(container: ContainerDep) -> dict[str, Any]:
    """Lecture seule sur tous les PoPs, puis persistance du graphe."""
    devices: list[dict[str, Any]] = []
    # Les deux sources de radios : le fournisseur statique (mock/UISP/env-airOS)
    # et les antennes ajoutees depuis l'interface. Leurs fiches se rattachent au
    # graphe par la MAC, exactement de la meme facon.
    for fournisseur in (container.backhaul_provider, container.collection.antennas_provider):
        if fournisseur is not None and hasattr(fournisseur, "raw_devices"):
            try:
                devices.extend(await fournisseur.raw_devices())  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - une source muette n'empeche pas l'autre
                logger.warning("raw_devices indisponible pour %s", type(fournisseur).__name__)

    snapshot = await container.shaping.discover(uisp_devices=devices)
    return {
        "nodes": len(snapshot.nodes),
        "links": len(snapshot.links),
        "warnings": snapshot.warnings,
    }


@router.get("/topology/links/{key:path}/throughput", summary="Debit mesure d'un lien")
async def link_throughput(
    key: str,
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=10080, description="Fenetre d'historique")] = 60,
    bucket_seconds: Annotated[int, Query(ge=5, le=3600, alias="bucket")] = 30,
) -> dict[str, Any]:
    """Le debit d'un lien, maintenant et sur la fenetre demandee.

    La mesure vient des compteurs du PORT qui porte le lien : c'est le seul
    endroit ou RouterOS compte des octets. ``interface_links`` a plus de 1
    signale un port partage par plusieurs voisins, donc un debit cumule.
    """
    repo = _require_topology(container)
    lien = await repo.link(key)
    if lien is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Lien inconnu : {key}")

    series: list[dict[str, Any]] = []
    if lien.get("discovered_by") and lien.get("interface"):
        series = await repo.interface_series(
            router_name=lien["discovered_by"],
            interface=lien["interface"],
            minutes=minutes,
            bucket_seconds=bucket_seconds,
        )

    return {
        "link": lien,
        "series": series,
        "window_minutes": minutes,
        "bucket_seconds": bucket_seconds,
        # Dit noir sur blanc d'ou vient le chiffre, pour qu'un port partage ou
        # un lien radio sans compteur ne passe pas pour une mesure du lien.
        "measurement": _origine_mesure(lien),
    }


@router.get("/topology/links/{key:path}/live", summary="Mesurer ce lien maintenant")
async def link_live(key: str, container: ContainerDep) -> dict[str, Any]:
    """Interroge le routeur pour le debit INSTANTANE du port.

    ``/interface/monitor-traffic`` est une commande de lecture : elle ne change
    rien sur l'equipement. Si elle echoue (version, droits, port sans
    compteur), on retombe sur la derniere mesure collectee en disant pourquoi,
    plutot que de renvoyer une erreur a l'operateur qui voulait juste un chiffre.
    """
    repo = _require_topology(container)
    lien = await repo.link(key)
    if lien is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Lien inconnu : {key}")

    routeur, interface = lien.get("discovered_by"), lien.get("interface")
    if not routeur or not interface:
        return {
            "key": key,
            "source": "aucune",
            "detail": (
                "Ce lien n'est porte par aucun port de routeur "
                "(adjacence declaree par UISP) : il n'y a pas de compteur a lire."
            ),
            "rx_bps": None,
            "tx_bps": None,
        }

    try:
        mesure = await container.collection.measure_link(routeur, interface)
    except KeyError:
        detail = f"Routeur '{routeur}' absent de l'inventaire actif"
    except Exception as exc:  # noqa: BLE001 - on degrade, on ne casse pas
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("Mesure instantanee impossible sur %s/%s : %s", routeur, interface, exc)
    else:
        return {
            "key": key,
            "router_name": routeur,
            "interface": interface,
            "source": "monitor-traffic",
            "measured_at": datetime.now(tz=UTC),
            **mesure,
        }

    return {
        "key": key,
        "router_name": routeur,
        "interface": interface,
        "source": "compteurs",
        "detail": detail,
        "measured_at": lien.get("measured_at"),
        "rx_bps": lien.get("rx_bps"),
        "tx_bps": lien.get("tx_bps"),
    }


def _origine_mesure(lien: dict[str, Any]) -> dict[str, Any]:
    partage = int(lien.get("interface_links") or 0)
    if not lien.get("interface"):
        origine, note = "aucune", "Lien sans port local : aucun compteur d'octets."
    elif partage > 1:
        origine, note = (
            "port-partage",
            f"{partage} voisins sont vus sur {lien['interface']} : "
            "le debit affiche est celui du port, pas celui de ce seul voisin.",
        )
    else:
        origine, note = "port", f"Compteurs de {lien['interface']} sur {lien.get('discovered_by')}."
    return {"source": origine, "interface_links": partage, "note": note}


@router.patch("/topology/nodes/{key:path}", summary="Corriger le role d'un equipement")
async def set_node_kind(
    key: str,
    container: ContainerDep,
    kind: Annotated[
        Literal["gateway", "core", "pop", "radio", "sector", "cpe", "unknown"] | None,
        Query(description="Role force ; omettre pour revenir a la detection"),
    ] = None,
) -> dict[str, Any]:
    """La classification automatique est une heuristique : l'operateur tranche."""
    repo = _require_topology(container)
    await repo.set_node_kind(key, kind)
    return {"key": key, "kind_override": kind}


# ------------------------------------------------------- etat du shaping
@router.get("/shaping/state", summary="Ce qui est deja configure sur les routeurs")
async def shaping_state(
    container: ContainerDep,
    router_name: Annotated[str | None, Query(alias="router")] = None,
) -> list[dict[str, Any]]:
    """Analyse de l'existant, sans rien modifier.

    Distingue explicitement ce qui appartient au controleur de ce qui a ete pose
    par l'operateur ou par RADIUS.
    """
    etats = await container.shaping.inspect(router_name)
    return [etat.to_dict() for etat in etats]


# ---------------------------------------------------------------- politique
def _en_mbps(
    mbps: float | None, kbps: float | None, gbps: float | None, champ: str
) -> float | None:
    """Ramene un debit a l'unite interne unique : le Mbps.

    Melanger les unites en base serait une fabrique a bugs ; on convertit donc a
    l'entree. Fournir deux unites pour le meme champ est ambigu, donc refuse.
    """
    fournis = [(v, u) for v, u in ((mbps, "mbps"), (kbps, "kbps"), (gbps, "gbps")) if v is not None]
    if len(fournis) > 1:
        raise ValueError(
            f"{champ} : une seule unite a la fois "
            f"({', '.join(u for _, u in fournis)} fournis ensemble)"
        )
    if not fournis:
        return None
    valeur, unite = fournis[0]
    if unite == "kbps":
        return valeur / 1000.0
    if unite == "gbps":
        return valeur * 1000.0
    return valeur


class PolicyInput(BaseModel):
    """Surcharge permanente de debit.

    Le debit s'exprime au choix en kbps, Mbps ou Gbps : un lien radio de secours
    ou un abonne bride se comptent souvent en centaines de kilobits, ou saisir
    0.512 Mbps serait absurde.
    """

    scope: Literal["link", "subscriber"]
    target_key: str = Field(min_length=1, max_length=256)
    max_down_mbps: float | None = Field(default=None, ge=0, le=100_000)
    max_up_mbps: float | None = Field(default=None, ge=0, le=100_000)
    max_down_kbps: float | None = Field(default=None, ge=0, le=100_000_000)
    max_up_kbps: float | None = Field(default=None, ge=0, le=100_000_000)
    max_down_gbps: float | None = Field(default=None, ge=0, le=100)
    max_up_gbps: float | None = Field(default=None, ge=0, le=100)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _normaliser(self) -> PolicyInput:
        self.max_down_mbps = _en_mbps(
            self.max_down_mbps, self.max_down_kbps, self.max_down_gbps, "download"
        )
        self.max_up_mbps = _en_mbps(self.max_up_mbps, self.max_up_kbps, self.max_up_gbps, "upload")
        return self


@router.get("/shaping/policies", summary="Surcharges de debit posees a la main")
async def list_policies(
    container: ContainerDep,
    scope: Annotated[Literal["link", "subscriber"] | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await _require_topology(container).policies(scope)


@router.put("/shaping/policies", summary="Fixer le debit d'un lien ou d'un abonne")
async def set_policy(payload: PolicyInput, container: ContainerDep) -> dict[str, Any]:
    """Enregistre la surcharge. N'ecrit RIEN sur le routeur : il faut ensuite
    demander un plan puis l'appliquer."""
    repo = _require_topology(container)
    enregistre = await repo.upsert_policy(
        scope=payload.scope,
        target_key=payload.target_key,
        max_down_mbps=payload.max_down_mbps,
        max_up_mbps=payload.max_up_mbps,
        enabled=payload.enabled,
        note=payload.note,
        updated_by="ui",
    )
    return {
        "policy": enregistre,
        "next_step": "POST /shaping/plan pour voir les commandes qui en decoulent",
    }


@router.delete("/shaping/policies/{scope}/{target_key:path}", summary="Retirer une surcharge")
async def delete_policy(
    scope: Literal["link", "subscriber"], target_key: str, container: ContainerDep
) -> dict[str, Any]:
    supprime = await _require_topology(container).delete_policy(scope, target_key)
    if not supprime:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Surcharge inconnue")
    return {"deleted": True}


# --------------------------------------------------------------------- plan
class PlanRequest(BaseModel):
    router: str = Field(min_length=1)


@router.post("/shaping/plan", summary="Calculer les commandes, sans rien envoyer")
async def build_shaping_plan(
    payload: PlanRequest, container: ContainerDep, metrics: RepositoryDep
) -> dict[str, Any]:
    """Produit le plan : la liste exacte des commandes RouterOS qui seraient
    envoyees, avec pour chacune la raison et ce qui change."""
    try:
        liens, abonnes = await container.shaping.build_targets(payload.router)
        plan = await container.shaping.plan(payload.router, links=liens, subscribers=abonnes)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Lecture du routeur impossible : {type(exc).__name__}: {exc}",
        ) from exc

    orphelins = [a.login for a in abonnes if a.parent is None]
    donnees = plan.to_dict()
    donnees["unparented_subscribers"] = len(orphelins)
    if orphelins:
        donnees["notes"] = [
            f"{len(orphelins)} abonne(s) sans backhaul identifie : leur file est "
            "creee sans parent. Le dernier km est bien shape, mais la contention "
            "sur le backhaul ne l'est pas. Le rattachement vient de la jointure "
            "entre le caller-id PPPoE et les stations UISP."
        ]
    return donnees


class ApplyRequest(BaseModel):
    router: str = Field(min_length=1)
    # Defaut volontairement sur : appliquer pour de vrai doit etre un choix.
    dry_run: bool = True
    confirm: bool = False


@router.post("/shaping/apply", summary="Appliquer un plan (ecriture sur le routeur)")
async def apply_shaping(
    payload: ApplyRequest, container: ContainerDep, metrics: RepositoryDep
) -> dict[str, Any]:
    """Recalcule le plan puis l'execute.

    Le plan est RECALCULE juste avant d'appliquer, volontairement : appliquer un
    plan calcule il y a dix minutes reviendrait a ecrire sur un etat qui a pu
    changer entre-temps.
    """
    if not payload.dry_run and not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Application reelle : 'confirm' doit valoir true",
        )
    try:
        liens, abonnes = await container.shaping.build_targets(payload.router)
        plan = await container.shaping.plan(payload.router, links=liens, subscribers=abonnes)
        resultat = await container.shaping.apply(plan, dry_run=payload.dry_run)
    except EnforcementDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MissingWriteCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{exc}. Declarez rw_username et rw_password_env pour ce routeur, "
                "et creez le compte qos-rw avec policy=read,write,api,test."
            ),
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"plan": plan.to_dict(), "result": resultat.to_dict()}


@router.get("/shaping/capability", summary="Droits reels du compte sur un routeur")
async def write_capability(
    container: ContainerDep, router_name: Annotated[str, Query(alias="router")]
) -> dict[str, Any]:
    """Interroge le routeur : ce compte a-t-il vraiment le droit d'ecrire ?

    Lit /user et /user/group plutot que de se fier a l'inventaire. Un verdict
    ``null`` signifie indeterminable — la commande sera tentee et RouterOS aura
    le dernier mot.
    """
    try:
        verdict = await container.shaping.write_capability(router_name)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return verdict.to_dict()


@router.get("/shaping/audit", summary="Journal des commandes envoyees")
async def audit(
    container: ContainerDep, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[dict[str, Any]]:
    return await _require_topology(container).audit(limit=limit)


# ------------------------------------------------- bascule de l'enforcement
class EnforcementInput(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=300)
    # Activer l'ecriture sur des routeurs de production merite un geste explicite.
    confirm: bool = False


@router.get("/shaping/enforcement", summary="Etat du drapeau d'ecriture")
async def enforcement_state(container: ContainerDep) -> dict[str, Any]:
    detail = None
    if container.topology_repo is not None:
        from app.services.shaping import FLAG_ENFORCEMENT

        detail = await container.topology_repo.flag_detail(FLAG_ENFORCEMENT)
    return {
        "enabled": container.shaping.enforcement_enabled,
        "locked": container.shaping.enforcement_locked,
        "env_default": container.settings.enforcement_enabled,
        "last_change": detail,
    }


@router.put("/shaping/enforcement", summary="Activer ou couper l'ecriture sur les routeurs")
async def set_enforcement(payload: EnforcementInput, container: ContainerDep) -> dict[str, Any]:
    """Bascule sans redemarrage.

    Activer exige une confirmation ; couper n'en demande pas — revenir en
    lecture seule doit toujours etre immediat.
    """
    if payload.enabled and not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Activer l'enforcement autorise l'ecriture sur vos routeurs : "
                "'confirm' doit valoir true."
            ),
        )
    try:
        return await container.shaping.set_enforcement(payload.enabled, reason=payload.reason)
    except EnforcementLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# ------------------------------------------------------------------ boost
class BoostInput(BaseModel):
    """Coup de debit temporaire sur un abonne PPPoE."""

    login: str = Field(min_length=1, max_length=128)
    duration_minutes: int = Field(ge=1, le=60 * 24 * 7)
    down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    down_kbps: float | None = Field(default=None, gt=0, le=100_000_000)
    up_kbps: float | None = Field(default=None, gt=0, le=100_000_000)
    down_gbps: float | None = Field(default=None, gt=0, le=100)
    up_gbps: float | None = Field(default=None, gt=0, le=100)
    # Alternative pratique : multiplier le plan plutot que saisir un debit.
    multiplier: float | None = Field(default=None, gt=1, le=50)
    reason: str | None = Field(default=None, max_length=300)
    apply_now: bool = True

    @model_validator(mode="after")
    def _normaliser(self) -> BoostInput:
        self.down_mbps = _en_mbps(self.down_mbps, self.down_kbps, self.down_gbps, "download")
        self.up_mbps = _en_mbps(self.up_mbps, self.up_kbps, self.up_gbps, "upload")
        return self


@router.get("/shaping/boosts", summary="Boosts en cours")
async def list_boosts(container: ContainerDep) -> list[dict[str, Any]]:
    return await _require_topology(container).active_boosts()


@router.post("/shaping/boosts", summary="Donner un coup de debit temporaire")
async def create_boost(
    payload: BoostInput, container: ContainerDep, metrics: RepositoryDep
) -> dict[str, Any]:
    """Pose un boost puis, si demande, l'applique immediatement.

    Il expire tout seul : un job verifie l'echeance et ramene la file au debit
    normal. Sans cela le boost resterait indefiniment, la file RouterOS ne
    sachant rien de sa duree.
    """
    repo = _require_topology(container)

    abonne = next(
        (
            row
            for row in await metrics.subscriber_latest(limit=5000, order_by="login")
            if row["pppoe_login"] == payload.login
        ),
        None,
    )
    if abonne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Aucune session active pour '{payload.login}'",
        )

    down, up = payload.down_mbps, payload.up_mbps
    if payload.multiplier is not None:
        down = down or (abonne.get("plan_down_mbps") or 0) * payload.multiplier or None
        up = up or (abonne.get("plan_up_mbps") or 0) * payload.multiplier or None
    if down is None and up is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Precisez un debit (down_mbps / down_kbps / down_gbps, idem en "
                "upload) ou un multiplier"
            ),
        )

    expire_le = datetime.now(tz=UTC) + timedelta(minutes=payload.duration_minutes)
    politique = await repo.set_boost(
        scope="subscriber",
        target_key=payload.login,
        down_mbps=down,
        up_mbps=up,
        expires_at=expire_le,
        reason=payload.reason,
        updated_by="ui",
    )

    resultat: dict[str, Any] = {
        "boost": {
            "login": payload.login,
            "down_mbps": politique["boost_down_mbps"],
            "up_mbps": politique["boost_up_mbps"],
            "expires_at": expire_le,
            "duration_minutes": payload.duration_minutes,
        },
        "applied": None,
    }

    if payload.apply_now:
        resultat["applied"] = await _apply_for_subscriber(container, payload.login)
    return resultat


@router.delete("/shaping/boosts/{login}", summary="Retirer un boost avant son echeance")
async def clear_boost(
    login: str, container: ContainerDep, apply_now: Annotated[bool, Query()] = True
) -> dict[str, Any]:
    retire = await _require_topology(container).clear_boost("subscriber", login)
    if not retire:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun boost en cours")
    applique = await _apply_for_subscriber(container, login) if apply_now else None
    return {"cleared": True, "applied": applique}


async def _apply_for_subscriber(container: ContainerDep, login: str) -> dict[str, Any] | None:
    """Applique le plan du routeur qui porte cet abonne, si l'ecriture est permise.

    Ne leve pas : poser un boost doit reussir meme quand l'enforcement est
    coupe. Le retour dit alors pourquoi rien n'a ete pousse.
    """
    routeurs = await container.shaping._routers_for_logins({login})  # noqa: SLF001
    if not routeurs:
        return {"ok": False, "detail": "aucun routeur ne porte cet abonne"}
    if not container.shaping.enforcement_enabled:
        return {
            "ok": False,
            "detail": (
                "enforcement desactive : le boost est enregistre mais rien n'a ete "
                "pousse sur le routeur"
            ),
        }
    try:
        # Sans purge : cette application est automatique, elle n'a pas ete
        # relue. Elle doit poser le nouveau debit de cet abonne, pas decider de
        # supprimer les files des autres.
        plan = await container.shaping.plan_router(routeurs[0], prune=False)
        applique = await container.shaping.apply(plan, dry_run=False)
        return applique.to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

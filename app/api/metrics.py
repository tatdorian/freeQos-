"""Endpoints de lecture des metriques collectees."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep, TimeRangeDep

router = APIRouter(tags=["metrics"])


@router.get("/pops", summary="List of PoPs")
async def list_pops(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.list_pops()


@router.delete("/pops/{pop_id}", summary="Remove a PoP and its data")
async def delete_pop(
    repo: RepositoryDep,
    container: ContainerDep,
    pop_id: Annotated[int, Path(ge=1)],
    confirm: Annotated[bool, Query(description="Required: the deletion is permanent")] = False,
) -> dict[str, Any]:
    """Supprime le PoP, ses abonnes, ses backhauls et leur historique.

    Retirer un routeur de l'inventaire ne suffit pas : ses donnees restent, ce
    qui est voulu. Cet appel est le menage explicite, et il est irreversible.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Permanent deletion of the PoP, its subscribers and all their "
                "measurement history: 'confirm' must be true."
            ),
        )
    try:
        supprime = await repo.delete_pop(pop_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # Le PoP emporte ses abonnes et ses backhauls : les identifiants gardes en
    # cache par la collecte designeraient des lignes disparues, et le lot
    # suivant echouerait en entier sur la cle etrangere.
    container.directory.clear_cache()
    return {"deleted": True, "cascaded": supprime}


@router.get("/subscribers", summary="List of subscribers")
async def list_subscribers(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query(description="Filter by PoP")] = None,
    search: Annotated[str | None, Query(description="Filter on the subscriber identifier")] = None,
    kind: Annotated[
        Literal["pppoe", "static"] | None,
        Query(description="Filter by kind: PPPoE subscriber or static-IP client"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await repo.list_subscribers(
        pop_id=pop_id, search=search, kind=kind, limit=limit, offset=offset
    )


@router.get("/subscribers/latest", summary="The subscribers of a PoP and their last sample")
async def subscribers_latest(
    repo: RepositoryDep,
    container: ContainerDep,
    pop_id: Annotated[int | None, Query()] = None,
    search: Annotated[str | None, Query(description="Filter on the subscriber identifier")] = None,
    kind: Annotated[
        Literal["pppoe", "static"] | None,
        Query(description="Filter by kind: PPPoE subscriber or static-IP client"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    order_by: Annotated[Literal["total", "down", "up", "login"], Query()] = "total",
    include_unmeasured: Annotated[
        bool,
        Query(
            description=(
                "Inclure les abonnes declares qui n'ont AUCUNE mesure : jamais "
                "connectes, PoP plus collecte, ou simplement hors ligne depuis "
                "l'origine. Leurs debits sortent a null, jamais a zero."
            )
        ),
    ] = False,
) -> list[dict[str, Any]]:
    """L'effectif d'un PoP, avec la derniere mesure de chacun quand elle existe.

    Par defaut, seuls les abonnes MESURES sont rendus : c'est ce que demande un
    classement par debit. ``include_unmeasured=true`` rend tout l'effectif --
    la liste des abonnes qu'il y a sur le PoP, y compris ceux dont on n'a
    encore rien vu passer. Un abonne facture qui n'apparait nulle part est
    indiscernable d'un abonne qui n'existe pas.
    """
    lignes = await repo.subscriber_latest(
        pop_id=pop_id,
        search=search,
        kind=kind,
        limit=limit,
        order_by=order_by,
        include_unmeasured=include_unmeasured,
    )
    # La SERIE derriere la latence affichee : mediane, extremes, gigue, perte,
    # age de la mesure. Un chiffre seul ne dit pas s'il est stable.
    collection = getattr(container, "collection", None)
    sonde = getattr(collection, "rtt_prober", None) if collection is not None else None
    if sonde is not None:
        for ligne in lignes:
            ligne["rtt_detail"] = sonde.detail(int(ligne["subscriber_id"]))
    return lignes


@router.delete("/subscribers/{subscriber_id}", summary="Delete a subscriber and its history")
async def delete_subscriber(
    repo: RepositoryDep,
    container: ContainerDep,
    subscriber_id: Annotated[int, Path(ge=1)],
    confirm: Annotated[bool, Query(description="Required: the deletion is permanent")] = False,
) -> dict[str, Any]:
    """Supprime l'abonne, son historique, et tout ce qui le ferait revenir.

    - un client A IP FIXE est retire de l'inventaire et sa file du routeur :
      sinon il serait recree au cycle suivant, puisque la fiche fait foi ;
    - ses surcharges de debit et son boost sont retires (et le routeur remis
      d'accord), pour ne pas laisser un plafond orphelin s'appliquer un jour a
      un nouvel abonne du meme login ;
    - le cache de la collecte l'oublie.

    UN ABONNE PPPoE ENCORE CONNECTE REVIENDRA : il est decouvert dans
    ``/ppp/active``, que ce controleur ne fait que lire. La reponse le dit
    (``will_reappear``) ; pour qu'il disparaisse, fermer sa session ou retirer
    son compte PPPoE sur le routeur / RADIUS.
    """
    import logging

    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Permanent deletion of the subscriber and its history: 'confirm' must be true.",
        )
    fiche = await repo.get_subscriber(subscriber_id)
    if fiche is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
    login = str(fiche["login"])
    rapport: dict[str, Any] = {"login": login, "kind": fiche.get("kind")}

    # 1. Client statique : la fiche d'inventaire et sa file.
    statiques = container.static_clients_repo
    if fiche.get("kind") == "static" and statiques is not None:
        from app.api.static_clients import _poser_la_file

        for ligne in await statiques.list_all():
            if str(ligne.get("reference")) == login:
                await statiques.delete(int(ligne["id"]))
                rapport["static_client_removed"] = True
                rapport["queue"] = await _poser_la_file(
                    container,
                    reference=login,
                    pop_name=str(ligne.get("pop_name") or ""),
                    removing=True,
                )
                break

    # 2. Surcharge et boost : retires, et le routeur remis d'accord.
    topo = container.topology_repo
    if topo is not None:
        surcharge = await topo.delete_policy("subscriber", login)
        boost = await topo.clear_boost("subscriber", login)
        rapport["override_removed"] = surcharge
        rapport["boost_removed"] = boost
        if surcharge and fiche.get("kind") != "static":
            try:
                rapport["cap"] = await container.shaping.enforce_policy(
                    "subscriber", login, author="ui:delete", removing=True
                )
            except Exception as exc:  # noqa: BLE001 - la suppression reste valable
                logging.getLogger(__name__).warning("Plafond de %s non retire : %s", login, exc)
                rapport["cap"] = {"state": "erreur", "reason": str(exc)}

    # 3. L'abonne et son historique.
    try:
        supprime = await repo.delete_subscriber(subscriber_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    rapport["samples_deleted"] = int(supprime.get("samples") or 0)

    # 4. La collecte l'oublie.
    collection = getattr(container, "collection", None)
    if collection is not None:
        collection.directory.forget_subscriber(login)
        getattr(collection, "_known_logins", set()).discard(login)

    dernier = supprime.get("last_sample")
    actif = (
        fiche.get("kind") != "static"
        and dernier is not None
        and (datetime.now(tz=UTC) - dernier).total_seconds() < 120
    )
    rapport["will_reappear"] = bool(actif)
    return rapport


@router.get("/collection/cycles", summary="State of the measurement cycles")
async def collection_cycles(collection: CollectionDep) -> dict[str, Any]:
    """Les derniers cycles de mesure : ok ou non, il y a combien, et l'erreur.

    En tete de la liste des abonnes : un debit absent doit se lire "rien ne
    passe" OU "la mesure est en panne", jamais l'un pour l'autre.
    """
    from app.services.collection import JOB_LINKS, JOB_SUBSCRIBERS

    maintenant = datetime.now(tz=UTC)
    sortie: dict[str, dict[str, Any] | None] = {}
    for job in (JOB_SUBSCRIBERS, JOB_LINKS):
        resultat = collection.last_results.get(job)
        sortie[job] = (
            None
            if resultat is None
            else {
                "ok": resultat.ok,
                "age_s": round((maintenant - resultat.started_at).total_seconds(), 1),
                "duration_s": round(resultat.duration_s, 2),
                "items": resultat.items,
                "errors": list(resultat.errors)[:5],
            }
        )
    return {"cycles": sortie}


@router.get("/subscribers/{subscriber_id}/live", summary="Live check of one subscriber")
async def subscriber_live(
    repo: RepositoryDep,
    container: ContainerDep,
    collection: CollectionDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    """CE QUE LE ROUTEUR VOIT DE CET ABONNE MAINTENANT, face a ce qui est affiche.

    Lit en direct, sur deux secondes, l'interface PPPoE de l'abonne et sa file ;
    y ajoute le debit NetFlow et le dernier echantillon enregistre ; et rend un
    verdict qui dit OU le debit se perd, s'il se perd.
    """
    from app.services.pop_match import resolve_pop

    fiche = await repo.get_subscriber(subscriber_id)
    if fiche is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
    login = str(fiche["login"])
    match = resolve_pop(str(fiche.get("pop_name") or ""), collection.collectors)
    candidats = list(match.collectors) or list(collection.collectors)

    live: dict[str, Any] | None = None
    erreurs: list[str] = []
    for collector in candidats:
        try:
            lu = await collector.live_subscriber(login, fiche.get("last_ip"))
        except Exception as exc:  # noqa: BLE001 - un routeur muet n'arrete pas les autres
            erreurs.append(f"{collector.name}: {type(exc).__name__}: {exc}")
            continue
        if lu["session"] or lu["queues"] or live is None:
            live = lu
        if lu["session"]:
            break

    netflow = None
    service = container.netflow
    if service is not None and service.measuring:
        rx, tx = service.rate_for(subscriber_id)
        netflow = {"rx_bps": rx, "tx_bps": tx}

    enregistre = None
    for ligne in await repo.subscriber_latest(search=login, limit=20):
        if str(ligne.get("login")) == login:
            enregistre = {
                "ts": ligne.get("ts"),
                "tx_bps": ligne.get("tx_bps"),
                "rx_bps": ligne.get("rx_bps"),
                "age_s": round((datetime.now(tz=UTC) - ligne["ts"]).total_seconds(), 1)
                if ligne.get("ts")
                else None,
            }
            break

    return {
        "login": login,
        "kind": fiche.get("kind"),
        "live": live,
        "netflow": netflow,
        "stored": enregistre,
        "errors": erreurs,
        "verdict": live_verdict(fiche.get("kind"), live, netflow, enregistre, erreurs),
    }


def live_verdict(
    kind: Any,
    live: dict[str, Any] | None,
    netflow: dict[str, Any] | None,
    stored: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, str]:
    """Une phrase, et une seule, sur OU se perd le debit."""
    seuil = 10_000.0  # 10 kbps : en dessous, des keepalives

    def fort(*valeurs: Any) -> bool:
        return any(v is not None and float(v) >= seuil for v in valeurs)

    if live is None:
        return {"level": "crit", "text": "The router could not be read: " + "; ".join(errors)}
    if kind != "static" and live["session"] is None:
        return {
            "level": "warn",
            "text": "No open PPPoE session for this login on " + str(live["router"]) + ".",
        }
    if kind != "static" and not live["interface_found"]:
        return {
            "level": "crit",
            "text": "The session is open but its interface was not found on the router: "
            "check PPPOE_INTERFACE_PATTERN.",
        }
    vu_routeur = fort(live.get("interface_tx_bps"), live.get("interface_rx_bps"))
    vu_netflow = netflow is not None and fort(netflow.get("tx_bps"), netflow.get("rx_bps"))
    perime = stored is None or stored.get("age_s") is None or stored["age_s"] > 90
    if vu_routeur and perime:
        return {
            "level": "crit",
            "text": "The router counts traffic but nothing recent is stored: the "
            "Subscribers cycle is failing (see its error at the top of the list).",
        }
    if vu_routeur:
        return {"level": "ok", "text": "The router counts this traffic and it is stored."}
    if vu_netflow:
        return {
            "level": "warn",
            "text": "NetFlow sees traffic for this subscriber but its PPPoE interface "
            "counts none: the traffic does not go through its session (another path, "
            "or fasttrack on a bridge).",
        }
    return {"level": "ok", "text": "No traffic right now: the session is idle."}


@router.get("/subscribers/{subscriber_id}", summary="Record of one subscriber")
async def get_subscriber(
    repo: RepositoryDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
    return subscriber


@router.get("/subscribers/{subscriber_id}/metrics", summary="Throughput series of a subscriber")
async def subscriber_metrics(
    repo: RepositoryDep,
    window: TimeRangeDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
    points = await repo.subscriber_metrics(
        subscriber_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
    )
    # Bufferbloat de CET abonne sur la meme fenetre : latence a vide vs sous
    # charge. Calcule sur les echantillons bruts, pas sur les points agreges,
    # pour que la note reste juste quel que soit le bucket demande.
    minutes = max(5, round((window.end - window.start).total_seconds() / 60))
    bloat = await repo.bufferbloat(minutes=minutes, subscriber_id=subscriber_id)
    return {
        "subscriber": subscriber,
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        # Rappel de convention : rx = upload abonne, tx = download abonne.
        "orientation": "rx=subscriber upload, tx=subscriber download (router point of view)",
        "points": points,
        "bufferbloat": bloat["subscribers"][0] if bloat["subscribers"] else None,
    }


@router.get("/bufferbloat", summary="Bufferbloat grade (latency under load) per subscriber")
async def bufferbloat(
    repo: RepositoryDep,
    collection: CollectionDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24 * 7, description="Observation window")] = 60,
    pop_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Le bufferbloat est la latence AJOUTEE quand le lien se remplit.

    Il se lit en correlant RTT et debit deja collectes : rien de nouveau a
    mesurer, juste a rapprocher. Un abonne sans charge sur la fenetre reste sans
    note (compte dans ``indeterminate``) plutot que d'en recevoir une flatteuse.

    LA REPONSE DIT SI LA SONDE TOURNE. Sans RTT, cette note ne peut pas exister :
    la moitie de la correlation manque. La sonde etant coupee par defaut (elle
    coute du CPU aux routeurs), un tableau vide etait indiscernable d'un reseau
    parfaitement sain -- c'est le pire des deux messages possibles. On l'annonce
    donc explicitement plutot que de laisser deviner.
    """
    resultat = await repo.bufferbloat(minutes=minutes, pop_id=pop_id)
    resultat["rtt_enabled"] = collection.rtt_enabled
    if not collection.rtt_enabled:
        resultat["unavailable_reason"] = (
            "The latency probe is off: without RTT, bufferbloat and the QoE score "
            "cannot be computed. Turn it on in the Executive tab "
            "('RTT probe' box), or via PUT /api/v1/rtt."
        )
    return resultat


@router.get("/heatmap", summary="Executive heatmap: QoE / RTT / utilisation over time")
async def heatmap(
    repo: RepositoryDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24, description="Observation window")] = 15,
    buckets: Annotated[int, Query(ge=5, le=120, description="Number of columns")] = 20,
) -> dict[str, Any]:
    """Bandes de cellules colorees facon LibreQoS. La ligne des retransmissions
    TCP est presente mais marquee indisponible : hors-bande, on ne l'invente pas."""
    return await repo.heatmap(minutes=minutes, buckets=buckets)


@router.get("/backhauls", summary="List of radio backhauls")
async def list_backhauls(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.list_backhauls(pop_id=pop_id)


@router.get("/backhauls/latest", summary="Last known capacity per backhaul")
async def backhauls_latest(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.backhaul_latest(pop_id=pop_id)


@router.get("/backhauls/{backhaul_id}/metrics", summary="Capacity series of a backhaul")
async def backhaul_metrics(
    repo: RepositoryDep,
    window: TimeRangeDep,
    backhaul_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    points = await repo.backhaul_metrics(
        backhaul_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
    )
    return {
        "backhaul_id": backhaul_id,
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        "points": points,
    }


# --------------------------------------------------------------------------
# Vues d'ensemble consommees par le tableau de bord
# --------------------------------------------------------------------------


@router.get("/overview", summary="Headline figures of the dashboard")
async def overview(repo: RepositoryDep) -> dict[str, Any]:
    return await repo.overview()


@router.get("/throughput", summary="Aggregate network throughput over time")
async def throughput(
    repo: RepositoryDep,
    window: TimeRangeDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    points = await repo.throughput_series(
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
        pop_id=pop_id,
    )
    return {
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        "orientation": "rx=subscribers upload, tx=subscribers download (router point of view)",
        "points": points,
    }


@router.get("/network/tree", summary="PoP tree -> backhauls, capacity and load")
async def network_tree(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.network_tree()


def name_ports(ports: list[dict[str, Any]], routers: list[str]) -> dict[str, str | None]:
    """Marque le port amont de chaque routeur et nomme le voisin de chaque port.

    La decouverte ne relie pas tous les ports a un lien de l'arbre. La route par
    defaut et la table d'adresses suffisent pourtant a dire ou va un port :
    vers la passerelle (le port amont), ou vers le routeur dont CE port porte
    la passerelle (gw ether2 -> core). "-" laissait croire a un port isole.
    Rend l'interface amont de chaque routeur.
    """
    from app.collectors.mikrotik import port_owning, router_owning, upstream_of

    passerelles = {nom: upstream_of(nom) for nom in routers}
    amonts = {nom: amont[1] for nom, amont in passerelles.items()}
    en_aval: dict[tuple[str, str | None], str] = {}
    for nom, (passerelle, _iface) in passerelles.items():
        porteur = port_owning(passerelle)
        if porteur is not None:
            en_aval.setdefault(porteur, nom)
    for port in ports:
        routeur = port["router_name"]
        port["upstream"] = bool(amonts.get(routeur)) and amonts.get(routeur) == port["interface"]
        if port.get("link_name"):
            continue
        if port["upstream"]:
            passerelle = passerelles.get(routeur, (None, None))[0]
            # Passerelle hors de nos routeurs : c'est la sortie de l'operateur.
            port["link_name"] = router_owning(passerelle) or (
                f"Internet ({passerelle})" if passerelle else "Upstream"
            )
        else:
            port["link_name"] = en_aval.get((routeur, port["interface"]))
    return amonts


@router.get("/ports/live", summary="Every router port with its current throughput")
async def ports_live(repo: RepositoryDep, collection: CollectionDep) -> dict[str, Any]:
    """OU PASSE LE TRAFIC EN CE MOMENT, port par port, tous routeurs confondus.

    La courbe du reseau additionne les SESSIONS D'ABONNES. Un trafic qui n'en
    traverse aucune -- un test de debit lance depuis un CPE ou entre deux
    routeurs, la gestion d'un equipement -- n'y figure pas, alors que les ports
    le comptent. Cette vue les montre tous, que la decouverte les ait relies a
    un lien de l'arbre ou non, avec l'etat des cycles de collecte : un chiffre
    absent se lit alors "rien ne passe" OU "la mesure est en panne", jamais
    l'un pour l'autre.
    """
    from app.services.collection import JOB_LINKS, JOB_RTT, JOB_SUBSCRIBERS

    ports = await repo.ports_live()
    amonts = name_ports(ports, [c.name for c in collection.collectors])
    maintenant = datetime.now(tz=UTC)
    cycles: dict[str, dict[str, Any] | None] = {}
    for job in (JOB_SUBSCRIBERS, JOB_LINKS, JOB_RTT):
        resultat = collection.last_results.get(job)
        if resultat is None:
            cycles[job] = None
            continue
        cycles[job] = {
            "ok": resultat.ok,
            "age_s": round((maintenant - resultat.started_at).total_seconds(), 1),
            "duration_s": round(resultat.duration_s, 2),
            "items": resultat.items,
            "errors": list(resultat.errors)[:5],
        }
    return {
        "ports": ports,
        "upstream": amonts,
        "cycles": cycles,
        "routers": [c.name for c in collection.collectors],
    }


@router.get("/search", summary="Instant lookup: subscriber, IP, MAC, device, site")
async def search(
    repo: RepositoryDep,
    collection: CollectionDep,
    q: Annotated[str, Query(min_length=2, max_length=128)],
) -> dict[str, Any]:
    """Un seul champ pour tout retrouver : le support tape ce qu'il a sous les yeux."""
    resultats = await repo.search_everything(q)
    motif = q.strip().lower()
    resultats["routers"] = [
        {
            "name": c.name,
            "host": c.config.host,
            "pop_name": c.config.effective_pop_name,
            "role": str(c.config.role),
        }
        for c in collection.collectors
        if motif in c.name.lower()
        or motif in str(c.config.host).lower()
        or motif in c.config.effective_pop_name.lower()
    ][:8]
    return {"q": q, **resultats}


@router.get("/insights/subscribers", summary="Churn risk and upgrade candidates")
async def subscriber_insights(
    repo: RepositoryDep,
    days: Annotated[int, Query(ge=1, le=30)] = 7,
) -> dict[str, Any]:
    """Plan, usage et experience de chaque abonne, cote a cote, et deux listes :
    ceux qui risquent de partir, ceux qui sont a l'etroit dans leur offre."""
    from app.services.insights import classify, summarise

    lignes = await repo.subscriber_trends(days=days)
    qoe: dict[int, dict[str, Any]] = {}
    try:
        bloat = await repo.bufferbloat(minutes=min(days, 7) * 1440)
        for b in bloat.get("subscribers") or []:
            if b.get("qoe"):
                qoe[int(b["subscriber_id"])] = b["qoe"] | {"grade": b.get("grade")}
    except Exception:  # noqa: BLE001 - sans QoE, les autres signes restent
        qoe = {}
    resultat = [classify(r, qoe.get(int(r["subscriber_id"]))) for r in lignes]
    ordre = {"at_risk": 0, "upgrade": 1, "healthy": 2}
    resultat.sort(key=lambda x: (ordre[x["status"]], -(x["avg_down_bps"] or 0)))
    return {"days": days, "summary": summarise(resultat), "subscribers": resultat}


@router.get("/insights/capacity", summary="How many more subscribers each site can take")
async def capacity_insights(
    repo: RepositoryDep,
    collection: CollectionDep,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    """Par site (PoP, VLAN) : capacite mesuree, pointe reelle, experience des
    abonnes, et la place restante AVANT que la QoE ne souffre."""
    from app.services.insights import ap_room

    sites = await repo.capacity_by_pop(hours=hours)
    # Sans antenne ni debit pose sur le lien, le PORT AMONT du routeur du site
    # borne quand meme ce qu'il peut porter : "?" partout alors que le port a
    # 1 Gbps est mesure n'aidait personne.
    from app.collectors.mikrotik import upstream_of

    ports = {(str(p["router_name"]), str(p["interface"])): p for p in await repo.ports_live()}
    port_amont: dict[str, tuple[float, str]] = {}
    for c in collection.collectors:
        sortie = upstream_of(c.name)[1]
        port = ports.get((c.name, str(sortie))) if sortie else None
        if port and port.get("capacity_mbps"):
            port_amont.setdefault(
                c.config.effective_pop_name,
                (float(port["capacity_mbps"]), f"uplink port {c.name} {sortie}"),
            )
    mauvais: dict[str, list[bool]] = {}
    try:
        bloat = await repo.bufferbloat(minutes=min(hours, 168) * 60)
        for b in bloat.get("subscribers") or []:
            if b.get("qoe") and b.get("pop_name"):
                mauvais.setdefault(str(b["pop_name"]), []).append(float(b["qoe"]["score"]) < 50)
    except Exception:  # noqa: BLE001
        mauvais = {}
    lignes = []
    for site in sites:
        base_capacite = None
        if not site.get("capacity_mbps") and site.get("pop_name") in port_amont:
            site = {**site, "capacity_mbps": port_amont[str(site["pop_name"])][0]}
            base_capacite = port_amont[str(site["pop_name"])][1]
        # Un site sans abonne et sans capacite connue (le coeur, la passerelle)
        # n'est pas un site d'acces : il n'y a pas de place a y compter.
        if not int(site.get("subscribers") or 0) and not site.get("capacity_mbps"):
            continue
        notes = mauvais.get(str(site.get("pop_name")), [])
        part = (sum(notes) / len(notes)) if notes else None
        lignes.append(
            {
                "pop_name": site.get("pop_name"),
                "subscribers": int(site.get("subscribers") or 0),
                "capacity_mbps": site.get("capacity_mbps"),
                "peak_mbps": round(float(site["peak_bps"]) / 1e6, 1)
                if site.get("peak_bps")
                else None,
                "sold_down_mbps": site.get("sold_down_mbps"),
                "poor_share": round(part, 2) if part is not None else None,
                "qoe_measured": len(notes),
                **ap_room(
                    capacity_mbps=site.get("capacity_mbps"),
                    peak_bps=site.get("peak_bps"),
                    subscribers=int(site.get("subscribers") or 0),
                    poor_share=part,
                ),
            }
        )
        if base_capacite:
            lignes[-1]["capacity_source"] = base_capacite
            lignes[-1]["reason"] += f" (capacity: {base_capacite})"
    lignes.sort(key=lambda x: (x["room"] is None, x["room"] if x["room"] is not None else 0))
    return {"hours": hours, "sites": lignes}

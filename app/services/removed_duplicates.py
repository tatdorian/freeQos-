"""Clients RETIRES qui sont en fait des clients DECLARES : plus de doublon.

LE BESOIN. La liste « Removed » se remplit de clients retires parce qu'ils
faisaient double emploi avec un client declare (ajoute a la main, pousse par
l'API, ou qui a un forfait) : la session PPPoE d'un client deja declare avec son
IP fixe, un ancien nom du meme client... Ils encombrent la liste alors que le
client existe deja, une fois, sous son nom declare.

CE QUI EST COMPARE, pour chaque client retire :

- son NOM : le meme qu'un client declare ou qui a un forfait (casse, espaces,
  tirets et points ignores) ;
- son IP et sa MAC, quand il est connecte : freeQoS n'en garde rien en base
  (un client retire est oublie), mais le releve voit encore sa session ; l'IP
  dans le reseau d'un client declare (adresse ou plage), l'IP d'un client qui a
  un forfait, ou la MAC du CPE d'un client declare.

CE QUI EST FAIT :

- MEME LOGIN, EXACTEMENT, qu'un client declare : le retrait cachait le client
  declare lui-meme (meme identifiant) -- sans file, invisible. La declaration
  gagne : le retrait est leve ;
- SINON (meme IP, meme MAC, nom equivalent) : c'est le meme client sous un
  autre identifiant. Il sort de la liste « Removed » -- le client declare suffit
  -- mais reste ignore : leve, il reviendrait comme second client, avec une
  seconde file sur la meme adresse.

Un client declare qui disparait rend son doublon a la liste « Removed ».
Un client retire hors ligne ne montre pas son IP : il est reconnu des qu'il se
connecte (le travail tourne chaque minute).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

RAISON_NOM = "same name"
RAISON_IP = "same IP"
RAISON_MAC = "same MAC"


def _cle_nom(valeur: Any) -> str:
    """Un nom compare sans casse, espaces, tirets, points ni soulignes."""
    return re.sub(r"[\s\-_.]+", "", str(valeur or "")).lower()


def _mac(valeur: Any) -> str:
    return re.sub(r"[^0-9a-f]", "", str(valeur or "").lower())


def _reseaux(adresses: list[Any]) -> list[Any]:
    reseaux = []
    for adresse in adresses:
        try:
            reseaux.append(ipaddress.ip_network(str(adresse).strip(), strict=False))
        except ValueError:
            continue
    return reseaux


def _ip(valeur: Any) -> Any:
    try:
        return ipaddress.ip_address(str(valeur or "").split("/")[0].strip())
    except ValueError:
        return None


def _a_un_forfait(ligne: dict[str, Any]) -> bool:
    source = str(ligne.get("plan_source") or "")
    if source == "default" or source.endswith(":default"):
        return False
    return bool(ligne.get("plan_down_mbps") or ligne.get("plan_up_mbps"))


async def _declares(container: Any) -> list[dict[str, Any]]:
    """Les clients declares (fiches) et ceux qui ont un forfait, comparables."""
    sortie: list[dict[str, Any]] = []
    statiques = getattr(container, "static_clients_repo", None)
    if statiques is not None:
        for fiche in await statiques.list_all():
            reference = str(fiche.get("reference") or "")
            if not reference:
                continue
            sortie.append(
                {
                    "name": reference,
                    "exact": {reference},
                    "names": {_cle_nom(reference), _cle_nom(fiche.get("label"))} - {""},
                    "networks": _reseaux(
                        [fiche.get("address"), *(fiche.get("extra_prefixes") or [])]
                    ),
                    "mac": _mac(fiche.get("cpe_mac")),
                }
            )
    metriques = getattr(container, "repository", None)
    if metriques is not None:
        lignes: list[dict[str, Any]] = await metriques.subscriber_latest(
            limit=5000, order_by="login", include_unmeasured=True
        )
        for ligne in lignes:
            if ligne.get("kind") == "static" or not _a_un_forfait(ligne):
                continue
            login = str(ligne.get("login") or "")
            ip = _ip(ligne.get("last_ip"))
            sortie.append(
                {
                    "name": login,
                    "exact": set(),
                    "names": {_cle_nom(login), _cle_nom(ligne.get("display_name"))} - {""},
                    "networks": _reseaux([f"{ip}/{ip.max_prefixlen}"]) if ip else [],
                    "mac": "",
                }
            )
    return sortie


def match(
    login: str, session: dict[str, Any] | None, declares: list[dict[str, Any]]
) -> tuple[dict[str, Any], str] | None:
    """Le client declare que ce client retire EST, et pourquoi -- ou None."""
    cle = _cle_nom(login)
    ip = _ip((session or {}).get("address"))
    mac = _mac((session or {}).get("caller_id"))
    for declare in declares:
        if declare["name"] == login:
            continue
        if login in declare["exact"] or (cle and cle in declare["names"]):
            return declare, RAISON_NOM
    for declare in declares:
        if declare["name"] == login:
            continue
        if ip is not None and any(ip.version == r.version and ip in r for r in declare["networks"]):
            return declare, RAISON_IP
        if mac and len(mac) == 12 and mac == declare["mac"]:
            return declare, RAISON_MAC
    return None


async def resolve(container: Any) -> dict[str, Any]:
    """Range les clients retires qui sont des clients declares. Rend le bilan."""
    retires = getattr(container, "exclusions", None)
    if retires is None:
        return {"restored": [], "same_as": [], "back": []}
    lignes = await retires.list()
    if not lignes:
        return {"restored": [], "same_as": [], "back": []}
    declares = await _declares(container)
    exacts = {d["name"] for d in declares if d["exact"]}
    collection = getattr(container, "collection", None)
    vues: dict[str, dict[str, Any]] = dict(getattr(collection, "removed_sessions", {}) or {})

    leves: list[str] = []
    memes: list[dict[str, str]] = []
    rendus: list[str] = []
    for ligne in lignes:
        login = str(ligne["login"])
        if login in exacts:
            # Le retrait cachait le client declare lui-meme : il est leve.
            await retires.remove(login)
            leves.append(login)
            logger.info("Client declare %s n'est plus retire (meme identifiant)", login)
            continue
        trouve = match(login, vues.get(login), declares)
        if trouve is not None:
            declare, raison = trouve
            if ligne.get("same_as") != declare["name"] or ligne.get("same_reason") != raison:
                await retires.mark_same_as(login, declare["name"], raison)
                logger.info(
                    "Client retire %s = client declare %s (%s)", login, declare["name"], raison
                )
            memes.append({"login": login, "same_as": declare["name"], "reason": raison})
        elif ligne.get("same_as") and not any(d["name"] == ligne["same_as"] for d in declares):
            # Le client declare a disparu : le retire revient dans la liste.
            await retires.mark_same_as(login, None, None)
            rendus.append(login)
        elif ligne.get("same_as"):
            memes.append(
                {
                    "login": login,
                    "same_as": str(ligne["same_as"]),
                    "reason": str(ligne.get("same_reason") or ""),
                }
            )
    return {"restored": leves, "same_as": memes, "back": rendus}

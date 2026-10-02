"""Services pousses sans adresse : places des que leur MAC est vue.

Et services pousses AVANT que leur routeur soit connu : rattaches au PoP du
routeur qui porte leur IP des qu'il l'est.

Preseem accepte un service designe par la seule MAC de son CPE (ou sans aucun
attachement) : il le rattache quand il voit passer ce materiel. Ici, la MAC est
cherchee dans les tables ARP et DHCP des routeurs ; trouvee, elle donne l'IP,
et le service devient un client a part entiere -- place, bride, visible.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

JOB_PLACE_SERVICES = "place_unplaced_services"


def _mac(texte: Any) -> str:
    return str(texte or "").strip().upper().replace("-", ":")


def ip_by_mac(rows: list[dict[str, Any]]) -> dict[str, str]:
    """``MAC -> IP`` d'apres des lignes ARP (``mac-address``) ou DHCP."""
    sortie: dict[str, str] = {}
    for row in rows:
        mac = _mac(row.get("mac-address") or row.get("active-mac-address"))
        ip = str(row.get("address") or row.get("active-address") or "").strip()
        if mac and ip and "/" not in ip:
            sortie.setdefault(mac, ip)
    return sortie


async def read_macs(collectors: list[Any]) -> dict[str, str]:
    """Toutes les MAC vues par les routeurs, un routeur muet n'arrete rien."""
    table: dict[str, str] = {}
    for collecteur in collectors:
        client = getattr(collecteur, "_mesure", None) or getattr(collecteur, "_client", None)
        if client is None:
            continue
        for lecteur in ("dhcp_leases", "arp"):
            lire = getattr(client, lecteur, None)
            if lire is None:
                continue
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(lire), timeout=20.0)
            except Exception as exc:  # noqa: BLE001
                logger.info("%s illisible sur %s : %s", lecteur, collecteur.name, exc)
                continue
            for mac, ip in ip_by_mac(list(rows or [])).items():
                table.setdefault(mac, ip)
    return table


async def _rattacher_au_pop(container: Any, repo: Any) -> int:
    """Un service pousse avant que son routeur soit decouvert est reste au PoP
    par defaut : il rejoint le PoP du routeur qui porte son IP, des qu'il est
    connu."""
    fait = 0
    for service in await repo.services_without_pop():
        try:
            lieu = await container.shaping.locate_address(service.get("address"))
        except Exception:  # noqa: BLE001
            lieu = None
        if lieu and lieu.get("pop_name"):
            await repo.set_service_pop(str(service["reference"]), str(lieu["pop_name"]))
            fait += 1
    return fait


async def place_unplaced(container: Any) -> int:
    """Place les services en attente dont la MAC est vue. Rend le nombre place."""
    repo = getattr(container, "model_repo", None)
    if repo is None:
        return 0
    places = await _rattacher_au_pop(container, repo)
    attente = [s for s in await repo.unplaced_services() if s.get("cpe_mac")]
    if not attente:
        return 0
    table = await read_macs(list(container.registry.collectors))
    for service in attente:
        ip = table.get(_mac(service["cpe_mac"]))
        if ip is None:
            continue
        payload = dict(service["payload"])
        payload["attachments"] = [{"cpe_mac": service["cpe_mac"], "network_prefixes": [ip]}]
        try:
            lieu = await container.shaping.locate_address(ip)
            if lieu and lieu.get("pop_name"):
                payload["pop_name"] = lieu["pop_name"]
            fiche = await repo.put_service(str(service["id"]), payload)
            await container.shaping.enforce_static_client(
                reference=str(fiche["id"]),
                pop_name=str(fiche.get("pop_name") or ""),
                author="api:model",
                address=ip,
            )
            places += 1
            logger.info("Service %s place sur %s (MAC %s)", service["id"], ip, service["cpe_mac"])
        except Exception as exc:  # noqa: BLE001 - le suivant ne doit pas en patir
            logger.warning("Service %s non place : %s", service["id"], exc)
    return places

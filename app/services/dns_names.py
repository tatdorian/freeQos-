"""Le nom que le client a DEMANDE, lu dans le cache DNS des routeurs.

NetFlow ne transporte que des adresses. "cluster100.hosting.ovh.net" (le nom
inverse) ou "ovh.net" (l'hebergeur) disent OU est le serveur, pas CE QUE le
client a voulu joindre -- syit.fr, en l'occurrence. Ce nom n'existe que dans
la requete DNS, qu'aucun outil hors-bande ne voit passer.

Sauf le routeur : quand les clients resolvent par lui (profil PPP ou DHCP qui
donne le routeur comme DNS, et allow-remote-requests), il garde chaque reponse
en cache. On la relit ici et on remonte les CNAME jusqu'au nom tape
(www.netflix.com -> cdn -> adresse). Un client qui interroge 8.8.8.8 en direct
contourne le routeur : pour lui, il n'y a rien a lire, et on ne l'invente pas.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

JOB_DNS_NAMES = "dns_names"
#: Au-dela, un nom n'est plus attribue : l'adresse a pu changer de proprietaire.
RETENTION_S = 86_400.0
MAX_ENTREES = 100_000


def _type(row: dict[str, Any]) -> str:
    return str(row.get("type") or "A").upper()


def _valeur(row: dict[str, Any]) -> str:
    return str(row.get("data") or row.get("address") or "").strip().rstrip(".")


def names_from_cache(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Adresse -> nom demande, CNAME remontes jusqu'a leur origine."""
    alias_de: dict[str, str] = {}  # cible CNAME -> nom qui y menait
    adresses: list[tuple[str, str]] = []
    for row in rows:
        nom = str(row.get("name") or "").strip().rstrip(".").lower()
        valeur = _valeur(row)
        if not nom or not valeur:
            continue
        genre = _type(row)
        if genre == "CNAME":
            alias_de.setdefault(valeur.lower(), nom)
        elif genre in ("A", "AAAA"):
            try:
                adresses.append((str(ipaddress.ip_address(valeur)), nom))
            except ValueError:
                continue
    sortie: dict[str, str] = {}
    for adresse, nom in adresses:
        origine, vus = nom, {nom}
        while origine in alias_de and alias_de[origine] not in vus:
            origine = alias_de[origine]
            vus.add(origine)
        sortie.setdefault(adresse, origine)
    return sortie


@dataclass
class DnsNames:
    names: dict[str, tuple[str, float]] = field(default_factory=dict)
    last_error: str | None = None
    routers_read: int = 0

    def name_for(self, address: str | None) -> str | None:
        if not address:
            return None
        trouve = self.names.get(str(address))
        if trouve is None or time.monotonic() - trouve[1] > RETENTION_S:
            return None
        return trouve[0]

    async def refresh(self, collectors: list[Any]) -> int:
        """Relit le cache DNS de chaque routeur ; un routeur muet n'arrete pas les autres."""
        lus = 0
        maintenant = time.monotonic()
        for collecteur in collectors:
            client = getattr(collecteur, "_mesure", None) or getattr(collecteur, "_client", None)
            lire = getattr(client, "dns_cache", None)
            if lire is None:
                continue
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(lire), timeout=20.0)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{getattr(collecteur, 'name', '?')}: {type(exc).__name__}: {exc}"
                continue
            lus += 1
            for adresse, nom in names_from_cache(rows).items():
                self.names[adresse] = (nom, maintenant)
        if len(self.names) > MAX_ENTREES:
            for adresse, _ in sorted(self.names.items(), key=lambda kv: kv[1][1])[
                : len(self.names) - MAX_ENTREES
            ]:
                del self.names[adresse]
        self.routers_read = lus
        return len(self.names)

    def annotate(self, rows: list[dict[str, Any]], key: str = "address") -> list[dict[str, Any]]:
        for row in rows:
            nom = self.name_for(row.get(key))
            if nom and not row.get("internal"):
                row["domain"] = nom
        return rows

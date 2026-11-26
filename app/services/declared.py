"""Mode « clients declares seulement » : un client n'existe que s'il est DECLARE.

LE BESOIN. Les clients etaient decouverts tout seuls : chaque session PPPoE vue
sur un routeur devenait un client. Un client declare a la main sur l'IP d'une
session apparaissait donc en double, et la liste « Removed » servait a chasser
les sessions qu'on ne voulait pas. Dans ce mode, la seule facon d'avoir un
client est de le DECLARER :

- a la main : client a IP fixe (fiche), login PPPoE declare avant sa session ;
- par l'API : service ou forfait pousse ;
- ou un client deja suivi par l'exploitant : un nom donne, un mode ou une
  limite poses a la main (un client fusionne dans sa session garde son nom).

UNE SESSION NON DECLAREE n'est pas un client : ni fiche, ni mesure, ni file --
sauf si la limite par defaut est etendue aux sessions non declarees (Settings >
Default plan) : elle recoit alors SA file a ce debit, sans devenir un client,
et la liste de ces sessions est affichee. Une session qui porte l'IP d'un
client declare a IP fixe est ce client : elle ne cree rien de plus.

Desactive par defaut (``declared_clients_only``) : rien ne change tant que
l'exploitant ne l'allume pas.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

#: Logins declares : fiche a IP fixe, forfait ecrit, nom donne ou declare a la
#: main, mode / limite poses a la main.
SQL_LOGINS_DECLARES = """
    SELECT reference AS login FROM static_clients
    UNION SELECT login FROM client_plans
    UNION SELECT login FROM client_profiles WHERE declared OR display_name IS NOT NULL
    UNION SELECT target_key FROM shaping_policies WHERE scope = 'subscriber'
"""

#: La meme regle, en condition sur un abonne ``s`` (requetes de liste).
SQL_ABONNE_DECLARE = """
    (s.kind = 'static'
     OR EXISTS (SELECT 1 FROM client_plans cp WHERE cp.login = s.login)
     OR EXISTS (SELECT 1 FROM client_profiles pr
                 WHERE pr.login = s.login AND (pr.declared OR pr.display_name IS NOT NULL))
     OR EXISTS (SELECT 1 FROM shaping_policies sp
                 WHERE sp.scope = 'subscriber' AND sp.target_key = s.login))
"""


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "declared_clients_only", False))


@dataclass
class Declared:
    """Ce qui est declare, lu en une fois : logins et reseaux des fiches a IP fixe."""

    logins: set[str] = field(default_factory=set)
    networks: list[tuple[Any, str]] = field(default_factory=list)

    def carrier_of(self, address: Any) -> str | None:
        """Le client declare a IP fixe dont le reseau contient cette adresse."""
        try:
            ip = ipaddress.ip_address(str(address or "").split("/")[0].strip())
        except ValueError:
            return None
        for reseau, reference in self.networks:
            if ip.version == reseau.version and ip in reseau:
                return reference
        return None

    def covers(self, login: str, address: Any) -> bool:
        return login in self.logins or self.carrier_of(address) is not None


def _reseaux(adresses: list[Any]) -> list[Any]:
    sortie = []
    for adresse in adresses:
        try:
            sortie.append(ipaddress.ip_network(str(adresse).strip(), strict=False))
        except ValueError:
            continue
    return sortie


async def load(pool: Any) -> Declared:
    """Lit les clients declares."""
    import json

    async with pool.acquire() as conn:
        logins = {str(r["login"]) for r in await conn.fetch(SQL_LOGINS_DECLARES)}
        fiches = await conn.fetch(
            "SELECT reference, host(address) || '/' || masklen(address) AS address, "
            "extra_prefixes FROM static_clients WHERE enabled"
        )
    reseaux: list[tuple[Any, str]] = []
    for fiche in fiches:
        plages = fiche["extra_prefixes"]
        if isinstance(plages, str):
            try:
                plages = json.loads(plages)
            except ValueError:
                plages = []
        for reseau in _reseaux([fiche["address"], *(plages or [])]):
            reseaux.append((reseau, str(fiche["reference"])))
    # Le plus precis d'abord : une /32 l'emporte sur le /24 qui la contient.
    reseaux.sort(key=lambda r: r[0].prefixlen, reverse=True)
    return Declared(logins=logins, networks=reseaux)


def conflicts(fiches: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Deux clients declares a IP fixe sur la meme adresse (ou des plages qui
    se recouvrent) : un doublon a corriger avant de ne garder que les declares."""
    reseaux: list[tuple[Any, str]] = []
    for fiche in fiches:
        if fiche.get("enabled") is False:
            continue
        for reseau in _reseaux([fiche.get("address"), *(fiche.get("extra_prefixes") or [])]):
            reseaux.append((reseau, str(fiche.get("reference"))))
    sortie: list[dict[str, str]] = []
    vus: set[tuple[str, str]] = set()
    for i, (a, ref_a) in enumerate(reseaux):
        for b, ref_b in reseaux[i + 1 :]:
            if ref_a == ref_b or a.version != b.version or not a.overlaps(b):
                continue
            paire = (min(ref_a, ref_b), max(ref_a, ref_b))
            if paire in vus:
                continue
            vus.add(paire)
            sortie.append({"client": paire[0], "other": paire[1], "address": str(a)})
    return sortie


def overlapping(
    address: Any, extra: Any, fiches: list[dict[str, Any]], *, ignore: str | None = None
) -> dict[str, str] | None:
    """Le client declare a IP fixe dont une adresse recouvre celles saisies."""
    from app.db.static_clients_repo import split_address

    principale, plages = split_address(address, extra)
    saisis = _reseaux([principale, *plages])
    for fiche in fiches:
        reference = str(fiche.get("reference") or "")
        if not reference or reference == ignore or fiche.get("enabled") is False:
            continue
        for reseau in _reseaux([fiche.get("address"), *(fiche.get("extra_prefixes") or [])]):
            for saisi in saisis:
                if saisi.version == reseau.version and saisi.overlaps(reseau):
                    return {"client": reference, "address": str(reseau)}
    return None

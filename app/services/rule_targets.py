"""Ce qu'une regle vise, tel qu'on l'ecrit : des adresses, des blocs, des plages,
des noms de domaine.

ADRESSES. Une regle accepte une adresse seule (``203.0.113.7``), un bloc
(``203.0.113.0/24``) ou une plage (``203.0.113.10-203.0.113.50``, decoupee en
blocs exacts). Tout le reste est refuse a la saisie, avec la ligne fautive :
une entree que le routeur rejetterait une heure plus tard ne sert a personne.

NOMS DE DOMAINE. ``example.com`` bloque les adresses du nom ET de ses
sous-domaines. Elles viennent de deux sources, et aucune ne suffit seule :

- la RESOLUTION DNS faite par le serveur (``example.com`` et ``www.``), refaite
  a chaque passage de la reconciliation ;
- le CACHE DNS DES ROUTEURS (``dns_names``) : les adresses que les clients ont
  reellement obtenues pour ``video.example.com``, ``cdn.example.com``... C'est
  la seule facon de connaitre les sous-domaines sans les enumerer.

Les adresses d'un nom CHANGENT (CDN, repartition de charge) : celles deja vues
restent dans la liste pendant ``KEEP_S`` apres leur derniere apparition. Sans
cette memoire, une adresse sortie de la reponse DNS un instant serait retiree
puis reposee a chaque passage, et le client aurait sa fenetre pour passer.

Seul l'IPv4 est pose : les listes d'adresses ``/ip/firewall`` sont IPv4.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Une adresse vue pour un nom reste visee une semaine apres sa derniere apparition.
KEEP_S = 7 * 86_400.0
#: Plafond par nom : un CDN peut repondre des centaines d'adresses.
MAX_PER_DOMAIN = 1_000
RESOLVE_TIMEOUT_S = 5.0

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class InvalidTargetError(ValueError):
    """Une adresse ou un nom saisi qui ne veut rien dire."""


def parse_address(texte: str) -> list[str]:
    """Une entree saisie -> les blocs IPv4 qu'elle designe.

    ``203.0.113.7`` -> ``["203.0.113.7/32"]`` ; un bloc reste un bloc ; une plage
    est decoupee au plus juste. Leve ``InvalidTargetError`` sinon.
    """
    brut = str(texte).strip()
    if not brut:
        return []
    if "-" in brut and "/" not in brut:
        debut_txt, _, fin_txt = brut.partition("-")
        try:
            debut = ipaddress.ip_address(debut_txt.strip())
            fin = ipaddress.ip_address(fin_txt.strip())
        except ValueError as exc:
            raise InvalidTargetError(f"'{brut}' is not a range like 10.0.0.10-10.0.0.50") from exc
        if debut.version != 4 or fin.version != 4:
            raise InvalidTargetError(f"'{brut}': only IPv4 ranges can be blocked on the routers")
        if int(fin) < int(debut):
            raise InvalidTargetError(f"'{brut}': the range ends before it starts")
        return [str(b) for b in ipaddress.summarize_address_range(debut, fin)]
    try:
        reseau = ipaddress.ip_network(brut, strict=False)
    except ValueError as exc:
        raise InvalidTargetError(
            f"'{brut}' is not an IP address, a block (a.b.c.d/nn) or a range (a-b)"
        ) from exc
    if reseau.version != 4:
        raise InvalidTargetError(f"'{brut}': only IPv4 can be blocked on the routers")
    if reseau.prefixlen == 0:
        raise InvalidTargetError(f"'{brut}' is the whole internet")
    return [str(reseau)]


def expand_addresses(valeurs: list[str]) -> list[str]:
    """Toutes les entrees d'une regle en blocs ; les fautives sont ignorees
    (elles ont ete refusees a la saisie : une ancienne regle ne casse rien)."""
    sortie: list[str] = []
    for valeur in valeurs:
        try:
            sortie.extend(parse_address(valeur))
        except InvalidTargetError:
            continue
    return sortie


def invalid_addresses(valeurs: list[str]) -> list[str]:
    erreurs: list[str] = []
    for valeur in valeurs:
        try:
            parse_address(valeur)
        except InvalidTargetError as exc:
            erreurs.append(str(exc))
    return erreurs


def normalise_domain(texte: str) -> str:
    """``https://www.Example.com/page`` -> ``www.example.com``.

    Le schema, le chemin, le port et un ``*.`` de tete sont retires : on colle
    souvent une URL ou un joker, et l'intention est claire. Une adresse IP n'est
    pas un nom : elle va dans les adresses.
    """
    brut = str(texte).strip().lower()
    brut = re.sub(r"^[a-z][a-z0-9+.-]*://", "", brut)
    brut = brut.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    brut = brut.rsplit("@", 1)[-1]
    if brut.count(":") == 1:
        brut = brut.split(":", 1)[0]
    brut = brut.lstrip("*").strip(".")
    if not brut:
        raise InvalidTargetError(f"'{texte}' is not a domain name")
    try:
        ipaddress.ip_address(brut)
    except ValueError:
        pass
    else:
        raise InvalidTargetError(f"'{texte}' is an IP address: put it in the addresses")
    try:
        brut = brut.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidTargetError(f"'{texte}' is not a valid domain name") from exc
    etiquettes = brut.split(".")
    if len(etiquettes) < 2 or len(brut) > 253 or not all(_LABEL.match(e) for e in etiquettes):
        raise InvalidTargetError(f"'{texte}' is not a domain name like example.com")
    if etiquettes[-1].isdigit():
        raise InvalidTargetError(f"'{texte}' is not a domain name like example.com")
    return brut


def normalise_domains(valeurs: list[str]) -> list[str]:
    return list(dict.fromkeys(normalise_domain(v) for v in valeurs if str(v).strip()))


def couvre(domaine: str, nom: str) -> bool:
    """``example.com`` couvre ``example.com`` et ``video.example.com``, pas
    ``notexample.com``."""
    nom = nom.lower().rstrip(".")
    return nom == domaine or nom.endswith("." + domaine)


async def _resoudre(nom: str) -> set[str]:
    boucle = asyncio.get_running_loop()
    try:
        reponses = await asyncio.wait_for(
            boucle.getaddrinfo(nom, None, family=socket.AF_INET, type=socket.SOCK_STREAM),
            timeout=RESOLVE_TIMEOUT_S,
        )
    except (OSError, TimeoutError, UnicodeError):
        return set()
    return {str(r[4][0]) for r in reponses}


@dataclass
class DomainResolver:
    """Adresses de chaque nom de domaine vise, avec leur memoire."""

    dns_names: Any | None = None
    #: domaine -> {adresse: derniere fois vue (monotone)}
    seen: dict[str, dict[str, float]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    resolved_at: dict[str, float] = field(default_factory=dict)

    async def resolve(self, domaines: list[str]) -> dict[str, list[str]]:
        maintenant = time.monotonic()
        sortie: dict[str, list[str]] = {}
        for domaine in dict.fromkeys(domaines):
            trouvees: set[str] = set()
            for nom in (domaine, "www." + domaine):
                trouvees |= await _resoudre(nom)
            trouvees |= self._depuis_routeurs(domaine)
            memoire = self.seen.setdefault(domaine, {})
            for adresse in trouvees:
                memoire[adresse] = maintenant
            for adresse, vu in list(memoire.items()):
                if maintenant - vu > KEEP_S:
                    del memoire[adresse]
            if len(memoire) > MAX_PER_DOMAIN:
                for adresse, _ in sorted(memoire.items(), key=lambda kv: kv[1])[
                    : len(memoire) - MAX_PER_DOMAIN
                ]:
                    del memoire[adresse]
            if memoire:
                self.errors.pop(domaine, None)
            else:
                self.errors[domaine] = "the name does not resolve (no IPv4 address)"
            self.resolved_at[domaine] = maintenant
            sortie[domaine] = sorted(memoire, key=lambda a: ipaddress.IPv4Address(a))
        return sortie

    def _depuis_routeurs(self, domaine: str) -> set[str]:
        noms = getattr(self.dns_names, "names", None) or {}
        sortie: set[str] = set()
        for adresse, (nom, _vu) in list(noms.items()):
            if not couvre(domaine, nom):
                continue
            try:
                if ipaddress.ip_address(adresse).version == 4:
                    sortie.add(adresse)
            except ValueError:
                continue
        return sortie

    def known(self, domaine: str) -> dict[str, Any]:
        """Ce qu'on sait d'un nom sans le resoudre a nouveau (pour la liste)."""
        return {
            "domain": domaine,
            "addresses": len(self.seen.get(domaine) or {}),
            "error": self.errors.get(domaine),
        }

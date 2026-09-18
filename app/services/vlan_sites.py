"""Un VLAN qui porte des clients EST un site.

POURQUOI. Chez un operateur radio, un VLAN ne decoupe pas un reseau au hasard :
il porte un village, un relais, une zone. Le routeur, lui, n'en est que la tete.
Tant que le controleur ne connaissait que le site du ROUTEUR, tous les clients
de tous les VLAN d'un meme NAS se retrouvaient dans un seul sac : impossible de
filtrer "les abonnes de Francophonie", impossible de dire lequel des sites
sature, impossible de lire l'arbre. Le decoupage existait sur le terrain et dans
la configuration ; il manquait seulement dans le referentiel.

CE QUI DEVIENT UN SITE, ET CE QUI N'EN DEVIENT PAS. Uniquement un VLAN sur
lequel au moins un client est declare ou vu. Un VLAN de gestion, de transit ou
de supervision ne porte pas d'abonne : en faire un site remplirait la liste des
PoP de lignes vides, et une liste de sites ou l'on ne reconnait plus ses sites
ne sert plus a rien.

CE MODULE EST PUR. Des noms d'interfaces entrent, des sites sortent. Aucune
base, aucun routeur : c'est ce qui permet de le couvrir cas par cas, y compris
les conventions de nommage bizarres qu'on trouve toujours dans un parc reel.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# Comment un site issu d'un VLAN se distingue d'un site de routeur, partout ou
# la distinction compte (interface, API, tests).
SITE_ROUTEUR = "router"
SITE_VLAN = "vlan"

# Les ecritures de VLAN qu'on rencontre reellement sur RouterOS :
#   vlan101, vlan-101, vlan_101       -> numero seul
#   vlan-francophonie, vlan_mairie    -> nom de site
#   ether1.101, sfp-sfpplus1.230      -> sous-interface 802.1Q
#   francophonie-vlan                 -> le suffixe plutot que le prefixe
_PREFIXE_VLAN = re.compile(r"^vlan[\s._-]*", re.IGNORECASE)
_SUFFIXE_VLAN = re.compile(r"[\s._-]*vlan$", re.IGNORECASE)
_SOUS_INTERFACE = re.compile(r"^(?P<port>[A-Za-z][\w-]*)\.(?P<tag>\d{1,4})$")
_NUMERIQUE = re.compile(r"^\d{1,4}$")


def _titre(texte: str) -> str:
    """Met un nom d'interface en forme de nom de site.

    'francophonie' -> 'Francophonie', 'zone-nord' -> 'Zone Nord'. On ne touche
    pas a ce qui est deja en majuscules (un sigle reste un sigle : 'ZTE', 'CCR').
    """
    mots = [m for m in re.split(r"[\s._-]+", texte) if m]
    return " ".join(m if m.isupper() else m.capitalize() for m in mots)


def site_name(vlan_interface: str | None, vlan_id: int | None = None) -> str | None:
    """Le nom de site que porte cette interface VLAN, ou None si elle n'en dit rien.

    LE NOM VIENT DE L'INTERFACE, PAS DU NUMERO. C'est celui que l'exploitant a
    lui-meme ecrit sur son routeur : c'est donc celui qu'il reconnaitra dans une
    liste de PoP. Un VLAN qui n'a qu'un numero garde son numero -- inventer un
    nom serait pire que de ne rien dire.
    """
    brut = (vlan_interface or "").strip()
    if not brut:
        return f"VLAN {vlan_id}" if vlan_id is not None else None

    # Sous-interface 802.1Q : le nom utile est le port, pas le tag, mais un port
    # ('ether1') ne designe pas un site. On s'en tient alors au numero.
    sous = _SOUS_INTERFACE.match(brut)
    if sous is not None:
        return f"VLAN {int(sous.group('tag'))}"

    noyau = _SUFFIXE_VLAN.sub("", _PREFIXE_VLAN.sub("", brut)).strip(" ._-")
    if not noyau:
        return f"VLAN {vlan_id}" if vlan_id is not None else None
    if _NUMERIQUE.match(noyau):
        return f"VLAN {int(noyau)}"
    return _titre(noyau)


def site_key(texte: str) -> str:
    """Forme comparable d'un nom de site : sans accent, sans casse, sans ponctuation.

    Publique parce que le shaping compare des noms de sites venus de deux
    sources (le referentiel et la configuration des routeurs) : deux
    canonisations differentes finiraient par ranger le meme site dans deux
    cases, et un abonne cesserait d'etre shape sans que rien ne le dise.
    """
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "", sans_accent.lower())


# Nom court, pour les appels internes de ce module.
_cle = site_key


@dataclass(slots=True, frozen=True)
class VlanSite:
    """Un VLAN reconnu comme site, et le routeur qui le dessert.

    ``router_name`` est ce qui rend ce site utilisable par le shaping : un site
    qui ne dirait pas quel routeur le porte serait un site dont les abonnes ne
    seraient jamais shapes -- exactement le defaut qu'on repare.
    """

    name: str
    router_name: str
    vlan_interface: str
    vlan_id: int | None = None
    subscribers: int = 0

    @property
    def key(self) -> str:
        return _cle(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "router_name": self.router_name,
            "vlan_interface": self.vlan_interface,
            "vlan_id": self.vlan_id,
            "subscribers": self.subscribers,
            "kind": SITE_VLAN,
        }


def _porteur(objet: Any, *noms: str) -> Any:
    """Lit un champ sur un dataclass comme sur un dict : les deux circulent ici."""
    for nom in noms:
        if isinstance(objet, dict):
            if objet.get(nom) is not None:
                return objet[nom]
        else:
            valeur = getattr(objet, nom, None)
            if valeur is not None:
                return valeur
    return None


def sites_from(sightings: Iterable[Any] = (), static_clients: Iterable[Any] = ()) -> list[VlanSite]:
    """Les VLAN qui portent au moins un client, donc les sites a declarer.

    DEUX SOURCES, VOLONTAIREMENT. Les observations ARP disent ce qui parle
    reellement sur un VLAN ; l'inventaire dit ce que l'exploitant a vendu. Un
    client declare mais silencieux doit quand meme faire exister son site --
    sinon le site apparaitrait et disparaitrait au gre des coupures, et une
    liste de PoP qui clignote ne se lit pas.

    Un client declare ne porte pas toujours le nom de son interface VLAN (sa
    fiche n'a qu'un numero) : il est alors rattache au site que les
    observations ont nomme pour ce meme numero, et seulement a defaut a
    'VLAN <n>'.
    """
    par_cle: dict[tuple[str, str], VlanSite] = {}
    # numero de VLAN -> interface nommee vue sur le terrain. Sert a donner son
    # vrai nom au site d'un client declare qui n'a qu'un numero.
    interface_par_tag: dict[tuple[str, int], str] = {}

    observations = list(sightings)
    for vue in observations:
        routeur = _porteur(vue, "router_name")
        interface = _porteur(vue, "vlan_interface")
        tag = _porteur(vue, "vlan_id")
        if not routeur or not interface:
            continue
        if tag is not None and site_name(str(interface)) is not None:
            interface_par_tag.setdefault((str(routeur), int(tag)), str(interface))

    def ajouter(routeur: str, interface: str, tag: int | None) -> None:
        nom = site_name(interface, tag)
        if nom is None:
            return
        cle = (routeur, _cle(nom))
        existant = par_cle.get(cle)
        if existant is None:
            par_cle[cle] = VlanSite(
                name=nom,
                router_name=routeur,
                vlan_interface=interface,
                vlan_id=tag,
                subscribers=1,
            )
            return
        par_cle[cle] = VlanSite(
            name=existant.name,
            router_name=existant.router_name,
            vlan_interface=existant.vlan_interface,
            # Le numero finit toujours par etre connu : une observation le
            # porte meme quand la fiche du client ne l'a pas.
            vlan_id=existant.vlan_id if existant.vlan_id is not None else tag,
            subscribers=existant.subscribers + 1,
        )

    for vue in observations:
        routeur = _porteur(vue, "router_name")
        interface = _porteur(vue, "vlan_interface")
        if not routeur or not interface:
            continue
        tag = _porteur(vue, "vlan_id")
        ajouter(str(routeur), str(interface), int(tag) if tag is not None else None)

    for client in static_clients:
        tag = _porteur(client, "vlan")
        routeur = _porteur(client, "router_name")
        if tag is None or not routeur:
            continue
        interface = interface_par_tag.get((str(routeur), int(tag))) or f"vlan{int(tag)}"
        ajouter(str(routeur), interface, int(tag))

    return sorted(par_cle.values(), key=lambda s: (s.router_name, s.name))


def routers_for_site(nom: str, sites: Sequence[VlanSite]) -> list[str]:
    """Quels routeurs desservent ce site. Vide si ce n'est pas un site de VLAN.

    C'est par cette fonction que le shaping retrouve le routeur d'un abonne
    range dans un site de VLAN. Sans elle, le rapprochement retomberait sur une
    comparaison de noms avec les PoP des routeurs -- qui echouerait, et
    l'abonne cesserait silencieusement d'etre shape.
    """
    cible = _cle(nom or "")
    if not cible:
        return []
    retenus: list[str] = []
    for site in sites:
        if site.key == cible and site.router_name not in retenus:
            retenus.append(site.router_name)
    return retenus

"""Qui se cache derriere une adresse atteinte par un client.

CE QUE CE MODULE REPOND, ET CE QU'IL NE REPONDRA JAMAIS
-------------------------------------------------------
NetFlow dit "10.20.0.10 a echange 4 Go avec 45.57.12.34". Seul, ce chiffre ne
sert a rien : personne ne sait de tete a qui appartient 45.57.12.34. Ce module
met un NOM sur l'adresse -- Netflix, YouTube, Twitch, un CDN, un fournisseur de
nuage -- pour que la question "qui fait du streaming sur ce secteur" ait une
reponse.

Il ne fait PAS d'inspection de contenu. Le trafic est chiffre, il le reste : on
ne regarde que l'adresse, son nom inverse et, optionnellement, ce que le
registre en dit. Trois sources, par ordre de confiance decroissante :

1. LE CATALOGUE (ci-dessous). Des prefixes publies par les operateurs
   eux-memes. C'est la seule source qui fonctionne sans acces internet, et la
   seule qui donne une reponse instantanee : un controleur sur une VM de
   management coupee du monde doit quand meme savoir reconnaitre Netflix.
2. LE NOM INVERSE (PTR). ``ipv4-c001-par001.1.oca.nflxvideo.net`` ne laisse
   aucun doute, et c'est ce qui permet de suivre un service QUI CHANGE DE
   PREFIXE sans que personne ne mette le catalogue a jour. Une seule requete
   DNS par adresse nouvelle, mise en cache ensuite.
3. RDAP (registre). Donne l'organisation, le numero d'AS, le pays et le bloc
   annonce. COUPE PAR DEFAUT : c'est le seul appel sortant du controleur, et un
   reseau souverain a le droit de ne pas en vouloir.

L'HONNETETE DU VERDICT EST PLUS IMPORTANTE QUE SA PRECISION
------------------------------------------------------------
Un CDN (Cloudflare, Akamai, Fastly) sert indifferemment un site de recettes, un
catalogue video et une mise a jour systeme : reconnaitre "Akamai" n'autorise
donc PAS a conclure "streaming". Ces services portent la categorie ``cdn`` et
non ``streaming``, precisement pour qu'une restriction posee dessus soit un
choix conscient et non une surprise. Le champ ``source`` dit d'ou vient le
verdict -- bloc publie, nom inverse, registre -- et l'interface l'affiche a cote
du nom : un exploitant qui va bloquer un trafic a le droit de savoir a quel
titre on l'a nomme.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# --- Categories ---------------------------------------------------------------
#
# Volontairement peu nombreuses : ce sont les familles sur lesquelles un
# exploitant pose reellement une regle. Multiplier les nuances donnerait une
# taxonomie plus jolie et des restrictions plus difficiles a ecrire.
CAT_STREAMING = "streaming"
CAT_SOCIAL = "social networks"
CAT_JEUX = "gaming"
CAT_VISIO = "voice / video"
CAT_CDN = "cdn"
CAT_NUAGE = "cloud"
CAT_SYSTEME = "updates"
CAT_DNS = "dns"
CAT_MESSAGERIE = "messaging"
CAT_INCONNU = "unknown"

CATEGORIES: tuple[str, ...] = (
    CAT_STREAMING,
    CAT_SOCIAL,
    CAT_JEUX,
    CAT_VISIO,
    CAT_CDN,
    CAT_NUAGE,
    CAT_SYSTEME,
    CAT_DNS,
    CAT_MESSAGERIE,
)

# D'ou vient le verdict. L'ordre est celui de la confiance : un nom inverse qui
# dit 'nflxvideo.net' l'emporte sur un prefixe de CDN, parce qu'il nomme le
# SERVICE alors que le prefixe ne nomme que l'hebergeur.
SOURCE_CATALOGUE = "catalogue"
SOURCE_RDNS = "reverse name"
SOURCE_RDAP = "registry"
SOURCE_INCONNU = "unknown"


@dataclass(frozen=True)
class ServiceDef:
    """Un service reconnaissable, et ce qui permet de le reconnaitre.

    ``prefixes`` sont des blocs PUBLIES par l'operateur du service. Ils servent
    deux fois : a nommer une adresse vue, et -- c'est le point important -- a
    remplir la liste d'adresses d'une restriction AVANT meme qu'un client
    n'atteigne ces adresses. Une regle "bloquer Netflix" qui n'attendrait que
    les adresses deja vues laisserait passer la premiere connexion de chaque
    nouveau serveur.

    ``rdns`` sont des suffixes de nom inverse. C'est ce qui suit un service
    quand il change de prefixe, et ce qui distingue YouTube (googlevideo.com)
    du reste de Google alors que les deux partagent les memes blocs.
    """

    key: str
    label: str
    category: str
    prefixes: tuple[str, ...] = ()
    rdns: tuple[str, ...] = ()
    asns: tuple[int, ...] = ()
    #: Ce qu'il faut savoir avant de poser une restriction dessus. Affiche tel
    #: quel dans l'interface : c'est la que se dit "ce bloc porte aussi autre
    #: chose".
    note: str = ""


# LE CATALOGUE.
#
# Les blocs viennent des publications des operateurs concernes (pages "IP
# ranges", objets RIR, AS-SETs). Ils ne sont pas exhaustifs et ne peuvent pas
# l'etre : c'est exactement pourquoi le nom inverse existe en deuxieme source,
# et pourquoi les adresses decouvertes par NetFlow viennent COMPLETER ces blocs
# dans les listes d'adresses posees sur les routeurs.
CATALOGUE: tuple[ServiceDef, ...] = (
    ServiceDef(
        key="netflix",
        label="Netflix",
        category=CAT_STREAMING,
        prefixes=(
            "23.246.0.0/18",
            "37.77.184.0/21",
            "45.57.0.0/17",
            "64.120.128.0/17",
            "66.197.128.0/17",
            "69.53.224.0/19",
            "108.175.32.0/20",
            "185.2.220.0/22",
            "185.9.188.0/22",
            "192.173.64.0/18",
            "198.38.96.0/19",
            "198.45.48.0/20",
            "208.75.76.0/22",
            "2a00:86c0::/32",
            "2620:10c:7000::/44",
        ),
        rdns=("nflxvideo.net", "nflxso.net", "nflximg.net", "nflxext.com", "netflix.com"),
        asns=(2906, 40027),
        note=(
            "Open Connect cache servers may be HOSTED ON YOUR OWN NETWORK "
            "or at your transit provider: their address is then outside these "
            "prefixes, and only the reverse name gives them away."
        ),
    ),
    ServiceDef(
        key="youtube",
        label="YouTube",
        category=CAT_STREAMING,
        # Pas de prefixe propre : YouTube vit sur les blocs de Google, qui
        # portent aussi la recherche, Gmail et Android. Un bloc ne suffit donc
        # pas a le distinguer -- seul 'googlevideo.com' le fait.
        rdns=("googlevideo.com", "youtube.com", "ytimg.com", "youtu.be"),
        note=(
            "Recognised by REVERSE NAME only. Google prefixes also carry "
            "search and Gmail: restricting the prefix would restrict all of "
            "Google."
        ),
    ),
    ServiceDef(
        key="google",
        label="Google",
        category=CAT_NUAGE,
        prefixes=(
            "8.8.4.0/24",
            "8.8.8.0/24",
            "34.64.0.0/10",
            "35.190.0.0/17",
            "64.233.160.0/19",
            "66.102.0.0/20",
            "66.249.64.0/19",
            "72.14.192.0/18",
            "74.125.0.0/16",
            "108.177.0.0/17",
            "142.250.0.0/15",
            "172.217.0.0/16",
            "173.194.0.0/16",
            "209.85.128.0/17",
            "216.58.192.0/19",
            "216.239.32.0/19",
            "2607:f8b0::/32",
            "2a00:1450::/32",
        ),
        rdns=("1e100.net", "google.com", "gstatic.com", "googleapis.com", "googleusercontent.com"),
        asns=(15169, 36040, 396982),
    ),
    ServiceDef(
        key="twitch",
        label="Twitch",
        category=CAT_STREAMING,
        prefixes=("23.160.0.0/24", "52.223.192.0/18", "99.181.64.0/18", "185.42.204.0/22"),
        rdns=("ttvnw.net", "twitch.tv", "jtvnw.net"),
        asns=(46489,),
    ),
    ServiceDef(
        key="disney",
        label="Disney+",
        category=CAT_STREAMING,
        rdns=("disney-plus.net", "dssott.com", "bamgrid.com", "disneyplus.com"),
        note="Served by third-party CDNs: recognised by reverse name, not by prefix.",
    ),
    ServiceDef(
        key="prime-video",
        label="Prime Video",
        category=CAT_STREAMING,
        rdns=("aiv-cdn.net", "aiv-delivery.net", "pv-cdn.net", "primevideo.com"),
        note="Served from Amazon infrastructure: only the reverse name tells it apart.",
    ),
    ServiceDef(
        key="spotify",
        label="Spotify",
        category=CAT_STREAMING,
        prefixes=("35.186.224.0/20", "194.132.176.0/21"),
        rdns=("spotify.com", "scdn.co", "spotifycdn.com", "spotifycdn.net"),
        asns=(8403, 43650),
    ),
    ServiceDef(
        key="tiktok",
        label="TikTok",
        category=CAT_SOCIAL,
        rdns=("tiktokcdn.com", "tiktokv.com", "byteoversea.com", "ibytedtos.com", "tiktok.com"),
        asns=(138699, 396986),
    ),
    ServiceDef(
        key="meta",
        label="Meta (Facebook, Instagram, WhatsApp)",
        category=CAT_SOCIAL,
        prefixes=(
            "31.13.24.0/21",
            "31.13.64.0/18",
            "45.64.40.0/22",
            "66.220.144.0/20",
            "69.63.176.0/20",
            "69.171.224.0/19",
            "102.132.96.0/20",
            "129.134.0.0/16",
            "157.240.0.0/16",
            "163.70.128.0/17",
            "173.252.64.0/18",
            "179.60.192.0/22",
            "185.60.216.0/22",
            "2a03:2880::/32",
        ),
        rdns=("fbcdn.net", "facebook.com", "instagram.com", "whatsapp.net", "fbsbx.com"),
        asns=(32934,),
    ),
    ServiceDef(
        key="x-twitter",
        label="X (Twitter)",
        category=CAT_SOCIAL,
        prefixes=("104.244.40.0/21", "192.133.76.0/22", "199.16.156.0/22", "199.59.148.0/22"),
        rdns=("twimg.com", "twitter.com", "x.com"),
        asns=(13414,),
    ),
    ServiceDef(
        key="telegram",
        label="Telegram",
        category=CAT_MESSAGERIE,
        prefixes=(
            "91.108.4.0/22",
            "91.108.8.0/22",
            "91.108.12.0/22",
            "91.108.16.0/22",
            "91.108.56.0/22",
            "149.154.160.0/20",
            "2001:b28:f23d::/48",
        ),
        rdns=("telegram.org", "t.me"),
        asns=(62041, 59930),
    ),
    ServiceDef(
        key="discord",
        label="Discord",
        category=CAT_VISIO,
        rdns=("discord.gg", "discordapp.net", "discord.com", "discordapp.com"),
    ),
    ServiceDef(
        key="zoom",
        label="Zoom",
        category=CAT_VISIO,
        prefixes=("3.7.35.0/25", "103.122.166.0/23", "149.137.0.0/17", "170.114.0.0/16"),
        rdns=("zoom.us", "zoomgov.com"),
        asns=(399489,),
    ),
    ServiceDef(
        key="steam",
        label="Steam (Valve)",
        category=CAT_JEUX,
        prefixes=(
            "103.10.124.0/23",
            "146.66.152.0/21",
            "155.133.224.0/19",
            "162.254.192.0/21",
            "185.25.180.0/22",
            "205.196.6.0/24",
        ),
        rdns=("steamcontent.com", "steamserver.net", "steampowered.com", "valve.net"),
        asns=(32590,),
    ),
    ServiceDef(
        key="playstation",
        label="PlayStation Network",
        category=CAT_JEUX,
        rdns=("playstation.net", "playstation.com", "sonyentertainmentnetwork.com"),
    ),
    ServiceDef(
        key="xbox",
        label="Xbox Live",
        category=CAT_JEUX,
        rdns=("xboxlive.com", "xbox.com"),
    ),
    ServiceDef(
        key="riot",
        label="Riot Games",
        category=CAT_JEUX,
        rdns=("riotgames.com", "leagueoflegends.com"),
        asns=(6507,),
    ),
    ServiceDef(
        key="microsoft",
        label="Microsoft / Azure",
        category=CAT_SYSTEME,
        prefixes=(
            "13.64.0.0/11",
            "13.104.0.0/14",
            "20.33.0.0/16",
            "20.36.0.0/14",
            "40.64.0.0/10",
            "52.96.0.0/12",
            "204.79.195.0/24",
            "2603:1000::/24",
        ),
        rdns=(
            "microsoft.com",
            "msedge.net",
            "windowsupdate.com",
            "trafficmanager.net",
            "azureedge.net",
            "office.com",
            "live.com",
        ),
        asns=(8075, 8068, 8069),
        note="Azure prefixes: they carry updates as readily as any third-party site.",
    ),
    ServiceDef(
        key="apple",
        label="Apple",
        category=CAT_SYSTEME,
        prefixes=("17.0.0.0/8", "2620:149::/32"),
        rdns=("apple.com", "aaplimg.com", "icloud.com", "mzstatic.com"),
        asns=(714, 6185),
    ),
    ServiceDef(
        key="amazon",
        label="Amazon (AWS / CloudFront)",
        category=CAT_NUAGE,
        prefixes=(
            "3.0.0.0/9",
            "13.32.0.0/15",
            "13.224.0.0/14",
            "18.64.0.0/14",
            "52.84.0.0/15",
            "54.192.0.0/16",
            "99.84.0.0/16",
            "143.204.0.0/16",
            "205.251.192.0/19",
        ),
        rdns=("amazonaws.com", "cloudfront.net", "amazon.com"),
        asns=(16509, 14618),
        note=(
            "An AWS prefix carries anything and everything, Prime Video included. "
            "Restricting this service restricts thousands of third-party sites."
        ),
    ),
    ServiceDef(
        key="cloudflare",
        label="Cloudflare",
        category=CAT_CDN,
        prefixes=(
            "103.21.244.0/22",
            "103.22.200.0/22",
            "103.31.4.0/22",
            "104.16.0.0/12",
            "108.162.192.0/18",
            "131.0.72.0/22",
            "141.101.64.0/18",
            "162.158.0.0/15",
            "172.64.0.0/13",
            "173.245.48.0/20",
            "188.114.96.0/20",
            "190.93.240.0/20",
            "197.234.240.0/22",
            "198.41.128.0/17",
            "1.1.1.0/24",
            "2606:4700::/32",
        ),
        rdns=("cloudflare.com", "cloudflare-dns.com"),
        asns=(13335,),
        note=(
            "A CDN IS NOT A SERVICE. The same prefix serves a recipe site, "
            "a video catalogue and a banking API. Restricting here is brutal."
        ),
    ),
    ServiceDef(
        key="akamai",
        label="Akamai",
        category=CAT_CDN,
        prefixes=("2.16.0.0/13", "23.32.0.0/11", "23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"),
        rdns=("akamaitechnologies.com", "akamaiedge.net", "akamai.net", "edgekey.net"),
        asns=(20940, 16625, 32787),
        note="Same warning as Cloudflare: a CDN carries everybody.",
    ),
    ServiceDef(
        key="fastly",
        label="Fastly",
        category=CAT_CDN,
        prefixes=("23.235.32.0/20", "146.75.0.0/16", "151.101.0.0/16", "199.232.0.0/16"),
        rdns=("fastly.net", "fastlylb.net"),
        asns=(54113,),
        note="Same warning as Cloudflare: a CDN carries everybody.",
    ),
    ServiceDef(
        key="dns-public",
        label="Resolveurs DNS publics",
        category=CAT_DNS,
        prefixes=("9.9.9.0/24", "208.67.222.0/24", "208.67.220.0/24", "2620:fe::/48"),
        rdns=("quad9.net", "opendns.com", "dns.google"),
    ),
    ServiceDef(
        key="ovh",
        label="OVHcloud",
        category=CAT_NUAGE,
        prefixes=(
            "51.68.0.0/14",
            "51.75.0.0/16",
            "54.36.0.0/14",
            "91.121.0.0/16",
            "145.239.0.0/16",
        ),
        rdns=("ovh.net", "ovh.com"),
        asns=(16276,),
    ),
)

PAR_CLE: dict[str, ServiceDef] = {service.key: service for service in CATALOGUE}


@dataclass(frozen=True)
class Verdict:
    """Ce qu'on sait d'une adresse, et a quel titre on le sait."""

    service: str | None = None
    label: str | None = None
    category: str = CAT_INCONNU
    source: str = SOURCE_INCONNU
    #: Le bloc du catalogue qui a repondu, quand c'est un bloc qui a repondu.
    matched_prefix: str | None = None

    @property
    def known(self) -> bool:
        return self.service is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "service_label": self.label,
            "category": self.category,
            "source": self.source,
            "matched_prefix": self.matched_prefix,
        }


class _Index:
    """Recherche du bloc le plus precis qui contient une adresse.

    Meme raison d'etre que ``PrefixIndex`` cote abonnes : on interroge par
    masquage plutot qu'en parcourant tous les blocs. Le catalogue en compte
    une centaine et chaque adresse vue par NetFlow le traverse.
    """

    def __init__(self) -> None:
        self._v4: dict[int, dict[int, str]] = {}
        self._v6: dict[int, dict[int, str]] = {}
        self._v4_lengths: tuple[int, ...] = ()
        self._v6_lengths: tuple[int, ...] = ()

    @classmethod
    def build(cls, entries: list[tuple[str, str]]) -> _Index:
        index = cls()
        for prefixe, cle in entries:
            try:
                reseau = ipaddress.ip_network(prefixe, strict=False)
            except ValueError:
                logger.warning("Prefixe ignore dans le catalogue : %r (%s)", prefixe, cle)
                continue
            table = index._v4 if reseau.version == 4 else index._v6
            table.setdefault(reseau.prefixlen, {})[int(reseau.network_address)] = cle
        index._v4_lengths = tuple(sorted(index._v4, reverse=True))
        index._v6_lengths = tuple(sorted(index._v6, reverse=True))
        return index

    def lookup(self, address: str) -> tuple[str, str] | None:
        """Rend (cle du service, prefixe qui a repondu), ou None."""
        try:
            adresse = ipaddress.ip_address(address)
        except ValueError:
            return None
        if adresse.version == 4:
            table, longueurs, bits = self._v4, self._v4_lengths, 32
        else:
            table, longueurs, bits = self._v6, self._v6_lengths, 128
        valeur = int(adresse)
        for longueur in longueurs:
            masque = (1 << bits) - (1 << (bits - longueur))
            trouve = table[longueur].get(valeur & masque)
            if trouve is not None:
                reseau = ipaddress.ip_network((valeur & masque, longueur))
                return trouve, str(reseau)
        return None


_INDEX = _Index.build([(p, s.key) for s in CATALOGUE for p in s.prefixes])


def is_routable(address: str) -> bool:
    """L'adresse designe-t-elle une machine sur internet ?

    Ce filtre decide de ce qui merite d'etre enrichi. Une adresse privee, de
    lien-local ou de multicast est l'autre bout du reseau de l'exploitant, pas
    un service a nommer : lui chercher un nom inverse ferait une requete DNS
    par client et par fenetre, pour rien.
    """
    try:
        adresse = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        adresse.is_private
        or adresse.is_loopback
        or adresse.is_link_local
        or adresse.is_multicast
        or adresse.is_reserved
        or adresse.is_unspecified
    )


def match_prefix(address: str) -> Verdict:
    """Verdict rendu par le seul catalogue. Ne fait AUCUN appel reseau."""
    trouve = _INDEX.lookup(address)
    if trouve is None:
        return Verdict()
    cle, prefixe = trouve
    service = PAR_CLE[cle]
    return Verdict(
        service=service.key,
        label=service.label,
        category=service.category,
        source=SOURCE_CATALOGUE,
        matched_prefix=prefixe,
    )


#: Suffixes a deux etiquettes : le domaine enregistrable y compte trois
#: etiquettes, pas deux. Sans cette liste, ``abo.wanadoo.fr`` serait lu comme
#: ``wanadoo.fr`` (correct), mais ``bbc.co.uk`` deviendrait ``co.uk`` -- le nom
#: du registre, pas celui de l'organisation.
SUFFIXES_COMPOSES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "gov.uk",
        "ac.uk",
        "net.uk",
        "sch.uk",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "co.nz",
        "net.nz",
        "org.nz",
        "com.br",
        "net.br",
        "org.br",
        "gov.br",
        "co.jp",
        "ne.jp",
        "or.jp",
        "ac.jp",
        "go.jp",
        "co.in",
        "net.in",
        "org.in",
        "gov.in",
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "co.za",
        "org.za",
        "net.za",
        "com.mx",
        "com.ar",
        "com.tr",
        "com.sg",
        "com.hk",
        "com.tw",
    }
)


def registrable_domain(hostname: str | None) -> str | None:
    """Le domaine sous lequel le nom inverse est enregistre.

    ``lfbn-lyo-1-878-160.w86-194.abo.wanadoo.fr`` ne dit rien a personne ;
    ``wanadoo.fr`` dit Orange. C'est la forme qu'un exploitant reconnait d'un
    coup d'oeil, et celle qu'il tapera dans un moteur de recherche s'il ne la
    reconnait pas.

    Ce n'est PAS la liste publique des suffixes (PSL) : l'embarquer ferait
    entrer plusieurs milliers d'entrees a tenir a jour pour un gain marginal.
    Les suffixes composes les plus courants suffisent, et le reste retombe sur
    les deux dernieres etiquettes -- ce qui est juste dans l'immense majorite
    des cas.
    """
    if not hostname:
        return None
    nom = hostname.strip().rstrip(".").lower()
    etiquettes = [e for e in nom.split(".") if e]
    if len(etiquettes) < 2:
        return None
    if len(etiquettes) >= 3 and ".".join(etiquettes[-2:]) in SUFFIXES_COMPOSES:
        return ".".join(etiquettes[-3:])
    return ".".join(etiquettes[-2:])


def match_hostname(hostname: str | None) -> Verdict:
    """Verdict rendu par le nom inverse.

    Le suffixe doit tomber sur une FRONTIERE DE LABEL : ``nflxvideo.net``
    reconnait ``a.b.nflxvideo.net`` mais pas ``pasnflxvideo.net``. Sans cette
    precaution, un nom de domaine achete pour l'occasion suffirait a se faire
    passer pour n'importe quel service du catalogue.
    """
    if not hostname:
        return Verdict()
    nom = hostname.strip().rstrip(".").lower()
    if not nom:
        return Verdict()
    for service in CATALOGUE:
        for suffixe in service.rdns:
            if nom == suffixe or nom.endswith("." + suffixe):
                return Verdict(
                    service=service.key,
                    label=service.label,
                    category=service.category,
                    source=SOURCE_RDNS,
                )
    return Verdict()


def match_asn(asn: int | None) -> Verdict:
    """Verdict rendu par le numero d'AS annonce par le registre."""
    if not asn:
        return Verdict()
    for service in CATALOGUE:
        if asn in service.asns:
            return Verdict(
                service=service.key,
                label=service.label,
                category=service.category,
                source=SOURCE_RDAP,
            )
    return Verdict()


def identify(address: str, *, hostname: str | None = None, asn: int | None = None) -> Verdict:
    """Le verdict retenu pour une adresse, toutes sources confondues.

    ORDRE DE PRIORITE, ET IL COMPTE. Le nom inverse passe AVANT le catalogue de
    blocs : ``ipv4-c001.1.oca.nflxvideo.net`` sur une adresse de votre propre
    AS (un cache Open Connect heberge chez vous) doit se lire "Netflix", pas
    "inconnu". A l'inverse, un bloc de CDN ne doit jamais ecraser un nom inverse
    qui nomme le service reellement servi.

    Le numero d'AS vient en dernier : il identifie l'HEBERGEUR, ce qui est
    exactement la reponse la moins precise des trois.
    """
    par_nom = match_hostname(hostname)
    if par_nom.known:
        return par_nom
    par_prefixe = match_prefix(address)
    if par_prefixe.known:
        return par_prefixe
    return match_asn(asn)


def service_prefixes(keys: set[str]) -> list[str]:
    """Les blocs publies des services demandes.

    C'est ce qui rend une restriction utile DES SA POSE : la liste d'adresses
    du routeur part deja remplie, sans attendre qu'un client atteigne chaque
    serveur du service.
    """
    sortie: list[str] = []
    for service in CATALOGUE:
        if service.key in keys:
            sortie.extend(service.prefixes)
    return sortie


def services_in_categories(categories: set[str]) -> set[str]:
    return {s.key for s in CATALOGUE if s.category in categories}


def describe_catalogue() -> list[dict[str, Any]]:
    """Le catalogue tel que l'interface le montre : de quoi ecrire une regle."""
    return [
        {
            "key": s.key,
            "label": s.label,
            "category": s.category,
            "prefixes": len(s.prefixes),
            "rdns": list(s.rdns),
            "note": s.note,
        }
        for s in CATALOGUE
    ]


# ------------------------------------------------------------------ resolution


@dataclass
class ReverseDns:
    """Nom inverse d'une adresse, avec un cache et une borne de temps.

    POURQUOI UN CACHE ICI ET PAS SEULEMENT EN BASE. La base retient le verdict
    d'une adresse deja enrichie ; ce cache-ci evite de redemander le meme nom
    dans la MEME rafale, quand plusieurs abonnes atteignent le meme serveur dans
    la meme fenetre. Sans lui, un serveur populaire genere une requete DNS par
    abonne.

    ``socket.gethostbyaddr`` est bloquant : il est appele dans un fil, avec une
    limite de temps. Un resolveur muet ne doit pas figer la boucle qui enrichit.
    """

    timeout_s: float = 2.0
    cache: dict[str, str | None] = field(default_factory=dict)
    cache_max: int = 20_000

    def lookup(self, address: str) -> str | None:
        """Version bloquante. Rend None si le nom n'existe pas ou ne repond pas."""
        if address in self.cache:
            return self.cache[address]
        nom: str | None = None
        try:
            socket.setdefaulttimeout(self.timeout_s)
            nom = socket.gethostbyaddr(address)[0]
        except OSError:
            nom = None
        except Exception:  # noqa: BLE001 - un resolveur exotique ne casse pas la boucle
            logger.debug("Nom inverse impossible pour %s", address, exc_info=True)
            nom = None
        finally:
            socket.setdefaulttimeout(None)
        if len(self.cache) >= self.cache_max:
            self.cache.clear()
        self.cache[address] = nom
        return nom

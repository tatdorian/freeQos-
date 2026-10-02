"""Cles d'API : fabrication, empreinte, verification.

AUCUN SECRET N'EST CONSERVE. Une cle est tiree une fois, montree une fois, puis
seule son empreinte SHA-256 reste en base. C'est la difference entre une fuite
de base qui coute une rotation et une fuite de base qui donne l'inventaire
complet d'un operateur a qui la lit.

Le format porte un PREFIXE en clair : ``fqos_<prefixe>_<secret>``. Il n'est pas
decoratif. Il permet de retrouver la ligne d'un seul index au lieu de comparer
l'empreinte de toutes les cles enregistrees, et il donne a l'interface un nom
affichable ("fqos_a1b2c3d4...") pour designer une cle sans jamais la reveler.

La comparaison passe par ``hmac.compare_digest`` : comparer deux empreintes avec
``==`` laisse fuir, par le temps de reponse, le nombre d'octets corrects. Sur une
empreinte de cle d'API c'est theorique, mais le cout de bien faire est nul.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass

PREFIX = "fqos"
PREFIX_LEN = 8
SECRET_LEN = 32

#: Alphabet du corps de la cle. Il EXCLUT le tiret bas, et ce n'est pas un
#: detail de style : le tiret bas separe les trois parties de la cle. Un corps
#: tire dans l'alphabet base64url en contient un de temps en temps, et la cle
#: devenait alors indecoupable -- donc refusee. Une cle sur quelques-unes, au
#: hasard, ce qui est exactement le genre de defaut qu'on ne reproduit jamais
#: chez soi et qu'on decouvre chez un client.
ALPHABET = string.ascii_letters + string.digits

#: Portees reconnues. 'read' donne les lectures, 'write' ajoute les ecritures.
#: Deux suffisent : une integration de facturation ecrit, une supervision lit.
SCOPES = ("read", "write")


class InvalidApiKeyError(ValueError):
    """La chaine fournie n'a pas la forme d'une cle freeQoS."""


@dataclass(frozen=True)
class GeneratedKey:
    """Une cle fraichement tiree. ``secret`` ne sera plus jamais disponible."""

    secret: str
    prefix: str
    key_hash: str


def generate_key() -> GeneratedKey:
    prefix = secrets.token_hex(PREFIX_LEN // 2)
    body = "".join(secrets.choice(ALPHABET) for _ in range(SECRET_LEN))
    secret = f"{PREFIX}_{prefix}_{body}"
    return GeneratedKey(secret=secret, prefix=prefix, key_hash=hash_key(secret))


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.strip().encode("utf-8")).hexdigest()


def extract_prefix(secret: str) -> str:
    """Rend le prefixe d'une cle presentee, ou leve si la forme est mauvaise.

    On refuse tot et explicitement : une chaine qui n'a pas la forme attendue ne
    merite pas une requete en base, et le message evite a l'integrateur de
    chercher du cote des droits alors qu'il a colle la mauvaise valeur.
    """
    # maxsplit=2 : le corps de la cle ne porte PAS de tiret bas, mais une cle
    # tiree par une version anterieure pouvait en contenir. Decouper en trois au
    # plus la rend encore lisible plutot que de la refuser silencieusement.
    morceaux = secret.strip().split("_", 2)
    if len(morceaux) != 3 or morceaux[0] != PREFIX or not morceaux[1] or not morceaux[2]:
        raise InvalidApiKeyError(f"cle mal formee : attendu {PREFIX}_<prefixe>_<secret>")
    return morceaux[1]


def matches(secret: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_key(secret), key_hash)


def normalise_scopes(scopes: object) -> list[str]:
    """Garde les portees connues, dans un ordre stable, et impose 'read'.

    Une cle qui ecrit lit forcement : refuser le GET a une cle 'write' seule
    serait une surprise sans aucun gain de securite.
    """
    if isinstance(scopes, str):
        brut: list[str] = [scopes]
    elif isinstance(scopes, (list, tuple, set)):
        brut = [str(item) for item in scopes]
    else:
        brut = []
    retenues = {item.strip().lower() for item in brut if item.strip().lower() in SCOPES}
    if not retenues:
        retenues = {"read"}
    retenues.add("read")
    return [scope for scope in SCOPES if scope in retenues]

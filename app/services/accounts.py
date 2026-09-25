"""Comptes de l'interface d'exploitation : qui peut lire, qui peut modifier.

DEUX GRADES, ET SEULEMENT DEUX
------------------------------
``read`` voit tout et ne change rien. ``edit`` fait tout, y compris creer,
modifier et supprimer des comptes. Un controleur qui ecrit sur des routeurs
n'a pas besoin d'une matrice de permissions : il a besoin qu'un stagiaire ou
un superviseur puisse REGARDER sans pouvoir poser une file par erreur.

LE GRADE EST APPLIQUE PAR LE SERVEUR, PAS PAR L'INTERFACE
---------------------------------------------------------
Masquer un bouton n'interdit rien : n'importe qui ouvre la console du
navigateur et rejoue la requete. La regle est donc posee sur l'API elle-meme :
un compte ``read`` n'obtient que les methodes de LECTURE (GET, HEAD, OPTIONS),
et toute autre methode lui est refusee, quelle que soit la route.

AUCUNE DEPENDANCE AJOUTEE
-------------------------
Les mots de passe sont haches avec scrypt, fourni par la bibliotheque standard
(``hashlib.scrypt``) : lent a dessein, sale, et comparable en temps constant.
Les jetons de session sont des aleas de 256 bits ; seule leur empreinte SHA-256
est stockee, de sorte qu'une copie de la base ne permet d'ouvrir aucune session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

ROLE_READ = "read"
ROLE_EDIT = "edit"
ROLES: tuple[str, ...] = (ROLE_READ, ROLE_EDIT)

#: Methodes qu'un compte en lecture seule peut employer.
SAFE_METHODES = frozenset({"GET", "HEAD", "OPTIONS"})

PASSWORD_MIN = 8
PASSWORD_MAX = 256

# scrypt : N=2^14, r=8, p=1 -- les parametres recommandes pour une connexion
# interactive (environ 16 Mio et quelques dizaines de ms par essai).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class InvalidAccountError(ValueError):
    """Saisie refusee : le message est rendu tel quel a l'interface."""


def normalise_email(value: str) -> str:
    """L'email sert d'identifiant : sans espaces, en minuscules, et plausible."""
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise InvalidAccountError("invalid email address")
    return email


def check_role(value: str) -> str:
    role = (value or "").strip().lower()
    if role not in ROLES:
        raise InvalidAccountError("role must be 'read' or 'edit'")
    return role


def check_password(value: str) -> str:
    if not isinstance(value, str) or len(value) < PASSWORD_MIN:
        raise InvalidAccountError(f"password too short (at least {PASSWORD_MIN} characters)")
    if len(value) > PASSWORD_MAX:
        raise InvalidAccountError("password too long")
    return value


def hash_password(password: str) -> str:
    """``scrypt$N$r$p$sel$empreinte`` -- les parametres voyagent avec l'empreinte,
    pour pouvoir les durcir plus tard sans invalider les comptes existants."""
    sel = secrets.token_bytes(16)
    empreinte = hashlib.scrypt(
        password.encode("utf-8"),
        salt=sel,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(sel)}${_b64(empreinte)}"


def _b64(octets: bytes) -> str:
    return base64.b64encode(octets).decode("ascii")


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, n, r, p, sel, attendu = stored.split("$")
        if algo != "scrypt":
            return False
        calcule = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(sel),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(attendu)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calcule, base64.b64decode(attendu))


#: Empreinte d'un mot de passe que personne ne connait : verifiee quand l'email
#: est inconnu, pour que la reponse prenne le MEME temps qu'un mauvais mot de
#: passe. Sans cela, la duree de la reponse dirait quels emails ont un compte.
_LEURRE = hash_password(secrets.token_urlsafe(24))


def verify_or_decoy(password: str, stored: str | None) -> bool:
    if stored is None:
        verify_password(password, _LEURRE)
        return False
    return verify_password(password, stored)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class LoginThrottle:
    """Freine la devinette de mots de passe, par email ET par adresse cliente.

    Cinq echecs dans la fenetre bloquent la cle pendant ``lock_s`` secondes.
    En memoire : un redemarrage remet les compteurs a zero, ce qui est
    acceptable -- le but est de rendre une attaque en ligne lente, pas de
    tenir un registre.
    """

    max_failures: int = 5
    window_s: float = 300.0
    lock_s: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _echecs: dict[str, list[float]] = field(default_factory=dict)
    _verrous: dict[str, float] = field(default_factory=dict)

    def locked_for(self, *keys: str) -> float:
        maintenant = self.clock()
        reste = 0.0
        for cle in keys:
            fin = self._verrous.get(cle, 0.0)
            if fin > maintenant:
                reste = max(reste, fin - maintenant)
        return reste

    def failure(self, *keys: str) -> None:
        maintenant = self.clock()
        for cle in keys:
            recents = [t for t in self._echecs.get(cle, []) if maintenant - t < self.window_s]
            recents.append(maintenant)
            self._echecs[cle] = recents
            if len(recents) >= self.max_failures:
                self._verrous[cle] = maintenant + self.lock_s
                self._echecs[cle] = []

    def success(self, *keys: str) -> None:
        for cle in keys:
            self._echecs.pop(cle, None)
            self._verrous.pop(cle, None)

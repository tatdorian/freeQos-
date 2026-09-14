"""Authentification du controleur.

Le controleur ecrit sur des routeurs de production : son API ne peut pas rester
anonyme. Cette brique fournit tout ce qu'il faut pour qu'aucun endpoint ne soit
joignable sans identite.

Choix de conception :

- **Mots de passe** : haches en **argon2** (secret a faible entropie, il faut un
  hachage lent et sale). Jamais stockes en clair, jamais renvoyes par l'API.
- **Cles d'API** (appels machine) : secret aleatoire a HAUTE entropie, donc un
  hachage rapide (SHA-256) suffit et convient -- c'est ce que font GitHub et
  consorts pour leurs jetons. Le secret n'est montre qu'une fois, a la creation.
- **Sessions** : jeton aleatoire pose dans un cookie ``SameSite=Strict`` /
  ``HttpOnly`` ; seul son SHA-256 est stocke, ce qui rend la session revocable
  (deconnexion) et invalidable a l'expiration.
- **Anti-bourrage** : la connexion est limitee en debit par identifiant, pour
  qu'un mot de passe ne se devine pas par force brute.

Deux implementations de stockage derriere la meme abstraction : ``PgAuthStore``
(production) et ``InMemoryAuthStore`` (tests, mode sans base).
"""

from __future__ import annotations

import hashlib
import logging
import secrets as secrets_module
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)

# Nom du cookie de session. SameSite=Strict + HttpOnly : le cookie ne part que
# vers notre propre origine, et JavaScript ne peut pas le lire.
SESSION_COOKIE = "freeqos_session"
# Prefixe visible d'une cle d'API : permet de la reconnaitre dans un journal
# sans exposer le secret.
API_KEY_PREFIX = "fq_"


class AuthError(RuntimeError):
    """Erreur d'authentification generique."""


class InvalidCredentialsError(AuthError):
    """Identifiant ou mot de passe refuse."""


class RateLimitedError(AuthError):
    """Trop de tentatives de connexion pour cet identifiant."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(
            f"Trop de tentatives de connexion. Reessayez dans {int(retry_after_s)} seconde(s)."
        )
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Identity:
    """Qui agit : un compte connecte, ou une cle d'API pour un appel machine."""

    kind: str  # "user" | "api_key"
    name: str
    display: str
    is_admin: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "display": self.display,
            "is_admin": self.is_admin,
        }


@dataclass
class UserRecord:
    username: str
    password_hash: str
    display_name: str
    is_admin: bool
    disabled: bool = False
    last_login_at: datetime | None = None


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    prefix: str
    key_hash: str
    is_admin: bool
    created_by: str | None
    created_at: datetime
    disabled: bool = False
    last_used_at: datetime | None = None

    def to_public(self) -> dict[str, object]:
        # Le hash n'est JAMAIS expose : une cle ne se relit pas, elle se recree.
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "is_admin": self.is_admin,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "disabled": self.disabled,
            "last_used_at": self.last_used_at,
        }


@dataclass
class SessionRecord:
    token_hash: str
    username: str
    display: str
    is_admin: bool
    expires_at: datetime


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- stockage
class AuthStore(Protocol):
    async def count_users(self) -> int: ...
    async def get_user(self, username: str) -> UserRecord | None: ...
    async def create_user(self, record: UserRecord) -> None: ...
    async def list_users(self) -> list[UserRecord]: ...
    async def set_password(self, username: str, password_hash: str) -> bool: ...
    async def set_user_disabled(self, username: str, disabled: bool) -> bool: ...
    async def touch_login(self, username: str) -> None: ...

    async def create_api_key(self, record: ApiKeyRecord) -> None: ...
    async def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None: ...
    async def list_api_keys(self) -> list[ApiKeyRecord]: ...
    async def delete_api_key(self, key_id: str) -> bool: ...
    async def touch_api_key(self, key_id: str) -> None: ...

    async def create_session(self, record: SessionRecord) -> None: ...
    async def get_session(self, token_hash: str) -> SessionRecord | None: ...
    async def delete_session(self, token_hash: str) -> None: ...
    async def purge_expired_sessions(self) -> int: ...


class InMemoryAuthStore:
    """Stockage memoire : tests et mode sans base."""

    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}
        self._api_keys: dict[str, ApiKeyRecord] = {}
        self._sessions: dict[str, SessionRecord] = {}

    # --- amorcage synchrone, reserve aux tests ---
    def seed_user(self, record: UserRecord) -> None:
        self._users[record.username] = record

    def seed_api_key(self, record: ApiKeyRecord) -> None:
        self._api_keys[record.id] = record

    async def count_users(self) -> int:
        return len(self._users)

    async def get_user(self, username: str) -> UserRecord | None:
        return self._users.get(username)

    async def create_user(self, record: UserRecord) -> None:
        if record.username in self._users:
            raise AuthError(f"le compte '{record.username}' existe deja")
        self._users[record.username] = record

    async def list_users(self) -> list[UserRecord]:
        return sorted(self._users.values(), key=lambda u: u.username)

    async def set_password(self, username: str, password_hash: str) -> bool:
        user = self._users.get(username)
        if user is None:
            return False
        user.password_hash = password_hash
        return True

    async def set_user_disabled(self, username: str, disabled: bool) -> bool:
        user = self._users.get(username)
        if user is None:
            return False
        user.disabled = disabled
        return True

    async def touch_login(self, username: str) -> None:
        user = self._users.get(username)
        if user is not None:
            user.last_login_at = datetime.now(tz=UTC)

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        self._api_keys[record.id] = record

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        for record in self._api_keys.values():
            if secrets_module.compare_digest(record.key_hash, key_hash):
                return record
        return None

    async def list_api_keys(self) -> list[ApiKeyRecord]:
        return sorted(self._api_keys.values(), key=lambda k: k.created_at)

    async def delete_api_key(self, key_id: str) -> bool:
        return self._api_keys.pop(key_id, None) is not None

    async def touch_api_key(self, key_id: str) -> None:
        record = self._api_keys.get(key_id)
        if record is not None:
            record.last_used_at = datetime.now(tz=UTC)

    async def create_session(self, record: SessionRecord) -> None:
        self._sessions[record.token_hash] = record

    async def get_session(self, token_hash: str) -> SessionRecord | None:
        return self._sessions.get(token_hash)

    async def delete_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    async def purge_expired_sessions(self) -> int:
        now = datetime.now(tz=UTC)
        expired = [h for h, s in self._sessions.items() if s.expires_at <= now]
        for token_hash in expired:
            del self._sessions[token_hash]
        return len(expired)


class _LoginRateLimiter:
    """Fenetre glissante des echecs par identifiant.

    Un mot de passe ne doit pas pouvoir se deviner par force brute : au-dela de
    ``max_attempts`` echecs dans ``window_s``, on refuse toute tentative jusqu'a
    ce que la fenetre se vide. Un succes remet le compteur a zero.
    """

    def __init__(
        self,
        max_attempts: int,
        window_s: float,
        clock: object = time.monotonic,
    ) -> None:
        self._max = max(1, max_attempts)
        self._window = window_s
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def _prune(self, key: str, now: float) -> None:
        attempts = self._failures[key]
        while attempts and now - attempts[0] > self._window:
            attempts.popleft()

    def check(self, key: str) -> None:
        now = self._now()
        self._prune(key, now)
        attempts = self._failures[key]
        if len(attempts) >= self._max:
            retry_after = self._window - (now - attempts[0])
            raise RateLimitedError(max(retry_after, 0.0))

    def record_failure(self, key: str) -> None:
        now = self._now()
        self._prune(key, now)
        self._failures[key].append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


class AuthService:
    """Authentification : mots de passe argon2, cles d'API, sessions, anti-bourrage."""

    def __init__(
        self,
        store: AuthStore,
        *,
        session_ttl_hours: float = 12.0,
        login_max_attempts: int = 5,
        login_window_s: float = 300.0,
        rate_clock: object = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl = timedelta(hours=session_ttl_hours)
        self._limiter = _LoginRateLimiter(login_max_attempts, login_window_s, clock=rate_clock)
        self._hasher = self._build_hasher()

    @staticmethod
    def _build_hasher() -> object:
        from argon2 import PasswordHasher

        return PasswordHasher()

    # --- mots de passe ---
    def hash_password(self, password: str) -> str:
        return str(self._hasher.hash(password))  # type: ignore[attr-defined]

    def _verify_password(self, password_hash: str, password: str) -> bool:
        try:
            self._hasher.verify(password_hash, password)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001 - toute erreur = refus
            return False

    # --- comptes ---
    async def count_users(self) -> int:
        return await self._store.count_users()

    async def ensure_admin(
        self, username: str, password: str, *, display: str | None = None
    ) -> bool:
        """Cree le compte admin initial UNIQUEMENT si aucun compte n'existe.

        Idempotent : une fois un compte present, ne fait plus rien. C'est ce qui
        evite un mot de passe par defaut connu tout en laissant le controleur
        utilisable au premier demarrage.
        """
        if await self._store.count_users() > 0:
            return False
        await self._store.create_user(
            UserRecord(
                username=username,
                password_hash=self.hash_password(password),
                display_name=display or username,
                is_admin=True,
            )
        )
        logger.warning("Compte administrateur initial '%s' cree.", username)
        return True

    async def create_user(
        self, username: str, password: str, *, is_admin: bool, display: str | None = None
    ) -> None:
        await self._store.create_user(
            UserRecord(
                username=username,
                password_hash=self.hash_password(password),
                display_name=display or username,
                is_admin=is_admin,
            )
        )

    async def set_password(self, username: str, password: str) -> bool:
        return await self._store.set_password(username, self.hash_password(password))

    async def set_user_disabled(self, username: str, disabled: bool) -> bool:
        return await self._store.set_user_disabled(username, disabled)

    async def list_users(self) -> list[dict[str, object]]:
        return [
            {
                "username": u.username,
                "display_name": u.display_name,
                "is_admin": u.is_admin,
                "disabled": u.disabled,
                "last_login_at": u.last_login_at,
            }
            for u in await self._store.list_users()
        ]

    async def authenticate(self, username: str, password: str) -> Identity:
        """Verifie un couple identifiant / mot de passe. Limite en debit.

        Le message d'erreur ne distingue jamais 'compte inconnu' de 'mauvais mot
        de passe' : le dire renseignerait un attaquant sur les comptes existants.
        """
        self._limiter.check(username)
        user = await self._store.get_user(username)
        ok = (
            user is not None
            and not user.disabled
            and self._verify_password(user.password_hash, password)
        )
        if not ok or user is None:
            self._limiter.record_failure(username)
            raise InvalidCredentialsError("identifiant ou mot de passe incorrect")
        self._limiter.reset(username)
        await self._store.touch_login(username)
        return Identity("user", user.username, user.display_name, user.is_admin)

    # --- sessions ---
    async def create_session(self, identity: Identity) -> tuple[str, datetime]:
        token = secrets_module.token_urlsafe(32)
        expires_at = datetime.now(tz=UTC) + self._ttl
        await self._store.create_session(
            SessionRecord(
                token_hash=sha256(token),
                username=identity.name,
                display=identity.display,
                is_admin=identity.is_admin,
                expires_at=expires_at,
            )
        )
        return token, expires_at

    async def resolve_session(self, token: str) -> Identity | None:
        record = await self._store.get_session(sha256(token))
        if record is None:
            return None
        if record.expires_at <= datetime.now(tz=UTC):
            await self._store.delete_session(record.token_hash)
            return None
        return Identity("user", record.username, record.display, record.is_admin)

    async def revoke_session(self, token: str) -> None:
        await self._store.delete_session(sha256(token))

    @property
    def session_ttl(self) -> timedelta:
        return self._ttl

    # --- cles d'API ---
    async def create_api_key(
        self, name: str, *, is_admin: bool, created_by: str | None
    ) -> tuple[ApiKeyRecord, str]:
        """Cree une cle d'API. Le secret n'est renvoye QU'ICI : il n'est jamais
        stocke en clair et ne peut plus etre relu ensuite."""
        secret = API_KEY_PREFIX + secrets_module.token_urlsafe(32)
        record = ApiKeyRecord(
            id=secrets_module.token_hex(8),
            name=name,
            prefix=secret[:12],
            key_hash=sha256(secret),
            is_admin=is_admin,
            created_by=created_by,
            created_at=datetime.now(tz=UTC),
        )
        await self._store.create_api_key(record)
        return record, secret

    async def resolve_api_key(self, secret: str) -> Identity | None:
        record = await self._store.get_api_key_by_hash(sha256(secret))
        if record is None or record.disabled:
            return None
        await self._store.touch_api_key(record.id)
        return Identity("api_key", record.name, f"cle d'API {record.name}", record.is_admin)

    async def list_api_keys(self) -> list[dict[str, object]]:
        return [record.to_public() for record in await self._store.list_api_keys()]

    async def delete_api_key(self, key_id: str) -> bool:
        return await self._store.delete_api_key(key_id)

    async def purge_expired_sessions(self) -> int:
        return await self._store.purge_expired_sessions()


def cookie_secure_default(app_env: str) -> bool:
    """Cookie ``Secure`` par defaut partout SAUF en lab/dev/test.

    En HTTPS le cookie ne doit voyager qu'en clair chiffre ; en lab sur HTTP, un
    cookie ``Secure`` ne serait tout simplement jamais renvoye par le navigateur,
    rendant la connexion impossible. On adapte donc au contexte, sans jamais
    imposer un defaut dangereux en production.
    """
    return app_env.strip().lower() not in {"lab", "dev", "test", "local"}

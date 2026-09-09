"""Configuration de l'application.

Tout est pilote par variables d'environnement / .env (pydantic-settings).
Les secrets ne sont JAMAIS ecrits dans l'inventaire : un routeur declare le *nom*
de la variable d'environnement qui porte son mot de passe (``password_env``).
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingSecretError(RuntimeError):
    """Un secret reference par la config est absent de l'environnement."""


class RouterRole(StrEnum):
    """Role du routeur dans la topologie, il determine ce qu'on y collecte."""

    POP = "pop"  # concentrateur PPPoE : dernier km par abonne
    CORE = "core"  # coeur de reseau
    GATEWAY = "gateway"  # sortie internet : egress (phase 2, optionnel)


class RouterConfig(BaseModel):
    """Un routeur MikroTik interroge par le collecteur.

    Phase 1 : lecture seule (compte ``qos-ro``). Les champs ``rw_*`` sont declares
    pour la phase 2 (push des files CAKE) mais ne sont jamais utilises ici.
    """

    name: str
    host: str
    port: int = 8728
    username: str = "qos-ro"
    password: SecretStr | None = None
    password_env: str | None = None
    role: RouterRole = RouterRole.POP
    pop_name: str | None = None
    enabled: bool = True
    timeout_s: float = 5.0
    use_ssl: bool = False

    # RouterOS cree une interface dynamique par session PPPoE. Son nom par defaut
    # est "<pppoe-LOGIN>" : c'est la seule facon d'obtenir les compteurs d'octets,
    # /ppp/active/print ne les expose pas.
    pppoe_interface_pattern: str = "<pppoe-{login}>"

    # --- Phase 2 : enforcement (declare, non utilise) ---
    rw_username: str | None = None
    rw_password_env: str | None = None

    @property
    def effective_pop_name(self) -> str:
        return self.pop_name or self.name

    def resolve_password(self) -> str:
        """Retourne le mot de passe de lecture, depuis l'env en priorite."""
        if self.password_env:
            value = os.environ.get(self.password_env)
            if value is None or value == "":
                raise MissingSecretError(
                    f"routeur '{self.name}': variable d'environnement "
                    f"'{self.password_env}' absente ou vide"
                )
            return value
        if self.password is not None:
            return self.password.get_secret_value()
        raise MissingSecretError(
            f"routeur '{self.name}': ni 'password_env' ni 'password' n'est defini"
        )


class BackhaulConfig(BaseModel):
    """Un lien radio backhaul. LECTURE SEULE : on ne pilote jamais la radio."""

    name: str
    pop_name: str
    uisp_device_id: str | None = None
    nominal_capacity_mbps: float | None = None
    enabled: bool = True


class Inventory(BaseModel):
    """Contenu du fichier d'inventaire (YAML ou JSON)."""

    routers: list[RouterConfig] = Field(default_factory=list)
    backhauls: list[BackhaulConfig] = Field(default_factory=list)


def _load_inventory_file(path: Path) -> Inventory:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yml", ".yaml"}:
        import yaml  # dependance seulement si l'inventaire est en YAML

        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw or "{}")
    if isinstance(data, list):  # tolerance : un fichier contenant juste la liste
        data = {"routers": data}
    return Inventory.model_validate(data)


class Settings(BaseSettings):
    """Configuration globale du controleur."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "freeQoS"
    app_env: str = "lab"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # --- Base de donnees ---
    database_url: str = "postgresql://qos:changeme@localhost:5432/qos"
    db_pool_min: int = 1
    db_pool_max: int = 8
    db_auto_migrate: bool = True
    # Cree la base nommee dans DATABASE_URL si elle n'existe pas encore.
    db_auto_create: bool = True
    db_command_timeout_s: float = 15.0

    # --- Politiques Timescale (0 = desactive) ---
    chunk_interval_hours: int = 24
    compression_after_days: int = 7
    retention_days: int = 90

    # --- Scheduler (boucle centrale lente) ---
    scheduler_enabled: bool = True
    subscriber_interval_s: float = 10.0
    backhaul_interval_s: float = 30.0
    # Compteurs des ports, d'ou vient le debit des liens. Deux lectures
    # (/interface et /interface/ethernet) par routeur et par cycle : la charge
    # est proportionnelle au nombre de PoPs, pas au nombre d'abonnes. Mettre 0
    # coupe la mesure sans toucher au reste de la collecte.
    link_interval_s: float = 10.0
    plan_refresh_interval_s: float = 300.0
    # Filet de securite : recharge l'inventaire meme si une modification a ete
    # faite hors de cette instance (edition directe en base, seconde instance).
    inventory_refresh_interval_s: float = 60.0

    # --- Sonde de latence (phase 3 amorcee) ---
    # DESACTIVEE par defaut : c'est une sonde ACTIVE (/ping depuis le routeur),
    # elle consomme du CPU routeur, contrairement a la mesure passive de LibreQoS
    # qui est impossible hors-bande.
    rtt_enabled: bool = False
    rtt_interval_s: float = 30.0
    rtt_batch_size: int = 20
    rtt_count: int = 2
    # Au-dela, une mesure n'est plus rattachee aux echantillons.
    rtt_max_age_s: float = 300.0

    # --- Inventaire ---
    routers: list[RouterConfig] = Field(default_factory=list)
    backhauls: list[BackhaulConfig] = Field(default_factory=list)
    routers_file: Path | None = None

    # --- Capacite backhaul ---
    backhaul_provider: Literal["mock", "uisp"] = "mock"
    uisp_base_url: str | None = None
    uisp_token: SecretStr | None = None
    uisp_verify_tls: bool = True
    uisp_timeout_s: float = 10.0
    mock_backhaul_capacity_mbps: float = 450.0
    mock_backhaul_variation_pct: float = 35.0
    mock_backhaul_period_s: float = 600.0
    mock_backhaul_seed: int = 1337

    # --- Plans abonnes ---
    plan_provider: Literal["mock", "freeradius_sql"] = "mock"
    radius_dsn: str | None = None
    radius_rate_attribute: str = "Mikrotik-Rate-Limit"
    radius_default_down_mbps: float = 100.0
    radius_default_up_mbps: float = 20.0

    # --- Secrets ---
    # Cle Fernet protegeant les mots de passe des routeurs ajoutes depuis
    # l'interface. Sans elle, l'API refuse d'en enregistrer (elle n'ecrira jamais
    # un secret en clair). Generer avec : python -m app.services.crypto
    app_secret_key: str | None = None
    # Ou persister la cle si APP_SECRET_KEY n'est pas fourni. Elle DOIT survivre
    # aux redemarrages : une cle regeneree rendrait illisibles tous les mots de
    # passe deja stockes.
    app_secret_key_file: Path | None = Path("data/secret.key")
    app_secret_key_autogenerate: bool = True

    # --- Shaping (phase 2) ---
    # On shape SOUS la capacite reelle pour que la file se forme dans CAKE, ou on
    # la controle, plutot que dans le buffer de la radio, ou on ne peut rien.
    # C'est le principe commun a Preseem et LibreQoS.
    shaping_safety_factor: float = 0.90
    shaping_floor_mbps: float = 5.0
    # Supprimer nos files devenues inutiles. A desactiver pendant une migration.
    shaping_prune: bool = True
    # Aligner le debit d'une file tierce deja posee sur la cible d'un abonne.
    #
    # RouterOS n'applique que la PREMIERE file d'une meme cible : creer la notre
    # a cote d'une file heritee ne briderait rien du tout. On envoie donc un
    # simple '/queue/simple/set <id> max-limit=...' sur la file en place. Elle
    # n'est ni renommee, ni reparentee, ni marquee, ni supprimable par le
    # controleur : seul son debit change.
    shaping_adopt_foreign_queues: bool = True
    # Poser une file des la DECOUVERTE d'un lien, avant toute mesure.
    #
    # Elle vise le segment L3 du lien (172.16.38.0/23) et nait ILLIMITEE
    # (max-limit=0/0) : elle ne bride rien, mais elle existe, elle porte les
    # files des abonnes qui passent par ce lien, et l'exploitant n'a plus qu'a
    # fixer son debit dans l'interface. A false, un lien sans capacite connue
    # reste sans file.
    shaping_queue_for_detected_links: bool = True
    # Sur quoi accrocher la file d'un abonne.
    #
    # "address" (defaut) : target=10.20.0.10/32. L'adresse de la session en
    #   cours, relue sur le routeur a chaque plan. Le sens est celui du client
    #   (max-limit=montant/descendant), et un abonne hors ligne n'a pas de file.
    # "interface" : target=<pppoe-login>. Deconseille -- l'interface dynamique
    #   est recreee a chaque reconnexion, et RouterOS INVERSE alors le sens des
    #   deux limites. Conserve pour un parc qui en depend deja.
    subscriber_queue_target: Literal["address", "interface"] = "address"
    # Encapsulation a compter dans CAKE. PPPoE sur ethernet = 8 + 14 octets ;
    # ajouter 4 par etiquette VLAN, 4 par label MPLS. Sous-estimer revient a
    # shaper au-dessus de la capacite du lien, ce qui annule l'AQM.
    cake_overhead: int = 22
    cake_rtt_ms: int = 50
    # Coupe-circuit : un plan anormalement gros signale un etat desire mal
    # calcule, il vaut mieux s'arreter que de reecrire tout un PoP.
    enforcement_max_actions: int = 500
    # Interdit la bascule depuis l'interface : seul un redemarrage avec
    # ENFORCEMENT_ENABLED modifie peut alors autoriser l'ecriture.
    enforcement_locked: bool = False
    # Exige un compte d'ecriture DISTINCT (rw_username). Desactive par defaut :
    # beaucoup d'exploitants se connectent deja avec un compte qui possede la
    # politique 'write', et refuser sur la seule absence de declaration
    # reviendrait a ignorer les droits reels.
    require_separate_write_account: bool = False
    # Verifie l'echeance des boosts et ramene les files a leur debit normal.
    boost_check_interval_s: float = 30.0
    # Reapplique l'etat desire sur tous les routeurs, sans intervention.
    #
    # Sans cette boucle, un debit saisi dans l'interface reste une INTENTION :
    # il n'atteint le routeur que si quelqu'un pense a demander un plan puis a
    # l'appliquer. Et une file posee hier vise l'adresse d'hier, donc l'abonne
    # qui s'est reconnecte depuis n'est plus bride du tout.
    #
    # Ne fait rien tant que ENFORCEMENT_ENABLED est faux : c'est ce drapeau, et
    # lui seul, qui autorise une ecriture. Mettre 0 desactive la boucle.
    shaping_reconcile_interval_s: float = 120.0
    topology_refresh_interval_s: float = 900.0

    # --- Garde-fous ---
    # Phase 2 uniquement : aucune ecriture n'est implementee aujourd'hui.
    enforcement_enabled: bool = False
    max_plausible_bps: float = 100_000_000_000.0
    # En dessous de ce delta on ne calcule pas de debit (bruit de division).
    min_rate_interval_s: float = 1.0

    @field_validator("routers", "backhauls", mode="before")
    @classmethod
    def _parse_json_list(cls, value: Any) -> Any:
        """Autorise ROUTERS='[{...}]' en variable d'environnement."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            return json.loads(value)
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _merge_inventory_file(self) -> Settings:
        """Fusionne l'inventaire fichier avec ce qui vient de l'environnement.

        L'environnement gagne : un routeur declare inline avec le meme nom
        remplace celui du fichier (pratique pour surcharger une IP en lab).
        """
        if self.routers_file is None:
            return self
        path = Path(self.routers_file)
        if not path.exists():
            # Absent = pas bloquant : la stack doit demarrer meme sans inventaire,
            # l'API de lecture et /health restent utiles.
            return self
        inventory = _load_inventory_file(path)

        by_name = {r.name: r for r in inventory.routers}
        by_name.update({r.name: r for r in self.routers})
        self.routers = list(by_name.values())

        bh_by_name = {b.name: b for b in inventory.backhauls}
        bh_by_name.update({b.name: b for b in self.backhauls})
        self.backhauls = list(bh_by_name.values())
        return self

    @property
    def enabled_routers(self) -> list[RouterConfig]:
        return [r for r in self.routers if r.enabled]

    @property
    def enabled_backhauls(self) -> list[BackhaulConfig]:
        return [b for b in self.backhauls if b.enabled]

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg n'accepte pas les schemas SQLAlchemy (postgresql+asyncpg://)."""
        dsn = self.database_url
        if "+" in dsn.split("://", 1)[0]:
            scheme, rest = dsn.split("://", 1)
            dsn = f"{scheme.split('+', 1)[0]}://{rest}"
        return dsn


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Utilise par les tests pour recharger la configuration."""
    get_settings.cache_clear()

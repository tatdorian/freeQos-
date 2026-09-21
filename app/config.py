"""Configuration de l'application.

Tout est pilote par variables d'environnement / .env (pydantic-settings).
Les secrets ne sont JAMAIS ecrits dans l'inventaire : un routeur declare le *nom*
de la variable d'environnement qui porte son mot de passe (``password_env``).
"""

from __future__ import annotations

import ipaddress
import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.flows import RESEAUX_CLIENTS_PAR_DEFAUT


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
    # Verification TLS quand use_ssl est vrai, PAR ROUTEUR (defaut : la plus
    # sure). "strict" verifie chaine + nom d'hote ; "fingerprint" epingle
    # l'empreinte SHA-256 (certificats auto-signes des CHR) ; "insecure" desactive
    # la verification -- un choix assume, jamais un defaut cache.
    tls_verify: Literal["strict", "fingerprint", "insecure"] = "strict"
    tls_fingerprint: str | None = None

    # IDENTITE DU ROUTEUR DANS LA TOPOLOGIE.
    #
    # Dans un reseau d'operateur, chaque routeur porte une adresse de loopback
    # unique, independante de toute interface physique. C'est elle -- et rien
    # d'autre -- qui dit "ce routeur est CE routeur" : son nom peut changer, ses
    # IP d'interface sont partagees avec le voisin d'en face (un /30 appartient
    # aux deux bouts), sa MAC depend du port par lequel on le regarde.
    #
    # Declaree ici, elle fait autorite. Laissee vide, le controleur la deduit
    # (adresse /32 sur une interface 'lo*', ou router-id de l'export) et
    # l'affiche pour que l'operateur puisse la corriger.
    loopback: str | None = None

    # RouterOS cree une interface dynamique par session PPPoE. Son nom par defaut
    # est "<pppoe-LOGIN>" : c'est la seule facon d'obtenir les compteurs d'octets,
    # /ppp/active/print ne les expose pas.
    pppoe_interface_pattern: str = "<pppoe-{login}>"

    # --- Phase 2 : enforcement (declare, non utilise) ---
    rw_username: str | None = None
    rw_password_env: str | None = None

    @model_validator(mode="after")
    def _check_loopback(self) -> RouterConfig:
        """Un loopback designe UNE machine, jamais un reseau.

        Accepte la forme nue comme la forme /32 et normalise : c'est une cle de
        rapprochement, elle doit se comparer sans ambiguite.
        """
        if self.loopback is None:
            return self
        texte = str(self.loopback).strip()
        if not texte:
            self.loopback = None
            return self
        try:
            interface = ipaddress.ip_interface(texte)
        except ValueError as exc:
            raise ValueError(f"loopback invalide : {texte}") from exc
        if interface.network.prefixlen != interface.ip.max_prefixlen:
            raise ValueError(
                f"loopback {texte} : un loopback est une adresse d'hote "
                f"(/{interface.ip.max_prefixlen}), pas un reseau"
            )
        if interface.ip.is_unspecified or interface.ip.is_loopback:
            raise ValueError(f"loopback inutilisable : {texte}")
        self.loopback = str(interface.ip)
        return self

    @model_validator(mode="after")
    def _check_tls(self) -> RouterConfig:
        """Une empreinte est obligatoire en mode ``fingerprint``, et doit etre un
        SHA-256 valide. Mieux vaut echouer au chargement qu'a la connexion."""
        if self.tls_verify == "fingerprint":
            from app.services.tls import TlsConfigurationError, normalise_fingerprint

            if not self.tls_fingerprint:
                raise ValueError(
                    f"routeur '{self.name}' : tls_verify=fingerprint exige tls_fingerprint"
                )
            try:
                self.tls_fingerprint = normalise_fingerprint(self.tls_fingerprint)
            except TlsConfigurationError as exc:
                raise ValueError(str(exc)) from exc
        return self

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

    # --- Antenne interrogee DIRECTEMENT (provider airos, sans UISP) ---
    # Adresse de management de la radio Ubiquiti. Quand elle est renseignee et que
    # BACKHAUL_PROVIDER=airos, la capacite est lue sur /status.cgi de cette
    # antenne. Les identifiants tombent sur les valeurs globales AIROS_* si on ne
    # les precise pas ici (compte lecture commun a tout le parc).
    api_host: str | None = None
    api_username: str | None = None
    api_password_env: str | None = None
    api_verify_tls: bool | None = None

    @property
    def airos_key(self) -> str:
        """Cle stable de la radio : son uisp_device_id, ou son nom a defaut."""
        return self.uisp_device_id or self.name

    def resolve_api_password(self, fallback: str | None = None) -> str | None:
        """Mot de passe de l'antenne, depuis l'env en priorite.

        Comme pour les routeurs, jamais de secret en clair dans l'inventaire : on
        declare le NOM d'une variable d'environnement. A defaut, on retombe sur le
        mot de passe global AIROS_PASSWORD.
        """
        if self.api_password_env:
            value = os.environ.get(self.api_password_env)
            if value is None or value == "":
                raise MissingSecretError(
                    f"antenne '{self.name}': variable d'environnement "
                    f"'{self.api_password_env}' absente ou vide"
                )
            return value
        return fallback


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

    # --- Detection des clients sur VLAN routee (aide a la saisie) ---
    #
    # COUPEE PAR DEFAUT, ET C'EST UN CHOIX. Les clients sur VLAN routee se
    # DECLARENT A LA MAIN : ils n'ouvrent aucune session, RADIUS ne les decrit
    # pas, et rien sur le reseau ne dit quel debit a ete vendu a quelle adresse.
    # Une adresse qui parle sur une VLAN peut etre un client, une imprimante,
    # une camera ou l'equipement d'un autre operateur : toutes laissent la meme
    # trace. Une liste automatique donne donc l'illusion d'un inventaire sans en
    # etre un, et fait perdre du temps a trier plutot qu'a saisir.
    #
    # L'activer ajoute une lecture de /ip/arp par routeur : une AIDE a la
    # declaration, rien de plus. Rien de ce qu'elle trouve n'est jamais shape.
    # Les flux NetFlow rendent le meme service sans rien demander aux routeurs
    # (cf. GET /netflow/hosts).
    vlan_detect_enabled: bool = False
    vlan_detect_interval_s: float = 300.0
    # Au-dela, une adresse qui s'est tue n'est plus une piste : on l'oublie.
    vlan_sighting_retention_s: float = 86_400.0
    # Plafond de candidats remontes a l'interface ET poses dans le graphe. Une
    # VLAN bavarde ne doit pas noyer l'arbre sous des centaines de cases.
    vlan_candidate_limit: int = 200

    # --- NetFlow : mesure du trafic sans etre sur le chemin des paquets ---
    #
    # OU LE CONTROLEUR SE PLACE. Il ecoute, il ne sonde rien. Les exporteurs
    # sont declares AUX DEUX EXTREMITES du reseau et jamais au milieu :
    #
    #   - en amont du coeur, a la sortie internet (vantage 'edge') : tout ce qui
    #     vient d'internet et tout ce qui y va passe la, une seule fois ;
    #   - au PoP (vantage 'pop') : le meme trafic, mais la ou l'etiquette VLAN
    #     et le secteur existent encore.
    #
    # Le coeur, entre les deux, n'exporte rien et n'est pas interroge : la
    # mesure ne lui ajoute aucune charge, ni a l'aller ni au retour. C'est tout
    # l'interet du flux exporte -- un miroir de port recopierait chaque octet
    # sur le lien de collecte, dans les deux sens.
    # ACTIVE PAR DEFAUT. Le collecteur ECOUTE : il n'emet rien, n'interroge
    # aucun equipement et n'ajoute aucune charge au reseau. Tant qu'aucun
    # routeur n'exporte vers lui, il ne fait rien de plus qu'ouvrir un port UDP
    # -- alors qu'a l'inverse, laisser l'ecoute coupee par defaut faisait perdre
    # DEFINITIVEMENT le trafic exporte pendant tout le temps ou personne ne
    # s'apercevait que le drapeau existait. Un datagramme non recu ne se
    # rattrape pas.
    netflow_enabled: bool = True
    netflow_bind: str = "0.0.0.0"  # noqa: S104 - un collecteur ecoute sur tous les liens
    netflow_port: int = 2055
    # Fin de fenetre : on ecrit UNE ligne par abonne et par fenetre, pas une par
    # flux. Descendre sous 30 s multiplie les lignes sans rien apprendre de plus.
    netflow_flush_interval_s: float = 60.0
    # LE MEME OCTET EST VU DEUX FOIS (au PoP puis a la sortie internet). Les
    # additionner doublerait la consommation de chacun : la lecture ne retient
    # qu'un point de mesure, et c'est celui-ci.
    netflow_accounting_vantage: Literal["edge", "pop"] = "edge"
    # Espace d'adressage ou vivent les clients. Sert a decider si une adresse non
    # rattachee merite d'etre proposee a la saisie : sans ce filtre, chaque
    # serveur contacte sur internet apparaitrait comme un candidat.
    netflow_customer_networks: list[str] = Field(
        default_factory=lambda: list(RESEAUX_CLIENTS_PAR_DEFAUT)
    )
    # Retenir les adresses non rattachees. C'est ce qui alimente l'aide a la
    # declaration des clients VLAN. A false, on ne garde que les abonnes connus.
    netflow_track_hosts: bool = True
    netflow_host_limit: int = 500
    netflow_host_retention_s: float = 86_400.0
    # Retenir l'adresse DISTANTE atteinte par chaque abonne. C'est ce qui
    # alimente "qui se connecte a quoi" (onglet Services), l'enrichissement, et
    # de la les restrictions de trafic. Le couper ne touche PAS a la mesure de
    # volume par abonne, qui continue exactement comme avant.
    netflow_track_destinations: bool = True
    # Plafond de destinations retenues par fenetre. Un seul abonne en p2p peut
    # toucher des milliers d'adresses en une minute ; sans plafond, une fenetre
    # de collecte deviendrait une rafale d'ecritures en base.
    netflow_destination_limit: int = 2_000
    # Au-dela, une destination qui ne repond plus est oubliee. SEULE LA MESURE
    # est purgee : ce qu'on a appris de l'adresse (son nom, son service) reste.
    netflow_destination_retention_s: float = 604_800.0

    # --- Export NetFlow pose par le controleur lui-meme ---
    #
    # Sans export, le collecteur n'a rien a mesurer -- et poser deux commandes
    # sur CHAQUE routeur a la main veut dire qu'un PoP oublie reste silencieux
    # sans que rien ne le signale. Le controleur a deja un acces en ecriture
    # gouverne et trace : il pose donc l'export lui-meme, sous les memes
    # garde-fous (ENFORCEMENT_ENABLED, plan affichable, audit).
    netflow_export_auto: bool = True
    netflow_export_interval_s: float = 600.0
    netflow_export_version: int = 9
    netflow_export_interfaces: str = "all"
    # CE QUI DECIDE EN COMBIEN DE TEMPS UN FLUX DEVIENT VISIBLE. Le defaut
    # RouterOS n'exporte un flux ENCORE ACTIF qu'au bout de trente minutes : un
    # streaming en cours n'apparait pas avant une demi-heure, et le routeur
    # s'affiche pourtant comme parfaitement configure.
    netflow_export_active_timeout: str = "1m"
    netflow_export_inactive_timeout: str = "15s"
    # Adresse annoncee aux routeurs. VIDE = deduite routeur par routeur, en
    # demandant au noyau quelle adresse source il utiliserait pour joindre ce
    # routeur. Sur un controleur multi-interfaces, une valeur saisie a la main
    # serait fausse pour une partie du parc.
    netflow_collector_address: str | None = None

    # --- ipfinder : qui se cache derriere une adresse atteinte ---
    #
    # Trois sources, par cout croissant :
    #   1. le CATALOGUE embarque (blocs publies par les operateurs de service).
    #      Gratuit, instantane, fonctionne sans acces internet ;
    #   2. le NOM INVERSE (PTR). Une requete DNS par adresse nouvelle. C'est ce
    #      qui suit un service qui change de prefixe, et ce qui distingue
    #      YouTube du reste de Google ;
    #   3. RDAP. Organisation, AS, pays. COUPE PAR DEFAUT : c'est le seul appel
    #      sortant que ce controleur emettrait, et un reseau souverain a le
    #      droit de ne pas en vouloir.
    ipfinder_enabled: bool = True
    ipfinder_rdns_enabled: bool = True
    ipfinder_rdap_enabled: bool = False
    ipfinder_rdap_url: str = "https://rdap.org/ip/"
    ipfinder_interval_s: float = 30.0
    ipfinder_batch_size: int = 40
    ipfinder_concurrency: int = 8
    ipfinder_timeout_s: float = 2.0
    # Au-dela, on cesse de redemander : la majorite d'internet n'a pas de nom
    # inverse, et insister ferait une requete perpetuelle par adresse muette.
    ipfinder_max_attempts: int = 3

    # --- Restrictions de trafic ---
    #
    # La boucle qui rend une regle VIVANTE : elle recalcule les adresses de
    # chaque restriction (catalogue + ce que NetFlow a decouvert) et pousse la
    # difference sur les routeurs. Soumise a ENFORCEMENT_ENABLED comme toute
    # ecriture ; elle ne fait rien tant qu'il est faux.
    restrictions_interval_s: float = 300.0
    # Plafond d'adresses par regle. Une liste que le routeur parcourt a chaque
    # paquet ne doit pas grossir sans limite parce qu'un service a beaucoup de
    # serveurs.
    restriction_address_limit: int = 5_000

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
    # mock  = simulateur ; uisp = controleur UISP centralise ; airos = API locale
    # de chaque antenne Ubiquiti (aucun UISP requis).
    backhaul_provider: Literal["mock", "uisp", "airos"] = "mock"
    uisp_base_url: str | None = None
    uisp_token: SecretStr | None = None
    uisp_verify_tls: bool = True
    uisp_timeout_s: float = 10.0
    # Identifiants par defaut des antennes airOS, si un backhaul ne les precise
    # pas lui-meme. Un compte lecture commun a tout le parc suffit souvent.
    airos_username: str | None = None
    airos_password: SecretStr | None = None
    airos_verify_tls: bool = False
    airos_timeout_s: float = 10.0
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
    # Options CAKE avancees. Le rendu RouterOS existe deja (QueueTypeSpec) ; il
    # ne restait qu'a les exposer. None = on ne pose pas le champ (defaut RouterOS).
    #
    # cake_diffserv   : classes de priorite selon le DSCP. "diffserv4" protege la
    #                   voix et le jeu ; "besteffort" ignore le DSCP.
    # cake_flowmode   : isolation des flux. "triple-isolate" est le bon defaut ;
    #                   "dual-dsthost" par abonne derriere le lien.
    # cake_nat        : DETERMINANT derriere CGNAT/PPPoE -- CAKE resout la NAT
    #                   pour isoler les hotes reels et non la seule IP publique.
    # cake_ack_filter : "filter" allege les ACK sur un lien tres asymetrique.
    # cake_wash       : remet le DSCP a zero en sortie. NECESSAIRE quand le DSCP
    #                   entrant n'est pas fiable (marquage client arbitraire).
    # cake_mpu        : taille de paquet minimale facturee (cadrage ATM/PPPoE).
    cake_diffserv: str | None = None
    cake_flowmode: str | None = None
    cake_nat: bool | None = None
    cake_ack_filter: str | None = None
    cake_wash: bool | None = None
    cake_mpu: int | None = None
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

    # --- Boucle fermee QoE (phase 4) ---
    #
    # Jusqu'ici la seule grandeur qui refermait une boucle etait la capacite
    # backhaul mesuree : le planificateur pose la file parent a
    # mesure * SHAPING_SAFETY_FACTOR. Elle ne voit pas un secteur dont la latence
    # GONFLE sous charge alors que la radio annonce toujours sa capacite.
    #
    # Ce job lit le score de QoE composite (bufferbloat + latence a vide, la MEME
    # fonction que la heatmap Executif) et, quand un secteur decroche, resserre
    # l'enveloppe PARTAGEE de ce secteur -- jamais le plan souscrit d'un abonne.
    #
    # Trois verrous, les memes que pour la reconciliation : rien n'est ecrit tant
    # que ENFORCEMENT_ENABLED est faux, JAMAIS de purge, et le job est inerte
    # tant que la sonde RTT ne fournit pas de latence a correler. Mettre 0
    # desactive la boucle.
    qoe_loop_interval_s: float = 300.0
    # Fenetre d'observation. Trop courte, on reagit a un pic ; trop longue, on
    # reagit a de l'histoire ancienne. Doit couvrir plusieurs tours de sonde RTT.
    qoe_window_minutes: int = 15
    # En dessous de ce score (0..100), l'abonne est considere degrade. 55 tombe
    # dans la bande "warn" : en pratique, un bufferbloat de note C ou pire.
    qoe_score_threshold: float = 55.0
    # Combien d'abonnes degrades il faut dans un secteur pour incriminer LE
    # SECTEUR. Un seul abonne qui gonfle, c'est son propre dernier km (CPE, wifi
    # domestique) : c'est la correlation entre plusieurs abonnes qui accuse le
    # partage. Mettre 1 rend la boucle sensible a un abonne isole.
    qoe_min_degraded_subscribers: int = 2
    # Un cran de resserrage, en fraction de la capacite du lien.
    qoe_trim_step: float = 0.10
    # Jamais en dessous de cette fraction : au-dela, le goulot n'est plus le
    # buffer radio mais bien la capacite, et resserrer encore ne ferait que
    # brider un secteur deja a genoux.
    qoe_trim_floor: float = 0.50
    # Cycles consecutifs de QoE saine avant de rendre UN cran. On resserre vite,
    # on relache lentement : sans cette asymetrie la boucle oscille.
    qoe_recovery_cycles: int = 3

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

    @model_validator(mode="after")
    def _check_qoe_loop(self) -> Settings:
        """Garde-fous de la boucle fermee.

        Un pas nul ne resserrerait jamais rien, un plancher a zero autoriserait a
        couper un secteur : mieux vaut refuser au chargement qu'a la premiere
        degradation, quand plus personne ne regarde la configuration.
        """
        if not 0.0 < self.qoe_trim_step <= 0.5:
            raise ValueError("QOE_TRIM_STEP doit etre dans ]0, 0.5] (un cran de resserrage)")
        if not 0.1 <= self.qoe_trim_floor <= 1.0:
            raise ValueError(
                "QOE_TRIM_FLOOR doit etre dans [0.1, 1.0] : la boucle ne coupe jamais un secteur"
            )
        if self.qoe_recovery_cycles < 1:
            raise ValueError("QOE_RECOVERY_CYCLES doit valoir au moins 1")
        if self.qoe_min_degraded_subscribers < 1:
            raise ValueError("QOE_MIN_DEGRADED_SUBSCRIBERS doit valoir au moins 1")
        return self

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

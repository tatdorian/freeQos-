"""Reglages pilotes par la BASE, pas par l'environnement.

PRINCIPE
--------
L'environnement ne sert plus qu'a AMORCER une valeur au tout premier demarrage.
Des qu'une valeur existe en base, c'est elle qui fait foi, et elle se change
depuis l'interface sans redemarrer le controleur. C'est exactement la regle deja
appliquee au drapeau d'enforcement, generalisee a tous les reglages
d'exploitation.

COMMENT LA VALEUR ATTEINT LE CODE
---------------------------------
On ne duplique pas un second objet de configuration : on ECRIT la valeur dans
l'objet ``Settings`` deja partage par tout le controleur. Consequence voulue,
tous les consommateurs existants (``self.settings.cake_nat``,
``settings.shaping_safety_factor``...) voient la valeur de la base sans une
seule ligne a changer chez eux, et la prise d'effet est immediate :

  - les options de shaping/CAKE sont relues a chaque calcul de plan ;
  - les cadences sont relues par le scheduler a chaque tour de boucle.

CE QUI RESTE DANS L'ENVIRONNEMENT, ET POURQUOI
----------------------------------------------
Tout ce qu'il faut connaitre AVANT de pouvoir lire la base : l'URL de la base
elle-meme, la cle de chiffrement, le fichier d'inventaire, le port d'ecoute.
Les y chercher en base serait circulaire. Tout le reste est ici.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.services.intel import JOB_INTEL
from app.services.netflow_export import JOB_NETFLOW_EXPORT
from app.services.restrictions import JOB_RESTRICTIONS

# Cadence minimale : une valeur nulle ou negative ferait tourner la boucle du
# scheduler a vide, sans jamais dormir.
INTERVAL_MIN_S = 1.0


class ReglageInconnuError(KeyError):
    """Le reglage demande n'est pas pilotable depuis la base."""


class ValeurInvalideError(ValueError):
    """La valeur proposee ne respecte pas le contrat du reglage."""


@dataclass(frozen=True)
class Reglage:
    """Un reglage modifiable a chaud, et ce qu'on accepte comme valeur."""

    name: str
    group: str
    kind: str  # "bool" | "int" | "float" | "choix"
    help: str
    nullable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    # Job du scheduler dont ce reglage porte la cadence, s'il y en a un : le
    # changer doit reprogrammer la boucle, pas seulement la valeur affichee.
    job: str | None = None

    def coerce(self, value: Any) -> Any:
        """Ramene une valeur venue de JSON (ou d'un formulaire) au type attendu."""
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if not self.nullable:
                raise ValeurInvalideError(f"{self.name} : une valeur est obligatoire")
            return None

        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            texte = str(value).strip().lower()
            if texte in {"true", "1", "oui", "yes", "on"}:
                return True
            if texte in {"false", "0", "non", "no", "off"}:
                return False
            raise ValeurInvalideError(f"{self.name} : booleen attendu, recu {value!r}")

        if self.kind == "choix":
            texte = str(value).strip()
            if texte not in self.choices:
                raise ValeurInvalideError(
                    f"{self.name} : valeur {texte!r} hors des choix ({', '.join(self.choices)})"
                )
            return texte

        try:
            nombre: float | int = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError) as exc:
            raise ValeurInvalideError(f"{self.name} : nombre attendu, recu {value!r}") from exc
        if self.minimum is not None and nombre < self.minimum:
            raise ValeurInvalideError(f"{self.name} : minimum {self.minimum}, recu {nombre}")
        if self.maximum is not None and nombre > self.maximum:
            raise ValeurInvalideError(f"{self.name} : maximum {self.maximum}, recu {nombre}")
        return nombre


def _cadence(name: str, job: str, aide: str) -> Reglage:
    return Reglage(
        name=name,
        group="cadences",
        kind="float",
        help=aide,
        minimum=INTERVAL_MIN_S,
        maximum=86_400.0,
        job=job,
    )


# Registre des reglages pilotables depuis la base. Tout ce qui n'est pas ici
# reste fixe au demarrage (cf. l'en-tete du module).
REGLAGES: tuple[Reglage, ...] = (
    # --- Shaping ---
    Reglage(
        "shaping_safety_factor",
        "shaping",
        "float",
        "Part de la capacite mesuree reellement appliquee. On shape SOUS la "
        "capacite reelle pour que la file se forme dans CAKE, pas dans le buffer "
        "de la radio.",
        minimum=0.1,
        maximum=1.0,
    ),
    Reglage(
        "shaping_floor_mbps",
        "shaping",
        "float",
        "Plancher de debit d'un lien : un fade profond ne doit pas le couper a zero.",
        minimum=0.0,
        maximum=10_000.0,
    ),
    Reglage(
        "shaping_prune",
        "shaping",
        "bool",
        "Supprimer nos files devenues inutiles (abonne parti, lien retire). "
        "A couper pendant une migration.",
    ),
    Reglage(
        "shaping_adopt_foreign_queues",
        "shaping",
        "bool",
        "Aligner le debit d'une file TIERCE deja posee sur la cible d'un abonne. "
        "RouterOS n'applique que la premiere file d'une meme cible : sans cela, "
        "la limite saisie serait purement decorative.",
    ),
    Reglage(
        "shaping_queue_for_detected_links",
        "shaping",
        "bool",
        "Poser une file des la decouverte d'un lien, en illimite (0/0). Elle ne "
        "bride rien mais sert de parent aux files des abonnes.",
    ),
    Reglage(
        "subscriber_queue_target",
        "shaping",
        "choix",
        "Sur quoi accrocher la file d'un abonne. 'address' (conseille) vise "
        "l'adresse de la session ; 'interface' est deconseille (RouterOS inverse "
        "le sens des deux limites).",
        choices=("address", "interface"),
    ),
    # --- CAKE ---
    Reglage(
        "cake_overhead",
        "cake",
        "int",
        "Encapsulation comptee par CAKE, en octets. PPPoE sur ethernet = 8 + 14. "
        "Ajouter 4 par etiquette VLAN, 4 par label MPLS. Sous-estimer revient a "
        "shaper au-dessus de la capacite du lien.",
        minimum=0,
        maximum=200,
    ),
    Reglage(
        "cake_rtt_ms",
        "cake",
        "int",
        "RTT de reference de l'AQM, en millisecondes.",
        minimum=1,
        maximum=1000,
    ),
    Reglage(
        "cake_diffserv",
        "cake",
        "choix",
        "Classes de priorite selon le DSCP. 'diffserv4' protege la voix et le jeu ; "
        "'besteffort' ignore le DSCP. Vide = defaut RouterOS.",
        nullable=True,
        choices=("besteffort", "diffserv3", "diffserv4", "diffserv8", "precedence"),
    ),
    Reglage(
        "cake_flowmode",
        "cake",
        "choix",
        "Isolation des flux. 'triple-isolate' est le bon defaut general.",
        nullable=True,
        choices=(
            "flow",
            "flows",
            "src-host",
            "dst-host",
            "hosts",
            "triple-isolate",
            "dual-src-host",
            "dual-dst-host",
        ),
    ),
    Reglage(
        "cake_nat",
        "cake",
        "bool",
        "Resoudre la NAT pour isoler les hotes reels et non la seule IP publique. "
        "DETERMINANT derriere du CGNAT ou du PPPoE.",
        nullable=True,
    ),
    Reglage(
        "cake_ack_filter",
        "cake",
        "choix",
        "Allege les ACK sur un lien tres asymetrique.",
        nullable=True,
        choices=("none", "filter", "filter-aggressive"),
    ),
    Reglage(
        "cake_wash",
        "cake",
        "bool",
        "Remet le DSCP a zero en sortie. NECESSAIRE quand le DSCP entrant n'est "
        "pas fiable (marquage client arbitraire).",
        nullable=True,
    ),
    Reglage(
        "cake_mpu",
        "cake",
        "int",
        "Taille de paquet minimale facturee (cadrage ATM/PPPoE).",
        nullable=True,
        minimum=0,
        maximum=256,
    ),
    # --- Garde-fous d'ecriture ---
    Reglage(
        "enforcement_max_actions",
        "enforcement",
        "int",
        "Coupe-circuit : un plan plus gros que cela signale un etat desire mal "
        "calcule, on s'arrete plutot que de reecrire tout un PoP.",
        minimum=1,
        maximum=100_000,
    ),
    Reglage(
        "require_separate_write_account",
        "enforcement",
        "bool",
        "Exiger un compte d'ecriture DISTINCT (rw_username) plutot que de se fier "
        "aux droits reels du compte configure.",
    ),
    # --- Cadences (prises en compte au tour de boucle suivant) ---
    _cadence(
        "subscriber_interval_s", "collect_subscribers", "Periode de collecte des sessions PPPoE."
    ),
    _cadence(
        "backhaul_interval_s",
        "collect_backhauls",
        "Periode de lecture de la capacite des backhauls radio.",
    ),
    _cadence(
        "link_interval_s",
        "collect_links",
        "Periode de lecture des compteurs de ports (debit des liens).",
    ),
    _cadence(
        "plan_refresh_interval_s", "refresh_plans", "Periode de rafraichissement des plans abonnes."
    ),
    _cadence(
        "inventory_refresh_interval_s",
        "reload_inventory",
        "Periode de rechargement de l'inventaire des routeurs.",
    ),
    _cadence(
        "rtt_interval_s", "probe_rtt", "Periode de la sonde de latence (si elle est activee)."
    ),
    _cadence(
        "boost_check_interval_s",
        "expire_boosts",
        "Periode de verification des boosts arrives a echeance.",
    ),
    _cadence(
        "shaping_reconcile_interval_s",
        "reconcile_shaping",
        "Periode de reapplication automatique de l'etat desire sur les routeurs.",
    ),
    _cadence(
        "topology_refresh_interval_s",
        "discover_topology",
        "Periode de redecouverte du graphe reseau. C'est ce job qui peuple les "
        "l'onglet Arbre reseau ; sans lui il reste vide.",
    ),
    _cadence(
        "qoe_loop_interval_s",
        "qoe_closed_loop",
        "Periode de la boucle fermee QoE (resserrage d'un secteur qui decroche).",
    ),
    _cadence(
        "vlan_detect_interval_s",
        "detect_vlan_clients",
        "Periode de lecture de la table ARP pour reperer les clients sur VLAN "
        "routee. Aide a la declaration : rien de ce qui est trouve n'est shape.",
    ),
    # --- Detection ---
    Reglage(
        "vlan_detect_enabled",
        "detection",
        "bool",
        "Lire /ip/arp pour proposer les clients a IP fixe non declares. "
        "Purement consultatif : aucun candidat n'est jamais façonne.",
    ),
    Reglage(
        "vlan_candidate_limit",
        "detection",
        "int",
        "Nombre maximum de candidats remontes a l'interface et poses dans le "
        "graphe. Une VLAN bavarde ne doit pas rendre l'arbre illisible.",
        minimum=1,
        maximum=5_000,
    ),
    Reglage(
        "vlan_sighting_retention_s",
        "detection",
        "float",
        "Duree au-dela de laquelle une adresse qui ne parle plus est oubliee.",
        minimum=60.0,
        maximum=2_592_000.0,
    ),
    # --- Trafic (NetFlow) ---
    #
    # NETFLOW_ENABLED, NETFLOW_BIND et NETFLOW_PORT ne sont PAS ici : ouvrir une
    # socket d'ecoute n'est pas un reglage qu'on bascule a chaud depuis une page
    # web, et le faire croire serait pire que de ne pas l'offrir.
    _cadence(
        "netflow_flush_interval_s",
        "netflow_flush",
        "Periode d'ecriture des fenetres de trafic. Une ligne par abonne et par "
        "fenetre : descendre sous 30 s multiplie les lignes sans rien apprendre.",
    ),
    Reglage(
        "netflow_accounting_vantage",
        "trafic",
        "choix",
        "D'ou la consommation est lue : 'edge' (en amont du coeur, a la sortie "
        "internet) ou 'pop'. Le meme octet est exporte par les deux : les "
        "additionner doublerait la consommation de chaque abonne.",
        choices=("edge", "pop"),
    ),
    Reglage(
        "netflow_track_hosts",
        "trafic",
        "bool",
        "Retenir les adresses vues qui ne correspondent a aucune fiche. Aide a "
        "la declaration des clients VLAN : rien n'en devient jamais un client.",
    ),
    Reglage(
        "netflow_host_limit",
        "trafic",
        "int",
        "Nombre maximum d'adresses non rattachees retenues par fenetre. Une VLAN "
        "bavarde ne doit pas noyer l'aide a la saisie.",
        minimum=1,
        maximum=10_000,
    ),
    Reglage(
        "netflow_host_retention_s",
        "trafic",
        "float",
        "Duree au-dela de laquelle une adresse non rattachee qui s'est tue est oubliee.",
        minimum=60.0,
        maximum=2_592_000.0,
    ),
    # --- Services atteints (ipfinder) ---
    Reglage(
        "netflow_track_destinations",
        "services",
        "bool",
        "Retenir l'adresse DISTANTE atteinte par chaque abonne. C'est ce qui "
        "alimente l'onglet Services et les restrictions. Le couper ne touche pas "
        "a la mesure de volume par abonne.",
    ),
    Reglage(
        "netflow_destination_limit",
        "services",
        "int",
        "Nombre maximum de destinations retenues par fenetre. Un abonne en p2p "
        "peut toucher des milliers d'adresses par minute.",
        minimum=10,
        maximum=50_000,
    ),
    Reglage(
        "netflow_destination_retention_s",
        "services",
        "float",
        "Duree au-dela de laquelle une destination qui s'est tue est oubliee. "
        "Seule la MESURE est purgee : le nom de l'adresse, lui, est conserve.",
        minimum=300.0,
        maximum=7_776_000.0,
    ),
    Reglage(
        "ipfinder_enabled",
        "services",
        "bool",
        "Mettre un nom sur les adresses atteintes. A false, les volumes restent "
        "mesures mais plus rien n'est identifie.",
    ),
    Reglage(
        "ipfinder_rdns_enabled",
        "services",
        "bool",
        "Interroger le nom inverse (PTR) des adresses nouvelles. C'est ce qui "
        "distingue YouTube du reste de Google, et ce qui reconnait un service "
        "qui a change de prefixe.",
    ),
    Reglage(
        "ipfinder_rdap_enabled",
        "services",
        "bool",
        "Interroger le registre (RDAP) pour l'organisation, l'AS et le pays. "
        "COUPE PAR DEFAUT : c'est le seul appel sortant du controleur.",
    ),
    Reglage(
        "ipfinder_batch_size",
        "services",
        "int",
        "Adresses nommees par passage. Monter cette valeur vide la file plus "
        "vite, au prix d'une rafale de requetes DNS.",
        minimum=1,
        maximum=1_000,
    ),
    Reglage(
        "ipfinder_max_attempts",
        "services",
        "int",
        "Tentatives avant d'abandonner une adresse sans nom inverse. La majorite "
        "d'internet n'en a pas : insister ferait une requete perpetuelle.",
        minimum=1,
        maximum=20,
    ),
    _cadence(
        "ipfinder_interval_s",
        JOB_INTEL,
        "Cadence a laquelle les adresses nouvellement vues sont nommees.",
    ),
    # --- Restrictions de trafic ---
    _cadence(
        "restrictions_interval_s",
        JOB_RESTRICTIONS,
        "Cadence a laquelle les restrictions sont reconciliees sur les routeurs. "
        "C'est ce passage qui ajoute aux listes les adresses nouvellement "
        "decouvertes d'un service restreint.",
    ),
    Reglage(
        "netflow_export_auto",
        "services",
        "bool",
        "Poser l'export NetFlow sur les routeurs automatiquement. Reste soumis "
        "a l'interrupteur d'ecriture.",
    ),
    _cadence(
        "netflow_export_interval_s",
        JOB_NETFLOW_EXPORT,
        "Cadence a laquelle l'export NetFlow des routeurs est verifie et repose.",
    ),
    Reglage(
        "restriction_address_limit",
        "services",
        "int",
        "Plafond d'adresses par restriction. Une liste que le routeur parcourt a "
        "chaque paquet ne doit pas grossir sans limite.",
        minimum=10,
        maximum=50_000,
    ),
)

PAR_NOM: dict[str, Reglage] = {r.name: r for r in REGLAGES}


@dataclass
class RuntimeConfig:
    """Vue vivante des reglages : base prioritaire, environnement en secours.

    ``defaults`` fige ce que l'environnement (ou le defaut du code) proposait au
    demarrage, pour pouvoir revenir en arriere et montrer d'ou vient chaque valeur.
    """

    settings: Settings
    defaults: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    # Appele quand une cadence change, pour reprogrammer le job correspondant.
    # Le retour n'est pas utilise (le scheduler renvoie s'il a trouve le job) :
    # on l'accepte pour pouvoir brancher directement Scheduler.set_interval.
    on_interval_change: Callable[[str, float], object] | None = None

    def __post_init__(self) -> None:
        if not self.defaults:
            self.defaults = {r.name: getattr(self.settings, r.name) for r in REGLAGES}

    # ------------------------------------------------------------- lecture
    @staticmethod
    def spec(name: str) -> Reglage:
        reglage = PAR_NOM.get(name)
        if reglage is None:
            raise ReglageInconnuError(f"reglage inconnu : {name}")
        return reglage

    def value(self, name: str) -> Any:
        self.spec(name)
        return getattr(self.settings, name)

    def describe(self) -> list[dict[str, Any]]:
        """Etat de chaque reglage : valeur, defaut, et d'ou vient la valeur."""
        lignes: list[dict[str, Any]] = []
        for reglage in REGLAGES:
            depuis_base = reglage.name in self.overrides
            lignes.append(
                {
                    "name": reglage.name,
                    "group": reglage.group,
                    "kind": reglage.kind,
                    "help": reglage.help,
                    "nullable": reglage.nullable,
                    "choices": list(reglage.choices),
                    "minimum": reglage.minimum,
                    "maximum": reglage.maximum,
                    "value": getattr(self.settings, reglage.name),
                    "default": self.defaults.get(reglage.name),
                    "source": "db" if depuis_base else "defaut",
                    "restart_required": False,
                }
            )
        return lignes

    # ------------------------------------------------------------- ecriture
    def _apply(self, name: str, value: Any) -> None:
        """Ecrit la valeur dans l'objet Settings partage, et reprogramme si besoin."""
        setattr(self.settings, name, value)
        reglage = self.spec(name)
        if reglage.job and self.on_interval_change is not None:
            self.on_interval_change(reglage.job, float(value))

    def load(self, stored: dict[str, Any]) -> list[str]:
        """Applique les valeurs lues en base au demarrage.

        Une ligne illisible (reglage retire depuis, valeur devenue hors bornes)
        est ignoree avec son nom en retour : mieux vaut demarrer sur le defaut
        que refuser de demarrer.
        """
        ignores: list[str] = []
        for name, brut in stored.items():
            reglage = PAR_NOM.get(name)
            if reglage is None:
                ignores.append(name)
                continue
            try:
                valeur = reglage.coerce(brut)
            except ValeurInvalideError:
                ignores.append(name)
                continue
            self.overrides[name] = valeur
            self._apply(name, valeur)
        return ignores

    def set(self, name: str, value: Any) -> Any:
        """Valide puis applique une valeur. Renvoie la valeur retenue."""
        reglage = self.spec(name)
        valeur = reglage.coerce(value)
        self.overrides[name] = valeur
        self._apply(name, valeur)
        return valeur

    def clear(self, name: str) -> Any:
        """Revient au defaut (celui de l'environnement au demarrage)."""
        self.spec(name)
        self.overrides.pop(name, None)
        valeur = self.defaults.get(name)
        self._apply(name, valeur)
        return valeur

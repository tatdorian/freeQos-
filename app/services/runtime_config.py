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
        "Share of the measured capacity actually applied. We shape BELOW the real "
        "capacity so the queue builds inside CAKE, not in the radio buffer.",
        minimum=0.1,
        maximum=1.0,
    ),
    Reglage(
        "shaping_floor_mbps",
        "shaping",
        "float",
        "Rate floor of a link: a deep fade must not cut it to zero.",
        minimum=0.0,
        maximum=10_000.0,
    ),
    Reglage(
        "shaping_prune",
        "shaping",
        "bool",
        "Delete our queues once they are useless (subscriber gone, link removed). "
        "Turn it off during a migration.",
    ),
    Reglage(
        "shaping_adopt_foreign_queues",
        "shaping",
        "bool",
        "Align the rate of a THIRD-PARTY queue already set on a subscriber target. "
        "RouterOS only applies the first queue of a given target: without this, "
        "the limit entered would be purely decorative.",
    ),
    Reglage(
        "shaping_queue_for_detected_links",
        "shaping",
        "bool",
        "Write a queue as soon as a link is discovered, unlimited (0/0). It throttles "
        "nothing but acts as the parent of the subscriber queues.",
    ),
    Reglage(
        "subscriber_queue_target",
        "shaping",
        "choix",
        "What a subscriber queue hangs on. 'address' (recommended) targets the "
        "session address; 'interface' is discouraged (RouterOS swaps the two "
        "limits).",
        choices=("address", "interface"),
    ),
    # --- CAKE ---
    Reglage(
        "cake_overhead",
        "cake",
        "int",
        "Encapsulation counted by CAKE, in bytes. PPPoE over ethernet = 8 + 14. "
        "Add 4 per VLAN tag, 4 per MPLS label. Underestimating means shaping "
        "above the link capacity.",
        minimum=0,
        maximum=200,
    ),
    Reglage(
        "cake_rtt_ms",
        "cake",
        "int",
        "Reference RTT of the AQM, in milliseconds.",
        minimum=1,
        maximum=1000,
    ),
    Reglage(
        "cake_diffserv",
        "cake",
        "choix",
        "Priority classes from the DSCP. 'diffserv4' protects voice and gaming; "
        "'besteffort' ignores the DSCP. Empty = RouterOS default.",
        nullable=True,
        choices=("besteffort", "diffserv3", "diffserv4", "diffserv8", "precedence"),
    ),
    Reglage(
        "cake_flowmode",
        "cake",
        "choix",
        "Flow isolation. 'triple-isolate' is the right general default.",
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
        "Resolve NAT to isolate real hosts rather than the single public IP. "
        "DECISIVE behind CGNAT or PPPoE.",
        nullable=True,
    ),
    Reglage(
        "cake_ack_filter",
        "cake",
        "choix",
        "Thins out ACKs on a very asymmetric link.",
        nullable=True,
        choices=("none", "filter", "filter-aggressive"),
    ),
    Reglage(
        "cake_wash",
        "cake",
        "bool",
        "Resets the DSCP to zero on egress. REQUIRED when the incoming DSCP is not "
        "trustworthy (arbitrary client marking).",
        nullable=True,
    ),
    Reglage(
        "cake_mpu",
        "cake",
        "int",
        "Minimum billed packet size (ATM/PPPoE framing).",
        nullable=True,
        minimum=0,
        maximum=256,
    ),
    # --- Garde-fous d'ecriture ---
    Reglage(
        "enforcement_max_actions",
        "enforcement",
        "int",
        "Circuit breaker: a plan larger than this signals a badly computed desired "
        "state; we stop rather than rewrite a whole PoP.",
        minimum=1,
        maximum=100_000,
    ),
    Reglage(
        "require_separate_write_account",
        "enforcement",
        "bool",
        "Require a SEPARATE write account (rw_username) rather than trusting the "
        "real rights of the configured account.",
    ),
    # --- Cadences (prises en compte au tour de boucle suivant) ---
    _cadence(
        "subscriber_interval_s", "collect_subscribers", "How often PPPoE sessions are collected."
    ),
    _cadence(
        "backhaul_interval_s",
        "collect_backhauls",
        "How often the capacity of radio backhauls is read.",
    ),
    _cadence(
        "link_interval_s",
        "collect_links",
        "How often port counters are read (link throughput).",
    ),
    _cadence(
        "plan_refresh_interval_s", "refresh_plans", "How often subscriber plans are refreshed."
    ),
    _cadence(
        "inventory_refresh_interval_s",
        "reload_inventory",
        "How often the router inventory is reloaded.",
    ),
    _cadence(
        "rtt_interval_s", "probe_rtt", "How often the latency probe runs (when it is enabled)."
    ),
    _cadence(
        "boost_check_interval_s",
        "expire_boosts",
        "How often expired boosts are checked.",
    ),
    _cadence(
        "shaping_reconcile_interval_s",
        "reconcile_shaping",
        "How often the desired state is automatically re-applied on the routers.",
    ),
    _cadence(
        "topology_refresh_interval_s",
        "discover_topology",
        "How often the network graph is re-discovered. This job fills the Network "
        "tree tab; without it the tab stays empty.",
    ),
    _cadence(
        "qoe_loop_interval_s",
        "qoe_closed_loop",
        "How often the closed QoE loop runs (tightening a sector that is dropping off).",
    ),
    _cadence(
        "vlan_detect_interval_s",
        "detect_vlan_clients",
        "How often the ARP table is read to spot clients on routed VLANs. This is an "
        "aid to declaration: nothing found here is ever shaped.",
    ),
    # --- Detection ---
    Reglage(
        "vlan_detect_enabled",
        "detection",
        "bool",
        "Read /ip/arp to suggest undeclared static-IP clients. Purely advisory: no "
        "candidate is ever shaped.",
    ),
    Reglage(
        "vlan_candidate_limit",
        "detection",
        "int",
        "Maximum number of candidates reported to the interface and placed in the "
        "graph. A chatty VLAN must not make the tree unreadable.",
        minimum=1,
        maximum=5_000,
    ),
    Reglage(
        "vlan_sighting_retention_s",
        "detection",
        "float",
        "How long before an address that stopped talking is forgotten.",
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
        "How often traffic windows are written. One row per subscriber per window: "
        "going below 30 s multiplies rows without learning anything.",
    ),
    Reglage(
        "netflow_accounting_vantage",
        "trafic",
        "choix",
        "Where usage is read from: 'edge' (upstream of the core, at the internet "
        "egress) or 'pop'. The same byte is exported by both: adding them up "
        "would double every subscriber usage.",
        choices=("edge", "pop"),
    ),
    Reglage(
        "netflow_track_hosts",
        "trafic",
        "bool",
        "Keep the addresses seen that match no record. An aid to declaring VLAN "
        "clients: none of them ever becomes a client on its own.",
    ),
    Reglage(
        "netflow_host_limit",
        "trafic",
        "int",
        "Maximum number of unmatched addresses kept per window. A chatty VLAN must "
        "not drown the entry aid.",
        minimum=1,
        maximum=10_000,
    ),
    Reglage(
        "netflow_host_retention_s",
        "trafic",
        "float",
        "How long before an unmatched address that went quiet is forgotten.",
        minimum=60.0,
        maximum=2_592_000.0,
    ),
    # --- Services atteints (ipfinder) ---
    Reglage(
        "netflow_track_destinations",
        "services",
        "bool",
        "Keep the REMOTE address each subscriber reaches. This feeds the Services "
        "tab and the restrictions. Turning it off does not affect per-subscriber "
        "volume measurement.",
    ),
    Reglage(
        "netflow_destination_limit",
        "services",
        "int",
        "Maximum number of destinations kept per window. A subscriber on p2p can "
        "touch thousands of addresses per minute.",
        minimum=10,
        maximum=50_000,
    ),
    Reglage(
        "netflow_destination_retention_s",
        "services",
        "float",
        "How long before a destination that went quiet is forgotten. Only the "
        "MEASUREMENT is purged: the name of the address is kept.",
        minimum=300.0,
        maximum=7_776_000.0,
    ),
    Reglage(
        "ipfinder_enabled",
        "services",
        "bool",
        "Put a name on the destinations reached. At false, volumes are still "
        "measured but nothing is identified any more.",
    ),
    Reglage(
        "ipfinder_rdns_enabled",
        "services",
        "bool",
        "Query the reverse name (PTR) of new addresses. This is what tells YouTube "
        "apart from the rest of Google, and what recognises a service that "
        "changed prefix.",
    ),
    Reglage(
        "ipfinder_rdap_enabled",
        "services",
        "bool",
        "Query the registry (RDAP) for the organisation, the AS and the country. "
        "OFF BY DEFAULT: this is the controller only outbound call.",
    ),
    Reglage(
        "ipfinder_geoip_enabled",
        "services",
        "bool",
        "Locate the destinations reached (country, region, city). OFF BY DEFAULT: "
        "without a local database, this sends the addresses your clients reach "
        "to a third party.",
    ),
    Reglage(
        "ipfinder_batch_size",
        "services",
        "int",
        "Addresses named per pass. Raising this drains the queue faster, at the "
        "cost of a burst of DNS queries.",
        minimum=1,
        maximum=1_000,
    ),
    Reglage(
        "ipfinder_max_attempts",
        "services",
        "int",
        "Attempts before giving up on an address with no reverse name. Most of the "
        "internet has none: insisting would make the query perpetual.",
        minimum=1,
        maximum=20,
    ),
    _cadence(
        "ipfinder_interval_s",
        JOB_INTEL,
        "How often newly seen addresses are named.",
    ),
    # --- Restrictions de trafic ---
    _cadence(
        "restrictions_interval_s",
        JOB_RESTRICTIONS,
        "How often restrictions are reconciled on the routers. This is the pass "
        "that adds newly discovered addresses of a restricted service to the "
        "lists.",
    ),
    Reglage(
        "netflow_export_auto",
        "services",
        "bool",
        "Configure the NetFlow export on the routers automatically. Still subject "
        "to the write switch.",
    ),
    _cadence(
        "netflow_export_interval_s",
        JOB_NETFLOW_EXPORT,
        "How often the NetFlow export on the routers is checked and re-applied.",
    ),
    Reglage(
        "restriction_address_limit",
        "services",
        "int",
        "Cap on addresses per restriction. A list the router walks on every packet "
        "must not grow without bound.",
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

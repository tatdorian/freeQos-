"""Restrictions de trafic : de l'intention aux commandes RouterOS.

ENTIEREMENT PUR, comme ``enforcement/planner.py``. On donne les regles resolues
et ce que le routeur porte deja ; on obtient une liste de commandes. Rien ici
n'ouvre de connexion, ce qui permet de montrer le plan avant d'ecrire quoi que
ce soit -- et de tester la partie ou une erreur coupe internet a des clients.

CE QU'UNE RESTRICTION POSE SUR LE ROUTEUR
-----------------------------------------
Deux objets, toujours les memes :

1. UNE LISTE D'ADRESSES (``/ip/firewall/address-list``) qui porte les adresses
   du service vise. Elle est le seul objet qui BOUGE tout seul : le catalogue
   fournit les blocs publies, NetFlow y ajoute les serveurs reellement
   rencontres, et la reconciliation pousse la difference. C'est la reponse
   concrete a "je veux que ce soit dynamique".

2. LA REGLE QUI S'EN SERT :
   - bloquer  -> deux regles ``/ip/firewall/filter action=drop``, une par sens ;
   - plafonner -> deux marquages ``/ip/firewall/mangle`` et deux files
     ``/queue/tree`` accrochees a ``global``, une par sens.

POURQUOI DEUX REGLES ET NON UNE. Une regle de pare-feu regarde UN sens : la
liste est en destination quand le client emet, en source quand le service
repond. Une seule regle laisserait passer le retour -- ce qui, pour du
streaming, revient a ne rien bloquer du tout.

POURQUOI PAS UNE FILE SIMPLE. ``/queue/simple`` sait viser une destination, mais
une seule par file : plafonner un service de cinquante blocs demanderait
cinquante files par client. Le marquage plus l'arbre de files ne coute que deux
objets, quel que soit le nombre d'adresses.

DEUX LIMITES ASSUMEES
---------------------
IPv6 n'est pas pose. ``/ip/firewall/address-list`` est une table IPv4 ; l'IPv6
vit dans ``/ipv6/firewall/address-list``, avec ses propres chaines. Plutot que
de faire semblant, les prefixes IPv6 d'une regle sont ECARTES et le plan le dit.

Rien n'est touche qui ne porte pas la marque ``freeqos:managed``. Une regle de
pare-feu ecrite par l'exploitant, un address-list utilise par son routage : le
controleur ne les lit meme pas.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.enforcement.models import (
    MANAGED_COMMENT,
    Plan,
    PlanAction,
    PlanConflict,
    PlanSkip,
    format_rate,
)

logger = logging.getLogger(__name__)

PREFIX = "freeqos-"

ACTION_BLOCK = "block"
ACTION_LIMIT = "limit"

#: Les quatre roles qu'une ligne posee peut tenir. Ils sont inscrits dans le
#: commentaire : c'est ce qui permet de RETROUVER nos lignes sur le routeur sans
#: se fier a leur position, qui change des que l'exploitant en ajoute une.
ROLE_DROP_UP = "drop-up"
ROLE_DROP_DOWN = "drop-down"
ROLE_MARK_UP = "mark-up"
ROLE_MARK_DOWN = "mark-down"
ROLE_QUEUE_UP = "queue-up"
ROLE_QUEUE_DOWN = "queue-down"

PATH_ADDRESS_LIST = "/ip/firewall/address-list"
PATH_FILTER = "/ip/firewall/filter"
PATH_MANGLE = "/ip/firewall/mangle"
PATH_QUEUE_TREE = "/queue/tree"


def rule_slug(rule_id: int) -> str:
    """Identifiant RouterOS d'une regle, derive de son NUMERO et non de son nom.

    Renommer une restriction dans l'interface ne doit pas laisser derriere elle
    une liste d'adresses orpheline que plus rien ne reclame : le nom affiche
    change, la cle de reconciliation non.
    """
    return f"r{int(rule_id)}"


def dst_list_name(rule_id: int) -> str:
    return f"{PREFIX}{rule_slug(rule_id)}-dst"


def src_list_name(rule_id: int) -> str:
    return f"{PREFIX}{rule_slug(rule_id)}-src"


def packet_mark(rule_id: int, *, descendant: bool) -> str:
    return f"{PREFIX}{rule_slug(rule_id)}-{'d' if descendant else 'u'}"


def queue_name(rule_id: int, *, descendant: bool) -> str:
    return f"{PREFIX}{rule_slug(rule_id)}-{'down' if descendant else 'up'}"


def tag(rule_id: int, role: str) -> str:
    """Commentaire pose sur chaque ligne : la marque, puis de quoi la retrouver."""
    return f"{MANAGED_COMMENT} restriction={rule_slug(rule_id)} role={role}"


def parse_tag(comment: Any) -> tuple[str, str] | None:
    """(slug de regle, role) lus dans un commentaire, ou None si ce n'est pas nous.

    Le test porte d'abord sur ``freeqos:managed`` : tout ce qui ne le porte pas
    appartient a quelqu'un d'autre et n'est meme pas analyse.
    """
    texte = str(comment or "")
    if MANAGED_COMMENT not in texte:
        return None
    slug = ""
    role = ""
    for morceau in texte.split():
        if morceau.startswith("restriction="):
            slug = morceau.split("=", 1)[1]
        elif morceau.startswith("role="):
            role = morceau.split("=", 1)[1]
    if not slug:
        return None
    return slug, role


@dataclass(frozen=True)
class RuleTarget:
    """Une restriction dont les criteres sont DEJA resolus en adresses.

    La resolution (catalogue + decouvertes NetFlow + saisie) se fait en amont,
    dans ``services/restrictions``. Ici on ne sait plus ce qu'est "Netflix" : on
    voit des prefixes, ce qui rend le calcul du plan verifiable ligne a ligne.
    """

    rule_id: int
    name: str
    action: str = ACTION_BLOCK
    limit_down_mbps: float | None = None
    limit_up_mbps: float | None = None
    #: Cote service : ce que la regle vise sur internet.
    destinations: tuple[str, ...] = ()
    #: Cote client : vide = tous les clients qui passent par ce routeur.
    clients: tuple[str, ...] = ()
    protocol: str | None = None
    ports: str | None = None

    @property
    def slug(self) -> str:
        return rule_slug(self.rule_id)


@dataclass
class RouterRestrictionState:
    """Ce que le routeur porte deja, tel qu'il a ete lu."""

    address_list: list[dict[str, Any]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    mangle: list[dict[str, Any]] = field(default_factory=list)
    queue_trees: list[dict[str, Any]] = field(default_factory=list)


def normalize_prefixes(values: Iterable[str]) -> tuple[list[str], list[str]]:
    """Range les prefixes en (IPv4 retenus, ecartes) sous forme canonique.

    CANONIQUE, ET C'EST CE QUI EVITE UNE REECRITURE PAR CYCLE. RouterOS relit
    ``10.0.0.0/8`` la ou on aurait ecrit ``10.0.0.1/8`` ; comparer des chaines
    brutes ferait voir un ecart a chaque passage, donc un ajout et un retrait
    perpetuels de la meme entree.

    Une adresse seule reste une adresse seule (``45.57.12.34``) : RouterOS la
    relit ainsi, sans ``/32``.
    """
    retenus: list[str] = []
    ecartes: list[str] = []
    vus: set[str] = set()
    for brut in values:
        texte = str(brut).strip()
        if not texte:
            continue
        try:
            reseau = ipaddress.ip_network(texte, strict=False)
        except ValueError:
            ecartes.append(texte)
            continue
        if reseau.version != 4:
            ecartes.append(texte)
            continue
        forme = (
            str(reseau.network_address) if reseau.prefixlen == reseau.max_prefixlen else str(reseau)
        )
        if forme not in vus:
            vus.add(forme)
            retenus.append(forme)
    return retenus, ecartes


def merge_addresses(
    base: Iterable[str], discovered: Iterable[str], *, limit: int = 5_000
) -> list[str]:
    """Blocs publies + adresses reellement rencontrees, sans redite.

    LES DEUX SOURCES SONT NECESSAIRES, ET AUCUNE NE SUFFIT :

    - les blocs publies couvrent les serveurs qu'AUCUN client n'a encore
      atteints. Sans eux, la toute premiere connexion vers chaque nouveau
      serveur passerait avant que la restriction ne le connaisse ;
    - les adresses decouvertes couvrent ce qui est HORS des blocs publies : un
      cache heberge chez l'operateur, un serveur loue chez un tiers. Sans elles,
      la regle laisserait passer exactement le trafic le plus volumineux.

    Une adresse deja contenue dans un bloc retenu est ecartee : elle ne changerait
    rien au filtrage et ferait grossir une liste que le routeur parcourt a chaque
    paquet. L'ordre de sortie garde les blocs d'abord, ce qui rend la liste
    lisible dans l'interface du routeur.
    """
    blocs, _ = normalize_prefixes(base)
    # IPv4Network et non ip_network : normalize_prefixes n'a laisse passer que
    # de l'IPv4, et 'subnet_of' refuse de comparer deux familles differentes.
    reseaux = [ipaddress.IPv4Network(b, strict=False) for b in blocs]
    sortie = list(blocs)
    vus = set(blocs)
    for adresse in discovered:
        retenues, _ = normalize_prefixes([adresse])
        if not retenues:
            continue
        forme = retenues[0]
        if forme in vus:
            continue
        candidat = ipaddress.IPv4Network(forme, strict=False)
        if any(candidat.subnet_of(reseau) for reseau in reseaux):
            continue
        vus.add(forme)
        sortie.append(forme)
        if len(sortie) >= limit:
            break
    return sortie


def _matchers(target: RuleTarget, *, descendant: bool) -> dict[str, str]:
    """Les champs de correspondance d'une ligne, pour un sens donne.

    LE SENS EST CELUI DU CLIENT. En descendant, le service est la SOURCE et le
    client la destination ; en montant, l'inverse. Inverser les deux reviendrait
    a poser une regle qui ne rencontre jamais un paquet -- et une restriction
    qui ne bloque rien est pire qu'une restriction absente, parce qu'elle
    s'affiche comme posee.
    """
    champs: dict[str, str] = {"chain": "forward"}
    liste_service = dst_list_name(target.rule_id)
    liste_client = src_list_name(target.rule_id)
    if descendant:
        champs["src-address-list"] = liste_service
        if target.clients:
            champs["dst-address-list"] = liste_client
    else:
        champs["dst-address-list"] = liste_service
        if target.clients:
            champs["src-address-list"] = liste_client
    if target.protocol:
        champs["protocol"] = target.protocol
        if target.ports:
            # Le port vise est celui du SERVICE : en destination quand le client
            # emet, en source quand le service repond.
            champs["src-port" if descendant else "dst-port"] = target.ports
    return champs


def desired_lines(target: RuleTarget) -> list[tuple[str, str, dict[str, str]]]:
    """Les lignes voulues pour une regle : (chemin, role, champs).

    Une seule fonction pour les deux actions : c'est elle qui decrit ce que
    "bloquer" et "plafonner" veulent dire, et il ne doit y avoir qu'un endroit
    ou le lire.
    """
    lignes: list[tuple[str, str, dict[str, str]]] = []
    if target.action == ACTION_BLOCK:
        for descendant, role in ((False, ROLE_DROP_UP), (True, ROLE_DROP_DOWN)):
            champs = _matchers(target, descendant=descendant)
            champs["action"] = "drop"
            champs["comment"] = tag(target.rule_id, role)
            lignes.append((PATH_FILTER, role, champs))
        return lignes

    for descendant, role_marque, role_file, plafond in (
        (True, ROLE_MARK_DOWN, ROLE_QUEUE_DOWN, target.limit_down_mbps),
        (False, ROLE_MARK_UP, ROLE_QUEUE_UP, target.limit_up_mbps),
    ):
        if not plafond:
            # Un plafond absent dans un sens n'est pas une erreur : on plafonne
            # souvent le descendant seul. On ne pose alors NI marquage NI file
            # dans ce sens -- marquer sans plafonner couterait du CPU routeur
            # pour rien.
            continue
        champs = _matchers(target, descendant=descendant)
        champs["action"] = "mark-packet"
        champs["new-packet-mark"] = packet_mark(target.rule_id, descendant=descendant)
        # passthrough=no : le paquet est marque une fois et ne redescend pas la
        # chaine. Sans cela, une regle suivante pourrait le remarquer et le
        # faire compter dans deux files.
        champs["passthrough"] = "no"
        champs["comment"] = tag(target.rule_id, role_marque)
        lignes.append((PATH_MANGLE, role_marque, champs))

        lignes.append(
            (
                PATH_QUEUE_TREE,
                role_file,
                {
                    "name": queue_name(target.rule_id, descendant=descendant),
                    "parent": "global",
                    "packet-mark": packet_mark(target.rule_id, descendant=descendant),
                    "max-limit": format_rate(plafond),
                    "comment": tag(target.rule_id, role_file),
                },
            )
        )
    return lignes


def _existantes(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Nos lignes sur le routeur, rangees par (slug de regle, role)."""
    sortie: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        marque = parse_tag(row.get("comment"))
        if marque is None:
            continue
        sortie[marque] = row
    return sortie


def _ecart(row: dict[str, Any], champs: dict[str, str]) -> dict[str, tuple[str | None, str]]:
    """Ce qui differe entre la ligne posee et la ligne voulue.

    Une ligne DESACTIVEE a la main compte comme un ecart : elle ne restreint
    plus rien alors que l'interface la montrerait comme posee. La reconciliation
    la reactive, exactement comme elle reactive une file desactivee.
    """
    changements: dict[str, tuple[str | None, str]] = {}
    for cle, voulu in champs.items():
        actuel = row.get(cle)
        texte = None if actuel is None else str(actuel)
        if texte != voulu:
            changements[cle] = (texte, voulu)
    if str(row.get("disabled", "false")).lower() in {"true", "yes"}:
        changements["disabled"] = ("yes", "no")
    return changements


def plan_restrictions(
    router_name: str,
    targets: Sequence[RuleTarget],
    state: RouterRestrictionState,
    *,
    address_limit: int = 5_000,
) -> Plan:
    """Compare ce qui est voulu a ce qui est pose, et rend les commandes.

    L'ORDRE DES ACTIONS EST VOULU : les adresses d'abord, les regles ensuite,
    les retraits en dernier. Poser une regle qui vise une liste encore vide la
    rendrait inoperante le temps du plan ; retirer avant d'avoir ajoute ouvrirait
    une fenetre ou le trafic passe.
    """
    plan = Plan(router_name=router_name)
    voulus_adresses: dict[tuple[str, str], str] = {}
    slugs_actifs: set[str] = set()

    for cible in targets:
        slugs_actifs.add(cible.slug)
        destinations, ecartees = normalize_prefixes(cible.destinations)
        clients, clients_ecartes = normalize_prefixes(cible.clients)

        if ecartees or clients_ecartes:
            plan.skipped.append(
                PlanSkip(
                    login=cible.name,
                    reason=(
                        f"{len(ecartees) + len(clients_ecartes)} prefixe(s) non poses "
                        "(IPv6 ou saisie invalide) : les listes d'adresses de "
                        "/ip/firewall sont IPv4. Le reste de la regle est pose."
                    ),
                )
            )
        if not destinations:
            plan.skipped.append(
                PlanSkip(
                    login=cible.name,
                    reason=(
                        "aucune adresse IPv4 a viser : le service choisi n'a pas de "
                        "bloc publie et rien n'a encore ete decouvert par NetFlow. "
                        "La regle sera posee des qu'une adresse sera connue."
                    ),
                )
            )
            continue
        if len(destinations) > address_limit:
            plan.conflicts.append(
                PlanConflict(
                    name=cible.name,
                    path=PATH_ADDRESS_LIST,
                    detail=(
                        f"{len(destinations)} adresses depassent la limite de securite "
                        f"({address_limit}). Restreignez le critere : une liste de cette "
                        "taille pese sur le routeur a chaque paquet."
                    ),
                )
            )
            continue
        if cible.action == ACTION_LIMIT and not (cible.limit_down_mbps or cible.limit_up_mbps):
            plan.skipped.append(
                PlanSkip(
                    login=cible.name,
                    reason="cap to apply but no rate entered: nothing to write",
                )
            )
            continue

        for adresse in destinations:
            voulus_adresses[(dst_list_name(cible.rule_id), adresse)] = tag(cible.rule_id, "dst")
        for adresse in clients:
            voulus_adresses[(src_list_name(cible.rule_id), adresse)] = tag(cible.rule_id, "src")

    # --- listes d'adresses : ajouts ------------------------------------------
    posees: dict[tuple[str, str], dict[str, Any]] = {}
    for row in state.address_list:
        if parse_tag(row.get("comment")) is None:
            continue
        cle = (str(row.get("list") or ""), str(row.get("address") or ""))
        posees[cle] = row

    for (liste, adresse), commentaire in voulus_adresses.items():
        if (liste, adresse) in posees:
            continue
        plan.actions.append(
            PlanAction(
                verb="add",
                path=PATH_ADDRESS_LIST,
                fields={"list": liste, "address": adresse, "comment": commentaire},
                name=f"{liste} {adresse}",
                reason="service address targeted by the restriction",
            )
        )

    # --- regles et files : ajouts et corrections -----------------------------
    existantes = {
        PATH_FILTER: _existantes(state.filters),
        PATH_MANGLE: _existantes(state.mangle),
        PATH_QUEUE_TREE: _existantes(state.queue_trees),
    }
    voulues: set[tuple[str, str, str]] = set()

    for cible in targets:
        if not any(cle[0] == dst_list_name(cible.rule_id) for cle in voulus_adresses):
            continue
        for chemin, role, champs in desired_lines(cible):
            voulues.add((chemin, cible.slug, role))
            posee = existantes[chemin].get((cible.slug, role))
            if posee is None:
                plan.actions.append(
                    PlanAction(
                        verb="add",
                        path=chemin,
                        fields=champs,
                        name=f"{cible.name} ({role})",
                        reason=f"restriction '{cible.name}': {role}",
                    )
                )
                continue
            changements = _ecart(posee, champs)
            if not changements:
                plan.unchanged += 1
                continue
            plan.actions.append(
                PlanAction(
                    verb="set",
                    path=chemin,
                    target_id=str(posee.get(".id") or ""),
                    fields={cle: apres for cle, (_, apres) in changements.items()},
                    name=f"{cible.name} ({role})",
                    reason=f"restriction '{cible.name}': aligning {role}",
                    changes=changements,
                )
            )

    # --- retraits ------------------------------------------------------------
    #
    # Ce qui porte notre marque et que plus aucune regle ne reclame : regle
    # supprimee, desactivee, ou adresse qui ne releve plus du service. C'est ce
    # retrait qui rend la liste VIVANTE dans les deux sens -- sans lui, une
    # adresse ajoutee un jour resterait bloquee pour toujours.
    for (liste, adresse), row in posees.items():
        if (liste, adresse) in voulus_adresses:
            continue
        plan.actions.append(
            PlanAction(
                verb="remove",
                path=PATH_ADDRESS_LIST,
                target_id=str(row.get(".id") or ""),
                name=f"{liste} {adresse}",
                reason="this address no longer belongs to any active restriction",
            )
        )
    for chemin, lignes in existantes.items():
        for (slug, role), row in lignes.items():
            if (chemin, slug, role) in voulues:
                continue
            plan.actions.append(
                PlanAction(
                    verb="remove",
                    path=chemin,
                    target_id=str(row.get(".id") or ""),
                    name=f"{slug} ({role})",
                    reason="restriction removed, disabled or no longer relevant",
                )
            )
    return plan


def plan_lift(router_name: str, rule_id: int, state: RouterRestrictionState) -> Plan:
    """Les retraits qui levent UNE regle, et rien d'autre.

    C'est le geste "suspendre" ou "supprimer" : l'exploitant a demande que ce
    trafic repasse. Attendre la reconciliation suivante laissait l'adresse
    bloquee plusieurs minutes -- et indefiniment si une AUTRE regle du meme
    routeur faisait echouer le plan complet avant qu'il n'atteigne ses retraits.
    Ce plan ne contient que des suppressions de lignes portant la marque de
    cette regle : il ne peut rien poser, ni toucher a une autre restriction.

    L'ORDRE EST L'INVERSE DE LA POSE : les regles d'abord, qui cessent de
    bloquer des qu'elles disparaissent, les listes d'adresses ensuite.
    """
    plan = Plan(router_name=router_name)
    slug = rule_slug(rule_id)
    for chemin, rows in (
        (PATH_FILTER, state.filters),
        (PATH_MANGLE, state.mangle),
        (PATH_QUEUE_TREE, state.queue_trees),
        (PATH_ADDRESS_LIST, state.address_list),
    ):
        for row in rows:
            marque = parse_tag(row.get("comment"))
            if marque is None or marque[0] != slug:
                continue
            nom = (
                f"{row.get('list') or ''} {row.get('address') or ''}"
                if chemin == PATH_ADDRESS_LIST
                else f"{slug} ({marque[1]})"
            )
            plan.actions.append(
                PlanAction(
                    verb="remove",
                    path=chemin,
                    target_id=str(row.get(".id") or ""),
                    name=nom.strip(),
                    reason="restriction lifted",
                )
            )
    return plan

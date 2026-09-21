"""Un plafond enregistre est-il REELLEMENT en vigueur sur le routeur ?

POURQUOI CE MODULE EXISTE. Le controleur savait dire ce qu'il VOULAIT poser
(le plan) et ce qu'il avait ECRIT (le journal). Il ne savait pas dire la seule
chose qui compte pour l'exploitant : *est-ce que le reseau peut encore depasser
ce plafond ?* Les trois questions sont differentes, et c'est la troisieme qui
fait qu'on decouvre un abonne a 497 kbps sous un plafond a 100 kbps.

Sur RouterOS, une file simple peut exister, porter le bon debit, etre lue sans
erreur -- et ne rien brider du tout. Quatre causes, toutes silencieuses :

1. LE FASTTRACK. Une regle ``action=fasttrack-connection`` fait sauter aux
   paquets etablis le reste du chemin, FILES SIMPLES COMPRISES. C'est la cause
   numero un d'un plafond qui ne plafonne pas, et elle est active par defaut
   dans le pare-feu d'usine de RouterOS. Aucun message ne le signale : la file
   affiche simplement un compteur qui n'avance pas.
2. LA FILE MASQUEE. RouterOS n'evalue les files simples qu'en LISTE, et seule la
   PREMIERE qui matche s'applique. Une file placee apres une autre qui vise la
   meme cible -- ou un reseau qui la contient -- est purement decorative.
3. LA FILE DESACTIVEE. ``disabled=yes`` ne bride rien, et la lecture du debit,
   elle, reste parfaitement correcte.
4. L'ECART DE DEBIT. La file en place porte un autre ``max-limit`` que celui
   qu'on a decide : plafond change en base, jamais repousse sur le routeur.

Tout ici est PUR : des dictionnaires lus sur le routeur entrent, un verdict
sort. Aucune connexion, aucun ecrit -- c'est ce qui permet de le tester sur des
cas reels sans routeur.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.enforcement.models import MANAGED_COMMENT, QueueSpec
from app.enforcement.planner import masked_queues, normalise_field

__all__ = [
    "GRAVITE",
    "LimitState",
    "VERDICT_ABSENTE",
    "VERDICT_CONTOURNE",
    "VERDICT_DESACTIVEE",
    "VERDICT_ECART",
    "VERDICT_EN_VIGUEUR",
    "VERDICT_MASQUEE",
    "VERDICT_SANS_PLAFOND",
    "VERDICTS_QUI_LAISSENT_PASSER",
    "audit_router",
    "fasttrack_rules",
    "fasttrack_verdict",
    "limit_state",
    "masked_queues",
]

# Verdicts, du plus grave au plus rassurant. L'ordre de ce tuple est celui qui
# decide ce qu'on montre quand plusieurs defauts frappent la meme file.
VERDICT_CONTOURNE = "contourne"
VERDICT_ABSENTE = "file-absente"
VERDICT_MASQUEE = "file-masquee"
VERDICT_DESACTIVEE = "file-desactivee"
VERDICT_ECART = "debit-different"
VERDICT_EN_VIGUEUR = "en-vigueur"
# Cas a part : il n'y a RIEN a tenir.
VERDICT_SANS_PLAFOND = "sans-plafond"

GRAVITE = (
    VERDICT_CONTOURNE,
    VERDICT_ABSENTE,
    VERDICT_MASQUEE,
    VERDICT_DESACTIVEE,
    VERDICT_ECART,
    VERDICT_EN_VIGUEUR,
    VERDICT_SANS_PLAFOND,
)

# Un verdict autre que ces deux-la veut dire : le reseau PEUT depasser le plafond.
VERDICTS_QUI_LAISSENT_PASSER = frozenset(GRAVITE) - {VERDICT_EN_VIGUEUR, VERDICT_SANS_PLAFOND}

# Ce que RouterOS ecrit quand une file ne borne rien.
SANS_LIMITE = frozenset({"0/0", "0", ""})

ACTION_FASTTRACK = "fasttrack-connection"


def _vrai(valeur: Any) -> bool:
    return str(valeur or "").strip().lower() in {"true", "yes", "1"}


def _nom(row: dict[str, Any]) -> str:
    return str(row.get("name") or "").strip()


# --------------------------------------------------------------- fasttrack
@dataclass(slots=True)
class FasttrackRule:
    """Une regle de fasttrack active, telle qu'elle est lue sur le routeur."""

    chain: str
    comment: str
    index: int

    def to_dict(self) -> dict[str, Any]:
        return {"chain": self.chain, "comment": self.comment, "index": self.index}


def fasttrack_rules(rows: Sequence[dict[str, Any]]) -> list[FasttrackRule]:
    """Les regles de fasttrack ACTIVES de ``/ip/firewall/filter``.

    Une regle desactivee ne contourne rien : elle n'est pas remontee, sans quoi
    on alarmerait sur un pare-feu deja corrige.
    """
    trouvees: list[FasttrackRule] = []
    for index, row in enumerate(rows):
        if str(row.get("action") or "").strip().lower() != ACTION_FASTTRACK:
            continue
        if _vrai(row.get("disabled")):
            continue
        trouvees.append(
            FasttrackRule(
                chain=str(row.get("chain") or "?"),
                comment=str(row.get("comment") or ""),
                index=index,
            )
        )
    return trouvees


def fasttrack_verdict(regles: Sequence[FasttrackRule] | None) -> dict[str, Any]:
    """Ce qu'il faut en dire, et la commande exacte qui le corrige.

    On ne desactive PAS le fasttrack tout seul : c'est une regle de pare-feu,
    elle porte une decision de performance que le controleur n'a pas prise. On
    la nomme, on dit ce qu'elle coute, et on donne la ligne a coller.
    """
    if regles is None:
        # NE JAMAIS conclure a partir du vide. Un compte en lecture seule peut
        # se voir refuser /ip/firewall/filter : annoncer "aucun fasttrack" alors
        # qu'on n'a rien pu lire ferait chercher la panne partout ailleurs.
        return {
            "active": None,
            "rules": [],
            "detail": (
                "firewall unreadable with this account: cannot tell whether fasttrack "
                "bypasses the queues. Check by hand "
                "(/ip firewall filter print where action=fasttrack-connection)"
            ),
            "remedy": None,
        }
    if not regles:
        return {
            "active": False,
            "rules": [],
            "detail": "no active fasttrack rule: the simple queues see all the traffic",
            "remedy": None,
        }
    return {
        "active": True,
        "rules": [r.to_dict() for r in regles],
        "detail": (
            f"{len(regles)} active fasttrack rule(s): established connections "
            "SKIP the simple queues. While they are in place, no cap on this "
            "router can be held, whatever queue is written."
        ),
        "remedy": (
            "/ip firewall filter disable [find action=fasttrack-connection] "
            "(run it on the router: the controller does not touch the firewall)"
        ),
    }


# ------------------------------------------------------------ masquage
#
# La regle du "premier qui matche" vit dans le planificateur : c'est LUI qui
# decide de ne pas ecrire une file masquee, et deux implementations de la meme
# regle finiraient par rendre des verdicts differents. On la reutilise telle
# quelle (``masked_queues``, importe plus haut) plutot que de la recopier.


# ---------------------------------------------------------------- verdict
@dataclass(slots=True)
class LimitState:
    """Le sort d'UN plafond sur le routeur : tenu, ou contournable et pourquoi."""

    name: str
    target: str
    wanted: str
    seen: str | None = None
    verdict: str = VERDICT_EN_VIGUEUR
    detail: str = ""
    managed: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def enforced(self) -> bool:
        return self.verdict == VERDICT_EN_VIGUEUR

    @property
    def is_cap(self) -> bool:
        """Y a-t-il seulement un plafond a tenir ? Une file a 0/0 n'en est pas un."""
        return self.verdict != VERDICT_SANS_PLAFOND

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "wanted": self.wanted,
            "seen": self.seen,
            "verdict": self.verdict,
            "detail": self.detail,
            "enforced": self.enforced,
            "managed": self.managed,
            **self.extras,
        }


def _motif_absence(enforcement_enabled: bool | None, deja_reconcilie: bool | None) -> str:
    """Pourquoi la file n'est pas (encore) la. LA question, sur un PoP neuf.

    "Aucune file de ce nom sur le routeur" est exact et inutile : sur un site
    qu'on vient d'ajouter, ca se lit comme une panne alors que rien n'a encore
    eu l'occasion d'etre ecrit. Le motif doit nommer ce qui manque -- une
    autorisation d'ecrire, un tour de reconciliation, ou rien du tout, auquel
    cas c'est bien un defaut.
    """
    if enforcement_enabled is False:
        return (
            "enforcement is off: the queue is computed but nothing is written "
            "until it is on (switch at the top of this page)"
        )
    if deja_reconcilie is False:
        return (
            "reconciliation has not run on this router yet: the queue goes out "
            "on the next pass. Normal on a site just added"
        )
    return "no queue by that name on the router: nothing throttles this subscriber"


def limit_state(
    spec: QueueSpec,
    *,
    rows: Sequence[dict[str, Any]],
    masquees: dict[str, dict[str, Any]],
    fasttrack: bool,
    enforcement_enabled: bool | None = None,
    deja_reconcilie: bool | None = None,
) -> LimitState:
    """Le verdict d'un plafond voulu, face a ce que le routeur porte vraiment.

    Le fasttrack l'emporte sur tout le reste : une file parfaite derriere un
    fasttrack actif ne bride rien, et annoncer "en vigueur" serait le pire des
    mensonges -- celui qui fait chercher la panne ailleurs.
    """
    index = {_nom(row): row for row in rows if _nom(row)}
    etat = LimitState(name=spec.name, target=spec.target, wanted=spec.max_limit)

    # UNE FILE SANS PLAFOND N'EST PAS UN PLAFOND.
    #
    # Une file parente dont la capacite du lien est inconnue sort en 0/0 :
    # illimitee. La compter parmi les plafonds "non tenus" gonflait l'alarme
    # d'un site neuf -- "3 plafonds sur 3 ne sont PAS tenus" -- et noyait la
    # seule ligne qui comptait vraiment. Il n'y a rien a tenir ici ; ce qui
    # manque, c'est une capacite mesuree sur le lien, et cela se dit autrement.
    if str(spec.max_limit).strip() in SANS_LIMITE:
        etat.verdict = VERDICT_SANS_PLAFOND
        etat.detail = (
            "this queue carries no cap (link capacity unknown): there is nothing "
            "to hold. Declare the link capacity so it carries one"
        )
        ligne_existante = index.get(spec.name)
        if ligne_existante is not None:
            etat.seen = str(ligne_existante.get("max-limit") or "")
        return etat

    ligne = index.get(spec.name)
    if ligne is None:
        etat.verdict = VERDICT_ABSENTE
        etat.detail = _motif_absence(enforcement_enabled, deja_reconcilie)
        return etat

    etat.seen = str(ligne.get("max-limit") or "")
    etat.managed = MANAGED_COMMENT in str(ligne.get("comment") or "")

    if fasttrack:
        etat.verdict = VERDICT_CONTOURNE
        etat.detail = (
            "the queue is in place, but fasttrack makes established connections "
            "skip the simple queues: the cap is not held"
        )
        return etat
    if _vrai(ligne.get("disabled")):
        etat.verdict = VERDICT_DESACTIVEE
        etat.detail = "queue disabled on the router: it throttles nothing"
        return etat
    masque = masquees.get(spec.name)
    if masque is not None:
        etat.verdict = VERDICT_MASQUEE
        etat.detail = str(masque["detail"])
        etat.extras["masked_by"] = masque["by"]
        return etat
    if normalise_field(etat.seen) != normalise_field(spec.max_limit):
        etat.verdict = VERDICT_ECART
        etat.detail = (
            f"the router carries {etat.seen or '-'} where the controller wants "
            f"{spec.max_limit}: the decided cap is not the one applied"
        )
        return etat
    etat.detail = f"cap {spec.max_limit} bits/s in force on {spec.target}"
    return etat


def audit_router(
    *,
    router_name: str,
    desired: Iterable[QueueSpec],
    rows: Sequence[dict[str, Any]],
    firewall: Sequence[dict[str, Any]] | None = (),
    enforcement_enabled: bool | None = None,
    deja_reconcilie: bool | None = None,
) -> dict[str, Any]:
    """L'audit complet d'un routeur : ce qui bride vraiment, et ce qui ne bride pas.

    ``firewall=None`` veut dire "pas lisible", et non "vide" : le verdict le dit
    alors explicitement au lieu de rassurer a tort.

    ``enforcement_enabled`` et ``deja_reconcilie`` ne changent aucun verdict :
    ils changent le MOTIF d'une file absente. Sur un site qu'on vient d'ajouter,
    "rien ne bride cet abonne" se lit comme une panne alors que rien n'a encore
    eu l'occasion d'etre ecrit.
    """
    regles = None if firewall is None else fasttrack_rules(firewall)
    ft = fasttrack_verdict(regles)
    masquees = masked_queues(rows)
    etats = [
        limit_state(
            spec,
            rows=rows,
            masquees=masquees,
            fasttrack=bool(regles),
            enforcement_enabled=enforcement_enabled,
            deja_reconcilie=deja_reconcilie,
        ).to_dict()
        for spec in desired
    ]
    etats.sort(key=lambda e: (GRAVITE.index(str(e["verdict"])), str(e["name"])))
    # Les files SANS plafond ne comptent ni d'un cote ni de l'autre : il n'y a
    # rien a tenir. Les mettre parmi les "non tenus" gonflait l'alarme et
    # noyait les lignes qui comptent.
    plafonds = [e for e in etats if e["verdict"] != VERDICT_SANS_PLAFOND]
    return {
        "router": router_name,
        "fasttrack": ft,
        "queues": etats,
        "enforced": sum(1 for e in plafonds if e["enforced"]),
        "leaking": sum(1 for e in plafonds if not e["enforced"]),
        "uncapped": len(etats) - len(plafonds),
        "counts": {v: sum(1 for e in etats if e["verdict"] == v) for v in GRAVITE},
    }

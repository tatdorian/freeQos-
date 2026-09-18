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

GRAVITE = (
    VERDICT_CONTOURNE,
    VERDICT_ABSENTE,
    VERDICT_MASQUEE,
    VERDICT_DESACTIVEE,
    VERDICT_ECART,
    VERDICT_EN_VIGUEUR,
)

# Un verdict autre que celui-la veut dire : le reseau PEUT depasser le plafond.
VERDICTS_QUI_LAISSENT_PASSER = frozenset(GRAVITE) - {VERDICT_EN_VIGUEUR}

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
                "pare-feu illisible avec ce compte : impossible de dire si le fasttrack "
                "contourne les files. A verifier a la main "
                "(/ip firewall filter print where action=fasttrack-connection)"
            ),
            "remedy": None,
        }
    if not regles:
        return {
            "active": False,
            "rules": [],
            "detail": "aucune regle fasttrack active : les files simples voient tout le trafic",
            "remedy": None,
        }
    return {
        "active": True,
        "rules": [r.to_dict() for r in regles],
        "detail": (
            f"{len(regles)} regle(s) fasttrack active(s) : les connexions etablies "
            "SAUTENT les files simples. Tant qu'elles sont en place, aucun plafond "
            "de ce routeur ne peut etre tenu, quelle que soit la file posee."
        ),
        "remedy": (
            "/ip firewall filter disable [find action=fasttrack-connection] "
            "(a passer sur le routeur : le controleur ne touche pas au pare-feu)"
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


def limit_state(
    spec: QueueSpec,
    *,
    rows: Sequence[dict[str, Any]],
    masquees: dict[str, dict[str, Any]],
    fasttrack: bool,
) -> LimitState:
    """Le verdict d'un plafond voulu, face a ce que le routeur porte vraiment.

    Le fasttrack l'emporte sur tout le reste : une file parfaite derriere un
    fasttrack actif ne bride rien, et annoncer "en vigueur" serait le pire des
    mensonges -- celui qui fait chercher la panne ailleurs.
    """
    index = {_nom(row): row for row in rows if _nom(row)}
    etat = LimitState(name=spec.name, target=spec.target, wanted=spec.max_limit)

    ligne = index.get(spec.name)
    if ligne is None:
        etat.verdict = VERDICT_ABSENTE
        etat.detail = "aucune file de ce nom sur le routeur : rien ne bride cet abonne"
        return etat

    etat.seen = str(ligne.get("max-limit") or "")
    etat.managed = MANAGED_COMMENT in str(ligne.get("comment") or "")

    if fasttrack:
        etat.verdict = VERDICT_CONTOURNE
        etat.detail = (
            "la file est en place, mais le fasttrack fait sauter les files simples "
            "aux connexions etablies : le plafond n'est pas tenu"
        )
        return etat
    if _vrai(ligne.get("disabled")):
        etat.verdict = VERDICT_DESACTIVEE
        etat.detail = "file desactivee sur le routeur : elle ne bride rien"
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
            f"le routeur porte {etat.seen or '-'} la ou le controleur veut "
            f"{spec.max_limit} : le plafond decide n'est pas celui qui s'applique"
        )
        return etat
    etat.detail = f"plafond {spec.max_limit} bits/s en vigueur sur {spec.target}"
    return etat


def audit_router(
    *,
    router_name: str,
    desired: Iterable[QueueSpec],
    rows: Sequence[dict[str, Any]],
    firewall: Sequence[dict[str, Any]] | None = (),
) -> dict[str, Any]:
    """L'audit complet d'un routeur : ce qui bride vraiment, et ce qui ne bride pas.

    ``firewall=None`` veut dire "pas lisible", et non "vide" : le verdict le dit
    alors explicitement au lieu de rassurer a tort.
    """
    regles = None if firewall is None else fasttrack_rules(firewall)
    ft = fasttrack_verdict(regles)
    masquees = masked_queues(rows)
    etats = [
        limit_state(spec, rows=rows, masquees=masquees, fasttrack=bool(regles)).to_dict()
        for spec in desired
    ]
    etats.sort(key=lambda e: (GRAVITE.index(str(e["verdict"])), str(e["name"])))
    return {
        "router": router_name,
        "fasttrack": ft,
        "queues": etats,
        "enforced": sum(1 for e in etats if e["enforced"]),
        "leaking": sum(1 for e in etats if not e["enforced"]),
        "counts": {v: sum(1 for e in etats if e["verdict"] == v) for v in GRAVITE},
    }

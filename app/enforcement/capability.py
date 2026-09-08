"""Ce que le compte configure a REELLEMENT le droit de faire.

Le controleur refusait d'ecrire tant qu'un compte ``qos-rw`` distinct n'etait pas
declare. C'etait verifier une DECLARATION, pas la realite : un exploitant dont le
compte principal possede deja la politique ``write`` se voyait refuser une
operation qu'il avait parfaitement le droit de faire.

On interroge donc le routeur : quel groupe porte ce compte, et quelles politiques
ce groupe accorde. En cas de doute — compte authentifie par RADIUS, ``/user`` non
lisible — on ne bloque pas : on tente la commande et on rapporte ce que RouterOS
repond vraiment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Politiques necessaires pour que l'enforcement fonctionne.
REQUIRED = ("api", "write")

# Le groupe integre "full" porte toutes les politiques sans les enumerer.
FULL_GROUPS = {"full"}


@dataclass(slots=True)
class WriteCapability:
    """Verdict sur la capacite d'ecriture d'un compte.

    ``can_write`` a trois etats et c'est voulu :
      True   le routeur confirme que le compte a les politiques necessaires ;
      False  le routeur confirme qu'il ne les a pas ;
      None   indeterminable — il faut essayer plutot que de bloquer.
    """

    username: str
    can_write: bool | None = None
    group: str | None = None
    policies: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def missing(self) -> list[str]:
        if self.can_write is not False:
            return []
        return [p for p in REQUIRED if p not in self.policies]

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "can_write": self.can_write,
            "group": self.group,
            "policies": self.policies,
            "missing_policies": self.missing,
            "detail": self.detail,
        }


def _split_policies(raw: Any) -> list[str]:
    """RouterOS rend la politique sous forme de liste separee par des virgules,
    avec parfois un prefixe '!' pour les politiques explicitement refusees."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        morceaux = list(raw)
    else:
        morceaux = str(raw).split(",")
    accordees = []
    for morceau in morceaux:
        nom = str(morceau).strip()
        if not nom or nom.startswith("!"):
            continue
        accordees.append(nom)
    return accordees


def inspect_write_capability(
    username: str,
    users: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> WriteCapability:
    """Deduit les droits du compte a partir de ce que le routeur a repondu."""
    verdict = WriteCapability(username=username)

    compte = next((u for u in users if str(u.get("name") or "").lower() == username.lower()), None)
    if compte is None:
        # Compte absent de /user : authentification RADIUS, ou lecture partielle.
        # On ne peut rien affirmer, donc on n'interdit rien.
        verdict.detail = (
            f"compte '{username}' absent de /user (authentification externe ?) : "
            "les droits seront verifies a l'execution"
        )
        return verdict

    groupe = str(compte.get("group") or "").strip()
    verdict.group = groupe or None

    if groupe.lower() in FULL_GROUPS:
        verdict.can_write = True
        verdict.policies = ["full"]
        verdict.detail = f"groupe '{groupe}' : tous les droits"
        return verdict

    definition = next(
        (g for g in groups if str(g.get("name") or "").lower() == groupe.lower()), None
    )
    if definition is None:
        verdict.detail = (
            f"groupe '{groupe}' introuvable dans /user/group : les droits seront "
            "verifies a l'execution"
        )
        return verdict

    verdict.policies = _split_policies(definition.get("policy"))
    manquantes = [p for p in REQUIRED if p not in verdict.policies]
    verdict.can_write = not manquantes
    verdict.detail = f"groupe '{groupe}' : {', '.join(verdict.policies) or 'aucune politique'}" + (
        f" — il manque {', '.join(manquantes)}" if manquantes else ""
    )
    return verdict


def permission_hint(error: Exception, username: str) -> str | None:
    """Traduit un refus de RouterOS en action concrete.

    RouterOS repond 'not enough permissions' sans dire laquelle manque : sans
    cette traduction, l'exploitant ne sait pas quoi corriger.
    """
    texte = f"{type(error).__name__}: {error}".lower()
    if "not enough permissions" in texte or "permission denied" in texte:
        return (
            f"Le compte '{username}' n'a pas les droits d'ecriture sur ce routeur. "
            "Ajoutez les politiques 'write' et 'api' a son groupe : "
            f"/user/group set [find name=<groupe>] policy=read,write,api,test"
        )
    return None

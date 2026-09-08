"""Enforcement : traduction de l'etat desire en commandes RouterOS.

PHASE 2. C'est le seul endroit du projet qui ecrit sur un equipement, et il ne
le fait que sur ordre explicite.

Trois garde-fous structurels :
  1. le planificateur est PUR : il produit des commandes comme donnees, sans
     rien envoyer. On peut les afficher, les relire, les tester ;
  2. rien n'est applique sans un appel distinct et un drapeau global actif ;
  3. seules les lignes portant notre commentaire de propriete sont modifiees.
     Une file creee par l'operateur ou par RADIUS n'est jamais touchee.
"""

from app.enforcement.models import (
    MANAGED_COMMENT,
    Plan,
    PlanAction,
    QueueSpec,
    QueueTypeSpec,
)
from app.enforcement.planner import build_plan, desired_state

__all__ = [
    "MANAGED_COMMENT",
    "Plan",
    "PlanAction",
    "QueueSpec",
    "QueueTypeSpec",
    "build_plan",
    "desired_state",
]

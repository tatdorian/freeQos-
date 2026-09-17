"""Rapprocher le PoP qu'un humain a SAISI et le PoP qu'un routeur PORTE.

LE PIEGE QUE CE MODULE EXISTE POUR FERMER
-----------------------------------------
Un client a IP fixe se declare avec un nom de PoP tape au clavier. Tout le reste
de la chaine -- la mesure, la planification, la file -- retrouve son routeur par
une EGALITE DE CHAINE avec le PoP du routeur. "francophonie" et "Francophonie"
sont alors deux sites differents, et le client declare sur le premier :

  - n'a pas de collecteur, donc aucun compteur, donc aucun debit affiche ;
  - n'entre dans l'etat desire d'aucun routeur, donc n'obtient jamais de file ;
  - fait naitre un PoP fantome dans la base, qui ressemble a s'y meprendre au vrai.

Rien ne tombe en panne : le client existe, sa fiche est correcte, et il ne
remonte simplement jamais. C'est la pire categorie de defaut -- silencieux, et
indiscernable d'un client muet.

CE QUI EST TOLERE, ET CE QUI NE L'EST PAS
-----------------------------------------
La casse, les accents, la ponctuation, les espaces en trop et le mot "PoP" lui-
meme : "PoP Francophonie", "pop-francophonie" et "Francophonie" designent le
meme site pour tout exploitant, et le controleur doit les lire ainsi.

En revanche, deux PoP REELLEMENT distincts qui se ressembleraient apres cette
normalisation ne sont jamais fusionnes : la resolution rend alors "ambigu" et
n'en choisit aucun. Choisir au hasard poserait une file sur le mauvais site, ce
qui est pire que de ne rien poser -- et bien plus difficile a voir.

L'egalite exacte garde la priorite : tant que le nom saisi correspond au
caractere pres, la tolerance ne sert a rien et ne peut rien casser.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.collectors.mikrotik import MikrotikCollector

logger = logging.getLogger(__name__)

# Mots qui ne distinguent pas deux sites : ils decrivent la NATURE du lieu, pas
# son nom. Les retirer en tete de chaine fait tomber l'ecart le plus courant
# entre l'inventaire des routeurs et la saisie d'un client.
_MOTS_VIDES = frozenset({"pop", "pops", "site", "nro"})

RESOLUTION_EXACTE = "exact"
RESOLUTION_NORMALISEE = "normalise"
RESOLUTION_AMBIGUE = "ambigu"
RESOLUTION_INCONNUE = "inconnu"


def normalise_pop(value: str | None) -> str:
    """Forme comparable d'un nom de PoP.

    Accents retires, casse pliee, ponctuation reduite a des espaces, et le mot
    generique de tete ecarte. ``"PoP Francophonie"`` et ``"francophonie"``
    rendent tous deux ``"francophonie"``.
    """
    texte = unicodedata.normalize("NFKD", str(value or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    mots = [mot for mot in re.split(r"[^0-9a-zA-Z]+", texte.casefold()) if mot]
    while mots and mots[0] in _MOTS_VIDES:
        mots = mots[1:]
    return " ".join(mots)


@dataclass(slots=True)
class PopMatch:
    """Le resultat d'une resolution, avec DE QUOI l'expliquer.

    ``reason`` n'est pas decoratif : c'est lui qui permet de dire a l'exploitant
    "aucun routeur ne porte ce PoP" plutot que de lui rendre une liste vide.
    """

    collectors: list[MikrotikCollector] = field(default_factory=list)
    pop_name: str | None = None
    resolution: str = RESOLUTION_INCONNUE

    @property
    def found(self) -> bool:
        return bool(self.collectors)


def pop_names(collectors: Sequence[MikrotikCollector]) -> list[str]:
    """Les PoP reellement collectes, tels qu'ils s'ecrivent."""
    return sorted({c.config.effective_pop_name for c in collectors})


def resolve_pop(declared: str | None, collectors: Sequence[MikrotikCollector]) -> PopMatch:
    """Les routeurs qui portent ce PoP. Plusieurs routeurs par PoP sont normaux.

    Un PoP peut avoir deux routeurs (redondance, separation acces/coeur) : tous
    sont rendus, et c'est voulu. Une file posee sur un routeur qui ne voit pas
    passer ce trafic ne bride rien, alors que l'oublier sur celui qui le voit
    laisse un client non bride.
    """
    saisi = str(declared or "").strip()
    exacts = [c for c in collectors if c.config.effective_pop_name == saisi]
    if exacts:
        return PopMatch(exacts, exacts[0].config.effective_pop_name, RESOLUTION_EXACTE)

    cible = normalise_pop(saisi)
    if not cible:
        return PopMatch([], None, RESOLUTION_INCONNUE)

    proches = [c for c in collectors if normalise_pop(c.config.effective_pop_name) == cible]
    if not proches:
        return PopMatch([], None, RESOLUTION_INCONNUE)

    noms = {c.config.effective_pop_name for c in proches}
    if len(noms) > 1:
        # Deux PoP distincts que la normalisation rapprocherait. En choisir un
        # poserait une file sur le mauvais site : on refuse, et on le dit.
        logger.warning(
            "PoP '%s' ambigu : %s se ressemblent apres normalisation, aucun n'est choisi",
            saisi,
            ", ".join(sorted(noms)),
        )
        return PopMatch([], None, RESOLUTION_AMBIGUE)

    logger.info(
        "PoP '%s' rapproche de '%s' (casse, accent ou mot 'PoP' pres)",
        saisi,
        proches[0].config.effective_pop_name,
    )
    return PopMatch(proches, proches[0].config.effective_pop_name, RESOLUTION_NORMALISEE)


def explain(match: PopMatch, declared: str | None, collectors: Sequence[MikrotikCollector]) -> str:
    """Phrase rendue a l'exploitant quand la resolution ne donne rien.

    Elle nomme les PoP qui existent : sans eux, "PoP inconnu" laisse chercher
    une faute de frappe a l'aveugle.
    """
    connus = pop_names(collectors)
    liste = ", ".join(f"'{nom}'" for nom in connus) or "aucun routeur n'est collecte"
    if match.resolution == RESOLUTION_AMBIGUE:
        return (
            f"Le PoP '{declared}' correspond a plusieurs PoP collectes a la casse et "
            f"aux accents pres. Reprenez le nom exact : {liste}."
        )
    return (
        f"Aucun routeur collecte ne porte le PoP '{declared}'. Tant que ce sera le cas, "
        f"ce client n'aura ni debit mesure ni file. PoP collectes : {liste}."
    )

"""Le premier compte Administrateur, cree TOUT SEUL au premier demarrage.

Sans lui, la premiere visite de l'interface proposait de « creer le premier
compte » : le premier venu qui trouvait l'adresse du serveur pouvait se
declarer administrateur. Desormais, quand aucun compte n'existe, l'application
en cree un au demarrage :

- email : ``FREEQOS_ADMIN_EMAIL`` (defaut ``admin@freeqos.local``) ;
- mot de passe : tire au hasard, ecrit dans ``data/first-admin.txt`` (lisible
  par le seul compte du service), JAMAIS dans le journal ;
- le fichier disparait des que ce mot de passe est change.

    docker compose exec app cat data/first-admin.txt
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.db.users_repo import UsersStore
from app.services.accounts import (
    InvalidAccountError,
    generate_password,
    hash_password,
    normalise_email,
    verify_password,
)

logger = logging.getLogger(__name__)

DEFAULT_EMAIL = "admin@freeqos.local"


def read_file(path: Path) -> dict[str, str]:
    """``email`` et ``password`` du fichier, ou {} s'il manque."""
    try:
        lignes = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    champs: dict[str, str] = {}
    for ligne in lignes:
        cle, sep, valeur = ligne.partition(":")
        if sep and cle.strip() in {"email", "password"}:
            champs[cle.strip()] = valeur.strip()
    return champs


def _write_file(path: Path, email: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    contenu = (
        "freeQoS -- first Administrator account\n"
        f"email: {email}\n"
        f"password: {password}\n"
        "Log in, then change this password (account menu): this file is then deleted.\n"
    )
    # Cree directement en 0600 : jamais lisible par d'autres, meme un instant.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fichier:
        fichier.write(contenu)


def forget_file(path: Path | None, email: str) -> None:
    """Efface le fichier quand SON compte change de mot de passe."""
    if path is None:
        return
    if read_file(path).get("email") == email:
        try:
            path.unlink()
            logger.info("Mot de passe initial change : %s efface", path)
        except OSError:
            pass


async def ensure_first_admin(
    store: UsersStore, path: Path | None, email: str | None
) -> dict[str, Any] | None:
    """Cree le premier Administrateur si AUCUN compte n'existe. Rend sa fiche
    (sans secret), ou None si un compte existait deja."""
    if await store.count():
        # Le fichier n'a plus lieu d'etre si son mot de passe ne marche plus
        # (change entre-temps, ou compte supprime).
        if path is not None and path.exists():
            champs = read_file(path)
            fiche = await store.credentials(champs.get("email", ""))
            if fiche is None or not verify_password(
                champs.get("password", ""), fiche.get("password_hash")
            ):
                forget_file(path, champs.get("email", ""))
        return None
    try:
        adresse = normalise_email(email or DEFAULT_EMAIL)
    except InvalidAccountError:
        logger.error("FREEQOS_ADMIN_EMAIL invalide (%r) : %s utilise", email, DEFAULT_EMAIL)
        adresse = DEFAULT_EMAIL
    mot = generate_password()
    fiche = await store.create_first(email=adresse, password_hash=hash_password(mot))
    if fiche is None:  # un autre demarrage l'a cree a l'instant
        return None
    await store.record_event("setup", email=adresse, actor="startup", detail="first account")
    if path is not None:
        try:
            _write_file(path, adresse, mot)
            ou = f"in {path} (docker compose exec app cat {path})"
        except OSError as exc:
            ou = f"NOT written ({exc}): reset it with python -m app.cli"
    else:
        ou = "not written (FIRST_ADMIN_FILE empty)"
    logger.warning("Premier compte Administrateur cree : %s -- mot de passe %s", adresse, ou)
    return {k: v for k, v in fiche.items() if k != "password_hash"}

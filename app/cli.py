"""Commandes d'exploitation, lancees dans le conteneur :

    docker compose exec app python -m app.cli create-admin --email noc@example.com

``count-accounts`` affiche le nombre de comptes. ``create-admin`` cree le
PREMIER compte, Administrateur, au deploiement : le serveur n'est jamais
expose avec un ecran « creer le premier compte » ouvert a qui le trouve le
premier. Sans ``--password-stdin``, un mot de passe solide est
tire au hasard et affiche UNE fois.

Codes de sortie : 0 cree, 2 saisie refusee, 3 un compte existe deja (rien
n'est change : c'est le cas normal d'une mise a jour), 1 base injoignable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

import asyncpg

from app.config import Settings
from app.db.users_repo import UsersRepository, UsersStore
from app.services.accounts import (
    InvalidAccountError,
    check_password,
    generate_password,
    hash_password,
    normalise_email,
)

SORTIE_CREE = 0
SORTIE_BASE = 1
SORTIE_SAISIE = 2
SORTIE_EXISTE = 3


async def create_admin(depot: UsersStore, email: str, password: str | None) -> tuple[int, str]:
    """Cree le premier compte Administrateur. Rend (code de sortie, message)."""
    try:
        email = normalise_email(email)
        mot = check_password(password, email=email) if password else generate_password()
    except InvalidAccountError as exc:
        return SORTIE_SAISIE, f"refused: {exc}"
    fiche = await depot.create_first(email=email, password_hash=hash_password(mot))
    if fiche is None:
        return SORTIE_EXISTE, "an account already exists: nothing changed"
    await depot.record_event(
        "setup", email=email, actor="install", detail="first account (command line)"
    )
    if password:
        return SORTIE_CREE, f"Administrator account created: {email}"
    return SORTIE_CREE, f"Administrator account created: {email}\npassword: {mot}"


async def reset_password(depot: UsersStore, email: str) -> tuple[int, str]:
    """Nouveau mot de passe tire au hasard pour un compte existant (oubli,
    ou fichier du premier compte perdu)."""
    try:
        email = normalise_email(email)
    except InvalidAccountError as exc:
        return SORTIE_SAISIE, f"refused: {exc}"
    fiche = await depot.credentials(email)
    if fiche is None:
        return SORTIE_SAISIE, f"no account {email}"
    mot = generate_password()
    await depot.update(int(fiche["id"]), password_hash=hash_password(mot), disabled=False)
    await depot.close_sessions_of(int(fiche["id"]))
    await depot.record_event(
        "password_reset", email=email, actor="command line", detail="reset from the server"
    )
    return SORTIE_CREE, f"New password for {email}\npassword: {mot}"


async def count_accounts(depot: UsersStore) -> tuple[int, str]:
    return SORTIE_CREE, str(await depot.count())


Action = Callable[[UsersStore], Awaitable[tuple[int, str]]]


async def _avec_la_base(dsn: str, action: Action) -> tuple[int, str]:
    derniere: Exception | None = None
    # L'application cree ses tables au demarrage : on attend qu'elles soient la.
    for _ in range(30):
        try:
            pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        except (OSError, asyncpg.PostgresError) as exc:
            derniere = exc
            await asyncio.sleep(2)
            continue
        try:
            return await action(UsersRepository(pool))
        except asyncpg.UndefinedTableError as exc:
            derniere = exc
            await asyncio.sleep(2)
        finally:
            await pool.close()
    return SORTIE_BASE, f"database unreachable: {derniere}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sous = parser.add_subparsers(dest="commande", required=True)
    admin = sous.add_parser("create-admin", help="create the first Administrator account")
    admin.add_argument("--email", required=True)
    admin.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input instead of generating one",
    )
    sous.add_parser("count-accounts", help="print how many accounts exist (0 on a new install)")
    reset = sous.add_parser("reset-password", help="give an account a new random password")
    reset.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    if args.commande == "count-accounts":
        action: Action = count_accounts
    elif args.commande == "reset-password":

        async def action(depot: UsersStore) -> tuple[int, str]:
            return await reset_password(depot, args.email)

    else:
        mot = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None

        async def action(depot: UsersStore) -> tuple[int, str]:
            return await create_admin(depot, args.email, mot)

    code, message = asyncio.run(_avec_la_base(Settings().asyncpg_dsn, action))
    print(message, file=sys.stdout if code == SORTIE_CREE else sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

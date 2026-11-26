"""Les clients RETIRES de freeQoS : ils ne reviennent pas, meme connectes.

LE BESOIN. Garder dans freeQoS les clients que l'on veut, et en retirer
d'autres pour de bon. Supprimer un client ne suffisait pas : freeQoS lit les
sessions PPPoE actives sur le routeur, et un client encore connecte revenait au
releve suivant (dix secondes plus tard), avec ses mesures.

CE QUE FAIT LE RETRAIT. Le login est inscrit ici. Des lors :

- le releve des sessions l'ignore (ni fiche, ni mesure, ni historique) ;
- le calcul des files l'ignore, et la file que FREEQOS avait posee pour lui --
  reconnaissable a son nom -- est retiree a la premiere ecriture, puis plus
  jamais reposee. Rien d'autre du client n'est touche sur le routeur (compte
  PPPoE, session, adresse) : il est retire de freeQoS, pas du routeur ;
- il n'apparait plus dans aucune page.

Le retablir le retire de cette liste : il revient au releve suivant, comme un
client nouvellement detecte (sans forfait, donc sans file).

La liste est petite et lue a chaque cycle : elle est gardee en memoire,
rechargee de la base au demarrage et a chaque modification.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ExcludedClients:
    """La liste des clients retires, en base et en memoire."""

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool
        self._logins: set[str] = set()

    def __contains__(self, login: object) -> bool:
        return isinstance(login, str) and login in self._logins

    @property
    def logins(self) -> frozenset[str]:
        return frozenset(self._logins)

    async def load(self) -> None:
        """Relit la liste. Une base illisible garde la liste connue : un client
        retire ne doit pas reapparaitre le temps d'une coupure."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("SELECT login FROM excluded_clients")
        except Exception:  # noqa: BLE001
            logger.exception("Liste des clients retires illisible : la precedente est gardee")
            return
        self._logins = {str(r["login"]) for r in rows}

    async def list(self) -> builtins.list[dict[str, Any]]:
        if self._pool is None:
            return [
                {"login": login, "removed_by": None, "removed_at": None}
                for login in sorted(self._logins)
            ]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT login, removed_by, removed_at FROM excluded_clients ORDER BY login"
            )
        return [dict(r) for r in rows]

    async def add(self, logins: builtins.list[str], actor: str | None) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO excluded_clients (login, removed_by) VALUES ($1, $2) "
                    "ON CONFLICT (login) DO NOTHING",
                    [(login, actor) for login in logins],
                )
        self._logins |= set(logins)

    async def remove(self, login: str) -> bool:
        trouve = login in self._logins
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                resultat = await conn.execute(
                    "DELETE FROM excluded_clients WHERE login = $1", login
                )
            trouve = trouve or not resultat.endswith(" 0")
        self._logins.discard(login)
        return trouve

"""Les dernieres erreurs inattendues du serveur, gardees en memoire.

Une erreur 500 s'affichait dans l'interface sans dire laquelle : il fallait
aller lire ``docker compose logs app`` sur le serveur. Les 50 dernieres sont
gardees ici (quand, quelle requete, quelle exception) et lisibles par un
Administrator depuis l'interface. Rien n'est ecrit sur disque : un redemarrage
les efface, le journal du conteneur reste la trace complete.
"""

from __future__ import annotations

import secrets
from collections import deque
from datetime import UTC, datetime
from typing import Any

MAX_ERREURS = 50


class ServerErrors:
    def __init__(self, taille: int = MAX_ERREURS) -> None:
        self._erreurs: deque[dict[str, Any]] = deque(maxlen=taille)

    def record(self, method: str, path: str, exc: BaseException) -> str:
        """Retient l'erreur, rend une reference courte (dans le journal et la reponse)."""
        ref = secrets.token_hex(3)
        self._erreurs.appendleft(
            {
                "ref": ref,
                "at": datetime.now(tz=UTC).isoformat(),
                "method": method,
                "path": path,
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            }
        )
        return ref

    def list(self) -> list[dict[str, Any]]:
        return list(self._erreurs)

    def clear(self) -> None:
        self._erreurs.clear()

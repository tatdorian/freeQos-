"""L'echec de lecture d'un routeur, dit en clair, et le pool de threads des
appels RouterOS assez large pour que les sondes ne bloquent pas les lectures."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.collection import describe_read_error


@pytest.mark.parametrize(
    ("erreur", "attendu"),
    [
        (TimeoutError(), "no answer in time"),
        (ConnectionRefusedError(111, "Connection refused"), "API service is off or restricted"),
        (Exception("invalid user name or password (6)"), "login refused"),
        (OSError(113, "No route to host"), "no route to the router"),
        (ValueError("autre chose"), "ValueError: autre chose"),
    ],
)
def test_une_lecture_ratee_se_dit_en_clair(erreur: BaseException, attendu: str) -> None:
    assert attendu in describe_read_error(erreur)


def test_les_appels_routeros_ont_assez_de_threads() -> None:
    assert Settings(_env_file=None).io_threads >= 32

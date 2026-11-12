# ruff: noqa: F811 - les fixtures importees portent le nom des parametres
"""Aucun secret ne repart vers le navigateur.

Des secrets REELS sont enregistres par l'API (mot de passe de routeur, jeton
UISP, mot de passe d'antenne, cles d'API, mot de passe du compte), puis CHAQUE
route GET est appelee en Administrateur. Aucune reponse, ni la page, ni le
JavaScript servi, ne doit contenir l'un d'eux ni un champ chiffre ou une
empreinte de mot de passe.

Ignore sans ``TEST_DATABASE_URL`` (cf. test_bout_en_bout_api_externe).
"""

from __future__ import annotations

import re

from tests.test_bout_en_bout_api_externe import (  # noqa: F401 - fixtures
    _cles,
    app_client,
    pytestmark,
    routeur,
)
from tests.test_modes_bout_en_bout import _brancher

MDP_COMPTE = "Baobab-Sahel-Niamey-27"
JETON_UISP = "jeton-uisp-0123456789-secret"
MDP_ANTENNE = "antenne-mdp-9876543210"
MDP_ROUTEUR = "secret-routeur"

#: Des NOMS de champ qui ne doivent jamais sortir, quelle que soit leur valeur.
CHAMPS_INTERDITS = re.compile(
    r'"(password|password_hash|password_enc|token|token_enc|secret|secret_enc|'
    r'key_hash|token_hash|api_key|private_key)"\s*:\s*"[^"]'
)

#: Valeurs a donner aux parametres de chemin pour que la route reponde.
PARAMETRES = {
    "login": "dupont",
    "name": "nas-test",
    "router_name": "nas-test",
    "pop_name": "PoP Test",
    "site": "PoP Test",
}


def _chemin(gabarit: str) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: PARAMETRES.get(m.group(1), "1"), gabarit)


def test_aucune_reponse_ne_rend_un_secret(app_client, routeur, monkeypatch) -> None:
    import app.api.uisp as module

    class FauxUisp:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def raw_devices(self) -> list[dict]:
            return []

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(module, "UispProvider", FauxUisp)

    # --- des secrets reels, enregistres comme le ferait l'interface
    ecriture, lecture = _cles(app_client)
    cle = {"Authorization": f"Bearer {ecriture}"}
    _brancher(app_client, cle)  # routeur avec MDP_ROUTEUR
    r = app_client.put(
        "/api/v1/uisp", headers=cle, json={"base_url": "https://uisp.test", "token": JETON_UISP}
    )
    assert r.status_code == 200, r.text
    r = app_client.post(
        "/api/v1/pops/antennas",
        headers=cle,
        json={
            "name": "ap-1",
            "pop_name": "PoP Test",
            "host": "192.0.2.50",
            "password": MDP_ANTENNE,
        },
    )
    assert r.status_code == 201, r.text
    r = app_client.post(
        "/api/v1/auth/setup", json={"email": "noc@example.com", "password": MDP_COMPTE}
    )
    assert r.status_code == 200, r.text  # session Administrateur (cookie)

    secrets = [MDP_COMPTE, JETON_UISP, MDP_ANTENNE, MDP_ROUTEUR, ecriture, lecture]
    gabarits = sorted(
        chemin
        for chemin, operations in app_client.app.openapi()["paths"].items()
        if "get" in operations and chemin.startswith("/api/") and "stream" not in chemin
    )
    assert len(gabarits) > 80, "les routes de l'API ne sont pas toutes enregistrees"

    appelees = 0
    for gabarit in [*gabarits, "/", "/static/app.js", "/static/api-guide.js", "/documentation"]:
        chemin = _chemin(gabarit)
        reponse = app_client.get(chemin)
        if reponse.status_code >= 500:
            raise AssertionError(f"{chemin}: {reponse.status_code} {reponse.text[:200]}")
        corps = reponse.text
        appelees += reponse.status_code == 200
        for s in secrets:
            assert s not in corps, f"{chemin} rend un secret"
        assert not CHAMPS_INTERDITS.search(corps), f"{chemin}: {CHAMPS_INTERDITS.search(corps)}"
        assert "scrypt$" not in corps, f"{chemin} rend une empreinte de mot de passe"
    assert appelees > 60

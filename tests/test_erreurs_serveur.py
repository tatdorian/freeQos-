"""« This page could not be loaded · Unexpected error on the server ».

CE QUI EST FIXE ICI
-------------------
1. UNE ERREUR PASSAGERE NE S'AFFICHE PAS : le chargement est retente une fois
   (donnees fraiches) avant de montrer quoi que ce soit.
2. UNE PAGE DEJA AFFICHEE GARDE SES DONNEES : un rafraichissement rate ne
   colle plus le grand encart rouge, la pastille dit « Update failed ».
3. L'ENCART DIT QUELLE REQUETE a echoue.
4. LE SERVEUR RETIENT SES ERREURS INATTENDUES (quand, quelle requete, quelle
   exception) et un Administrator les lit dans l'interface, sans aller lire
   le journal du conteneur. La reponse ne contient jamais la trace.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from app.api import accounts
from app.config import Settings
from app.main import create_app
from app.services.server_errors import ServerErrors
from app.web.ui import STATIC_DIR

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _app_qui_plante(role: str = "edit") -> TestClient:
    app = create_app(Settings(_env_file=None, auth_enabled=True))

    async def compte(request: Request) -> dict[str, str]:
        request.state.user = {"role": role}
        return {"role": role}

    app.dependency_overrides[accounts.require_access] = compte

    @app.get("/api/v1/_test_plante")
    async def plante() -> None:
        raise RuntimeError("base injoignable : mot-de-passe=secret")

    return TestClient(app, raise_server_exceptions=False)


def test_une_erreur_inattendue_est_retenue_et_lisible() -> None:
    client = _app_qui_plante()
    r = client.get("/api/v1/_test_plante")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail.startswith("RuntimeError (ref ")
    # Le message de l'exception ne part pas dans la reponse.
    assert "secret" not in r.text

    liste = client.get("/api/v1/status/errors").json()
    assert liste["count"] == 1
    erreur = liste["errors"][0]
    assert erreur["path"] == "/api/v1/_test_plante"
    assert erreur["method"] == "GET"
    assert erreur["error"] == "RuntimeError"
    assert erreur["ref"] in detail

    assert client.delete("/api/v1/status/errors").status_code == 200
    assert client.get("/api/v1/status/errors").json()["count"] == 0


def test_la_liste_est_bornee() -> None:
    erreurs = ServerErrors(taille=3)
    for i in range(5):
        erreurs.record("GET", f"/x/{i}", ValueError(str(i)))
    chemins = [e["path"] for e in erreurs.list()]
    assert chemins == ["/x/4", "/x/3", "/x/2"]


def test_la_liste_est_reservee_aux_administrateurs() -> None:
    for role in ("read", "operator"):
        client = _app_qui_plante(role)
        assert client.get("/api/v1/status/errors").status_code == 403
        assert client.delete("/api/v1/status/errors").status_code == 403


def test_le_chargement_retente_une_fois_avant_d_afficher() -> None:
    corps = JS[JS.index("async function refresh()") :]
    corps = corps[: corps.index("\nfunction route()")]
    assert "err.status === 0 || err.status >= 500" in corps
    assert "setTimeout(ok, 1500)" in corps
    # Deux appels du chargeur : le premier, et la seconde chance.
    assert corps.count("await LOADERS[vue]();") == 2
    # Une page deja affichee n'affiche pas l'encart.
    assert "if (!DIRECT.ok) appError(err);" in corps


def test_l_encart_nomme_la_requete_en_cause() -> None:
    assert "e.path = path;" in JS
    assert "Failed request: <code>" in JS
    assert "ouvrirErreursServeur" in JS

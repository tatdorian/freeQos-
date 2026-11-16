"""Le HTTP simple est redirige vers https://<domaine> des que le HTTPS a fait ses preuves.

CE QUE CES TESTS FIXENT
-----------------------
1. SANS DOMAINE, ou tant qu'aucune visite n'est arrivee en HTTPS, rien n'est
   redirige : on ne s'enferme pas dehors pendant que le certificat arrive.
2. LA PREMIERE REQUETE REELLE EN https://<domaine> est la preuve : elle est
   notee, et des lors l'adresse IP et le port 8000 renvoient (308, meme chemin,
   meme requete) vers le domaine. La page de connexion n'est plus servie en clair.
3. /health reste en HTTP (la sonde Docker) ; une requete deja adressee au
   domaine n'est jamais redirigee (pas de boucle).
4. CHANGER DE DOMAINE remet la preuve a zero ; le retirer coupe la redirection ;
   HTTPS_REDIRECT=false la coupe aussi.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, install_https_redirect
from app.services.public_access import HttpsRedirect

DOMAINE = "qos.example.com"


class FauxDepot:
    def __init__(self, domaine: str | None = DOMAINE) -> None:
        self.fiche: dict[str, Any] | None = (
            {"domain": domaine, "https_confirmed_at": None} if domaine else None
        )
        self.confirmations = 0

    async def get(self) -> dict[str, Any] | None:
        return dict(self.fiche) if self.fiche else None

    async def confirm_https(self, domain: str) -> None:
        if self.fiche and self.fiche["domain"] == domain:
            self.confirmations += 1
            self.fiche["https_confirmed_at"] = datetime.now(tz=UTC)


def appli(depot: FauxDepot, *, actif: bool = True) -> tuple[FastAPI, HttpsRedirect]:
    app = FastAPI()
    install_https_redirect(app, Settings(_env_file=None, https_redirect=actif), repo=depot)

    @app.get("/")
    async def accueil() -> dict[str, str]:
        return {"page": "login"}

    @app.get("/health")
    async def sante() -> dict[str, str]:
        return {"status": "ok"}

    garde: HttpsRedirect = app.state.https_redirect
    garde.REFRESH_S = 0  # relire a chaque requete : le test change la base
    return app, garde


def par_ip(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://192.0.2.10:8000", follow_redirects=False)


def par_domaine(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"https://{DOMAINE}", follow_redirects=False)


# ============================================================ 1. pas trop tot


def test_sans_domaine_le_http_reste_servi() -> None:
    app, _ = appli(FauxDepot(domaine=None))
    assert par_ip(app).get("/").status_code == 200


def test_domaine_pose_mais_https_pas_encore_vu_rien_n_est_redirige() -> None:
    app, _ = appli(FauxDepot())
    assert par_ip(app).get("/").status_code == 200


# ============================================================ 2. la preuve


def test_la_premiere_visite_https_active_la_redirection() -> None:
    depot = FauxDepot()
    app, garde = appli(depot)
    assert par_domaine(app).get("/").status_code == 200
    assert depot.confirmations == 1
    assert garde.active

    r = par_ip(app).get("/", params={"x": "1"})
    assert r.status_code == 308
    assert r.headers["location"] == f"https://{DOMAINE}/?x=1"
    # Une ecriture garde sa methode (308, pas 302).
    r = par_ip(app).post("/api/v1/auth/login", json={})
    assert r.status_code == 308
    assert r.headers["location"] == f"https://{DOMAINE}/api/v1/auth/login"


def test_la_preuve_n_est_ecrite_qu_une_fois() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    for _ in range(3):
        par_domaine(app).get("/")
    assert depot.confirmations == 1


def test_une_visite_https_sur_une_autre_adresse_ne_prouve_rien() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    TestClient(app, base_url="https://192.0.2.10").get("/")
    assert depot.confirmations == 0
    assert par_ip(app).get("/").status_code == 200


# ============================================================ 3. exceptions


def test_la_sonde_docker_reste_en_http() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    par_domaine(app).get("/")
    assert par_ip(app).get("/health").status_code == 200


def test_pas_de_boucle_si_le_proxy_ne_transmet_pas_le_schema() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    par_domaine(app).get("/")
    # Le proxy a parle en HTTP a l'application, sans X-Forwarded-Proto cru.
    client = TestClient(app, base_url=f"http://{DOMAINE}", follow_redirects=False)
    assert client.get("/").status_code == 200
    assert par_ip(app).get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 200


# ============================================================ 4. revenir en arriere


def test_changer_de_domaine_remet_la_preuve_a_zero() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    par_domaine(app).get("/")
    depot.fiche = {"domain": "autre.example.com", "https_confirmed_at": None}
    assert par_ip(app).get("/").status_code == 200


def test_retirer_le_domaine_coupe_la_redirection() -> None:
    depot = FauxDepot()
    app, _ = appli(depot)
    par_domaine(app).get("/")
    depot.fiche = None
    assert par_ip(app).get("/").status_code == 200


def test_https_redirect_false_laisse_le_http_ouvert() -> None:
    depot = FauxDepot()
    depot.fiche["https_confirmed_at"] = datetime.now(tz=UTC)  # type: ignore[index]
    app, garde = appli(depot, actif=False)
    assert not garde.active
    assert par_ip(app).get("/").status_code == 200


def test_base_injoignable_rien_ne_casse() -> None:
    class Panne(FauxDepot):
        async def get(self) -> dict[str, Any] | None:
            raise OSError("base absente")

    app, _ = appli(Panne())
    assert par_ip(app).get("/").status_code == 200


# ================================================== bout en bout, vraie base

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL absent")
def test_bout_en_bout_la_preuve_est_gardee_en_base(tmp_path: Path) -> None:
    async def sql(requete: str, *args: Any) -> Any:
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetchval(requete, *args)
        finally:
            await conn.close()

    reglages = Settings(
        _env_file=None,
        database_url=DSN,
        auth_enabled=True,
        scheduler_enabled=False,
        netflow_enabled=False,
        rtt_enabled=False,
        routers=[],
        routers_file=None,
        app_secret_key_file=tmp_path / "secret.key",
        proxy_admin_url=None,
    )
    app = create_app(reglages)
    with TestClient(app, base_url="http://192.0.2.10:8000", follow_redirects=False) as ip:
        asyncio.run(sql("DELETE FROM public_access"))
        asyncio.run(sql("INSERT INTO public_access (id, domain) VALUES (1, $1)", DOMAINE))
        app.state.https_redirect.invalidate()
        assert ip.get("/").status_code == 200

        # Meme client (meme boucle que le pool), adresse absolue en https.
        assert ip.get(f"https://{DOMAINE}/").status_code == 200
        assert asyncio.run(sql("SELECT https_confirmed_at IS NOT NULL FROM public_access"))
        r = ip.get("/")
        assert r.status_code == 308
        assert r.headers["location"] == f"https://{DOMAINE}/"

        # Le meme domaine re-enregistre garde sa preuve ; un autre la perd.
        depot = app.state.container.public_access_repo
        ip.portal.call(depot.save, DOMAINE, None, "test")
        assert asyncio.run(sql("SELECT https_confirmed_at IS NOT NULL FROM public_access"))
        ip.portal.call(depot.save, "autre.example.com", None, "test")
        assert asyncio.run(sql("SELECT https_confirmed_at IS NULL FROM public_access"))
        app.state.https_redirect.invalidate()
        assert ip.get("/").status_code == 200
        asyncio.run(sql("DELETE FROM public_access"))
    # Ce demarrage a amorce runtime_flags (ecriture coupee) : ne pas le leguer.
    asyncio.run(sql("DELETE FROM runtime_flags"))

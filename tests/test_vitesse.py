"""Vitesse de l'interface : ce qui part sur le reseau, et combien de fois.

CE QUE CES TESTS FIXENT
-----------------------
1. TOUT CE QUI EST GROS EST COMPRIME : le JavaScript de l'interface (850 Ko
   bruts) et le JSON de l'API (environ 750 octets par client) partent en gzip.
2. LES FICHIERS A EMPREINTE NE SONT TELECHARGES QU'UNE FOIS : ``app.js?v=...``
   est garde un an par le navigateur (l'adresse change a chaque version) ; sans
   empreinte, il revalide ; l'API n'est jamais mise en cache.
3. LE NAVIGATEUR NE DEMANDE PAS DEUX FOIS LA MEME CHOSE : une lecture identique
   en cours est partagee, une reponse recente reutilisee, et toute ecriture
   vide ce cache (un geste se voit tout de suite).
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.ui import STATIC_DIR, asset_version

JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, auth_enabled=True)))


def test_le_javascript_part_compresse_et_garde_un_an() -> None:
    client = _client()
    v = asset_version("app.js")
    r = client.get(f"/static/app.js?v={v}", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Decompresse par le client de test : c'est bien le fichier entier.
    assert len(r.content) > 500_000


def test_un_fichier_sans_empreinte_est_revalide() -> None:
    r = _client().get("/static/app.css")
    assert r.headers["cache-control"] == "no-cache"


def test_la_page_et_l_api_ne_sont_jamais_gardees() -> None:
    client = _client()
    assert client.get("/").headers["cache-control"] == "no-store"
    r = client.get("/api/v1/auth/status", headers={"Accept-Encoding": "gzip"})
    assert r.headers["cache-control"] == "no-store"


def test_la_page_reference_les_fichiers_avec_leur_empreinte() -> None:
    page = _client().get("/").text
    assert f"/static/app.js?v={asset_version('app.js')}" in page
    assert f"/static/app.css?v={asset_version('app.css')}" in page


def test_un_gros_json_part_compresse() -> None:
    from fastapi import FastAPI

    from app.main import create_app as _creer

    app: FastAPI = _creer(Settings(_env_file=None, auth_enabled=True))

    @app.get("/api/v1/_test_gros")
    async def gros() -> list[dict[str, str]]:
        return [{"login": f"client-{i:04d}", "pop_name": "North Tower"} for i in range(2000)]

    r = TestClient(app).get("/api/v1/_test_gros", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.json()) == 2000


def test_le_navigateur_partage_et_reutilise_les_lectures() -> None:
    assert "const CACHE_GET_MS = 5000;" in JS
    corps = JS[JS.index("async function api(path, options)") :]
    corps = corps[: corps.index("\n}\n")]
    # Une ecriture vide le cache, avant ET apres.
    assert re.search(r"if \(methode !== 'GET'\) \{\s*viderCacheApi\(\);", corps)
    assert "finally { viderCacheApi(); }" in corps
    # Les routes de session ne sont jamais mises en cache.
    assert "path.indexOf('/auth/') === 0" in corps
    # Chaque appelant recoit sa copie : un tri sur place ne contamine pas les autres.
    assert "return copieApi(" in corps
    # Le bouton « Rafraichir » relit tout.
    assert "viderCacheApi(); refresh();" in JS


def test_le_rafraichissement_automatique_lit_toujours_du_frais() -> None:
    """Le cache (5 s) expire avant le rafraichissement automatique (10 s)."""
    boucle = (
        r"if \(state\.link\) openLink\(state\.link\.key, "
        r"state\.link\.minutes, true\);\n\}, (\d+)\);"
    )
    m = re.search(boucle, JS)
    assert m, "boucle de rafraichissement introuvable"
    assert int(m.group(1)) > 5000

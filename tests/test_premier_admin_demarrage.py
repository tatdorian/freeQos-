"""De bout en bout : l'application cree seule son premier Administrateur au
demarrage, on se connecte avec le mot de passe du fichier, et le fichier part
quand ce mot de passe est change. Ignore sans ``TEST_DATABASE_URL``."""

from __future__ import annotations

import asyncio

import asyncpg
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.first_admin import read_file
from tests.test_bout_en_bout_api_externe import DSN, pytestmark  # noqa: F401


def test_le_premier_administrateur_nait_au_demarrage(tmp_path) -> None:
    async def vider() -> None:
        conn = await asyncpg.connect(DSN)
        try:
            if await conn.fetchval("SELECT to_regclass('app_users')"):
                await conn.execute("TRUNCATE app_users RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    asyncio.run(vider())
    fichier = tmp_path / "first-admin.txt"
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
        first_admin_auto=True,
        freeqos_admin_email="noc@example.com",
        first_admin_file=fichier,
    )
    with TestClient(create_app(reglages)) as client:
        # Aucun ecran « creer le premier compte » ouvert au premier venu.
        assert client.get("/api/v1/auth/status").json()["setup_required"] is False
        r = client.post(
            "/api/v1/auth/setup", json={"email": "x@example.com", "password": "Pirate-Baobab-2026"}
        )
        assert r.status_code == 409
        champs = read_file(fichier)
        assert champs["email"] == "noc@example.com"
        r = client.post("/api/v1/auth/login", json=champs)
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "edit"
        r = client.post(
            "/api/v1/auth/password",
            json={"current": champs["password"], "new": "Nouveau-Baobab-Sahel-2026"},
        )
        assert r.status_code == 200, r.text
        assert not fichier.exists()

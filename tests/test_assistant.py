"""Assistant de support IA : coupe sans cle, ouvert a tout compte, nourri des mesures.

Aucun appel reel : le client Anthropic est remplace par un faux qui garde la
requete recue et rend une reponse fixe.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.services import assistant as svc
from tests.test_comptes import MDP, app_client, lecteur, setup, users  # noqa: F401


class FauxMessages:
    def __init__(self, reponse: Any) -> None:
        self.reponse = reponse
        self.appels: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.appels.append(kw)
        return self.reponse


def faux_client(texte: str = "Most likely cause: evening saturation.", stop: str = "end_turn"):
    reponse = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=texte),
        ],
        stop_reason=stop,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=80),
    )
    messages = FauxMessages(reponse)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


# ============================================================ service


def test_sans_cle_l_assistant_est_coupe(settings: Settings) -> None:
    assert not svc.enabled(settings.model_copy(update={"anthropic_api_key": None}))
    assert not svc.enabled(settings.model_copy(update={"anthropic_api_key": SecretStr("  ")}))
    assert svc.enabled(settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-x")}))


def test_la_question_et_les_mesures_partent_ensemble() -> None:
    texte = svc.build_prompt(" Pourquoi ca coupe ? ", {"b": 1, "a": {"rtt_ms": 42}})
    assert texte.startswith("<question>\nPourquoi ca coupe ?\n</question>")
    # Cles triees : un meme etat donne toujours le meme texte.
    assert '{"a": {"rtt_ms": 42}, "b": 1}' in texte


async def test_la_reponse_ne_garde_que_le_texte() -> None:
    client, messages = faux_client()
    r = await svc.ask(client, model="claude-opus-5-5", question="q?", context={})
    assert r["answer"] == "Most likely cause: evening saturation."
    assert r["output_tokens"] == 80
    (appel,) = messages.appels
    assert appel["model"] == "claude-opus-5-5"
    assert appel["system"] == svc.SYSTEM_PROMPT
    # Reprise automatique par un autre modele si le premier decline.
    assert appel["fallbacks"] == "default" and appel["betas"] == [svc.BETA_REPRISE]
    assert "thinking" not in appel  # adaptatif par defaut sur ce modele


async def test_un_autre_modele_n_envoie_pas_la_reprise() -> None:
    client, messages = faux_client()
    await svc.ask(client, model="claude-haiku-4-5", question="q?", context={})
    assert "fallbacks" not in messages.appels[0]


async def test_une_reponse_tronquee_le_dit() -> None:
    client, _ = faux_client("Start of the answer", stop="max_tokens")
    r = await svc.ask(client, model="claude-opus-5-5", question="q?", context={})
    assert r["answer"].endswith("[answer cut short]")


# ============================================================ API


def test_sans_session_l_assistant_est_ferme(app_client: TestClient) -> None:  # noqa: F811
    assert app_client.get("/api/v1/assistant/status").status_code == 401
    assert app_client.post("/api/v1/assistant", json={"question": "abc"}).status_code == 401


def test_sans_cle_l_api_dit_comment_l_activer(app_client: TestClient) -> None:  # noqa: F811
    setup(app_client)
    assert app_client.get("/api/v1/assistant/status").json()["enabled"] is False
    r = app_client.post("/api/v1/assistant", json={"question": "Pourquoi ?"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_un_compte_en_lecture_seule_peut_poser_une_question(
    app_client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, messages = faux_client()
    monkeypatch.setattr(svc, "make_client", lambda _settings: client)
    conteneur = app_client.app.dependency_overrides  # type: ignore[attr-defined]
    from app.api.deps import get_container

    c = conteneur[get_container]()
    c.settings = c.settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})

    lecteur(app_client)
    r = app_client.post("/api/v1/assistant", json={"question": "Which site saturates first?"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["answer"].startswith("Most likely cause")
    # Le modele a recu les mesures du reseau avec la question.
    envoye = messages.appels[0]["messages"][0]["content"]
    assert "Which site saturates first?" in envoye
    assert "rtt_probe_enabled" in envoye
    assert "rtt_probe_enabled" in corps["context"]


def test_un_abonne_inconnu_repond_404(
    app_client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = faux_client()
    monkeypatch.setattr(svc, "make_client", lambda _settings: client)
    from app.api.deps import get_container

    c = app_client.app.dependency_overrides[get_container]()  # type: ignore[attr-defined]
    c.settings = c.settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})
    setup(app_client)
    r = app_client.post(
        "/api/v1/assistant", json={"question": "Why?", "subscriber_id": 999_999, "live": False}
    )
    assert r.status_code == 404

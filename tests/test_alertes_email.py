"""Alertes par email : ce qui est detecte, ce qui est envoye, et comment un
echec SMTP est explique."""

from __future__ import annotations

import smtplib
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import notifier
from app.services.health_watch import evaluate

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def client(site: str, il_y_a: timedelta) -> dict[str, Any]:
    return {"pop_name": site, "ts": NOW - il_y_a}


def test_rien_a_signaler() -> None:
    assert (
        evaluate(
            now=NOW,
            router_reads={"nas": {"ok_at": NOW, "failing_since": None}},
            router_sites={"nas": "Plateau"},
            clients=[client("Plateau", timedelta(seconds=10))] * 3,
            jobs=[{"job": "collect_subscribers", "last_ok": True, "interval_s": 10}],
        )
        == []
    )


def test_un_routeur_muet_et_pas_son_site() -> None:
    """Le routeur ne repond plus : c'est LUI le probleme. Ses clients sans
    mesure ne font pas, en plus, un « site tombe »."""
    problemes = evaluate(
        now=NOW,
        router_reads={"nas": {"failing_since": NOW - timedelta(minutes=5), "error": "timeout"}},
        router_sites={"nas": "Plateau"},
        clients=[client("Plateau", timedelta(minutes=5))] * 4,
        jobs=[],
    )
    assert [p["key"] for p in problemes] == ["router:nas"]
    assert "timeout" in problemes[0]["detail"]


def test_un_site_dont_les_clients_tombent_ensemble() -> None:
    clients = [client("Bassora", timedelta(minutes=10))] * 3 + [
        client("Bassora", timedelta(seconds=5))
    ]
    [p] = evaluate(now=NOW, router_reads={}, router_sites={}, clients=clients, jobs=[])
    assert p["key"] == "site:Bassora" and "3 of 4" in p["detail"]
    # Un seul client parti : ce n'est pas le site.
    assert not evaluate(
        now=NOW,
        router_reads={},
        router_sites={},
        clients=[client("X", timedelta(minutes=10)), client("X", timedelta(seconds=5))],
        jobs=[],
    )


def test_une_mesure_qui_echoue_durablement() -> None:
    jobs = [
        {
            "job": "collect_links",
            "last_ok": False,
            "interval_s": 30,
            "seconds_since_last_success": 400,
            "last_error": "TimeoutError",
        },
        {
            "job": "collect_subscribers",
            "last_ok": False,
            "interval_s": 10,
            "seconds_since_last_success": 20,
        },
    ]
    [p] = evaluate(now=NOW, router_reads={}, router_sites={}, clients=[], jobs=jobs)
    assert p["key"] == "job:collect_links" and p["category"] == "measurement"


def test_l_email_dit_ce_qui_ne_va_pas_et_ce_qui_est_resolu() -> None:
    sujet, texte, corps = notifier.render_alerts(
        nouveaux=[{"title": "Router nas not answering freeQoS", "detail": "timeout"}],
        resolus=[{"title": "Site Bassora down"}],
        url="https://qos.example.com/",
    )
    assert sujet == "[freeQoS] 1 problem: Router nas not answering freeQoS"
    assert "Resolved:" in texte and "https://qos.example.com/" in texte
    assert "<b>Router nas not answering freeQoS</b>" in corps
    sujet, _, _ = notifier.render_alerts(nouveaux=[], resolus=[{"title": "A"}], url=None)
    assert sujet == "[freeQoS] Resolved: A"


@pytest.mark.parametrize(
    ("exc", "attendu"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "refused the login"),
        (TimeoutError(), "no answer"),
        (ConnectionRefusedError(), "connection refused"),
        (socket.gaierror(), "does not resolve"),
    ],
)
def test_un_echec_smtp_se_dit_en_clair(exc: BaseException, attendu: str) -> None:
    assert attendu in notifier.explain(exc)


class FauxSmtp:
    envoyes: list[Any] = []
    connexions: list[tuple[str, int]] = []
    refuser_login = False

    def __init__(self, host: str, port: int, timeout: float = 0, **_k: Any) -> None:
        FauxSmtp.connexions.append((host, port))
        self.tls = False

    def ehlo(self) -> None: ...

    def starttls(self, context: Any = None) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        if FauxSmtp.refuser_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
        assert self.tls, "le mot de passe ne part jamais en clair avec STARTTLS"

    def send_message(self, message: Any) -> None:
        FauxSmtp.envoyes.append(message)

    def quit(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture
def faux_smtp(monkeypatch):  # type: ignore[no-untyped-def]
    FauxSmtp.envoyes = []
    FauxSmtp.connexions = []
    FauxSmtp.refuser_login = False
    monkeypatch.setattr(smtplib, "SMTP", FauxSmtp)
    return FauxSmtp


async def test_un_envoi_starttls_avec_login(faux_smtp) -> None:  # type: ignore[no-untyped-def]
    message = notifier.build_message(
        from_addr="qos@example.com",
        recipients=["noc@example.com"],
        subject="s",
        text="t",
        html_body="<b>t</b>",
    )
    config = {"host": "smtp.example.com", "port": 587, "security": "starttls", "username": "qos"}
    await notifier.send(config, "secret", message)
    assert faux_smtp.connexions == [("smtp.example.com", 587)]
    assert faux_smtp.envoyes[0]["To"] == "noc@example.com"
    faux_smtp.refuser_login = True
    with pytest.raises(notifier.MailError, match="refused the login"):
        await notifier.send(config, "faux", message)


def test_des_clients_au_dessus_de_leur_limite() -> None:
    clients = [
        {"login": "a", "ts": NOW, "effective_down_mbps": 1.0, "tx_bps": 3_000_000},
        {"login": "b", "ts": NOW, "effective_down_mbps": 10.0, "tx_bps": 9_000_000},
    ]
    [p] = evaluate(now=NOW, router_reads={}, router_sites={}, clients=clients, jobs=[])
    assert p["key"] == "limit:over" and p["category"] == "limit" and p["detail"].startswith("a:")

"""Envoi d'emails par le compte SMTP saisi dans Settings.

smtplib (bibliotheque standard) dans un thread : aucune dependance de plus, et
un serveur SMTP lent ne fige jamais la boucle de l'application. Chaque echec
est traduit en une phrase qu'un exploitant peut corriger.
"""

from __future__ import annotations

import asyncio
import html
import smtplib
import socket
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any


class MailError(RuntimeError):
    """Un envoi refuse, dit en clair."""


def explain(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the SMTP server refused the login: check the user name and password"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the SMTP server refused the recipient address(es)"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "the SMTP server refused the sender address: use an address of this account"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "the server does not offer this security: try SSL (port 465) or STARTTLS (587)"
    if isinstance(exc, ssl.SSLError):
        return f"secure connection failed ({exc.reason or exc}): check the security mode and port"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "no answer from the SMTP server in time: check the host, the port and the firewall"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused: wrong port, or the server does not accept connections"
    if isinstance(exc, socket.gaierror):
        return "the SMTP host name does not resolve: check its spelling"
    if isinstance(exc, smtplib.SMTPException):
        return f"the SMTP server answered: {exc}"
    return f"{type(exc).__name__}: {exc}"


def build_message(
    *, from_addr: str, recipients: Sequence[str], subject: str, text: str, html_body: str
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="freeqos")
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")
    return message


def _send_sync(config: dict[str, Any], password: str | None, message: EmailMessage) -> None:
    host, port = str(config["host"]), int(config["port"])
    securite = str(config.get("security") or "starttls")
    contexte = ssl.create_default_context()
    if securite == "ssl":
        serveur: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=contexte)
    else:
        serveur = smtplib.SMTP(host, port, timeout=20)
    try:
        serveur.ehlo()
        if securite == "starttls":
            serveur.starttls(context=contexte)
            serveur.ehlo()
        if config.get("username"):
            serveur.login(str(config["username"]), password or "")
        serveur.send_message(message)
    finally:
        try:
            serveur.quit()
        except Exception:  # noqa: BLE001 - la connexion est peut-etre deja fermee
            serveur.close()


async def send(config: dict[str, Any], password: str | None, message: EmailMessage) -> None:
    try:
        await asyncio.to_thread(_send_sync, config, password, message)
    except Exception as exc:  # noqa: BLE001
        raise MailError(explain(exc)) from exc


def render_alerts(
    *,
    nouveaux: Sequence[dict[str, Any]],
    resolus: Sequence[dict[str, Any]],
    url: str | None,
) -> tuple[str, str, str]:
    """(sujet, texte, html) d'un email de problemes et de resolutions."""
    if nouveaux:
        titres = ", ".join(p["title"] for p in nouveaux[:2])
        reste = f" (+{len(nouveaux) - 2})" if len(nouveaux) > 2 else ""
        sujet = (
            f"[freeQoS] {len(nouveaux)} problem{'s' if len(nouveaux) > 1 else ''}: {titres}{reste}"
        )
    else:
        sujet = f"[freeQoS] Resolved: {', '.join(p['title'] for p in resolus[:2])}"
        if len(resolus) > 2:
            sujet += f" (+{len(resolus) - 2})"

    def ligne(p: dict[str, Any]) -> str:
        return f"- {p['title']}" + (f"\n  {p['detail']}" if p.get("detail") else "")

    morceaux: list[str] = []
    if nouveaux:
        morceaux.append("Needs attention:\n" + "\n".join(ligne(p) for p in nouveaux))
    if resolus:
        morceaux.append("Resolved:\n" + "\n".join(ligne(p) for p in resolus))
    if url:
        morceaux.append(f"Open freeQoS: {url}")
    texte = "\n\n".join(morceaux) + "\n"

    def bloc(titre: str, couleur: str, liste: Sequence[dict[str, Any]]) -> str:
        items = "".join(
            f'<li style="margin:0 0 8px"><b>{html.escape(p["title"])}</b>'
            + (
                f'<br><span style="color:#555">{html.escape(str(p["detail"]))}</span>'
                if p.get("detail")
                else ""
            )
            + "</li>"
            for p in liste
        )
        return (
            f'<h3 style="color:{couleur};margin:16px 0 8px">{titre}</h3>'
            f'<ul style="padding-left:18px;margin:0">{items}</ul>'
        )

    corps = '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px">'
    if nouveaux:
        corps += bloc("Needs attention", "#d70015", nouveaux)
    if resolus:
        corps += bloc("Resolved", "#248a3d", resolus)
    if url:
        corps += (
            f'<p style="margin-top:18px"><a href="{html.escape(url)}" '
            'style="color:#0071e3">Open freeQoS</a></p>'
        )
    corps += '<p style="color:#999;font-size:12px">Sent by freeQoS.</p></div>'
    return sujet, texte, corps

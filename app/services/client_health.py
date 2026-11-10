"""La sante d'UN client, en cinq reponses simples.

Ce que l'Operator doit savoir quand un client appelle (« c'est lent ») :

1. son EXPERIENCE (note 0-100 et lenteur sous charge) ;
2. le SIGNAL radio de son antenne ;
3. si son SITE est sature (le lien du site, pas le client) ;
4. si sa LIMITE est vraiment tenue sur le routeur ;
5. ce qu'il CONSOMME (volume, principaux usages).

Chaque reponse porte un niveau (``ok`` / ``warn`` / ``crit`` / ``unknown``),
une phrase courte et un detail. Le module est pur : l'API lui passe ce qu'elle
a lu, il ne fait que trancher -- et se teste sans base ni routeur.
"""

from __future__ import annotations

import re
from typing import Any

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"

#: Signal radio d'un CPE (dBm) : au-dessus, bon ; en dessous du second, faible.
SIGNAL_BON = -65.0
SIGNAL_FAIBLE = -75.0
#: Charge du lien d'un site : occupe, puis sature.
SITE_OCCUPE = 0.70
SITE_SATURE = 0.90


def reponse(niveau: str, titre: str, detail: str = "", **donnees: Any) -> dict[str, Any]:
    return {"level": niveau, "title": titre, "detail": detail, **donnees}


def experience(ligne: dict[str, Any] | None) -> dict[str, Any]:
    """D'apres la ligne de ``/latency/clients`` de ce client."""
    if not ligne or ligne.get("qoe_score") is None and ligne.get("median_ms") is None:
        return reponse(
            UNKNOWN, "Not measured", "The latency probe has no measurement for this client."
        )
    score = ligne.get("qoe_score")
    etat = ligne.get("experience")
    niveau = {"good": OK, "fair": WARN, "poor": CRIT, "limit": WARN}.get(str(etat), UNKNOWN)
    titre = {"good": "Good", "fair": "Fair", "poor": "Poor", "limit": "At its limit"}.get(
        str(etat), "Measured"
    )
    morceaux = []
    if ligne.get("median_ms") is not None:
        morceaux.append(f"latency {ligne['median_ms']:g} ms")
    if ligne.get("grade"):
        morceaux.append(f"under load: grade {ligne['grade']}")
    if ligne.get("loss_pct"):
        morceaux.append(f"{ligne['loss_pct']:g} % loss")
    return reponse(
        niveau,
        titre + (f" · {score:g}/100" if score is not None else ""),
        ", ".join(morceaux),
        score=score,
        grade=ligne.get("grade"),
        reasons=list(ligne.get("reasons") or []),
    )


def normaliser_mac(mac: Any) -> str | None:
    brut = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(brut) != 12:
        return None
    return ":".join(brut[i : i + 2] for i in range(0, 12, 2)).upper()


def radio(cpe_mac: Any, antennes: list[dict[str, Any]], ip: str | None = None) -> dict[str, Any]:
    """Le CPE du client parmi les stations des antennes lues (par MAC, sinon IP)."""
    cle = normaliser_mac(cpe_mac)
    trouve = None
    for antenne in antennes:
        for sta in antenne.get("stations") or []:
            if cle and normaliser_mac(sta.get("mac")) == cle:
                trouve = (antenne, sta)
            elif not cle and ip and sta.get("ip") and str(sta["ip"]).split("/")[0] == ip:
                trouve = (antenne, sta)
            if trouve:
                break
        if trouve:
            break
    if trouve is None:
        motif = (
            "Its antenna is not among the antennas read by freeQoS."
            if cle
            else "No CPE MAC known for this client (PPPoE caller-id or declared CPE MAC)."
        )
        return reponse(UNKNOWN, "No radio data", motif)
    antenne, sta = trouve
    signal = sta.get("signal_dbm")
    if signal is None:
        return reponse(UNKNOWN, "Signal not read", "", ap=antenne.get("name"))
    signal = float(signal)
    niveau = OK if signal >= SIGNAL_BON else WARN if signal >= SIGNAL_FAIBLE else CRIT
    titre = {OK: "Good", WARN: "Fair", CRIT: "Weak"}[niveau] + f" · {signal:g} dBm"
    morceaux = [f"on {antenne.get('name') or antenne.get('key')}"]
    if sta.get("snr_db") is not None:
        morceaux.append(f"SNR {float(sta['snr_db']):g} dB")
    if sta.get("ccq_pct") is not None:
        morceaux.append(f"link quality {float(sta['ccq_pct']):g} %")
    if niveau == CRIT:
        morceaux.append("check the antenna alignment or obstacles")
    return reponse(niveau, titre, ", ".join(morceaux), signal_dbm=signal, ap=antenne.get("name"))


def site(nom: str | None, pop: dict[str, Any] | None) -> dict[str, Any]:
    """Le lien du site du client : sa charge face a la capacite radio mesuree."""
    if not nom or pop is None:
        return reponse(UNKNOWN, "No site", "This client is attached to no known site.")
    charge = float(pop.get("tx_bps") or 0) / 1e6
    capacites = [
        float(b["capacity_mbps"])
        for b in pop.get("backhauls") or []
        if b.get("capacity_mbps") not in (None, 0)
    ]
    if not capacites:
        return reponse(
            UNKNOWN,
            f"{nom} · capacity unknown",
            f"{charge:.1f} Mbps going to its clients now; add its backhaul antenna to compare.",
            site=nom,
            load_mbps=round(charge, 2),
        )
    capacite = min(capacites)
    part = charge / capacite if capacite else 0
    niveau = CRIT if part >= SITE_SATURE else WARN if part >= SITE_OCCUPE else OK
    titre = {OK: "OK", WARN: "Busy", CRIT: "Saturated"}[niveau]
    return reponse(
        niveau,
        f"{nom} · {titre} · {round(part * 100)} %",
        f"{charge:.1f} of {capacite:g} Mbps"
        + (" : every client of the site is slowed, not only this one" if niveau == CRIT else ""),
        site=nom,
        load_mbps=round(charge, 2),
        capacity_mbps=capacite,
        load_pct=round(part * 100, 1),
    )


def limite(file: dict[str, Any] | None, audit_fait: bool) -> dict[str, Any]:
    """La file du client lue sur le routeur : le plafond tient-il vraiment ?"""
    if file is None:
        if not audit_fait:
            return reponse(UNKNOWN, "Not checked yet", "The routers are checked every few minutes.")
        return reponse(UNKNOWN, "No queue", "No freeQoS queue for this client on its router.")
    verdict = str(file.get("verdict") or "")
    detail = str(file.get("detail") or "")
    if verdict == "en-vigueur":
        return reponse(
            OK, "Applied", f"held on the router ({file.get('seen') or file.get('wanted')})"
        )
    if verdict == "sans-plafond":
        return reponse(OK, "No limit", "the client has no cap to hold")
    titres = {
        "contourne": "NOT held · FastTrack",
        "file-absente": "Not written yet",
        "file-masquee": "NOT held · hidden by another queue",
        "file-desactivee": "NOT held · queue disabled",
        "debit-different": "Different on the router",
    }
    niveau = WARN if verdict in ("file-absente", "debit-different") else CRIT
    return reponse(niveau, titres.get(verdict, verdict), detail, verdict=verdict)


def usage(
    jour_bas: float, jour_haut: float, mois_bas: float, mois_haut: float, apps: list[dict[str, Any]]
) -> dict[str, Any]:
    """Volumes et principaux usages (NetFlow) : de quoi repondre « il telecharge »."""
    total = sum(int(a.get("down_bytes") or 0) + int(a.get("up_bytes") or 0) for a in apps)
    principaux = [
        {
            "app": a.get("app") or "other",
            "bytes": int(a.get("down_bytes") or 0) + int(a.get("up_bytes") or 0),
            "share": round(
                (int(a.get("down_bytes") or 0) + int(a.get("up_bytes") or 0)) / total * 100, 1
            )
            if total
            else 0,
        }
        for a in apps[:5]
    ]
    return {
        "day_down_bytes": round(jour_bas),
        "day_up_bytes": round(jour_haut),
        "month_down_bytes": round(mois_bas),
        "month_up_bytes": round(mois_haut),
        "top": principaux,
    }

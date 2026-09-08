"""Normalisation des valeurs renvoyees par RouterOS et RADIUS.

RouterOS expose ses valeurs en texte, avec plusieurs formats pour une meme notion
(l'uptime notamment). Ces fonctions sont pures et couvertes par les tests : c'est
la premiere source de bugs silencieux sur ce genre d'integration.
"""

from __future__ import annotations

import re

# "1w2d03:04:05", "2d03:04:05", "03:04:05"
_COLON_UPTIME = re.compile(
    r"^(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2})$"
)
# "1w2d3h4m5s", "6m30s", "45s"
_SUFFIX_UPTIME = re.compile(
    r"^(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?"
    r"(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)

_UNIT_MULTIPLIERS = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def parse_routeros_uptime(value: object) -> int | None:
    """Convertit un uptime RouterOS en secondes.

    RouterOS utilise deux notations selon les commandes et les versions :
    ``1w2d03:04:05`` et ``1w2d3h4m5s``. Les deux sont acceptees.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        return None
    for pattern in (_COLON_UPTIME, _SUFFIX_UPTIME):
        match = pattern.match(text)
        if match and any(match.groupdict().values()):
            parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
            return (
                parts["w"] * 604_800
                + parts["d"] * 86_400
                + parts["h"] * 3_600
                + parts["m"] * 60
                + parts["s"]
            )
    return None


def parse_counter(value: object) -> int | None:
    """Convertit un compteur d'octets RouterOS en entier.

    Les compteurs peuvent arriver en int (API binaire) ou en texte, parfois avec
    des espaces de milliers selon la version.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_bitrate(value: object) -> float | None:
    """Convertit un debit RADIUS/RouterOS en bits par seconde.

    Accepte ``10M``, ``1024k``, ``2G``, ``10000000`` et les suffixes ``bps``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().lower().replace("bps", "").replace("b/s", "")
    if not text:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", text)
    if not match:
        return None
    return float(match.group(1)) * _UNIT_MULTIPLIERS[match.group(2)]


def parse_mikrotik_rate_limit(value: str | None) -> tuple[float, float] | None:
    """Decode l'attribut RADIUS ``Mikrotik-Rate-Limit``.

    Format : ``rx-rate[/tx-rate] [rx-burst/tx-burst] ...``, ou rx/tx sont vus
    depuis le routeur. Donc ``"10M/50M"`` = 10 Mbps d'upload abonne et 50 Mbps
    de download abonne.

    Retourne ``(down_mbps, up_mbps)``, cote abonne, ou None si indecodable.
    """
    if not value:
        return None
    first = str(value).strip().split()[0] if str(value).strip() else ""
    if not first:
        return None
    parts = first.split("/")
    up_bps = parse_bitrate(parts[0])
    down_bps = parse_bitrate(parts[1]) if len(parts) > 1 else up_bps
    if up_bps is None or down_bps is None:
        return None
    return down_bps / 1_000_000.0, up_bps / 1_000_000.0


_DURATION = re.compile(
    r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m(?!s))?(?:(?P<s>\d+)s)?"
    r"(?:(?P<ms>\d+)ms)?(?:(?P<us>\d+)us)?"
)


def parse_routeros_duration_ms(value: object) -> float | None:
    """Convertit une duree RouterOS en millisecondes.

    ``/ping`` renvoie des valeurs composees comme ``1ms500us`` ou ``2s100ms``.
    Attention au piege : ``m`` est une minute et ``ms`` une milliseconde, d'ou
    le lookahead negatif dans l'expression.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    match = _DURATION.fullmatch(text)
    if not match or not any(match.groupdict().values()):
        return None
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return (
        parts["h"] * 3_600_000
        + parts["m"] * 60_000
        + parts["s"] * 1_000
        + parts["ms"]
        + parts["us"] / 1000.0
    )


def pppoe_interface_name(pattern: str, login: str) -> str:
    """Construit le nom de l'interface dynamique associee a une session PPPoE."""
    return pattern.format(login=login, name=login, user=login)

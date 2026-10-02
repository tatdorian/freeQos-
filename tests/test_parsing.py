"""Parsing des valeurs RouterOS et RADIUS.

Ces fonctions sont la premiere source de bugs silencieux : un uptime mal lu et
la detection de reconnexion ne fonctionne plus.
"""

from __future__ import annotations

import pytest

from app.collectors.parsing import (
    parse_bitrate,
    parse_counter,
    parse_mikrotik_rate_limit,
    parse_routeros_uptime,
    pppoe_interface_name,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:00:05", 5),
        ("01:00:00", 3600),
        ("2d03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ("1w2d03:04:05", 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ("1w2d3h4m5s", 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ("6m30s", 390),
        ("45s", 45),
        (3600, 3600),
        (None, None),
        ("", None),
        ("pas-un-uptime", None),
    ],
)
def test_parse_routeros_uptime(value: object, expected: int | None) -> None:
    assert parse_routeros_uptime(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1234", 1234), (5678, 5678), ("1 234 567", 1234567), ("", None), (None, None), ("abc", None)],
)
def test_parse_counter(value: object, expected: int | None) -> None:
    assert parse_counter(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10M", 10_000_000.0),
        ("1024k", 1_024_000.0),
        ("2G", 2_000_000_000.0),
        ("10000000", 10_000_000.0),
        ("50Mbps", 50_000_000.0),
        (None, None),
        ("plein pot", None),
    ],
)
def test_parse_bitrate(value: object, expected: float | None) -> None:
    assert parse_bitrate(value) == expected


def test_parse_mikrotik_rate_limit_ordre_up_puis_down() -> None:
    """Mikrotik-Rate-Limit est "rx/tx" cote routeur : rx = upload abonne."""
    assert parse_mikrotik_rate_limit("10M/50M") == (50.0, 10.0)


def test_parse_mikrotik_rate_limit_symetrique_si_une_seule_valeur() -> None:
    assert parse_mikrotik_rate_limit("20M") == (20.0, 20.0)


def test_parse_mikrotik_rate_limit_ignore_les_bursts() -> None:
    assert parse_mikrotik_rate_limit("10M/50M 20M/100M 15M/75M 10 8 20M/100M") == (50.0, 10.0)


def test_parse_mikrotik_rate_limit_invalide() -> None:
    assert parse_mikrotik_rate_limit("") is None
    assert parse_mikrotik_rate_limit(None) is None


def test_pppoe_interface_name() -> None:
    assert pppoe_interface_name("<pppoe-{login}>", "dupont") == "<pppoe-dupont>"
    assert pppoe_interface_name("pppoe-{name}-in", "dupont") == "pppoe-dupont-in"

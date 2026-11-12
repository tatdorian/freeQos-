"""Regles : des adresses, des blocs, des plages, des noms de domaine.

DEMANDE : "a la place du trafic predefini, mettre une ou plusieurs adresses IP
ou une plage avec le /, ou un nom de domaine qui bloque toutes les IP
associees". Le DNS est simule : les tests ne sortent jamais sur le reseau.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import rule_targets
from app.services.restrictions import InvalidRuleError, RestrictionService, validate
from app.services.rule_targets import (
    DomainResolver,
    InvalidTargetError,
    couvre,
    expand_addresses,
    normalise_domain,
    parse_address,
)

# --------------------------------------------------------------- adresses


def test_une_adresse_un_bloc_une_plage() -> None:
    assert parse_address("203.0.113.7") == ["203.0.113.7/32"]
    assert parse_address("198.51.100.9/24") == ["198.51.100.0/24"]
    # Une plage est decoupee en blocs EXACTS, sans deborder.
    assert parse_address("192.0.2.10 - 192.0.2.17") == [
        "192.0.2.10/31",
        "192.0.2.12/30",
        "192.0.2.16/31",
    ]


@pytest.mark.parametrize(
    "texte",
    ["pas-une-ip", "10.0.0.50-10.0.0.10", "2001:db8::/32", "0.0.0.0/0", "10.0.0.1-2001:db8::1"],
)
def test_les_entrees_fausses_sont_refusees(texte: str) -> None:
    with pytest.raises(InvalidTargetError):
        parse_address(texte)


def test_une_regle_avec_une_adresse_fausse_est_refusee() -> None:
    with pytest.raises(InvalidRuleError, match="not an IP address"):
        validate({"prefixes": ["203.0.113.7", "n'importe quoi"]})


def test_expand_ignore_les_anciennes_entrees_fausses() -> None:
    assert expand_addresses(["203.0.113.7", "x", "10.0.0.0-10.0.0.3"]) == [
        "203.0.113.7/32",
        "10.0.0.0/30",
    ]


# --------------------------------------------------------------- domaines


@pytest.mark.parametrize(
    ("saisi", "attendu"),
    [
        ("Example.COM", "example.com"),
        ("https://www.example.com/page?x=1", "www.example.com"),
        ("*.example.org", "example.org"),
        ("example.com:443", "example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_un_nom_est_ramene_a_sa_forme_simple(saisi: str, attendu: str) -> None:
    assert normalise_domain(saisi) == attendu


@pytest.mark.parametrize("saisi", ["localhost", "203.0.113.7", "exa mple.com", "-a.com", ""])
def test_un_nom_faux_est_refuse(saisi: str) -> None:
    with pytest.raises(InvalidTargetError):
        normalise_domain(saisi)


def test_un_domaine_couvre_ses_sous_domaines_seulement() -> None:
    assert couvre("example.com", "example.com")
    assert couvre("example.com", "video.cdn.example.com.")
    assert not couvre("example.com", "notexample.com")


def test_une_regle_avec_seulement_un_domaine_est_valide() -> None:
    validate({"domains": ["example.com"]})
    with pytest.raises(InvalidRuleError):
        validate({"domains": ["pas un nom"]})


class FauxDns:
    def __init__(self, reponses: dict[str, set[str]]) -> None:
        self.reponses = reponses
        self.demandes: list[str] = []

    async def __call__(self, nom: str) -> set[str]:
        self.demandes.append(nom)
        return set(self.reponses.get(nom, set()))


async def test_un_domaine_donne_ses_adresses_et_celles_de_ses_sous_domaines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = FauxDns({"example.com": {"203.0.113.10"}, "www.example.com": {"203.0.113.11"}})
    monkeypatch.setattr(rule_targets, "_resoudre", dns)
    # Ce que les clients ont obtenu par le routeur pour video.example.com.
    cache = type("Cache", (), {})()
    cache.names = {
        "198.51.100.5": ("video.example.com", 0.0),
        "198.51.100.6": ("notexample.com", 0.0),
        "2001:db8::1": ("example.com", 0.0),
    }
    resolveur = DomainResolver(dns_names=cache)
    adresses = (await resolveur.resolve(["example.com"]))["example.com"]
    assert adresses == ["198.51.100.5", "203.0.113.10", "203.0.113.11"]
    assert set(dns.demandes) == {"example.com", "www.example.com"}


async def test_une_adresse_sortie_du_dns_reste_bloquee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un CDN tourne ses adresses : celle vue au passage precedent reste dans la
    liste, sinon elle serait retiree et reposee a chaque passage."""
    dns = FauxDns({"example.com": {"203.0.113.10"}})
    monkeypatch.setattr(rule_targets, "_resoudre", dns)
    resolveur = DomainResolver()
    await resolveur.resolve(["example.com"])
    dns.reponses["example.com"] = {"203.0.113.99"}
    assert (await resolveur.resolve(["example.com"]))["example.com"] == [
        "203.0.113.10",
        "203.0.113.99",
    ]


async def test_un_nom_qui_ne_resout_pas_est_signale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_targets, "_resoudre", FauxDns({}))
    resolveur = DomainResolver()
    assert (await resolveur.resolve(["inconnu.example"]))["inconnu.example"] == []
    assert resolveur.known("inconnu.example")["error"]


async def test_la_regle_resolue_porte_les_adresses_du_nom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_targets, "_resoudre", FauxDns({"example.com": {"203.0.113.10"}}))
    service = RestrictionService.__new__(RestrictionService)
    service.domain_resolver = DomainResolver()
    service.destinations = None
    service.flows_repo = None
    service.address_limit = 5000
    regle: dict[str, Any] = {
        "id": 1,
        "name": "x",
        "prefixes": ["192.0.2.0-192.0.2.3"],
        "domains": ["example.com"],
    }
    cible = await service.resolve(regle)
    assert cible.destinations == ("192.0.2.0/30", "203.0.113.10")

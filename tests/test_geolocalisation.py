"""La localisation d'une adresse atteinte, et ce qu'elle coute.

CE QUE CES TESTS PROTEGENT
--------------------------
1. LA VIE PRIVEE DES CLIENTS. Interroger un service de geolocalisation revient
   a LUI ENVOYER les adresses que vos clients atteignent. C'est une information
   sur eux : l'appel ne doit jamais partir sans autorisation explicite.
2. LA BASE LOCALE D'ABORD. Quand un fichier MaxMind est pose, la meme reponse
   s'obtient sans qu'aucun paquet ne sorte. C'est la seule forme recommandable.
3. UN BONUS NE FAIT PAS TOMBER LE RESTE. Un service muet, limite ou injoignable
   ne doit pas faire perdre le verdict que le catalogue et le nom inverse ont
   deja rendu.
"""

from __future__ import annotations

from typing import Any

from app.services.intel import IntelService


class FauxDepot:
    def __init__(self, attente: list[str]) -> None:
        self.attente = list(attente)
        self.enregistres: list[dict[str, Any]] = []

    async def pending(self, *, limit: int = 50, max_attempts: int = 3) -> list[str]:
        return self.attente[:limit]

    async def count_pending(self, **kwargs: Any) -> int:
        return len(self.attente)

    async def save_intel(self, verdicts: list[dict[str, Any]]) -> int:
        self.enregistres.extend(verdicts)
        self.attente = [a for a in self.attente if a not in {v["address"] for v in verdicts}]
        return len(verdicts)


def service(**kwargs: Any) -> tuple[IntelService, FauxDepot]:
    depot = FauxDepot(["45.57.12.34"])
    return (
        IntelService(destinations=depot, rdns_enabled=False, **kwargs),  # type: ignore[arg-type]
        depot,
    )


async def test_aucun_appel_de_localisation_sans_autorisation() -> None:
    """LE DEFAUT EST LE SILENCE. Demander la position d'une adresse revient a
    dire a un tiers ce que votre client regarde."""
    intel, depot = service()
    appels: list[str] = []
    intel._geoip = lambda a: appels.append(a)  # type: ignore[assignment,method-assign,return-value]

    await intel.resolve_pending()

    assert appels == []
    # Le verdict du catalogue, lui, est rendu : il ne coute rien.
    assert depot.enregistres[0]["service"] == "netflix"
    assert depot.enregistres[0]["city"] is None


async def test_la_localisation_autorisee_enrichit_la_fiche() -> None:
    intel, depot = service(geoip_enabled=True)

    async def faux_geoip(address: str) -> dict[str, Any]:
        return {
            "country": "NL",
            "city": "Amsterdam",
            "region": "Noord-Holland",
            "latitude": 52.37,
            "longitude": 4.89,
            "org": "Netflix Streaming Services",
        }

    intel._geoip = faux_geoip  # type: ignore[assignment,method-assign]
    await intel.resolve_pending()

    fiche = depot.enregistres[0]
    assert fiche["city"] == "Amsterdam"
    assert fiche["region"] == "Noord-Holland"
    assert fiche["country"] == "NL"
    assert fiche["latitude"] == 52.37
    # Le verdict du catalogue reste : la localisation complete, elle ne remplace
    # pas ce qu'on savait deja.
    assert fiche["service"] == "netflix"


async def test_un_service_muet_ne_fait_pas_perdre_le_verdict() -> None:
    """Le service peut limiter le debit, etre injoignable, ou ne rien savoir de
    cette adresse. Rien de tout cela n'est une erreur d'exploitation."""
    intel, depot = service(geoip_enabled=True)

    async def geoip_casse(address: str) -> dict[str, Any]:
        raise RuntimeError("429 Too Many Requests")

    intel._geoip = geoip_casse  # type: ignore[assignment,method-assign]
    try:
        await intel.resolve_pending()
    except RuntimeError:
        raise AssertionError("un service de localisation muet ne doit pas remonter") from None

    assert depot.enregistres[0]["service"] == "netflix"


async def test_la_base_locale_est_preferee_au_service_distant() -> None:
    """MEME REPONSE, AUCUN PAQUET QUI SORT. C'est la seule forme de
    geolocalisation qu'on puisse recommander sans reserve."""
    intel, _ = service(geoip_enabled=True, geoip_db="/tmp/pas-de-base.mmdb")
    sortants: list[str] = []

    intel._geoip_local = lambda a: {"city": "Paris", "country": "FR"}  # type: ignore[assignment,method-assign]

    async def http_interdit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        sortants.append("appel")
        return {}

    intel._rdap = http_interdit  # type: ignore[assignment,method-assign]
    trouve = await intel._geoip("45.57.12.34")

    assert trouve == {"city": "Paris", "country": "FR"}
    assert sortants == []


async def test_une_base_illisible_retombe_sur_le_service_sans_bloquer() -> None:
    """Fichier absent, paquet geoip2 non installe : la fonctionnalite se degrade,
    elle ne casse pas -- et elle le dit une fois dans les journaux."""
    intel, _ = service(geoip_enabled=True, geoip_db="/tmp/vraiment-pas-la.mmdb")
    assert intel._geoip_local("45.57.12.34") is None
    # Marque comme cassee : on ne retente pas a chaque adresse.
    assert intel._geoip_broken is True


async def test_l_etat_dit_si_la_localisation_reste_locale() -> None:
    intel, _ = service(geoip_enabled=True)
    etat = await intel.status()
    assert etat["geoip_enabled"] is True
    assert etat["geoip_local"] is False

"""Les adresses de NOS routeurs, reconnues comme telles dans les flux.

Un client qui pingue le loopback d'un routeur de l'operateur ne va pas sur
internet : l'afficher "unidentified" et le geolocaliser a Columbus (Ohio) --
ce que rendent les bases publiques pour un bloc qu'elles ne connaissent pas --
etait faux deux fois. Les adresses de chaque routeur sont lues dans sa table
d'adresses a la decouverte : elles font foi, avant toute source externe.
"""

from __future__ import annotations

from typing import Any

from app.collectors.mikrotik import own_address

SERVICE_INTERNE = "your router"
CATEGORIE_INTERNE = "internal"
_EXTERNES = ("org", "asn", "country", "city", "region", "latitude", "longitude", "network")


def mark_internal(row: dict[str, Any], key: str = "address") -> bool:
    """Reetiquette la ligne si l'adresse est celle d'un de nos routeurs.

    Rend True si elle l'etait. Les champs de localisation et d'organisation
    sont vides : ils venaient de bases publiques qui ne savent rien de ce bloc.
    """
    trouve = own_address(str(row.get(key) or ""))
    if trouve is None:
        return False
    routeur, interface = trouve
    row["service"] = SERVICE_INTERNE
    row["category"] = CATEGORIE_INTERNE
    row["hostname"] = f"{routeur} · {interface}"
    row["source"] = "inventory"
    row["internal"] = True
    row["router"] = routeur
    for cle in _EXTERNES:
        if cle in row:
            row[cle] = None
    return True


def mark_all(rows: list[dict[str, Any]], key: str = "address") -> list[dict[str, Any]]:
    """Les lignes SANS celles qui visent nos routeurs.

    DEMANDE EXPLICITE : le lien vers un routeur (sonde de latence, ping du
    loopback) n'a rien a faire dans le trafic des abonnes. Ecartees ici aussi,
    pour que l'historique deja ecrit disparaisse sans attendre le menage.
    """
    return [row for row in rows if not mark_internal(row, key)]

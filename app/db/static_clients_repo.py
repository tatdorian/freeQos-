"""Inventaire des clients a IP fixe, saisi a la main.

POURQUOI UNE SAISIE MANUELLE, ET POURQUOI C'EST LEGITIME
--------------------------------------------------------
Un abonne PPPoE se decouvre tout seul : il ouvre une session, ``/ppp/active``
le nomme, RADIUS donne son plan. Un client a IP fixe n'a rien de tout cela. Il
n'ouvre aucune session, aucun attribut RADIUS ne le decrit, et rien dans
RouterOS ne dit "cette adresse appartient a tel client, qui a souscrit tel
debit". Les seules choses qu'on pourrait deviner -- scanner un VLAN, croiser
des baux -- donneraient une liste d'adresses vivantes, pas une liste de
CLIENTS avec un contrat.

Cet inventaire est donc declaratif, exactement comme l'inventaire de routeurs :
l'operateur saisit ce qu'il a vendu. Ce n'est pas un pis-aller en attendant une
integration, c'est la bonne source.

Le controleur n'ecrit jamais dans cette table : elle porte l'intention humaine,
et lui n'en fait que des files.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import asyncpg

from app.models import StaticClient

logger = logging.getLogger(__name__)

COLUMNS = """
    id, reference, label, pop_name, host(address) AS address,
    masklen(address) AS prefix_len, vlan, sector_key,
    plan_down_mbps, plan_up_mbps, enabled, note, created_at, updated_at
"""

CHAMPS_MODIFIABLES = (
    "reference",
    "label",
    "pop_name",
    "address",
    "vlan",
    "sector_key",
    "plan_down_mbps",
    "plan_up_mbps",
    "enabled",
    "note",
)


class StaticClientNotFoundError(LookupError):
    pass


class DuplicateStaticClientError(ValueError):
    pass


class InvalidStaticClientError(ValueError):
    pass


def normalise_address(value: Any) -> str:
    """Valide l'adresse declaree et la rend sous forme canonique.

    Accepte une IP seule (``10.0.0.5``) comme un sous-reseau (``10.0.0.0/29``) :
    un client professionnel se voit souvent attribuer un bloc entier, et le
    brider a une seule adresse laisserait le reste du bloc sans plafond.

    Une adresse d'hote portant un prefixe large (``10.0.0.5/29``) est ramenee au
    reseau : c'est la lecture la plus probable de la saisie, et surtout la seule
    qui soit stable d'un cycle a l'autre puisque RouterOS reecrit sa cible ainsi.
    """
    texte = str(value or "").strip()
    if not texte:
        raise InvalidStaticClientError("adresse manquante")
    try:
        interface = ipaddress.ip_interface(texte)
    except ValueError as exc:
        raise InvalidStaticClientError(f"adresse invalide : {texte}") from exc
    if interface.ip.is_unspecified or interface.ip.is_loopback:
        raise InvalidStaticClientError(f"adresse inutilisable : {texte}")
    return str(interface.network)


def _to_row(record: asyncpg.Record) -> dict[str, Any]:
    """Recompose l'adresse au format CIDR attendu par l'interface."""
    row = dict(record)
    adresse = row.pop("address", None)
    longueur = row.pop("prefix_len", None)
    if adresse is not None:
        row["address"] = f"{adresse}/{longueur}" if longueur is not None else str(adresse)
    return row


def to_model(row: dict[str, Any]) -> StaticClient:
    return StaticClient(
        reference=str(row["reference"]),
        pop_name=str(row["pop_name"]),
        address=str(row["address"]),
        label=row.get("label"),
        vlan=row.get("vlan"),
        sector_key=row.get("sector_key"),
        plan_down_mbps=row.get("plan_down_mbps"),
        plan_up_mbps=row.get("plan_up_mbps"),
        enabled=bool(row.get("enabled", True)),
        note=row.get("note"),
    )


class StaticClientsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ lectures
    async def list_all(self, *, pop_name: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {COLUMNS} FROM static_clients
                 WHERE ($1::text IS NULL OR pop_name = $1)
                 ORDER BY reference
                """,  # noqa: S608 - COLUMNS est une constante du module
                pop_name,
            )
        return [_to_row(row) for row in rows]

    async def get(self, client_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {COLUMNS} FROM static_clients WHERE id = $1",  # noqa: S608
                client_id,
            )
        if row is None:
            raise StaticClientNotFoundError(f"client statique {client_id} inconnu")
        return _to_row(row)

    async def load_enabled(self) -> list[StaticClient]:
        """Les clients a prendre en compte dans le cycle de collecte.

        Un client desactive disparait du plan au cycle suivant, donc sa file est
        retiree du routeur : c'est le moyen de suspendre un client sans perdre
        sa fiche.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {COLUMNS} FROM static_clients WHERE enabled ORDER BY reference"  # noqa: S608
            )
        clients: list[StaticClient] = []
        for row in rows:
            try:
                clients.append(to_model(_to_row(row)))
            except (KeyError, TypeError, ValueError):
                logger.exception("Fiche de client statique illisible, ignoree : %s", row.get("id"))
        return clients

    # ------------------------------------------------------------ ecritures
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        adresse = normalise_address(payload.get("address"))
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO static_clients (reference, label, pop_name, address, vlan,
                                                sector_key, plan_down_mbps, plan_up_mbps,
                                                enabled, note)
                    VALUES ($1, $2, $3, $4::inet, $5, $6, $7, $8, $9, $10)
                    RETURNING {COLUMNS}
                    """,  # noqa: S608
                    payload["reference"],
                    payload.get("label"),
                    payload["pop_name"],
                    adresse,
                    payload.get("vlan"),
                    payload.get("sector_key"),
                    payload.get("plan_down_mbps"),
                    payload.get("plan_up_mbps"),
                    payload.get("enabled", True),
                    payload.get("note"),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateStaticClientError(
                    f"un client statique nomme '{payload['reference']}' existe deja"
                ) from exc
        return _to_row(row)

    async def update(self, client_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        fields = {
            key: value
            for key, value in payload.items()
            if key in CHAMPS_MODIFIABLES and value is not None
        }
        if "address" in fields:
            fields["address"] = normalise_address(fields["address"])
        if not fields:
            return await self.get(client_id)

        assignments = ", ".join(
            f"{name} = ${i + 2}" + ("::inet" if name == "address" else "")
            for i, name in enumerate(fields)
        )
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE static_clients SET {assignments}, updated_at = now()
                     WHERE id = $1
                    RETURNING {COLUMNS}
                    """,  # noqa: S608 - noms de colonnes issus d'une liste blanche
                    client_id,
                    *fields.values(),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateStaticClientError(
                    f"un client statique nomme '{payload.get('reference')}' existe deja"
                ) from exc
        if row is None:
            raise StaticClientNotFoundError(f"client statique {client_id} inconnu")
        return _to_row(row)

    async def delete(self, client_id: int) -> None:
        """Retire la fiche. L'abonne materialise et son historique restent.

        Supprimer les mesures avec la fiche ferait disparaitre le passe d'un
        client qu'on vient peut-etre juste de debrancher par erreur. La ligne
        'subscribers' cesse simplement d'etre rafraichie, et sa file tombe au
        plan suivant faute de cible declaree.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM static_clients WHERE id = $1", client_id)
        if result.endswith(" 0"):
            raise StaticClientNotFoundError(f"client statique {client_id} inconnu")

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
import json
import logging
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from app.enforcement.models import MAX_TARGET_PREFIXES
from app.models import StaticClient, VlanSighting

logger = logging.getLogger(__name__)

COLUMNS = """
    id, reference, label, pop_name, host(address) AS address,
    masklen(address) AS prefix_len, vlan, sector_key,
    plan_down_mbps, plan_up_mbps, enabled, note, created_at, updated_at,
    source, account_ref, package_ref, access_point_ref, cpe_mac, extra_prefixes
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
    "cpe_mac",
    "extra_prefixes",
)


def _mac(value: Any) -> str | None:
    """Normalise une MAC de CPE. Elle sert d'identite de secours quand
    l'adresse change, et de jointure avec le voisinage radio."""
    texte = str(value or "").strip().upper().replace("-", ":")
    return texte or None


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


def _plage_en_blocs(texte: str) -> list[str]:
    """``10.0.0.10-10.0.0.50`` -> les blocs CIDR qui couvrent exactement la plage.

    C'est la forme d'un pool MikroTik (``/ip/pool ranges=``). Une file RouterOS
    ne vise pas de plage, seulement des blocs : on la decoupe donc au plus
    juste, sans deborder sur les voisins de debut et de fin de plage.
    """
    debut_txt, _, fin_txt = texte.partition("-")
    try:
        debut = ipaddress.ip_address(debut_txt.strip())
        fin = ipaddress.ip_address(fin_txt.strip())
    except ValueError as exc:
        raise InvalidStaticClientError(f"invalid range: {texte}") from exc
    if debut.version != fin.version:
        raise InvalidStaticClientError(f"range mixes IPv4 and IPv6: {texte}")
    if int(fin) < int(debut):
        raise InvalidStaticClientError(f"range ends before it starts: {texte}")
    return [str(bloc) for bloc in ipaddress.summarize_address_range(debut, fin)]


def normalise_extra_prefixes(value: Any) -> list[str]:
    """Valide les plages additionnelles d'un client, sous forme canonique.

    POURQUOI PLUSIEURS PLAGES. Un client sur VLAN a d'ordinaire un seul pool ;
    certains en ont plusieurs sur la meme VLAN (un bloc public ajoute, un pool
    de service). Toutes portent le trafic du MEME contrat : elles partagent sa
    file, et donc son debit.

    Chaque element est une IP (``10.0.1.5``), un bloc (``10.0.1.0/28``) ou une
    plage de pool (``10.0.1.10-10.0.1.50``, decoupee en blocs exacts). On
    accepte une liste ou un texte separe par virgules, espaces ou retours a la
    ligne -- ce qu'on colle depuis ``/ip/pool``. Doublons retires, ordre de
    saisie conserve.
    """
    if value is None:
        return []
    texte = value if isinstance(value, str) else " ".join(str(v) for v in value)
    # "10.0.1.10 - 10.0.1.50" s'ecrit aussi avec des espaces autour du tiret :
    # decoupee sur les espaces, la plage devenait trois morceaux, dont un "-"
    # refuse avec un message incomprehensible.
    texte = re.sub(r"\s*-\s*", "-", texte)
    morceaux = re.split(r"[\s,;]+", texte)
    blocs: list[str] = []
    for morceau in morceaux:
        texte = morceau.strip()
        if not texte:
            continue
        candidats = _plage_en_blocs(texte) if "-" in texte else [normalise_address(texte)]
        for bloc in candidats:
            if bloc not in blocs:
                blocs.append(bloc)
    if len(blocs) > MAX_TARGET_PREFIXES - 1:
        raise InvalidStaticClientError(
            f"too many address blocks ({len(blocs)}): one queue targets at most "
            f"{MAX_TARGET_PREFIXES} -- check the ranges entered"
        )
    return blocs


def split_address(address: Any, extra: Any) -> tuple[str, list[str]]:
    """Adresse principale et plages en plus, depuis ce qui a ete saisi.

    L'adresse principale accepte, elle aussi, une PLAGE (``10.0.0.10-10.0.0.20``)
    ou PLUSIEURS IP (``10.0.0.5, 10.0.0.6``) : c'est la saisie naturelle d'un
    client VLAN a qui l'on a donne quelques adresses. Le premier bloc devient
    l'adresse de la fiche, les autres rejoignent les plages en plus -- une
    seule file, un seul debit, comme si on les avait saisies la.
    """
    texte = str(address or "").strip()
    if not texte:
        raise InvalidStaticClientError("adresse manquante")
    if not re.search(r"[-,;\s]", texte):
        return normalise_address(texte), normalise_extra_prefixes(extra)
    blocs = normalise_extra_prefixes(texte)
    if not blocs:
        raise InvalidStaticClientError("adresse manquante")
    principale, autres = blocs[0], blocs[1:]
    plages = [b for b in autres + normalise_extra_prefixes(extra) if b != principale]
    return principale, normalise_extra_prefixes(plages)


def _liste_json(value: Any) -> list[str]:
    """Une colonne JSONB de liste, quelle que soit la forme rendue par asyncpg."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v]


def _to_row(record: asyncpg.Record) -> dict[str, Any]:
    """Recompose l'adresse au format CIDR attendu par l'interface."""
    row = dict(record)
    adresse = row.pop("address", None)
    longueur = row.pop("prefix_len", None)
    if adresse is not None:
        row["address"] = f"{adresse}/{longueur}" if longueur is not None else str(adresse)
    if "extra_prefixes" in row:
        row["extra_prefixes"] = _liste_json(row["extra_prefixes"])
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
        extra_prefixes=tuple(_liste_json(row.get("extra_prefixes"))),
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
            raise StaticClientNotFoundError(f"unknown static-IP client {client_id}")
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
        adresse, plages = split_address(payload.get("address"), payload.get("extra_prefixes"))
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO static_clients (reference, label, pop_name, address, vlan,
                                                sector_key, plan_down_mbps, plan_up_mbps,
                                                enabled, note, cpe_mac, source,
                                                extra_prefixes)
                    VALUES ($1, $2, $3, $4::inet, $5, $6, $7, $8, $9, $10, $11, 'manual',
                            $12::jsonb)
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
                    _mac(payload.get("cpe_mac")),
                    json.dumps(plages),
                )
            except asyncpg.UniqueViolationError as exc:
                raise DuplicateStaticClientError(
                    f"a static-IP client named '{payload['reference']}' already exists"
                ) from exc
        return _to_row(row)

    async def update(self, client_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        fields = {
            key: value
            for key, value in payload.items()
            if key in CHAMPS_MODIFIABLES and value is not None
        }
        if "address" in fields:
            if "extra_prefixes" in fields:
                deja = fields["extra_prefixes"]
            elif re.search(r"[-,;\s]", str(fields["address"]).strip()):
                # Une plage dans l'adresse, sans toucher aux plages en plus :
                # ses autres blocs s'AJOUTENT a celles de la fiche.
                deja = (await self.get(client_id)).get("extra_prefixes") or []
            else:
                deja = None
            fields["address"], plages = split_address(fields["address"], deja)
            if deja is not None:
                fields["extra_prefixes"] = plages
        if "cpe_mac" in fields:
            # Meme normalisation qu'a la creation : sans elle, une MAC saisie en
            # minuscules ou avec des tirets ne se rapprocherait plus de celle du
            # voisinage radio, et le client paraitrait introuvable.
            fields["cpe_mac"] = _mac(fields["cpe_mac"])
        if "extra_prefixes" in fields:
            # Une liste VIDE est une valeur : elle retire les plages en plus.
            fields["extra_prefixes"] = json.dumps(
                normalise_extra_prefixes(fields["extra_prefixes"])
            )
        if not fields:
            return await self.get(client_id)

        conversions = {"address": "::inet", "extra_prefixes": "::jsonb"}
        assignments = ", ".join(
            f"{name} = ${i + 2}" + conversions.get(name, "") for i, name in enumerate(fields)
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
                    f"a static-IP client named '{payload.get('reference')}' already exists"
                ) from exc
        if row is None:
            raise StaticClientNotFoundError(f"unknown static-IP client {client_id}")
        return _to_row(row)

    async def set_plan(
        self, reference: str, down_mbps: float | None, up_mbps: float | None
    ) -> bool:
        """Plan d'un client a IP fixe ; NULL/NULL = plan par defaut."""
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE static_clients SET plan_down_mbps = $2, plan_up_mbps = $3, "
                "updated_at = now() WHERE reference = $1",
                reference,
                down_mbps,
                up_mbps,
            )
        return not resultat.endswith(" 0")

    async def by_vlan(self) -> list[dict[str, Any]]:
        """Les clients declares, groupes par VLAN.

        LES CLIENTS SUR VLAN ROUTEE SE SAISISSENT A LA MAIN, et c'est la seule
        facon correcte de les connaitre. Ils n'ouvrent aucune session, RADIUS ne
        les decrit pas, et rien sur le reseau ne dit quel debit a ete vendu a
        quelle adresse. Ce que les flux ou la table ARP montrent, ce sont des
        ADRESSES QUI PARLENT -- une imprimante et un client professionnel y ont
        exactement la meme apparence.

        Cette vue est donc le miroir de la saisie, pas d'une decouverte : elle
        rend ce que l'exploitant a declare, range par VLAN, pour qu'il voie d'un
        coup d'oeil ce qui manque.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT vlan,
                       count(*)                                        AS clients,
                       count(*) FILTER (WHERE enabled)                 AS actifs,
                       count(*) FILTER (WHERE source = 'api')          AS depuis_api,
                       sum(coalesce(plan_down_mbps, 0))::float8        AS vendu_down_mbps,
                       sum(coalesce(plan_up_mbps, 0))::float8          AS vendu_up_mbps,
                       array_agg(DISTINCT pop_name)                    AS pops
                FROM static_clients
                WHERE vlan IS NOT NULL
                GROUP BY vlan
                ORDER BY vlan
                """
            )
        return [dict(row) for row in rows]

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
            raise StaticClientNotFoundError(f"unknown static-IP client {client_id}")


class VlanSightingsRepository:
    """Ce que la table ARP des routeurs a montre, et ce qu'on en deduit.

    Le controleur ecrit ici, et NULLE PART ailleurs cote clients statiques :
    l'inventaire ``static_clients`` porte l'intention humaine et lui reste
    interdit. Separer les deux est ce qui garantit qu'une detection ne pourra
    jamais se transformer en fiche toute seule.

    Le rapprochement entre une adresse vue et un client declare se fait par
    CONTENANCE reseau (``<<=``), pas par egalite : un client declare en
    10.0.0.0/29 doit etre reconnu quand c'est 10.0.0.3 qui parle. Ses plages
    additionnelles comptent autant que son adresse : un second pool de la meme
    VLAN n'est pas un candidat, c'est le meme client.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, sightings: Sequence[VlanSighting], *, seen_at: datetime) -> int:
        """Enregistre un tour d'observation. ``first_seen`` n'est jamais recule."""
        if not sightings:
            return 0
        lignes = [
            (
                v.router_name,
                v.address,
                v.mac,
                v.vlan_interface,
                v.vlan_id,
                v.pop_name,
                seen_at,
            )
            for v in sightings
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO vlan_sightings
                       (router_name, address, mac, vlan_interface, vlan_id, pop_name,
                        first_seen, last_seen)
                VALUES ($1, $2::inet, $3, $4, $5, $6, $7, $7)
                ON CONFLICT (router_name, address) DO UPDATE
                   SET mac            = EXCLUDED.mac,
                       vlan_interface = EXCLUDED.vlan_interface,
                       vlan_id        = EXCLUDED.vlan_id,
                       pop_name       = COALESCE(EXCLUDED.pop_name, vlan_sightings.pop_name),
                       last_seen      = EXCLUDED.last_seen
                """,
                lignes,
            )
        return len(lignes)

    async def candidates(
        self, *, limit: int = 500, max_age_s: float | None = None
    ) -> list[dict[str, Any]]:
        """Adresses vues qui ne correspondent a AUCUN client declare.

        C'est la liste que l'interface propose a l'operateur. Elle ne sert qu'a
        ca : rien dans le controleur ne la lit pour construire un plan.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.router_name, host(v.address) AS address, v.mac,
                       v.vlan_interface, v.vlan_id, v.pop_name,
                       v.first_seen, v.last_seen
                  FROM vlan_sightings v
                 WHERE NOT EXISTS (
                           SELECT 1 FROM static_clients c
                            WHERE v.address <<= c.address
                               OR EXISTS (
                                      SELECT 1
                                        FROM jsonb_array_elements_text(c.extra_prefixes) p
                                       WHERE v.address <<= p::inet)
                       )
                   AND ($1::float IS NULL
                        OR v.last_seen > now() - make_interval(secs => $1::float))
                 ORDER BY v.last_seen DESC, v.address
                 LIMIT $2
                """,
                max_age_s,
                limit,
            )
        return [dict(row) for row in rows]

    async def presence(self) -> dict[str, dict[str, Any]]:
        """``reference du client declare -> derniere fois vu actif``.

        Un client declare dont aucune adresse n'a parle n'apparait pas : mieux
        vaut une absence d'information qu'une date inventee.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.reference,
                       max(v.last_seen)                        AS last_seen,
                       count(*)                                AS addresses_seen,
                       (array_agg(v.mac ORDER BY v.last_seen DESC))[1]            AS mac,
                       (array_agg(v.vlan_interface ORDER BY v.last_seen DESC))[1] AS vlan_interface,
                       (array_agg(v.router_name ORDER BY v.last_seen DESC))[1]    AS router_name
                  FROM static_clients c
                  JOIN vlan_sightings v
                    ON v.address <<= c.address
                    OR EXISTS (
                           SELECT 1 FROM jsonb_array_elements_text(c.extra_prefixes) p
                            WHERE v.address <<= p::inet)
                 GROUP BY c.reference
                """
            )
        return {row["reference"]: dict(row) for row in rows}

    async def vlan_sites(self, *, max_age_s: float | None = None) -> list[dict[str, Any]]:
        """Les VLAN sur lesquels QUELQUE CHOSE a ete vu, un par routeur.

        Rend l'interface la plus recemment observee pour chaque couple
        (routeur, VLAN) : c'est de la que vient le NOM du site. Un VLAN sur
        lequel plus rien ne parle depuis longtemps n'est plus un site vivant --
        d'ou le filtre d'age, le meme que pour les candidats.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.router_name,
                       v.vlan_id,
                       (array_agg(v.vlan_interface ORDER BY v.last_seen DESC))[1]
                           AS vlan_interface,
                       count(*)      AS addresses_seen,
                       max(v.last_seen) AS last_seen
                  FROM vlan_sightings v
                 WHERE v.vlan_interface IS NOT NULL
                   AND ($1::float IS NULL
                        OR v.last_seen > now() - make_interval(secs => $1::float))
                 GROUP BY v.router_name, v.vlan_id
                 ORDER BY v.router_name, v.vlan_id
                """,
                max_age_s,
            )
        return [dict(row) for row in rows]

    async def prune(self, *, older_than_s: float) -> int:
        """Oublie ce qui n'a plus parle depuis longtemps.

        Une adresse qui s'est tue pendant des jours n'est plus une piste utile,
        et laisser grossir la liste finirait par la rendre illisible.
        """
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM vlan_sightings "
                "WHERE last_seen < now() - make_interval(secs => $1::float)",
                older_than_s,
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)

"""Le modele de reseau expose par l'API publique (contrat Preseem).

CE QUE CE MODULE FAIT, ET CE QU'IL NE FAIT PAS
----------------------------------------------
Il porte les quatre collections de REFERENCE -- clients, forfaits, sites, points
d'acces -- dans leurs propres tables, parce qu'elles n'ont pas d'equivalent
ailleurs dans le controleur.

Il ne cree PAS de table pour les services. Un service (une ligne vendue : une
adresse, un debit souscrit, un rattachement) est exactement ce que
``static_clients`` porte deja, et c'est de cette table que naissent les abonnes,
les plans et les files posees sur les routeurs. Lui en donner une seconde
reviendrait a avoir deux verites sur le meme client : l'une que l'exploitant
voit dans l'interface, l'autre que la facturation pousse, et une file sur le
routeur qui ne saurait plus laquelle suivre. L'API ecrit donc dans l'inventaire
existant, marque ``source='api'``.

D'OU LA SEULE REGLE DURE DE CE MODULE : l'API ne touche jamais une fiche saisie
a la main. Une integration qui reprend un identifiant deja utilise par un humain
recoit un 409, pas un ecrasement silencieux. L'inverse -- un exploitant qui
corrige a la main une fiche venue de l'API -- reste possible et assume : c'est
lui qui a le dernier mot sur son reseau, et la prochaine synchronisation le
dira.

UNITES. Preseem compte en kbit/s, le controleur en Mbit/s. La conversion se fait
ICI, a la frontiere, et nulle part ailleurs : une unite qui voyage est une unite
qu'on finit par oublier de convertir.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

#: PoP attribue a un service dont le point d'acces ne mene a aucun site connu.
#: Il vaut mieux une fiche rattachee a un PoP visiblement faux qu'une fiche
#: refusee : la facturation ne doit jamais etre bloquee par un trou de
#: referentiel, mais le trou doit se voir.
POP_PAR_DEFAUT = "non-affecte"

TABLES = {
    "accounts": "model_accounts",
    "packages": "model_packages",
    "sites": "model_sites",
    "access_points": "model_access_points",
}


class ModelConflictError(ValueError):
    """L'identifiant vise appartient a une fiche saisie a la main."""


class ModelNotFoundError(LookupError):
    pass


class ModelValidationError(ValueError):
    pass


def kbps_to_mbps(value: Any) -> float | None:
    if value is None:
        return None
    try:
        nombre = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelValidationError(f"debit invalide : {value!r}") from exc
    if nombre <= 0:
        return None
    return nombre / 1000.0


def mbps_to_kbps(value: Any) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 1000.0))


def normalise_prefixes(value: Any) -> list[str]:
    """Valide une liste de prefixes et la rend sous forme canonique.

    Accepte l'adresse nue (``10.0.0.5``) comme le bloc (``10.0.0.0/29``) : un
    professionnel se voit souvent attribuer un bloc entier, et le ramener a une
    adresse laisserait le reste du bloc sans plafond.
    """
    if value is None:
        return []
    brut = value if isinstance(value, (list, tuple)) else [value]
    sortie: list[str] = []
    for item in brut:
        texte = str(item or "").strip()
        if not texte:
            continue
        try:
            reseau = ipaddress.ip_interface(texte).network
        except ValueError as exc:
            raise ModelValidationError(f"prefixe invalide : {texte}") from exc
        canonique = str(reseau)
        if canonique not in sortie:
            sortie.append(canonique)
    return sortie


def parse_attachments(value: Any) -> tuple[list[str], str | None]:
    """Lit le champ ``attachments`` de Preseem : prefixes et MAC du CPE.

    Preseem accepte aussi bien un objet unique qu'une liste d'objets ; les deux
    circulent dans la nature selon l'integrateur. On prend les deux plutot que
    d'imposer la forme que notre lecteur prefere.
    """
    if value is None:
        return [], None
    elements = value if isinstance(value, (list, tuple)) else [value]
    prefixes: list[str] = []
    mac: str | None = None
    for element in elements:
        if not isinstance(element, dict):
            # Une chaine nue dans attachments est lue comme un prefixe : c'est
            # la seule interpretation raisonnable, et la refuser ferait echouer
            # une synchronisation entiere pour une ligne mal formee.
            for prefixe in normalise_prefixes(element):
                if prefixe not in prefixes:
                    prefixes.append(prefixe)
            continue
        for prefixe in normalise_prefixes(element.get("network_prefixes")):
            if prefixe not in prefixes:
                prefixes.append(prefixe)
        brut_mac = element.get("cpe_mac") or element.get("mac")
        if brut_mac and mac is None:
            mac = str(brut_mac).strip().upper() or None
    return prefixes, mac


class ModelRepository:
    """Acces aux quatre collections de reference et aux services."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ refs
    async def list_objects(self, collection: str) -> list[dict[str, Any]]:
        table = TABLES[collection]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
        return [self._render(collection, row) for row in rows]

    async def get_object(self, collection: str, object_id: str) -> dict[str, Any]:
        table = TABLES[collection]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE id = $1",  # noqa: S608
                object_id,
            )
        if row is None:
            raise ModelNotFoundError(f"{collection}/{object_id} inconnu")
        return self._render(collection, row)

    async def put_account(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_accounts (id, name, attributes)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (id) DO UPDATE
                   SET name = EXCLUDED.name,
                       attributes = EXCLUDED.attributes,
                       updated_at = now()
                RETURNING *
                """,
                object_id,
                str(payload.get("name") or object_id),
                _json(payload, {"id", "name"}),
            )
        return self._render("accounts", row)

    async def put_package(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_packages (id, name, down_kbps, up_kbps, attributes)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (id) DO UPDATE
                   SET name = EXCLUDED.name,
                       down_kbps = EXCLUDED.down_kbps,
                       up_kbps = EXCLUDED.up_kbps,
                       attributes = EXCLUDED.attributes,
                       updated_at = now()
                RETURNING *
                """,
                object_id,
                str(payload.get("name") or object_id),
                _positive_int(payload.get("down_speed")),
                _positive_int(payload.get("up_speed")),
                _json(payload, {"id", "name", "down_speed", "up_speed"}),
            )
        return self._render("packages", row)

    async def put_site(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_sites (id, name, attributes)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (id) DO UPDATE
                   SET name = EXCLUDED.name,
                       attributes = EXCLUDED.attributes,
                       updated_at = now()
                RETURNING *
                """,
                object_id,
                str(payload.get("name") or object_id),
                _json(payload, {"id", "name"}),
            )
        return self._render("sites", row)

    async def put_access_point(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        adresse = str(payload.get("ip_address") or "").strip() or None
        if adresse is not None:
            try:
                adresse = str(ipaddress.ip_address(adresse))
            except ValueError as exc:
                raise ModelValidationError(f"ip_address invalide : {adresse}") from exc
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_access_points (id, name, site_id, ip_address, attributes)
                VALUES ($1, $2, $3, $4::inet, $5::jsonb)
                ON CONFLICT (id) DO UPDATE
                   SET name = EXCLUDED.name,
                       site_id = EXCLUDED.site_id,
                       ip_address = EXCLUDED.ip_address,
                       attributes = EXCLUDED.attributes,
                       updated_at = now()
                RETURNING *
                """,
                object_id,
                str(payload.get("name") or object_id),
                _text(payload.get("tower") or payload.get("site")),
                adresse,
                _json(payload, {"id", "name", "tower", "site", "ip_address"}),
            )
        return self._render("access_points", row)

    async def delete_object(self, collection: str, object_id: str) -> None:
        table = TABLES[collection]
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                f"DELETE FROM {table} WHERE id = $1",  # noqa: S608
                object_id,
            )
        if resultat.endswith(" 0"):
            raise ModelNotFoundError(f"{collection}/{object_id} inconnu")

    # -------------------------------------------------------------- services
    async def list_services(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_SERVICES + " ORDER BY reference")
        return [self._render_service(row) for row in rows]

    async def get_service(self, service_id: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_SERVICES + " WHERE reference = $1", service_id)
        if row is None:
            raise ModelNotFoundError(f"services/{service_id} inconnu")
        return self._render_service(row)

    async def put_service(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Cree ou met a jour un service, donc un client a IP fixe.

        Les debits du service priment sur ceux de son forfait, comme chez
        Preseem : une derogation commerciale sur une ligne ne doit pas obliger a
        inventer un forfait pour elle seule.
        """
        prefixes, mac = parse_attachments(payload.get("attachments"))
        prefixes = prefixes or normalise_prefixes(payload.get("network_prefixes"))
        if not prefixes:
            raise ModelValidationError(
                "un service doit porter au moins un prefixe reseau (attachments[].network_prefixes)"
            )

        # ``parent_device_id`` est l'identifiant du point d'acces CHEZ LE
        # FACTURIER. Il est conserve tel quel dans ``access_point_ref``, et
        # surtout PAS recopie dans ``sector_key`` : cette derniere designe un
        # noeud de la topologie decouverte, et y mettre un identifiant externe
        # MASQUERAIT le rattachement reel (celui que la jointure caller-id /
        # UISP etablit) sans rien apporter -- le planificateur ne trouverait
        # simplement aucun parent. Un secteur ne se declare ici que si
        # l'appelant nomme explicitement une cle de topologie.
        point_acces = _text(payload.get("parent_device_id") or payload.get("access_point"))
        async with self._pool.acquire() as conn:
            existante = await conn.fetchrow(
                "SELECT source FROM static_clients WHERE reference = $1", service_id
            )
            if existante is not None and existante["source"] != "api":
                raise ModelConflictError(
                    f"'{service_id}' designe une fiche saisie a la main : "
                    "l'API ne l'ecrase pas. Renommez le service cote facturation, "
                    "ou supprimez la fiche dans l'interface."
                )

            site_id, pop_name = await self._resolve_pop(conn, point_acces, payload)
            descendant, montant = await self._resolve_rates(conn, payload)

            row = await conn.fetchrow(
                """
                INSERT INTO static_clients (reference, label, pop_name, address, vlan,
                                            sector_key, plan_down_mbps, plan_up_mbps,
                                            enabled, note, source, account_ref,
                                            package_ref, access_point_ref, site_ref,
                                            cpe_mac, extra_prefixes)
                VALUES ($1, $2, $3, $4::inet, $5, $6, $7, $8, $9, $10, 'api',
                        $11, $12, $13, $14, $15, $16::jsonb)
                ON CONFLICT (reference) DO UPDATE
                   SET label = EXCLUDED.label,
                       pop_name = EXCLUDED.pop_name,
                       address = EXCLUDED.address,
                       vlan = EXCLUDED.vlan,
                       sector_key = EXCLUDED.sector_key,
                       plan_down_mbps = EXCLUDED.plan_down_mbps,
                       plan_up_mbps = EXCLUDED.plan_up_mbps,
                       enabled = EXCLUDED.enabled,
                       note = EXCLUDED.note,
                       account_ref = EXCLUDED.account_ref,
                       package_ref = EXCLUDED.package_ref,
                       access_point_ref = EXCLUDED.access_point_ref,
                       site_ref = EXCLUDED.site_ref,
                       cpe_mac = EXCLUDED.cpe_mac,
                       extra_prefixes = EXCLUDED.extra_prefixes,
                       updated_at = now()
                RETURNING id
                """,
                service_id,
                _text(payload.get("name") or payload.get("label")),
                pop_name,
                prefixes[0],
                _vlan(payload.get("vlan")),
                _text(payload.get("sector_key")),
                descendant,
                montant,
                bool(payload.get("enabled", True)),
                _text(payload.get("note")),
                _text(payload.get("account")),
                _text(payload.get("package")),
                point_acces,
                site_id,
                mac,
                _json_list(prefixes[1:]),
            )
            complete = await conn.fetchrow(_SELECT_SERVICES + " WHERE id = $1", row["id"])
        return self._render_service(complete)

    async def delete_service(self, service_id: str) -> None:
        async with self._pool.acquire() as conn:
            existante = await conn.fetchrow(
                "SELECT source FROM static_clients WHERE reference = $1", service_id
            )
            if existante is None:
                raise ModelNotFoundError(f"services/{service_id} inconnu")
            if existante["source"] != "api":
                raise ModelConflictError(
                    f"'{service_id}' designe une fiche saisie a la main : l'API ne la supprime pas."
                )
            await conn.execute("DELETE FROM static_clients WHERE reference = $1", service_id)

    # ----------------------------------------------------------------- outils
    async def _resolve_pop(
        self, conn: asyncpg.Connection, access_point: str | None, payload: dict[str, Any]
    ) -> tuple[str | None, str]:
        """Remonte service -> point d'acces -> site -> nom de PoP.

        C'est la seule jointure qui relie le modele de facturation a la
        topologie du controleur : le NOM du site devient le pop_name, et c'est
        par lui que le planificateur trouvera le routeur a configurer.
        """
        site_id = _text(payload.get("site") or payload.get("tower"))
        if site_id is None and access_point is not None:
            row = await conn.fetchrow(
                "SELECT site_id FROM model_access_points WHERE id = $1", access_point
            )
            if row is not None:
                site_id = _text(row["site_id"])
        nom = _text(payload.get("pop_name"))
        if nom is None and site_id is not None:
            row = await conn.fetchrow("SELECT name FROM model_sites WHERE id = $1", site_id)
            nom = _text(row["name"]) if row is not None else site_id
        return site_id, nom or POP_PAR_DEFAUT

    async def _resolve_rates(
        self, conn: asyncpg.Connection, payload: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        descendant = kbps_to_mbps(payload.get("down_speed"))
        montant = kbps_to_mbps(payload.get("up_speed"))
        forfait = _text(payload.get("package"))
        if forfait is not None and (descendant is None or montant is None):
            row = await conn.fetchrow(
                "SELECT down_kbps, up_kbps FROM model_packages WHERE id = $1", forfait
            )
            if row is not None:
                descendant = (
                    descendant if descendant is not None else kbps_to_mbps(row["down_kbps"])
                )
                montant = montant if montant is not None else kbps_to_mbps(row["up_kbps"])
        return descendant, montant

    def _render(self, collection: str, row: asyncpg.Record) -> dict[str, Any]:
        data = dict(row)
        attributs = _load_json(data.pop("attributes", None)) or {}
        sortie: dict[str, Any] = {"id": data["id"], "name": data["name"]}
        if collection == "packages":
            sortie["down_speed"] = data.get("down_kbps")
            sortie["up_speed"] = data.get("up_kbps")
        if collection == "access_points":
            sortie["tower"] = data.get("site_id")
            adresse = data.get("ip_address")
            sortie["ip_address"] = str(adresse) if adresse is not None else None
        if isinstance(attributs, dict):
            for cle, valeur in attributs.items():
                sortie.setdefault(cle, valeur)
        sortie["updated_at"] = data.get("updated_at")
        return sortie

    def _render_service(self, row: asyncpg.Record) -> dict[str, Any]:
        data = dict(row)
        prefixe = f"{data['address']}/{data['prefix_len']}"
        prefixes = [prefixe, *(_load_json(data.get("extra_prefixes")) or [])]
        return {
            "id": data["reference"],
            "name": data.get("label"),
            "pop_name": data.get("pop_name"),
            "account": data.get("account_ref"),
            "package": data.get("package_ref"),
            "parent_device_id": data.get("access_point_ref"),
            "site": data.get("site_ref"),
            "down_speed": mbps_to_kbps(data.get("plan_down_mbps")),
            "up_speed": mbps_to_kbps(data.get("plan_up_mbps")),
            "vlan": data.get("vlan"),
            "enabled": data.get("enabled"),
            "source": data.get("source"),
            "attachments": [
                {"cpe_mac": data.get("cpe_mac"), "network_prefixes": prefixes},
            ],
            "updated_at": data.get("updated_at"),
        }


_SELECT_SERVICES = """
    SELECT id, reference, label, pop_name, host(address) AS address,
           masklen(address) AS prefix_len, vlan, sector_key,
           plan_down_mbps, plan_up_mbps, enabled, note, source,
           account_ref, package_ref, access_point_ref, site_ref,
           cpe_mac, extra_prefixes, created_at, updated_at
    FROM static_clients
"""


def _text(value: Any) -> str | None:
    texte = str(value).strip() if value is not None else ""
    return texte or None


def _vlan(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        numero = int(value)
    except (TypeError, ValueError) as exc:
        raise ModelValidationError(f"vlan invalide : {value!r}") from exc
    if not 1 <= numero <= 4094:
        raise ModelValidationError(f"vlan hors bornes : {numero}")
    return numero


def _positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        nombre = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ModelValidationError(f"debit invalide : {value!r}") from exc
    return nombre if nombre > 0 else None


def _json(payload: dict[str, Any], connus: set[str]) -> str:
    """Conserve les champs qu'on ne modelise pas.

    Un integrateur envoie souvent plus que ce qu'on sait lire (identifiants
    internes, coordonnees, etiquettes). Les jeter rendrait l'API silencieusement
    lossy : la relecture ne rendrait pas ce qui a ete ecrit, et l'integrateur
    passerait des heures a chercher ou son champ s'est perdu.
    """
    return json.dumps({cle: val for cle, val in payload.items() if cle not in connus})


def _json_list(values: list[str]) -> str:
    return json.dumps(values)


def _load_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None

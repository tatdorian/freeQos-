"""Persistance de la topologie et de la politique de shaping."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from app.collectors.topology import TopologySnapshot

logger = logging.getLogger(__name__)


class TopologyRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------- ecriture
    async def save_snapshot(self, snapshot: TopologySnapshot) -> dict[str, int]:
        """Ecrit noeuds et liens en upsert.

        On ne supprime rien : un equipement momentanement invisible (redemarrage,
        fade) ne doit pas disparaitre du graphe. C'est ``last_seen`` qui dit ce
        qui est frais, et l'interface qui le signale.
        """
        if not snapshot.nodes and not snapshot.links:
            return {"nodes": 0, "links": 0}

        noeuds = [
            (
                n.key,
                n.name,
                n.kind,
                n.mac,
                n.address,
                n.platform,
                n.version,
                n.router_name,
                n.uisp_device_id,
                n.config_parent,
                json.dumps(n.attributes),
            )
            for n in snapshot.nodes.values()
        ]
        liens = [
            (
                lk.key,
                lk.source_key,
                lk.target_key,
                lk.kind,
                lk.interface,
                lk.capacity_mbps,
                lk.discovered_by,
                json.dumps(lk.attributes),
            )
            for lk in snapshot.links.values()
        ]

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO topology_nodes
                       (key, name, kind, mac, address, platform, version,
                        router_name, uisp_device_id, config_parent, attributes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    name           = EXCLUDED.name,
                    kind           = EXCLUDED.kind,
                    mac            = COALESCE(EXCLUDED.mac, topology_nodes.mac),
                    address        = COALESCE(EXCLUDED.address, topology_nodes.address),
                    platform       = COALESCE(EXCLUDED.platform, topology_nodes.platform),
                    version        = COALESCE(EXCLUDED.version, topology_nodes.version),
                    router_name    = COALESCE(EXCLUDED.router_name, topology_nodes.router_name),
                    uisp_device_id = COALESCE(EXCLUDED.uisp_device_id,
                                              topology_nodes.uisp_device_id),
                    -- Ecrase, jamais COALESCE : une route par defaut retiree
                    -- doit faire DISPARAITRE le parent qu'elle justifiait.
                    config_parent  = EXCLUDED.config_parent,
                    attributes     = topology_nodes.attributes || EXCLUDED.attributes,
                    last_seen      = now()
                """,
                noeuds,
            )
            await conn.executemany(
                """
                INSERT INTO topology_links
                       (key, source_key, target_key, kind, interface,
                        capacity_mbps, discovered_by, attributes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    kind          = EXCLUDED.kind,
                    capacity_mbps = COALESCE(EXCLUDED.capacity_mbps,
                                             topology_links.capacity_mbps),
                    discovered_by = COALESCE(EXCLUDED.discovered_by,
                                             topology_links.discovered_by),
                    attributes    = topology_links.attributes || EXCLUDED.attributes,
                    last_seen     = now()
                """,
                liens,
            )
        return {"nodes": len(noeuds), "links": len(liens)}

    async def forget_removed_routers(self, current: Sequence[str]) -> int:
        """Efface les cases des routeurs qui ne sont PLUS dans l'inventaire.

        ``save_snapshot`` n'ajoute et ne met a jour, jamais n'efface -- a dessein :
        une lecture qui echoue ne doit pas faire disparaitre un PoP de l'arbre.
        Mais un routeur RETIRE de l'inventaire, lui, n'a plus rien a y faire, et
        il y restait indefiniment.

        DEUX GARDE-FOUS, parce que cette methode efface :

        1. seules les cles ``router:<nom>`` sont concernees. Elles ne sont creees
           que pour un routeur de l'inventaire ; tout le reste du graphe (voisins
           decouverts, radios, clients) n'est jamais touche. Si l'equipement
           existe toujours physiquement, la decouverte suivante le reposera comme
           voisin non gere -- ce qu'il est devenu ;
        2. un inventaire VIDE n'efface rien. Il signifie soit une installation
           neuve (rien a effacer), soit une base momentanement illisible -- et
           dans ce second cas, purger viderait tout l'arbre sur un incident
           passager.
        """
        if not current:
            return 0
        gardes = [f"router:{nom}" for nom in current]
        async with self._pool.acquire() as conn, conn.transaction():
            obsoletes = [
                row["key"]
                for row in await conn.fetch(
                    "SELECT key FROM topology_nodes "
                    " WHERE key LIKE 'router:%' AND NOT (key = ANY($1::text[]))",
                    gardes,
                )
            ]
            if not obsoletes:
                return 0
            # Les liens qui aboutissent a une case effacee n'ont plus de sens,
            # et ceux decouverts PAR ce routeur non plus : plus personne ne les
            # rafraichira.
            await conn.execute(
                "DELETE FROM topology_links "
                " WHERE source_key = ANY($1::text[]) OR target_key = ANY($1::text[]) "
                "    OR discovered_by = ANY($2::text[])",
                obsoletes,
                [cle.removeprefix("router:") for cle in obsoletes],
            )
            await conn.execute("DELETE FROM topology_nodes WHERE key = ANY($1::text[])", obsoletes)
        logger.info("Topologie : %d case(s) de routeur retire effacee(s)", len(obsoletes))
        return len(obsoletes)

    async def forget_stale(self, *, older_than_minutes: int) -> dict[str, int]:
        """Oublie les equipements que plus aucune decouverte ne revoit.

        POURQUOI CETTE ACTION EXISTE. ``save_snapshot`` n'efface jamais rien, et
        c'est le bon defaut : un equipement momentanement invisible -- fade
        radio, redemarrage, lecture en echec -- ne doit pas disparaitre de
        l'arbre. Mais rien ne nettoyait non plus ce qui a VRAIMENT disparu : une
        adresse de gestion changee, un lien de test demonte, un voisin croise une
        fois pendant une migration. Ces cases s'accumulaient indefiniment, et
        l'arbre finissait encombre de fantomes qu'aucun geste ne pouvait retirer
        -- sinon les masquer une par une.

        POURQUOI ELLE EST MANUELLE. Une purge automatique sur l'age ferait
        exactement ce que le defaut refuse : effacer un PoP injoignable depuis
        une heure. C'est donc l'exploitant qui declenche, qui choisit le seuil,
        et qui voit d'abord combien de cases partiraient.

        CE QU'ELLE EPARGNE, TOUJOURS :

        - les routeurs de l'inventaire (cles ``router:``). Ils sont declares, pas
          decouverts : leur case existe meme injoignable, c'est tout l'interet ;
        - les liens et fusions poses A LA MAIN (``discovered_by = 'manual'``) :
          ce sont des decisions d'exploitant, pas des observations.
        """
        seuil = max(1, int(older_than_minutes))
        async with self._pool.acquire() as conn, conn.transaction():
            obsoletes = [
                row["key"]
                for row in await conn.fetch(
                    """
                    SELECT key FROM topology_nodes
                     WHERE key NOT LIKE 'router:%'
                       AND last_seen < now() - ($1 || ' minutes')::interval
                    """,
                    str(seuil),
                )
            ]
            if not obsoletes:
                return {"nodes": 0, "links": 0}
            liens = await conn.fetchval(
                """
                DELETE FROM topology_links
                 WHERE discovered_by IS DISTINCT FROM 'manual'
                   AND (source_key = ANY($1::text[]) OR target_key = ANY($1::text[]))
                RETURNING 1
                """,
                obsoletes,
            )
            # Une case encore reliee par un lien MANUEL reste : l'exploitant a
            # declare cette adjacence, la retirer effacerait sa decision.
            retenues = {
                row["key"]
                for row in await conn.fetch(
                    """
                    SELECT n.key FROM topology_nodes n
                     WHERE n.key = ANY($1::text[])
                       AND EXISTS (SELECT 1 FROM topology_links l
                                    WHERE l.discovered_by = 'manual'
                                      AND (l.source_key = n.key OR l.target_key = n.key))
                    """,
                    obsoletes,
                )
            }
            a_effacer = [cle for cle in obsoletes if cle not in retenues]
            if a_effacer:
                await conn.execute(
                    "DELETE FROM topology_nodes WHERE key = ANY($1::text[])", a_effacer
                )
        logger.info(
            "Topologie : %d case(s) disparue(s) oubliee(s) (seuil %d min)", len(a_effacer), seuil
        )
        return {"nodes": len(a_effacer), "links": int(liens or 0)}

    async def save_attachments(self, attachments: dict[str, tuple[str, str | None]]) -> int:
        """Rattachements abonne -> secteur radio (login -> (secteur, MAC du CPE)).

        La MAC est absente pour un client statique : il n'a pas de CPE observe,
        son rattachement vient d'une declaration.

        LA CLE EST LE LOGIN, PAS L'IDENTIFIANT NUMERIQUE. C'est l'identite stable
        que manipulent le graphe (``subscriber_sectors``), le planificateur et la
        boucle QoE ; leur imposer de resoudre eux-memes un ``subscriber_id``
        obligeait a un aller-retour de plus et ouvrait un decalage possible entre
        les deux tables. La resolution se fait donc ICI, dans la meme requete :
        un login inconnu de ``subscribers`` est simplement ignore par la
        jointure, ce qui est le bon comportement pour un abonne pas encore
        materialise par un cycle de collecte.
        """
        if not attachments:
            return 0
        lignes = [(login, secteur, mac) for login, (secteur, mac) in attachments.items()]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO subscriber_attachments (subscriber_id, sector_key, cpe_mac)
                SELECT s.id, $2, $3 FROM subscribers s WHERE s.login = $1
                ON CONFLICT (subscriber_id) DO UPDATE SET
                    sector_key = EXCLUDED.sector_key,
                    cpe_mac    = COALESCE(EXCLUDED.cpe_mac, subscriber_attachments.cpe_mac),
                    updated_at = now()
                """,
                lignes,
            )
        return len(lignes)

    # -------------------------------------------------------------- lecture
    async def nodes(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, name, COALESCE(kind_override, kind) AS kind, kind AS kind_detected,
                       kind_override, mac, address, platform, version, router_name,
                       uisp_device_id, config_parent, attributes, first_seen, last_seen,
                       pos_x, pos_y, parent_override, hidden,
                       (last_seen > now() - INTERVAL '10 minutes') AS fresh
                  FROM topology_nodes
                 ORDER BY kind, name
                """
            )
        return [dict(row) for row in rows]

    async def links(self) -> list[dict[str, Any]]:
        """Liens du graphe, avec le DEBIT MESURE de leur port.

        La mesure vient de ``interface_latest``, donc de l'interface qui porte le
        lien. ``interface_links`` dit combien d'adjacences partagent ce port :
        a 1 le debit est bien celui du lien, au-dela c'est le debit cumule du
        port (un switch entre le routeur et plusieurs voisins). On expose le
        compte plutot que de laisser croire a une mesure par voisin.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT l.key, l.source_key, l.target_key, l.kind, l.interface,
                       l.capacity_mbps, l.discovered_by, l.attributes,
                       l.first_seen, l.last_seen,
                       (l.last_seen > now() - INTERVAL '10 minutes') AS fresh,
                       s.name AS source_name, t.name AS target_name,
                       COALESCE(t.kind_override, t.kind) AS target_kind,
                       -- Identite PHYSIQUE de l'equipement d'en face. C'est par
                       -- elle que la capacite radio mesuree retrouve son lien :
                       -- le NOM ne peut pas servir (cote lien c'est l'identite
                       -- annoncee en MNDP, cote backhaul le libelle saisi par
                       -- l'exploitant -- deux choses sans rapport).
                       t.uisp_device_id AS target_uisp_device_id,
                       t.mac AS target_mac,
                       p.max_down_mbps, p.max_up_mbps, p.enabled AS policy_enabled,
                       p.note AS policy_note,
                       im.rx_bps, im.tx_bps, im.running,
                       im.capacity_mbps AS port_capacity_mbps,
                       im.ts AS measured_at,
                       (im.ts > now() - INTERVAL '2 minutes') AS measure_fresh,
                       count(*) FILTER (WHERE l.interface IS NOT NULL)
                           OVER (PARTITION BY l.discovered_by, l.interface)
                           AS interface_links
                  FROM topology_links l
                  LEFT JOIN topology_nodes s ON s.key = l.source_key
                  LEFT JOIN topology_nodes t ON t.key = l.target_key
                  LEFT JOIN shaping_policies p
                         ON p.scope = 'link' AND p.target_key = l.key
                  LEFT JOIN interface_latest im
                         ON im.router_name = l.discovered_by AND im.interface = l.interface
                 WHERE NOT COALESCE(l.hidden, FALSE)
                 ORDER BY s.name NULLS LAST, l.interface
                """
            )
        return [dict(row) for row in rows]

    async def add_manual_link(self, source_key: str, target_key: str) -> str:
        """Cree (ou reaffiche) un lien pose a la main entre deux noeuds.

        La decouverte n'ecrase jamais ces liens : ils portent ``discovered_by
        = 'manual'`` et une cle prefixee ``manual:``. Aucune capacite : c'est une
        adjacence declaree par l'operateur, pas un port mesure.
        """
        if source_key == target_key:
            raise ValueError("un lien ne peut pas relier un noeud a lui-meme")
        key = f"manual:{source_key}|{target_key}"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO topology_links
                       (key, source_key, target_key, kind, discovered_by, hidden, attributes)
                VALUES ($1, $2, $3, 'manual', 'manual', FALSE, '{"manual": true}'::jsonb)
                ON CONFLICT (key) DO UPDATE SET
                    hidden = FALSE, last_seen = now()
                """,
                key,
                source_key,
                target_key,
            )
        return key

    async def hide_link(self, key: str) -> bool:
        """Ecarte un lien de l'affichage (adjacence erronee). Reversible."""
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_links SET hidden = TRUE WHERE key = $1", key
            )
        return not resultat.endswith(" 0")

    async def link(self, key: str) -> dict[str, Any] | None:
        """Un lien precis. Passe par ``links()`` : une seule definition de ce
        qu'est un lien enrichi, donc pas de divergence entre la liste et le
        detail."""
        for row in await self.links():
            if row["key"] == key:
                return row
        return None

    async def interface_series(
        self,
        *,
        router_name: str,
        interface: str,
        minutes: int = 60,
        bucket_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Historique de debit d'un port, agrege par pas de temps.

        ``date_bin`` plutot que ``time_bucket`` : le controleur doit tourner sur
        un PostgreSQL nu autant que sur TimescaleDB.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date_bin($4::interval, ts, TIMESTAMPTZ 'epoch') AS bucket,
                       avg(rx_bps) AS rx_bps,
                       avg(tx_bps) AS tx_bps,
                       max(rx_bps) AS rx_peak_bps,
                       max(tx_bps) AS tx_peak_bps,
                       max(capacity_mbps) AS capacity_mbps
                  FROM interface_metrics
                 WHERE router_name = $1 AND interface = $2
                   AND ts > now() - $3::interval
                 GROUP BY bucket
                 ORDER BY bucket
                """,
                router_name,
                interface,
                timedelta(minutes=minutes),
                timedelta(seconds=bucket_seconds),
            )
        return [dict(row) for row in rows]

    async def interface_latest(self) -> list[dict[str, Any]]:
        """Derniere mesure de chaque port, tous routeurs confondus."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT router_name, interface, ts, rx_bps, tx_bps,
                       running, capacity_mbps,
                       (ts > now() - INTERVAL '2 minutes') AS fresh
                  FROM interface_latest
                 ORDER BY router_name, interface
                """
            )
        return [dict(row) for row in rows]

    async def attachments(self) -> dict[str, str]:
        """Identite d'abonne -> cle du secteur radio.

        Deux origines se melangent ici sans distinction, et c'est voulu : la
        jointure caller-id pour les abonnes PPPoE, la declaration manuelle de
        l'inventaire pour les clients statiques. Le consommateur n'a besoin que
        du rattachement, pas de savoir comment on l'a obtenu.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.login, a.sector_key
                  FROM subscriber_attachments a
                  JOIN subscribers s ON s.id = a.subscriber_id
                 WHERE a.sector_key IS NOT NULL
                """
            )
        return {row["login"]: row["sector_key"] for row in rows}

    async def set_node_kind(self, key: str, kind: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE topology_nodes SET kind_override = $2 WHERE key = $1", key, kind
            )

    async def set_node_position(self, key: str, x: float | None, y: float | None) -> bool:
        """Range une case a l'endroit ou l'operateur l'a laissee tomber.

        Purement cosmetique : deplacer une case ne touche aucun equipement.

        DEUX SORTES DE CASES. Celles qui ont un equipement derriere elles
        portent leur position sur leur propre ligne. Les cases d'ABONNES, elles,
        sont calculees a l'affichage : aucune ligne ne les attend, et c'est pour
        cela qu'elles etaient les seules qu'on ne pouvait pas deplacer. Leur
        position vit donc dans ``topology_layout``, a part -- plutot que de
        fabriquer de faux equipements dans la topologie, qui apparaitraient
        ensuite partout ou l'on compte des equipements.
        """
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET pos_x = $2, pos_y = $3 WHERE key = $1", key, x, y
            )
            if not resultat.endswith(" 0"):
                return True
            if x is None and y is None:
                # Remettre une case libre en automatique, c'est OUBLIER sa
                # position, pas en enregistrer une vide : une ligne nulle
                # resterait la pour toujours sans rien dire.
                await conn.execute("DELETE FROM topology_layout WHERE key = $1", key)
                return True
            await conn.execute(
                """
                INSERT INTO topology_layout (key, pos_x, pos_y)
                VALUES ($1, $2, $3)
                ON CONFLICT (key) DO UPDATE
                   SET pos_x = EXCLUDED.pos_x,
                       pos_y = EXCLUDED.pos_y,
                       updated_at = now()
                """,
                key,
                x,
                y,
            )
        return True

    async def node_layout(self) -> dict[str, dict[str, float | None]]:
        """Les positions des cases sans equipement ('abos:<pop>|<login>').

        Rendu a part de ``nodes()`` : ces cles ne designent aucun equipement, et
        les melanger aux noeuds ferait apparaitre des abonnes dans les
        inventaires, les comptages et les recherches d'equipement.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, pos_x, pos_y FROM topology_layout")
        return {
            str(r["key"]): {"pos_x": r["pos_x"], "pos_y": r["pos_y"]}
            for r in rows
            if r["pos_x"] is not None and r["pos_y"] is not None
        }

    async def forget_layout(self, keys: Sequence[str]) -> int:
        """Oublie des positions devenues sans objet (abonne parti, PoP renomme)."""
        if not keys:
            return 0
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM topology_layout WHERE key = ANY($1::text[])", list(keys)
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)

    async def set_node_parent(self, key: str, parent_key: str | None) -> bool:
        """Force le parent d'une case (glisser-deposer un lien).

        NULL retablit l'orientation automatique par role. On refuse qu'une case
        soit son propre parent : ce serait un cycle immediat. Les cycles plus
        longs sont evites cote lecture, en coupant la boucle a l'affichage.
        """
        if parent_key is not None and parent_key == key:
            raise ValueError("un equipement ne peut pas etre son propre parent")
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET parent_override = $2 WHERE key = $1", key, parent_key
            )
        return not resultat.endswith(" 0")

    async def set_node_hidden(self, key: str, hidden: bool) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "UPDATE topology_nodes SET hidden = $2 WHERE key = $1", key, hidden
            )
        return not resultat.endswith(" 0")

    # ------------------------------------------------ fusions manuelles
    async def aliases(self) -> dict[str, str]:
        """Fusions declarees par l'operateur : alias_key -> canonical_key."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT alias_key, canonical_key FROM topology_aliases")
        return {row["alias_key"]: row["canonical_key"] for row in rows}

    async def merge_nodes(self, alias_key: str, canonical_key: str) -> None:
        """Declare que deux cases sont le meme equipement.

        On garde ``canonical_key`` et on replie ``alias_key`` dessus. Refuse une
        case sur elle-meme, et rechaine toute fusion qui pointait deja vers
        l'alias pour eviter une chaine alias -> alias -> canonique."""
        if alias_key == canonical_key:
            raise ValueError("un noeud ne peut pas etre fusionne avec lui-meme")
        async with self._pool.acquire() as conn, conn.transaction():
            # Si le canonique choisi etait lui-meme un alias, on remonte a SA cible.
            cible = await conn.fetchval(
                "SELECT canonical_key FROM topology_aliases WHERE alias_key = $1", canonical_key
            )
            canonical_key = cible or canonical_key
            if alias_key == canonical_key:
                raise ValueError("fusion circulaire refusee")
            await conn.execute(
                """
                INSERT INTO topology_aliases (alias_key, canonical_key)
                VALUES ($1, $2)
                ON CONFLICT (alias_key) DO UPDATE SET
                    canonical_key = EXCLUDED.canonical_key, updated_at = now()
                """,
                alias_key,
                canonical_key,
            )
            # Les cases repliees sur l'alias suivent desormais le meme canonique.
            await conn.execute(
                "UPDATE topology_aliases SET canonical_key = $2, updated_at = now() "
                "WHERE canonical_key = $1",
                alias_key,
                canonical_key,
            )

    async def unmerge_node(self, alias_key: str) -> bool:
        """Annule une fusion manuelle : la case redevient distincte."""
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM topology_aliases WHERE alias_key = $1", alias_key
            )
        return not resultat.endswith(" 0")

    # ------------------------------------------------------------ politique
    async def upsert_policy(
        self,
        *,
        scope: str,
        target_key: str,
        max_down_mbps: float | None,
        max_up_mbps: float | None,
        enabled: bool = True,
        note: str | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO shaping_policies
                       (scope, target_key, max_down_mbps, max_up_mbps, enabled, note, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope, target_key) DO UPDATE SET
                    max_down_mbps = EXCLUDED.max_down_mbps,
                    max_up_mbps   = EXCLUDED.max_up_mbps,
                    enabled       = EXCLUDED.enabled,
                    note          = EXCLUDED.note,
                    updated_by    = EXCLUDED.updated_by,
                    updated_at    = now()
                RETURNING *
                """,
                scope,
                target_key,
                max_down_mbps,
                max_up_mbps,
                enabled,
                note,
                updated_by,
            )
        return dict(row)

    async def delete_policy(self, scope: str, target_key: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                "DELETE FROM shaping_policies WHERE scope = $1 AND target_key = $2",
                scope,
                target_key,
            )
        return not resultat.endswith(" 0")

    async def policies(self, scope: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM shaping_policies
                 WHERE ($1::text IS NULL OR scope = $1)
                 ORDER BY scope, target_key
                """,
                scope,
            )
        return [dict(row) for row in rows]

    async def policy_map(self, scope: str) -> dict[str, dict[str, Any]]:
        return {row["target_key"]: row for row in await self.policies(scope)}

    # ------------------------------------------------- boucle fermee QoE
    async def qoe_link_states(self) -> dict[str, dict[str, Any]]:
        """Etat de la boucle fermee, par lien de secteur."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM qoe_link_states ORDER BY link_key",
            )
        return {row["link_key"]: dict(row) for row in rows}

    async def qoe_trims(self) -> dict[str, float]:
        """Resserrages en cours, lus par ``build_targets``.

        Seuls les liens REELLEMENT resserres remontent : un facteur a 1.0 est
        l'absence de decision, il n'a pas a occuper une entree ni a laisser
        croire que la boucle agit sur ce lien.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT link_key, trim_factor FROM qoe_link_states WHERE trim_factor < 1.0",
            )
        return {row["link_key"]: float(row["trim_factor"]) for row in rows}

    async def save_qoe_link_state(
        self,
        *,
        link_key: str,
        sector_key: str | None,
        trim_factor: float,
        healthy_cycles: int,
        scored_count: int,
        degraded_count: int,
        worst_score: float | None,
        last_action: str,
        last_reason: str,
        triggered: bool,
    ) -> None:
        """Ecrit l'etat d'un secteur apres un cycle de la boucle.

        ``triggered`` dit si le resserrage a REELLEMENT bouge : seul ce cas
        horodate ``last_trigger_at``, sinon un secteur stable verrait sa date de
        declenchement avancer a chaque cycle et le journal ne voudrait plus rien
        dire.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO qoe_link_states
                       (link_key, sector_key, trim_factor, healthy_cycles,
                        scored_count, degraded_count, worst_score,
                        last_action, last_reason, last_trigger_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                        CASE WHEN $10 THEN now() ELSE NULL END)
                ON CONFLICT (link_key) DO UPDATE SET
                    sector_key      = EXCLUDED.sector_key,
                    trim_factor     = EXCLUDED.trim_factor,
                    healthy_cycles  = EXCLUDED.healthy_cycles,
                    scored_count    = EXCLUDED.scored_count,
                    degraded_count  = EXCLUDED.degraded_count,
                    worst_score     = EXCLUDED.worst_score,
                    last_action     = EXCLUDED.last_action,
                    last_reason     = EXCLUDED.last_reason,
                    last_trigger_at = COALESCE(
                        EXCLUDED.last_trigger_at, qoe_link_states.last_trigger_at
                    ),
                    updated_at      = now()
                """,
                link_key,
                sector_key,
                trim_factor,
                healthy_cycles,
                scored_count,
                degraded_count,
                worst_score,
                last_action,
                last_reason,
                triggered,
            )

    # -------------------------------------------------------------- boost
    async def set_boost(
        self,
        *,
        scope: str,
        target_key: str,
        down_mbps: float | None,
        up_mbps: float | None,
        expires_at: datetime,
        reason: str | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Pose un boost temporaire, en creant la ligne de politique si besoin.

        Le boost n'ecrase pas la surcharge permanente : il vient par-dessus et
        s'efface a echeance, laissant l'abonne revenir a son plan.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO shaping_policies
                       (scope, target_key, boost_down_mbps, boost_up_mbps,
                        boost_expires_at, boost_reason, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope, target_key) DO UPDATE SET
                    boost_down_mbps  = EXCLUDED.boost_down_mbps,
                    boost_up_mbps    = EXCLUDED.boost_up_mbps,
                    boost_expires_at = EXCLUDED.boost_expires_at,
                    boost_reason     = EXCLUDED.boost_reason,
                    updated_by       = EXCLUDED.updated_by,
                    updated_at       = now()
                RETURNING *
                """,
                scope,
                target_key,
                down_mbps,
                up_mbps,
                expires_at,
                reason,
                updated_by,
            )
        return dict(row)

    async def clear_boost(self, scope: str, target_key: str) -> bool:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                """
                UPDATE shaping_policies
                   SET boost_down_mbps = NULL, boost_up_mbps = NULL,
                       boost_expires_at = NULL, boost_reason = NULL, updated_at = now()
                 WHERE scope = $1 AND target_key = $2 AND boost_expires_at IS NOT NULL
                """,
                scope,
                target_key,
            )
        return not resultat.endswith(" 0")

    async def active_boosts(self, scope: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scope, target_key, boost_down_mbps, boost_up_mbps,
                       boost_expires_at, boost_reason,
                       EXTRACT(EPOCH FROM (boost_expires_at - now()))::double precision
                           AS seconds_left
                  FROM shaping_policies
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at > now()
                   AND ($1::text IS NULL OR scope = $1)
                 ORDER BY boost_expires_at
                """,
                scope,
            )
        return [dict(row) for row in rows]

    async def expired_boosts(self) -> list[dict[str, Any]]:
        """Boosts arrives a echeance mais dont les champs trainent encore.

        C'est ce que le job d'expiration doit nettoyer, et surtout : ce sont les
        abonnes dont la file doit etre ramenee a son debit normal.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT scope, target_key, boost_expires_at
                  FROM shaping_policies
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at <= now()
                """
            )
        return [dict(row) for row in rows]

    async def purge_expired_boosts(self) -> int:
        async with self._pool.acquire() as conn:
            resultat = await conn.execute(
                """
                UPDATE shaping_policies
                   SET boost_down_mbps = NULL, boost_up_mbps = NULL,
                       boost_expires_at = NULL, boost_reason = NULL, updated_at = now()
                 WHERE boost_expires_at IS NOT NULL AND boost_expires_at <= now()
                """
            )
        return int(resultat.rsplit(" ", 1)[-1] or 0)

    # ------------------------------------------------------ drapeaux runtime
    async def get_flag(self, name: str) -> bool | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT value FROM runtime_flags WHERE name = $1", name)
        return None if value is None else bool(value)

    async def set_flag(
        self, name: str, value: bool, *, updated_by: str | None = None, reason: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_flags (name, value, updated_by, reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE SET
                    value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                    reason = EXCLUDED.reason, updated_at = now()
                """,
                name,
                value,
                updated_by,
                reason,
            )

    async def flag_detail(self, name: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM runtime_flags WHERE name = $1", name)
        return dict(row) if row else None

    # ---------------------------------------------------------------- audit
    async def record_audit(
        self,
        router_name: str,
        *,
        dry_run: bool,
        outcomes: list[tuple[Any, bool, str]],
        author: str | None = None,
    ) -> int:
        """Journalise chaque commande, avec son AUTEUR et le detail des changements.

        ``author`` remonte l'identite qui a declenche l'action (compte connecte,
        ou "system:*" pour les boucles automatiques). ``changes`` conserve le
        dictionnaire {champ: [avant, apres]} de l'action : la commande finale
        seule ne dit pas ce qui a change, donc ne se diagnostique pas depuis
        l'interface.
        """
        if not outcomes:
            return 0
        lignes = [
            (
                router_name,
                action.verb,
                action.path,
                action.command,
                dry_run,
                ok,
                detail[:2000],
                author,
                json.dumps({k: list(v) for k, v in action.changes.items()})
                if getattr(action, "changes", None)
                else None,
            )
            for action, ok, detail in outcomes
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO enforcement_audit
                       (router_name, verb, path, command, dry_run, ok, detail, author, changes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                lignes,
            )
        return len(lignes)

    async def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, router_name, verb, path, command, dry_run, ok, detail,
                       author, changes
                  FROM enforcement_audit
                 ORDER BY ts DESC
                 LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

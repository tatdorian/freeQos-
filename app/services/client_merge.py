"""Un client declare a la main sur l'IP d'une session deja connue : UN seul client.

LE BESOIN. Un client existe deja (sa session PPPoE, decouverte sur le routeur)
et l'exploitant le declare a la main avec son IP fixe, sous un autre nom. Il
apparaissait alors DEUX fois : la fiche a IP fixe et la session, dans la liste
comme dans l'arbre -- et un mode pose sur l'un ne valait pas pour l'autre.

LA FUSION. La session reste le client (c'est elle que le routeur connait) ; ce
que l'exploitant a saisi sur la fiche passe sur elle :

- le NOM donne a la main devient son nom affiche ;
- son mode, sa limite, son boost, son forfait, son journal d'activite ;
- la fiche a IP fixe en double est supprimee.

Seulement pour une fiche DECLAREE A LA MAIN (``source = 'manual'``), sur UNE
adresse (/32), quand UNE seule session mesuree recemment porte cette IP et
qu'elle n'a pas ete retiree de freeQoS. Une fiche poussee par l'API de
facturation reste a l'API : elle la gere, et la recreerait.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

JOB_MERGE = "merge_clients"
#: Une session mesuree il y a plus longtemps ne prouve plus qui porte l'IP.
FRAICHEUR = timedelta(minutes=10)
_COORDONNEES = ("cpe_mac", "phone", "email", "postal_address", "note")


def _hote(adresse: Any) -> str:
    return str(adresse or "").split("/")[0].strip()


async def merge_duplicates(
    container: Any, *, only_reference: str | None = None, actor: str = "system:merge"
) -> list[dict[str, str]]:
    """Fusionne les fiches a la main posees sur l'IP d'une session. Rend les fusions."""
    statiques = getattr(container, "static_clients_repo", None)
    metriques = getattr(container, "repository", None)
    if statiques is None or metriques is None:
        return []
    fiches = await statiques.list_all()
    abonnes = await metriques.subscriber_latest(
        limit=5000, order_by="login", include_unmeasured=True, kind="pppoe"
    )
    retires = getattr(container, "exclusions", None)
    maintenant = datetime.now(tz=UTC)
    sessions_par_ip: dict[str, list[str]] = {}
    for abonne in abonnes:
        if abonne.get("kind") == "static":
            continue
        ip = _hote(abonne.get("last_ip"))
        vus = [
            v if v.tzinfo is not None else v.replace(tzinfo=UTC)
            for v in (abonne.get("ts"), abonne.get("last_seen"))
            if isinstance(v, datetime)
        ]
        if not ip or not vus or maintenant - max(vus) > FRAICHEUR:
            continue
        login = str(abonne.get("login"))
        if retires is not None and login in retires:
            continue
        sessions_par_ip.setdefault(ip, []).append(login)

    fusions: list[dict[str, str]] = []
    for fiche in fiches:
        reference = str(fiche.get("reference") or "")
        if only_reference is not None and reference != only_reference:
            continue
        if fiche.get("source") != "manual":
            continue
        if int(fiche.get("prefix_len") or 32) not in (32, 128):
            continue
        ip = _hote(fiche.get("address"))
        candidats = [c for c in sessions_par_ip.get(ip, []) if c != reference]
        if len(candidats) != 1:
            continue
        cible = candidats[0]
        try:
            await _fusionner(container, fiche, cible, ip, actor)
        except Exception:  # noqa: BLE001 - une fusion ratee n'empeche pas les autres
            logger.exception("Fusion de '%s' dans '%s' impossible", reference, cible)
            continue
        fusions.append({"from": reference, "into": cible, "address": ip})
        logger.info("Client '%s' (fiche a la main, %s) fusionne dans '%s'", reference, ip, cible)
    return fusions


async def _fusionner(
    container: Any, fiche: dict[str, Any], cible: str, ip: str, actor: str
) -> None:
    reference = str(fiche["reference"])
    nom = str(fiche.get("label") or "").strip() or reference
    pool = container.database.pool
    async with pool.acquire() as conn, conn.transaction():
        # Mode, limite, boost : ceux de la fiche passent sur la session si elle
        # n'en a pas ; un double eventuel de la fiche disparait.
        await conn.execute(
            """
            UPDATE shaping_policies SET target_key = $2, updated_at = now()
             WHERE scope = 'subscriber' AND target_key = $1
               AND NOT EXISTS (SELECT 1 FROM shaping_policies
                                WHERE scope = 'subscriber' AND target_key = $2)
            """,
            reference,
            cible,
        )
        await conn.execute(
            "DELETE FROM shaping_policies WHERE scope = 'subscriber' AND target_key = $1",
            reference,
        )
        # Le forfait : celui saisi pour la fiche, sinon celui de la fiche elle-meme.
        await conn.execute(
            """
            UPDATE client_plans SET login = $2, updated_at = now()
             WHERE login = $1 AND NOT EXISTS (SELECT 1 FROM client_plans WHERE login = $2)
            """,
            reference,
            cible,
        )
        await conn.execute("DELETE FROM client_plans WHERE login = $1", reference)
        if fiche.get("plan_down_mbps") or fiche.get("plan_up_mbps"):
            await conn.execute(
                """
                INSERT INTO client_plans (login, down_mbps, up_mbps, source, updated_by,
                                          updated_at)
                VALUES ($1, $2, $3, 'ui', $4, now())
                ON CONFLICT (login) DO NOTHING
                """,
                cible,
                fiche.get("plan_down_mbps"),
                fiche.get("plan_up_mbps"),
                actor,
            )
        # Le journal suit le client.
        await conn.execute(
            "UPDATE client_activity SET login = $2 WHERE login = $1", reference, cible
        )
        # Le nom donne a la main devient le nom affiche de la session ; les
        # coordonnees saisies sur la fiche completent les siennes, sans les ecraser.
        ancien = await conn.fetchrow("SELECT * FROM client_profiles WHERE login = $1", reference)
        reprise = {c: (ancien[c] if ancien is not None else None) for c in _COORDONNEES}
        await conn.execute(
            """
            INSERT INTO client_profiles (login, display_name, cpe_mac, phone, email,
                                         postal_address, note, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (login) DO UPDATE SET
                display_name   = COALESCE(client_profiles.display_name, EXCLUDED.display_name),
                cpe_mac        = COALESCE(client_profiles.cpe_mac, EXCLUDED.cpe_mac),
                phone          = COALESCE(client_profiles.phone, EXCLUDED.phone),
                email          = COALESCE(client_profiles.email, EXCLUDED.email),
                postal_address = COALESCE(client_profiles.postal_address,
                                          EXCLUDED.postal_address),
                note           = COALESCE(client_profiles.note, EXCLUDED.note),
                updated_at     = now()
            """,
            cible,
            nom,
            reprise["cpe_mac"] or fiche.get("cpe_mac"),
            reprise["phone"],
            reprise["email"],
            reprise["postal_address"],
            reprise["note"] or fiche.get("note"),
            actor,
        )
        await conn.execute("DELETE FROM client_profiles WHERE login = $1", reference)
        # La fiche en double, et son abonne (son historique double celui de la session).
        await conn.execute("DELETE FROM static_clients WHERE id = $1", fiche["id"])
        await conn.execute("DELETE FROM subscribers WHERE login = $1", reference)

    collection = getattr(container, "collection", None)
    if collection is not None:
        try:
            collection.directory.forget_subscriber(reference)
            getattr(collection, "_known_logins", set()).discard(reference)
        except Exception:  # noqa: BLE001
            logger.debug("Oubli de '%s' par la collecte impossible", reference)
    activite = getattr(container, "activity_repo", None)
    applique: Any = None
    if getattr(container, "shaping", None) is not None:
        # Le forfait repris est relu (fiche de l'abonne), puis sa file posee.
        from app.services.plans import apply_now

        applique = await apply_now(container, cible, author=actor)
    if activite is not None:
        await activite.record(
            actor=actor,
            login=cible,
            action="Merged",
            detail=f"{nom} (declared by hand on {ip}) is this client: one client now",
            applied=applique,
        )

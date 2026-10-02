"""Le bouton "Reset queues" : effacer NOS files d'un routeur, puis les reposer.

Une file d'une version precedente (ciblee sur une interface, sur l'adresse
d'hier) peut masquer la bonne -- RouterOS s'arrete a la premiere qui
correspond. Le nettoyage la fait partir ; il ne doit JAMAIS toucher une file
posee a la main ou par RADIUS.
"""

from __future__ import annotations

from app.config import Settings
from app.enforcement.models import MANAGED_COMMENT
from tests.conftest import FakeRouterOsClient
from tests.test_enforcement import FauxClientEcriture
from tests.test_shaping_service import make_service


async def test_seules_nos_files_partent_les_enfants_avant_les_parents(
    settings: Settings,
) -> None:
    settings.enforcement_enabled = False  # geste humain : ecrit quand meme
    routeur = FakeRouterOsClient()
    routeur.simple_queue_rows = [
        {".id": "*1", "name": "freeqos-parent-bh", "comment": MANAGED_COMMENT, "parent": "none"},
        {
            ".id": "*2",
            "name": "freeqos-nestl",
            "comment": MANAGED_COMMENT,
            "parent": "freeqos-parent-bh",
        },
        # Ancienne file sans commentaire, mais avec notre prefixe : elle part.
        {".id": "*3", "name": "freeqos-vieille", "comment": "", "parent": "none"},
        # File de l'exploitant : intouchable.
        {".id": "*4", "name": "client-vip", "comment": "pose a la main", "parent": "none"},
    ]
    ecriture = FauxClientEcriture()
    service = make_service(settings, routeur, write_client_factory=lambda c: ecriture)
    await service.registry.reload()

    rapport = await service.clean_queues("pop-test", author="test")

    retraits = [a for a in ecriture.executed if a.verb == "remove"]
    ids = [a.target_id for a in retraits]
    assert "*4" not in ids
    assert set(ids) == {"*1", "*2", "*3"}
    # L'enfant part avant son parent.
    assert ids.index("*2") < ids.index("*1")
    assert rapport["removed"] == 3
    assert rapport["kept_foreign"] == 1
    assert rapport["errors"] == []


async def test_un_routeur_inconnu_est_refuse(settings: Settings) -> None:
    service = make_service(settings, FakeRouterOsClient())
    await service.registry.reload()
    try:
        await service.clean_queues("inexistant")
    except KeyError:
        return
    raise AssertionError("un routeur absent de l'inventaire doit etre refuse")

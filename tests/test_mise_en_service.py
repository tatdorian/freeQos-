"""Mise en service automatique d'un equipement, des son ajout.

DEMANDE EXPLICITE : une fois le routeur connecte par son API, tout se fait
seul -- types de file CAKE, premieres files, export NetFlow, arbre -- sans
attendre les cycles (files sous 2 min, export sous 10, arbre sous 15).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.enforcement.capability import WriteCapability
from app.services import provisioning
from app.services.collection import JOB_RECONCILE, JOB_SUBSCRIBERS, JOB_TOPOLOGY
from app.services.netflow_export import JOB_NETFLOW_EXPORT
from app.services.shaping import RouterShapingState


class Planificateur:
    def __init__(self) -> None:
        self.lances: list[str] = []

    def status(self) -> list[dict[str, Any]]:
        return [{"job": n, "last_ok": True, "last_error": None} for n in self.lances]

    async def run_once(self, nom: str) -> Any:
        self.lances.append(nom)
        return SimpleNamespace(ok=True, errors=[], items=3)


class Shaping:
    def __init__(self, *, ecriture: bool = True, droits: bool | None = True) -> None:
        self.enforcement_enabled = ecriture
        self.droits = droits

    async def write_capability(self, nom: str) -> WriteCapability:
        return WriteCapability(
            username="qos",
            can_write=self.droits,
            group="full",
            detail="" if self.droits is not False else "no 'write' policy",
        )

    async def inspect(self, nom: str | None = None) -> list[RouterShapingState]:
        return [
            RouterShapingState(
                router_name="nas-a",
                simple_queues=[
                    {"name": "freeqos-dupont", "comment": "freeqos:managed"},
                    {"name": "a-la-main", "comment": ""},
                ],
                queue_types=[
                    {"name": "freeqos-cake-down", "kind": "cake"},
                    {"name": "freeqos-cake-up", "kind": "cake"},
                    {"name": "default", "kind": "pfifo"},
                ],
            )
        ]


class Export:
    async def state_of(self, collecteur: Any) -> Any:
        return SimpleNamespace(enabled=True, collector="10.0.0.1:2055", state="posee")


def conteneur(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "registry": SimpleNamespace(collectors=[SimpleNamespace(name="nas-a")]),
        "scheduler": Planificateur(),
        "shaping": Shaping(),
        "netflow_export": Export(),
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_tout_est_deroule_dans_l_ordre_puis_constate_sur_le_routeur() -> None:
    c = conteneur()
    rapport = await provisioning.run(c, "nas-a")

    assert rapport["state"] == "done" and rapport["blocker"] is None
    lances = c.scheduler.lances
    # Les files ont besoin des sessions et de la decouverte ; l'export vient apres.
    assert lances.index(JOB_SUBSCRIBERS) < lances.index(JOB_TOPOLOGY) < lances.index(JOB_RECONCILE)
    assert lances.index(JOB_RECONCILE) < lances.index(JOB_NETFLOW_EXPORT)
    # Ce qui est REELLEMENT sur le routeur, relu apres coup.
    assert rapport["result"]["cake_types"] == ["freeqos-cake-down", "freeqos-cake-up"]
    assert rapport["result"]["managed_queues"] == 1
    assert rapport["result"]["netflow_export"]["enabled"] is True


async def test_ecriture_coupee_rien_n_est_ecrit_et_le_rapport_dit_comment_debloquer() -> None:
    c = conteneur(shaping=Shaping(ecriture=False))
    rapport = await provisioning.run(c, "nas-a")

    assert rapport["state"] == "blocked"
    assert "Settings > Shaping and writing" in rapport["blocker"]
    assert JOB_RECONCILE not in c.scheduler.lances
    assert JOB_NETFLOW_EXPORT not in c.scheduler.lances
    # La lecture, elle, a bien eu lieu : l'arbre et les abonnes apparaissent.
    assert JOB_TOPOLOGY in c.scheduler.lances


async def test_un_compte_sans_droits_d_ecriture_bloque_avec_le_motif() -> None:
    c = conteneur(shaping=Shaping(droits=False))
    rapport = await provisioning.run(c, "nas-a")
    assert rapport["state"] == "blocked" and "write" in rapport["blocker"]
    assert JOB_RECONCILE not in c.scheduler.lances


async def test_un_routeur_non_collecte_est_dit_tel_quel() -> None:
    rapport = await provisioning.run(conteneur(), "inconnu")
    assert rapport["state"] == "failed" and "not collected" in rapport["blocker"]


async def test_le_lancement_rend_la_main_et_l_etat_se_lit_ensuite() -> None:
    import asyncio

    c = conteneur()
    initial = provisioning.start(c, "nas-a")
    assert initial["state"] == "running"
    for _ in range(50):
        await asyncio.sleep(0)
        if provisioning.status("nas-a")["state"] != "running":  # type: ignore[index]
            break
    assert provisioning.status("nas-a")["state"] == "done"  # type: ignore[index]

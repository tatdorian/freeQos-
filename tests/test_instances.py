"""Deux instances de freeQoS sur les memes routeurs ne se battent plus.

CONSTATE : une instance Docker et une instance Kubernetes ont pilote les memes
routeurs ; chacune reecrivait toutes les deux minutes ce que l'autre venait de
poser. Chaque objet porte desormais l'identifiant de l'instance qui l'a pose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.container import resolve_instance_id
from app.enforcement import models
from app.enforcement.models import QueueSpec
from app.enforcement.planner import build_plan


@pytest.fixture(autouse=True)
def instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "INSTANCE_ID", "fq-moi")


def _spec(nom: str = "freeqos-dupont", cible: str = "10.20.0.10/32") -> QueueSpec:
    return QueueSpec(name=nom, target=cible, max_up_mbps=10, max_down_mbps=50)


def _plan(existantes: list[dict[str, str]], spec: QueueSpec | None = None):  # type: ignore[no-untyped-def]
    return build_plan(
        "nas",
        desired_types=[],
        desired_queues=[spec or _spec()],
        actual_types=[],
        actual_queues=existantes,
        adopt=True,
    )


def test_le_commentaire_porte_l_instance() -> None:
    assert _spec().comment == "freeqos:managed instance=fq-moi"


def test_une_file_d_une_autre_instance_n_est_jamais_modifiee_ni_retiree() -> None:
    autre = {
        ".id": "*5A",
        "name": "freeqos-dupont",
        "target": "ether3,lan-bridge",
        "max-limit": "1000000/1000000",
        "comment": "freeqos:managed instance=fq-autre",
    }
    plan = _plan([autre])
    assert not plan.actions
    assert "ANOTHER freeQoS instance (fq-autre)" in plan.conflicts[0].detail
    # Ni le menage (prune) : elle n'est pas a nous.
    plan = _plan([autre], _spec(nom="freeqos-autre-client", cible="10.20.0.99/32"))
    assert not [a for a in plan.actions if a.verb == "remove"]


def test_une_file_tierce_d_une_autre_instance_n_est_pas_adoptee() -> None:
    autre = {
        ".id": "*26",
        "name": "freeqos-x",
        "target": "10.20.0.10/32",
        "max-limit": "1000000/1000000",
        "comment": "freeqos:managed instance=fq-autre",
    }
    plan = _plan([autre])
    assert not plan.actions
    assert "ANOTHER freeQoS instance" in plan.conflicts[0].detail


def test_une_ancienne_marque_sans_instance_est_reclamee_une_fois() -> None:
    ancienne = {
        ".id": "*1",
        "name": "freeqos-dupont",
        "target": "10.20.0.10/32",
        "max-limit": "10000000/50000000",
        "comment": "freeqos:managed",
        "parent": "none",
        "disabled": "no",
    }
    [reclame] = _plan([ancienne]).actions
    assert reclame.verb == "set" and reclame.fields == {
        "comment": "freeqos:managed instance=fq-moi"
    }
    # Une fois reclamee, plus rien a ecrire.
    ancienne["comment"] = "freeqos:managed instance=fq-moi"
    assert _plan([ancienne]).is_empty


def test_l_identifiant_est_garde_d_un_demarrage_a_l_autre(tmp_path: Path) -> None:
    reglages = Settings(_env_file=None, app_secret_key_file=tmp_path / "secret.key")
    premier = resolve_instance_id(reglages)
    assert premier.startswith("fq-")
    assert resolve_instance_id(reglages) == premier
    assert (tmp_path / "instance.id").read_text().strip() == premier
    impose = Settings(_env_file=None, freeqos_instance_id="docker lab", app_secret_key_file=None)
    assert resolve_instance_id(impose) == "docker-lab"

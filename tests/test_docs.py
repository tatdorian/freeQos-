"""Coherence entre la documentation OpenAPI et l'etat reel des fonctionnalites.

P2-7 : la description de ``/docs`` affirmait encore "Phase 1 : lecture uniquement,
aucun endpoint n'ecrit" alors que l'ecriture (enforcement) est active et tracee.
Ce test verrouille la coherence : si un endpoint d'ecriture existe, la description
ne doit pas pretendre le contraire, et inversement.
"""

from __future__ import annotations

from app.config import Settings
from app.main import create_app


def _schema() -> dict:
    return create_app(Settings(_env_file=None)).openapi()


def test_description_ne_pretend_plus_la_lecture_seule() -> None:
    description = _schema()["info"]["description"].lower()
    # Les anciennes affirmations, devenues fausses, ne doivent plus figurer.
    assert "aucun endpoint n'ecrit" not in description
    assert "lecture uniquement" not in description
    assert "phase 1 : collecte et lecture" not in description


def test_description_coherente_avec_les_endpoints_d_ecriture() -> None:
    schema = _schema()
    description = schema["info"]["description"].lower()
    paths = schema["paths"]

    # Un endpoint qui ECRIT sur un routeur existe reellement...
    apply_path = "/api/v1/shaping/apply"
    assert apply_path in paths
    assert "post" in paths[apply_path]

    # ... donc la description doit reconnaitre l'ecriture, et son garde-fou.
    assert "ecriture" in description or "enforcement" in description
    assert "enforcement_enabled" in description
    # ... et la tracabilite (audit) promise par P0-4.
    assert "enforcement_audit" in description


def test_description_annonce_les_deux_natures_d_abonnes() -> None:
    """Un lecteur de /docs doit savoir que les clients a IP fixe existent, et
    surtout qu'ils sont DECLARES : croire a une decouverte automatique ferait
    chercher longtemps pourquoi un client n'apparait pas tout seul."""
    schema = _schema()
    description = schema["info"]["description"].lower()
    paths = schema["paths"]

    assert "/api/v1/static-clients" in paths
    assert "post" in paths["/api/v1/static-clients"]

    assert "static" in description
    assert "pppoe" in description
    # L'origine manuelle doit etre dite, pas sous-entendue.
    assert "declare a la main" in description or "declare" in description


def test_description_dit_que_la_detection_ne_cree_rien() -> None:
    """La pire erreur de lecture possible sur cette fonctionnalite serait de
    croire qu'un client detecte est pris en charge. La description doit dire
    l'inverse, explicitement."""
    schema = _schema()
    description = schema["info"]["description"].lower()
    paths = schema["paths"]

    # La route de consultation existe...
    candidats = "/api/v1/static-clients/candidates"
    assert candidats in paths
    assert set(paths[candidats]) == {"get"}

    # ... et il n'existe AUCUNE route de promotion.
    assert not [c for c in paths if "promote" in c or "declare" in c]

    assert "/ip/arp" in description
    assert "candidat" in description
    # L'absence de faconnage doit etre dite, pas sous-entendue.
    assert "jamais faconne" in description or "aucun candidat n'est jamais" in description

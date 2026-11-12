"""Le premier compte Administrateur, cree au deploiement par la ligne de
commande (install.sh) plutot qu'a la premiere visite de l'interface."""

from __future__ import annotations

from pathlib import Path

from app import cli
from app.db.users_repo import InMemoryUsersRepository
from app.services.accounts import check_password, role_at_least, verify_password

RACINE = Path(__file__).resolve().parents[1]


def test_le_mot_de_passe_tire_au_hasard_est_solide_et_lisible() -> None:
    mots = {cli.generate_password() for _ in range(50)}
    assert len(mots) == 50
    for mot in mots:
        assert check_password(mot) == mot
        assert len(mot) == 23 and mot.count("-") == 3
        assert not set(mot) & set("0O1lI")


async def test_un_premier_compte_administrateur_avec_mot_de_passe_genere() -> None:
    depot = InMemoryUsersRepository()
    code, message = await cli.create_admin(depot, " NOC@Example.com ", None)
    assert code == cli.SORTIE_CREE
    mot = message.split("password: ", 1)[1].strip()
    fiche = await depot.credentials("noc@example.com")
    assert fiche is not None and role_at_least(fiche["role"], "edit")
    assert verify_password(mot, fiche["password_hash"])
    assert await cli.count_accounts(depot) == (cli.SORTIE_CREE, "1")


async def test_une_mise_a_jour_ne_touche_pas_aux_comptes() -> None:
    depot = InMemoryUsersRepository()
    assert (await cli.create_admin(depot, "noc@example.com", "Baobab-Sahel-Niamey-27"))[0] == 0
    code, message = await cli.create_admin(depot, "autre@example.com", None)
    assert code == cli.SORTIE_EXISTE and "already exists" in message
    assert await depot.count() == 1


async def test_une_saisie_refusee_ne_cree_rien() -> None:
    depot = InMemoryUsersRepository()
    assert (await cli.create_admin(depot, "pas-un-email", None))[0] == cli.SORTIE_SAISIE
    code, message = await cli.create_admin(depot, "noc@example.com", "motdepasse")
    assert code == cli.SORTIE_SAISIE and "too short" in message
    assert await depot.count() == 0


def test_install_cree_l_administrateur_seulement_sur_une_installation_neuve() -> None:
    script = (RACINE / "install.sh").read_text()
    assert "python -m app.cli" in script and "cli count-accounts" in script
    assert 'if [ "$COMPTES" = "0" ]' in script
    assert "create-admin --email" in script
    # Le mot de passe fourni passe par l'entree standard, jamais en argument
    # (visible dans `ps`).
    assert "--password-stdin" in script and 'FREEQOS_ADMIN_PASSWORD" |' in script

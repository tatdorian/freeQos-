"""Le premier compte Administrateur, cree au deploiement par la ligne de
commande (install.sh) plutot qu'a la premiere visite de l'interface."""

from __future__ import annotations

from pathlib import Path

from app import cli
from app.db.users_repo import InMemoryUsersRepository
from app.services.accounts import check_password, hash_password, role_at_least, verify_password

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


def test_install_affiche_le_compte_cree_par_l_application_sans_rien_demander() -> None:
    script = (RACINE / "install.sh").read_text()
    assert "cat data/first-admin.txt" in script
    assert "/dev/tty" not in script and "read -r" not in script
    assert "FREEQOS_ADMIN_EMAIL=%s" in script  # transmis a l'application par .env


async def test_le_premier_demarrage_cree_l_administrateur_et_son_fichier(tmp_path) -> None:
    from app.services.first_admin import ensure_first_admin, forget_file, read_file

    depot = InMemoryUsersRepository()
    fichier = tmp_path / "data" / "first-admin.txt"
    fiche = await ensure_first_admin(depot, fichier, "noc@example.com")
    assert fiche is not None and fiche["email"] == "noc@example.com"
    assert "password_hash" not in fiche
    assert role_at_least(fiche["role"], "edit")
    assert fichier.stat().st_mode & 0o777 == 0o600
    champs = read_file(fichier)
    stocke = await depot.credentials("noc@example.com")
    assert stocke is not None and verify_password(champs["password"], stocke["password_hash"])

    # Redemarrage : rien ne change, le fichier reste tant que le mot de passe vaut.
    assert await ensure_first_admin(depot, fichier, "autre@example.com") is None
    assert await depot.count() == 1 and fichier.exists()

    # Le mot de passe change : le fichier part (au geste, ou au demarrage suivant).
    forget_file(fichier, "noc@example.com")
    assert not fichier.exists()


async def test_un_fichier_perime_part_au_demarrage(tmp_path) -> None:
    from app.services.first_admin import ensure_first_admin

    depot = InMemoryUsersRepository()
    fichier = tmp_path / "first-admin.txt"
    await ensure_first_admin(depot, fichier, None)
    stocke = await depot.credentials("admin@freeqos.local")
    assert stocke is not None
    await depot.update(int(stocke["id"]), password_hash=hash_password("Nouveau-Baobab-2026!"))
    await ensure_first_admin(depot, fichier, None)
    assert not fichier.exists()


async def test_reinitialiser_un_mot_de_passe_en_ligne_de_commande() -> None:
    depot = InMemoryUsersRepository()
    await cli.create_admin(depot, "noc@example.com", "Baobab-Sahel-Niamey-27")
    code, message = await cli.reset_password(depot, "noc@example.com")
    assert code == cli.SORTIE_CREE
    mot = message.split("password: ", 1)[1].strip()
    stocke = await depot.credentials("noc@example.com")
    assert stocke is not None and verify_password(mot, stocke["password_hash"])
    assert (await cli.reset_password(depot, "inconnu@example.com"))[0] == cli.SORTIE_SAISIE

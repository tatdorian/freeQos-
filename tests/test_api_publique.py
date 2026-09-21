"""L'API publique : le contrat qui permet de remplacer Preseem.

POURQUOI CES TESTS COMPTENT PLUS QUE LA MOYENNE
-----------------------------------------------
Ce qui coute cher dans une bascule, ce n'est pas le controleur : c'est tout ce
qui lui parle. Splynx, UISP/UCRM, Powercode, Visp et les developpements maison
poussent deja leur inventaire vers l'API "model" de Preseem. Si la FORME differe
d'un octet -- une authentification Basic refusee, un PUT qui repond 405, un
debit lu en Mbit/s la ou l'appelant envoie des kbit/s -- l'integration est a
reecrire, et l'interet de l'operation disparait.

Chaque test ci-dessous fige donc un point du contrat, pas une preference.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.db.model_repo import (
    ModelConflictError,
    ModelNotFoundError,
    ModelValidationError,
    kbps_to_mbps,
    mbps_to_kbps,
    normalise_prefixes,
    parse_attachments,
)
from app.main import register_routes
from app.services.api_keys import (
    InvalidApiKeyError,
    extract_prefix,
    generate_key,
    hash_key,
    matches,
    normalise_scopes,
)
from tests.test_api import build_container


# =========================================================================
# Doubles : ils reproduisent le CONTRAT des depots, pas leur SQL.
# =========================================================================
class DepotCles:
    def __init__(self) -> None:
        self.lignes: list[dict[str, Any]] = []
        self._suivant = 1

    async def list_all(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in ligne.items() if k != "key_hash"} for ligne in self.lignes]

    async def create(
        self,
        *,
        name: str,
        scopes: list[str] | None = None,
        note: str | None = None,
        created_by: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        tiree = generate_key()
        fiche = {
            "id": self._suivant,
            "name": name,
            "prefix": tiree.prefix,
            "key_hash": tiree.key_hash,
            "scopes": normalise_scopes(scopes),
            "enabled": True,
            "note": note,
            "created_by": created_by,
            "expires_at": expires_at,
            "last_used_at": None,
            "created_at": datetime.now(tz=UTC),
            "updated_at": datetime.now(tz=UTC),
        }
        self._suivant += 1
        self.lignes.append(fiche)
        return {k: v for k, v in fiche.items() if k != "key_hash"}, tiree.secret

    async def set_enabled(self, key_id: int, *, enabled: bool) -> dict[str, Any]:
        for ligne in self.lignes:
            if ligne["id"] == key_id:
                ligne["enabled"] = enabled
                return {k: v for k, v in ligne.items() if k != "key_hash"}
        raise LookupError(key_id)

    async def delete(self, key_id: int) -> None:
        self.lignes = [ligne for ligne in self.lignes if ligne["id"] != key_id]

    async def authenticate(self, secret: str) -> dict[str, Any] | None:
        try:
            prefix = extract_prefix(secret)
        except InvalidApiKeyError:
            return None
        for ligne in self.lignes:
            if ligne["prefix"] != prefix or not matches(secret, ligne["key_hash"]):
                continue
            if not ligne["enabled"]:
                return None
            if ligne["expires_at"] and ligne["expires_at"] <= datetime.now(tz=UTC):
                return None
            ligne["last_used_at"] = datetime.now(tz=UTC)
            return {k: v for k, v in ligne.items() if k != "key_hash"}
        return None


class DepotModele:
    def __init__(self) -> None:
        self.objets: dict[str, dict[str, dict[str, Any]]] = {
            "accounts": {},
            "packages": {},
            "sites": {},
            "access_points": {},
        }
        self.services: dict[str, dict[str, Any]] = {}
        self.manuels: set[str] = set()

    async def list_objects(self, collection: str) -> list[dict[str, Any]]:
        return list(self.objets[collection].values())

    async def get_object(self, collection: str, object_id: str) -> dict[str, Any]:
        try:
            return self.objets[collection][object_id]
        except KeyError as exc:
            raise ModelNotFoundError(f"{collection}/{object_id}") from exc

    async def _put(self, collection: str, object_id: str, fiche: dict[str, Any]) -> dict[str, Any]:
        self.objets[collection][object_id] = fiche
        return fiche

    async def put_account(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put(
            "accounts", object_id, {"id": object_id, "name": payload.get("name") or object_id}
        )

    async def put_package(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put(
            "packages",
            object_id,
            {
                "id": object_id,
                "name": payload.get("name") or object_id,
                "down_speed": payload.get("down_speed"),
                "up_speed": payload.get("up_speed"),
            },
        )

    async def put_site(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put(
            "sites", object_id, {"id": object_id, "name": payload.get("name") or object_id}
        )

    async def put_access_point(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._put(
            "access_points",
            object_id,
            {
                "id": object_id,
                "name": payload.get("name") or object_id,
                "tower": payload.get("tower"),
                "ip_address": payload.get("ip_address"),
            },
        )

    async def delete_object(self, collection: str, object_id: str) -> None:
        if object_id not in self.objets[collection]:
            raise ModelNotFoundError(f"{collection}/{object_id}")
        del self.objets[collection][object_id]

    async def list_services(self) -> list[dict[str, Any]]:
        return list(self.services.values())

    async def get_service(self, service_id: str) -> dict[str, Any]:
        try:
            return self.services[service_id]
        except KeyError as exc:
            raise ModelNotFoundError(f"services/{service_id}") from exc

    async def put_service(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if service_id in self.manuels:
            raise ModelConflictError(f"'{service_id}' est une fiche saisie a la main")
        prefixes, mac = parse_attachments(payload.get("attachments"))
        if not prefixes:
            raise ModelValidationError("un service doit porter au moins un prefixe reseau")
        descendant = kbps_to_mbps(payload.get("down_speed"))
        montant = kbps_to_mbps(payload.get("up_speed"))
        forfait = payload.get("package")
        if forfait in self.objets["packages"]:
            offre = self.objets["packages"][forfait]
            descendant = (
                descendant if descendant is not None else kbps_to_mbps(offre.get("down_speed"))
            )
            montant = montant if montant is not None else kbps_to_mbps(offre.get("up_speed"))
        fiche = {
            "id": service_id,
            "name": payload.get("name"),
            "pop_name": payload.get("pop_name") or "non-affecte",
            "account": payload.get("account"),
            "package": forfait,
            "parent_device_id": payload.get("parent_device_id"),
            "down_speed": mbps_to_kbps(descendant),
            "up_speed": mbps_to_kbps(montant),
            "vlan": payload.get("vlan"),
            "enabled": payload.get("enabled", True),
            "source": "api",
            "attachments": [{"cpe_mac": mac, "network_prefixes": prefixes}],
        }
        self.services[service_id] = fiche
        return dict(fiche)

    async def delete_service(self, service_id: str) -> None:
        if service_id in self.manuels:
            raise ModelConflictError(f"'{service_id}' est une fiche saisie a la main")
        if service_id not in self.services:
            raise ModelNotFoundError(f"services/{service_id}")
        del self.services[service_id]


@pytest.fixture
def pieces(settings: Settings):
    cles = DepotCles()
    modele = DepotModele()
    container = build_container(settings, api_keys_repo=cles, model_repo=modele)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), cles, modele


async def _cle(depot: DepotCles, *, scopes: list[str]) -> str:
    _fiche, secret = await depot.create(name="test", scopes=scopes)
    return secret


@pytest.fixture
def ecriture(pieces):
    client, cles, modele = pieces
    reponse = client.post("/api/v1/api-keys", json={"name": "facturation", "scopes": ["write"]})
    assert reponse.status_code == 201
    return client, reponse.json()["secret"], cles, modele


def basic(secret: str) -> dict[str, str]:
    """La forme Preseem : la cle en nom d'utilisateur, mot de passe vide."""
    jeton = base64.b64encode(f"{secret}:".encode()).decode()
    return {"Authorization": f"Basic {jeton}"}


# =========================================================================
# 1. LES CLES
# =========================================================================


def test_le_secret_n_est_rendu_qu_a_la_creation(pieces) -> None:
    """UNE FUITE DE BASE NE DOIT PAS DONNER L'INVENTAIRE D'UN OPERATEUR.

    La base ne garde que l'empreinte. Le secret circule une fois, dans la
    reponse de creation, et n'est plus jamais relisible -- y compris par celui
    qui l'a creee.
    """
    client, cles, _ = pieces
    creation = client.post("/api/v1/api-keys", json={"name": "splynx"}).json()
    assert creation["secret"].startswith("fqos_")

    liste = client.get("/api/v1/api-keys").json()
    assert "secret" not in liste[0]
    assert "key_hash" not in liste[0]
    assert cles.lignes[0]["key_hash"] != creation["secret"]
    assert cles.lignes[0]["key_hash"] == hash_key(creation["secret"])


def test_une_cle_qui_ecrit_lit_forcement(pieces) -> None:
    client, _, _ = pieces
    fiche = client.post("/api/v1/api-keys", json={"name": "x", "scopes": ["write"]}).json()
    assert fiche["scopes"] == ["read", "write"]


def test_une_cle_tiree_est_toujours_relisible() -> None:
    """LE CORPS DE LA CLE NE DOIT JAMAIS CONTENIR LE SEPARATEUR.

    La cle s'ecrit ``fqos_<prefixe>_<secret>`` et se decoupe sur le tiret bas.
    Un corps tire dans l'alphabet base64url en contient un de temps en temps :
    la cle devenait alors indecoupable, donc refusee -- une sur quelques-unes,
    au hasard. C'est exactement le defaut qu'on ne reproduit jamais chez soi et
    qu'on decouvre chez un client, une semaine plus tard, sur une integration
    qui "marche presque toujours".

    Mille tirages suffisent a le faire tomber : la probabilite d'un tiret bas
    dans 32 caracteres base64url depasse trente pour cent.
    """
    for _ in range(1000):
        tiree = generate_key()
        assert extract_prefix(tiree.secret) == tiree.prefix
        assert matches(tiree.secret, tiree.key_hash)


def test_une_cle_mal_formee_est_refusee_avant_toute_requete() -> None:
    for mauvaise in ("", "abc", "fqos_", "fqos__secret", "autre_prefixe_secret"):
        with pytest.raises(InvalidApiKeyError):
            extract_prefix(mauvaise)


def test_une_portee_inconnue_est_ignoree() -> None:
    assert normalise_scopes(["admin"]) == ["read"]
    assert normalise_scopes([]) == ["read"]


def test_revoquer_une_cle_la_rend_inutilisable(ecriture) -> None:
    client, secret, cles, _ = ecriture
    assert client.get("/model/v1", headers=basic(secret)).status_code == 200
    client.delete(f"/api/v1/api-keys/{cles.lignes[0]['id']}")
    assert client.get("/model/v1", headers=basic(secret)).status_code == 401


def test_une_cle_desactivee_est_refusee_sans_le_dire(ecriture) -> None:
    """LE REFUS NE RENSEIGNE PAS CELUI QUI ESSAIE.

    Distinguer "cette cle n'existe pas" de "cette cle est desactivee" donnerait
    gratuitement un oracle a qui essaie des cles au hasard. Le journal, lui, dit
    laquelle.
    """
    client, secret, cles, _ = ecriture
    client.patch(f"/api/v1/api-keys/{cles.lignes[0]['id']}", json={"enabled": False})
    reponse = client.get("/model/v1", headers=basic(secret))
    assert reponse.status_code == 401
    assert "desactiv" in reponse.json()["detail"] or "invalide" in reponse.json()["detail"]


def test_une_cle_expiree_ne_passe_plus(pieces) -> None:
    client, cles, _ = pieces
    creation = client.post(
        "/api/v1/api-keys",
        json={
            "name": "temporaire",
            "expires_at": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
        },
    ).json()
    assert client.get("/model/v1", headers=basic(creation["secret"])).status_code == 401


# =========================================================================
# 2. LES TROIS FACONS DE PRESENTER LA CLE
# =========================================================================


def test_basic_avec_la_cle_en_utilisateur_est_la_forme_preseem(ecriture) -> None:
    """C'EST LE POINT ENTIER DE CE MODULE.

    Un integrateur qui parlait a Preseem change l'URL de base et la cle. S'il
    doit aussi changer son mode d'authentification, l'integration est a
    reecrire.
    """
    client, secret, _, _ = ecriture
    assert client.get("/model/v1", headers=basic(secret)).status_code == 200


def test_la_cle_passe_aussi_en_bearer_et_en_entete_dedie(ecriture) -> None:
    client, secret, _, _ = ecriture
    assert client.get("/model/v1", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    assert client.get("/model/v1", headers={"X-API-Key": secret}).status_code == 200


def test_une_cle_dans_le_mot_de_passe_passe_aussi(ecriture) -> None:
    """Un client qui inverse les deux champs ne doit pas se heurter a un 401
    muet : le cout d'accepter est nul, celui de refuser est une heure de
    support."""
    client, secret, _, _ = ecriture
    jeton = base64.b64encode(f":{secret}".encode()).decode()
    assert client.get("/model/v1", headers={"Authorization": f"Basic {jeton}"}).status_code == 200


def test_sans_cle_l_api_refuse_et_dit_comment_s_authentifier(pieces) -> None:
    client, _, _ = pieces
    reponse = client.get("/model/v1/services")
    assert reponse.status_code == 401
    # Sans ce defi, `curl -u cle:` abandonne au premier 401 sans rien presenter.
    assert reponse.headers["www-authenticate"].startswith("Basic")


def test_une_cle_lecture_ne_peut_pas_ecrire(pieces) -> None:
    """Le 403 est distinct du 401 a dessein : "je ne sais pas qui tu es" et
    "cette cle ne peut que lire" appellent deux gestes opposes."""
    client, _, _ = pieces
    secret = client.post("/api/v1/api-keys", json={"name": "lecture"}).json()["secret"]
    assert client.get("/model/v1/services", headers=basic(secret)).status_code == 200
    reponse = client.put(
        "/model/v1/services/s1",
        headers=basic(secret),
        json={"attachments": [{"network_prefixes": ["10.0.0.5"]}]},
    )
    assert reponse.status_code == 403


# =========================================================================
# 3. LE CONTRAT DU MODELE
# =========================================================================


def test_les_cinq_collections_de_preseem_existent(ecriture) -> None:
    client, secret, _, _ = ecriture
    corps = client.get("/model/v1", headers=basic(secret)).json()
    assert set(corps["collections"]) == {
        "accounts",
        "packages",
        "sites",
        "access_points",
        "services",
    }


def test_un_put_cree_puis_remplace_sans_rien_casser(ecriture) -> None:
    """LA FACTURATION RESYNCHRONISE. Elle doit pouvoir rejouer son inventaire
    entier sans se demander ce qui existe deja : c'est pour cela que
    l'identifiant est choisi par l'appelant et que la methode est PUT."""
    client, secret, _, _ = ecriture
    for _ in range(2):
        reponse = client.put(
            "/model/v1/accounts/cust-41", headers=basic(secret), json={"name": "Mairie de Vitre"}
        )
        assert reponse.status_code == 200
    assert len(client.get("/model/v1/accounts", headers=basic(secret)).json()) == 1


def test_un_identifiant_contradictoire_est_refuse(ecriture) -> None:
    """Deviner lequel des deux est le bon reviendrait a ecrire au hasard sur le
    reseau d'un operateur."""
    client, secret, _, _ = ecriture
    reponse = client.put(
        "/model/v1/accounts/cust-41", headers=basic(secret), json={"id": "cust-42", "name": "X"}
    )
    assert reponse.status_code == 400


def test_une_collection_inconnue_n_existe_pas(ecriture) -> None:
    client, secret, _, _ = ecriture
    assert client.get("/model/v1/devices", headers=basic(secret)).status_code == 422


def test_les_debits_sont_en_kbit_s_comme_chez_preseem(ecriture) -> None:
    """UNE UNITE QUI VOYAGE EST UNE UNITE QU'ON OUBLIE DE CONVERTIR.

    Preseem compte en kbit/s, le controleur en Mbit/s. La conversion se fait a
    la frontiere, et la relecture doit rendre exactement ce qui a ete ecrit.
    """
    client, secret, _, modele = ecriture
    client.put(
        "/model/v1/services/svc-1",
        headers=basic(secret),
        json={
            "account": "cust-41",
            "down_speed": 10_000,
            "up_speed": 2_000,
            "attachments": [{"cpe_mac": "00:10:0b:6e:4c:ff", "network_prefixes": ["12.12.12.12"]}],
        },
    )
    fiche = client.get("/model/v1/services/svc-1", headers=basic(secret)).json()
    assert (fiche["down_speed"], fiche["up_speed"]) == (10_000, 2_000)
    assert fiche["attachments"][0]["network_prefixes"] == ["12.12.12.12/32"]
    assert fiche["attachments"][0]["cpe_mac"] == "00:10:0B:6E:4C:FF"


def test_le_forfait_fournit_le_debit_quand_le_service_n_en_porte_pas(ecriture) -> None:
    """Et le service prime quand il en porte un : une derogation commerciale sur
    une ligne ne doit pas obliger a inventer un forfait pour elle seule."""
    client, secret, _, _ = ecriture
    client.put(
        "/model/v1/packages/pack-100",
        headers=basic(secret),
        json={"name": "100/20", "down_speed": 100_000, "up_speed": 20_000},
    )
    herite = client.put(
        "/model/v1/services/svc-2",
        headers=basic(secret),
        json={"package": "pack-100", "attachments": [{"network_prefixes": ["10.0.0.0/29"]}]},
    ).json()
    assert (herite["down_speed"], herite["up_speed"]) == (100_000, 20_000)

    derogation = client.put(
        "/model/v1/services/svc-3",
        headers=basic(secret),
        json={
            "package": "pack-100",
            "down_speed": 250_000,
            "attachments": [{"network_prefixes": ["10.0.1.0/29"]}],
        },
    ).json()
    assert derogation["down_speed"] == 250_000
    assert derogation["up_speed"] == 20_000


def test_un_service_sans_prefixe_est_refuse(ecriture) -> None:
    """Sans adresse, rien ne peut etre shape : accepter la fiche ferait croire a
    une ligne configuree."""
    client, secret, _, _ = ecriture
    reponse = client.put("/model/v1/services/svc-4", headers=basic(secret), json={"account": "a"})
    assert reponse.status_code == 400


def test_l_api_n_ecrase_jamais_une_fiche_saisie_a_la_main(ecriture) -> None:
    """LA SEULE REGLE DURE DE CE MODULE.

    Une integration qui reprend l'identifiant d'un client saisi par un humain
    recoit un 409, pas un ecrasement silencieux. L'exploitant a le dernier mot
    sur son reseau.
    """
    client, secret, _, modele = ecriture
    modele.manuels.add("mairie-vitre")
    reponse = client.put(
        "/model/v1/services/mairie-vitre",
        headers=basic(secret),
        json={"attachments": [{"network_prefixes": ["10.0.0.5"]}]},
    )
    assert reponse.status_code == 409
    assert (
        client.delete("/model/v1/services/mairie-vitre", headers=basic(secret)).status_code == 409
    )


def test_supprimer_un_objet_absent_repond_404(ecriture) -> None:
    client, secret, _, _ = ecriture
    assert client.delete("/model/v1/sites/inconnu", headers=basic(secret)).status_code == 404


def test_supprimer_rend_204_et_retire_la_fiche(ecriture) -> None:
    client, secret, _, _ = ecriture
    client.put("/model/v1/sites/tour-nord", headers=basic(secret), json={"name": "Tour Nord"})
    assert client.delete("/model/v1/sites/tour-nord", headers=basic(secret)).status_code == 204
    assert client.get("/model/v1/sites", headers=basic(secret)).json() == []


# =========================================================================
# 4. LES CONVERSIONS, ISOLEES
# =========================================================================


def test_les_attachements_acceptent_l_objet_seul_comme_la_liste() -> None:
    """Les deux formes circulent dans la nature selon l'integrateur. Imposer
    celle que notre lecteur prefere ferait echouer la moitie des synchros."""
    unique = parse_attachments({"cpe_mac": "aa:bb:cc:dd:ee:ff", "network_prefixes": ["10.0.0.5"]})
    liste = parse_attachments([{"cpe_mac": "AA:BB:CC:DD:EE:FF", "network_prefixes": ["10.0.0.5"]}])
    assert unique == liste == (["10.0.0.5/32"], "AA:BB:CC:DD:EE:FF")


def test_un_prefixe_invalide_est_refuse_explicitement() -> None:
    with pytest.raises(ModelValidationError):
        normalise_prefixes(["pas-une-adresse"])


def test_les_doublons_de_prefixes_sont_replies() -> None:
    assert normalise_prefixes(["10.0.0.0/29", "10.0.0.0/29"]) == ["10.0.0.0/29"]


def test_un_debit_nul_ou_negatif_vaut_absence() -> None:
    """Zero chez un integrateur veut dire "pas de limite", pas "zero bit par
    seconde" -- et poser une file a 0 couperait le client."""
    assert kbps_to_mbps(0) is None
    assert kbps_to_mbps(-5) is None
    assert kbps_to_mbps(None) is None
    assert mbps_to_kbps(None) is None


# =========================================================================
# 5. LA CONSOMMATION
# =========================================================================
class DepotConsommation:
    """Rend des octets deja agreges, comme le ferait la base."""

    def __init__(self) -> None:
        self.appels: list[dict[str, Any]] = []
        self.lignes: list[dict[str, Any]] = [
            {
                "id": "svc-1",
                "account": "cust-41",
                "package": "pack-100",
                "period_start": None,
                "down_bytes": 12_000_000,
                "up_bytes": 3_000_000,
            }
        ]

    async def usage(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.appels.append(kwargs)
        if kwargs.get("service_id") not in (None, "svc-1"):
            return []
        return list(self.lignes)


@pytest.fixture
def consommation(settings: Settings):
    cles = DepotCles()
    flux = DepotConsommation()
    container = build_container(settings, api_keys_repo=cles, flows_repo=flux)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    client = TestClient(app)
    secret = client.post("/api/v1/api-keys", json={"name": "portail"}).json()["secret"]
    return client, secret, flux


def test_la_consommation_est_rendue_en_octets(consommation) -> None:
    client, secret, _ = consommation
    corps = client.get("/usage/v1/services", headers=basic(secret)).json()
    assert corps["unit"] == "bytes"
    service = corps["services"][0]
    assert service["down_bytes"] == 12_000_000
    assert service["total_bytes"] == 15_000_000


def test_la_consommation_dit_d_ou_elle_vient(consommation) -> None:
    """SANS EXPORTEUR NETFLOW, ELLE REND DES ZEROS -- et le dit.

    Laisser croire a un reseau silencieux alors qu'on ne mesure rien est le
    genre d'erreur qui se decouvre a la facturation du mois suivant.
    """
    client, secret, _ = consommation
    corps = client.get("/usage/v1/services", headers=basic(secret)).json()
    assert corps["source"] == "netflow"
    assert corps["vantage"] in ("edge", "pop")


def test_un_seul_point_de_mesure_est_lu(consommation) -> None:
    """Le meme octet est exporte par le PoP et par la sortie internet : les
    additionner doublerait chaque facture."""
    client, secret, flux = consommation
    client.get("/usage/v1/services?vantage=pop", headers=basic(secret))
    assert flux.appels[-1]["vantage"] == "pop"


def test_le_decoupage_par_periode_est_transmis_tel_quel(consommation) -> None:
    client, secret, flux = consommation
    client.get("/usage/v1/services?bucket=month", headers=basic(secret))
    assert flux.appels[-1]["bucket"] == "month"
    client.get("/usage/v1/services?bucket=total", headers=basic(secret))
    assert flux.appels[-1]["bucket"] is None


def test_la_consommation_d_un_service_est_totalisee(consommation) -> None:
    client, secret, _ = consommation
    corps = client.get("/usage/v1/services/svc-1", headers=basic(secret)).json()
    assert corps["id"] == "svc-1"
    assert corps["down_bytes"] == 12_000_000
    assert corps["up_bytes"] == 3_000_000


def test_une_fenetre_a_l_envers_est_refusee(consommation) -> None:
    client, secret, _ = consommation
    reponse = client.get(
        "/usage/v1/services?start=2026-09-21T00:00:00Z&end=2026-09-20T00:00:00Z",
        headers=basic(secret),
    )
    assert reponse.status_code == 422


def test_la_consommation_exige_une_cle(consommation) -> None:
    client, _, _ = consommation
    assert client.get("/usage/v1/services").status_code == 401

"""Fiche d'un client : ce que le reseau dit, et ce qu'on y ajoute a la main.

- ``GET /clients/{login}/profile`` : les faits decouverts (type, adresse IP,
  site, derniere session ; pour un client a IP fixe, son adresse, son VLAN) et
  la fiche saisie (nom, MAC du CPE, contact, note).
- ``PUT /clients/{login}/profile`` : modifier la fiche saisie (Operator et
  au-dessus). La decouverte ne l'ecrase jamais.
- ``POST /clients`` : declarer a la main un client PPPoE qui ne s'est pas
  encore connecte (Administrator). La decouverte le rejoint par son login.
- ``GET /client-profiles`` : toutes les fiches, pour la liste.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur, noter
from app.db.client_profiles_repo import FIELDS, ClientProfilesRepository
from app.services.client_health import normaliser_mac

router = APIRouter(tags=["clients"])

LoginPath = Annotated[str, Path(min_length=1, max_length=128)]
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_LOGIN = re.compile(r"^[A-Za-z0-9._@:+-]{1,64}$")


class ProfileInput(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    cpe_mac: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    postal_address: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=1000)


class DeclaredClient(ProfileInput):
    login: str = Field(min_length=1, max_length=64)
    pop_name: str = Field(min_length=1, max_length=128)


def _repo(container: ContainerDep) -> ClientProfilesRepository:
    repo = getattr(container, "client_profiles_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not initialised"
        )
    return repo  # type: ignore[no-any-return]


def _nettoyer(payload: ProfileInput) -> dict[str, str | None]:
    """Champs vides -> None ; MAC normalisee ; email plausible."""
    champs: dict[str, str | None] = {}
    for nom in FIELDS:
        valeur = getattr(payload, nom)
        valeur = valeur.strip() if isinstance(valeur, str) else None
        champs[nom] = valeur or None
    if champs["cpe_mac"]:
        mac = normaliser_mac(champs["cpe_mac"])
        if mac is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CPE MAC: 12 hexadecimal digits, like 24:5A:4C:12:34:56",
            )
        champs["cpe_mac"] = mac
    if champs["email"] and not _EMAIL.match(champs["email"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid email")
    return champs


async def _faits(container: ContainerDep, login: str) -> dict[str, Any] | None:
    """Ce que la decouverte sait du client, ou None s'il est inconnu."""
    async with container.database.pool.acquire() as conn:
        ligne = await conn.fetchrow(
            """
            SELECT s.kind, host(s.last_ip) AS ip, s.last_seen, p.name AS pop_name
              FROM subscribers s LEFT JOIN pops p ON p.id = s.pop_id
             WHERE s.login = $1
            """,
            login,
        )
        if ligne is None:
            return None
        faits = dict(ligne)
        faits["static"] = None
        if faits["kind"] == "static":
            fixe = await conn.fetchrow(
                """
                SELECT id, host(address) AS ip, masklen(address) AS prefix, vlan,
                       pop_name, label, cpe_mac
                  FROM static_clients WHERE reference = $1
                """,
                login,
            )
            if fixe is not None:
                faits["static"] = dict(fixe)
                faits["ip"] = faits["ip"] or fixe["ip"]
    return faits


@router.get("/client-profiles", summary="Every client's hand-entered details")
async def list_profiles(container: ContainerDep) -> dict[str, Any]:
    fiches = await _repo(container).list_all()
    return {"profiles": {f["login"]: f for f in fiches}}


@router.get("/clients/{login}/profile", summary="A client: discovered facts and its details")
async def get_profile(login: LoginPath, container: ContainerDep) -> dict[str, Any]:
    faits = await _faits(container, login)
    if faits is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown client")
    from app.services.vlan_sites import present_site, router_pops, vlan_parents

    try:
        sites = list(await container.repository.pop_sites())
        present_site(faits, vlan_parents(sites, router_pops(container)))
    except Exception:  # noqa: BLE001
        pass
    fiche = await _repo(container).get(login)
    # D'ou vient le debit affiche : file, interface, NetFlow -- et pourquoi.
    collection = getattr(container, "collection", None)
    sources = getattr(collection, "measure_sources", {}) if collection is not None else {}
    faits["measure"] = sources.get(login)
    return {"login": login, "discovered": faits, "profile": fiche}


@router.put("/clients/{login}/profile", summary="Edit a client's details (name, CPE MAC, contact)")
async def put_profile(
    login: LoginPath, payload: ProfileInput, request: Request, container: ContainerDep
) -> dict[str, Any]:
    if await _faits(container, login) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown client")
    champs = _nettoyer(payload)
    avant = await _repo(container).get(login) or {}
    fiche = await _repo(container).save(login, champs, updated_by=acteur(request))
    changes = [f for f in FIELDS if (avant.get(f) or None) != champs[f]]
    if changes:
        await noter(
            container,
            request,
            login=login,
            action="details",
            detail="edited: " + ", ".join(c.replace("_", " ") for c in changes),
        )
    return {"login": login, "profile": fiche}


@router.post(
    "/clients",
    status_code=status.HTTP_201_CREATED,
    summary="Declare a PPPoE client by hand, before its first session",
)
async def declare_client(
    payload: DeclaredClient, request: Request, container: ContainerDep
) -> dict[str, Any]:
    login = payload.login.strip()
    if not _LOGIN.match(login):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="login: letters, digits and . _ @ : + - only, as on the router",
        )
    if await _faits(container, login) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"a client '{login}' already exists"
        )
    champs = _nettoyer(payload)
    pop_id = await container.directory.ensure_pop(payload.pop_name.strip(), None)
    await container.directory.ensure_subscriber(login, pop_id=pop_id)
    fiche = await _repo(container).save(login, champs, updated_by=acteur(request), declared=True)
    await noter(
        container,
        request,
        login=login,
        action="declared",
        detail=f"added by hand on {payload.pop_name.strip()}; joined at its first session",
    )
    return {"login": login, "profile": fiche}

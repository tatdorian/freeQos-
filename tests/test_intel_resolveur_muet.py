"""Un resolveur DNS muet ne fige ni l'enrichissement, ni le reste du controleur.

Constate sur le banc de bout en bout : un passage 'ip_intel' tenait plus de
quatre minutes (gethostbyaddr ignore socket.setdefaulttimeout), le controleur
se declarait "degrade", et les fils bloques l'etaient sur le pool COMMUN
d'asyncio, celui qui lit les routeurs.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

from app.services.intel import IntelService
from app.services.ipfinder import ReverseDns


class ResolveurMuet(ReverseDns):
    def __init__(self) -> None:
        super().__init__()
        self.relache = threading.Event()

    def lookup(self, address: str) -> str | None:
        self.relache.wait(30)
        return None


async def test_une_adresse_rend_son_verdict_malgre_un_resolveur_muet() -> None:
    muet = ResolveurMuet()
    service = IntelService(rdns_enabled=True, timeout_s=0.2, resolver=muet)
    try:
        debut = time.monotonic()
        verdict = await service.analyse("8.8.8.8")
        assert time.monotonic() - debut < 3
        assert verdict.hostname is None
        # Le catalogue a quand meme parle.
        assert verdict.verdict.service
    finally:
        muet.relache.set()


async def test_les_fils_figes_ne_privent_pas_le_pool_commun() -> None:
    muet = ResolveurMuet()
    service = IntelService(rdns_enabled=True, timeout_s=0.1, resolver=muet)
    try:
        await asyncio.gather(*(service.analyse(f"192.0.2.{i}") for i in range(1, 30)))
        # Le pool d'asyncio (celui de la lecture des routeurs) repond aussitot.
        debut = time.monotonic()
        assert await asyncio.to_thread(lambda: 42) == 42
        assert time.monotonic() - debut < 1
    finally:
        muet.relache.set()


def test_le_nom_inverse_ne_touche_plus_au_delai_global_des_sockets() -> None:
    socket.setdefaulttimeout(None)
    ReverseDns().lookup("127.0.0.1")
    assert socket.getdefaulttimeout() is None

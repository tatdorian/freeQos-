"""En-tetes de securite poses sur CHAQUE reponse.

POURQUOI ICI, ET PAS DANS UN PROXY
----------------------------------
Le controleur est souvent expose tel quel (``docker compose up``, port 8000),
sans nginx devant. Les protections du navigateur doivent donc venir de
l'application elle-meme : sinon elles dependent d'une configuration que
personne n'a ecrite.

CE QUE CHAQUE EN-TETE EMPECHE
-----------------------------
- ``Content-Security-Policy`` : la page n'execute QUE les scripts servis par le
  controleur. Un nom d'equipement ou un commentaire piege qui parviendrait a
  injecter du HTML ne peut pas lancer de script, ni envoyer la session ailleurs
  (``connect-src 'self'``). Les styles en ligne restent permis : l'interface
  en pose beaucoup, et un style ne vole rien.
- ``frame-ancestors 'none'`` / ``X-Frame-Options`` : la page ne peut pas etre
  chargee dans le cadre invisible d'un autre site qui ferait cliquer
  l'exploitant a son insu ("clickjacking").
- ``X-Content-Type-Options`` : un fichier n'est jamais interprete comme autre
  chose que ce qu'il declare etre.
- ``Referrer-Policy`` : les adresses internes de l'interface ne fuient pas vers
  les sites ouverts depuis elle.
- ``Permissions-Policy`` : camera, micro, geolocalisation... coupes ; la page
  n'en a aucun usage.
- ``Strict-Transport-Security`` : en HTTPS seulement, le navigateur refuse
  ensuite de revenir en HTTP clair.
- ``Cache-Control: no-store`` sur l'API : les reponses (comptes, adresses,
  inventaire) ne restent pas dans le cache disque d'un poste partage.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

#: La documentation interactive (/docs) charge Swagger UI depuis son CDN et
#: l'initialise par un script en ligne : elle a sa propre politique, plus
#: large, et le reste de l'interface garde la stricte.
CSP_DOCS = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "img-src 'self' data: https://fastapi.tiangolo.com",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)

PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

#: Chemins dont les reponses ne doivent jamais etre gardees en cache.
_SANS_CACHE = ("/api/", "/model/", "/usage/")


def _https(request: Request) -> bool:
    # Le schema reel, une fois les en-tetes du proxy de confiance appliques par
    # uvicorn (FORWARDED_ALLOW_IPS) : on ne croit pas un X-Forwarded-Proto brut.
    return request.url.scheme == "https"


def apply_security_headers(request: Request, response: Response) -> None:
    """Pose les en-tetes sans ecraser ceux qu'une route aurait choisis."""
    chemin = request.url.path
    entetes = response.headers
    csp = CSP_DOCS if chemin.startswith(("/docs", "/redoc")) else CSP
    entetes.setdefault("Content-Security-Policy", csp)
    entetes.setdefault("X-Content-Type-Options", "nosniff")
    entetes.setdefault("X-Frame-Options", "DENY")
    entetes.setdefault("Referrer-Policy", "same-origin")
    entetes.setdefault("Permissions-Policy", PERMISSIONS)
    entetes.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if _https(request):
        entetes.setdefault("Strict-Transport-Security", "max-age=31536000")
    if chemin.startswith(_SANS_CACHE) or chemin == "/":
        entetes.setdefault("Cache-Control", "no-store")

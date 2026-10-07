#!/bin/sh
# =============================================================================
# freeQoS -- installation automatique (et mise a jour) avec Docker.
#
#   Depuis le depot :            sudo ./install.sh
#   Sur un serveur vierge :      curl -fsSL https://raw.githubusercontent.com/tatdorian/freeQos-/HEAD/install.sh | sudo sh
#
# Ce que fait le script, sans rien demander :
#   1. installe Docker (et le plugin compose) s'il manque ;
#   2. recupere le code (ou le met a jour s'il est deja la) ;
#   3. ecrit un .env avec des secrets ALEATOIRES (mot de passe PostgreSQL) --
#      jamais le "changeme" d'exemple sur un serveur de production ;
#   4. choisit tout seul un miroir si Docker Hub est injoignable ;
#   5. construit et demarre la pile, attend qu'elle reponde, et donne l'adresse.
#
# Relancer le script = mettre a jour : le code est rafraichi, l'image
# reconstruite, les donnees et le .env conserves.
#
# Variables facultatives (a passer devant la commande) :
#   FREEQOS_DIR=/opt/freeqos        ou installer quand on n'est pas dans le depot
#   FREEQOS_REPO=<url git>          depot a cloner
#   FREEQOS_BRANCH=<branche>        branche (defaut : la branche principale du depot)
#   APP_PORT=8000  NETFLOW_PORT=2055
#   AIROS_USERNAME=... AIROS_PASSWORD=...
#       identifiants communs des antennes Ubiquiti : avec eux, chaque radio
#       decouverte est ajoutee et interrogee automatiquement.
# =============================================================================
set -eu

FREEQOS_DIR="${FREEQOS_DIR:-/opt/freeqos}"
FREEQOS_REPO="${FREEQOS_REPO:-https://github.com/tatdorian/freeQos-.git}"
FREEQOS_BRANCH="${FREEQOS_BRANCH:-}"
APP_PORT="${APP_PORT:-8000}"
NETFLOW_PORT="${NETFLOW_PORT:-2055}"
MIROIR_PYTHON="mirror.gcr.io/library/python:3.11-slim"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- privileges
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    die "Lancer en root (ou installer sudo)."
  fi
else
  SUDO=""
fi

# ------------------------------------------------------------------ docker
if ! command -v docker >/dev/null 2>&1; then
  info "Docker absent : installation (script officiel get.docker.com)"
  command -v curl >/dev/null 2>&1 || die "curl est requis pour installer Docker."
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
  ok "Docker installe"
fi
if ! $SUDO docker compose version >/dev/null 2>&1; then
  info "Plugin docker compose absent : installation"
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq docker-compose-plugin
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q docker-compose-plugin
  else
    die "Installer le plugin 'docker compose' puis relancer."
  fi
fi
ok "$($SUDO docker compose version | head -1)"

# ------------------------------------------------------------------- code
if [ -f docker-compose.yml ] && [ -f Dockerfile ] && [ -d app ]; then
  DIR="$(pwd)"
  info "Depot trouve dans $DIR"
  if [ -d .git ] && command -v git >/dev/null 2>&1; then
    git pull --ff-only >/dev/null 2>&1 && ok "Code a jour" || warn "git pull impossible : on garde le code present"
  fi
else
  DIR="$FREEQOS_DIR"
  if ! command -v git >/dev/null 2>&1; then
    info "git absent : installation"
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git
    elif command -v dnf >/dev/null 2>&1; then
      $SUDO dnf install -y -q git
    else
      die "Installer git puis relancer."
    fi
  fi
  if [ -d "$DIR/.git" ]; then
    info "Mise a jour de $DIR"
    $SUDO git -C "$DIR" pull --ff-only
  else
    info "Telechargement de freeQoS dans $DIR"
    # Sans branche precisee : la branche principale du depot, quel que soit son nom.
    if [ -n "$FREEQOS_BRANCH" ]; then
      $SUDO git clone --depth 1 --branch "$FREEQOS_BRANCH" "$FREEQOS_REPO" "$DIR"
    else
      $SUDO git clone --depth 1 "$FREEQOS_REPO" "$DIR"
    fi
  fi
  cd "$DIR"
fi

# -------------------------------------------------------------------- .env
aleatoire() {
  # 32 caracteres alphanumeriques, sans dependre d'openssl.
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 32
}
# Un volume de base existe deja sans .env : il a ete cree avec le mot de passe
# par defaut. En changer maintenant rendrait la base inaccessible.
base_existante() {
  $SUDO docker volume ls -q 2>/dev/null | grep -q "_pgdata$"
}

if [ ! -f .env ]; then
  if base_existante; then
    MDP_PG="changeme"
    warn "Base existante creee avec le mot de passe par defaut : il est conserve."
  else
    MDP_PG="$(aleatoire)"
  fi
  info "Creation de .env (secrets aleatoires)"
  umask 077
  cat > .env <<EOF
# Genere par install.sh le $(date -u +%Y-%m-%dT%H:%M:%SZ). Garder ce fichier :
# il contient le mot de passe de la base.
POSTGRES_PASSWORD=${MDP_PG}
APP_PORT=${APP_PORT}
NETFLOW_PORT=${NETFLOW_PORT}
AUTH_ENABLED=true
EOF
  if [ -n "${AIROS_USERNAME:-}" ] && [ -n "${AIROS_PASSWORD:-}" ]; then
    printf 'AIROS_USERNAME=%s\nAIROS_PASSWORD=%s\n' "$AIROS_USERNAME" "$AIROS_PASSWORD" >> .env
    ok "Identifiants airOS enregistres : les radios decouvertes seront interrogees seules"
  fi
  ok ".env cree"
else
  ok ".env present : conserve"
  if [ -n "${AIROS_USERNAME:-}" ] && [ -n "${AIROS_PASSWORD:-}" ] && ! grep -q '^AIROS_USERNAME=' .env; then
    printf 'AIROS_USERNAME=%s\nAIROS_PASSWORD=%s\n' "$AIROS_USERNAME" "$AIROS_PASSWORD" >> .env
    ok "Identifiants airOS ajoutes a .env"
  fi
fi

# -------------------------------------------------- acces a Docker Hub
if ! grep -q '^PYTHON_IMAGE=' .env; then
  if ! curl -s -m 8 -o /dev/null -I https://registry-1.docker.io/v2/ 2>/dev/null; then
    warn "Docker Hub injoignable : passage par le miroir $MIROIR_PYTHON"
    printf 'PYTHON_IMAGE=%s\n' "$MIROIR_PYTHON" >> .env
  fi
fi

# --------------------------------------------------------------- demarrage
info "Construction et demarrage (premiere fois : quelques minutes)"
if ! $SUDO docker compose up -d --build; then
  if ! grep -q '^PYTHON_IMAGE=' .env; then
    warn "Echec de construction : nouvel essai via le miroir $MIROIR_PYTHON"
    printf 'PYTHON_IMAGE=%s\n' "$MIROIR_PYTHON" >> .env
    $SUDO docker compose up -d --build || die "La construction echoue. Voir : docker compose logs"
  else
    die "La construction echoue. Voir : docker compose logs"
  fi
fi

PORT="$(grep '^APP_PORT=' .env | cut -d= -f2)"
PORT="${PORT:-8000}"
info "Attente de l'application sur le port $PORT"
i=0
until curl -fs -m 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 90 ]; then
    warn "Pas de reponse apres 3 minutes. Journaux :"
    $SUDO docker compose logs --tail 40 app
    exit 1
  fi
  sleep 2
done
ok "freeQoS repond"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-<adresse-du-serveur>}"
NF="$(grep '^NETFLOW_PORT=' .env | cut -d= -f2)"
cat <<EOF

  freeQoS est installe.

  Interface  : http://${IP}:${PORT}/
               (premiere visite : creer le compte administrateur)
  NetFlow    : UDP ${NF:-2055} -- pose tout seul sur les routeurs ajoutes
  Dossier    : $(pwd)

  Ensuite, dans l'interface : Devices > ajouter vos routeurs. Le reste se fait
  seul : files CAKE, export NetFlow, decouverte du reseau, nature des liens
  (filaire ou radio)${AIROS_USERNAME:+ et interrogation des antennes}.

  Mettre a jour : relancer ce script (donnees conservees).
EOF

#!/usr/bin/env bash
# tgarr installer - Linux, and the Linux inside Windows (WSL2) and macOS (Colima).
#
#   curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.sh | bash
#
# What it does, and nothing else: makes a folder, puts tgarr's two files in it
# (docker-compose.yml and .env, with two secrets it makes up), starts the containers and
# waits until the web page answers. Run again in the same place it brings the compose file
# up to date and starts what is not running; your .env and your data are left as they are.
#
# On a Mac it first sets up Colima (a small Linux machine with Docker in it, through
# Homebrew) and then runs itself inside that. On Windows, install.ps1 does the same with WSL2.
#
# Settings, all optional, as environment variables:
#   TGARR_DIR=/path          where to install            (default: ~/tgarr)
#   TGARR_PORT=8765          port of the web page
#   TGARR_DOWNLOADS=/path    where fetched files are kept (default: inside TGARR_DIR)
#   TGARR_VERSION=0.5.11     stay on one version          (default: follow new releases)
#   TGARR_INSTALL_DOCKER=1   install Docker without asking when it is missing (Linux)
#   TGARR_GPU=no             do not hand this machine's graphics card to tgarr
#   TGARR_REF=main           which branch or tag the two files are taken from
set -euo pipefail

REF="${TGARR_REF:-main}"
RAW="${TGARR_RAW:-https://raw.githubusercontent.com/tgarrpro/tgarr/$REF}"
PORT="${TGARR_PORT:-8765}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mtgarr install:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

case "$PORT" in ''|*[!0-9]*) die "TGARR_PORT must be a number, not '$PORT'";; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "TGARR_PORT must be between 1 and 65535"

OS="$(uname -s)"
WSL=""
if [ "$OS" = "Linux" ] && grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then WSL=1; fi

# ---------------------------------------------------------------------------------------------
# macOS: Colima, then this same script inside it
# ---------------------------------------------------------------------------------------------
if [ "$OS" = "Darwin" ]; then
  say "macOS: tgarr runs in Colima, a small Linux machine with Docker in it"
  have brew || die "Homebrew is needed to install Colima. Install it from https://brew.sh and run this again."
  for f in colima docker; do
    have "$f" || { say "installing $f (Homebrew)"; brew install "$f"; }
  done
  if ! colima status >/dev/null 2>&1; then
    say "starting Colima (2 processors, 4 GB of memory, 100 GB of disk at most)"
    colima start --cpu "${TGARR_COLIMA_CPU:-2}" --memory "${TGARR_COLIMA_MEMORY:-4}" --disk "${TGARR_COLIMA_DISK:-100}"
  fi
  # started again whenever you log in
  brew services start colima >/dev/null 2>&1 || note "(could not register Colima to start at login: 'brew services start colima' does it)"
  DL="${TGARR_DOWNLOADS:-$HOME/tgarr/downloads}"
  mkdir -p "$DL"
  say "installing tgarr inside Colima (its database stays on the Linux side; fetched files go to $DL)"
  colima ssh -- env TGARR_DIR="${TGARR_VM_DIR:-/opt/tgarr}" TGARR_PORT="$PORT" TGARR_DOWNLOADS="$DL" \
      TGARR_VERSION="${TGARR_VERSION:-}" TGARR_REF="$REF" TGARR_RAW="$RAW" TGARR_INSIDE=colima \
      sh -c "curl -fsSL '$RAW/install/install.sh' | bash"
  echo
  say "tgarr is at http://localhost:$PORT"
  note "fetched files:  $DL"
  note "logs:           docker logs -f tgarr-api"
  note "stop / start:   colima stop   /   colima start"
  note "its folder:     colima ssh -- ls ${TGARR_VM_DIR:-/opt/tgarr}"
  note "remove it:      colima ssh -- sh -c 'cd ${TGARR_VM_DIR:-/opt/tgarr} && sudo docker compose down' ; colima delete"
  exit 0
fi

[ "$OS" = "Linux" ] || die "this installer is for Linux, macOS and Windows (through install.ps1); '$OS' is none of them"

# ---------------------------------------------------------------------------------------------
# Linux (also: inside WSL2, inside Colima)
# ---------------------------------------------------------------------------------------------
SUDO=""
if [ "$(id -u)" != 0 ]; then have sudo && SUDO="sudo" || true; fi
have curl || die "curl is needed (for example: ${SUDO:+sudo }apt-get install -y curl)"

ask() {   # ask "question" -> 0 for yes. Only when somebody is there to answer.
  { : > /dev/tty; } 2>/dev/null || return 1          # (a terminal that is listed but cannot be opened is nobody)
  printf '%s [y/N] ' "$1" > /dev/tty
  local a; read -r a < /dev/tty || return 1
  case "$a" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

if ! have docker; then
  if [ -n "${TGARR_INSTALL_DOCKER:-}" ] || [ -n "$WSL" ] || ask "Docker is not installed. Install it now (Docker's own script, get.docker.com)?"; then
    say "installing Docker"
    curl -fsSL https://get.docker.com | $SUDO sh
  else
    die "Docker is needed. Install it (https://docs.docker.com/engine/install/) and run this again, or run it with TGARR_INSTALL_DOCKER=1."
  fi
fi

# is the daemon running? (in WSL without systemd nothing starts it)
DOCKER="docker"
docker_up() { $DOCKER info >/dev/null 2>&1; }
if ! docker_up; then
  if [ -n "$SUDO" ] && sudo -n true 2>/dev/null && sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    say "starting Docker"
    if have systemctl && systemctl is-system-running >/dev/null 2>&1 || [ -d /run/systemd/system ]; then
      $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    else
      $SUDO service docker start >/dev/null 2>&1 || true
    fi
    n=0; until docker_up || { [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; } || [ $n -ge 30 ]; do sleep 1; n=$((n+1)); done
    docker_up || { [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1 && DOCKER="$SUDO docker"; } || die "Docker is installed but does not answer. Is its service running?  ($SUDO systemctl status docker)"
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || die "Docker's compose plugin is missing (docker compose version). Install docker-compose-plugin and run this again."

DIR="${TGARR_DIR:-$HOME/tgarr}"
if [ ! -d "$DIR" ]; then
  mkdir -p "$DIR" 2>/dev/null || { $SUDO mkdir -p "$DIR" && $SUDO chown "$(id -u):$(id -g)" "$DIR"; }
fi
[ -w "$DIR" ] || die "cannot write to $DIR"
cd "$DIR"

secret() {  # so many bytes, as hex
  if have openssl; then openssl rand -hex "$1"; else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
setenv() {  # setenv KEY VALUE: set it in .env whether the line is there, commented out, or missing
  local k="$1" v="$2" esc
  esc=$(printf '%s' "$v" | sed -e 's/[\\&|]/\\&/g')
  if grep -qE "^#?${k}=" .env; then
    sed -i -e "0,/^#\{0,1\}${k}=.*/s||${k}=${esc}|" .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

FRESH=""
if [ -f docker-compose.yml ] && [ -f .env ]; then
  say "tgarr is already in $DIR: bringing its compose file up to date (your .env and data are kept)"
  cp -p docker-compose.yml "docker-compose.yml.before-$(date +%Y%m%d-%H%M%S)"
else
  say "installing tgarr in $DIR"
  FRESH=1
fi
curl -fsSL "$RAW/docker-compose.yml" -o docker-compose.yml.new
grep -q '^name: tgarr' docker-compose.yml.new || { rm -f docker-compose.yml.new; die "what came from $RAW is not tgarr's compose file"; }
mv docker-compose.yml.new docker-compose.yml

if [ -n "$FRESH" ]; then
  [ -f .env ] || curl -fsSL "$RAW/.env.example" -o .env
  chmod 600 .env
  grep -qE '^DB_PASSWORD=.+' .env      || setenv DB_PASSWORD "$(secret 24)"
  grep -qE '^MEILI_MASTER_KEY=.+' .env || setenv MEILI_MASTER_KEY "$(secret 32)"
  [ "$PORT" = 8765 ] || setenv TGARR_PORT "$PORT"
  [ -z "${TGARR_DOWNLOADS:-}" ] || { mkdir -p "$TGARR_DOWNLOADS" 2>/dev/null || true; setenv TGARR_DOWNLOADS "$TGARR_DOWNLOADS"; }
  [ -z "${TGARR_VERSION:-}" ]   || setenv TGARR_TAG "$TGARR_VERSION"
else
  PORT="$(sed -n 's/^TGARR_PORT=\([0-9][0-9]*\).*/\1/p' .env | head -1)"; PORT="${PORT:-8765}"
fi

# The graphics card. tgarr converts films on it when it is given one (far lighter on the
# processor than doing it in software). Docker hands a card over only if told to, and refuses
# to start a container that names a card which is not there - so it is not in the compose
# file everybody gets, but in docker-compose.override.yml, written here for this machine.
# An override file of your own is never touched.
gpu_lines=""
if [ "${TGARR_GPU:-}" != no ]; then
  if ls /dev/dri/renderD* >/dev/null 2>&1; then
    gpu_lines="$gpu_lines    devices:
      - /dev/dri:/dev/dri
"
  fi
  if $DOCKER info 2>/dev/null | grep -qiE 'Runtimes:.*nvidia' && have nvidia-smi && nvidia-smi >/dev/null 2>&1; then
    gpu_lines="$gpu_lines    environment:
      NVIDIA_DRIVER_CAPABILITIES: compute,video,utility
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
"
  fi
fi
if [ -n "$gpu_lines" ]; then
  if [ ! -e docker-compose.override.yml ] || grep -q '^# written by tgarr.s installer' docker-compose.override.yml; then
    { echo "# written by tgarr's installer for this machine: its graphics card, handed to tgarr."
      echo "# (films are then converted on the card; Settings says whether it is in use)"
      echo "services:"
      echo "  api:"
      printf '%s' "$gpu_lines"; } > docker-compose.override.yml
    say "this machine's graphics card is handed to tgarr (docker-compose.override.yml)"
  else
    note "(this machine has a graphics card tgarr could convert films on, but there is a docker-compose.override.yml of your own:"
    note " see docs/install/gpu.md for the lines to add to it)"
  fi
fi

say "fetching tgarr (about 600 MB the first time)"
$DOCKER compose pull -q
say "starting"
$DOCKER compose up -d

say "waiting for the web page"
n=0
until curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/" || [ $n -ge 90 ]; do sleep 2; n=$((n+1)); done
if ! curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/"; then
  $DOCKER compose ps || true
  die "tgarr was started but its page does not answer on port $PORT yet. Look at:  cd $DIR && $DOCKER compose logs --tail 50 api"
fi

[ -n "${TGARR_INSIDE:-}" ] && exit 0          # the Mac or Windows side says the rest
ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "tgarr is running"
note "here:               http://localhost:$PORT"
[ -z "$ADDR" ] || [ -n "$WSL" ] || note "from other devices: http://$ADDR:$PORT"
note "its folder:         $DIR   (docker-compose.yml, .env, _data)"
note "logs:               cd $DIR && $DOCKER compose logs -f api"
note "stop / start:       cd $DIR && $DOCKER compose stop   /   $DOCKER compose up -d"
note "It keeps itself up to date. Nothing needs an account to begin with: open the page."

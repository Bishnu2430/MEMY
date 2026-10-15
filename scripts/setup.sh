#!/usr/bin/env bash
# Memy setup for macOS and Linux: checks/installs tools, creates .env with generated secrets, builds and starts
# the stack, and waits until it is healthy. Safe to re-run: existing .env values are kept.
#
#   ./scripts/setup.sh                       # interactive
#   ./scripts/setup.sh --groq-key gsk_... -y  # non-interactive
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GROQ_KEY="${GROQ_API_KEY:-}"
ELEVEN_KEY="${ELEVENLABS_API_KEY:-}"
INSTALL=0; START=1; REBUILD=0; YES=0
WEB_URL="http://localhost:8080"

usage() {
  cat <<EOF
Usage: ./scripts/setup.sh [options]
  --groq-key KEY         Groq API key (required for agents; or set GROQ_API_KEY)
  --elevenlabs-key KEY   ElevenLabs API key (optional; or set ELEVENLABS_API_KEY)
  --install              Install missing tools (Docker, uv, git) without asking
  --no-start             Only prepare .env; do not build or start containers
  --rebuild              Rebuild all images from scratch (no cache)
  -y, --yes              Answer yes to every prompt
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --groq-key) GROQ_KEY="$2"; shift 2 ;;
    --elevenlabs-key) ELEVEN_KEY="$2"; shift 2 ;;
    --install) INSTALL=1; shift ;;
    --no-start) START=0; shift ;;
    --rebuild) REBUILD=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

# ---------- output helpers ----------
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'; else B=; G=; Y=; R=; D=; N=; fi
step() { echo; echo "${B}==> $*${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
fail() { echo "  ${R}✗ $*${N}"; exit 1; }
confirm() {  # confirm "question" -> 0 if yes
  [[ $YES -eq 1 || $INSTALL -eq 1 ]] && return 0
  [[ -t 0 ]] || return 1
  read -r -p "  $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]
}
have() { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"
case "$OS" in
  Linux*)  PLATFORM=linux ;;
  Darwin*) PLATFORM=mac ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
  *) PLATFORM=other ;;
esac
if [[ $PLATFORM == windows ]]; then
  warn "You are on Windows (Git Bash). Prefer: powershell -ExecutionPolicy Bypass -File scripts\\setup.ps1"
fi

# ---------- 1. tools ----------
step "1/5  Checking required tools"

install_docker() {
  case "$PLATFORM" in
    mac)
      if have brew; then brew install --cask docker; else fail "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ (or install Homebrew first)."; fi ;;
    linux)
      echo "  Installing Docker Engine with the official convenience script (https://get.docker.com)…"
      curl -fsSL https://get.docker.com | sh
      if have sudo && [[ $(id -u) -ne 0 ]]; then sudo usermod -aG docker "$USER" || true
        warn "Added $USER to the docker group. If docker commands fail with 'permission denied', log out and back in."; fi ;;
    *) fail "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and re-run." ;;
  esac
}

install_uv() {
  if [[ $PLATFORM == mac ]] && have brew; then brew install uv; else curl -LsSf https://astral.sh/uv/install.sh | sh; fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

if have docker; then ok "Docker $(docker --version | sed 's/Docker version //; s/,.*//')"
else
  warn "Docker is not installed (required)."
  if confirm "Install Docker now?"; then install_docker; else fail "Docker is required: https://docs.docker.com/get-docker/"; fi
fi

if docker compose version >/dev/null 2>&1; then ok "Docker Compose $(docker compose version --short 2>/dev/null || echo v2)"
else fail "Docker Compose v2 is required (bundled with Docker Desktop; on Linux install the docker-compose-plugin package)."; fi

if have git; then ok "git $(git --version | awk '{print $3}')"; else warn "git not found (only needed to pull updates)."; fi

if have uv; then ok "uv $(uv --version | awk '{print $2}') (terminal client and evals)"
else
  warn "uv not found (optional: used by the terminal client and the eval scripts)."
  if confirm "Install uv now?"; then install_uv && ok "uv installed"; fi
fi

have curl || fail "curl is required."

# ---------- 2. docker daemon ----------
step "2/5  Making sure Docker is running"
if ! docker info >/dev/null 2>&1; then
  case "$PLATFORM" in
    mac) open -a Docker || true ;;
    linux) (have systemctl && sudo systemctl start docker) || true ;;
  esac
  echo -n "  waiting for the Docker engine"
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; echo -n "."; sleep 3; done; echo
fi
docker info >/dev/null 2>&1 || fail "Docker is not running. Start Docker Desktop (or 'sudo systemctl start docker') and re-run."
MEM=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
MEM_GB=$(( MEM / 1024 / 1024 / 1024 ))
ok "Docker engine is running (${MEM_GB} GB memory available to containers)"
[[ $MEM_GB -ge 4 ]] || warn "Less than 4 GB of memory for Docker; increase it in Docker Desktop › Settings › Resources."
for port in 8080 8000; do
  if (echo >/dev/tcp/127.0.0.1/$port) >/dev/null 2>&1 && ! docker compose ps --format '{{.Ports}}' 2>/dev/null | grep -q ":$port->"; then
    warn "Port $port is already in use by another program; the web UI (8080) or API (8000) may fail to start."
  fi
done

# ---------- 3. .env ----------
step "3/5  Preparing configuration (.env)"
rand() { if have openssl; then openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-24}"; else LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; fi; }
b64key() { if have openssl; then openssl rand -base64 32; else head -c 32 /dev/urandom | base64; fi; }

[[ -f .env ]] || { cp .env.example .env; ok "created .env from .env.example"; }
get_env() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
set_env() {  # set_env KEY VALUE  (replace or append, without printing the value)
  local key="$1" val="$2" tmp; tmp="$(mktemp)"
  if grep -qE "^$key=" .env; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env >"$tmp" && mv "$tmp" .env
  else printf '%s=%s\n' "$key" "$val" >>.env; rm -f "$tmp"; fi
}
fill() { [[ -n "$(get_env "$1")" ]] || { set_env "$1" "$2"; echo "  ${D}generated $1${N}"; }; }

fill POSTGRES_PASSWORD "$(rand 24)"
fill APP_DB_PASSWORD "$(rand 24)"
fill JWT_SECRET "$(rand 48)"
fill MASTER_KEY "$(b64key)"
fill SEED_ADMIN_EMAIL "admin@memy.local"; fill SEED_ADMIN_PASSWORD "$(rand 16)"
fill SEED_ALICE_EMAIL "alice@memy.local"; fill SEED_ALICE_PASSWORD "$(rand 16)"
fill SEED_BOB_EMAIL "bob@memy.local";     fill SEED_BOB_PASSWORD "$(rand 16)"

if [[ -n "$GROQ_KEY" ]]; then set_env GROQ_API_KEY "$GROQ_KEY"; fi
if [[ -z "$(get_env GROQ_API_KEY)" && -t 0 && $YES -eq 0 ]]; then
  echo "  A Groq API key is required for agents (free at https://console.groq.com/keys)."
  read -r -s -p "  Paste GROQ_API_KEY (input hidden, Enter to skip): " k; echo
  [[ -n "$k" ]] && set_env GROQ_API_KEY "$k"
fi
if [[ -n "$ELEVEN_KEY" ]]; then set_env ELEVENLABS_API_KEY "$ELEVEN_KEY"; fi
if [[ -z "$(get_env ELEVENLABS_API_KEY)" && -t 0 && $YES -eq 0 ]]; then
  read -r -s -p "  Paste ELEVENLABS_API_KEY (optional, input hidden, Enter to skip): " k; echo
  [[ -n "$k" ]] && set_env ELEVENLABS_API_KEY "$k"
fi
chmod 600 .env 2>/dev/null || true
[[ -n "$(get_env GROQ_API_KEY)" ]] && ok "Groq key configured" || warn "No GROQ_API_KEY yet: the app starts, but agents cannot run until you add it to .env."
[[ -n "$(get_env ELEVENLABS_API_KEY)" ]] && ok "ElevenLabs key configured" || warn "No ELEVENLABS_API_KEY: media features stay disabled (optional)."

if [[ $START -eq 0 ]]; then step "Done (.env prepared, containers not started)"; exit 0; fi

# ---------- 4. build & start ----------
step "4/5  Building and starting the stack (first run downloads ~3 GB and takes a few minutes)"
if [[ $REBUILD -eq 1 ]]; then docker compose build --no-cache; fi
docker compose up -d --build

# ---------- 5. health ----------
step "5/5  Waiting for the platform to become healthy"
for i in $(seq 1 120); do
  if curl -fsS "$WEB_URL/api/health" >/dev/null 2>&1; then break; fi
  [[ $i -eq 120 ]] && { docker compose ps; fail "Timed out. Inspect logs with: docker compose logs api"; }
  sleep 5
done
ok "All services are up"
docker compose ps --format '  {{.Name}}\t{{.Status}}'

cat <<EOF

${B}Memy is running:${N} ${WEB_URL}
  Sign in as     $(get_env SEED_ADMIN_EMAIL)   (password: SEED_ADMIN_PASSWORD in .env)
  Stop           docker compose down
  Logs           docker compose logs -f api orchestrator worker
  Verify         uv run evals/run_all.py
  Terminal       uv run cli/hub.py login
EOF

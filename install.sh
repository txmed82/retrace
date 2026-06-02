#!/usr/bin/env bash
set -euo pipefail

# Retrace Installer
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/txmed82/retrace/master/install.sh | bash
#   ./install.sh --docker
#   ./install.sh --local

REPO_URL="https://github.com/txmed82/retrace"
INSTALL_DIR="${INSTALL_DIR:-$HOME/retrace}"
MODE="${1:-local}"  # local | docker

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[retrace]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[retrace]${NC} $1"; }
log_error() { echo -e "${RED}[retrace]${NC} $1"; }

check_cmd() {
  if ! command -v "$1" &>/dev/null; then
    return 1
  fi
  return 0
}

install_uv() {
  log_info "uv not found. Installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Add to PATH for this session
  export PATH="$HOME/.cargo/bin:$PATH"
  if ! check_cmd uv; then
    log_error "Failed to install uv. Please install manually: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
  log_info "uv installed successfully."
}

local_install() {
  log_info "Local install mode"

  if ! check_cmd git; then
    log_error "git is required but not installed."
    exit 1
  fi

  if ! check_cmd uv; then
    install_uv
  fi

  if [ -d "$INSTALL_DIR" ]; then
    log_warn "$INSTALL_DIR already exists. Updating..."
    cd "$INSTALL_DIR"
    git pull origin master
  else
    log_info "Cloning retrace into $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi

  log_info "Creating virtual environment..."
  uv venv

  log_info "Installing retrace + dev dependencies..."
  uv pip install -e ".[dev]"

  log_info "Validating installation..."
  if ! .venv/bin/retrace --version &>/dev/null; then
    log_error "Installation validation failed."
    exit 1
  fi

  log_info "✅ Installed successfully."
  echo ""
  echo "Next steps:"
  echo "  cd $INSTALL_DIR"
  echo "  source .venv/bin/activate"
  echo "  retrace quickstart"
  echo ""
  echo "Or run the demo:"
  echo "  retrace demo seed"
  echo "  retrace ui"
}

docker_install() {
  log_info "Docker install mode"

  if ! check_cmd git; then
    log_error "git is required but not installed."
    exit 1
  fi

  if ! check_cmd docker; then
    log_error "docker is required but not installed."
    exit 1
  fi

  if ! docker compose version &>/dev/null; then
    log_error "docker compose is required but not installed."
    exit 1
  fi

  if [ -d "$INSTALL_DIR" ]; then
    log_warn "$INSTALL_DIR already exists. Updating..."
    cd "$INSTALL_DIR"
    git pull origin master
  else
    log_info "Cloning retrace into $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi

  log_info "Starting services..."
  docker compose up -d

  log_info "✅ Retrace is running."
  echo ""
  echo "Services:"
  echo "  API:    http://127.0.0.1:8788"
  echo "  UI:     http://127.0.0.1:8787"
  echo ""
  echo "Next steps:"
  echo "  cd $INSTALL_DIR"
  echo "  docker compose logs -f api"
  echo ""
  echo "Generate an SDK key:"
  echo "  docker compose exec api retrace api create-sdk-key --project Web --environment production"
}

# --- Main ---

case "$MODE" in
  --local|-l|local)
    local_install
    ;;
  --docker|-d|docker)
    docker_install
    ;;
  *)
    echo "Usage: $0 [--local|--docker]"
    echo ""
    echo "  --local   Install retrace locally with uv (default)"
    echo "  --docker  Install and run retrace via Docker Compose"
    exit 1
    ;;
esac

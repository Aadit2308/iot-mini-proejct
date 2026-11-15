#!/usr/bin/env bash
# Install everything the project needs inside WSL2 Ubuntu.
#
#   bash scripts/install_wsl_deps.sh            # everything
#   bash scripts/install_wsl_deps.sh --no-cooja # skip Java/Contiki-NG
#
# Steps:
#   1. apt packages (compiler, cmake, Python venv/pip, SSH/TLS libs, Java)
#   2. Build libyang -> libnetconf2 -> sysrepo -> netopeer2 from source
#      (CESNET GitHub, master branch = latest mutually compatible releases)
#   3. Clone Contiki-NG (with Cooja submodule) into ~/contiki-ng
#   4. Create the project Python venv (.venv) and install requirements
set -euo pipefail

WITH_COOJA=1
[[ "${1:-}" == "--no-cooja" ]] && WITH_COOJA=0

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${HOME}/netconf-src"
JOBS="$(nproc)"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

log "1/4 Installing apt packages (sudo password required)"
sudo apt-get update
sudo apt-get install -y \
    build-essential cmake pkg-config git curl \
    python3-dev python3-pip python3-venv libffi-dev \
    libpcre2-dev libssh-dev libssl-dev libmbedtls-dev libcurl4-openssl-dev \
    libpam0g-dev libsystemd-dev zlib1g-dev openssh-client

if [[ $WITH_COOJA -eq 1 ]]; then
    # Cooja (Contiki-NG) needs JDK 25 (its Gradle toolchain); fall back to default.
    sudo apt-get install -y openjdk-25-jdk || sudo apt-get install -y default-jdk
fi

log "2/4 Building libyang, libnetconf2, sysrepo, netopeer2 into /usr/local"
mkdir -p "$SRC_DIR"
build_repo() {
    local name="$1"; shift
    cd "$SRC_DIR"
    if [[ ! -d "$name" ]]; then
        git clone --depth 1 "https://github.com/CESNET/${name}.git"
    fi
    mkdir -p "$name/build" && cd "$name/build"
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local "$@" ..
    make -j"$JOBS"
    sudo make install
    sudo ldconfig
}
build_repo libyang
build_repo libnetconf2
build_repo sysrepo
# netopeer2's install step also installs its own YANG modules into sysrepo
# and generates the SSH host key / default listen config (port 830).
build_repo netopeer2

if [[ $WITH_COOJA -eq 1 ]]; then
    log "3/4 Cloning Contiki-NG into ~/contiki-ng"
    if [[ ! -d "${HOME}/contiki-ng" ]]; then
        git clone --recursive https://github.com/contiki-ng/contiki-ng.git "${HOME}/contiki-ng"
    else
        echo "~/contiki-ng already exists - leaving it untouched"
    fi
else
    log "3/4 Skipping Contiki-NG/Cooja"
fi

log "4/4 Creating project venv at ${PROJECT_DIR}/.venv"
cd "$PROJECT_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-netconf.txt

log "Versions installed"
yanglint --version || true
sysrepoctl --version || true
netopeer2-server -V || true
.venv/bin/pyang --version
.venv/bin/python -c "import sysrepo, libyang; print('sysrepo-python OK')"

log "Done. Next: bash scripts/setup_netconf.sh"

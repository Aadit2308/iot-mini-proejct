#!/usr/bin/env bash
# One-time NETCONF setup (after install_wsl_deps.sh):
#   1. install the irrigation-monitoring YANG module into sysrepo
#   2. start netopeer2-server (port 830)
#   3. create an SSH key and authorise it, so ncclient can log in as you
#      without a password (netopeer2 checks ~/.ssh/authorized_keys)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="$(pwd)/.venv/bin/python"

sudo "$PY" -m netconf.netconf_server install
sudo "$PY" -m netconf.netconf_server start

mkdir -p ~/.ssh && chmod 700 ~/.ssh
[[ -f ~/.ssh/id_ed25519 ]] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qF "$(cat ~/.ssh/id_ed25519.pub)" ~/.ssh/authorized_keys || cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys

"$PY" -m netconf.netconf_server status
echo
echo "Test the connection:  .venv/bin/python -m netconf.netconf_client capabilities | grep irrigation"
echo "If key login is refused, use your WSL password:  NETCONF_PASSWORD='...' or --ask-password"

#!/usr/bin/env bash
# Start the full stack in WSL (background processes, PIDs in runtime/):
#   netopeer2-server, NETCONF adapter, controller, dashboard.
# The simulator is started from the dashboard (or manually).
#
#   bash scripts/start_all.sh            # controller reads the Python simulator (UDP)
#   bash scripts/start_all.sh serial     # controller reads Cooja Serial Socket :60001
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="$(pwd)/.venv/bin/python"
SOURCE="${1:-udp}"
mkdir -p runtime logs

sudo "$PY" -m netconf.netconf_server start

if ! pgrep -f "netconf.adapter" >/dev/null; then
    sudo nohup "$PY" -m netconf.adapter > logs/adapter_stdout.log 2>&1 &
    sleep 1
    echo "adapter started (log: logs/netconf_adapter.log)"
fi

if [[ ! -f runtime/controller.pid ]] || ! kill -0 "$(cat runtime/controller.pid)" 2>/dev/null; then
    nohup "$PY" -m controller.main --source "$SOURCE" --quiet > logs/controller_stdout.log 2>&1 &
    echo $! > runtime/controller.pid
    echo "controller started (source=$SOURCE, pid $(cat runtime/controller.pid))"
fi

if [[ ! -f runtime/dashboard.pid ]] || ! kill -0 "$(cat runtime/dashboard.pid)" 2>/dev/null; then
    nohup "$PY" -m streamlit run dashboard/app.py --server.headless true --server.port 8501 \
        > logs/dashboard_stdout.log 2>&1 &
    echo $! > runtime/dashboard.pid
    echo "dashboard: http://localhost:8501"
fi

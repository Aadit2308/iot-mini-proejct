#!/usr/bin/env bash
# Stop everything started by start_all.sh (controller forces the pump OFF on exit).
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="$(pwd)/.venv/bin/python"
for name in dashboard controller; do
    if [[ -f runtime/$name.pid ]]; then
        kill "$(cat runtime/$name.pid)" 2>/dev/null && echo "stopped $name"
        rm -f runtime/$name.pid
    fi
done
pkill -f "simulator.sensor_simulator" 2>/dev/null && echo "stopped simulator"
sudo pkill -f "netconf.adapter" 2>/dev/null && echo "stopped adapter"
sudo "$PY" -m netconf.netconf_server stop

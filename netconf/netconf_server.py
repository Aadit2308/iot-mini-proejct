"""Manage the NETCONF server side (Netopeer2 + sysrepo) for this project.

The NETCONF server itself is Netopeer2 (C, built on libnetconf2/sysrepo/
libyang). This helper only drives the official CLI tools:

    sudo .venv/bin/python -m netconf.netconf_server install   # sysrepoctl -i
    sudo .venv/bin/python -m netconf.netconf_server start     # netopeer2-server
         .venv/bin/python -m netconf.netconf_server status
    sudo .venv/bin/python -m netconf.netconf_server stop
    sudo .venv/bin/python -m netconf.netconf_server uninstall

Linux/WSL only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YANG_DIR = ROOT / "yang"
YANG_FILE = YANG_DIR / "irrigation-monitoring.yang"
MODULE = "irrigation-monitoring"
NETCONF_PORT = 830


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.stdout.strip():
        print(res.stdout.rstrip())
    if res.stderr.strip():
        print(res.stderr.rstrip(), file=sys.stderr)
    if check and res.returncode != 0:
        raise SystemExit(f"command failed ({res.returncode}): {' '.join(cmd)}")
    return res


def _require(tool: str) -> str:
    path = shutil.which(tool) or (f"/usr/local/bin/{tool}" if Path(f"/usr/local/bin/{tool}").exists() else None)
    if not path:
        raise SystemExit(f"'{tool}' not found. Run scripts/install_wsl_deps.sh first.")
    return path


def _require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise SystemExit(f"'{action}' needs root: run with sudo")


def module_installed() -> bool:
    res = subprocess.run([_require("sysrepoctl"), "-l"], text=True, capture_output=True)
    return any(line.split("|")[0].strip() == MODULE for line in res.stdout.splitlines())


def server_pids() -> list[int]:
    res = subprocess.run(["pgrep", "-x", "netopeer2-server"], text=True, capture_output=True)
    return [int(p) for p in res.stdout.split()]


def port_open(host: str = "127.0.0.1", port: int = NETCONF_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def install() -> None:
    _require_root("install")
    sysrepoctl = _require("sysrepoctl")
    if module_installed():
        print(f"{MODULE} already installed - updating to the current file")
        _run([sysrepoctl, "-U", str(YANG_FILE), "-s", str(YANG_DIR)])
    else:
        _run([sysrepoctl, "-i", str(YANG_FILE), "-s", str(YANG_DIR), "-v", "2"])
    _run([sysrepoctl, "-l"], check=False)


def uninstall() -> None:
    _require_root("uninstall")
    _run([_require("sysrepoctl"), "-u", MODULE])


def start() -> None:
    _require_root("start")
    if server_pids():
        print("netopeer2-server already running, pid(s):", server_pids())
        return
    # netopeer2-server daemonises itself; -v1 = errors+warnings to syslog/stderr
    _run([_require("netopeer2-server"), "-v", "1"])
    for _ in range(20):
        if port_open():
            print(f"netopeer2-server listening on port {NETCONF_PORT}")
            return
        time.sleep(0.25)
    raise SystemExit("netopeer2-server did not open port 830; run `sudo netopeer2-server -d -v2` to see errors")


def stop() -> None:
    _require_root("stop")
    pids = server_pids()
    if not pids:
        print("netopeer2-server not running")
        return
    _run(["kill", *map(str, pids)], check=False)
    print("stopped", pids)


def status() -> int:
    ok = True
    for tool in ("netopeer2-server", "sysrepoctl", "yanglint"):
        path = shutil.which(tool)
        print(f"{tool:18} {'OK ' + path if path else 'MISSING'}")
        ok &= bool(path)
    if not ok:
        return 1
    try:
        installed = module_installed()
    except Exception as exc:  # permission problems etc.
        installed = False
        print("module check failed:", exc)
    pids = server_pids()
    print(f"{'module ' + MODULE:18} {'installed' if installed else 'NOT installed'}")
    print(f"{'netopeer2-server':18} {'running pid ' + str(pids) if pids else 'NOT running'}")
    print(f"{'port 830':18} {'open' if port_open() else 'closed'}")
    return 0 if (installed and pids) else 1


def main(argv=None) -> int:
    if sys.platform != "linux":
        print("netconf_server.py manages Netopeer2 and only runs on Linux/WSL.", file=sys.stderr)
        return 1
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    args = p.parse_args(argv)
    if args.action == "status":
        return status()
    {"install": install, "uninstall": uninstall, "start": start, "stop": stop}[args.action]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

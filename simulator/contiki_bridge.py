"""Run the Contiki-NG sensor node (native target) and forward its readings.

    python -m simulator.contiki_bridge --scenario mixed

Builds and starts ``simulator/contiki/sensor_node.c`` for the Contiki-NG
``native`` target (inside WSL when run on Windows), then:

  * forwards every sensor line (``MOISTURE node=.. seq=.. value=..``) to the
    controller over UDP (default 127.0.0.1:5005), exactly like the Python
    simulator, so the controller needs no special mode;
  * mirrors the node's complete serial output (build log, boot banner,
    readings) to the console and to ``logs/contiki_node.log`` so the
    dashboard can show a live node console.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.sensor_manager import is_sensor_line  # noqa: E402
from simulator.sensor_simulator import SCENARIOS  # noqa: E402

NODE_LOG = PROJECT_ROOT / "logs" / "contiki_node.log"
IS_WINDOWS = os.name == "nt"


def wsl_prefix() -> list[str]:
    return ["wsl.exe", "--"] if IS_WINDOWS else []


def kill_node() -> None:
    """Terminating wsl.exe does not stop the Linux process, so kill it by name."""
    try:
        subprocess.run([*wsl_prefix(), "pkill", "-f", "sensor_node.native"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def scenario_number(value: str) -> int:
    if value.isdigit() and 0 <= int(value) <= 7:
        return int(value)
    if value in SCENARIOS:
        return SCENARIOS.index(value)
    raise argparse.ArgumentTypeError(f"scenario must be 0-7 or one of {', '.join(SCENARIOS)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", type=scenario_number, default=7, help="0-7 or name (default: mixed)")
    p.add_argument("--host", default="127.0.0.1", help="controller UDP host")
    p.add_argument("--port", type=int, default=5005, help="controller UDP port")
    args = p.parse_args(argv)

    NODE_LOG.parent.mkdir(exist_ok=True)
    log = open(NODE_LOG, "w", encoding="utf-8", buffering=1)  # fresh console per run
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def emit(text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{stamp}  {text}"
        print(line, flush=True)
        log.write(line + "\n")

    kill_node()  # a node left over from an earlier run would hold the terminal
    cmd = [*wsl_prefix(), "bash", "scripts/contiki.sh", "run-native", str(args.scenario)]
    emit(f"# bridge: scenario {args.scenario} ({SCENARIOS[args.scenario]}) -> udp://{args.host}:{args.port}")
    emit(f"# bridge: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)

    def shutdown(*_):
        proc.terminate()
        kill_node()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    forwarded = 0
    try:
        for raw in proc.stdout:
            text = raw.rstrip("\r\n").replace("\x1b[1;32m", "").replace("\x1b[1;31m", "").replace("\x1b[0m", "")
            if not text:
                continue
            if is_sensor_line(text):
                sock.sendto(text.encode(), (args.host, args.port))
                forwarded += 1
                emit(f"{text}   -> controller")
            else:
                emit(text)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        kill_node()
    code = proc.wait()
    emit(f"# bridge: node exited (code {code}) after forwarding {forwarded} readings")
    time.sleep(0.2)
    return code


if __name__ == "__main__":
    sys.exit(main())

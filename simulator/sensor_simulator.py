"""Soil-moisture sensor simulator (Python fallback for Cooja).

Sends one JSON reading per interval (default 5 s) over UDP to the controller:

    {"sensor_id": "node-1", "seq": 7, "moisture": 25.0,
     "timestamp": 1730000000.12, "boot_id": "a1b2c3"}

Scenarios (``--scenario``):
    normal        20, 25, 35, 40, 50 % repeating
    invalid       injects 120 %
    negative      injects -10 %
    missing       drops 4 consecutive messages (20 s gap > 15 s timeout)
    delayed       holds 3 messages back for 4 intervals (they arrive late)
    intermittent  random drops / delays (seeded, reproducible)
    malformed     injects a corrupted, unparsable message
    mixed         all of the above in sequence

Examples:
    python -m simulator.sensor_simulator --scenario normal
    python -m simulator.sensor_simulator --scenario missing --interval 5 --cycles 2
    python -m simulator.sensor_simulator --scenario invalid --stdout   # print only
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import random
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NORMAL_VALUES = [20.0, 25.0, 35.0, 40.0, 50.0]


@dataclass(frozen=True)
class Step:
    """What happens at one reporting tick."""
    kind: str                  # "send" | "drop" | "delay" | "malformed"
    value: float | None = None
    delay_intervals: float = 0.0  # for "delay": held back this many intervals


def send(v: float) -> Step:
    return Step("send", v)


def drop(v: float) -> Step:
    return Step("drop", v)


def delay(v: float, intervals: float) -> Step:
    return Step("delay", v, intervals)


MALFORMED = Step("malformed")


def build_scenario(name: str, seed: int = 42) -> list[Step]:
    """Return one cycle of steps for a named scenario."""
    if name == "normal":
        return [send(v) for v in NORMAL_VALUES]
    if name == "invalid":
        return [send(25), send(35), send(120), send(120), send(40), send(50), send(20)]
    if name == "negative":
        return [send(25), send(35), send(-10), send(40), send(50), send(20)]
    if name == "missing":
        # 4 dropped reports: 25 s between valid readings -> stale at 15 s
        return [send(25), send(35), drop(40), drop(50), drop(20), drop(25), send(40), send(50)]
    if name == "delayed":
        # first one slightly late (accepted with warning); then 3 held back
        # for 4 intervals -> sensor goes stale, late copies are discarded
        return [
            send(25), delay(35, 0.6), send(40),
            delay(50, 4), delay(20, 4), delay(25, 4),
            send(40), send(50), send(20),
        ]
    if name == "intermittent":
        rng = random.Random(seed)
        steps = []
        for i in range(15):
            v = NORMAL_VALUES[i % len(NORMAL_VALUES)]
            r = rng.random()
            if r < 0.30:
                steps.append(drop(v))
            elif r < 0.40:
                steps.append(delay(v, rng.choice([0.6, 2, 4])))
            else:
                steps.append(send(v))
        return steps
    if name == "malformed":
        return [send(25), MALFORMED, send(40), send(50)]
    if name == "mixed":
        out: list[Step] = []
        for part in ("normal", "invalid", "negative", "malformed", "missing", "delayed"):
            out += build_scenario(part, seed)
        return out
    raise ValueError(f"unknown scenario {name!r}")


SCENARIOS = ["normal", "invalid", "negative", "missing", "delayed", "intermittent", "malformed", "mixed"]


class SensorSimulator:
    def __init__(
        self,
        scenario: str = "normal",
        interval: float = 5.0,
        host: str = "127.0.0.1",
        port: int = 5005,
        sensor_id: str = "node-1",
        stdout_only: bool = False,
        seed: int = 42,
        logger: logging.Logger | None = None,
    ):
        self.steps = build_scenario(scenario, seed)
        self.scenario = scenario
        self.interval = interval
        self.addr = (host, port)
        self.sensor_id = sensor_id
        self.stdout_only = stdout_only
        self.boot_id = uuid.uuid4().hex[:8]
        self.seq = 0
        self.log = logger or logging.getLogger("simulator")
        self.sock = None if stdout_only else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._pending: list[tuple[float, int, str]] = []  # (deliver_at, seq, payload)

    def _payload(self, value: float, created: float) -> str:
        return json.dumps({
            "sensor_id": self.sensor_id,
            "seq": self.seq,
            "moisture": value,
            "timestamp": round(created, 3),
            "boot_id": self.boot_id,
        })

    def _transmit(self, payload: str) -> None:
        if self.stdout_only:
            print(payload, flush=True)
        else:
            self.sock.sendto(payload.encode(), self.addr)

    def _flush_due(self, now: float) -> None:
        while self._pending and self._pending[0][0] <= now:
            _, seq, payload = heapq.heappop(self._pending)
            self._transmit(payload)
            self.log.info("DELIVERED-LATE seq=%d %s", seq, payload)

    def tick(self, step: Step, now: float) -> None:
        self.seq += 1
        if step.kind == "send":
            payload = self._payload(step.value, now)
            self._transmit(payload)
            self.log.info("SENT seq=%d value=%g", self.seq, step.value)
        elif step.kind == "drop":
            self.log.info("DROPPED seq=%d value=%g (simulated packet loss)", self.seq, step.value)
        elif step.kind == "delay":
            deliver_at = now + step.delay_intervals * self.interval
            heapq.heappush(self._pending, (deliver_at, self.seq, self._payload(step.value, now)))
            self.log.info("DELAYED seq=%d value=%g by %.1fs", self.seq, step.value,
                          step.delay_intervals * self.interval)
        elif step.kind == "malformed":
            payload = '{"sensor_id": "%s", "seq": %d, "moisture": "##ERR' % (self.sensor_id, self.seq)
            self._transmit(payload)
            self.log.info("SENT-MALFORMED seq=%d %s", self.seq, payload)

    def run(self, cycles: int | None = None, stop_event=None) -> None:
        self.log.info("simulator start scenario=%s interval=%.1fs target=%s boot_id=%s",
                      self.scenario, self.interval,
                      "stdout" if self.stdout_only else f"udp://{self.addr[0]}:{self.addr[1]}",
                      self.boot_id)
        cycle = 0
        next_tick = time.time()
        try:
            while cycles is None or cycle < cycles:
                for step in self.steps:
                    # sleep until next tick, delivering delayed messages on time
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            return
                        now = time.time()
                        self._flush_due(now)
                        if now >= next_tick:
                            break
                        wake = next_tick
                        if self._pending:
                            wake = min(wake, self._pending[0][0])
                        time.sleep(max(0.0, min(0.2, wake - now)))
                    self.tick(step, time.time())
                    next_tick += self.interval
                cycle += 1
            # let held-back messages arrive before exiting
            while self._pending:
                if stop_event is not None and stop_event.is_set():
                    return
                self._flush_due(time.time())
                time.sleep(0.1)
        finally:
            self.log.info("simulator stopped after %d cycle(s), last seq=%d", cycle, self.seq)
            if self.sock:
                self.sock.close()


def _setup_logger(log_dir: Path, console: bool) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("simulator")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d [SIM] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_dir / "simulator.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    logger.propagate = False
    return logger


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Soil moisture sensor simulator")
    p.add_argument("--scenario", choices=SCENARIOS, default="normal")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between readings")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--sensor-id", default="node-1")
    p.add_argument("--cycles", type=int, default=None, help="repeat scenario N times (default: forever)")
    p.add_argument("--seed", type=int, default=42, help="random seed for 'intermittent'")
    p.add_argument("--stdout", action="store_true", help="print messages instead of UDP")
    p.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "logs")
    args = p.parse_args(argv)

    logger = _setup_logger(args.log_dir, console=not args.stdout)
    sim = SensorSimulator(args.scenario, args.interval, args.host, args.port,
                          args.sensor_id, args.stdout, args.seed, logger)
    try:
        sim.run(args.cycles)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

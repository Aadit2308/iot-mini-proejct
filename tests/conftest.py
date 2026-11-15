"""Shared fixtures.

Tests drive the controller with an explicit simulated clock (``now=``) so
timeout tests are exact and fast; one test in test_timeout.py also checks the
real wall clock with time.sleep().
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller.config_manager import ConfigManager  # noqa: E402
from controller.logging_setup import setup_logging  # noqa: E402
from controller.main import IrrigationSystem  # noqa: E402
from controller.state_manager import StateManager  # noqa: E402

T0 = 1_800_000_000.0  # fixed simulated start time (epoch seconds)


def make_msg(value, seq, ts=None, sensor_id="node-1", boot_id="test-boot") -> str:
    body = {"sensor_id": sensor_id, "seq": seq, "moisture": value, "boot_id": boot_id}
    if ts is not None:
        body["timestamp"] = ts
    return json.dumps(body)


@pytest.fixture
def tmp_project(tmp_path):
    settings = tmp_path / "config" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "moisture_threshold_low": 30.0,
        "moisture_threshold_high": 45.0,
        "sensor_timeout": 15,
    }))
    setup_logging(tmp_path / "logs", console=False)
    return tmp_path


@pytest.fixture
def config(tmp_project):
    return ConfigManager(tmp_project / "config" / "settings.json")


@pytest.fixture
def system(config, tmp_project):
    return IrrigationSystem(
        config,
        StateManager(tmp_project / "runtime" / "state.json"),
        start_time=T0,
    )


class Feeder:
    """Sends readings into a system with a controllable clock."""

    def __init__(self, system: IrrigationSystem, start: float = T0):
        self.system = system
        self.now = start
        self.seq = 0

    def send(self, value, advance: float = 5.0, sent_offset: float = 0.0):
        """Advance the clock, then deliver a reading created ``sent_offset``
        seconds ago (i.e. network latency)."""
        self.now += advance
        self.seq += 1
        self.system.check_timeouts(self.now)
        verdict = self.system.process_raw(
            make_msg(value, self.seq, ts=self.now - sent_offset), self.now
        )
        return verdict

    def wait(self, seconds: float, step: float = 0.5):
        """Advance time with no messages, running the timeout check like the
        real loop does."""
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now + step)
            self.system.check_timeouts(self.now)

    @property
    def state(self):
        return self.system.st


@pytest.fixture
def feeder(system):
    return Feeder(system)

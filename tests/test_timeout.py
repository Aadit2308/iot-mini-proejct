"""Timeout / stale detection and recovery: Tests 5, 7."""

import time

import pytest

from controller.config_manager import ConfigManager
from controller.irrigation_control import PumpState, SensorStatus
from controller.main import IrrigationSystem
from controller.state_manager import ALERT_INVALID, ALERT_TIMEOUT, StateManager


# ---------------------------------------------------------------- Test 5
def test_05_no_messages_for_15s_goes_stale_pump_off(feeder):
    feeder.send(25)
    assert feeder.state.pump_state is PumpState.ON

    feeder.wait(14.5)
    assert feeder.state.sensor_status is SensorStatus.HEALTHY   # not yet
    assert feeder.state.pump_state is PumpState.ON

    feeder.wait(0.5)                                            # t = 15 s
    s = feeder.state
    assert s.sensor_status is SensorStatus.STALE
    assert s.pump_state is PumpState.OFF
    assert s.auto_control is False
    assert ALERT_TIMEOUT in s.alerts
    assert s.total_timeout_events == 1


def test_05_timeout_counted_once_per_episode(feeder):
    feeder.send(25)
    feeder.wait(60)
    assert feeder.state.total_timeout_events == 1


def test_05_timeout_without_any_reading_since_start(feeder):
    feeder.wait(15)
    assert feeder.state.sensor_status is SensorStatus.STALE
    assert feeder.state.pump_state is PumpState.OFF
    assert feeder.state.total_timeout_events == 1


def test_05_invalid_readings_do_not_reset_timeout(feeder):
    """Only VALID readings refresh the timer; a stream of 120 % goes stale."""
    feeder.send(25)
    for _ in range(3):
        feeder.send(120)                                        # t = 5,10,15 s
    assert feeder.state.sensor_status is SensorStatus.STALE
    assert feeder.state.total_timeout_events == 1
    assert feeder.state.pump_state is PumpState.OFF


def test_05_real_wall_clock_timeout(tmp_project):
    """Same rule with real time.time() and a 1 s timeout (no simulated clock)."""
    cfg = ConfigManager(tmp_project / "config" / "settings.json")
    cfg.update(sensor_timeout=1)
    sysm = IrrigationSystem(cfg, StateManager(None))
    sysm.process_raw('{"sensor_id": "n", "seq": 1, "moisture": 25}')
    assert sysm.st.pump_state is PumpState.ON
    time.sleep(1.2)
    sysm.check_timeouts()
    assert sysm.st.sensor_status is SensorStatus.STALE
    assert sysm.st.pump_state is PumpState.OFF


# ---------------------------------------------------------------- Test 7
def test_07_valid_readings_resume_auto_control(feeder):
    s = feeder.state
    feeder.send(25)
    feeder.wait(20)
    assert s.sensor_status is SensorStatus.STALE
    stale_since_valid = s.last_valid_time

    feeder.send(20, advance=0.0)
    assert s.sensor_status is SensorStatus.HEALTHY
    assert s.last_valid_time > stale_since_valid           # timestamp updated
    assert not s.alerts                                    # alerts cleared
    assert s.auto_control is True                          # control resumed
    assert s.pump_state is PumpState.ON                    # 20 % < 30 %
    assert s.total_recovery_events == 1
    categories = [e["category"] for e in s.recent_events]
    assert "RECOVERY" in categories
    assert "AUTO_CONTROL" in categories


def test_07_recovery_after_invalid(feeder):
    feeder.send(120)
    assert ALERT_INVALID in feeder.state.alerts
    feeder.send(50)
    assert feeder.state.sensor_status is SensorStatus.HEALTHY
    assert not feeder.state.alerts
    assert feeder.state.total_recovery_events == 1


def test_07_stale_stays_stale_on_invalid_until_valid(feeder):
    feeder.send(25)
    feeder.wait(16)
    feeder.send(120, advance=0)
    assert feeder.state.sensor_status is SensorStatus.STALE
    assert feeder.state.pump_state is PumpState.OFF
    feeder.send(25, advance=1)
    assert feeder.state.sensor_status is SensorStatus.HEALTHY


@pytest.mark.parametrize("timeout", [1, 10, 300])
def test_timeout_is_configurable(config, timeout):
    assert config.update(sensor_timeout=timeout).sensor_timeout == timeout

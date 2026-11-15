"""Irrigation rules: Tests 1, 2, 8, 9 plus safety-invariant checks."""

import itertools

import pytest

from controller.config_manager import ConfigValidationError, validate_config
from controller.irrigation_control import PumpState, SensorStatus, decide_pump_state


# ---------------------------------------------------------------- Test 1
def test_01_25_percent_healthy_pump_on(feeder):
    feeder.send(25)
    assert feeder.state.sensor_status is SensorStatus.HEALTHY
    assert feeder.state.pump_state is PumpState.ON
    assert feeder.state.auto_control is True
    assert feeder.state.soil_moisture == 25


# ---------------------------------------------------------------- Test 2
def test_02_50_percent_healthy_pump_off(feeder):
    feeder.send(25)                       # pump ON first, so OFF is a real change
    feeder.send(50)
    assert feeder.state.sensor_status is SensorStatus.HEALTHY
    assert feeder.state.pump_state is PumpState.OFF


# ---------------------------------------------------------------- Test 8
def test_08_35_percent_maintains_on(feeder):
    feeder.send(25)
    assert feeder.state.pump_state is PumpState.ON
    feeder.send(35)
    assert feeder.state.pump_state is PumpState.ON


def test_08_35_percent_maintains_off(feeder):
    feeder.send(50)
    assert feeder.state.pump_state is PumpState.OFF
    feeder.send(35)
    assert feeder.state.pump_state is PumpState.OFF


@pytest.mark.parametrize("value", [30.0, 45.0])
def test_08_boundaries_maintain_state(value):
    for prev in PumpState:
        d = decide_pump_state(SensorStatus.HEALTHY, value, prev, 30, 45)
        assert d.state is prev


# ---------------------------------------------------------------- Test 9
@pytest.mark.parametrize("low,high", [(45, 30), (40, 40), (50, 49.9)])
def test_09_high_not_greater_than_low_rejected(config, low, high):
    before = config.get()
    with pytest.raises(ConfigValidationError):
        config.update(moisture_threshold_low=low, moisture_threshold_high=high)
    assert config.get() == before              # nothing applied
    assert validate_config(before.to_dict()) == before


def test_09_partial_update_violating_rule_rejected(config):
    with pytest.raises(ConfigValidationError):
        config.update(moisture_threshold_low=60)   # 60 >= current high 45


@pytest.mark.parametrize("changes", [
    {"moisture_threshold_low": -1},
    {"moisture_threshold_high": 101},
    {"sensor_timeout": 0},
    {"sensor_timeout": 301},
    {"sensor_timeout": 10.5},
    {"sensor_timeout": "abc"},
    {"sensor_timeout": True},
    {"unknown_leaf": 1},
])
def test_invalid_config_values_rejected(config, changes):
    with pytest.raises(ConfigValidationError):
        config.update(**changes)


def test_valid_threshold_change_reevaluates_pump(feeder, config):
    feeder.send(35)                                # within 30-45: stays OFF
    assert feeder.state.pump_state is PumpState.OFF
    config.update(moisture_threshold_low=40, moisture_threshold_high=60)
    assert feeder.state.pump_state is PumpState.ON  # 35 < new low 40


# ---------------------------------------------------- safety invariant
@pytest.mark.parametrize(
    "status,moisture,prev",
    list(itertools.product(
        [SensorStatus.INVALID, SensorStatus.STALE],
        [-10.0, 0.0, 10.0, 25.0, 35.0, 50.0, 120.0, None],
        list(PumpState),
    )),
)
def test_invalid_or_stale_never_turns_pump_on(status, moisture, prev):
    d = decide_pump_state(status, moisture, prev, 30, 45)
    assert d.state is PumpState.OFF
    assert d.auto_control is False


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -10.0, 120.0])
def test_healthy_with_bad_value_still_off(bad):
    d = decide_pump_state(SensorStatus.HEALTHY, bad, PumpState.ON, 30, 45)
    assert d.state is PumpState.OFF

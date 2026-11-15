"""Reading validation: Tests 3, 4, 10 plus parser / malformed handling."""

import pytest

from controller.fault_detector import FaultDetector, MessageParseError, Verdict, parse_message
from controller.irrigation_control import PumpState, SensorStatus
from controller.state_manager import ALERT_INVALID, ALERT_MALFORMED, ALERT_TIMEOUT
from tests.conftest import make_msg


# ---------------------------------------------------------------- Test 3
def test_03_120_percent_invalid_alert_pump_off(feeder):
    feeder.send(25)
    assert feeder.state.pump_state is PumpState.ON     # was irrigating
    verdict = feeder.send(120)
    assert verdict is Verdict.INVALID
    assert feeder.state.sensor_status is SensorStatus.INVALID
    assert ALERT_INVALID in feeder.state.alerts
    assert feeder.system.state_mgr.fault_alert is True
    assert feeder.state.pump_state is PumpState.OFF
    assert feeder.state.auto_control is False
    assert feeder.state.total_invalid_readings == 1
    assert feeder.state.soil_moisture == 25            # invalid value not stored


# ---------------------------------------------------------------- Test 4
def test_04_negative_10_invalid_alert_pump_off(feeder):
    feeder.send(20)
    verdict = feeder.send(-10)
    assert verdict is Verdict.INVALID
    assert feeder.state.sensor_status is SensorStatus.INVALID
    assert ALERT_INVALID in feeder.state.alerts
    assert feeder.state.pump_state is PumpState.OFF
    assert "-10" in feeder.system.state_mgr.fault_description


def test_invalid_as_first_reading_never_starts_pump(feeder):
    feeder.send(120)
    assert feeder.state.pump_state is PumpState.OFF
    feeder.send(-10)
    assert feeder.state.pump_state is PumpState.OFF


@pytest.mark.parametrize("value", [0, 100, 0.0, 99.9])
def test_range_edges_are_valid(feeder, value):
    assert feeder.send(value) is Verdict.VALID


@pytest.mark.parametrize("value", [-0.1, 100.1, 1e9])
def test_just_outside_range_invalid(feeder, value):
    assert feeder.send(value) is Verdict.INVALID


# ------------------------------------------------------ malformed input
@pytest.mark.parametrize("raw", [
    "",
    "hello",
    "{not json",
    '{"seq": 1}',                                  # no moisture
    '{"moisture": "25"}',                          # string, not number
    '{"moisture": true}',
    '{"moisture": NaN}',
    '{"moisture": 25, "seq": -3}',
    "[1, 2, 3]",
    "MOISTURE node=1 seq=x value=25",
    b"\xff\xfe\x00",
])
def test_malformed_messages_rejected(raw):
    with pytest.raises(MessageParseError):
        parse_message(raw)


def test_malformed_message_raises_alert_and_pump_off(feeder):
    feeder.send(25)
    feeder.now += 5
    verdict = feeder.system.process_raw('{"moisture": "##ERR', feeder.now)
    assert verdict is Verdict.MALFORMED
    assert feeder.state.sensor_status is SensorStatus.INVALID
    assert ALERT_MALFORMED in feeder.state.alerts
    assert feeder.state.pump_state is PumpState.OFF
    assert feeder.state.total_invalid_readings == 1


def test_contiki_text_format_parsed():
    msg = parse_message("MOISTURE node=3 seq=12 value=25")
    assert (msg.sensor_id, msg.seq, msg.moisture) == ("node-3", 12, 25.0)
    assert parse_message("MOISTURE node=3 seq=13 value=-10").moisture == -10.0


def test_rejected_readings_are_logged(feeder, tmp_project):
    feeder.send(120)
    log = (tmp_project / "logs" / "irrigation.log").read_text()
    assert "[INVALID_READING]" in log and "120" in log


# ---------------------------------------------------------------- Test 10
def test_10_delayed_messages_detection_and_recovery(feeder):
    s = feeder.state
    feeder.send(25)
    assert s.pump_state is PumpState.ON

    # (a) slightly delayed (3 s latency) -> accepted, logged as delayed
    assert feeder.send(40, sent_offset=3.0) is Verdict.VALID
    assert s.total_delayed_messages == 1
    assert s.sensor_status is SensorStatus.HEALTHY

    # (b) network holds the next readings back: nothing arrives for 20 s
    held = []
    for value in (50, 20, 25):
        feeder.seq += 1
        held.append((value, feeder.seq, feeder.now + 5 * len(held) + 5))
    feeder.wait(20)
    assert s.sensor_status is SensorStatus.STALE          # timeout detected
    assert ALERT_TIMEOUT in s.alerts
    assert s.pump_state is PumpState.OFF

    # (c) a fresh reading arrives first -> recovery
    assert feeder.send(40, advance=0.0) is Verdict.VALID
    assert s.sensor_status is SensorStatus.HEALTHY
    assert not s.alerts
    assert s.total_recovery_events == 1

    # (d) the held-back readings finally arrive: older seq -> discarded,
    #     they must NOT change health or the pump (20 % would turn it ON!)
    for value, seq, created in held:
        feeder.now += 1
        verdict = feeder.system.process_raw(make_msg(value, seq, ts=created), feeder.now)
        assert verdict is Verdict.OUT_OF_ORDER
    assert s.pump_state is PumpState.OFF
    assert s.soil_moisture == 40
    assert s.sensor_status is SensorStatus.HEALTHY
    assert s.total_delayed_messages == 1 + 3


def test_10_reading_older_than_timeout_is_stale_data(feeder):
    feeder.send(50)
    # in-order seq, but created 20 s ago (> 15 s timeout)
    assert feeder.send(20, sent_offset=20.0) is Verdict.STALE_DATA
    assert feeder.state.pump_state is PumpState.OFF       # 20 % ignored
    assert feeder.state.soil_moisture == 50


def test_sequence_gap_counts_missing_messages(feeder):
    feeder.send(25)
    feeder.seq += 3                                        # 3 lost
    feeder.send(40)
    assert feeder.state.total_missing_messages == 3


def test_malformed_message_not_double_counted_as_missing(feeder):
    feeder.send(25)
    feeder.seq += 1
    feeder.system.process_raw('{"seq": %d, "moisture": "##ERR' % feeder.seq, feeder.now + 1)
    feeder.send(40)
    assert feeder.state.total_missing_messages == 0
    assert feeder.state.total_invalid_readings == 1


def test_sensor_reboot_resets_sequence():
    det = FaultDetector()
    assert det.evaluate(make_msg(25, 100, boot_id="a"), 0, 15).ok
    # rebooted sensor starts counting from 1 again with a new boot id
    assert det.evaluate(make_msg(25, 1, boot_id="b"), 1, 15).ok
    assert det.reboots_detected == 1

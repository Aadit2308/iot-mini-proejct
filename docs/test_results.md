# Test Results

All results below are **real output** captured on 2026-10-31. Nothing is
hand-edited except that progress percentages were stripped from the pytest
listing.

**Environment:** Windows 11, Python 3.13.0 pytest 9.2.0, project venv `.venv-windows`.
NETCONF live tests were **not** run yet because Netopeer2/sysrepo are not
installed in WSL (installing needs `sudo`; see README). They are reported
as *SKIPPED*, not passed.

## 1. Required test cases (observation table)

| # | Test case | Input / action | Expected | Observed | pytest function(s) | Result |
|---|---|---|---|---|---|---|
| 1 | Normal low moisture | 25 % | Healthy, pump ON | Healthy, pump ON, auto control active | `test_01_25_percent_healthy_pump_on` | ✅ PASS |
| 2 | Normal high moisture | 25 % then 50 % | Healthy, pump OFF | Healthy, pump OFF | `test_02_50_percent_healthy_pump_off` | ✅ PASS |
| 3 | Over-range reading | 120 % | Invalid, alert, pump OFF | Invalid, `invalid-reading` alert, pump OFF, 120 not stored | `test_03_120_percent_invalid_alert_pump_off` | ✅ PASS |
| 4 | Negative reading | −10 % | Invalid, alert, pump OFF | Invalid, alert, pump OFF | `test_04_negative_10_invalid_alert_pump_off` | ✅ PASS |
| 5 | Silence | no message for 15 s | Stale, pump OFF | Healthy at 14.5 s, **stale at 15.0 s**, pump OFF, 1 timeout event | `test_05_*` (5 tests incl. real wall-clock) | ✅ PASS |
| 6 | NETCONF timeout change | edit-config timeout 10 | Controller uses 10 s | *Local sync path:* controller stays healthy at 9.9 s and goes stale at 10.0 s ✅. *Live NETCONF:* not run yet | `test_06_local_settings_sync_applies_new_timeout` ✅ / `test_06_netconf_*` ⏭ | ⚠️ PARTIAL (live part pending) |
| 7 | Recovery | valid reading after stale | Healthy, auto control resumes | Healthy, alerts cleared, timestamp updated, auto control resumed, RECOVERY logged | `test_07_*` (3 tests) | ✅ PASS |
| 8 | Hysteresis | 35 % after ON / after OFF | Maintain previous state | ON stays ON, OFF stays OFF; 30 %/45 % boundaries also maintain | `test_08_*` (4 tests) | ✅ PASS |
| 9 | Bad thresholds | high ≤ low | Reject | ConfigValidationError, config unchanged (3 cases + partial update). YANG `must` present; live NETCONF rejection pending | `test_09_*` | ✅ PASS (Python) / ⏭ live NETCONF |
| 10 | Delayed messages | 3 s latency; 20 s hold-back | Detect, recover | 3 s: accepted + DELAYED logged. Held-back: stale at 15 s → recovery on fresh reading → late copies discarded as out-of-order; pump not driven by late 20 % | `test_10_*` (2 tests) | ✅ PASS |

## 2. Full pytest run

Command: `python -m pytest -v`

```text
tests/test_fault_detection.py::test_03_120_percent_invalid_alert_pump_off PASSED
tests/test_fault_detection.py::test_04_negative_10_invalid_alert_pump_off PASSED
tests/test_fault_detection.py::test_invalid_as_first_reading_never_starts_pump PASSED
tests/test_fault_detection.py::test_range_edges_are_valid[0] PASSED
tests/test_fault_detection.py::test_range_edges_are_valid[100] PASSED
tests/test_fault_detection.py::test_range_edges_are_valid[0.0] PASSED
tests/test_fault_detection.py::test_range_edges_are_valid[99.9] PASSED
tests/test_fault_detection.py::test_just_outside_range_invalid[-0.1] PASSED
tests/test_fault_detection.py::test_just_outside_range_invalid[100.1] PASSED
tests/test_fault_detection.py::test_just_outside_range_invalid[1000000000.0] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[hello] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{not json] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{"seq": 1}] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{"moisture": "25"}] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{"moisture": true}] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{"moisture": NaN}] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[{"moisture": 25, "seq": -3}] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[[1, 2, 3]] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[MOISTURE node=1 seq=x value=25] PASSED
tests/test_fault_detection.py::test_malformed_messages_rejected[\xff\xfe\x00] PASSED
tests/test_fault_detection.py::test_malformed_message_raises_alert_and_pump_off PASSED
tests/test_fault_detection.py::test_contiki_text_format_parsed PASSED
tests/test_fault_detection.py::test_rejected_readings_are_logged PASSED
tests/test_fault_detection.py::test_10_delayed_messages_detection_and_recovery PASSED
tests/test_fault_detection.py::test_10_reading_older_than_timeout_is_stale_data PASSED
tests/test_fault_detection.py::test_sequence_gap_counts_missing_messages PASSED
tests/test_fault_detection.py::test_malformed_message_not_double_counted_as_missing PASSED
tests/test_fault_detection.py::test_sensor_reboot_resets_sequence PASSED
tests/test_irrigation_control.py::test_01_25_percent_healthy_pump_on PASSED
tests/test_irrigation_control.py::test_02_50_percent_healthy_pump_off PASSED
tests/test_irrigation_control.py::test_08_35_percent_maintains_on PASSED
tests/test_irrigation_control.py::test_08_35_percent_maintains_off PASSED
tests/test_irrigation_control.py::test_08_boundaries_maintain_state[30.0] PASSED
tests/test_irrigation_control.py::test_08_boundaries_maintain_state[45.0] PASSED
tests/test_irrigation_control.py::test_09_high_not_greater_than_low_rejected[45-30] PASSED
tests/test_irrigation_control.py::test_09_high_not_greater_than_low_rejected[40-40] PASSED
tests/test_irrigation_control.py::test_09_high_not_greater_than_low_rejected[50-49.9] PASSED
tests/test_irrigation_control.py::test_09_partial_update_violating_rule_rejected PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes0] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes1] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes2] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes3] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes4] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes5] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes6] PASSED
tests/test_irrigation_control.py::test_invalid_config_values_rejected[changes7] PASSED
tests/test_irrigation_control.py::test_valid_threshold_change_reevaluates_pump PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid--10.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid--10.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-0.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-0.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-10.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-10.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-25.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-25.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-35.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-35.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-50.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-50.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-120.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-120.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-None-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[invalid-None-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale--10.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale--10.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-0.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-0.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-10.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-10.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-25.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-25.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-35.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-35.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-50.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-50.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-120.0-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-120.0-off] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-None-on] PASSED
tests/test_irrigation_control.py::test_invalid_or_stale_never_turns_pump_on[stale-None-off] PASSED
tests/test_irrigation_control.py::test_healthy_with_bad_value_still_off[None] PASSED
tests/test_irrigation_control.py::test_healthy_with_bad_value_still_off[nan] PASSED
tests/test_irrigation_control.py::test_healthy_with_bad_value_still_off[inf] PASSED
tests/test_irrigation_control.py::test_healthy_with_bad_value_still_off[-10.0] PASSED
tests/test_irrigation_control.py::test_healthy_with_bad_value_still_off[120.0] PASSED
tests/test_netconf.py::test_yang_model_validates_with_pyang PASSED
tests/test_netconf.py::test_yang_tree_contains_required_leaves PASSED
tests/test_netconf.py::test_yang_declares_threshold_must_rule PASSED
tests/test_netconf.py::test_edit_config_payload_is_namespaced PASSED
tests/test_netconf.py::test_parse_get_config_reply_with_defaults PASSED
tests/test_netconf.py::test_parse_get_reply PASSED
tests/test_netconf.py::test_leaf_name PASSED
tests/test_netconf.py::test_adapter_translates_edit_config_changes PASSED
tests/test_netconf.py::test_settings_from_sysrepo_data PASSED
tests/test_netconf.py::test_state_to_yang_from_live_controller PASSED
tests/test_netconf.py::test_state_to_yang_reports_safe_state_when_controller_down PASSED
tests/test_netconf.py::test_06_local_settings_sync_applies_new_timeout PASSED
tests/test_netconf.py::test_06_netconf_edit_config_timeout_10_applied_by_controller SKIPPED
tests/test_netconf.py::test_06_running_controller_reports_applied_timeout_via_get SKIPPED
tests/test_netconf.py::test_09_netconf_rejects_high_not_greater_than_low SKIPPED
tests/test_netconf.py::test_netconf_get_returns_operational_status SKIPPED
tests/test_timeout.py::test_05_no_messages_for_15s_goes_stale_pump_off PASSED
tests/test_timeout.py::test_05_timeout_counted_once_per_episode PASSED
tests/test_timeout.py::test_05_timeout_without_any_reading_since_start PASSED
tests/test_timeout.py::test_05_invalid_readings_do_not_reset_timeout PASSED
tests/test_timeout.py::test_05_real_wall_clock_timeout PASSED
tests/test_timeout.py::test_07_valid_readings_resume_auto_control PASSED
tests/test_timeout.py::test_07_recovery_after_invalid PASSED
tests/test_timeout.py::test_07_stale_stays_stale_on_invalid_until_valid PASSED
tests/test_timeout.py::test_timeout_is_configurable[1] PASSED
tests/test_timeout.py::test_timeout_is_configurable[10] PASSED
tests/test_timeout.py::test_timeout_is_configurable[300] PASSED
SKIPPED [1] tests\test_netconf.py:188: set NETCONF_TESTS=1 to run against a live netopeer2-server
SKIPPED [1] tests\test_netconf.py:217: set NETCONF_TESTS=1 to run against a live netopeer2-server
SKIPPED [1] tests\test_netconf.py:228: set NETCONF_TESTS=1 to run against a live netopeer2-server
SKIPPED [1] tests\test_netconf.py:239: set NETCONF_TESTS=1 to run against a live netopeer2-server
======================= 108 passed, 4 skipped in 3.01s ========================
```

## 3. YANG validation

```text
$ pyang --strict --lint yang/irrigation-monitoring.yang
(no output - zero errors, zero warnings)
```

`pyang 2.7.1`. Also covered by `test_yang_model_validates_with_pyang`.

## 4. Live end-to-end run (controller + simulator over UDP)

Real processes: `controller.main` (timeout temporarily 3 s) and
`sensor_simulator --scenario mixed --interval 1`. Excerpt of
`logs/irrigation.log` (VALID_READING lines omitted):

```text
19:55:47.485 [INFO   ] [PUMP_STATE_CHANGE] pump OFF -> ON (moisture 20% < low threshold 30%)
19:55:51.486 [INFO   ] [PUMP_STATE_CHANGE] pump ON -> OFF (moisture 50% > high threshold 45%)
19:55:54.487 [WARNING] [ALERT] raised invalid-reading: invalid reading 120% (moisture 120% outside 0-100%)
19:55:54.487 [INFO   ] [PUMP_STATE_CHANGE] pump ON -> OFF (sensor invalid: pump forced OFF)
19:55:54.490 [INFO   ] [AUTO_CONTROL] automatic control SUSPENDED (sensor invalid: pump forced OFF)
19:55:56.486 [INFO   ] [RECOVERY] sensor recovered: invalid -> healthy; cleared alerts: invalid-reading
19:56:01.486 [WARNING] [ALERT] raised invalid-reading: invalid reading -10% (moisture -10% outside 0-100%)
19:56:06.485 [WARNING] [MALFORMED_MESSAGE] rejected: invalid JSON: Unterminated string starting at
19:56:12.023 [WARNING] [MISSING_MESSAGE] no message received for 1.5s (expected every 1s)
19:56:13.551 [WARNING] [TIMEOUT] sensor stale: no valid reading for 3.1s (timeout 3s)
19:56:13.552 [INFO   ] [PUMP_STATE_CHANGE] pump ON -> OFF (sensor stale: pump forced OFF)
19:56:15.485 [WARNING] [MISSING_MESSAGE] 4 message(s) missing before seq=29
19:56:15.486 [INFO   ] [RECOVERY] sensor recovered: stale -> healthy; cleared alerts: communication-timeout
19:56:24.487 [WARNING] [DELAYED_MESSAGE] discarded 50% seq=34: seq 34 <= last accepted seq 38 (late or duplicate)
19:56:25.488 [WARNING] [DELAYED_MESSAGE] discarded 20% seq=35: seq 35 <= last accepted seq 39 (late or duplicate)
19:56:38.463 [INFO   ] [SYSTEM] controller stopped; pump forced OFF
```

Bug found in this run and fixed: a malformed message was also counted as one
"missing" message (its sequence number could not be read). The fix is
`malformed_since_valid` in `fault_detector.py`, with the regression test
`test_malformed_message_not_double_counted_as_missing`.

## 5. Contiki-NG node logic

`sensor_node.c` was compiled with MSVC 19.41 (`/W4 /std:c11`) against minimal
host stubs of `contiki.h`/`etimer`/`node-id`/`random` (only to check the C code
and its scenario logic; this is **not** a Contiki-NG build). The output of 39
ticks of the *mixed* scenario was piped into the controller
(`--source stdin`):

```text
[INVALID_READING] rejected 120% from node-1 seq=8: moisture 120% outside 0-100%
[INVALID_READING] rejected -10% from node-1 seq=14: moisture -10% outside 0-100%
[MALFORMED_MESSAGE] rejected: unrecognised message format: 'MOISTURE node=1 seq=19 value=ERR#'
[MISSING_MESSAGE] 4 message(s) missing before seq=28
[MISSING_MESSAGE] 3 message(s) missing before seq=35
[DELAYED_MESSAGE] discarded 50% seq=32: seq 32 <= last accepted seq 35 (late or duplicate)
[DELAYED_MESSAGE] discarded 20% seq=33: seq 33 <= last accepted seq 36 (late or duplicate)
[DELAYED_MESSAGE] discarded 25% seq=34: seq 34 <= last accepted seq 37 (late or duplicate)
state: total_valid_readings=29 total_invalid_readings=3 total_missing_messages=7 total_delayed_messages=3
```

Pending: `make TARGET=native` and a Cooja run with the real Contiki-NG tree.

## 6. Dashboard

Streamlit `AppTest` with a live controller and an *invalid* scenario simulation:
no exceptions. Metrics rendered from the controller state:

```text
Soil moisture 20 % | Sensor health ✅ Healthy | Pump 💧 ON | Automatic control ▶ Active
Low 30 % | High 45 % | Timeout 15 s | Last valid reading 20:06:05
Valid 9 | Invalid 4 | Timeouts 0 | Missing 0 | Delayed 0 | Recoveries 2
```

## 7. Pending verification (to be filled in after the WSL install)

| Item | Command |
|---|---|
| Live NETCONF tests (Test 6, Test 9 via server, `<get>`) | `NETCONF_TESTS=1 pytest -m netconf -v` |
| NETCONF demonstration | `python -m netconf.netconf_client demo` |
| Server-side YANG validation | `yanglint yang/irrigation-monitoring.yang` |
| Contiki-NG native build | `cd simulator/contiki && make TARGET=native` |
| Cooja simulation | see docs/demonstration.md §4 |

"""NETCONF-YANG tests.

Part A (always runs): YANG validation with pyang, edit-config payload
building, reply parsing, and the adapter's translation logic.

Part B (Test 6, genuine NETCONF): talks to a real netopeer2-server through
ncclient. It needs, in WSL:
    netopeer2-server running, irrigation-monitoring installed,
    netconf.adapter running, NETCONF credentials (NETCONF_USER / _PASSWORD
    or an SSH key). Enable with:  NETCONF_TESTS=1 pytest -m netconf
Without a server these tests are SKIPPED (reported, never faked).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from lxml import etree

from controller import DEFAULT_SETTINGS_PATH
from controller.config_manager import ConfigManager
from controller.irrigation_control import PumpState, SensorStatus
from controller.main import IrrigationSystem
from controller.state_manager import StateManager
from netconf import NAMESPACE
from netconf.adapter import changes_to_config, leaf_name, settings_from_yang, state_to_yang
from netconf.netconf_client import build_settings_config, parse_settings, parse_status

ROOT = Path(__file__).resolve().parent.parent
YANG = ROOT / "yang" / "irrigation-monitoring.yang"
CFG = {"moisture_threshold_low": 30.0, "moisture_threshold_high": 45.0, "sensor_timeout": 15}


# =========================================================== Part A: YANG
def _pyang() -> list[str] | None:
    exe = shutil.which("pyang") or str(Path(sys.executable).parent / ("pyang.exe" if os.name == "nt" else "pyang"))
    return [exe] if Path(exe).exists() else None


def test_yang_model_validates_with_pyang():
    cmd = _pyang()
    if cmd is None:
        pytest.skip("pyang not installed")
    res = subprocess.run(cmd + ["--strict", "--lint", str(YANG)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "" and res.stderr.strip() == ""


def test_yang_tree_contains_required_leaves():
    cmd = _pyang()
    if cmd is None:
        pytest.skip("pyang not installed")
    tree = subprocess.run(cmd + ["-f", "tree", str(YANG)], capture_output=True, text=True).stdout
    for leaf in ["moisture-threshold-low", "moisture-threshold-high", "sensor-timeout"]:
        assert f"+--rw {leaf}" in tree
    for leaf in ["soil-moisture", "sensor-status", "pump-state", "fault-alert",
                 "fault-description", "last-valid-reading", "last-reading-timestamp",
                 "total-valid-readings", "total-invalid-readings", "total-timeout-events"]:
        assert f"+--ro {leaf}" in tree


def test_yang_declares_threshold_must_rule():
    text = YANG.read_text()
    assert 'must "moisture-threshold-high > moisture-threshold-low"' in text
    assert 'range "1 .. 300"' in text and 'default "15"' in text


# ================================================ Part A: client XML
def test_edit_config_payload_is_namespaced():
    xml = build_settings_config(sensor_timeout=10, moisture_threshold_low=25)
    root = etree.fromstring(xml.encode())
    ns = {"im": NAMESPACE}
    assert root.find("im:irrigation/im:settings/im:sensor-timeout", ns).text == "10"
    assert root.find("im:irrigation/im:settings/im:moisture-threshold-low", ns).text == "25.0"


def test_parse_get_config_reply_with_defaults():
    data = (f'<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><irrigation xmlns="{NAMESPACE}">'
            f'<settings><sensor-timeout>10</sensor-timeout></settings></irrigation></data>')
    assert parse_settings(data) == {**CFG, "sensor_timeout": 10}


def test_parse_get_reply():
    data = (f'<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><irrigation xmlns="{NAMESPACE}"><status>'
            '<soil-moisture>25.0</soil-moisture><sensor-status>healthy</sensor-status>'
            '<pump-state>on</pump-state><fault-alert>false</fault-alert>'
            '<total-valid-readings>3</total-valid-readings>'
            '<applied-settings><sensor-timeout>10</sensor-timeout></applied-settings>'
            '</status></irrigation></data>')
    st = parse_status(data)
    assert st["soil-moisture"] == 25.0 and st["pump-state"] == "on"
    assert st["fault-alert"] is False and st["total-valid-readings"] == 3
    assert st["applied-settings"]["sensor-timeout"] == 10


# ============================================== Part A: adapter logic
def test_leaf_name():
    assert leaf_name("/irrigation-monitoring:irrigation/settings/sensor-timeout") == "sensor-timeout"


def test_adapter_translates_edit_config_changes():
    xp = "/irrigation-monitoring:irrigation/settings/"
    out = changes_to_config(CFG, [("modified", xp + "sensor-timeout", 10),
                                  ("modified", xp + "moisture-threshold-low", "25.0")])
    assert out == {**CFG, "sensor_timeout": 10, "moisture_threshold_low": 25.0}
    # deleting a leaf restores its YANG default
    assert changes_to_config(out, [("deleted", xp + "sensor-timeout", None)])["sensor_timeout"] == 15


def test_settings_from_sysrepo_data():
    data = {"irrigation": {"settings": {"sensor-timeout": 10, "moisture-threshold-high": "50.0"}}}
    assert settings_from_yang(data) == {"sensor_timeout": 10, "moisture_threshold_high": 50.0}
    assert settings_from_yang(None) == {}


def test_state_to_yang_from_live_controller(feeder):
    feeder.send(120)
    snap = feeder.system.snapshot()
    st = state_to_yang(snap, now=snap["updated_epoch"])["irrigation"]["status"]
    assert st["sensor-status"] == "invalid" and st["pump-state"] == "off"
    assert st["fault-alert"] is True
    assert st["active-alert"][0]["type"] == "invalid-reading"
    assert st["applied-settings"]["sensor-timeout"] == 15
    assert st["total-invalid-readings"] == 1


def test_state_to_yang_reports_safe_state_when_controller_down():
    st = state_to_yang(None)["irrigation"]["status"]
    assert st["sensor-status"] == "stale" and st["pump-state"] == "off" and st["fault-alert"] is True


def test_06_local_settings_sync_applies_new_timeout(tmp_project):
    """The adapter's write path: once settings.json changes, the running
    controller picks up 10 s and times out at 10 s instead of 15 s."""
    path = tmp_project / "config" / "settings.json"
    controller_cfg = ConfigManager(path)
    system = IrrigationSystem(controller_cfg, StateManager(None), start_time=0)
    adapter_cfg = ConfigManager(path)                 # separate instance = other process
    time.sleep(0.05)                                  # distinct mtime on coarse filesystems
    adapter_cfg.update(source="netconf", sensor_timeout=10)
    assert controller_cfg.reload_if_changed() is True
    assert controller_cfg.get().sensor_timeout == 10

    system.process_raw('{"seq": 1, "moisture": 25}', now=100.0)
    assert system.st.pump_state is PumpState.ON
    system.check_timeouts(now=109.9)
    assert system.st.sensor_status is SensorStatus.HEALTHY
    system.check_timeouts(now=110.0)
    assert system.st.sensor_status is SensorStatus.STALE
    assert system.st.pump_state is PumpState.OFF


# ===================================== Part B: genuine NETCONF (Test 6)
def _netconf_available() -> str | None:
    if os.environ.get("NETCONF_TESTS") != "1":
        return "set NETCONF_TESTS=1 to run against a live netopeer2-server"
    host = os.environ.get("NETCONF_HOST", "127.0.0.1")
    port = int(os.environ.get("NETCONF_PORT", "830"))
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return f"no NETCONF server on {host}:{port}"
    return None


netconf_live = pytest.mark.skipif(_netconf_available() is not None,
                                  reason=_netconf_available() or "")


@pytest.fixture
def nc():
    from netconf.netconf_client import IrrigationNetconfClient

    client = IrrigationNetconfClient().connect()
    original = client.get_config()
    yield client
    client.edit_config(**original)                    # restore
    client.close()


@pytest.mark.netconf
@netconf_live
def test_06_netconf_edit_config_timeout_10_applied_by_controller(nc):
    assert nc.supports_module(), "irrigation-monitoring not advertised in <hello>"
    nc.edit_config(sensor_timeout=15)
    time.sleep(0.5)

    nc.edit_config(sensor_timeout=10)                 # genuine <edit-config>
    assert nc.get_config()["sensor_timeout"] == 10    # genuine <get-config>

    # adapter -> settings.json -> controller config manager. sysrepo delivers
    # the "done" event asynchronously, so allow up to 3 s.
    deadline = time.time() + 3
    while True:
        controller_cfg = ConfigManager(DEFAULT_SETTINGS_PATH)
        if controller_cfg.get().sensor_timeout == 10 or time.time() > deadline:
            break
        time.sleep(0.1)
    assert controller_cfg.get().sensor_timeout == 10

    # controller behaviour with the NETCONF-provided timeout
    system = IrrigationSystem(controller_cfg, StateManager(None), start_time=0)
    system.process_raw('{"seq": 1, "moisture": 25}', now=100.0)
    system.check_timeouts(now=109.9)
    assert system.st.sensor_status is SensorStatus.HEALTHY
    system.check_timeouts(now=110.0)
    assert system.st.sensor_status is SensorStatus.STALE


@pytest.mark.netconf
@netconf_live
def test_06_running_controller_reports_applied_timeout_via_get(nc):
    """Requires the controller process too (python -m controller.main)."""
    nc.edit_config(sensor_timeout=10)
    applied = nc.wait_until_applied({"sensor_timeout": 10}, timeout_s=5)
    if applied is None and nc.get_status().get("status-detail", "").startswith("controller not running"):
        pytest.skip("controller process not running")
    assert applied is not None and applied["sensor-timeout"] == 10


@pytest.mark.netconf
@netconf_live
def test_09_netconf_rejects_high_not_greater_than_low(nc):
    from netconf.netconf_client import NetconfOperationError

    nc.edit_config(moisture_threshold_low=30, moisture_threshold_high=45)
    with pytest.raises(NetconfOperationError):
        nc.edit_config(moisture_threshold_high=30)
    assert nc.get_config()["moisture_threshold_high"] == 45.0


@pytest.mark.netconf
@netconf_live
def test_netconf_get_returns_operational_status(nc):
    st = nc.get_status()
    assert st["sensor-status"] in {"healthy", "invalid", "stale"}
    assert st["pump-state"] in {"on", "off"}
    assert isinstance(st["fault-alert"], bool)

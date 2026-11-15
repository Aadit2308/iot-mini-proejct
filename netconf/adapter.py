"""Sysrepo <-> controller adapter (runs next to netopeer2-server).

    NETCONF client (ncclient) --SSH:830--> netopeer2-server --> sysrepo
                                                                  |
                         this adapter (sysrepo-python subscriptions)
                           |  edit-config  -> validate -> config/settings.json
                           |  <get> status <- runtime/controller_state.json
                           |  notification <- sensor status changes
                                                                  |
                                                     controller (polls settings.json)

1. **Configuration** (<edit-config> / <get-config>): a module-change
   subscription on ``/irrigation-monitoring:irrigation/settings``.
   - "change" event: build the resulting config and run the controller's
     validator; raising SysrepoValidationFailedError makes netopeer2 send an
     <rpc-error> and nothing is committed.
     (sysrepo also enforces the YANG ``must`` rule itself.)
   - "done" event: write the new values to config/settings.json; the
     controller picks them up within ~0.25 s.
2. **Operational data** (<get>): an oper-data subscription on
   ``/irrigation-monitoring:irrigation/status`` that converts the controller's
   state snapshot into YANG data.
3. **Notifications**: sends ``sensor-health-change`` when the controller's
   sensor-status changes.

Run (WSL/Linux, after scripts/setup_netconf.sh):

    sudo .venv/bin/python -m netconf.adapter

The pure helpers (``changes_to_config``, ``state_to_yang``) have no sysrepo
dependency so they are unit-tested on any OS.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller import DEFAULT_SETTINGS_PATH, DEFAULT_STATE_PATH  # noqa: E402
from controller.config_manager import (  # noqa: E402
    ConfigManager,
    ConfigValidationError,
    validate_config,
)
from controller.logging_setup import log_event, setup_logging  # noqa: E402
from controller.state_manager import load_state  # noqa: E402
from netconf import (  # noqa: E402
    KEY_TO_LEAF,
    LEAF_TO_KEY,
    MODULE,
    NOTIFICATION_XPATH,
    SETTINGS_XPATH,
    STATUS_XPATH,
    YANG_DEFAULTS,
)

STALE_STATE_AFTER_S = 5.0  # controller writes state every ~0.25 s

log = logging.getLogger("irrigation")


# ====================================================================
# Pure helpers (unit-tested without sysrepo)
# ====================================================================
def leaf_name(xpath: str) -> str:
    """'/irrigation-monitoring:irrigation/settings/sensor-timeout' -> 'sensor-timeout'"""
    last = xpath.rstrip("/").rsplit("/", 1)[-1]
    return last.split(":", 1)[-1]


def _num(value: Any) -> float:
    # libyang may hand decimal64 values over as Decimal, float or str.
    return float(str(value))


def changes_to_config(current: dict[str, Any], changes: Iterable[tuple[str, str, Any]]) -> dict[str, Any]:
    """Apply sysrepo changes to a config dict.

    ``changes`` is an iterable of ``(operation, xpath, value)`` where
    operation is "created", "modified" or "deleted". Deleting a leaf reverts
    it to its YANG default. Unrelated xpaths are ignored.
    """
    cfg = dict(current)
    for op, xpath, value in changes:
        key = LEAF_TO_KEY.get(leaf_name(xpath))
        if key is None:
            continue
        if op == "deleted":
            cfg[key] = YANG_DEFAULTS[key]
        elif key == "sensor_timeout":
            cfg[key] = int(_num(value))
        else:
            cfg[key] = _num(value)
    return cfg


def settings_from_yang(data: dict | None) -> dict[str, Any]:
    """Extract explicitly-set settings from a sysrepo get_data() dict."""
    if not data:
        return {}
    settings = (data.get("irrigation") or {}).get("settings") or {}
    out: dict[str, Any] = {}
    for leaf, key in LEAF_TO_KEY.items():
        if leaf in settings:
            v = settings[leaf]
            out[key] = int(_num(v)) if key == "sensor_timeout" else _num(v)
    return out


def _dec(value: float | None) -> str | None:
    return None if value is None else f"{float(value):.1f}"


def state_to_yang(snapshot: dict | None, now: float | None = None) -> dict:
    """Controller snapshot -> YANG data for /irrigation/status.

    If the controller is not running (no snapshot or an old one), report the
    safe state: stale, pump off, fault alert raised.
    """
    now = time.time() if now is None else now
    if snapshot is None or now - float(snapshot.get("updated_epoch", 0)) > STALE_STATE_AFTER_S:
        last = snapshot or {}
        status = {
            "sensor-status": "stale",
            "status-detail": "controller not running (no fresh state)",
            "pump-state": "off",
            "auto-control": "suspended",
            "fault-alert": True,
            "fault-description": "controller-offline: irrigation controller is not running",
            "total-valid-readings": int(last.get("total_valid_readings", 0)),
            "total-invalid-readings": int(last.get("total_invalid_readings", 0)),
            "total-timeout-events": int(last.get("total_timeout_events", 0)),
            "total-missing-messages": int(last.get("total_missing_messages", 0)),
            "total-delayed-messages": int(last.get("total_delayed_messages", 0)),
            "total-recovery-events": int(last.get("total_recovery_events", 0)),
        }
        if last.get("last_valid_reading"):
            status["last-valid-reading"] = last["last_valid_reading"]
        return {"irrigation": {"status": status}}

    s = snapshot
    status: dict[str, Any] = {
        "sensor-status": s["sensor_status"],
        "status-detail": s.get("status_detail") or "",
        "pump-state": s["pump_state"],
        "auto-control": s.get("auto_control", "suspended"),
        "fault-alert": bool(s["fault_alert"]),
        "fault-description": s.get("fault_description") or "none",
        "total-valid-readings": int(s.get("total_valid_readings", 0)),
        "total-invalid-readings": int(s.get("total_invalid_readings", 0)),
        "total-timeout-events": int(s.get("total_timeout_events", 0)),
        "total-missing-messages": int(s.get("total_missing_messages", 0)),
        "total-delayed-messages": int(s.get("total_delayed_messages", 0)),
        "total-recovery-events": int(s.get("total_recovery_events", 0)),
    }
    optional = {
        "sensor-id": s.get("sensor_id"),
        "soil-moisture": _dec(s.get("soil_moisture")),
        "last-valid-reading": s.get("last_valid_reading"),
        "last-reading-timestamp": s.get("last_reading_timestamp"),
        "last-raw-value": s.get("last_raw_value"),
    }
    status.update({k: v for k, v in optional.items() if v is not None})

    cfg = s.get("config")
    if cfg:
        status["applied-settings"] = {
            "moisture-threshold-low": _dec(cfg["moisture_threshold_low"]),
            "moisture-threshold-high": _dec(cfg["moisture_threshold_high"]),
            "sensor-timeout": int(cfg["sensor_timeout"]),
        }
    alerts = s.get("active_alerts") or []
    if alerts:
        status["active-alert"] = [
            {"type": a["type"], "description": a["description"], "raised-at": a["raised_at"]}
            for a in alerts
        ]
    return {"irrigation": {"status": status}}


# ====================================================================
# Sysrepo adapter (Linux only)
# ====================================================================
class SysrepoAdapter:
    def __init__(self, settings_path: Path, state_path: Path):
        import sysrepo  # imported here so the helpers work without it

        self.sysrepo = sysrepo
        self.settings_path = settings_path
        self.state_path = state_path
        self.config = ConfigManager(settings_path)
        self.conn = sysrepo.SysrepoConnection()
        self.sess = self.conn.start_session("running")
        self._stop = threading.Event()
        self._last_status: str | None = None

    # ----------------------------------------------------- initial sync
    def initial_sync(self) -> None:
        """Make sysrepo and settings.json agree at start-up.

        If the running datastore holds explicitly configured settings, they
        win (NETCONF is the management plane). Otherwise the running
        datastore is initialised from settings.json.
        """
        data = self.sess.get_data(SETTINGS_XPATH)  # explicit nodes only
        explicit = settings_from_yang(data)
        if explicit:
            merged = {**YANG_DEFAULTS, **explicit}
            cfg = validate_config(merged)
            if cfg != self.config.get():
                self.config.update(source="netconf-startup-sync", **cfg.to_dict())
            log_event("SYSTEM", f"adapter: loaded settings from sysrepo running: {cfg.to_dict()}")
        else:
            cfg = self.config.get()
            for key, leaf in KEY_TO_LEAF.items():
                value = getattr(cfg, key)
                self.sess.set_item(f"{SETTINGS_XPATH}/{leaf}",
                                   str(int(value)) if key == "sensor_timeout" else f"{value:.1f}")
            self.sess.apply_changes()
            log_event("SYSTEM", f"adapter: initialised sysrepo running from settings.json: {cfg.to_dict()}")

    # --------------------------------------------------- subscriptions
    def _module_change_cb(self, event: str, req_id: int, changes, private_data, **kwargs) -> None:
        sr = self.sysrepo
        triples = []
        for ch in changes:
            if isinstance(ch, sr.ChangeDeleted):
                triples.append(("deleted", ch.xpath, None))
            elif isinstance(ch, sr.ChangeCreated):
                triples.append(("created", ch.xpath, ch.value))
            elif isinstance(ch, sr.ChangeModified):
                triples.append(("modified", ch.xpath, ch.value))
        current = self.config.get().to_dict()
        proposed = changes_to_config(current, triples)

        if event == "change":
            try:
                validate_config(proposed)
            except ConfigValidationError as exc:
                log_event("CONFIG_REJECTED", f"source=netconf edit-config rejected: {exc}")
                raise sr.SysrepoValidationFailedError(str(exc))
        elif event == "done":
            if proposed != current:
                try:
                    self.config.update(source="netconf", **proposed)
                except ConfigValidationError as exc:  # should not happen
                    log_event("CONFIG_REJECTED", f"netconf done-event write failed: {exc}")

    def _oper_cb(self, xpath: str, private_data, **kwargs) -> dict:
        return state_to_yang(load_state(self.state_path))

    def subscribe(self) -> None:
        self.sess.subscribe_module_change(
            MODULE, SETTINGS_XPATH, self._module_change_cb
        )
        self.sess.subscribe_oper_data_request(
            MODULE, STATUS_XPATH, self._oper_cb
        )
        log_event("SYSTEM", "adapter: subscribed to module changes and operational data requests")

    # --------------------------------------------------- notifications
    def _watch_status(self) -> None:
        while not self._stop.wait(0.5):
            data = state_to_yang(load_state(self.state_path))["irrigation"]["status"]
            status = data["sensor-status"]
            if self._last_status is not None and status != self._last_status:
                try:
                    self.sess.notification_send(
                        NOTIFICATION_XPATH,
                        {
                            "previous-status": self._last_status,
                            "new-status": status,
                            "pump-state": data["pump-state"],
                            "description": data.get("status-detail", ""),
                        },
                    )
                    log_event("SYSTEM", f"adapter: notification sensor-health-change "
                                        f"{self._last_status} -> {status}")
                except Exception as exc:  # notifications are best-effort
                    log.warning("notification_send failed: %s", exc)
            self._last_status = status

    def run(self) -> None:
        self.initial_sync()
        self.subscribe()
        watcher = threading.Thread(target=self._watch_status, daemon=True)
        watcher.start()
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        log_event("SYSTEM", "adapter running (Ctrl+C to stop)")
        while not self._stop.wait(1.0):
            pass
        self.sess.stop()
        self.conn.disconnect()
        log_event("SYSTEM", "adapter stopped")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="sysrepo <-> irrigation controller adapter")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    p.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    args = p.parse_args(argv)
    setup_logging(args.log_dir, log_name="netconf_adapter")
    try:
        import sysrepo  # noqa: F401
    except ImportError:
        print("sysrepo-python is not installed. Run scripts/install_wsl_deps.sh in WSL.", file=sys.stderr)
        return 1
    SysrepoAdapter(args.settings, args.state).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Controller state: health, counters, alerts, recent events.

The state is persisted to ``runtime/controller_state.json`` after every
controller cycle. Two readers use that file:
  * the Streamlit dashboard,
  * the NETCONF adapter's operational-data callback (<get>).
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import DEFAULT_STATE_PATH
from .irrigation_control import PumpState, SensorStatus
from .logging_setup import iso_timestamp

ALERT_INVALID = "invalid-reading"
ALERT_MALFORMED = "malformed-message"
ALERT_TIMEOUT = "communication-timeout"


@dataclass
class Alert:
    alert_type: str
    description: str
    raised_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.alert_type,
            "description": self.description,
            "raised_at": iso_timestamp(self.raised_at),
        }


@dataclass
class ControllerState:
    sensor_id: str | None = None
    soil_moisture: float | None = None          # latest *valid* value, %
    sensor_status: SensorStatus = SensorStatus.STALE
    status_detail: str = "awaiting first reading"
    pump_state: PumpState = PumpState.OFF
    auto_control: bool = False
    last_valid_time: float | None = None        # epoch of last valid reading
    last_reading_time: float | None = None      # epoch of last reading of any kind
    last_raw_value: str | None = None
    last_sequence: int | None = None
    total_valid_readings: int = 0
    total_invalid_readings: int = 0             # out-of-range + malformed
    total_timeout_events: int = 0
    total_missing_messages: int = 0
    total_delayed_messages: int = 0
    total_recovery_events: int = 0
    total_pump_activations: int = 0
    alerts: dict[str, Alert] = field(default_factory=dict)
    recent_events: deque = field(default_factory=lambda: deque(maxlen=50))
    started_at: float = field(default_factory=time.time)


class StateManager:
    def __init__(self, path: Path | str | None = DEFAULT_STATE_PATH):
        self.path = Path(path) if path is not None else None
        self.state = ControllerState()

    # -------------------------------------------------------------- alerts
    def raise_alert(self, alert_type: str, description: str, now: float) -> bool:
        """Raise or refresh an alert. Returns True if it is new."""
        existing = self.state.alerts.get(alert_type)
        if existing is None:
            self.state.alerts[alert_type] = Alert(alert_type, description, now)
            return True
        existing.description = description
        return False

    def clear_alerts(self) -> list[Alert]:
        cleared = list(self.state.alerts.values())
        self.state.alerts.clear()
        return cleared

    @property
    def fault_alert(self) -> bool:
        return bool(self.state.alerts)

    @property
    def fault_description(self) -> str:
        if not self.state.alerts:
            return "none"
        # most recently raised first
        alerts = sorted(self.state.alerts.values(), key=lambda a: a.raised_at, reverse=True)
        return "; ".join(f"{a.alert_type}: {a.description}" for a in alerts)

    def add_event(self, record: dict) -> None:
        self.state.recent_events.append(record)

    # ------------------------------------------------------------ snapshot
    def snapshot(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        s = self.state
        snap = {
            "updated_at": iso_timestamp(),
            "updated_epoch": time.time(),
            "controller_pid": os.getpid(),
            "sensor_id": s.sensor_id,
            "soil_moisture": s.soil_moisture,
            "sensor_status": s.sensor_status.value,
            "status_detail": s.status_detail,
            "pump_state": s.pump_state.value,
            "auto_control": "active" if s.auto_control else "suspended",
            "fault_alert": self.fault_alert,
            "fault_description": self.fault_description,
            "active_alerts": [a.to_dict() for a in s.alerts.values()],
            "last_valid_reading": iso_timestamp(s.last_valid_time) if s.last_valid_time else None,
            "last_valid_epoch": s.last_valid_time,
            "last_reading_timestamp": iso_timestamp(s.last_reading_time) if s.last_reading_time else None,
            "last_raw_value": s.last_raw_value,
            "last_sequence": s.last_sequence,
            "total_valid_readings": s.total_valid_readings,
            "total_invalid_readings": s.total_invalid_readings,
            "total_timeout_events": s.total_timeout_events,
            "total_missing_messages": s.total_missing_messages,
            "total_delayed_messages": s.total_delayed_messages,
            "total_recovery_events": s.total_recovery_events,
            "total_pump_activations": s.total_pump_activations,
            "recent_events": list(s.recent_events),
            "started_at": iso_timestamp(s.started_at),
        }
        if config is not None:
            snap["config"] = config
        return snap

    def save(self, config: dict[str, Any] | None = None) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.snapshot(config), indent=2, default=str), encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # Windows: reader has the file open
                if attempt == 19:
                    raise
                time.sleep(0.05)


def load_state(path: Path | str = DEFAULT_STATE_PATH) -> dict[str, Any] | None:
    """Read the controller snapshot; None if the controller never ran."""
    path = Path(path)
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.05)  # caught mid-replace; retry
    return None

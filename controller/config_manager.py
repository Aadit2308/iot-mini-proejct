"""Validated configuration storage shared by the controller, NETCONF adapter
and dashboard.

``config/settings.json`` is the hand-off point:
  * the NETCONF adapter writes it after a successful <edit-config>,
  * the controller polls it (mtime) and applies changes at runtime.

All writes go through :func:`validate_config`, so an invalid configuration
(e.g. high threshold <= low threshold) can never reach the controller.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import DEFAULT_SETTINGS_PATH
from .logging_setup import log_event

MOISTURE_MIN = 0.0
MOISTURE_MAX = 100.0
TIMEOUT_MIN = 1
TIMEOUT_MAX = 300

CONFIG_KEYS = ("moisture_threshold_low", "moisture_threshold_high", "sensor_timeout")


class ConfigValidationError(ValueError):
    """Raised when a configuration change violates a constraint."""


@dataclass(frozen=True)
class IrrigationConfig:
    moisture_threshold_low: float = 30.0   # percent
    moisture_threshold_high: float = 45.0  # percent
    sensor_timeout: int = 15               # seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigValidationError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigValidationError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ConfigValidationError(f"{name} must be finite, got {value!r}")
    return number


def validate_config(data: dict[str, Any]) -> IrrigationConfig:
    """Check types, ranges and the cross-field rule; return a typed config."""
    unknown = set(data) - set(CONFIG_KEYS)
    if unknown:
        raise ConfigValidationError(f"Unknown configuration key(s): {sorted(unknown)}")
    missing = set(CONFIG_KEYS) - set(data)
    if missing:
        raise ConfigValidationError(f"Missing configuration key(s): {sorted(missing)}")

    low = _as_number("moisture_threshold_low", data["moisture_threshold_low"])
    high = _as_number("moisture_threshold_high", data["moisture_threshold_high"])
    timeout_f = _as_number("sensor_timeout", data["sensor_timeout"])

    for name, val in (("moisture_threshold_low", low), ("moisture_threshold_high", high)):
        if not MOISTURE_MIN <= val <= MOISTURE_MAX:
            raise ConfigValidationError(
                f"{name}={val} outside {MOISTURE_MIN:g}-{MOISTURE_MAX:g} %"
            )
    if high <= low:
        raise ConfigValidationError(
            f"moisture_threshold_high ({high:g}) must be greater than "
            f"moisture_threshold_low ({low:g})"
        )
    if timeout_f != int(timeout_f):
        raise ConfigValidationError(f"sensor_timeout must be a whole number, got {timeout_f}")
    timeout = int(timeout_f)
    if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise ConfigValidationError(
            f"sensor_timeout={timeout} outside {TIMEOUT_MIN}-{TIMEOUT_MAX} s"
        )
    return IrrigationConfig(round(low, 1), round(high, 1), timeout)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    # If root (the NETCONF adapter) rewrites the file, keep the original owner
    # so the controller/dashboard running as a normal user can still write it.
    if hasattr(os, "geteuid") and os.geteuid() == 0 and path.exists():
        st = path.stat()
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
    # os.replace can briefly fail on Windows if a reader holds the file open.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


Listener = Callable[[IrrigationConfig, IrrigationConfig, str], None]


class ConfigManager:
    """Thread-safe, validated access to ``settings.json``."""

    def __init__(self, path: Path | str = DEFAULT_SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._mtime: float | None = None
        self._config = self._load_or_create()

    # ------------------------------------------------------------------ io
    def _read_file(self) -> IrrigationConfig:
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ConfigValidationError("settings.json must contain a JSON object")
        return validate_config({k: raw.get(k) for k in CONFIG_KEYS if k in raw})

    def _load_or_create(self) -> IrrigationConfig:
        if not self.path.exists():
            cfg = IrrigationConfig()
            _atomic_write_json(self.path, cfg.to_dict())
            self._mtime = self.path.stat().st_mtime
            return cfg
        cfg = self._read_file()
        self._mtime = self.path.stat().st_mtime
        return cfg

    # ----------------------------------------------------------------- api
    def get(self) -> IrrigationConfig:
        with self._lock:
            return self._config

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def update(self, source: str = "local", **changes: Any) -> IrrigationConfig:
        """Validate and persist a (partial) configuration change.

        Raises ConfigValidationError and leaves the stored config untouched
        if the result would be invalid.
        """
        with self._lock:
            old = self._config
            merged = {**old.to_dict(), **changes}
            try:
                new = validate_config(merged)
            except ConfigValidationError as exc:
                log_event("CONFIG_REJECTED", f"source={source}: {exc}", requested=changes)
                raise
            _atomic_write_json(self.path, new.to_dict())
            self._mtime = self.path.stat().st_mtime
            self._config = new
        self._notify(old, new, source)
        return new

    def reload_if_changed(self) -> bool:
        """Pick up external edits to settings.json (e.g. from the NETCONF
        adapter). Returns True if a new valid config was applied."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime == self._mtime:
            return False
        with self._lock:
            self._mtime = mtime
            old = self._config
            try:
                new = self._read_file()
            except (ConfigValidationError, json.JSONDecodeError, OSError) as exc:
                log_event(
                    "CONFIG_REJECTED",
                    f"settings.json change ignored, keeping previous config: {exc}",
                )
                return False
            if new == old:
                return False
            self._config = new
        self._notify(old, new, "settings-file")
        return True

    def _notify(self, old: IrrigationConfig, new: IrrigationConfig, source: str) -> None:
        if old == new:
            return
        diff = {
            k: {"old": getattr(old, k), "new": getattr(new, k)}
            for k in CONFIG_KEYS
            if getattr(old, k) != getattr(new, k)
        }
        desc = ", ".join(f"{k}: {v['old']} -> {v['new']}" for k, v in diff.items())
        log_event("CONFIG_UPDATE", f"source={source}: {desc}", changes=diff)
        for listener in self._listeners:
            listener(old, new, source)


"""Fault-aware smart irrigation controller package."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "runtime"
DEFAULT_STATE_PATH = DEFAULT_RUNTIME_DIR / "controller_state.json"

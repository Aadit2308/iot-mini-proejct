"""Message parsing and reading validation.

Two wire formats are accepted:

1. JSON (Python simulator, over UDP)::

       {"sensor_id": "node-1", "seq": 7, "moisture": 25.0, "timestamp": 1730000000.12}

2. Text line (Contiki-NG node, printed on the serial port / Cooja)::

       MOISTURE node=1 seq=7 value=25

Validation pipeline (first failing check wins):

  parse      -> malformed            (not JSON/known text, missing fields, NaN ...)
  range      -> invalid              (moisture outside 0-100 %)
  ordering   -> out-of-order         (seq <= last accepted seq: old/duplicate)
  age        -> stale-data           (sender timestamp older than sensor timeout)
  otherwise  -> valid  (may still carry a "delayed" warning and a seq gap)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum

MOISTURE_MIN = 0.0
MOISTURE_MAX = 100.0
DEFAULT_DELAY_WARNING_S = 2.0
# A sequence number this far *behind* the last one is treated as a sensor
# reboot (counter restarted) rather than a late message.
REBOOT_SEQ_WINDOW = 20

_TEXT_RE = re.compile(
    r"^MOISTURE\s+node=(?P<node>\w+)\s+seq=(?P<seq>\d+)\s+value=(?P<value>-?\d+(?:\.\d+)?)\s*$"
)


class Verdict(str, Enum):
    VALID = "valid"
    INVALID = "invalid"            # out of 0-100 % range
    MALFORMED = "malformed"        # could not be parsed
    OUT_OF_ORDER = "out-of-order"  # older than an already accepted reading
    STALE_DATA = "stale-data"      # delivered later than the sensor timeout


class MessageParseError(ValueError):
    pass


@dataclass(frozen=True)
class SensorMessage:
    sensor_id: str
    moisture: float
    seq: int | None = None
    sent_ts: float | None = None  # sender's epoch timestamp, if provided
    raw: str = ""
    boot_id: str | None = None    # changes when the sensor restarts


@dataclass(frozen=True)
class ValidationResult:
    verdict: Verdict
    reason: str
    message: SensorMessage | None = None
    latency: float | None = None    # receive time - sender timestamp
    delayed: bool = False           # accepted but arrived late
    missing_before: int = 0         # sequence numbers skipped before this one

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.VALID


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MessageParseError(f"field '{field}' must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise MessageParseError(f"field '{field}' is not finite ({value})")
    return value


def parse_message(raw: str | bytes) -> SensorMessage:
    """Parse one sensor message or raise MessageParseError."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise MessageParseError("message is not valid UTF-8") from None
    text = raw.strip()
    if not text:
        raise MessageParseError("empty message")

    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MessageParseError(f"invalid JSON: {exc.msg}") from None
        if not isinstance(obj, dict):
            raise MessageParseError("JSON message must be an object")
        if "moisture" not in obj:
            raise MessageParseError("missing field 'moisture'")
        moisture = _number(obj["moisture"], "moisture")
        sensor_id = str(obj.get("sensor_id", "unknown"))
        seq = obj.get("seq")
        if seq is not None:
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise MessageParseError(f"field 'seq' must be a non-negative integer, got {seq!r}")
        ts = obj.get("timestamp")
        sent_ts = _number(ts, "timestamp") if ts is not None else None
        boot = obj.get("boot_id")
        return SensorMessage(sensor_id, moisture, seq, sent_ts, text,
                             None if boot is None else str(boot))

    match = _TEXT_RE.match(text)
    if match:
        return SensorMessage(
            sensor_id=f"node-{match['node']}",
            moisture=float(match["value"]),
            seq=int(match["seq"]),
            sent_ts=None,
            raw=text,
        )
    raise MessageParseError(f"unrecognised message format: {text[:60]!r}")


class FaultDetector:
    """Stateless checks plus the bit of state needed for ordering/gaps."""

    def __init__(self, delay_warning_s: float = DEFAULT_DELAY_WARNING_S):
        self.delay_warning_s = delay_warning_s
        self.last_seq: int | None = None
        self.boot_id: str | None = None
        self.reboots_detected = 0
        # malformed messages consumed a sequence number we could not read;
        # don't also report them as missing
        self.malformed_since_valid = 0

    @staticmethod
    def in_range(moisture: float) -> bool:
        return MOISTURE_MIN <= moisture <= MOISTURE_MAX

    def evaluate(self, raw: str | bytes, now: float, sensor_timeout: float) -> ValidationResult:
        try:
            msg = parse_message(raw)
        except MessageParseError as exc:
            self.malformed_since_valid += 1
            return ValidationResult(Verdict.MALFORMED, str(exc))

        latency = None if msg.sent_ts is None else max(0.0, now - msg.sent_ts)
        self._detect_reboot(msg)

        if not self.in_range(msg.moisture):
            # Counts as "seen" for ordering so a late duplicate is not reprocessed.
            self._advance_seq(msg.seq)
            return ValidationResult(
                Verdict.INVALID,
                f"moisture {msg.moisture:g}% outside {MOISTURE_MIN:g}-{MOISTURE_MAX:g}%",
                msg,
                latency,
            )

        if msg.seq is not None and self.last_seq is not None and msg.seq <= self.last_seq:
            return ValidationResult(
                Verdict.OUT_OF_ORDER,
                f"seq {msg.seq} <= last accepted seq {self.last_seq} (late or duplicate)",
                msg,
                latency,
                delayed=True,
            )

        if latency is not None and latency > sensor_timeout:
            self._advance_seq(msg.seq)
            return ValidationResult(
                Verdict.STALE_DATA,
                f"reading is {latency:.1f}s old, older than sensor timeout {sensor_timeout:g}s",
                msg,
                latency,
                delayed=True,
            )

        missing = 0
        if msg.seq is not None and self.last_seq is not None:
            missing = max(0, msg.seq - self.last_seq - 1 - self.malformed_since_valid)
        self.malformed_since_valid = 0
        self._advance_seq(msg.seq)

        delayed = latency is not None and latency > self.delay_warning_s
        reason = "ok" if not delayed else f"accepted but delayed by {latency:.1f}s"
        return ValidationResult(Verdict.VALID, reason, msg, latency, delayed, missing)

    def _detect_reboot(self, msg: SensorMessage) -> None:
        rebooted = False
        if msg.boot_id is not None:
            if self.boot_id is not None and msg.boot_id != self.boot_id:
                rebooted = True
            self.boot_id = msg.boot_id
        elif (msg.seq is not None and self.last_seq is not None
              and self.last_seq - msg.seq > REBOOT_SEQ_WINDOW):
            rebooted = True
        if rebooted:
            self.last_seq = None
            self.reboots_detected += 1

    def _advance_seq(self, seq: int | None) -> None:
        if seq is not None and (self.last_seq is None or seq > self.last_seq):
            self.last_seq = seq

    def reset_sequence(self) -> None:
        """Forget the last sequence number (e.g. after a sensor reboot)."""
        self.last_seq = None


def is_timed_out(last_valid_time: float, now: float, timeout: float) -> bool:
    """True when no valid reading has been seen for ``timeout`` seconds."""
    return (now - last_valid_time) >= timeout

"""Safe hysteresis control of the virtual irrigation pump.

Rules (low/high thresholds are configurable, defaults 30 % / 45 %):

    sensor healthy  and moisture <  low   -> pump ON
    sensor healthy  and moisture >  high  -> pump OFF
    sensor healthy  and low <= m <= high  -> keep previous state (hysteresis)
    sensor invalid  or  stale             -> pump OFF, automatic control suspended

SAFETY INVARIANT: the only path that returns PumpState.ON requires
status == HEALTHY *and* a finite moisture value inside 0-100 %.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class SensorStatus(str, Enum):
    HEALTHY = "healthy"
    INVALID = "invalid"
    STALE = "stale"


class PumpState(str, Enum):
    ON = "on"
    OFF = "off"


@dataclass(frozen=True)
class PumpDecision:
    state: PumpState
    reason: str
    auto_control: bool  # False = automatic control suspended


def decide_pump_state(
    status: SensorStatus,
    moisture: float | None,
    previous: PumpState,
    low: float,
    high: float,
) -> PumpDecision:
    """Pure function implementing the irrigation rules."""
    if status is not SensorStatus.HEALTHY:
        return PumpDecision(PumpState.OFF, f"sensor {status.value}: pump forced OFF", False)
    if moisture is None or not math.isfinite(moisture) or not 0.0 <= moisture <= 100.0:
        # Defensive: a healthy status must always come with a valid value.
        return PumpDecision(PumpState.OFF, "no valid moisture value: pump forced OFF", False)
    if moisture < low:
        return PumpDecision(PumpState.ON, f"moisture {moisture:g}% < low threshold {low:g}%", True)
    if moisture > high:
        return PumpDecision(PumpState.OFF, f"moisture {moisture:g}% > high threshold {high:g}%", True)
    return PumpDecision(
        previous,
        f"moisture {moisture:g}% within {low:g}-{high:g}%: maintain {previous.value.upper()}",
        True,
    )


class IrrigationController:
    """Holds the pump state and applies :func:`decide_pump_state`."""

    def __init__(self) -> None:
        self.pump_state = PumpState.OFF  # pump always starts OFF
        self.auto_control = False        # until the first valid reading
        self.pump_on_since: float | None = None

    def apply(
        self,
        status: SensorStatus,
        moisture: float | None,
        low: float,
        high: float,
        now: float,
    ) -> tuple[PumpDecision, bool, bool]:
        """Apply the rules.

        Returns (decision, pump_changed, auto_control_changed).
        """
        decision = decide_pump_state(status, moisture, self.pump_state, low, high)
        pump_changed = decision.state is not self.pump_state
        auto_changed = decision.auto_control != self.auto_control
        self.pump_state = decision.state
        self.auto_control = decision.auto_control
        if pump_changed:
            self.pump_on_since = now if decision.state is PumpState.ON else None
        return decision, pump_changed, auto_changed

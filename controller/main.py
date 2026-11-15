"""Fault-aware irrigation controller - entry point and core logic.

Run (from the project root):

    python -m controller.main                       # UDP from Python simulator
    python -m controller.main --source serial       # Cooja Serial Socket on :60001
    ./sensor_node.native | python -m controller.main --source stdin

The core (:class:`IrrigationSystem`) takes explicit timestamps, so the tests
can drive it with a simulated clock while the real loop uses time.time().
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path

from . import DEFAULT_LOG_DIR, DEFAULT_SETTINGS_PATH, DEFAULT_STATE_PATH
from .config_manager import ConfigManager, IrrigationConfig
from .fault_detector import FaultDetector, Verdict
from .irrigation_control import IrrigationController, PumpState, SensorStatus
from .logging_setup import log_event, setup_logging
from .sensor_manager import SensorManager, is_sensor_line
from .state_manager import (
    ALERT_INVALID,
    ALERT_MALFORMED,
    ALERT_TIMEOUT,
    StateManager,
)

DEFAULT_EXPECTED_INTERVAL = 5.0  # sensors report every 5 s


class IrrigationSystem:
    """Fault detection + safe pump control for one soil-moisture sensor."""

    def __init__(
        self,
        config: ConfigManager,
        state: StateManager | None = None,
        expected_interval: float = DEFAULT_EXPECTED_INTERVAL,
        delay_warning_s: float = 2.0,
        start_time: float | None = None,
    ):
        self.config = config
        self.state_mgr = state or StateManager(path=None)
        self.detector = FaultDetector(delay_warning_s=delay_warning_s)
        self.pump = IrrigationController()
        self.expected_interval = expected_interval
        self._lock = threading.RLock()
        self._timeout_episode = False   # True while stale (one event per episode)
        self._silence_logged = False    # real-time "missing message" warning
        if start_time is not None:
            self.state_mgr.state.started_at = start_time
        config.add_listener(self._on_config_change)

    # ------------------------------------------------------------ helpers
    @property
    def st(self):
        return self.state_mgr.state

    def _event(self, category: str, message: str, **data) -> None:
        record = log_event(category, message, **data)
        self.state_mgr.add_event(record)

    @property
    def cfg(self) -> IrrigationConfig:
        return self.config.get()

    # ------------------------------------------------------- message path
    def process_raw(self, raw: str | bytes, now: float | None = None) -> Verdict:
        """Validate one incoming message and update health + pump."""
        now = time.time() if now is None else now
        with self._lock:
            cfg = self.cfg
            st = self.st
            st.last_reading_time = now
            self._silence_logged = False
            res = self.detector.evaluate(raw, now, cfg.sensor_timeout)
            msg = res.message

            if msg is not None:
                st.sensor_id = msg.sensor_id
                st.last_raw_value = f"{msg.moisture:g}"
                if msg.seq is not None:
                    st.last_sequence = msg.seq
            else:
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                st.last_raw_value = text.strip()[:40]

            if res.verdict is Verdict.MALFORMED:
                st.total_invalid_readings += 1
                self._event("MALFORMED_MESSAGE", f"rejected: {res.reason}", raw=st.last_raw_value)
                self._sensor_fault(ALERT_MALFORMED, f"malformed message ({res.reason})", now)

            elif res.verdict is Verdict.INVALID:
                st.total_invalid_readings += 1
                self._event(
                    "INVALID_READING",
                    f"rejected {msg.moisture:g}% from {msg.sensor_id} seq={msg.seq}: {res.reason}",
                    value=msg.moisture,
                    seq=msg.seq,
                )
                self._sensor_fault(ALERT_INVALID, f"invalid reading {msg.moisture:g}% ({res.reason})", now)

            elif res.verdict in (Verdict.OUT_OF_ORDER, Verdict.STALE_DATA):
                st.total_delayed_messages += 1
                self._event(
                    "DELAYED_MESSAGE",
                    f"discarded {msg.moisture:g}% seq={msg.seq}: {res.reason}",
                    value=msg.moisture,
                    seq=msg.seq,
                    latency=res.latency,
                )
                # Old data never refreshes health or drives the pump.

            else:  # VALID
                if res.missing_before > 0:
                    st.total_missing_messages += res.missing_before
                    self._event(
                        "MISSING_MESSAGE",
                        f"{res.missing_before} message(s) missing before seq={msg.seq}",
                        missing=res.missing_before,
                        seq=msg.seq,
                    )
                if res.delayed:
                    st.total_delayed_messages += 1
                    self._event(
                        "DELAYED_MESSAGE",
                        f"seq={msg.seq} {res.reason}",
                        seq=msg.seq,
                        latency=res.latency,
                    )
                self._accept_valid(msg.moisture, msg.seq, now)

            self._apply_control(now)
            return res.verdict

    def _accept_valid(self, moisture: float, seq: int | None, now: float) -> None:
        st = self.st
        st.total_valid_readings += 1
        st.soil_moisture = moisture
        st.last_valid_time = now
        self._event("VALID_READING", f"accepted {moisture:g}% seq={seq}", value=moisture, seq=seq)

        if st.sensor_status is not SensorStatus.HEALTHY or self.state_mgr.fault_alert:
            previous = st.sensor_status.value
            cleared = self.state_mgr.clear_alerts()
            if st.total_valid_readings > 1 or cleared or self._timeout_episode:
                st.total_recovery_events += 1
                names = ", ".join(a.alert_type for a in cleared) or "none"
                self._event(
                    "RECOVERY",
                    f"sensor recovered: {previous} -> healthy; cleared alerts: {names}",
                    previous_status=previous,
                    cleared=[a.alert_type for a in cleared],
                )
        st.sensor_status = SensorStatus.HEALTHY
        st.status_detail = "receiving valid readings"
        self._timeout_episode = False

    def _sensor_fault(self, alert_type: str, description: str, now: float) -> None:
        if self.state_mgr.raise_alert(alert_type, description, now):
            self._event("ALERT", f"raised {alert_type}: {description}", alert=alert_type)
        if not self._timeout_episode:
            # While stale, the communication fault dominates: stay stale until a
            # valid reading arrives (avoids invalid<->stale flapping).
            self.st.sensor_status = SensorStatus.INVALID
            self.st.status_detail = description

    # ------------------------------------------------------- timeout path
    def check_timeouts(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            st = self.st
            timeout = self.cfg.sensor_timeout

            last_msg = st.last_reading_time or st.started_at
            silence = now - last_msg
            if not self._silence_logged and silence >= 1.5 * self.expected_interval:
                self._silence_logged = True
                self._event(
                    "MISSING_MESSAGE",
                    f"no message received for {silence:.1f}s (expected every {self.expected_interval:g}s)",
                    silence=round(silence, 2),
                )

            if self._timeout_episode:
                return
            reference = st.last_valid_time or st.started_at
            elapsed = now - reference
            if elapsed >= timeout:
                self._timeout_episode = True
                st.total_timeout_events += 1
                st.sensor_status = SensorStatus.STALE
                desc = f"no valid reading for {elapsed:.1f}s (timeout {timeout}s)"
                st.status_detail = desc
                self._event("TIMEOUT", f"sensor stale: {desc}", elapsed=round(elapsed, 2), timeout=timeout)
                if self.state_mgr.raise_alert(ALERT_TIMEOUT, f"communication fault: {desc}", now):
                    self._event("ALERT", f"raised {ALERT_TIMEOUT}: {desc}", alert=ALERT_TIMEOUT)
                self._apply_control(now)

    # -------------------------------------------------------- pump path
    def _apply_control(self, now: float) -> None:
        cfg, st = self.cfg, self.st
        moisture = st.soil_moisture if st.sensor_status is SensorStatus.HEALTHY else None
        decision, pump_changed, auto_changed = self.pump.apply(
            st.sensor_status, moisture, cfg.moisture_threshold_low, cfg.moisture_threshold_high, now
        )
        old_pump = st.pump_state
        st.pump_state = decision.state
        st.auto_control = decision.auto_control
        if pump_changed:
            if decision.state is PumpState.ON:
                st.total_pump_activations += 1
            self._event(
                "PUMP_STATE_CHANGE",
                f"pump {old_pump.value.upper()} -> {decision.state.value.upper()} ({decision.reason})",
                old=old_pump.value,
                new=decision.state.value,
            )
        if auto_changed:
            self._event(
                "AUTO_CONTROL",
                ("automatic control RESUMED" if decision.auto_control else "automatic control SUSPENDED")
                + f" ({decision.reason})",
                active=decision.auto_control,
            )

    def _on_config_change(self, old: IrrigationConfig, new: IrrigationConfig, source: str) -> None:
        now = time.time()
        with self._lock:
            # New thresholds take effect immediately on the current reading.
            self._apply_control(now)

    # ---------------------------------------------------------- output
    def snapshot(self) -> dict:
        with self._lock:
            return self.state_mgr.snapshot(self.cfg.to_dict())

    def save_state(self) -> None:
        with self._lock:
            self.state_mgr.save(self.cfg.to_dict())

    def shutdown(self) -> None:
        with self._lock:
            if self.st.pump_state is PumpState.ON:
                self._event("PUMP_STATE_CHANGE", "pump ON -> OFF (controller shutdown)")
            self.st.pump_state = PumpState.OFF
            self.pump.pump_state = PumpState.OFF
            self.st.auto_control = False
            self.st.status_detail = "controller stopped"
            self.save_state()


# ====================================================================== CLI
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fault-aware smart irrigation controller")
    p.add_argument("--source", choices=["udp", "serial", "stdin"], action="append",
                   help="message source (repeatable). Default: udp")
    p.add_argument("--udp-host", default="127.0.0.1")
    p.add_argument("--udp-port", type=int, default=5005)
    p.add_argument("--serial-host", default="127.0.0.1", help="Cooja Serial Socket host")
    p.add_argument("--serial-port", type=int, default=60001, help="Cooja Serial Socket port")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--expected-interval", type=float, default=DEFAULT_EXPECTED_INTERVAL,
                   help="expected seconds between sensor reports")
    p.add_argument("--run-for", type=float, default=None, help="stop after N seconds (testing)")
    p.add_argument("--quiet", action="store_true", help="no console log output")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_dir, console=not args.quiet)
    config = ConfigManager(args.settings)
    system = IrrigationSystem(config, StateManager(args.state), args.expected_interval)

    sources = args.source or ["udp"]
    sensors = SensorManager()
    if "udp" in sources:
        sensors.add_udp(args.udp_host, args.udp_port)
    if "serial" in sources:
        # Cooja: the serial socket belongs to the controller mote. Push the
        # (NETCONF-managed) thresholds to it on connect and on every change.
        def mote_config_line(c: IrrigationConfig | None = None) -> str:
            c = c or config.get()
            return (f"CONFIG low={round(c.moisture_threshold_low)} "
                    f"high={round(c.moisture_threshold_high)} timeout={int(c.sensor_timeout)}")

        console = Path(args.log_dir) / "cooja_motes.log"
        console.parent.mkdir(parents=True, exist_ok=True)
        console.write_text("", encoding="utf-8")
        serial = sensors.add_serial_socket(args.serial_host, args.serial_port,
                                           console_log=console, greeting=mote_config_line)

        def push_config(_old, new, _source):
            line = mote_config_line(new)
            if serial.send(line):
                serial.mirror(f"<< {line}")
                log_event("SYSTEM", f"configuration pushed to controller mote: {line}")

        config.add_listener(push_config)
    if "stdin" in sources:
        sensors.add_stdin()
    filter_lines = "serial" in sources or "stdin" in sources

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    cfg = config.get()
    log_event(
        "SYSTEM",
        f"controller started: sources={sources} low={cfg.moisture_threshold_low}% "
        f"high={cfg.moisture_threshold_high}% timeout={cfg.sensor_timeout}s",
    )
    sensors.start()
    deadline = None if args.run_for is None else time.time() + args.run_for
    try:
        while not stop.is_set():
            for raw, received in sensors.poll(timeout=0.25):
                if filter_lines and not is_sensor_line(raw):
                    continue  # Contiki boot/log output
                system.process_raw(raw, received)
            config.reload_if_changed()
            system.check_timeouts()
            system.save_state()
            if deadline is not None and time.time() >= deadline:
                break
    finally:
        sensors.stop()
        system.shutdown()
        log_event("SYSTEM", "controller stopped; pump forced OFF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

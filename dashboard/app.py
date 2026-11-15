"""Streamlit dashboard for the fault-aware irrigation controller.

    streamlit run dashboard/app.py

Everything shown comes from the running controller (runtime/controller_state.json
and logs/events.jsonl); nothing is hard-coded. Configuration changes go through
NETCONF <edit-config> when a Netopeer2 server is reachable, or are written to
config/settings.json (validated) otherwise; the controller applies either within
~0.25 s.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller import DEFAULT_LOG_DIR, DEFAULT_SETTINGS_PATH, DEFAULT_STATE_PATH  # noqa: E402
from controller.config_manager import ConfigManager, ConfigValidationError  # noqa: E402
from controller.state_manager import load_state  # noqa: E402
from simulator.sensor_simulator import SCENARIOS  # noqa: E402

EVENTS_PATH = DEFAULT_LOG_DIR / "events.jsonl"
CONTROLLER_ALIVE_S = 3.0

st.set_page_config(page_title="Smart Irrigation - NETCONF", page_icon="💧", layout="wide")


# ------------------------------------------------------------ processes
class ProcessManager:
    """Keeps controller/simulator subprocesses across Streamlit reruns."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.meta: dict[str, str] = {}

    def running(self, name: str) -> bool:
        p = self.procs.get(name)
        return p is not None and p.poll() is None

    def start(self, name: str, args: list[str], label: str = "", python: bool = True) -> None:
        """Start `python <args>` (or the raw command `args` when python=False)."""
        if self.running(name):
            return
        DEFAULT_LOG_DIR.mkdir(exist_ok=True)
        out = open(DEFAULT_LOG_DIR / f"{name}_stdout.log", "w", encoding="utf-8")
        cmd = [sys.executable, *args] if python else args
        self.procs[name] = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        self.meta[name] = label

    def stop(self, name: str) -> None:
        p = self.procs.get(name)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs.pop(name, None)
        self.meta.pop(name, None)


@st.cache_resource
def process_manager() -> ProcessManager:
    return ProcessManager()


# ------------------------------------------------------- sensor sources
# One controller listens on UDP :5005 (Python simulator, Contiki-NG native node
# via simulator/contiki_bridge.py) and on Cooja's Serial Socket :60001.
CONTROLLER_ARGS = ["-m", "controller.main", "--quiet", "--source", "udp", "--source", "serial"]
SOURCES = {
    "simulator": "🐍 Python simulator",
    "native": "🟢 Contiki-NG node (native, WSL)",
    "cooja": "🖥️ Cooja GUI (sensor + controller motes)",
}
WSL = ["wsl.exe", "--"] if sys.platform == "win32" else []
NODE_LOG = DEFAULT_LOG_DIR / "contiki_node.log"
COOJA_LOG = DEFAULT_LOG_DIR / "cooja_stdout.log"
MOTES_LOG = DEFAULT_LOG_DIR / "cooja_motes.log"   # controller mote serial (written by the controller)


@st.cache_data(ttl=60, show_spinner="Checking Contiki-NG / Cooja in WSL…")
def contiki_ready() -> tuple[bool, bool, str]:
    """(native build possible, Cooja possible, checker output)."""
    try:
        out = subprocess.run([*WSL, "bash", "scripts/contiki.sh", "check"], cwd=ROOT, capture_output=True,
                             text=True, timeout=60).stdout.replace("\x00", "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, False, f"WSL not available: {exc}"
    missing = [ln for ln in out.splitlines() if "[missing]" in ln]
    native_ok = bool(out) and not any(t in ln for ln in missing for t in ("gcc", "make", "Contiki-NG"))
    return native_ok, bool(out) and not missing, out


def ensure_controller(pm: ProcessManager) -> None:
    if not controller_alive(load_state(DEFAULT_STATE_PATH)) and not pm.running("controller"):
        pm.start("controller", CONTROLLER_ARGS)
        time.sleep(0.5)


def start_source(pm: ProcessManager, kind: str, scenario: str, interval: float) -> None:
    ensure_controller(pm)
    n = SCENARIOS.index(scenario)
    if kind == "simulator":
        pm.start("simulator", ["-m", "simulator.sensor_simulator", "--scenario", scenario,
                               "--interval", str(interval)], label=f"Python simulator · {scenario} every {interval:g}s")
    elif kind == "native":
        pm.start("native", ["-m", "simulator.contiki_bridge", "--scenario", str(n)],
                 label=f"Contiki-NG node · scenario {n} ({scenario})")
    elif kind == "cooja":
        pm.start("cooja", [*WSL, "bash", "scripts/contiki.sh", "cooja", str(n)], python=False,
                 label=f"Cooja · scenario {n} ({scenario})")


def stop_source(pm: ProcessManager, kind: str) -> None:
    pm.stop(kind)
    # Stopping wsl.exe does not end the Linux processes behind it.
    pattern = {"native": "sensor_node.native", "cooja": "org.contikios.cooja"}.get(kind)
    if pattern:
        subprocess.run([*WSL, "pkill", "-f", pattern], cwd=ROOT, capture_output=True, timeout=15)


def active_source(pm: ProcessManager) -> str | None:
    return next((k for k in SOURCES if pm.running(k)), None)


def tail(path: Path, n: int = 60) -> str:
    if not path.exists():
        return ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return "".join(deque(fh, maxlen=n)).replace("\x00", "")


# ---------------------------------------------------------------- data
def controller_alive(state: dict | None) -> bool:
    return bool(state) and time.time() - float(state.get("updated_epoch", 0)) < CONTROLLER_ALIVE_S


def read_events(limit: int = 2000) -> pd.DataFrame:
    if not EVENTS_PATH.exists():
        return pd.DataFrame(columns=["timestamp", "category", "message", "data"])
    with open(EVENTS_PATH, encoding="utf-8") as fh:
        lines = deque(fh, maxlen=limit)
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "category", "message", "data"])
    if "data" not in df:
        df["data"] = None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def netconf_reachable() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 830), timeout=0.3).close()
        return True
    except OSError:
        return False


def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except AttributeError:
        return False


# ---------------------------------------------------------------- views
HEALTH_LABEL = {"healthy": "✅ Healthy", "invalid": "⚠️ Invalid", "stale": "⛔ Stale"}
PUMP_LABEL = {"on": "💧 ON", "off": "⏹ OFF"}


def moisture_chart(events: pd.DataFrame, cfg: dict, since: str | None) -> None:
    valid = events[events["category"] == "VALID_READING"].copy()
    if since:
        valid = valid[valid["timestamp"] >= pd.to_datetime(since, utc=True)]
    if valid.empty:
        st.info("No valid readings yet - start the simulator.")
        return
    valid["moisture"] = valid["data"].apply(lambda d: d.get("value") if isinstance(d, dict) else None)
    valid = valid.dropna(subset=["moisture"]).tail(200)
    valid["time"] = valid["timestamp"].dt.tz_convert(None)

    dark = is_dark()
    series = "#3987e5" if dark else "#2a78d6"
    muted = "#898781"
    grid = "#2c2c2a" if dark else "#e1e0d9"

    nearest = alt.selection_point(nearest=True, on="pointerover", fields=["time"], empty=False)
    base = alt.Chart(valid).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False, labelColor=muted)),
        y=alt.Y("moisture:Q", title="Soil moisture (%)", scale=alt.Scale(domain=[0, 100]),
                axis=alt.Axis(gridColor=grid, labelColor=muted, titleColor=muted)),
    )
    line = base.mark_line(color=series, strokeWidth=2, interpolate="step-after")
    points = base.mark_point(color=series, filled=True, size=70).encode(
        opacity=alt.condition(nearest, alt.value(1), alt.value(0))
    )
    hit = base.mark_point(size=400, opacity=0).encode(
        tooltip=[alt.Tooltip("time:T", title="Time", format="%H:%M:%S"),
                 alt.Tooltip("moisture:Q", title="Moisture %", format=".1f")]
    ).add_params(nearest)
    crosshair = base.mark_rule(color=muted, strokeWidth=1).encode(
        opacity=alt.condition(nearest, alt.value(0.6), alt.value(0))
    )
    thresholds = pd.DataFrame({
        "y": [cfg["moisture_threshold_low"], cfg["moisture_threshold_high"]],
        "label": [f"low {cfg['moisture_threshold_low']:g}% → pump ON below",
                  f"high {cfg['moisture_threshold_high']:g}% → pump OFF above"],
    })
    rules = alt.Chart(thresholds).mark_rule(color=muted, strokeDash=[4, 4], strokeWidth=1).encode(y="y:Q")
    labels = alt.Chart(thresholds).mark_text(align="left", dx=4, dy=-6, color=muted, fontSize=11).encode(
        y="y:Q", x=alt.value(0), text="label:N"
    )
    chart = (rules + labels + line + crosshair + points + hit).properties(height=280).configure_view(strokeWidth=0)
    st.altair_chart(chart, width="stretch")


def status_panel() -> None:
    state = load_state(DEFAULT_STATE_PATH)
    alive = controller_alive(state)
    settings = ConfigManager(DEFAULT_SETTINGS_PATH).get().to_dict()

    if not state:
        st.warning("Controller has never run. Start it in the sidebar.")
        return
    if not alive:
        st.error(f"⛔ Controller not running (last update {state.get('updated_at')}). "
                 "Values below are the last known state; the pump is OFF when the controller stops.")

    cfg = state.get("config") or settings
    m = state.get("soil_moisture")

    c = st.columns(4)
    c[0].metric("Soil moisture", f"{m:g} %" if m is not None else "—",
                help="Latest *valid* reading. Invalid values are never shown here.")
    c[1].metric("Sensor health", HEALTH_LABEL.get(state["sensor_status"], state["sensor_status"]))
    c[2].metric("Pump", PUMP_LABEL.get(state["pump_state"], state["pump_state"]))
    c[3].metric("Automatic control", "▶ Active" if state["auto_control"] == "active" else "⏸ Suspended")
    st.caption(f"Status detail: {state.get('status_detail', '')}")

    c = st.columns(4)
    c[0].metric("Low threshold", f"{cfg['moisture_threshold_low']:g} %")
    c[1].metric("High threshold", f"{cfg['moisture_threshold_high']:g} %")
    c[2].metric("Sensor timeout", f"{cfg['sensor_timeout']} s")
    lv = state.get("last_valid_reading")
    c[3].metric("Last valid reading",
                datetime.fromisoformat(lv.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S") if lv else "—",
                help=lv or "no valid reading yet")

    c = st.columns(6)
    c[0].metric("Valid readings", state["total_valid_readings"])
    c[1].metric("Invalid readings", state["total_invalid_readings"])
    c[2].metric("Timeout events", state["total_timeout_events"])
    c[3].metric("Missing msgs", state["total_missing_messages"])
    c[4].metric("Delayed msgs", state["total_delayed_messages"])
    c[5].metric("Recoveries", state["total_recovery_events"])

    st.subheader("Active fault alerts")
    alerts = state.get("active_alerts") or []
    if alerts:
        for a in alerts:
            st.error(f"🚨 **{a['type']}** — {a['description']}  \n_raised {a['raised_at']}_")
    else:
        st.success("✅ No active alerts")

    st.subheader("Soil moisture (valid readings)")
    moisture_chart(read_events(), cfg, state.get("started_at"))


@st.fragment(run_every=2)
def live_section() -> None:
    status_panel()


# --------------------------------------------------------------- sidebar
def sidebar() -> None:
    pm = process_manager()
    st.sidebar.title("💧 Controls")

    st.sidebar.subheader("Controller")
    state = load_state(DEFAULT_STATE_PATH)
    external = controller_alive(state) and not pm.running("controller")
    if external:
        st.sidebar.info("Controller is running outside the dashboard (e.g. a terminal).")
    elif pm.running("controller"):
        if st.sidebar.button("⏹ Stop controller", width="stretch"):
            pm.stop("controller")
            st.rerun()
    else:
        if st.sidebar.button("▶ Start controller", width="stretch"):
            pm.start("controller", CONTROLLER_ARGS)
            time.sleep(0.5)
            st.rerun()

    st.sidebar.subheader("Sensor source")
    active = active_source(pm)
    if active:
        st.sidebar.success(f"Running: {pm.meta.get(active)}")
        if active == "cooja":
            st.sidebar.caption("Cooja opens in its own window. The first start builds Cooja and "
                               "compiles the node (1-3 min); see the Contiki-NG tab for progress.")
        if st.sidebar.button("⏹ Stop sensor source", width="stretch"):
            stop_source(pm, active)
            st.rerun()
    else:
        kind = st.sidebar.radio("Source", list(SOURCES), format_func=SOURCES.get,
                                help="Python simulator: quickest. Contiki-NG node: the real sensor_node.c "
                                     "built for the native target. Cooja: the same node in the Cooja GUI.")
        scenario = st.sidebar.selectbox("Fault scenario", SCENARIOS, index=SCENARIOS.index("mixed"),
                                        help="normal · invalid (120 %) · negative (-10 %) · missing · "
                                             "delayed · intermittent · malformed · mixed")
        ready = True
        interval = 5.0
        if kind == "simulator":
            interval = st.sidebar.number_input("Report interval (s)", 1.0, 60.0, 5.0, 1.0)
        else:
            native_ok, cooja_ok, report = contiki_ready()
            ready = native_ok if kind == "native" else cooja_ok
            st.sidebar.caption("Contiki-NG reports every 5 s (set in project-conf.h).")
            if not ready:
                st.sidebar.warning("Contiki-NG/Cooja is not fully installed in WSL. "
                                   "Run **`launch.bat setup`** once, then press Recheck.")
                with st.sidebar.expander("Details"):
                    st.code(report or "no output", language=None)
                if st.sidebar.button("🔄 Recheck", width="stretch"):
                    contiki_ready.clear()
                    st.rerun()
        if st.sidebar.button("▶ Start", width="stretch", type="primary", disabled=not ready):
            start_source(pm, kind, scenario, interval)
            st.rerun()

    st.sidebar.subheader("Configuration")
    current = ConfigManager(DEFAULT_SETTINGS_PATH).get()
    nc_ok = netconf_reachable()
    method = st.sidebar.radio(
        "Apply via",
        ["NETCONF <edit-config>", "Local settings.json"],
        index=0 if nc_ok else 1,
        help="NETCONF requires netopeer2-server + netconf.adapter (WSL).",
    )
    if method.startswith("NETCONF") and not nc_ok:
        st.sidebar.warning("No NETCONF server on 127.0.0.1:830.")
    with st.sidebar.form("config"):
        low = st.number_input("Low threshold (%)", 0.0, 100.0, float(current.moisture_threshold_low), 0.5)
        high = st.number_input("High threshold (%)", 0.0, 100.0, float(current.moisture_threshold_high), 0.5)
        timeout = st.number_input("Sensor timeout (s)", 1, 300, int(current.sensor_timeout), 1)
        submitted = st.form_submit_button("Apply", width="stretch")
    if submitted:
        changes = {"moisture_threshold_low": low, "moisture_threshold_high": high, "sensor_timeout": int(timeout)}
        if method.startswith("NETCONF"):
            try:
                from netconf.netconf_client import IrrigationNetconfClient, NetconfOperationError

                with IrrigationNetconfClient() as nc:
                    nc.edit_config(**changes)
                    applied = nc.wait_until_applied(changes, timeout_s=3)
                st.sidebar.success(f"edit-config OK; controller applied: {applied}" if applied
                                   else "edit-config OK (controller confirmation pending)")
            except NetconfOperationError as exc:
                st.sidebar.error(f"Server rejected configuration: {exc}")
            except Exception as exc:
                st.sidebar.error(f"NETCONF connection failed: {exc}")
        else:
            try:
                ConfigManager(DEFAULT_SETTINGS_PATH).update(source="dashboard", **changes)
                st.sidebar.success("Saved; controller applies it within a second.")
            except ConfigValidationError as exc:
                st.sidebar.error(f"Rejected: {exc}")


def logs_tab() -> None:
    df = read_events()
    if df.empty:
        st.info("No log events yet.")
        return
    cats = sorted(df["category"].unique())
    default = [c for c in cats if c != "VALID_READING"]
    chosen = st.multiselect("Categories", cats, default=default)
    n = st.slider("Show last N events", 20, 1000, 200, 20)
    view = df[df["category"].isin(chosen)].tail(n).iloc[::-1]
    view = view.assign(time=view["timestamp"].dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3])
    st.dataframe(view[["time", "category", "message"]], width="stretch", hide_index=True, height=480)
    log_file = DEFAULT_LOG_DIR / "irrigation.log"
    if log_file.exists():
        st.download_button("Download irrigation.log", log_file.read_bytes(), "irrigation.log")


def last_mote_state() -> dict[str, str]:
    """Latest full `CTRL event=... moisture=... pump=...` line from the controller mote."""
    for line in reversed(tail(MOTES_LOG, 200).splitlines()):
        if "CTRL " in line and " health=" in line:
            body = line.split("CTRL ", 1)[1]
            return dict(kv.split("=", 1) for kv in body.split() if "=" in kv)
    return {}


def cooja_view() -> None:
    st.markdown("**Cooja network** — mote 1 *soil moisture sensor* broadcasts a report every 5 s over "
                "the simulated radio; mote 2 *irrigation controller* validates it, drives the pump "
                "(green LED; red LED = sensor fault) and reports over the Serial Socket :60001. "
                "The Python controller supervises the same readings and pushes NETCONF thresholds "
                "down to the mote (`<< CONFIG …`).")
    m = last_mote_state()
    if m:
        c = st.columns(5)
        c[0].metric("Mote moisture", f"{m.get('moisture')} %" if m.get("moisture", "-1") != "-1" else "—")
        c[1].metric("Mote sensor health", HEALTH_LABEL.get(m.get("health", ""), m.get("health", "—")))
        c[2].metric("Mote pump (LED)", PUMP_LABEL.get(m.get("pump", ""), "—"))
        c[3].metric("Mote thresholds", f"{m.get('low')}–{m.get('high')} %", help=f"timeout {m.get('timeout')} s")
        c[4].metric("Mote counters", f"{m.get('valid')} ok / {m.get('invalid')} bad",
                    help=f"missing {m.get('missing')} · old discarded {m.get('old')} · timeouts {m.get('timeouts')}")
    else:
        st.info("Waiting for the controller mote… Cooja compiles both motes on first start (1-3 min).")
    st.markdown("**Controller mote serial console** (`>>` from mote, `<<` sent to mote)")
    st.code(tail(MOTES_LOG, 40) or "(not connected to Cooja yet)", language=None)
    with st.expander("Cooja / build output"):
        st.code(tail(COOJA_LOG) or "(no output yet)", language=None)


@st.fragment(run_every=2)
def contiki_tab() -> None:
    pm = process_manager()
    active = active_source(pm)
    state = load_state(DEFAULT_STATE_PATH) or {}

    c = st.columns(4)
    c[0].metric("Sensor source", SOURCES.get(active, "—").split(" ", 1)[-1] if active else "none running")
    c[1].metric("Node", state.get("sensor_id", "—"))
    c[2].metric("Last sequence no.", state.get("last_sequence", "—"))
    c[3].metric("Last raw value", state.get("last_raw_value", "—"),
                help="Exactly what the node sent, before validation (may be invalid or malformed).")

    if active == "cooja" or (active is None and MOTES_LOG.exists() and MOTES_LOG.stat().st_size):
        cooja_view()
    else:
        st.markdown("**Contiki-NG node serial console** — `sensor_node.c` built for the native target; "
                    "lines marked `-> controller` are forwarded over UDP.")
        if active == "simulator":
            st.info("The Python simulator is the active source; start the Contiki-NG node or Cooja "
                    "from the sidebar to use the Contiki-NG firmware instead.")
        st.code(tail(NODE_LOG) or "(no output yet - start the Contiki-NG node from the sidebar)", language=None)
    st.caption("Fault scenario is compiled into the node (FAULT_SCENARIO); choosing another one rebuilds it.")


# ------------------------------------------------------------------ main
st.title("Fault-Aware Smart Irrigation — NETCONF-YANG")
sidebar()
tab_live, tab_node, tab_logs = st.tabs(["📊 Live status", "🛰️ Contiki-NG / Cooja", "📜 Logs"])
with tab_live:
    live_section()
with tab_node:
    contiki_tab()
with tab_logs:
    logs_tab()

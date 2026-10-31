# Fault-Aware Smart Irrigation Monitoring and Management Using NETCONF-YANG

A virtual smart-irrigation system that:

* simulates a soil-moisture sensor (Python **or** a Contiki-NG node in Cooja),
* detects invalid (120 %, −10 %), malformed, missing, delayed and stale readings,
* drives a **virtual pump** with hysteresis (ON < 30 %, OFF > 45 %) and **never**
  switches it on from invalid or stale data,
* is configured and monitored over **genuine NETCONF** (Netopeer2 + sysrepo +
  libyang + libnetconf2, client: ncclient) using the YANG 1.1 module
  `irrigation-monitoring`,
* shows everything on a **Streamlit** dashboard, with timestamped logs and a
  **pytest** suite.

```
sensor (Python sim / Contiki-NG in Cooja)
     │ UDP JSON  /  serial "MOISTURE node=1 seq=7 value=25"
     ▼
controller (fault detection → health → safe pump control) ──► runtime/controller_state.json
     ▲ polls config/settings.json                                   │
     │                                                              ▼
NETCONF adapter (sysrepo-python) ◄── sysrepo ◄── netopeer2-server ◄── ncclient / dashboard
```

Full design: [docs/architecture.md](docs/architecture.md) · report:
[docs/project_report.md](docs/project_report.md) · demo script:
[docs/demonstration.md](docs/demonstration.md) · test evidence:
[docs/test_results.md](docs/test_results.md)

## What is verified so far

| Part | Status |
|---|---|
| Python simulator, fault detection, pump control, logging | ✅ tested (pytest + live end-to-end run) |
| YANG model | ✅ `pyang --strict --lint` clean |
| Streamlit dashboard | ✅ loads with live controller data (Streamlit AppTest) |
| Contiki-NG node logic (`sensor_node.c`) | ✅ compiled with MSVC against host stubs; output fed into the controller. ⏳ Real Contiki-NG `native`/Cooja build not yet run |
| NETCONF (Netopeer2/sysrepo adapter, ncclient) | ⏳ code + unit tests done; **live tests need the WSL install below** |

## Project layout

```
config/settings.json        thresholds + timeout (written by NETCONF adapter / dashboard)
config/netconf.json         NETCONF connection (host, port, user, key)
yang/irrigation-monitoring.yang
controller/                 main.py (loop + core), fault_detector.py, irrigation_control.py,
                            sensor_manager.py, config_manager.py, state_manager.py, logging_setup.py
simulator/sensor_simulator.py
simulator/contiki/          sensor_node.c, project-conf.h, Makefile
netconf/                    adapter.py (sysrepo), netconf_client.py (ncclient), netconf_server.py
dashboard/app.py
tests/                      pytest suite (Tests 1-10 + extras)
scripts/                    install_wsl_deps.sh, setup_netconf.sh, start_all.sh, stop_all.sh
logs/                       irrigation.log, events.jsonl, simulator.log, netconf_adapter.log
docs/
```

## Installation (WSL2 Ubuntu)

Open **Ubuntu** (WSL) and go to the project (it lives on the Windows drive):

```bash
cd /mnt/c/Users/aadit/Documents/iot_mini_project/smart-irrigation-netconf
bash scripts/install_wsl_deps.sh        # asks for your sudo password; ~10-20 min
```

This installs the compiler, Java (Cooja), builds **libyang → libnetconf2 → sysrepo →
netopeer2** from source into `/usr/local`, clones **Contiki-NG** to `~/contiki-ng`,
and creates `.venv` with all Python packages (`--no-cooja` skips Java/Contiki).

Then the one-time NETCONF setup:

```bash
bash scripts/setup_netconf.sh           # installs YANG module, starts netopeer2-server, SSH key
```

Python-only alternative (no NETCONF, works on plain Windows too):

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Quick launch (Windows)

**Easiest:** double-click **`dashboard.bat`** (or the *Smart Irrigation Dashboard* desktop
shortcut). The web dashboard opens at http://localhost:8501; in its sidebar choose a
**sensor source** and press **Start** (the controller starts automatically):

* 🐍 **Python simulator**
* 🟢 **Contiki-NG node**: the real `sensor_node.c`, built for the native target in WSL.
  `simulator/contiki_bridge.py` forwards its readings to the controller, and the
  **Contiki-NG / Cooja** tab shows the node's live serial console.
* 🖥️ **Cooja GUI (sensor + controller motes)**: opens Cooja (WSLg window) with a
  two-mote network at real-time speed:
  * **mote 1, soil moisture sensor** (`sensor_node.c`) broadcasts
    `MOISTURE node=1 seq=N value=V` every 5 s over the simulated radio (NullNet/UDGM),
    including the chosen fault scenario;
  * **mote 2, irrigation controller** (`controller_node.c`) validates each report
    (range, malformed, out-of-order, missing, timeout) and runs hysteresis pump
    control on the mote. **Green LED = pump ON, red LED = sensor fault**, shown in the
    Network window.
  * Mote 2's serial port is exposed by the Serial Socket server on :60001. The
    Python controller reads it, supervises the same readings, and pushes the
    NETCONF thresholds down to the mote (`CONFIG low=.. high=.. timeout=..`) on connect
    and on every change. The dashboard's **Contiki-NG / Cooja** tab shows the mote's
    pump/health/thresholds and its serial console (`logs/cooja_motes.log`).

Every controller listens on both UDP :5005 and Cooja's serial socket :60001, so you
can switch sources without restarting it.

Double-click **`launch.bat`** for the full menu, or run it from a terminal:

```bat
launch.bat dashboard          :: web dashboard only (same as dashboard.bat)
launch.bat setup              :: one-time: Python venv + gcc/JDK 25/Contiki-NG/Cooja in WSL + desktop shortcuts
launch.bat python [N]         :: Python simulator  -> controller -> dashboard
launch.bat native [N]         :: Contiki-NG node (native target, WSL) in a live console window
launch.bat cooja  [N]         :: Cooja GUI with the node loaded + Serial Socket :60001 -> controller
launch.bat status | stop | test
```

`N` is the fault scenario: 0 normal, 1 invalid, 2 negative, 3 missing, 4 delayed,
5 intermittent, 6 malformed, 7 mixed (default); names work too (`launch.bat python missing`).
Each component opens in its own titled window and the dashboard opens at
http://localhost:8501. Cooja mode generates `simulator/contiki/irrigation.csc` from
`irrigation.csc.in` and autostarts it at real-time speed. The WSL side is
`scripts/contiki.sh` (`setup | check | build-native | run-native | cooja`).

## Running

Four terminals (all from the project root, `source .venv/bin/activate` first):

```bash
# 1  NETCONF adapter (needs root to reach sysrepo)
sudo .venv/bin/python -m netconf.adapter
# 2  controller
python -m controller.main                      # or --source serial for Cooja
# 3  sensor simulator
python -m simulator.sensor_simulator --scenario mixed
# 4  dashboard  -> http://localhost:8501
streamlit run dashboard/app.py
```

or `bash scripts/start_all.sh` / `bash scripts/stop_all.sh`.

NETCONF operations:

```bash
python -m netconf.netconf_client get-config
python -m netconf.netconf_client set --timeout 10
python -m netconf.netconf_client set --low 25 --high 50
python -m netconf.netconf_client get
python -m netconf.netconf_client demo          # full required demonstration
```

Simulator scenarios: `normal invalid negative missing delayed intermittent malformed mixed`
(`--interval 1` speeds things up).

## Tests

```bash
pytest                                   # unit + integration (NETCONF live tests skip)
NETCONF_TESTS=1 pytest -m netconf -v     # live NETCONF tests (server + adapter running)
pyang --strict --lint yang/irrigation-monitoring.yang
```

## Safety rules (enforced in code and tested)

* Pump ON only when sensor **healthy** and moisture < low threshold.
* Invalid / malformed / stale → pump forced OFF, automatic control suspended.
* Old or out-of-order messages are discarded and never drive the pump.
* Config is validated twice: YANG `must` (server side) and Python validator
  (high > low, 0–100 %, timeout 1–300 s).
* Controller exit → pump OFF.

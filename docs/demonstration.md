# Demonstration Guide

A step-by-step script for presenting the project. All commands run in **WSL
Ubuntu** from the project root, with the venv active:

```bash
cd /mnt/c/Users/aadit/Documents/iot_mini_project/smart-irrigation-netconf
source .venv/bin/activate
```

Prerequisite (once): `bash scripts/install_wsl_deps.sh` and `bash scripts/setup_netconf.sh`.

---

## 1. Start the system

| Terminal | Command | What it does |
|---|---|---|
| T1 | `sudo .venv/bin/python -m netconf.adapter` | sysrepo ↔ controller bridge |
| T2 | `python -m controller.main` | fault detection + pump control |
| T3 | `streamlit run dashboard/app.py` | open http://localhost:8501 in Windows |
| T4 | (commands below) | simulator and NETCONF client |

Check the server: `python -m netconf.netconf_server status`.

## 2. Fault scenarios (Python simulator)

Watch the dashboard and `tail -f logs/irrigation.log` while running each:

| Scenario | Command | Expected observation |
|---|---|---|
| Normal | `python -m simulator.sensor_simulator --scenario normal --cycles 1` | 20 % → ON, 25 % ON, 35/40 % keep ON, 50 % → OFF |
| Invalid 120 % | `... --scenario invalid --cycles 1` | Health **Invalid**, alert, pump OFF, recovers on 40 % |
| Negative −10 % | `... --scenario negative --cycles 1` | same, with −10 % |
| Missing | `... --scenario missing --cycles 1` | after 15 s **Stale**, communication-timeout alert, pump OFF; “4 message(s) missing” on resume |
| Delayed | `... --scenario delayed --cycles 1` | 3 s late reading accepted with DELAYED warning; held-back readings: stale, then discarded as out-of-order |
| Malformed | `... --scenario malformed --cycles 1` | MALFORMED_MESSAGE, alert, pump OFF |
| Intermittent | `... --scenario intermittent` | random loss and delay |
| Everything | `... --scenario mixed --cycles 1` | all of the above (~3.5 min at 5 s) |

Add `--interval 1` for a faster run. Note that with a 1 s interval the
15 s timeout is rarely reached; use `set --timeout 3` first.

The scenarios can also be started from the dashboard sidebar.

## 3. NETCONF demonstration

### 3.1 Automatic

```bash
python -m netconf.netconf_client demo
```

It performs, using genuine RPCs:
1. `<get-config>` of current settings
2. `<edit-config>` sensor-timeout **15 → 10**
3. `<get>` of `status/applied-settings`, confirming the **controller uses 10 s**
4. `<edit-config>` thresholds low 25 / high 50
5. `<edit-config>` high = 20 (≤ low), which the server **rejects** with an `<rpc-error>`
6. `<get>` of the full operational status
7. restoring the original settings

### 3.2 Manual, step by step

```bash
python -m netconf.netconf_client get-config            # shows XML + parsed values
python -m netconf.netconf_client set --timeout 10
grep CONFIG_UPDATE logs/irrigation.log | tail -1       # controller log: 15 -> 10
python -m netconf.netconf_client get                   # applied-settings: sensor-timeout 10
```

Prove the controller really times out at 10 s: send one reading and stop.

```bash
python -m simulator.sensor_simulator --scenario normal --cycles 1 --interval 1 && sleep 12
grep TIMEOUT logs/irrigation.log | tail -1             # "... (timeout 10s)"
```

Thresholds and the safety rule:

```bash
python -m netconf.netconf_client set --low 25 --high 50
python -m netconf.netconf_client set --high 20         # rejected: must-violation
```

### 3.3 Raw NETCONF with netopeer2-cli (optional)

```text
$ netopeer2-cli
> connect --host 127.0.0.1 --login $USER
> get-config --source running --filter-xpath /irrigation-monitoring:irrigation
> get --filter-xpath /irrigation-monitoring:irrigation/status
> edit-config --target running --config=/tmp/timeout10.xml
```

with `/tmp/timeout10.xml`:

```xml
<irrigation xmlns="urn:example:irrigation-monitoring">
  <settings><sensor-timeout>10</sensor-timeout></settings>
</irrigation>
```

## 4. Contiki-NG and Cooja

### 4.1 Without Cooja: native target

```bash
cd simulator/contiki
make TARGET=native                       # FAULT_SCENARIO=0..7 to choose a scenario
cd ../..
./simulator/contiki/build/native/sensor_node.native | python -m controller.main --source stdin
```

The node prints one `MOISTURE node=… seq=… value=…` line every 5 s in real time.

### 4.2 Cooja GUI

> **Automatic:** `launch.bat cooja` (or the dashboard's *Cooja GUI* source) opens the
> ready-made two-mote simulation: sensor mote 1 → radio → controller mote 2
> (green LED = pump, red LED = fault) → Serial Socket :60001 → Python controller.
> The manual steps below build a single sensor mote by hand.

1. Start Cooja: `cd ~/contiki-ng/tools/cooja && ./gradlew run`.
   WSLg shows the window on Windows 11.
2. **File → New simulation…**, name it `irrigation`, then **Create**.
3. **Motes → Add motes → Create new mote type → Cooja mote**.
4. **Browse** to
   `/mnt/c/Users/aadit/Documents/iot_mini_project/smart-irrigation-netconf/simulator/contiki/sensor_node.c`,
   click **Compile**, then **Create**, and add **1** mote.
5. Right-click the mote → **Mote tools for Cooja 1 → Serial Socket (SERVER)**,
   set port **60001**, click **Start**.
6. Set **Speed limit → 100 %** (real time). The controller uses wall-clock
   timeouts, so faster-than-real-time simulation would look like delays.
7. Press **Start** in the Simulation control.
8. In a terminal: `python -m controller.main --source serial --serial-port 60001`.
9. The Mote output window and `logs/irrigation.log` show the same readings, and
   the dashboard updates.

To change the fault pattern, edit `FAULT_SCENARIO` in
`simulator/contiki/project-conf.h` (0 normal … 7 mixed) and recompile the mote
type (**Motes → Reload**).

> Save the simulation (**File → Save simulation as…**) as
> `simulator/contiki/irrigation.csc` to reopen it later.

## 5. Running the tests during the demo

```bash
pytest -v                                # 108 tests, live NETCONF tests skipped
NETCONF_TESTS=1 pytest -m netconf -v     # with server + adapter (+ controller) running
```

## 6. Viva questions to be ready for

* *Why can't an invalid reading turn the pump on?* `decide_pump_state` returns
  ON only for status healthy **and** a finite value in 0–100. This is checked by
  32 parametrised tests (`test_invalid_or_stale_never_turns_pump_on`).
* *Where is high > low enforced?* In three places: the YANG `must` statement
  (sysrepo rejects the edit), the adapter's veto in the `change` event, and
  `validate_config` in Python.
* *Why the adapter and not a NETCONF server written in Python?* Netopeer2 is the
  reference open-source NETCONF server. The adapter is the standard
  sysrepo "application" pattern.
* *What happens to a message delayed beyond the timeout?* It is discarded
  (stale-data / out-of-order) and counted as delayed. Only fresh data refreshes
  health.

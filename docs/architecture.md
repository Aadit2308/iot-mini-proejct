# System Architecture

## 1. Component view

```mermaid
flowchart LR
    subgraph Sensors["Sensor layer"]
        PS["Python simulator<br/>simulator/sensor_simulator.py<br/>UDP JSON :5005"]
        CN["Contiki-NG node (Cooja)<br/>simulator/contiki/sensor_node.c<br/>serial text"]
    end

    subgraph Controller["Controller (Python) - controller/"]
        SM["sensor_manager<br/>UDP / Serial-Socket / stdin"]
        FD["fault_detector<br/>parse + validate"]
        CORE["main.IrrigationSystem<br/>health · alerts · timeout"]
        IC["irrigation_control<br/>hysteresis + safety"]
        ST["state_manager<br/>runtime/controller_state.json"]
        CM["config_manager<br/>config/settings.json"]
        LG["logging_setup<br/>logs/*.log, events.jsonl"]
    end

    subgraph NETCONF["NETCONF management plane (WSL)"]
        NP["netopeer2-server<br/>SSH :830"]
        SR[("sysrepo<br/>running datastore")]
        AD["netconf/adapter.py<br/>sysrepo-python"]
    end

    CL["ncclient<br/>netconf/netconf_client.py"]
    DB["Streamlit dashboard<br/>dashboard/app.py"]

    PS -- UDP --> SM
    CN -- "Cooja Serial Socket TCP :60001" --> SM
    SM --> FD --> CORE --> IC
    CORE --> ST
    CORE --> LG
    CM --> CORE
    CL -- "get / get-config / edit-config" --> NP
    DB -- "edit-config" --> NP
    NP <--> SR
    SR -- "module-change callback" --> AD
    AD -- "oper-data callback" --> SR
    AD -- "validated write" --> CM
    ST -- "read" --> AD
    ST -- "read" --> DB
    LG -- "read" --> DB
```

## 2. Why this split

* **The controller has no NETCONF dependency.** It reads `settings.json` and
  writes a JSON state snapshot. It runs (and is fully tested) on any OS.
* **Netopeer2 is the NETCONF server.** It implements the protocol (SSH
  transport, `<hello>`, `<get>`, `<get-config>`, `<edit-config>`, `<rpc-error>`),
  and sysrepo stores the configuration and validates it against the YANG model
  (types, ranges, `must`).
* **The adapter connects sysrepo to the controller.** It subscribes to:
  * *module changes* on `/irrigation-monitoring:irrigation/settings`. On the
    `change` event it validates with the controller's own validator and can
    veto. On `done` it writes `settings.json`.
  * *operational data requests* on `/irrigation-monitoring:irrigation/status`.
    It answers `<get>` from the controller's live snapshot.
  * It also emits a `sensor-health-change` notification when health changes.

## 3. Configuration change sequence (edit-config)

```mermaid
sequenceDiagram
    participant C as ncclient
    participant N as netopeer2-server
    participant S as sysrepo
    participant A as adapter
    participant F as settings.json
    participant K as controller
    C->>N: <edit-config> sensor-timeout=10
    N->>S: apply changes (running)
    S->>S: YANG validation (range 1..300, must high>low)
    S->>A: module-change event "change"
    A->>A: validate_config()  (veto -> rpc-error)
    S-->>N: OK
    N-->>C: <ok/>
    S->>A: event "done"
    A->>F: atomic write {sensor_timeout: 10}
    K->>F: mtime poll (every 0.25 s)
    K->>K: CONFIG_UPDATE logged, new timeout used
    C->>N: <get> status/applied-settings
    N->>S: oper request
    S->>A: oper callback
    A-->>S: applied-settings/sensor-timeout = 10
    N-->>C: <data> ... 10 ...
```

## 4. Sensor health state machine

```mermaid
stateDiagram-v2
    [*] --> Stale: start (awaiting first reading)
    Stale --> Healthy: valid reading
    Healthy --> Invalid: reading outside 0-100 % / malformed
    Invalid --> Healthy: valid reading (RECOVERY, alerts cleared)
    Healthy --> Stale: no valid reading for sensor-timeout s
    Invalid --> Stale: no valid reading for sensor-timeout s
    Stale --> Stale: invalid reading (stays stale, alert added)
    note right of Stale
        pump OFF, auto control suspended,
        communication-timeout alert
    end note
    note right of Invalid
        pump OFF, auto control suspended,
        invalid-reading / malformed alert
    end note
```

## 5. Message validation pipeline

```mermaid
flowchart TD
    M[raw message] --> P{parse OK?}
    P -- no --> MAL[MALFORMED<br/>invalid count +1, alert, status invalid]
    P -- yes --> R{0 ≤ moisture ≤ 100?}
    R -- no --> INV[INVALID<br/>invalid count +1, alert, status invalid]
    R -- yes --> O{seq > last seq?}
    O -- no --> OOO[OUT-OF-ORDER<br/>delayed count +1, discarded]
    O -- yes --> A{age ≤ timeout?}
    A -- no --> SD[STALE-DATA<br/>delayed count +1, discarded]
    A -- yes --> V[VALID<br/>gap → missing count, latency > 2 s → delayed warning]
    V --> H[status healthy, clear alerts, refresh timer]
    MAL --> PC[pump control]
    INV --> PC
    H --> PC
```

## 6. Pump control

```mermaid
flowchart TD
    S{sensor healthy?} -- no --> OFF1[Pump OFF<br/>auto control suspended]
    S -- yes --> L{moisture < low?}
    L -- yes --> ON[Pump ON]
    L -- no --> Hh{moisture > high?}
    Hh -- yes --> OFF2[Pump OFF]
    Hh -- no --> KEEP[Keep previous state]
```

## 7. Files and data contracts

| File | Writer | Readers | Content |
|---|---|---|---|
| `config/settings.json` | adapter, dashboard (local mode) | controller | 3 validated config values |
| `runtime/controller_state.json` | controller (every 0.25 s) | adapter, dashboard | full status snapshot |
| `logs/events.jsonl` | controller | dashboard | one JSON event per line |
| `logs/irrigation.log` | controller | humans | timestamped text log |
| `logs/netconf_adapter.log` | adapter | humans | NETCONF-side events |
| `logs/simulator.log` | simulator | humans | what was sent/dropped/delayed |

## 8. Wire formats

Python simulator (UDP datagram):

```json
{"sensor_id": "node-1", "seq": 7, "moisture": 25.0, "timestamp": 1793476800.123, "boot_id": "a1b2c3d4"}
```

Contiki-NG node (serial line):

```
MOISTURE node=1 seq=7 value=25
```

`boot_id` (or a sequence number jumping back by more than 20) marks a sensor
restart, so the sequence check starts again.

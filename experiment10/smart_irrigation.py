"""
Experiment 10: Smart Irrigation using NETCONF and YANG
=======================================================
Single-file simulation of a smart irrigation system.

Contents:
  1. YANG data model, compiled at startup with pyang.
  2. Datastore class whose rules all come from the compiled model
     (ranges, enums, defaults, must rules, config vs state leaves).
  3. IrrigationController with the hysteresis control logic.
  4. NetconfServer exposing get-config / edit-config / get.
  5. NetconfClient that drives the server.
  6. SoilMoistureSensor generating sample readings.
  7. main() wiring everything together and running the
     observation-table test cases.

Requires:  pip install pyang lxml
Run:       python smart_irrigation.py
"""

import xml.etree.ElementTree as ET
from xml.dom import minidom

from lxml import etree
from pyang import context, error, repository


# ---------------------------------------------------------------
# 1. YANG MODEL
# ---------------------------------------------------------------
YANG_MODEL = """\
module smart-irrigation {
  yang-version 1.1;
  namespace "urn:example:smart-irrigation";
  prefix si;

  container irrigation {

    must "moisture-threshold-low < moisture-threshold-high" {
      error-message
        "moisture-threshold-low must be less than moisture-threshold-high";
    }

    leaf irrigation-mode {
      type enumeration {
        enum automatic;
        enum manual;
      }
      default automatic;
    }

    leaf moisture-threshold-low {
      type uint8 { range "0..100"; }
      default 30;
    }

    leaf moisture-threshold-high {
      type uint8 { range "0..100"; }
      default 45;
    }

    leaf soil-moisture {
      config false;
      type uint8 { range "0..100"; }
    }

    leaf pump-state {
      config false;
      type enumeration {
        enum on;
        enum off;
      }
    }
  }
}
"""


class YangSchema:
    """The YANG model compiled by pyang. Answers questions about its leaves."""

    def __init__(self, yang_text):
        ctx = context.Context(repository.FileRepository("", use_env=False))
        module = ctx.add_module("smart-irrigation.yang", yang_text, in_format="yang")
        ctx.validate()
        problems = [f"line {pos.line}: {error.err_to_str(tag, args)}"
                    for pos, tag, args in ctx.errors
                    if error.is_error(error.err_level(tag))]
        if module is None or problems:
            raise ValueError("invalid YANG model: " + "; ".join(problems))
        container = module.search_one("container", "irrigation")
        self.leaves = {leaf.arg: leaf for leaf in container.i_children}
        self.musts = container.search("must")

    def check_musts(self, config):
        """Evaluate the model's must XPath expressions against a config."""
        root = etree.Element("irrigation")
        for leaf, value in config.items():
            etree.SubElement(root, leaf).text = str(value)
        for must in self.musts:
            if not root.xpath(must.arg):
                raise ValueError(must.search_one("error-message").arg)

    def is_config(self, leaf):
        return self._leaf(leaf).i_config

    def type_name(self, leaf):
        return self._leaf(leaf).search_one("type").arg

    def default(self, leaf):
        stmt = self._leaf(leaf).search_one("default")
        return None if stmt is None else self.parse(leaf, stmt.arg)

    def parse(self, leaf, value):
        """Check value against the leaf's YANG type; return the typed value."""
        stmt = self._leaf(leaf)
        spec = stmt.search_one("type").i_type_spec
        errors = []
        parsed = spec.str_to_val(errors, stmt.pos, str(value), stmt.i_module)
        if parsed is None or not spec.validate(errors, stmt.pos, parsed, stmt.i_module):
            detail = "; ".join(error.err_to_str(tag, args)
                               for _, tag, args in errors)
            raise ValueError(f"{leaf}: {detail}")
        return parsed

    def _leaf(self, leaf):
        if leaf not in self.leaves:
            raise ValueError(f"unknown leaf: {leaf}")
        return self.leaves[leaf]


# ---------------------------------------------------------------
# 2. DATASTORE (enforces the YANG constraints)
# ---------------------------------------------------------------
class YangDatastore:
    """In-memory datastore for the smart-irrigation module."""

    def __init__(self, schema: YangSchema):
        self.schema = schema
        # Configuration (defaults from the YANG model)
        self._config = {leaf: schema.default(leaf)
                        for leaf in schema.leaves if schema.is_config(leaf)}
        # Operational state
        self._state = {}
        self.set_state("soil-moisture", 0)
        self.set_state("pump-state", "off")

    # ---- config accessors --------------------------------------------
    def set_config(self, leaf, value):
        if not self.schema.is_config(leaf):
            raise ValueError(f"{leaf} is not a configurable leaf")
        candidate = {**self._config, leaf: self.schema.parse(leaf, value)}
        self.schema.check_musts(candidate)
        self._config = candidate

    def get_config(self):
        return dict(self._config)

    # ---- operational accessors ---------------------------------------
    def set_state(self, leaf, value):
        if self.schema.is_config(leaf):
            raise ValueError(f"{leaf} is not a state leaf")
        self._state[leaf] = self.schema.parse(leaf, value)

    def get_all(self):
        merged = {}
        merged.update(self._config)
        merged.update(self._state)
        return merged


# ---------------------------------------------------------------
# 3. IRRIGATION CONTROLLER (hysteresis logic from Part B)
# ---------------------------------------------------------------
class IrrigationController:
    """Reads config + current moisture, decides pump-state."""

    def __init__(self, datastore: YangDatastore):
        self.ds = datastore

    def process_reading(self, moisture: int) -> str:
        """Push a new moisture reading through the control logic."""
        self.ds.set_state("soil-moisture", moisture)

        cfg = self.ds.get_config()
        mode = cfg["irrigation-mode"]
        low = cfg["moisture-threshold-low"]
        high = cfg["moisture-threshold-high"]
        prev_pump = self.ds.get_all()["pump-state"]

        if mode == "automatic":
            if moisture < low:
                new_pump = "on"
            elif moisture >= high:
                new_pump = "off"
            else:
                new_pump = prev_pump      # hysteresis band
        else:
            new_pump = prev_pump          # manual mode: no change here

        self.ds.set_state("pump-state", new_pump)
        return new_pump


# ---------------------------------------------------------------
# 4. NETCONF SERVER (XML-encoded operations)
# ---------------------------------------------------------------
NS = "urn:example:smart-irrigation"
ET.register_namespace("si", NS)


def _pretty(elem) -> str:
    raw = ET.tostring(elem, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ").strip()


class NetconfServer:
    """Mimics a NETCONF server that implements the smart-irrigation model."""

    def __init__(self, datastore: YangDatastore):
        self.ds = datastore

    # ---- <get-config> -------------------------------------------------
    def get_config(self) -> str:
        irrig = ET.Element(f"{{{NS}}}irrigation")
        for k, v in self.ds.get_config().items():
            ET.SubElement(irrig, f"{{{NS}}}{k}").text = str(v)
        rpc_reply = ET.Element("rpc-reply")
        data = ET.SubElement(rpc_reply, "data")
        data.append(irrig)
        return _pretty(rpc_reply)

    # ---- <edit-config> ------------------------------------------------
    def edit_config(self, leaf: str, value) -> str:
        rpc_reply = ET.Element("rpc-reply")
        try:
            self.ds.set_config(leaf, value)
            ET.SubElement(rpc_reply, "ok")
        except ValueError as exc:
            err = ET.SubElement(rpc_reply, "rpc-error")
            ET.SubElement(err, "error-message").text = str(exc)
        return _pretty(rpc_reply)

    # ---- <get> --------------------------------------------------------
    def get(self) -> str:
        irrig = ET.Element(f"{{{NS}}}irrigation")
        for k, v in self.ds.get_all().items():
            ET.SubElement(irrig, f"{{{NS}}}{k}").text = str(v)
        rpc_reply = ET.Element("rpc-reply")
        data = ET.SubElement(rpc_reply, "data")
        data.append(irrig)
        return _pretty(rpc_reply)


# ---------------------------------------------------------------
# 5. NETCONF CLIENT
# ---------------------------------------------------------------
class NetconfClient:
    def __init__(self, server: NetconfServer):
        self.server = server

    def get_config(self):
        print("\n--> CLIENT: <get-config>")
        print(self.server.get_config())

    def edit_config(self, leaf, value):
        print(f"\n--> CLIENT: <edit-config> {leaf} = {value}")
        print(self.server.edit_config(leaf, value))

    def get(self):
        print("\n--> CLIENT: <get>")
        print(self.server.get())


# ---------------------------------------------------------------
# 6. SOIL MOISTURE SENSOR
# ---------------------------------------------------------------
class SoilMoistureSensor:
    """Yields pre-programmed moisture readings (simulating a Cooja mote)."""

    def __init__(self, readings):
        self.readings = list(readings)

    def __iter__(self):
        return iter(self.readings)


# ---------------------------------------------------------------
# 7. MAIN
# ---------------------------------------------------------------
def banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def print_schema(schema):
    header = f"{'Leaf':<26}{'Kind':<8}{'Type':<14}{'Default':<10}"
    print(header)
    print("-" * len(header))
    for leaf in schema.leaves:
        kind = "config" if schema.is_config(leaf) else "state"
        default = schema.default(leaf)
        print(f"{leaf:<26}{kind:<8}{schema.type_name(leaf):<14}"
              f"{'-' if default is None else default:<10}")


def run_test_cases(controller, cases, title):
    banner(title)
    header = f"{'Test':<5}{'Moisture':<10}{'Low':<6}{'High':<6}{'Prev':<6}{'Pump':<6}"
    print(header)
    print("-" * len(header))
    for i, (moisture, prev) in enumerate(cases, 1):
        # Force the previous pump state before applying the reading
        controller.ds.set_state("pump-state", prev)
        cfg = controller.ds.get_config()
        new_pump = controller.process_reading(moisture)
        print(f"{i:<5}{str(moisture)+'%':<10}"
              f"{str(cfg['moisture-threshold-low'])+'%':<6}"
              f"{str(cfg['moisture-threshold-high'])+'%':<6}"
              f"{prev:<6}{new_pump:<6}")


def main():
    banner("YANG MODEL (smart-irrigation)")
    print(YANG_MODEL)

    # ---- Set up the system ----
    schema = YangSchema(YANG_MODEL)
    banner("Schema compiled from the YANG model by pyang")
    print_schema(schema)

    datastore = YangDatastore(schema)
    controller = IrrigationController(datastore)
    server = NetconfServer(datastore)
    client = NetconfClient(server)

    # ---- Part A/B: run the sensor stream through the controller ----
    banner("Part A + B — Sensor stream through controller (defaults 30/45)")
    sensor = SoilMoistureSensor([25, 35, 40, 48, 55])
    datastore.set_state("pump-state", "off")   # start OFF
    for reading in sensor:
        pump = controller.process_reading(reading)
        cfg = datastore.get_config()
        print(f"  moisture={reading:>3}%  low={cfg['moisture-threshold-low']}%  "
              f"high={cfg['moisture-threshold-high']}%  pump={pump.upper()}")

    # ---- Part D: NETCONF operations ----
    banner("Part D — NETCONF operations")
    client.get_config()                                       # initial config
    client.edit_config("moisture-threshold-low", 25)          # change low 30 -> 25
    client.get_config()                                       # verify change
    client.get()                                              # operational data

    # Edits the YANG model must reject
    banner("Edits rejected by the YANG model")
    client.edit_config("moisture-threshold-low", 150)         # outside range
    client.edit_config("moisture-threshold-low", "abc")       # not a uint8
    client.edit_config("irrigation-mode", "eco")              # not an enum value
    client.edit_config("soil-moisture", 50)                   # config false
    client.edit_config("flow-rate", 5)                        # not in the model
    client.edit_config("moisture-threshold-low", 60)          # low above high
    client.edit_config("moisture-threshold-high", 25)         # high equal to low

    # Feed a new reading and verify the updated threshold is used
    banner("Reading after threshold change (low = 25%)")
    pump = controller.process_reading(28)
    print(f"  moisture=28%  low=25%  high=45%  pump={pump.upper()}  "
          "(28 >= 25, so pump keeps its previous state)")

    # ---- Observation-table test cases ----
    # Each tuple: (moisture, previous pump state)
    # Tests 1-4 use defaults (30/45); tests 5-6 use low=25
    datastore.set_config("moisture-threshold-low", 30)
    cases_default = [(25, "off"), (35, "off"), (48, "on"), (22, "off")]
    run_test_cases(controller, cases_default,
                   "Observation Table — Tests 1-4 (low=30%, high=45%)")

    datastore.set_config("moisture-threshold-low", 25)
    cases_updated = [(28, "on"), (50, "on")]
    run_test_cases(controller, cases_updated,
                   "Observation Table — Tests 5-6 (low=25%, high=45%)")

    banner("Simulation complete")


if __name__ == "__main__":
    main()

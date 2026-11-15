"""NETCONF client for the irrigation controller (ncclient over SSH).

Real NETCONF RPCs against netopeer2-server: <get-config>, <get>, <edit-config>.

Connection settings come from config/netconf.json, overridden by env vars:
    NETCONF_HOST, NETCONF_PORT, NETCONF_USER, NETCONF_PASSWORD, NETCONF_KEY

Usage (from project root, inside WSL):

    python -m netconf.netconf_client get-config
    python -m netconf.netconf_client get
    python -m netconf.netconf_client set --timeout 10
    python -m netconf.netconf_client set --low 25 --high 50
    python -m netconf.netconf_client demo          # required demonstration
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netconf import KEY_TO_LEAF, LEAF_TO_KEY, NAMESPACE, YANG_DEFAULTS  # noqa: E402

NS = {"im": NAMESPACE}
NETCONF_SETTINGS_PATH = ROOT / "config" / "netconf.json"

SETTINGS_FILTER = f'<irrigation xmlns="{NAMESPACE}"><settings/></irrigation>'
STATUS_FILTER = f'<irrigation xmlns="{NAMESPACE}"><status/></irrigation>'

_COUNTERS = {
    "total-valid-readings", "total-invalid-readings", "total-timeout-events",
    "total-missing-messages", "total-delayed-messages", "total-recovery-events",
}
_DECIMALS = {"soil-moisture", "moisture-threshold-low", "moisture-threshold-high"}


class NetconfOperationError(RuntimeError):
    """An RPC returned <rpc-error> (e.g. edit-config rejected)."""


# ------------------------------------------------------------------ xml
def build_settings_config(**changes: Any) -> str:
    """<config> payload for edit-config. Keys use settings.json names."""
    unknown = set(changes) - set(KEY_TO_LEAF)
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")
    config = etree.Element("config", nsmap={None: "urn:ietf:params:xml:ns:netconf:base:1.0"})
    irrigation = etree.SubElement(config, f"{{{NAMESPACE}}}irrigation", nsmap={None: NAMESPACE})
    settings = etree.SubElement(irrigation, f"{{{NAMESPACE}}}settings")
    for key, value in changes.items():
        leaf = etree.SubElement(settings, f"{{{NAMESPACE}}}{KEY_TO_LEAF[key]}")
        leaf.text = str(int(value)) if key == "sensor_timeout" else f"{float(value):.1f}"
    return etree.tostring(config, encoding="unicode")


def _root(xml: str | bytes) -> etree._Element:
    if isinstance(xml, str):
        xml = xml.encode()
    return etree.fromstring(xml)


def parse_settings(xml: str | bytes) -> dict[str, Any]:
    """Parse <data> from get-config. Absent leaves take their YANG default."""
    settings = _root(xml).find(".//im:irrigation/im:settings", NS)
    out = dict(YANG_DEFAULTS)
    if settings is not None:
        for child in settings:
            leaf = etree.QName(child).localname
            key = LEAF_TO_KEY.get(leaf)
            if key:
                out[key] = int(child.text) if key == "sensor_timeout" else float(child.text)
    return out


def _convert(leaf: str, text: str | None) -> Any:
    if text is None:
        return None
    if leaf in _COUNTERS or leaf == "sensor-timeout":
        return int(text)
    if leaf in _DECIMALS:
        return float(text)
    if leaf == "fault-alert":
        return text == "true"
    return text


def parse_status(xml: str | bytes) -> dict[str, Any]:
    """Parse <data> from <get> into a dict keyed by YANG leaf names."""
    status = _root(xml).find(".//im:irrigation/im:status", NS)
    out: dict[str, Any] = {}
    if status is None:
        return out
    alerts = []
    for child in status:
        name = etree.QName(child).localname
        if name == "active-alert":
            alerts.append({etree.QName(c).localname: c.text for c in child})
        elif name == "applied-settings":
            out[name] = {etree.QName(c).localname: _convert(etree.QName(c).localname, c.text)
                         for c in child}
        else:
            out[name] = _convert(name, child.text)
    out["active-alert"] = alerts
    return out


# --------------------------------------------------------------- client
def load_connection_params(path: Path = NETCONF_SETTINGS_PATH) -> dict[str, Any]:
    params: dict[str, Any] = {
        "host": "127.0.0.1", "port": 830, "username": None,
        "key_filename": None, "hostkey_verify": False, "timeout": 10,
    }
    if path.exists():
        params.update(json.loads(path.read_text()))
    env = {
        "host": os.environ.get("NETCONF_HOST"),
        "port": os.environ.get("NETCONF_PORT"),
        "username": os.environ.get("NETCONF_USER"),
        "password": os.environ.get("NETCONF_PASSWORD"),
        "key_filename": os.environ.get("NETCONF_KEY"),
    }
    params.update({k: v for k, v in env.items() if v})
    params["port"] = int(params["port"])
    if not params.get("username"):
        params["username"] = getpass.getuser()
    if params.get("key_filename"):
        params["key_filename"] = os.path.expanduser(params["key_filename"])
    return params


class IrrigationNetconfClient:
    """Thin wrapper around an ncclient session."""

    def __init__(self, **overrides: Any):
        self.params = {**load_connection_params(), **{k: v for k, v in overrides.items() if v is not None}}
        self.m = None

    def connect(self) -> "IrrigationNetconfClient":
        from ncclient import manager

        p = self.params
        self.m = manager.connect(
            host=p["host"],
            port=p["port"],
            username=p["username"],
            password=p.get("password"),
            key_filename=p.get("key_filename"),
            hostkey_verify=bool(p.get("hostkey_verify", False)),
            allow_agent=True,
            look_for_keys=True,
            timeout=p.get("timeout", 10),
        )
        return self

    def close(self) -> None:
        if self.m is not None:
            self.m.close_session()
            self.m = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    @property
    def capabilities(self) -> list[str]:
        return list(self.m.server_capabilities)

    def supports_module(self) -> bool:
        return any(NAMESPACE in c or "irrigation-monitoring" in c for c in self.capabilities)

    def _call(self, fn, *args, **kwargs):
        from ncclient.operations import RPCError

        try:
            return fn(*args, **kwargs)
        except RPCError as exc:
            msg = exc.message or str(exc)
            tag = getattr(exc, "tag", "")
            app_tag = ""
            if exc.xml is not None:
                el = exc.xml.find(".//{urn:ietf:params:xml:ns:netconf:base:1.0}error-app-tag")
                app_tag = el.text if el is not None else ""
            raise NetconfOperationError(f"{tag} {app_tag}: {msg}".strip()) from exc

    def get_config_xml(self) -> str:
        return self._call(self.m.get_config, source="running", filter=("subtree", SETTINGS_FILTER)).data_xml

    def get_config(self) -> dict[str, Any]:
        return parse_settings(self.get_config_xml())

    def get_status_xml(self) -> str:
        return self._call(self.m.get, filter=("subtree", STATUS_FILTER)).data_xml

    def get_status(self) -> dict[str, Any]:
        return parse_status(self.get_status_xml())

    def edit_config(self, **changes: Any) -> None:
        payload = build_settings_config(**changes)
        self._call(self.m.edit_config, target="running", config=payload, default_operation="merge")

    def wait_until_applied(self, expected: dict[str, Any], timeout_s: float = 5.0) -> dict | None:
        """Poll <get> until status/applied-settings matches ``expected``
        (settings.json keys). Returns applied-settings or None on timeout."""
        deadline = time.time() + timeout_s
        applied = None
        while time.time() < deadline:
            applied = self.get_status().get("applied-settings")
            if applied:
                as_keys = {LEAF_TO_KEY[k]: v for k, v in applied.items()}
                if all(abs(float(as_keys[k]) - float(v)) < 1e-6 for k, v in expected.items()):
                    return applied
            time.sleep(0.25)
        return None


# ------------------------------------------------------------------ cli
def _print_status(st: dict) -> None:
    for k, v in st.items():
        if k == "active-alert":
            print(f"  {k}: {len(v)} active")
            for a in v:
                print(f"    - {a.get('type')}: {a.get('description')} (since {a.get('raised-at')})")
        else:
            print(f"  {k}: {v}")


def run_demo(client: IrrigationNetconfClient) -> int:
    print("== 1. <get-config> current settings")
    original = client.get_config()
    print("  ", original)

    print("\n== 2. <edit-config> sensor-timeout 15 -> 10")
    if original["sensor_timeout"] != 15:
        client.edit_config(sensor_timeout=15)
        client.wait_until_applied({"sensor_timeout": 15})
    client.edit_config(sensor_timeout=10)
    print("   get-config now:", client.get_config())

    print("\n== 3. Verify the controller applied 10 s (status/applied-settings via <get>)")
    applied = client.wait_until_applied({"sensor_timeout": 10})
    if applied is None:
        print("   FAILED: controller did not report sensor-timeout=10 (is controller + adapter running?)")
        return 1
    print("   applied-settings:", applied)

    print("\n== 4. <edit-config> thresholds low=25 high=50")
    client.edit_config(moisture_threshold_low=25, moisture_threshold_high=50)
    applied = client.wait_until_applied({"moisture_threshold_low": 25, "moisture_threshold_high": 50})
    print("   applied-settings:", applied)

    print("\n== 5. <edit-config> invalid: high (20) <= low (25) - must be rejected")
    try:
        client.edit_config(moisture_threshold_high=20)
        print("   UNEXPECTED: server accepted invalid configuration")
        return 1
    except NetconfOperationError as exc:
        print("   rejected by server as expected:", exc)
    print("   get-config unchanged:", client.get_config())

    print("\n== 6. <get> operational status")
    _print_status(client.get_status())

    print("\n== 7. restore original settings")
    client.edit_config(**original)
    print("  ", client.get_config())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="NETCONF client for the irrigation controller")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--user", dest="username")
    p.add_argument("--key", dest="key_filename")
    p.add_argument("--ask-password", action="store_true", help="prompt for SSH password")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get-config", help="<get-config> settings from running")
    sub.add_parser("get", help="<get> operational status")
    sub.add_parser("capabilities", help="list server capabilities")
    s = sub.add_parser("set", help="<edit-config> settings")
    s.add_argument("--low", type=float)
    s.add_argument("--high", type=float)
    s.add_argument("--timeout", type=int)
    s.add_argument("--xml", action="store_true", help="only print the edit-config payload")
    sub.add_parser("demo", help="run the full NETCONF demonstration")
    args = p.parse_args(argv)

    if args.cmd == "set":
        changes = {k: v for k, v in {
            "moisture_threshold_low": args.low,
            "moisture_threshold_high": args.high,
            "sensor_timeout": args.timeout,
        }.items() if v is not None}
        if not changes:
            p.error("set: give at least one of --low/--high/--timeout")
        if args.xml:
            print(build_settings_config(**changes))
            return 0

    password = getpass.getpass("NETCONF SSH password: ") if args.ask_password else None
    client = IrrigationNetconfClient(host=args.host, port=args.port, username=args.username,
                                     key_filename=args.key_filename, password=password)
    try:
        client.connect()
    except Exception as exc:
        print(f"Could not connect to NETCONF server at {client.params['host']}:{client.params['port']} "
              f"as {client.params['username']}: {exc}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "capabilities":
            for c in client.capabilities:
                print(c)
        elif args.cmd == "get-config":
            print(client.get_config_xml())
            print(json.dumps(client.get_config(), indent=2))
        elif args.cmd == "get":
            _print_status(client.get_status())
        elif args.cmd == "set":
            client.edit_config(**changes)
            print("edit-config OK. running config:", client.get_config())
            applied = client.wait_until_applied(changes, timeout_s=3)
            print("controller applied-settings:", applied if applied else "not confirmed (controller running?)")
        elif args.cmd == "demo":
            return run_demo(client)
    except NetconfOperationError as exc:
        print(f"NETCONF error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

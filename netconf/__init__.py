"""NETCONF-YANG integration: Netopeer2/sysrepo adapter and ncclient client."""

MODULE = "irrigation-monitoring"
NAMESPACE = "urn:example:irrigation-monitoring"
SETTINGS_XPATH = f"/{MODULE}:irrigation/settings"
STATUS_XPATH = f"/{MODULE}:irrigation/status"
NOTIFICATION_XPATH = f"/{MODULE}:sensor-health-change"

# YANG leaf name <-> settings.json key
LEAF_TO_KEY = {
    "moisture-threshold-low": "moisture_threshold_low",
    "moisture-threshold-high": "moisture_threshold_high",
    "sensor-timeout": "sensor_timeout",
}
KEY_TO_LEAF = {v: k for k, v in LEAF_TO_KEY.items()}
YANG_DEFAULTS = {
    "moisture_threshold_low": 30.0,
    "moisture_threshold_high": 45.0,
    "sensor_timeout": 15,
}

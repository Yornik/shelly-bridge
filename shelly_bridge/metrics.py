"""Map Shelly Gen2 replies onto Prometheus metrics.

Field names and units come from the Gen2 API docs (Switch, Sys, WiFi, Cloud,
Mqtt, Input) and from real Plus 1PM replies on firmware 1.1.0 and 1.7.5. Every
reading is optional: each successful poll builds a complete new set of series,
so a field the device stops reporting disappears instead of keeping its last
value. When polls keep failing, the whole set disappears once it is older than
a few poll intervals (Readings.max_age), and the health metrics say why.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, disable_created_metrics
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric

from .api import DeviceInfo
from .energy import WH_TO_JOULES, EnergyCounter

# A *_created series next to every counter only doubles the series count.
disable_created_metrics()

_D = ("device",)
_S = ("device", "switch")


@dataclass(frozen=True)
class Family:
    kind: str  # "gauge" or "counter"
    help: str
    labels: tuple[str, ...]


# Every device series the bridge can publish. Counters are named with their
# _total suffix, as they appear in queries.
FAMILIES: dict[str, Family] = {
    "shelly_device_info": Family(
        "gauge",
        "Always 1; carries the device's model, generation, app and firmware.",
        ("device", "model", "gen", "app", "version", "fw_id"),
    ),
    # --- switch: relay and metering ---
    "shelly_switch_output": Family("gauge", "1 while the relay is on, 0 while it is off.", _S),
    "shelly_switch_source_info": Family(
        "gauge",
        "Always 1; source names what last changed the relay, as the device reports it "
        "(init, button, http, timer, WS_in, ...).",
        ("device", "switch", "source"),
    ),
    "shelly_switch_power_watts": Family("gauge", "Active power in W.", _S),
    "shelly_switch_voltage_volts": Family("gauge", "Supply voltage in V.", _S),
    "shelly_switch_current_amperes": Family("gauge", "Current in A.", _S),
    "shelly_switch_frequency_hertz": Family(
        "gauge", "Supply frequency in Hz. Only on devices that report it.", _S
    ),
    "shelly_switch_power_factor": Family(
        "gauge", "Power factor. Only on devices that report it.", _S
    ),
    "shelly_switch_temperature_celsius": Family(
        "gauge", "Temperature inside the device in degrees Celsius.", _S
    ),
    "shelly_switch_energy_joules_total": Family(
        "counter",
        "Energy used since the bridge started, in J. Kept by the bridge: it only "
        "grows, also when the device's own total goes back (see the README).",
        _S,
    ),
    "shelly_switch_returned_energy_joules_total": Family(
        "counter",
        "Energy returned to the grid since the bridge started, in J. Only on devices "
        "that report it.",
        _S,
    ),
    "shelly_switch_device_energy_joules": Family(
        "gauge",
        "The device's own energy total (aenergy.total) in J. Goes back to 0 when the "
        "device's counter is reset; use shelly_switch_energy_joules_total for increase().",
        _S,
    ),
    "shelly_switch_energy_resets_total": Family(
        "counter",
        "Times the device's own energy total went back since the bridge started.",
        _S,
    ),
    "shelly_switch_error": Family(
        "gauge",
        "1 while the device reports this protection error on the switch, else 0.",
        ("device", "switch", "error"),
    ),
    "shelly_switch_power_limit_watts": Family(
        "gauge", "Power above which the device turns the relay off, in W.", _S
    ),
    "shelly_switch_voltage_limit_volts": Family(
        "gauge", "Voltage above which the device turns the relay off, in V.", _S
    ),
    "shelly_switch_current_limit_amperes": Family(
        "gauge", "Current above which the device turns the relay off, in A.", _S
    ),
    "shelly_input_state": Family(
        "gauge", "1 while the switch input (SW terminal) is on, else 0.", ("device", "input")
    ),
    # --- system ---
    "shelly_sys_uptime_seconds": Family("gauge", "Seconds since the device started.", _D),
    "shelly_sys_reset_reason": Family(
        "gauge",
        "Why the device last started, as the raw ESP-IDF code: 1 power-on, 3 software "
        "restart, 4 panic, 5-7 watchdog, 9 brownout.",
        _D,
    ),
    "shelly_sys_restart_required": Family(
        "gauge", "1 if the device needs a restart to apply a setting.", _D
    ),
    "shelly_sys_update_available": Family(
        "gauge",
        "1 if the device offers a firmware update on this stage (stable, beta).",
        ("device", "stage"),
    ),
    "shelly_sys_ram_size_bytes": Family("gauge", "Total RAM in bytes.", _D),
    "shelly_sys_ram_free_bytes": Family("gauge", "Free RAM in bytes.", _D),
    "shelly_sys_fs_size_bytes": Family("gauge", "Total file system size in bytes.", _D),
    "shelly_sys_fs_free_bytes": Family("gauge", "Free file system space in bytes.", _D),
    "shelly_sys_config_revision": Family(
        "gauge", "Configuration revision; goes up with every settings change.", _D
    ),
    # --- network ---
    "shelly_wifi_rssi_dbm": Family("gauge", "Wi-Fi signal strength in dBm.", _D),
    "shelly_cloud_connected": Family("gauge", "1 if the device is connected to Shelly Cloud.", _D),
    "shelly_mqtt_connected": Family("gauge", "1 if the device is connected to an MQTT broker.", _D),
}

# --- bridge health: always published, without a device label ---
# Its value is worked out at scrape time (Readings.scrape_success, below), so
# a poll that hangs shows as down once the last good poll is too old.
SCRAPE_SUCCESS = Gauge(
    "shelly_scrape_success",
    "1 if the most recent poll of the device succeeded and is recent enough to serve readings.",
)
LAST_SUCCESS = Gauge(
    "shelly_last_success_timestamp_seconds", "Unix time of the most recent successful poll."
)
SCRAPE_DURATION = Gauge("shelly_scrape_duration_seconds", "How long the most recent poll took.")
ERROR_REASONS = ("timeout", "connection", "auth", "http", "invalid_response")
SCRAPE_ERRORS = Counter(
    "shelly_scrape_errors_total",
    "Failed polls, by reason: timeout, connection, auth, http or invalid_response.",
    ("reason",),
)

# Documented protection errors. Anything else the device sends becomes "other",
# so the label stays bounded.
SWITCH_ERRORS = ("overtemp", "overpower", "overvoltage", "undervoltage")
UPDATE_STAGES = ("stable", "beta")
# What a change source may look like before it becomes a label value.
_SOURCE = re.compile(r"[A-Za-z0-9_.:-]{1,32}")
_COMPONENT = re.compile(r"(?P<kind>[a-z]+):(?P<id>\d{1,3})")


def init_health() -> None:
    """Create every error reason at 0, so rate() works from the first failure."""
    for reason in ERROR_REASONS:
        SCRAPE_ERRORS.labels(reason)


init_health()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def flag(value: Any) -> float | None:
    """Booleans arrive as JSON true/false; tolerate 0/1 as well."""
    if isinstance(value, bool):
        return float(value)
    result = number(value)
    return None if result is None else float(result != 0)


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def components(status: Mapping[str, Any], kind: str) -> Iterator[tuple[int, dict]]:
    """The `kind:N` entries of a Shelly.GetStatus reply, such as switch:0."""
    for key, value in status.items():
        match = _COMPONENT.fullmatch(key) if isinstance(key, str) else None
        if match and match["kind"] == kind and isinstance(value, dict):
            yield int(match["id"]), value


def source_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if _SOURCE.fullmatch(value) else "other"


class Snapshot:
    """One poll's worth of device series."""

    def __init__(self) -> None:
        self.samples: dict[str, list[tuple[tuple[str, ...], float]]] = {}

    def add(self, name: str, value: float | None, *labels: str) -> None:
        if len(labels) != len(FAMILIES[name].labels):
            raise ValueError(f"{name} takes {len(FAMILIES[name].labels)} labels")
        if value is not None:
            self.samples.setdefault(name, []).append((labels, float(value)))

    def families(self) -> Iterator[Metric]:
        for name, family in FAMILIES.items():
            samples = self.samples.get(name)
            if not samples:
                continue
            metric = _empty(name, family)
            for labels, value in samples:
                metric.add_metric(list(labels), value)
            yield metric


def _empty(name: str, family: Family) -> Metric:
    cls = CounterMetricFamily if family.kind == "counter" else GaugeMetricFamily
    return cls(name, family.help, labels=list(family.labels))


def build_snapshot(
    device: str,
    info: DeviceInfo,
    status: Mapping[str, Any],
    switch_configs: Mapping[int, Mapping[str, Any]],
    energy: Mapping[tuple[int, str], EnergyCounter],
) -> Snapshot:
    snap = Snapshot()
    snap.add(
        "shelly_device_info", 1, device, info.model, info.gen, info.app, info.version, info.fw_id
    )

    sys = as_dict(status.get("sys"))
    snap.add("shelly_sys_uptime_seconds", number(sys.get("uptime")), device)
    snap.add("shelly_sys_reset_reason", number(sys.get("reset_reason")), device)
    snap.add("shelly_sys_restart_required", flag(sys.get("restart_required")), device)
    for name, key in (
        ("shelly_sys_ram_size_bytes", "ram_size"),
        ("shelly_sys_ram_free_bytes", "ram_free"),
        ("shelly_sys_fs_size_bytes", "fs_size"),
        ("shelly_sys_fs_free_bytes", "fs_free"),
        ("shelly_sys_config_revision", "cfg_rev"),
    ):
        snap.add(name, number(sys.get(key)), device)
    updates = sys.get("available_updates")
    if isinstance(updates, dict):
        for stage in UPDATE_STAGES:
            offered = bool(as_dict(updates.get(stage)).get("version"))
            snap.add("shelly_sys_update_available", float(offered), device, stage)

    snap.add("shelly_wifi_rssi_dbm", number(as_dict(status.get("wifi")).get("rssi")), device)
    snap.add("shelly_cloud_connected", flag(as_dict(status.get("cloud")).get("connected")), device)
    snap.add("shelly_mqtt_connected", flag(as_dict(status.get("mqtt")).get("connected")), device)

    for input_id, component in components(status, "input"):
        snap.add("shelly_input_state", flag(component.get("state")), device, str(input_id))

    for switch_id, sw in components(status, "switch"):
        labels = (device, str(switch_id))
        snap.add("shelly_switch_output", flag(sw.get("output")), *labels)
        source = source_label(sw.get("source"))
        if source is not None:
            snap.add("shelly_switch_source_info", 1, *labels, source)
        for name, key in (
            ("shelly_switch_power_watts", "apower"),
            ("shelly_switch_voltage_volts", "voltage"),
            ("shelly_switch_current_amperes", "current"),
            ("shelly_switch_frequency_hertz", "freq"),
            ("shelly_switch_power_factor", "pf"),
        ):
            snap.add(name, number(sw.get(key)), *labels)
        snap.add(
            "shelly_switch_temperature_celsius",
            number(as_dict(sw.get("temperature")).get("tC")),
            *labels,
        )

        total_wh = number(as_dict(sw.get("aenergy")).get("total"))
        counter = energy.get((switch_id, "aenergy"))
        if total_wh is not None and total_wh >= 0:
            snap.add("shelly_switch_device_energy_joules", total_wh * WH_TO_JOULES, *labels)
            if counter is not None:
                snap.add("shelly_switch_energy_joules_total", counter.joules, *labels)
                snap.add("shelly_switch_energy_resets_total", counter.resets, *labels)
        returned_wh = number(as_dict(sw.get("ret_aenergy")).get("total"))
        returned = energy.get((switch_id, "ret_aenergy"))
        if returned_wh is not None and returned_wh >= 0 and returned is not None:
            snap.add("shelly_switch_returned_energy_joules_total", returned.joules, *labels)

        # The errors key is only there while at least one error is active.
        raw_errors = sw.get("errors")
        active = {str(e) for e in raw_errors} if isinstance(raw_errors, list) else set()
        for error in SWITCH_ERRORS:
            snap.add("shelly_switch_error", float(error in active), *labels, error)
        snap.add("shelly_switch_error", float(bool(active - set(SWITCH_ERRORS))), *labels, "other")

        config = switch_configs.get(switch_id) or {}
        for name, key in (
            ("shelly_switch_power_limit_watts", "power_limit"),
            ("shelly_switch_voltage_limit_volts", "voltage_limit"),
            ("shelly_switch_current_limit_amperes", "current_limit"),
        ):
            snap.add(name, number(config.get(key)), *labels)
    return snap


class Readings:
    """Serves the latest snapshot while it is fresh; a collector in REGISTRY.

    Freshness is checked when Prometheus scrapes, not when a poll fails, so the
    readings also disappear if polling stops altogether.
    """

    def __init__(self, max_age: float = 45.0, clock=time.time) -> None:
        self.max_age = max_age
        self.clock = clock
        self._current: tuple[Snapshot, float] | None = None
        self._last_poll_ok = False

    def publish(self, snapshot: Snapshot, taken_at: float) -> None:
        """A poll succeeded and read this."""
        self._current = (snapshot, taken_at)
        self._last_poll_ok = True

    def poll_failed(self) -> None:
        self._last_poll_ok = False

    def clear(self) -> None:
        self._current = None
        self._last_poll_ok = False

    def _fresh(self) -> Snapshot | None:
        current = self._current
        if current is None or self.clock() - current[1] > self.max_age:
            return None
        return current[0]

    def scrape_success(self) -> float:
        """shelly_scrape_success: the last poll worked and its readings are still served."""
        return 1.0 if self._last_poll_ok and self._fresh() is not None else 0.0

    def collect(self) -> Iterator[Metric]:
        snapshot = self._fresh()
        if snapshot is not None:
            yield from snapshot.families()

    def describe(self) -> Iterator[Metric]:
        for name, family in FAMILIES.items():
            yield _empty(name, family)


READINGS = Readings()
REGISTRY.register(READINGS)
SCRAPE_SUCCESS.set_function(READINGS.scrape_success)

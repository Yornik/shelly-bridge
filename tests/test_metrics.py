from __future__ import annotations

import copy
import time

import pytest
from conftest import DEVICE_INFO, STATUS_ALL, SWITCH_CONFIG, device_series, sample

from shelly_bridge import metrics
from shelly_bridge.api import DeviceInfo
from shelly_bridge.energy import EnergyCounter

INFO = DeviceInfo.from_reply(DEVICE_INFO)
SW = {"device": "boiler", "switch": "0"}


def publish(status, configs=None, energy=None, taken_at=None):
    energy = {} if energy is None else energy
    snapshot = metrics.build_snapshot(
        "boiler", INFO, status, {0: SWITCH_CONFIG} if configs is None else configs, energy
    )
    metrics.READINGS.publish(snapshot, time.time() if taken_at is None else taken_at)


def test_maps_a_plus_1pm_status(status):
    counter = EnergyCounter()
    counter.observe(1234.567)
    publish(status, energy={(0, "aenergy"): counter})

    assert (
        sample(
            "shelly_device_info",
            device="boiler",
            model="SNSW-001P16EU",
            gen="2",
            app="Plus1PM",
            version="1.7.5",
            fw_id="20260311-095847/1.7.5-g9979d16",
        )
        == 1
    )
    assert sample("shelly_switch_output", **SW) == 1
    assert sample("shelly_switch_source_info", **SW, source="SHC") == 1
    assert sample("shelly_switch_power_watts", **SW) == 1103.4
    assert sample("shelly_switch_voltage_volts", **SW) == 231.2
    assert sample("shelly_switch_current_amperes", **SW) == 4.771
    assert sample("shelly_switch_temperature_celsius", **SW) == 48.5
    assert sample("shelly_switch_device_energy_joules", **SW) == pytest.approx(1234.567 * 3600)
    assert sample("shelly_switch_energy_joules_total", **SW) == 0
    assert sample("shelly_switch_energy_resets_total", **SW) == 0
    for error in metrics.SWITCH_ERRORS + ("other",):
        assert sample("shelly_switch_error", **SW, error=error) == 0
    assert sample("shelly_switch_power_limit_watts", **SW) == 4480
    assert sample("shelly_switch_voltage_limit_volts", **SW) == 280
    assert sample("shelly_switch_current_limit_amperes", **SW) == 16
    assert sample("shelly_input_state", device="boiler", input="0") == 0

    dev = {"device": "boiler"}
    assert sample("shelly_sys_uptime_seconds", **dev) == 3912345
    assert sample("shelly_sys_reset_reason", **dev) == 1
    assert sample("shelly_sys_restart_required", **dev) == 0
    assert sample("shelly_sys_update_available", **dev, stage="stable") == 0
    assert sample("shelly_sys_update_available", **dev, stage="beta") == 0
    assert sample("shelly_sys_ram_size_bytes", **dev) == 250600
    assert sample("shelly_sys_ram_free_bytes", **dev) == 126500
    assert sample("shelly_sys_fs_size_bytes", **dev) == 393216
    assert sample("shelly_sys_fs_free_bytes", **dev) == 122880
    assert sample("shelly_sys_config_revision", **dev) == 31
    assert sample("shelly_wifi_rssi_dbm", **dev) == -60
    assert sample("shelly_cloud_connected", **dev) == 1
    assert sample("shelly_mqtt_connected", **dev) == 0


def test_plus_1pm_does_not_report_frequency_power_factor_or_returned_energy(status):
    publish(status, energy={(0, "aenergy"): EnergyCounter()})
    names = device_series()
    assert "shelly_switch_frequency_hertz" not in names
    assert "shelly_switch_power_factor" not in names
    assert "shelly_switch_returned_energy_joules_total" not in names


def test_optional_fields_when_reported():
    returned = EnergyCounter()
    returned.observe(12.5)
    publish(STATUS_ALL, energy={(0, "aenergy"): EnergyCounter(), (0, "ret_aenergy"): returned})
    assert sample("shelly_switch_frequency_hertz", **SW) == 50.0
    assert sample("shelly_switch_power_factor", **SW) == 0.98
    assert sample("shelly_switch_returned_energy_joules_total", **SW) == 0
    assert sample("shelly_switch_error", **SW, error="overtemp") == 1
    assert sample("shelly_switch_error", **SW, error="overpower") == 0
    assert sample("shelly_sys_update_available", device="boiler", stage="stable") == 1
    assert sample("shelly_sys_update_available", device="boiler", stage="beta") == 0
    assert sample("shelly_sys_restart_required", device="boiler") == 1


def test_every_family_appears_with_all_fields():
    counters = {(0, "aenergy"): EnergyCounter(), (0, "ret_aenergy"): EnergyCounter()}
    for counter, total in zip(counters.values(), (1.0, 2.0), strict=True):
        counter.observe(total)
    publish(STATUS_ALL, energy=counters)
    exposed = {family.name for family in metrics.READINGS.collect()}
    expected = {name.removesuffix("_total") for name in metrics.FAMILIES}
    assert exposed == expected


def test_a_field_that_disappears_removes_its_series(status):
    publish(status)
    assert sample("shelly_switch_power_watts", **SW) == 1103.4
    del status["switch:0"]["apower"]
    status["switch:0"]["temperature"]["tC"] = None
    publish(status)
    assert sample("shelly_switch_power_watts", **SW) is None
    assert sample("shelly_switch_temperature_celsius", **SW) is None
    assert sample("shelly_switch_voltage_volts", **SW) == 231.2


def test_missing_sections_do_not_raise_or_publish_zeroes():
    publish({}, configs={})
    names = device_series()
    assert names == {"shelly_device_info"}


@pytest.mark.parametrize(
    "junk",
    [None, "n/a", "NaN", float("inf"), [], {}, True],
)
def test_junk_values_are_dropped(status, junk):
    status["switch:0"]["apower"] = junk
    status["sys"]["uptime"] = junk
    publish(status)
    assert sample("shelly_switch_power_watts", **SW) is None
    assert sample("shelly_sys_uptime_seconds", device="boiler") is None


def test_flags_accept_numbers():
    assert metrics.flag(1) == 1.0
    assert metrics.flag(0) == 0.0
    assert metrics.flag(False) == 0.0
    assert metrics.flag("maybe") is None


def test_source_label_is_bounded(status):
    status["switch:0"]["source"] = "x" * 100
    publish(status)
    assert sample("shelly_switch_source_info", **SW, source="other") == 1
    status["switch:0"]["source"] = "button"
    publish(status)
    assert sample("shelly_switch_source_info", **SW, source="button") == 1
    assert sample("shelly_switch_source_info", **SW, source="other") is None


def test_unknown_error_becomes_other(status):
    status["switch:0"]["errors"] = ["cooling_down", "overvoltage"]
    publish(status)
    assert sample("shelly_switch_error", **SW, error="overvoltage") == 1
    assert sample("shelly_switch_error", **SW, error="other") == 1


def test_only_numbered_components_count(status):
    status["switch:x"] = {"output": True}
    status["switch:1"] = "not an object"
    status["switch:2"] = {"output": False}
    publish(status, configs={})
    assert sample("shelly_switch_output", device="boiler", switch="2") == 0
    assert sample("shelly_switch_output", device="boiler", switch="1") is None
    # Without Switch.GetConfig, no limits are published.
    assert sample("shelly_switch_power_limit_watts", **SW) is None


def test_readings_disappear_once_too_old(status):
    now = [1000.0]
    metrics.READINGS.clock = lambda: now[0]
    metrics.READINGS.max_age = 45
    publish(status, taken_at=1000.0)
    now[0] = 1045.0
    assert sample("shelly_switch_output", **SW) == 1
    now[0] = 1045.1
    assert sample("shelly_switch_output", **SW) is None
    assert device_series() == set()


def test_labels_must_match_the_family():
    snapshot = metrics.Snapshot()
    with pytest.raises(ValueError):
        snapshot.add("shelly_switch_output", 1, "only-device")


def test_health_metrics_have_every_reason_from_the_start():
    for reason in metrics.ERROR_REASONS:
        assert sample("shelly_scrape_errors_total", reason=reason) == 0


def test_a_status_is_not_mutated(status):
    before = copy.deepcopy(status)
    publish(status)
    assert status == before

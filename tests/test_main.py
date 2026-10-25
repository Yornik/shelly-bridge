from __future__ import annotations

import contextlib
import copy
import logging
import threading
import time

import pytest
from conftest import DEVICE_ID, DEVICE_INFO, PASSWORD, STATUS, SWITCH_CONFIG, device_series, sample

from shelly_bridge import __main__ as app
from shelly_bridge import metrics
from shelly_bridge.api import (
    DeviceInfo,
    ReadOnlyViolation,
    ShellyAuthError,
    ShellyClient,
    ShellyHTTPError,
    ShellyTimeout,
    ShellyUnreachable,
)

SW = {"device": DEVICE_ID, "switch": "0"}


class FakeClient:
    """Stands in for ShellyClient; every reply can be changed between polls."""

    def __init__(self):
        self.info = copy.deepcopy(DEVICE_INFO)
        self.status = copy.deepcopy(STATUS)
        self.config = copy.deepcopy(SWITCH_CONFIG)
        self.error: Exception | None = None
        self.calls: list[str] = []
        self.time_limits: list[float] = []

    @contextlib.contextmanager
    def time_limit(self, seconds):
        self.time_limits.append(seconds)
        yield

    def _call(self, name):
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    def get_device_info(self):
        self._call("info")
        return DeviceInfo.from_reply(self.info)

    def get_status(self):
        self._call("status")
        return copy.deepcopy(self.status)

    def get_switch_config(self, switch_id):
        self._call(f"config:{switch_id}")
        return copy.deepcopy(self.config)

    def set_energy(self, total_wh, uptime=None):
        self.status["switch:0"]["aenergy"]["total"] = total_wh
        if uptime is not None:
            self.status["sys"]["uptime"] = uptime


@pytest.fixture
def clock():
    """One clock for the poller and for the freshness check at scrape time."""
    now = [1000.0]
    metrics.READINGS.clock = lambda: now[0]
    return now


def poller_for(client, clock, **kwargs):
    poller = app.Poller(client, poll_interval=15, clock=lambda: clock[0], **kwargs)
    metrics.READINGS.max_age = poller.stale_after
    return poller


def test_successful_poll(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    assert poller.poll_once() is True
    assert fake.calls == ["info", "status", "config:0"]
    assert sample("shelly_scrape_success") == 1
    assert sample("shelly_last_success_timestamp_seconds") == 1000.0
    assert sample("shelly_scrape_duration_seconds") >= 0
    assert sample("shelly_switch_power_watts", **SW) == 1103.4
    assert sample("shelly_switch_energy_joules_total", **SW) == 0

    clock[0] = 1015.0
    fake.calls.clear()
    assert poller.poll_once() is True
    assert fake.calls == ["status"]


def test_configured_name_is_the_device_label(clock):
    poller = poller_for(FakeClient(), clock, device_name="boiler")
    poller.poll_once()
    assert sample("shelly_switch_output", device="boiler", switch="0") == 1
    assert sample("shelly_switch_output", **SW) is None


def test_unreachable_device_keeps_readings_for_a_grace_period_then_drops_them(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    assert poller.ready()

    fake.error = ShellyTimeout("Shelly.GetDeviceInfo timed out after 5s")
    clock[0] = 1015.0
    assert poller.poll_once() is False
    assert sample("shelly_scrape_success") == 0
    assert sample("shelly_scrape_errors_total", reason="timeout") == 1
    assert sample("shelly_last_success_timestamp_seconds") == 1000.0
    # Still within 3 poll intervals of the last good poll.
    clock[0] = 1045.0
    assert sample("shelly_switch_power_watts", **SW) == 1103.4
    assert poller.ready()
    clock[0] = 1045.5
    assert sample("shelly_switch_power_watts", **SW) is None
    assert device_series() == set()
    assert not poller.ready()
    # Health stays.
    assert sample("shelly_scrape_success") == 0


def test_not_ready_before_the_first_good_poll(clock):
    fake = FakeClient()
    fake.error = ShellyUnreachable("Shelly.GetDeviceInfo failed: ConnectionError")
    poller = poller_for(fake, clock)
    assert poller.poll_once() is False
    assert not poller.ready()
    assert device_series() == set()


def test_outage_is_logged_on_change_not_on_every_poll(clock, caplog):
    caplog.set_level(logging.INFO, logger="shelly-bridge")
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    caplog.clear()

    fake.error = ShellyUnreachable("Shelly.GetStatus failed: ConnectionError")
    for i in range(1, 6):
        clock[0] = 1000.0 + 15 * i
        poller.poll_once()
    assert len(caplog.records) == 1
    assert "cannot read the device: Shelly.GetStatus failed" in caplog.text

    # Another reason is counted, not logged.
    fake.error = ShellyTimeout("Shelly.GetDeviceInfo timed out after 5s")
    clock[0] += 15
    poller.poll_once()
    assert len(caplog.records) == 1

    clock[0] += app.REMINDER_SECONDS
    poller.poll_once()
    assert len(caplog.records) == 2
    assert caplog.records[-1].getMessage() == (
        "still cannot read the device after 11m (7 failed polls; since the last line: "
        "connection 4, timeout 2); latest: Shelly.GetDeviceInfo timed out after 5s"
    )

    caplog.clear()
    fake.error = None
    clock[0] += 15
    assert poller.poll_once() is True
    assert [r.getMessage() for r in caplog.records] == [
        "the device answers again after 11m (7 failed polls)"
    ]


def test_reasons_that_take_turns_do_not_flood_the_log(clock, caplog):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    caplog.clear()
    errors = [
        ShellyTimeout("Shelly.GetStatus timed out after 5s"),
        ShellyUnreachable("Shelly.GetStatus failed: ConnectionError"),
        ShellyHTTPError("Shelly.GetStatus answered HTTP 503"),
    ]
    # 15 minutes of polls, every one failing for another reason than the last.
    for i in range(60):
        fake.error = errors[i % 3]
        clock[0] += 15
        poller.poll_once()
    assert sample("shelly_scrape_errors_total", reason="timeout") == 20
    assert len(caplog.records) == 2
    assert "41 failed polls; since the last line: connection 14, http 13, timeout 13" in caplog.text


def test_a_wrong_password_during_an_outage_is_logged_at_once(clock, caplog):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.error = ShellyUnreachable("Shelly.GetDeviceInfo failed: ConnectionError")
    poller.poll_once()
    fake.error = ShellyAuthError("the device refused the password (HTTP 401)")
    clock[0] += 15
    poller.poll_once()
    assert len(caplog.records) == 2
    assert "latest: the device refused the password (HTTP 401); check SHELLY_PASSWORD" in (
        caplog.records[-1].getMessage()
    )


def test_a_poll_that_hangs_shows_as_down_once_its_readings_are_stale(clock):
    poller = poller_for(FakeClient(), clock)
    poller.poll_once()
    # No poll finishes after this one, as if the next one never returned.
    clock[0] = 1045.0
    assert sample("shelly_scrape_success") == 1
    clock[0] = 1045.5
    assert sample("shelly_scrape_success") == 0
    assert device_series() == set()
    assert not poller.ready()


def test_every_poll_runs_under_one_time_limit(clock):
    fake = FakeClient()
    poller = app.Poller(fake, poll_interval=15, poll_budget=7, clock=lambda: clock[0])
    poller.poll_once()
    assert fake.time_limits == [7]
    assert app.Poller(fake, poll_interval=15).poll_budget == 15


def test_device_info_is_read_again_after_an_outage(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    fake.error = ShellyTimeout("x")
    clock[0] = 1015.0
    poller.poll_once()
    fake.error = None
    fake.calls.clear()
    clock[0] = 1030.0
    poller.poll_once()
    assert fake.calls == ["info", "status", "config:0"]


def test_device_info_and_limits_are_refreshed_periodically(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    fake.calls.clear()
    fake.config["power_limit"] = 2500
    clock[0] = 1000.0 + app.INFO_REFRESH_SECONDS
    poller.poll_once()
    assert fake.calls == ["info", "status", "config:0"]
    assert sample("shelly_switch_power_limit_watts", **SW) == 2500


def test_a_switch_that_disappears_takes_its_series_and_limits_along(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    poller.poll_once()
    del fake.status["switch:0"]
    clock[0] += 15
    assert poller.poll_once() is True
    assert poller.switch_configs == {}
    assert not any(name.startswith("shelly_switch_") for name in device_series())
    assert sample("shelly_sys_uptime_seconds", device=DEVICE_ID) is not None


def test_auth_error_backs_off_and_names_the_variable(clock, caplog):
    fake = FakeClient()
    fake.error = ShellyAuthError("the device refused the password (HTTP 401)")
    poller = poller_for(fake, clock)
    assert poller.poll_once() is False
    assert sample("shelly_scrape_errors_total", reason="auth") == 1
    assert "check SHELLY_PASSWORD" in caplog.text
    clock[0] = 1000.0 + app.AUTH_BACKOFF_SECONDS - 1
    assert poller.poll_once() is False
    assert fake.calls == ["info"]
    clock[0] = 1000.0 + app.AUTH_BACKOFF_SECONDS
    poller.poll_once()
    assert fake.calls == ["info", "info"]


def test_an_unexpected_error_is_an_invalid_response(clock):
    fake = FakeClient()
    fake.error = KeyError("surprise")
    poller = poller_for(fake, clock)
    assert poller.poll_once() is False
    assert sample("shelly_scrape_errors_total", reason="invalid_response") == 1


def test_read_only_violation_is_not_swallowed(clock):
    fake = FakeClient()
    fake.error = ReadOnlyViolation("refusing to call Switch.Set")
    with pytest.raises(ReadOnlyViolation):
        poller_for(fake, clock).poll_once()


def test_counter_reset_on_device_restart_keeps_the_export_growing(clock, caplog):
    caplog.set_level(logging.INFO, logger="shelly-bridge")
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0, uptime=5000)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 0

    clock[0] += 15
    fake.set_energy(1001.0, uptime=5015)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 3600

    # The device restarts and its counter was reset.
    clock[0] += 15
    fake.calls.clear()
    fake.set_energy(0.25, uptime=10)
    fake.status["sys"]["reset_reason"] = 3
    poller.poll_once()
    assert fake.calls == ["status", "info", "config:0"]
    assert sample("shelly_switch_energy_joules_total", **SW) == 3600
    assert sample("shelly_switch_energy_resets_total", **SW) == 1
    assert sample("shelly_switch_device_energy_joules", **SW) == 900
    assert "the device restarted 10s ago (reset reason: software restart)" in caplog.text
    assert "went back from 1001.000 Wh to 0.250 Wh" in caplog.text

    clock[0] += 15
    fake.set_energy(0.75, uptime=25)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 3600 + 1800


def test_restart_with_a_persisted_counter_is_not_a_reset(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0, uptime=5000)
    poller.poll_once()
    clock[0] += 60
    fake.set_energy(1000.5, uptime=3)
    fake.status["sys"]["reset_reason"] = 1
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 1800
    assert sample("shelly_switch_energy_resets_total", **SW) == 0


def test_small_rollback_after_a_power_cut_adds_nothing(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0, uptime=5000)
    poller.poll_once()
    clock[0] += 60
    fake.set_energy(999.9, uptime=4)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 0
    clock[0] += 15
    fake.set_energy(1000.1, uptime=19)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == pytest.approx(720)


def test_energy_used_during_an_outage_is_counted_when_it_answers_again(clock):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0)
    poller.poll_once()
    fake.error = ShellyTimeout("x")
    for _ in range(10):
        clock[0] += 15
        poller.poll_once()
    fake.error = None
    fake.set_energy(1002.0)
    clock[0] += 15
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 7200


def test_returned_energy_counter(clock):
    fake = FakeClient()
    fake.status["switch:0"]["ret_aenergy"] = {"total": 5.0}
    poller = poller_for(fake, clock)
    poller.poll_once()
    fake.status["switch:0"]["ret_aenergy"] = {"total": 5.5}
    clock[0] += 15
    poller.poll_once()
    assert sample("shelly_switch_returned_energy_joules_total", **SW) == 1800


def test_firmware_change_is_picked_up_after_the_update_restart(clock, caplog):
    caplog.set_level(logging.INFO, logger="shelly-bridge")
    fake = FakeClient()
    fake.info.update(ver="1.7.4", fw_id="20251001-000000/1.7.4-gabcdef0")
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0, uptime=5000)
    poller.poll_once()
    assert sample("shelly_device_info", **_info_labels("1.7.4", "20251001-000000/1.7.4-gabcdef0"))

    fake.info.update(ver="1.7.5", fw_id="20260311-095847/1.7.5-g9979d16")
    fake.set_energy(1000.0, uptime=30)
    clock[0] += 60
    poller.poll_once()
    assert "firmware changed from 1.7.4 to 1.7.5" in caplog.text
    assert sample("shelly_device_info", **_info_labels("1.7.5", "20260311-095847/1.7.5-g9979d16"))
    assert (
        sample("shelly_device_info", **_info_labels("1.7.4", "20251001-000000/1.7.4-gabcdef0"))
        is None
    )


def _info_labels(version, fw_id):
    return {
        "device": DEVICE_ID,
        "model": "SNSW-001P16EU",
        "gen": "2",
        "app": "Plus1PM",
        "version": version,
        "fw_id": fw_id,
    }


def test_a_different_device_at_the_address_starts_its_counters_over(clock, caplog):
    fake = FakeClient()
    poller = poller_for(fake, clock)
    fake.set_energy(1000.0)
    poller.poll_once()
    clock[0] += 15
    fake.set_energy(1001.0)
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 3600

    other = "shellyplus1pm-111111111111"
    fake.info.update(id=other, mac="111111111111")
    fake.status["sys"]["mac"] = "111111111111"
    fake.set_energy(50.0)
    clock[0] += 15
    poller.poll_once()
    assert "a different device answers at this address now" in caplog.text
    assert sample("shelly_switch_energy_joules_total", **SW) is None
    assert sample("shelly_switch_energy_joules_total", device=other, switch="0") == 0


def test_startup_logs_the_device_and_warns_when_auth_is_off(clock, caplog):
    caplog.set_level(logging.INFO, logger="shelly-bridge")
    fake = FakeClient()
    fake.info["auth_en"] = False
    poller_for(fake, clock).poll_once()
    first = caplog.records[0].getMessage()
    assert first == (
        "found Plus1PM (model SNSW-001P16EU, gen 2), firmware 1.7.5, authentication off; "
        f"device label {DEVICE_ID!r}"
    )
    assert "anyone on its network can switch the relay" in caplog.text


# -- the real client against the fake device ---------------------------------


@pytest.mark.parametrize("password", [None, PASSWORD])
def test_end_to_end_with_and_without_auth(fake_shelly, clock, password):
    fake_shelly.password = password
    poller = poller_for(ShellyClient(fake_shelly.url, password, timeout=2), clock)
    assert poller.poll_once() is True
    assert sample("shelly_switch_power_watts", **SW) == 1103.4
    assert sample("shelly_switch_current_limit_amperes", **SW) == 16
    assert {method for method, _, _ in fake_shelly.requests} == {"GET"}


def test_end_to_end_wrong_password(fake_shelly, clock, caplog):
    fake_shelly.password = PASSWORD
    poller = poller_for(ShellyClient(fake_shelly.url, "not-it", timeout=2), clock)
    assert poller.poll_once() is False
    assert sample("shelly_scrape_errors_total", reason="auth") == 1
    assert "not-it" not in caplog.text
    assert PASSWORD not in caplog.text


def test_end_to_end_timeout(fake_shelly, clock):
    fake_shelly.delay = 0.5
    poller = poller_for(ShellyClient(fake_shelly.url, timeout=0.1), clock)
    assert poller.poll_once() is False
    assert sample("shelly_scrape_errors_total", reason="timeout") == 1


def test_end_to_end_bad_json(fake_shelly, clock):
    fake_shelly.raw_body = b"{not json"
    poller = poller_for(ShellyClient(fake_shelly.url, timeout=2), clock)
    assert poller.poll_once() is False
    assert sample("shelly_scrape_errors_total", reason="invalid_response") == 1


def test_end_to_end_restart_with_counter_reset_and_firmware_update(fake_shelly, clock, caplog):
    caplog.set_level(logging.INFO, logger="shelly-bridge")
    fake_shelly.password = PASSWORD
    fake_shelly.info.update(ver="1.7.4", fw_id="20251001-000000/1.7.4-gabcdef0")
    switch = fake_shelly.status["switch:0"]
    switch["aenergy"]["total"] = 500.0
    poller = poller_for(ShellyClient(fake_shelly.url, PASSWORD, timeout=2), clock)
    assert poller.poll_once() is True

    clock[0] += 15
    switch["aenergy"]["total"] = 502.0
    assert poller.poll_once() is True
    assert sample("shelly_switch_energy_joules_total", **SW) == 7200

    # The update restarts the device, and someone reset its counters too.
    fake_shelly.info.update(ver="1.7.5", fw_id="20260311-095847/1.7.5-g9979d16")
    fake_shelly.status["sys"]["uptime"] = 20
    switch["aenergy"]["total"] = 0.5
    clock[0] += 60
    assert poller.poll_once() is True
    assert sample("shelly_switch_energy_joules_total", **SW) == 7200
    assert sample("shelly_switch_energy_resets_total", **SW) == 1
    assert sample("shelly_device_info", **_info_labels("1.7.5", "20260311-095847/1.7.5-g9979d16"))
    assert "firmware changed from 1.7.4 to 1.7.5" in caplog.text
    assert "the device restarted 20s ago" in caplog.text

    clock[0] += 15
    switch["aenergy"]["total"] = 1.0
    poller.poll_once()
    assert sample("shelly_switch_energy_joules_total", **SW) == 7200 + 1800


def test_end_to_end_a_device_that_trickles_cannot_stretch_a_poll(fake_shelly, clock):
    fake_shelly.trickle = 0.005
    client = ShellyClient(fake_shelly.url, timeout=1)
    poller = app.Poller(client, poll_interval=15, poll_budget=0.3, clock=lambda: clock[0])
    started = time.monotonic()
    assert poller.poll_once() is False
    assert time.monotonic() - started < 1.5
    assert sample("shelly_scrape_errors_total", reason="timeout") == 1
    assert sample("shelly_scrape_duration_seconds") < 1.5


def test_end_to_end_partial_status(fake_shelly, clock):
    fake_shelly.status = {"switch:0": {"id": 0, "output": False}, "sys": {"uptime": 12}}
    poller = poller_for(ShellyClient(fake_shelly.url, timeout=2), clock)
    assert poller.poll_once() is True
    assert sample("shelly_switch_output", **SW) == 0
    assert sample("shelly_sys_uptime_seconds", device=DEVICE_ID) == 12
    assert sample("shelly_switch_power_watts", **SW) is None
    assert sample("shelly_wifi_rssi_dbm", device=DEVICE_ID) is None
    assert sample("shelly_switch_energy_joules_total", **SW) is None


# -- configuration and the loop -----------------------------------------------


def test_config_defaults():
    config = app.Config({"SHELLY_URL": "http://192.0.2.25"})
    config.validate()
    assert config.url == "http://192.0.2.25"
    assert config.password is None
    assert config.device_name is None
    assert config.poll_interval == 15
    assert config.timeout == 5
    assert config.listen_port == 9122
    assert config.stale_after == 45
    # Three requests at most, each allowed the timeout, but never past the interval.
    assert config.poll_budget == 15


def test_poll_budget_follows_a_short_timeout():
    config = app.Config({"SHELLY_URL": "http://192.0.2.25", "SHELLY_TIMEOUT_SECONDS": "2"})
    assert config.poll_budget == 6


def test_config_from_environment():
    config = app.Config(
        {
            "SHELLY_URL": "192.0.2.25",
            "SHELLY_PASSWORD": PASSWORD,
            "SHELLY_DEVICE_NAME": " boiler ",
            "SHELLY_POLL_SECONDS": "30",
            "SHELLY_TIMEOUT_SECONDS": "2.5",
            "LISTEN_PORT": "9999",
        }
    )
    config.validate()
    assert config.url == "http://192.0.2.25"
    assert (config.password, config.device_name) == (PASSWORD, "boiler")
    assert (config.poll_interval, config.timeout, config.listen_port) == (30, 2.5, 9999)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "SHELLY_URL"),
        ({"SHELLY_URL": "http://192.0.2.25/relay/0"}, "without a path"),
        ({"SHELLY_URL": "http://192.0.2.25\\relay\\0"}, "without a path"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_POLL_SECONDS": "2"}, "between 5 and 3600"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_POLL_SECONDS": "3601"}, "between 5 and 3600"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_POLL_SECONDS": "inf"}, "finite"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_POLL_SECONDS": "1e400"}, "finite"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_POLL_SECONDS": "nan"}, "finite"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_TIMEOUT_SECONDS": "inf"}, "finite"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_TIMEOUT_SECONDS": "nan"}, "finite"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_TIMEOUT_SECONDS": "15"}, "TIMEOUT"),
        ({"SHELLY_URL": "http://192.0.2.25", "LISTEN_PORT": "70000"}, "LISTEN_PORT"),
        ({"SHELLY_URL": "http://192.0.2.25", "SHELLY_DEVICE_NAME": "x" * 65}, "64"),
    ],
)
def test_config_validation(env, message):
    with pytest.raises(SystemExit, match=message):
        app.Config(env).validate()


def test_missing_url_message_names_no_real_address():
    with pytest.raises(SystemExit) as exc:
        app.Config({}).validate()
    assert "192.168." not in str(exc.value)


def test_config_rejects_a_non_number():
    with pytest.raises(SystemExit, match="SHELLY_POLL_SECONDS must be a number"):
        app.Config({"SHELLY_URL": "http://x", "SHELLY_POLL_SECONDS": "fast"})


def test_config_error_does_not_echo_a_password_in_the_url():
    with pytest.raises(SystemExit) as exc:
        app.Config({"SHELLY_URL": f"http://admin:{PASSWORD}@192.0.2.25"}).validate()
    assert PASSWORD not in str(exc.value)


def test_run_polls_until_stopped():
    stop = threading.Event()

    class CountingPoller:
        polls = 0

        def poll_once(self):
            self.polls += 1
            if self.polls == 3:
                stop.set()

    poller = CountingPoller()
    app.run(poller, 0.001, stop)
    assert poller.polls == 3


@pytest.mark.parametrize(
    ("seconds", "text"), [(5, "5s"), (119, "119s"), (600, "10m"), (7260, "2h01m")]
)
def test_duration_text(seconds, text):
    assert app._duration(seconds) == text

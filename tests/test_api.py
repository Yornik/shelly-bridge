"""The client against a fake device over real HTTP, with and without digest auth."""

from __future__ import annotations

import time

import pytest
from conftest import DEVICE_ID, PASSWORD, STATUS, SWITCH_CONFIG

from shelly_bridge import api
from shelly_bridge.api import (
    ShellyAuthError,
    ShellyClient,
    ShellyError,
    ShellyHTTPError,
    ShellyTimeout,
    ShellyUnreachable,
    normalize_base_url,
)


def test_reads_without_auth(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    info = client.get_device_info()
    assert info.id == DEVICE_ID
    assert (info.model, info.gen, info.app, info.version) == (
        "SNSW-001P16EU",
        "2",
        "Plus1PM",
        "1.7.5",
    )
    assert info.auth_enabled is True
    assert client.get_status() == STATUS
    assert client.get_switch_config(0) == SWITCH_CONFIG
    assert [(m, p) for m, p, _ in fake_shelly.requests] == [
        ("GET", "/rpc/Shelly.GetDeviceInfo"),
        ("GET", "/rpc/Shelly.GetStatus"),
        ("GET", "/rpc/Switch.GetConfig?id=0"),
    ]


def test_reads_with_digest_auth_and_one_use_nonces(fake_shelly):
    fake_shelly.password = PASSWORD
    client = ShellyClient(fake_shelly.url, PASSWORD)
    for _ in range(3):
        assert client.get_status() == STATUS
    assert client.get_switch_config(0) == SWITCH_CONFIG
    accepted = [path for _, path, ok in fake_shelly.requests if ok]
    assert accepted == ["/rpc/Shelly.GetStatus"] * 3 + ["/rpc/Switch.GetConfig?id=0"]


def test_reuses_a_nonce_when_the_device_allows_it(fake_shelly):
    fake_shelly.password = PASSWORD
    fake_shelly.one_use_nonces = False
    client = ShellyClient(fake_shelly.url, PASSWORD)
    client.get_status()
    client.get_status()
    # One challenge for the first call, none for the second.
    assert [ok for _, _, ok in fake_shelly.requests] == [False, True, True]


def test_device_info_needs_no_password(fake_shelly):
    fake_shelly.password = PASSWORD
    assert ShellyClient(fake_shelly.url).get_device_info().id == DEVICE_ID


def test_wrong_password_is_an_auth_error_without_the_password(fake_shelly):
    fake_shelly.password = PASSWORD
    client = ShellyClient(fake_shelly.url, "wrong-password")
    with pytest.raises(ShellyAuthError) as exc:
        client.get_status()
    assert "refused the password" in str(exc.value)
    assert "wrong-password" not in str(exc.value)
    assert exc.value.reason == "auth"


def test_missing_password_is_an_auth_error(fake_shelly):
    fake_shelly.password = PASSWORD
    with pytest.raises(ShellyAuthError, match="requires a password"):
        ShellyClient(fake_shelly.url).get_status()


def test_timeout(fake_shelly):
    fake_shelly.delay = 0.5
    with pytest.raises(ShellyTimeout) as exc:
        ShellyClient(fake_shelly.url, timeout=0.1).get_status()
    assert exc.value.reason == "timeout"


def test_connection_refused(closed_port_url):
    with pytest.raises(ShellyUnreachable) as exc:
        ShellyClient(closed_port_url, timeout=1).get_status()
    assert exc.value.reason == "connection"


def test_body_that_is_not_json(fake_shelly):
    fake_shelly.raw_body = b"<html>not json</html>"
    with pytest.raises(ShellyError, match="not JSON") as exc:
        ShellyClient(fake_shelly.url).get_status()
    assert exc.value.reason == "invalid_response"


def test_json_that_is_not_an_object(fake_shelly):
    fake_shelly.raw_body = b"[1, 2, 3]"
    with pytest.raises(ShellyError, match="unexpected JSON type"):
        ShellyClient(fake_shelly.url).get_status()


def test_http_error(fake_shelly):
    fake_shelly.http_status = 503
    with pytest.raises(ShellyHTTPError, match="HTTP 503") as exc:
        ShellyClient(fake_shelly.url).get_status()
    assert exc.value.reason == "http"


def test_device_info_without_an_id_is_rejected(fake_shelly):
    fake_shelly.info.pop("id")
    with pytest.raises(ShellyError, match="no device id"):
        ShellyClient(fake_shelly.url).get_device_info()


def test_redirect_is_not_followed(fake_shelly):
    fake_shelly.redirect_to = "/rpc/Shelly.GetDeviceInfo"
    with pytest.raises(ShellyHTTPError, match="HTTP 302"):
        ShellyClient(fake_shelly.url).get_status()
    assert len(fake_shelly.requests) == 1


def test_errors_never_carry_the_password(fake_shelly, closed_port_url):
    fake_shelly.password = PASSWORD
    for url in (fake_shelly.url, closed_port_url):
        try:
            ShellyClient(url, PASSWORD + "x", timeout=1).get_status()
        except ShellyError as exc:
            assert PASSWORD not in str(exc)
        else:  # pragma: no cover
            pytest.fail("expected an error")


def test_environment_proxies_and_netrc_are_ignored():
    assert ShellyClient("http://192.0.2.1").session.trust_env is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.0.2.25", "http://192.0.2.25"),
        ("http://192.0.2.25/", "http://192.0.2.25"),
        ("192.0.2.25", "http://192.0.2.25"),
        ("  http://boiler.lan:8080  ", "http://boiler.lan:8080"),
        ("http://shelly_boiler-1.lan", "http://shelly_boiler-1.lan"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
    ],
)
def test_base_url_is_normalized(url, expected):
    assert normalize_base_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.25/relay/0?turn=on",
        "http://192.0.2.25/?x=1",
        "http://192.0.2.25/#frag",
        "http://admin:pw@192.0.2.25",
        "ftp://192.0.2.25",
        "http://",
        "http://192.0.2.25:notaport",
        "http://192.0.2.25:99999",
        # urlsplit takes these for a host; urllib3 would send them as a path.
        "http://192.0.2.25\\relay\\0",
        "http://192.0.2.25\\rpc\\Switch.Set%3Fid=0%26on=true%23",
        "http://192.0.2.25%2Frelay%2F0",
        "http://192.0.2.25;turn=on",
    ],
)
def test_base_url_with_more_than_a_host_is_refused(url):
    with pytest.raises(ValueError):
        normalize_base_url(url)
    with pytest.raises(ValueError):
        ShellyClient(url)


def test_digest_user_is_admin():
    assert ShellyClient("http://192.0.2.1", PASSWORD).session.auth.username == "admin"


# -- one deadline for a whole poll -------------------------------------------


def test_a_reply_sent_byte_by_byte_runs_into_the_deadline(fake_shelly):
    # The whole status would take about 7 s; no single read waits long enough
    # to hit the 1 s timeout.
    fake_shelly.trickle = 0.005
    client = ShellyClient(fake_shelly.url, timeout=1)
    started = time.monotonic()
    with pytest.raises(ShellyTimeout, match="ran past its 0.3s limit"), client.time_limit(0.3):
        client.get_status()
    assert time.monotonic() - started < 1.5


def test_the_deadline_shortens_each_request_timeout(fake_shelly):
    fake_shelly.delay = 0.5
    client = ShellyClient(fake_shelly.url, timeout=5)
    with pytest.raises(ShellyTimeout, match=r"timed out after 0\.\d+s"), client.time_limit(0.2):
        client.get_status()


def test_nothing_is_sent_once_the_deadline_has_passed(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    with pytest.raises(ShellyTimeout, match="during Shelly.GetStatus"), client.time_limit(0):
        client.get_status()
    assert fake_shelly.requests == []


def test_the_deadline_ends_with_the_poll(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    with client.time_limit(5):
        client.get_status()
    assert client._deadline is None
    assert client.get_status() == STATUS


def test_a_reply_that_stalls_mid_body_is_a_timeout(fake_shelly):
    fake_shelly.cut_body = 10
    fake_shelly.stall = 0.6
    with pytest.raises(ShellyTimeout) as exc:
        ShellyClient(fake_shelly.url, timeout=0.2).get_status()
    assert exc.value.reason == "timeout"


def test_a_reply_cut_short_is_a_connection_error(fake_shelly):
    fake_shelly.cut_body = 10
    with pytest.raises(ShellyUnreachable) as exc:
        ShellyClient(fake_shelly.url, timeout=2).get_status()
    assert exc.value.reason == "connection"


def test_an_oversized_reply_is_refused(fake_shelly, monkeypatch):
    monkeypatch.setattr(api, "MAX_REPLY_BYTES", 100)
    with pytest.raises(ShellyError, match="more than 100 bytes") as exc:
        ShellyClient(fake_shelly.url).get_status()
    assert exc.value.reason == "invalid_response"

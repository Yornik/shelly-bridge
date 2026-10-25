"""The bridge must not be able to change the device; these keep that true."""

from __future__ import annotations

import pytest
import requests
from conftest import PASSWORD

from scripts.check_read_only import EXPECTED_METHODS, check_package, check_source
from shelly_bridge.api import READ_ONLY_METHODS, ReadOnlyViolation, ShellyClient, ShellyError

# Every request path the client can produce. Pinned here as well as in the guard.
EXPECTED_PATHS = {
    "/rpc/Shelly.GetDeviceInfo",
    "/rpc/Shelly.GetStatus",
    "/rpc/Switch.GetConfig?id=0",
}


def test_package_passes_the_guard():
    assert check_package() == []


def test_allowlist_is_pinned():
    assert dict(READ_ONLY_METHODS) == EXPECTED_METHODS


def test_allowlist_cannot_be_changed_at_runtime():
    with pytest.raises(TypeError):
        READ_ONLY_METHODS["Switch.Set"] = frozenset()  # type: ignore[index]


def test_every_request_is_a_get_to_a_pinned_path(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    client.get_device_info()
    client.get_status()
    client.get_switch_config(0)
    assert {method for method, _, _ in fake_shelly.requests} == {"GET"}
    assert {path for _, path, _ in fake_shelly.requests} == EXPECTED_PATHS


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("Switch.Set", {"id": 0}),
        ("Switch.Toggle", {"id": 0}),
        ("Shelly.Reboot", {}),
        ("Sys.SetConfig", {}),
        ("Script.Start", {"id": 1}),
        ("HTTP.GET", {}),
        ("Shelly.GetConfig", {}),
        # Allowlisted method, parameter that is not: ident adds the cloud key.
        ("Shelly.GetDeviceInfo", {"ident": 1}),
        ("Switch.GetConfig", {"id": "0&on=true"}),
    ],
)
def test_client_refuses_before_sending(fake_shelly, method, params):
    client = ShellyClient(fake_shelly.url)
    with pytest.raises(ReadOnlyViolation):
        client._get(method, **params)
    assert fake_shelly.requests == []


@pytest.mark.parametrize(
    ("http_method", "path"),
    [
        ("POST", "/rpc/Shelly.GetStatus"),
        ("POST", "/rpc"),
        ("GET", "/rpc/Switch.Set?id=0&on=true"),
        ("GET", "/relay/0?turn=on"),
        ("GET", "/%5Crelay%5C0/rpc/Shelly.GetStatus"),
        ("GET", "/rpc/Shelly.GetStatus?id=0"),
        ("GET", "/rpc/Shelly.GetDeviceInfo?ident=true"),
        ("GET", "/rpc/Switch.GetConfig?id=0&id=1"),
        ("GET", "/rpc/Switch.GetConfig?id=true"),
        ("GET", "/rpc/Switch.GetConfig?id=0#x"),
        ("HEAD", "/rpc/Shelly.GetStatus"),
    ],
)
def test_the_session_itself_refuses_anything_but_an_allowlisted_get(fake_shelly, http_method, path):
    # Code that reaches the session without _get still meets the check that
    # runs as each request is sent.
    client = ShellyClient(fake_shelly.url)
    with pytest.raises(ReadOnlyViolation):
        client.session.request(http_method, fake_shelly.url + path, timeout=2)
    prepared = client.session.prepare_request(requests.Request(http_method, fake_shelly.url + path))
    with pytest.raises(ReadOnlyViolation):
        client.session.send(prepared, timeout=2)
    assert fake_shelly.requests == []


def test_the_session_refuses_another_host(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    other = fake_shelly.url.replace("127.0.0.1", "localhost")
    with pytest.raises(ReadOnlyViolation):
        client.session.get(f"{other}/rpc/Shelly.GetStatus", timeout=2)
    assert fake_shelly.requests == []


def test_the_digest_retry_passes_the_send_time_check(fake_shelly):
    fake_shelly.password = PASSWORD
    client = ShellyClient(fake_shelly.url, PASSWORD)
    client.get_switch_config(0)
    assert [(path, ok) for _, path, ok in fake_shelly.requests] == [
        ("/rpc/Switch.GetConfig?id=0", False),
        ("/rpc/Switch.GetConfig?id=0", True),
    ]


def test_method_names_are_matched_like_the_device_does(fake_shelly):
    client = ShellyClient(fake_shelly.url)
    assert client._get("shelly.getstatus")
    # The canonical spelling is what goes on the wire.
    assert fake_shelly.requests[0][1] == "/rpc/Shelly.GetStatus"


def test_violation_is_not_swallowed_as_a_shelly_error():
    assert not issubclass(ReadOnlyViolation, ShellyError)


@pytest.mark.parametrize(
    "source",
    [
        'M = "Switch.Set"',
        'M = "switch.toggle"',
        'M = "Sys.SetConfig"',
        'M = "Shelly.Reboot"',
        'M = "Shelly.FactoryReset"',
        'M = "Switch.ResetCounters"',
        'M = "Script.Start"',
        'M = "Webhook.Create"',
        'M = "HTTP.GET"',
        'M = "Shelly.GetConfig"',
        'M = "KVS.Get"',
        'M = "Shelly.SomethingNew"',
        'URL = "/relay/0?turn=on"',
        'URL = "/ota?url=http://x/fw.zip"',
        'URL = "/backup.zip"',
        'URL = "/script/1/handler"',
        'URL = "/some/new/path"',
        'P = {"ident": True}',
        'U = f"{base}/rpc/Shelly.GetDeviceInfo?ident=true"',
        'U = "ws://device/rpc"',
        "def toggle_relay(): ...",
        "self.session.post(u, allow_redirects=False)",
        "self.session.get(u)",
        "self.session.get(u, allow_redirects=False)",
        "requests.get(u)",
        "requests.post(u)",
        "requests.api.request(m, u)",
        "requests.Session().get(u, allow_redirects=False)",
        "requests.sessions.Session()",
        'getattr(self.session, "post")',
        'getattr(client, "ses" + "sion")',
        # The session reached some other way than self.session.get(...) in _get.
        "s = client.session\ns.get(u, allow_redirects=False)",
        "class ShellyClient:\n    def _get(self):\n        s = self.session\n"
        "        return s.get(u, allow_redirects=False)",
        "class ShellyClient:\n    def other(self):\n"
        "        return self.session.get(u, allow_redirects=False)",
        # The one session call, without allow_redirects=False.
        "class ShellyClient:\n    def _get(self):\n        return self.session.get(u)",
        "self.session.send(prepared)",
        "self.session.prepare_request(r)",
        "client.session.mount('http://', HTTPAdapter())",
        "from requests import get as fetch",
        "from requests import Session",
        "from requests.api import get",
        "from requests.sessions import Session",
        "import requests.api",
        "import importlib",
        "from importlib import import_module",
        'importlib.import_module("requests")',
        'eval("1")',
        "x.send(prepared)",
        "import websocket",
        "import paho.mqtt.client",
        "from http.client import HTTPConnection",
        "from urllib.request import urlopen",
        "import socket",
    ],
)
def test_guard_catches_write_side_code(source):
    problems = [p for p in check_source(source, "probe.py") if "__http_calls__" not in p]
    assert problems, source


def test_guard_ignores_comments():
    source = "# never call Switch.Set, Shelly.Reboot or /relay/0?turn=on\nx = 1\n"
    assert check_source(source, "probe.py") == []


CLIENT = """
import requests
from requests.adapters import HTTPAdapter


class _ReadOnlyAdapter(HTTPAdapter):
    def send(self, request, *args, **kwargs):
        return super().send(request, *args, **kwargs)


class ShellyClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.mount("http://", _ReadOnlyAdapter())

    def _get(self, method):
        return self.session.get(u, allow_redirects=False)
"""


def test_guard_allows_reads_gauge_set_and_the_client_shape():
    source = 'M = "Shelly.GetStatus"\nN = "switch.getconfig"\ngauge.set(1)\nd.get("k")\n' + CLIENT
    assert check_source(source, "probe.py") == ["probe.py: __http_calls__=1"]


def test_guard_counts_http_call_sites(tmp_path):
    (tmp_path / "a.py").write_text(CLIENT)
    (tmp_path / "b.py").write_text(CLIENT)
    problems = check_package(tmp_path)
    assert any("exactly one HTTP call site" in p and "found 2" in p for p in problems)

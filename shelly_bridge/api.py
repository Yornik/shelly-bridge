"""Read-only client for the local RPC API of a Shelly Gen2 device.

Gen2 devices (Plus, Pro, Mini) answer `GET /rpc/<Method>` with the bare result
as JSON. With authentication on, every call except Shelly.GetDeviceInfo needs
SHA-256 digest auth as user `admin`, with the device id as realm
(shelly-api-docs.shelly.cloud, General/Authentication). requests'
HTTPDigestAuth does that.

Only three methods exist here, and all of them read. Nothing in this module
can switch the relay, change a setting, run a script or restart the device.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPDigestAuth

LOG = logging.getLogger("shelly-bridge.api")

# Gen2 has one user, and it is always called admin.
DIGEST_USER = "admin"

# Every RPC method this client may call, with the parameters each one may
# carry. All of them read: device identity and firmware, the status of every
# component, and one switch's limits. _get refuses anything else before a byte
# leaves the process, and CI (scripts/check_read_only.py) fails if this
# changes or if a write method, a Gen1 control path or a privacy-sensitive read
# (the full device config with its location and Wi-Fi names) appears anywhere
# in the package.
READ_ONLY_METHODS = MappingProxyType(
    {
        "Shelly.GetDeviceInfo": frozenset(),
        "Shelly.GetStatus": frozenset(),
        "Switch.GetConfig": frozenset({"id"}),
    }
)

# The device treats method names case-insensitively, so the allowlist is
# looked up the same way and the canonical spelling is what goes on the wire.
_CANONICAL = {method.casefold(): method for method in READ_ONLY_METHODS}

# The only request paths that may leave the process, checked again at send time.
_WIRE_PATHS = {f"/rpc/{method}": method for method in READ_ONLY_METHODS}

# SHELLY_URL may name a host and a port, nothing else. Anything looser lets a
# path through: urlsplit reads http://host\relay\0 as one odd host name, and
# urllib3 then sends /%5Crelay%5C0/rpc/... to the device.
_NETLOC = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?")

# A Shelly.GetStatus reply is a few KiB. A bigger one is not from a Shelly.
MAX_REPLY_BYTES = 1024 * 1024
# Read the reply in small pieces, so a device that sends it byte by byte runs
# into the poll's deadline instead of holding the poll for minutes.
_READ_CHUNK = 64


class ShellyError(Exception):
    """A call failed or its reply made no sense. The poller retries next time."""

    # Bounded label value for shelly_scrape_errors_total.
    reason = "invalid_response"


class ShellyTimeout(ShellyError):
    reason = "timeout"


class ShellyUnreachable(ShellyError):
    reason = "connection"


class ShellyHTTPError(ShellyError):
    reason = "http"


class ShellyAuthError(ShellyError):
    """HTTP 401: no password, or the wrong one. Retrying at once will not help."""

    reason = "auth"


class ReadOnlyViolation(RuntimeError):
    """Code tried to call a method outside READ_ONLY_METHODS.

    Deliberately not a ShellyError: the poller swallows those and keeps going,
    while this one is a bug that has to stop the process.
    """


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    mac: str
    model: str
    gen: str
    app: str
    version: str
    fw_id: str
    auth_enabled: bool

    @classmethod
    def from_reply(cls, data: dict) -> DeviceInfo:
        device_id = data.get("id")
        if not isinstance(device_id, str) or not device_id:
            raise ShellyError("device info carried no device id")

        def text(key: str) -> str:
            value = data.get(key)
            return "" if value is None else str(value)

        return cls(
            id=device_id,
            mac=text("mac"),
            model=text("model"),
            gen=text("gen"),
            app=text("app"),
            version=text("ver"),
            fw_id=text("fw_id"),
            auth_enabled=data.get("auth_en") is True,
        )


def normalize_base_url(url: str) -> str:
    """Return scheme://host[:port], or raise ValueError.

    The client appends the RPC path to this, so the URL must not carry a path,
    a query or credentials of its own.
    """
    # A path in SHELLY_URL would ride along on every request: a Gen1-style
    # control path with a query, such as /relay/0?turn=on, would switch the
    # relay on each poll. So anything but scheme, host and port is refused.
    url = url.strip()
    if "://" not in url:
        url = f"http://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not contain a user or password; use SHELLY_PASSWORD")
    if parts.path.rstrip("/") or parts.query or parts.fragment:
        raise ValueError("must be only scheme, host and port, without a path or query")
    if not _NETLOC.fullmatch(parts.netloc):
        raise ValueError("must be only scheme, host and port, without a path or query")
    parts.port  # noqa: B018 - raises ValueError for a port above 65535
    return f"{parts.scheme}://{parts.netloc}"


def _check_wire(method: str | None, url: str | None, origin: tuple[str, str]) -> None:
    """Raise ReadOnlyViolation unless this is a GET of an allowlisted method on the device."""
    parts = urlsplit(url or "")
    canonical = _WIRE_PATHS.get(parts.path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    allowed = READ_ONLY_METHODS[canonical] if canonical else frozenset()
    if (
        method != "GET"
        or canonical is None
        or (parts.scheme, parts.netloc.lower()) != origin
        or parts.fragment
        or len({key for key, _ in query}) != len(query)
        or any(
            key not in allowed or not (value.isascii() and value.isdigit()) for key, value in query
        )
    ):
        raise ReadOnlyViolation(f"refusing to send {method} {parts.path}: not an allowlisted read")


class _ReadOnlyAdapter(HTTPAdapter):
    """Checks each request as it is sent, after requests has built the final URL.

    The second line of the runtime check, after the allowlist in _get. It also
    covers the request that answers a digest challenge, and anything that
    reaches the session without going through _get.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__()
        parts = urlsplit(base_url)
        self._origin = (parts.scheme, parts.netloc.lower())

    def send(self, request, *args, **kwargs):
        _check_wire(request.method, request.url, self._origin)
        return super().send(request, *args, **kwargs)


class ShellyClient:
    def __init__(self, base_url: str, password: str | None = None, *, timeout: float = 5.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self._has_password = bool(password)
        self._deadline: float | None = None
        self._budget = 0.0
        self.session = requests.Session()
        # Replaces requests' default adapters, so every request this session
        # sends, whichever code sends it, is checked against the allowlist.
        adapter = _ReadOnlyAdapter(self.base_url)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        # The device is on the LAN: no proxy from the environment, and no
        # credentials from ~/.netrc.
        self.session.trust_env = False
        if password:
            # requests answers the device's 401 challenge itself, with the
            # algorithm the challenge names (SHA-256 on Gen2).
            self.session.auth = HTTPDigestAuth(DIGEST_USER, password)

    @contextmanager
    def time_limit(self, seconds: float) -> Iterator[None]:
        """Give every request inside one shared deadline.

        The timeout of a single request bounds each wait on the socket, not
        the whole request, so a device that answers slowly could otherwise
        stretch one poll far beyond the poll interval.
        """
        self._deadline, self._budget = time.monotonic() + seconds, seconds
        try:
            yield
        finally:
            self._deadline = None

    # -- transport ---------------------------------------------------------

    def _remaining(self, canonical: str) -> float:
        if self._deadline is None:
            return self.timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ShellyTimeout(f"the poll ran past its {self._budget:g}s limit during {canonical}")
        return min(self.timeout, remaining)

    def _get(self, method: str, **params: int) -> dict:
        canonical = _CANONICAL.get(method.casefold())
        if canonical is None:
            raise ReadOnlyViolation(f"refusing to call {method}: not an allowlisted read method")
        unexpected = sorted(set(params) - READ_ONLY_METHODS[canonical])
        if unexpected:
            raise ReadOnlyViolation(f"refusing to call {canonical} with parameter(s) {unexpected}")
        if any(type(value) is not int for value in params.values()):
            raise ReadOnlyViolation(f"refusing to call {canonical}: parameters must be integers")
        timeout = self._remaining(canonical)
        try:
            resp = self.session.get(
                f"{self.base_url}/rpc/{canonical}",
                params=params or None,
                timeout=timeout,
                # A redirect could point anywhere on the device, so none is followed.
                allow_redirects=False,
                # The body is read below, against the deadline.
                stream=True,
            )
        except requests.Timeout:
            raise ShellyTimeout(f"{canonical} timed out after {timeout:g}s") from None
        except requests.RequestException as exc:
            raise ShellyUnreachable(f"{canonical} failed: {exc.__class__.__name__}") from None
        with resp:
            if resp.status_code == 401:
                if self._has_password:
                    raise ShellyAuthError("the device refused the password (HTTP 401)")
                raise ShellyAuthError("the device requires a password (HTTP 401)")
            if resp.status_code != 200:
                raise ShellyHTTPError(f"{canonical} answered HTTP {resp.status_code}")
            body = self._read(resp, canonical, timeout)
        try:
            data = json.loads(body)
        except ValueError:
            raise ShellyError(f"{canonical} returned a body that is not JSON") from None
        if not isinstance(data, dict):
            raise ShellyError(f"{canonical} returned an unexpected JSON type")
        return data

    def _read(self, resp: requests.Response, canonical: str, timeout: float) -> bytes:
        body = bytearray()
        started = time.monotonic()
        try:
            for chunk in resp.iter_content(_READ_CHUNK):
                body += chunk
                if len(body) > MAX_REPLY_BYTES:
                    raise ShellyError(f"{canonical} sent more than {MAX_REPLY_BYTES} bytes")
                self._remaining(canonical)
        except requests.RequestException as exc:
            # requests reports a read that timed out mid-body as a ConnectionError.
            if time.monotonic() - started >= timeout:
                raise ShellyTimeout(f"{canonical} timed out after {timeout:g}s") from None
            raise ShellyUnreachable(f"{canonical} failed: {exc.__class__.__name__}") from None
        return bytes(body)

    # -- reads -------------------------------------------------------------

    def get_device_info(self) -> DeviceInfo:
        """Identity and firmware. Never needs a password, even with auth on."""
        return DeviceInfo.from_reply(self._get("Shelly.GetDeviceInfo"))

    def get_status(self) -> dict:
        """Status of every component in one call: switch:N, input:N, sys, wifi, cloud, ..."""
        return self._get("Shelly.GetStatus")

    def get_switch_config(self, switch_id: int) -> dict:
        """One switch's name and limits. Deliberately not the device-wide config
        call, which also holds the device's coordinates and Wi-Fi names."""
        return self._get("Switch.GetConfig", id=switch_id)

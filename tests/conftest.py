from __future__ import annotations

import copy
import json
import socket
import time
from pathlib import Path

import pytest
from fake_shelly import FakeShelly
from prometheus_client import REGISTRY

from shelly_bridge import metrics

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# Shaped like replies of a Shelly Plus 1PM on firmware 1.7.5. Identifiers,
# addresses and readings are made up.
DEVICE_INFO = load("device_info.json")
STATUS = load("status.json")
# The same, plus every optional field the docs describe and the Plus 1PM does
# not send (frequency, power factor, returned energy, an error, an update).
STATUS_ALL = load("status_all_fields.json")
SWITCH_CONFIG = load("switch_config.json")

DEVICE_ID = DEVICE_INFO["id"]
PASSWORD = "correct horse battery staple"  # noqa: S105 - a test password, not a secret


@pytest.fixture
def status():
    return copy.deepcopy(STATUS)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Metrics are module globals; start every test from an empty state."""
    metrics.READINGS.clear()
    metrics.READINGS.max_age = 45.0
    metrics.READINGS.clock = time.time
    for gauge in (metrics.LAST_SUCCESS, metrics.SCRAPE_DURATION):
        gauge.set(0)
    metrics.SCRAPE_ERRORS.clear()
    metrics.init_health()
    yield


@pytest.fixture
def fake_shelly():
    """A started fake device without a password; set .password to turn auth on."""
    fake = FakeShelly(DEVICE_INFO, STATUS, SWITCH_CONFIG)
    fake.url = fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def closed_port_url():
    """A local URL where nothing listens, so connecting is refused at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def sample(metric: str, **labels) -> float | None:
    return REGISTRY.get_sample_value(metric, labels)


def device_series() -> set[str]:
    """Names of every device sample currently exposed (health metrics excluded)."""
    return {s.name for family in metrics.READINGS.collect() for s in family.samples}

#!/usr/bin/env python3
"""Print what /metrics serves after two polls of a device that reports every field.

CI pipes this into `promtool check metrics`, which checks every name, type,
unit suffix and help text against Prometheus' conventions. The replies are the
test fixtures; nothing is contacted.
"""

from __future__ import annotations

import contextlib
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

from prometheus_client import REGISTRY, generate_latest  # noqa: E402

from shelly_bridge.__main__ import Poller  # noqa: E402
from shelly_bridge.api import DeviceInfo  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(self) -> None:
        self.status = _load("status_all_fields.json")

    def get_device_info(self) -> DeviceInfo:
        return DeviceInfo.from_reply(_load("device_info.json"))

    def get_status(self) -> dict:
        return copy.deepcopy(self.status)

    def get_switch_config(self, switch_id: int) -> dict:
        return _load("switch_config.json")

    def time_limit(self, seconds: float) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


def main() -> None:
    client = FixtureClient()
    poller = Poller(client)
    poller.poll_once()
    client.status["switch:0"]["aenergy"]["total"] += 1
    poller.poll_once()
    sys.stdout.write(generate_latest(REGISTRY).decode())


if __name__ == "__main__":
    main()

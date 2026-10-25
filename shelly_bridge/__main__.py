"""Entry point: poll one Shelly Gen2 device and serve its readings to Prometheus."""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time

from . import metrics
from .api import (
    DeviceInfo,
    ReadOnlyViolation,
    ShellyAuthError,
    ShellyClient,
    ShellyError,
    normalize_base_url,
)
from .energy import EnergyCounter
from .server import start_server

LOG = logging.getLogger("shelly-bridge")

# Readings disappear, and /readyz turns 503, once the last good poll is this
# many poll intervals old: one missed poll is noise, three are an outage.
STALE_AFTER_POLLS = 3

# Device identity, firmware and switch limits are re-read this often, and at
# once after a device restart (a firmware update always restarts it).
INFO_REFRESH_SECONDS = 600

# After a 401, wait this long before sending the password again. The firmware
# of a Plus 1PM (1.7.x) has no lockout, but there is no point in trying a wrong
# password every few seconds.
AUTH_BACKOFF_SECONDS = 300

# While the device stays unreachable, repeat the error line this often instead
# of on every poll, also when the reason keeps changing (timeout, connection,
# http in turn on a flaky link).
REMINDER_SECONDS = 600

MIN_POLL_SECONDS = 5
# Readings drop out 3 intervals after the last good poll, so a longer interval
# would leave a dead device's last readings up for hours.
MAX_POLL_SECONDS = 3600

# A poll makes at most three requests on a one-switch device (device info,
# status, switch config), each allowed the full timeout.
REQUESTS_PER_POLL = 3

# ESP-IDF esp_reset_reason_t values seen in sys.reset_reason, for the log line.
RESET_REASONS = {
    1: "power-on",
    3: "software restart",
    4: "panic",
    5: "watchdog",
    6: "watchdog",
    7: "watchdog",
    9: "brownout",
}


def _env_number(env, name: str, default: str, kind=float):
    raw = env.get(name, default).strip() or default
    try:
        return kind(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Config:
    """Runtime configuration, sourced entirely from environment variables."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.url = env.get("SHELLY_URL", "").strip()
        self.password = env.get("SHELLY_PASSWORD", "") or None
        self.device_name = env.get("SHELLY_DEVICE_NAME", "").strip() or None
        self.poll_interval = _env_number(env, "SHELLY_POLL_SECONDS", "15")
        self.timeout = _env_number(env, "SHELLY_TIMEOUT_SECONDS", "5")
        self.listen_port = _env_number(env, "LISTEN_PORT", "9122", int)

    @property
    def stale_after(self) -> float:
        return self.poll_interval * STALE_AFTER_POLLS

    @property
    def poll_budget(self) -> float:
        """How long one whole poll may take, all of its requests together."""
        return min(self.poll_interval, REQUESTS_PER_POLL * self.timeout)

    def validate(self) -> None:
        if not self.url:
            raise SystemExit(
                "missing required environment variable: SHELLY_URL "
                "(for example http://<device-address>)"
            )
        try:
            self.url = normalize_base_url(self.url)
        except ValueError as exc:
            # Not echoed: a URL with a password in it would end up in the log.
            raise SystemExit(f"SHELLY_URL {exc}") from None
        for name, value in (
            ("SHELLY_POLL_SECONDS", self.poll_interval),
            ("SHELLY_TIMEOUT_SECONDS", self.timeout),
        ):
            if not math.isfinite(value):
                raise SystemExit(f"{name} must be a finite number of seconds")
        if not MIN_POLL_SECONDS <= self.poll_interval <= MAX_POLL_SECONDS:
            raise SystemExit(
                f"SHELLY_POLL_SECONDS must be between {MIN_POLL_SECONDS} and {MAX_POLL_SECONDS}: "
                "the device serves only a few connections at a time, and readings of a "
                "device that stopped answering must not stay up for hours"
            )
        if not 0 < self.timeout < self.poll_interval:
            raise SystemExit("SHELLY_TIMEOUT_SECONDS must be above 0 and below SHELLY_POLL_SECONDS")
        if not 0 < self.listen_port < 65536:
            raise SystemExit("LISTEN_PORT must be between 1 and 65535")
        if self.device_name is not None and len(self.device_name) > 64:
            raise SystemExit("SHELLY_DEVICE_NAME must be at most 64 characters")


class Poller:
    """Polls the device once per call and publishes what it read.

    Logs state changes only: the device found, firmware changed, device
    restarted, unreachable, reachable again. Not one line per poll.
    """

    def __init__(
        self,
        client: ShellyClient,
        *,
        device_name: str | None = None,
        poll_interval: float = 15.0,
        poll_budget: float | None = None,
        clock=time.time,
    ) -> None:
        self.client = client
        self.device_name = device_name
        self.stale_after = poll_interval * STALE_AFTER_POLLS
        self.poll_budget = poll_interval if poll_budget is None else poll_budget
        self.clock = clock
        self.info: DeviceInfo | None = None
        self.info_read_at = 0.0
        self.switch_configs: dict[int, dict] = {}
        self.energy: dict[tuple[int, str], EnergyCounter] = {}
        self.last_uptime: float | None = None
        self.last_success: float | None = None
        self.failing_since: float | None = None
        self.failed_polls = 0
        # Failed polls by reason since the last error line.
        self.unlogged: dict[str, int] = {}
        self.last_reminder = 0.0
        self.auth_blocked_until = 0.0

    @property
    def device(self) -> str:
        """The `device` label: SHELLY_DEVICE_NAME, or else the device's own id."""
        if self.device_name:
            return self.device_name
        return self.info.id if self.info else ""

    def ready(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return self.last_success is not None and now - self.last_success <= self.stale_after

    def poll_once(self, now: float | None = None) -> bool:
        """Poll the device once. Returns whether it worked.

        Never raises for a device problem: those become shelly_scrape_success 0
        and a log line. ReadOnlyViolation is a bug and does stop the process.
        """
        now = self.clock() if now is None else now
        if now < self.auth_blocked_until:
            metrics.READINGS.poll_failed()
            return False
        started = time.monotonic()
        try:
            with self.client.time_limit(self.poll_budget):
                self._poll(now)
        except ReadOnlyViolation:
            raise
        except ShellyError as exc:
            self._failed(now, exc.reason, str(exc), auth=isinstance(exc, ShellyAuthError))
            return False
        except Exception as exc:  # noqa: BLE001 - a reply shaped unlike any seen must not stop the bridge
            self._failed(now, "invalid_response", f"unexpected reply ({exc.__class__.__name__})")
            return False
        finally:
            metrics.SCRAPE_DURATION.set(time.monotonic() - started)
        self._succeeded(now)
        return True

    # -- one poll ----------------------------------------------------------

    def _poll(self, now: float) -> None:
        refreshed = False
        if (
            self.info is None
            or self.failing_since is not None
            or now - self.info_read_at >= INFO_REFRESH_SECONDS
        ):
            self._read_info(now)
            refreshed = True

        status = self.client.get_status()
        sys_status = metrics.as_dict(status.get("sys"))
        uptime = metrics.number(sys_status.get("uptime"))
        restarted = (
            uptime is not None and self.last_uptime is not None and uptime < self.last_uptime
        )
        mac = sys_status.get("mac")
        replaced = (
            isinstance(mac, str)
            and bool(self.info.mac)
            and mac.casefold() != self.info.mac.casefold()
        )
        if restarted:
            reason = metrics.number(sys_status.get("reset_reason"))
            LOG.info(
                "the device restarted %s ago (reset reason: %s)",
                _duration(uptime),
                RESET_REASONS.get(int(reason), f"code {int(reason)}") if reason else "unknown",
            )
        if (restarted or replaced) and not refreshed:
            self._read_info(now)
            refreshed = True
        self.last_uptime = uptime

        switches = dict(metrics.components(status, "switch"))
        for switch_id in switches:
            if refreshed or switch_id not in self.switch_configs:
                self.switch_configs[switch_id] = self.client.get_switch_config(switch_id)
        for switch_id in set(self.switch_configs) - set(switches):
            del self.switch_configs[switch_id]

        for switch_id, sw in switches.items():
            for field in ("aenergy", "ret_aenergy"):
                total = metrics.number(metrics.as_dict(sw.get(field)).get("total"))
                if total is None or total < 0:
                    continue
                counter = self.energy.setdefault((switch_id, field), EnergyCounter())
                previous = counter.previous_wh
                if counter.observe(total):
                    LOG.warning(
                        "switch %d: the device's %s total went back from %.3f Wh to %.3f Wh "
                        "(counter reset, factory reset or power cut); the exported counter "
                        "keeps its value and counts on from here",
                        switch_id,
                        field,
                        previous,
                        total,
                    )

        snapshot = metrics.build_snapshot(
            self.device, self.info, status, self.switch_configs, self.energy
        )
        metrics.READINGS.publish(snapshot, now)

    def _read_info(self, now: float) -> None:
        info = self.client.get_device_info()
        old, self.info, self.info_read_at = self.info, info, now
        if old is None:
            LOG.info(
                "found %s (model %s, gen %s), firmware %s, authentication %s; device label %r",
                info.app or "a Shelly",
                info.model,
                info.gen,
                info.version,
                "on" if info.auth_enabled else "off",
                self.device,
            )
            if not info.auth_enabled:
                LOG.warning(
                    "authentication is off on the device: anyone on its network can switch "
                    "the relay. Turn it on in the Shelly app or web UI."
                )
        elif old.id != info.id:
            LOG.warning(
                "a different device answers at this address now (%s, model %s, firmware %s); "
                "its energy counters start from 0",
                info.app,
                info.model,
                info.version,
            )
            self.energy.clear()
            self.switch_configs.clear()
            self.last_uptime = None
        elif old.fw_id != info.fw_id:
            LOG.info("firmware changed from %s to %s", old.version, info.version)

    # -- outcome -----------------------------------------------------------

    def _succeeded(self, now: float) -> None:
        # READINGS.publish in _poll already marked the poll as good.
        metrics.LAST_SUCCESS.set(now)
        if self.failing_since is not None and self.last_success is not None:
            LOG.info(
                "the device answers again after %s (%d failed polls)",
                _duration(now - self.failing_since),
                self.failed_polls,
            )
        self.failing_since = None
        self.failed_polls = 0
        self.unlogged.clear()
        self.last_success = now

    def _failed(self, now: float, reason: str, message: str, *, auth: bool = False) -> None:
        metrics.READINGS.poll_failed()
        metrics.SCRAPE_ERRORS.labels(reason).inc()
        if auth:
            self.auth_blocked_until = now + AUTH_BACKOFF_SECONDS
            message += f"; check SHELLY_PASSWORD (next attempt in {AUTH_BACKOFF_SECONDS}s)"
        if self.failing_since is None:
            self.failing_since = now
            self.failed_polls = 1
            self.last_reminder = now
            LOG.error(
                "cannot read the device: %s; readings drop out %ds after the last good poll",
                message,
                self.stale_after,
            )
            return
        self.failed_polls += 1
        self.unlogged[reason] = self.unlogged.get(reason, 0) + 1
        # One line per reminder period, however often the reason changes. A
        # password problem is logged at once; the backoff already limits it to
        # one line per AUTH_BACKOFF_SECONDS.
        if auth or now - self.last_reminder >= REMINDER_SECONDS:
            self.last_reminder = now
            counts = ", ".join(f"{r} {n}" for r, n in sorted(self.unlogged.items()))
            self.unlogged.clear()
            LOG.error(
                "still cannot read the device after %s (%d failed polls; since the last line: "
                "%s); latest: %s",
                _duration(now - self.failing_since),
                self.failed_polls,
                counts,
                message,
            )


def run(poller: Poller, interval: float, stop: threading.Event, monotonic=time.monotonic) -> None:
    """Poll at a steady interval until `stop` is set."""
    while not stop.is_set():
        started = monotonic()
        poller.poll_once()
        stop.wait(max(0.0, interval - (monotonic() - started)))


def serve(config: Config) -> None:  # pragma: no cover - process wiring
    stop = threading.Event()
    # As PID 1 in a container, Python ignores SIGTERM unless it has a handler,
    # and the pod would wait out its whole grace period.
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())

    metrics.READINGS.max_age = config.stale_after
    client = ShellyClient(config.url, config.password, timeout=config.timeout)
    poller = Poller(
        client,
        device_name=config.device_name,
        poll_interval=config.poll_interval,
        poll_budget=config.poll_budget,
    )
    server = start_server(config.listen_port, poller.ready)
    LOG.info(
        "serving /metrics, /healthz and /readyz on :%d; polling %s every %gs %s a password",
        config.listen_port,
        config.url,
        config.poll_interval,
        "with" if config.password else "without",
    )
    run(poller, config.poll_interval, stop)
    LOG.info("stopping")
    server.shutdown()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process wiring
    parser = argparse.ArgumentParser(
        prog="shelly-bridge",
        description="Prometheus exporter for a Shelly Gen2 device. Configured with "
        "environment variables; see the README.",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    config = Config()
    config.validate()
    serve(config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

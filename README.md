# shelly-bridge

A Prometheus exporter for a **Shelly Gen2** relay, plus a Grafana dashboard for
it. It reads the device over its local RPC API on an interval and publishes
relay state, power, energy, supply voltage and current, and device health as
Prometheus metrics. It was built for a Shelly Plus 1PM that switches an
electric water boiler, and works with other Gen2 devices that have a switch.

**The bridge never changes the device.** It cannot switch the relay, change a
setting, run a script, install firmware or restart the device. It only reads,
and CI enforces that (see [Read-only, and how that's enforced](#read-only-and-how-thats-enforced)).

> [!WARNING]
> **The device itself can be switched by others.** With authentication off,
> anyone on the device's network can switch the relay with one HTTP request.
> Turn authentication on. Then `SHELLY_PASSWORD` is the device's admin
> password: with it, other software can switch the relay, change every setting
> and install firmware. Keep it in an encrypted secret (SOPS, Vault, …), never
> in git, logs or screenshots. The Plus 1PM stays on firmware 1.7.x, which
> serves HTTP only and has no protection against password guessing, so keep it
> on a trusted network.

> [!IMPORTANT]
> **Unofficial project.** This isn't made, endorsed or supported by Shelly
> Group SE. **Shelly** and all related names and logos are trademarks of
> Shelly Group SE. See [Disclaimer & trademarks](#disclaimer--trademarks).

<!--
  Screenshots go here once they exist, rendered with test data:
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/dashboard-light.png">
    <img alt="The Shelly relay Grafana dashboard, rendered with test data" src="docs/dashboard-dark.png" width="600">
  </picture>
-->
_Dashboard screenshots will follow: `docs/dashboard-light.png` and
`docs/dashboard-dark.png`, rendered with test data._

- **Read-only, enforced.** The client refuses any RPC method outside a fixed
  list of three read methods, before anything is sent, and checks every
  request again as it leaves. A CI job fails if that list changes or if
  write-side code appears anywhere in the package.
- **Honest data.** A reading the device stops reporting disappears instead of
  keeping its last value. If the device can't be read for three poll
  intervals, all its readings disappear and the health metrics say why. The
  energy counter never goes back, also when the device resets its own.
- **Fails safe.** A failed poll sets `shelly_scrape_success` to `0` instead of
  crashing the process. Configuration comes from environment variables only,
  and nothing is stored on disk.

The API knowledge (methods, fields, units, authentication) comes from Shelly's
[Gen2 API documentation](https://shelly-api-docs.shelly.cloud/gen2/) and from
replies of a real Plus 1PM. See [NOTICE](NOTICE).

## Read-only, and how that's enforced

Read-only rests on three layers. You can check each one yourself in a few
minutes:

1. **No write code.** The package calls three RPC methods, all of them reads:
   `Shelly.GetDeviceInfo`, `Shelly.GetStatus` and `Switch.GetConfig`. Every
   request is an HTTP `GET` to `/rpc/<method>`. No code exists that switches,
   configures, restarts or updates the device.
2. **Runtime checks, twice.** Every request goes through one function,
   `ShellyClient._get`. It raises `ReadOnlyViolation` before anything is sent
   if the method isn't in `READ_ONLY_METHODS` ([`api.py`](shelly_bridge/api.py)),
   or if a parameter isn't allowed for that method. Method names are compared
   case-insensitively, as the device does. Then the HTTP session checks each
   request again as it is sent, after `requests` has built the final URL: it
   must be a `GET` of `/rpc/<allowlisted method>` on the configured host, with
   only that method's integer parameters. That second check also covers the
   request that answers a digest challenge, and any code that reaches the
   session without `_get`. `ReadOnlyViolation` deliberately isn't caught, so
   the process stops instead of carrying on. `SHELLY_URL` may hold only a
   scheme, a host and a port, and redirects aren't followed.
3. **CI guard.** The `Read-only guard` job runs
   [`scripts/check_read_only.py`](scripts/check_read_only.py) on every pull
   request and before every release. It reads the code's syntax tree, not its
   comments, and fails if:
   - the allowlist differs from the copy pinned in the script;
   - any string in the code looks like an RPC method or a path that isn't
     allowed;
   - any string or name refers to a write method (`.Set`, `Toggle`, `Reboot`,
     `SetConfig`, `ResetCounters`, `Update`, …), a script, schedule, webhook or
     KVS call, the device's outbound `HTTP.GET`, a Gen1 control path
     (`/relay/0?turn=on`), firmware or backup paths, or a read that exposes
     private data (`Shelly.GetConfig` holds the location and Wi-Fi names);
   - an HTTP, WebSocket or MQTT client library is imported, or anything from
     `requests` other than its `auth`, `adapters` and `exceptions` modules;
   - the session appears anywhere except `ShellyClient.__init__`, which sets it
     up, and one `self.session.get(..., allow_redirects=False)` in
     `ShellyClient._get`. A local alias, a second call, `send`,
     `prepare_request`, a new `Session`, another `mount`, any other HTTP verb
     or `requests.get` fails the job;
   - `getattr`, `setattr`, `eval`, `exec`, `compile` or `importlib` could hide
     what is called.

   Tests also prove the guard catches each of these, and that the client
   refuses `Switch.Set`, `Shelly.Reboot` and others without sending anything,
   whether the call goes through `_get` or straight to the session.

**One caveat, stated plainly.** With authentication on, the bridge holds the
device's only password, and Gen2 has a single user (`admin`). The bridge's own
code can't use it to change anything, but the password can, from other
software. See the warning at the top.

## Metrics

All device series carry a `device` label: `SHELLY_DEVICE_NAME`, or else the
device's own id (`shellyplus1pm-<mac>`). Switch series also carry `switch`
(`0` on a Plus 1PM).

| Metric | Extra labels | Description |
| --- | --- | --- |
| `shelly_device_info` | `model`, `gen`, `app`, `version`, `fw_id` | Always `1`. Gives the model and firmware. |
| `shelly_switch_output` | `switch` | `1` while the relay is on, `0` while it's off |
| `shelly_switch_source_info` | `switch`, `source` | Always `1`. What last changed the relay, as the device names it: `init`, `button`, `http`, `timer`, `WS_in`, … Unusual values become `other`. |
| `shelly_switch_power_watts` | `switch` | Active power in W |
| `shelly_switch_voltage_volts` | `switch` | Supply voltage in V |
| `shelly_switch_current_amperes` | `switch` | Current in A |
| `shelly_switch_frequency_hertz` | `switch` | Supply frequency in Hz. Only on devices that report it; the Plus 1PM doesn't. |
| `shelly_switch_power_factor` | `switch` | Power factor. Only on devices that report it; the Plus 1PM doesn't. |
| `shelly_switch_temperature_celsius` | `switch` | Temperature inside the device |
| `shelly_switch_energy_joules_total` | `switch` | Counter. Energy used since the bridge started, in J. Never goes back. See [Energy](#energy-a-counter-that-only-grows). |
| `shelly_switch_returned_energy_joules_total` | `switch` | Counter. Energy returned to the grid, same rules. Only on devices that report it. |
| `shelly_switch_device_energy_joules` | `switch` | The device's own energy total, in J. Goes back to `0` when the device's counter is reset. |
| `shelly_switch_energy_resets_total` | `switch` | Counter. How often the device's own total went back. |
| `shelly_switch_error` | `switch`, `error` | `1` while a protection error is active: `overtemp`, `overpower`, `overvoltage`, `undervoltage`, `other`. All five are always present. |
| `shelly_switch_power_limit_watts` / `..._voltage_limit_volts` / `..._current_limit_amperes` | `switch` | The limits above which the device turns the relay off |
| `shelly_input_state` | `input` | `1` while the switch input (SW terminal) is on |
| `shelly_sys_uptime_seconds` | | Time since the device started |
| `shelly_sys_reset_reason` | | Why the device last started, as the raw ESP-IDF code: `1` power-on (for example after a power cut), `3` software restart, `4` panic, `5`–`7` watchdog, `9` brownout |
| `shelly_sys_restart_required` | | `1` if the device needs a restart to apply a setting |
| `shelly_sys_update_available` | `stage` | `1` if Shelly offers a firmware update on `stable` or `beta` |
| `shelly_sys_ram_size_bytes` / `shelly_sys_ram_free_bytes` | | RAM |
| `shelly_sys_fs_size_bytes` / `shelly_sys_fs_free_bytes` | | File system |
| `shelly_sys_config_revision` | | Goes up with every settings change. Alert on `changes()` to see when a setting changed. |
| `shelly_wifi_rssi_dbm` | | Wi-Fi signal strength |
| `shelly_cloud_connected` / `shelly_mqtt_connected` | | `1` if the device is connected to Shelly Cloud or to an MQTT broker |
| `shelly_scrape_success` | _(no `device`)_ | `1` if the last poll succeeded and its readings are still served. `0` after a failed poll, and once the last good poll is 3 poll intervals old, also while a poll hangs. The bridge's `up` for the device. |
| `shelly_last_success_timestamp_seconds` | _(no `device`)_ | Time of the last successful poll |
| `shelly_scrape_duration_seconds` | _(no `device`)_ | How long the last poll took |
| `shelly_scrape_errors_total` | `reason` _(no `device`)_ | Counter. Failed polls by reason: `timeout`, `connection`, `auth`, `http`, `invalid_response`. Every reason is present from the start. |

The exporter only publishes fields the device actually reports. If a field
disappears, its series is removed, so a missing reading never shows as `0` or
as a stale value. The 4 health metrics carry no `device` label, because they
exist before the device first answers. Run one bridge per device.

Not exported, on purpose: the Wi-Fi network name, the access point, the
device's IP address and its location. The bridge never asks for the device's
full configuration, which holds them.

### Energy: a counter that only grows

The device counts energy in Wh (`aenergy.total`). Since firmware 1.1.0 that
total survives a power cut, but it still goes back:

- to `0` after a counter reset or a factory reset;
- by a little after a power cut, when the last value the device saved is older
  than the last value the bridge read.

Exported as-is, `increase()` would read any drop as a counter reset and count
the whole remaining total as new energy, for example 1000 kWh in one minute.
So the bridge keeps its own counter, `shelly_switch_energy_joules_total`, the
Prometheus way:

1. It starts at `0` when the bridge starts. To `rate()` and `increase()` that
   is an ordinary counter reset.
2. Each poll adds what the device total grew by since the last poll.
3. If the device total goes back, the poll adds nothing, the new value becomes
   the base, and `shelly_switch_energy_resets_total` goes up. The bridge logs
   a warning. At most one poll interval of energy is lost.
4. Energy used while the device couldn't be read is added at the first poll
   after it answers again, in one step. The total stays right.

Energy used while the bridge itself isn't running isn't counted. The device's
own total is still in `shelly_switch_device_energy_joules` if you need it.

Use it like any counter: `rate(shelly_switch_energy_joules_total[5m])` is the
average power in W, and `increase(shelly_switch_energy_joules_total[1d]) / 3.6e6`
is the energy of a day in kWh.

### When the device can't be read

A failed poll sets `shelly_scrape_success` to `0` and counts the reason in
`shelly_scrape_errors_total`. The readings of the last good poll stay for 3
poll intervals (45 s by default), so one lost reply doesn't punch a hole in
every graph. After that, all device series disappear until the device answers
again, graphs show a gap, and `shelly_scrape_success` reads `0` even if a poll
is still waiting for the device.

A whole poll, all of its requests together, has one deadline: 3 times
`SHELLY_TIMEOUT_SECONDS`, and never more than `SHELLY_POLL_SECONDS`. A device
that answers very slowly, or sends its reply byte by byte, fails the poll as a
`timeout` instead of holding it for minutes. A device restart shows as a drop in
`shelly_sys_uptime_seconds`; `shelly_sys_reset_reason` `1` means power was
lost.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `SHELLY_URL` | _(required)_ | Address of the device, for example `http://192.168.1.25`. Scheme, host and port only. |
| `SHELLY_PASSWORD` | _(none)_ | The device's password, if authentication is on. The user is always `admin`. |
| `SHELLY_DEVICE_NAME` | the device id | Value of the `device` label, for example `boiler`. |
| `SHELLY_POLL_SECONDS` | `15` | Seconds between polls, from 5 to 3600 |
| `SHELLY_TIMEOUT_SECONDS` | `5` | How long the bridge waits for the device to connect or send more of its reply. Must be below `SHELLY_POLL_SECONDS`. A whole poll may take 3 times this, and never longer than `SHELLY_POLL_SECONDS`. |
| `LISTEN_PORT` | `9122` | Port for `/metrics`, `/healthz` and `/readyz` |

### What the bridge asks the device

| Method | When | What for |
| --- | --- | --- |
| `Shelly.GetDeviceInfo` | At start, after an outage, after a device restart, and every 10 minutes | Model, firmware and id. Needs no password. |
| `Shelly.GetStatus` | Every poll | Every component's status in one reply: the same objects that `Switch.GetStatus`, `Sys.GetStatus`, `WiFi.GetStatus`, `Cloud.GetStatus`, `Mqtt.GetStatus` and `Input.GetStatus` return one by one |
| `Switch.GetConfig` (`id=0`) | With `Shelly.GetDeviceInfo` | The switch's power, voltage and current limits |

One `Shelly.GetStatus` instead of a call per component keeps each poll to one
request, or two with authentication: on firmware 1.7.x every authenticated
request first gets a `401` challenge. The device serves only a few
connections at a time.

A firmware update always restarts the device, so the bridge sees the new
version at the next poll and logs the change.

### HTTP endpoints

| Path | Answers |
| --- | --- |
| `/metrics` | The metrics above |
| `/healthz` | `200` while the process runs |
| `/readyz` | `200` if a poll succeeded in the last 3 poll intervals, else `503` |

### Logs

The bridge logs to stdout, one line per change, not one per poll. It never
logs the password.

```text
INFO shelly-bridge serving /metrics, /healthz and /readyz on :9122; polling http://192.168.1.25 every 15s with a password
INFO shelly-bridge found Plus1PM (model SNSW-001P16EU, gen 2), firmware 1.7.5, authentication on; device label 'boiler'
ERROR shelly-bridge cannot read the device: Shelly.GetStatus timed out after 5s; readings drop out 45s after the last good poll
ERROR shelly-bridge still cannot read the device after 10m (41 failed polls; since the last line: connection 14, timeout 26); latest: Shelly.GetStatus timed out after 5s
INFO shelly-bridge the device answers again after 12m (48 failed polls)
INFO shelly-bridge the device restarted 12s ago (reset reason: power-on)
INFO shelly-bridge firmware changed from 1.7.4 to 1.7.5
```

While the device stays away, there is one error line every 10 minutes, with
the failed polls by reason since the line before, however often the reason
changes. A wrong password is logged at once; the 5-minute backoff limits that
to one line per attempt.

## Getting started

You need a Shelly Gen2 device on your network, and Docker, or Python 3.12+ for
running from source.

### 1. Turn on authentication (recommended)

In the Shelly app or the device's web UI, open the device's settings and turn
on authentication. Note the password. Without it, anyone on the network can
switch the relay, with or without this bridge.

### 2. Start the exporter

> [!NOTE]
> **Before the first release:** `ghcr.io/yornik/shelly-bridge` only exists
> once release-please has published v1.0.0 and the package is public. Until
> then, build the image yourself:
> `docker build -t ghcr.io/yornik/shelly-bridge:latest .`

```bash
export SHELLY_PASSWORD='the-device-password'

docker run -d --name shelly-bridge -p 9122:9122 \
  -e SHELLY_URL=http://192.168.1.25 -e SHELLY_DEVICE_NAME=boiler -e SHELLY_PASSWORD \
  ghcr.io/yornik/shelly-bridge:latest
```

To run it from source instead: `pip install -r requirements.txt`, set the same
variables, then `python -m shelly_bridge`.

### 3. Check it works

```bash
docker logs shelly-bridge        # expect: "found Plus1PM (...)"
curl -s localhost:9122/metrics | grep -E '^shelly_(scrape_success|switch_power_watts)'
```

`shelly_scrape_success 1.0` means the poll worked. If it reads `0.0`, the log
says why:

| Log says | Fix |
| --- | --- |
| `the device requires a password (HTTP 401)` | Set `SHELLY_PASSWORD`. |
| `the device refused the password (HTTP 401)` | Correct `SHELLY_PASSWORD`, then restart the container so it doesn't wait out the 5-minute backoff. |
| `Shelly.GetDeviceInfo failed: ConnectionError` | Wrong address, or the device is offline. Check `SHELLY_URL`, and that the container can reach the device's network. |
| `… timed out after 5s` or `the poll ran past its 15s limit` | Weak Wi-Fi or a busy device. Check `shelly_wifi_rssi_dbm`. |
| `… answered HTTP 404` | Not a Gen2 device. Gen1 devices have no RPC API. |

### 4. Scrape it and import the dashboard

Point Prometheus at `:9122/metrics`. With the default 15-second poll, a
30-second scrape interval is plenty. Don't scrape less often than every 30 s:
the dashboard's Failed polls panel needs at least two scrapes a minute. Then import the dashboard; see
[Grafana dashboard](#grafana-dashboard).

## Grafana dashboard

[`dashboards/shelly.json`](dashboards/shelly.json) has these sections:

- **Now:** relay state, power, energy and cost in the selected range, device
  temperature and the last poll.
- **Power & relay:** average power over time, and the relay as a timeline.
- **Energy & cost:** energy and cost per day. Each bar is a day from midnight
  to midnight in the dashboard's time zone.
- **Supply:** voltage and current.
- **Device:** firmware, firmware update, restart required, uptime, protection
  errors, Shelly Cloud, temperature and Wi-Fi signal over time.
- **Bridge:** time since the last good poll, poll duration, poll success and
  failed polls by reason.

Gaps in the graphs are times the device couldn't be read or the bridge
wasn't running. They aren't drawn over. The tiles at the top show current
values only: when the device can't be read they say `No data`, never the last
reading.

**Cost uses the Tibber price.** The cost panels multiply the energy of each
15-minute slot by that slot's total price (energy plus tax) from
`tibber_power_price{type="total"}`, published by a
[tibber-exporter](https://github.com/Yornik/tibber-exporter). A slot without
a price uses the latest price of the hour before it. Without any price, or
without energy readings, the cost panels show `No data`; everything else works
without Tibber. Energy used while the device couldn't be read is costed in the
slot in which it answered again (for outages of up to a day), so cost and
energy agree. With more than one Tibber home, the highest price is used. The
price is in your Tibber account's currency; the panels show it as EUR.

To import it, go to **Dashboards → New → Import**, upload the file and pick
your Prometheus data source. The `Device` variable lists every `device` label
the bridge publishes. To provision it with the kube-prometheus-stack Grafana
sidecar, wrap the file in a `ConfigMap` labelled `grafana_dashboard: "1"`.

## Kubernetes

The image runs as uid 65532 with a read-only root filesystem and needs no
volume. Run it as a Deployment with one replica, a Service and a
ServiceMonitor. The pod must be able to reach the device's network. The image
tag below exists once v1.0.0 is released; see the note under
[Start the exporter](#2-start-the-exporter).

**1. If authentication is on, create a Secret** (encrypt it before
committing, for example with SOPS):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: shelly-bridge
  namespace: monitoring
type: Opaque
stringData:
  SHELLY_PASSWORD: the-device-password
```

**2. Run the container:**

```yaml
containers:
  - name: shelly-bridge
    image: ghcr.io/yornik/shelly-bridge:v1.0.0
    env:
      - name: SHELLY_URL
        value: http://192.168.1.25
      - name: SHELLY_DEVICE_NAME
        value: boiler
    envFrom:
      - secretRef:
          name: shelly-bridge
    ports:
      - name: http
        containerPort: 9122
    livenessProbe:
      httpGet: {path: /healthz, port: http}
    readinessProbe:
      httpGet: {path: /healthz, port: http}
    securityContext:
      allowPrivilegeEscalation: false
      capabilities: {drop: [ALL]}
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 65532
```

NOTE: Use `/healthz`, not `/readyz`, for the readiness probe. With `/readyz`,
the pod turns unready whenever the device is away. Kubernetes and Argo CD then
report a healthy bridge as broken, and generic alerts such as
`KubePodNotReady` fire for a problem that `shelly_scrape_success` already
reports. Use `/readyz` for an external check of whether the device is being
read.

The bridge only needs to reach the device on port 80. A NetworkPolicy that
allows only that egress limits what a compromised pod could reach.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest
python scripts/check_read_only.py
```

The tests run the real client against a fake Gen2 device on `127.0.0.1`, with
and without digest authentication. They need no Shelly.

### CI

[`checks.yaml`](.github/workflows/checks.yaml) holds every check. It runs on
pull requests and on `main`, and again on the release commit before an image
is published:

| Job | What it checks |
| --- | --- |
| Lint | `ruff check`, `ruff format`, `actionlint`, `hadolint` |
| Read-only guard | No write method, control path, private read, client library or allowlist change in the package. See [Read-only, and how that's enforced](#read-only-and-how-thats-enforced). |
| Test | `pytest` with coverage in the run summary. The tests also check that every metric the dashboard queries is exported. |
| Dashboard and metric names | Runs every dashboard query through `promtool`, the Prometheus parser. Runs the energy, cost and failed-poll queries on made-up data with a known answer: a price step, a 2-hour outage, a missing price and a bridge restart. Checks every exported metric name with `promtool check metrics`. |
| Audit | `pip-audit` over the runtime dependencies |
| Image | Builds the image, then runs it read-only as non-root against an unreachable device and checks that it serves `/metrics`, `/healthz` and `/readyz` and reports the failed poll |

Releases are cut by release-please from Conventional Commits and pushed to
`ghcr.io/yornik/shelly-bridge` with an SBOM and provenance. Every action is
pinned to a commit digest. Renovate keeps the digests and tool versions
current, with a three-day minimum release age.

## Security

See [SECURITY.md](SECURITY.md). In short:

- **A problem in this project**, such as a read-only bypass or a leaked
  password: report it privately via **Security → Report a vulnerability**,
  never in a public issue.
- **A problem in a Shelly device, its firmware, app or cloud:** don't publish
  it. Report it to Shelly through their
  [vulnerability reporting page](https://www.shelly.com/pages/security-information-and-vulnerability-reporting).

## Disclaimer & trademarks

**This is an independent, unofficial project.** It isn't made, endorsed,
sponsored, authorized or supported by Shelly Group SE (formerly Allterco
Robotics) or its subsidiaries, and has no affiliation with them.

**Trademarks.** **Shelly**, the Shelly logo and all related names, marks and
logos are trademarks or registered trademarks of Shelly Group SE. They are
used here only to identify the devices this software works with (nominative
use). That use doesn't imply any affiliation with or endorsement by Shelly
Group SE. This project doesn't use the Shelly logo.

**No support.** Shelly doesn't support this software. Don't contact Shelly
support about it; open an issue in this repository instead.

**Use at your own risk.** The exporter only reads, over the device's own local
API. The software comes without any warranty. See [LICENSE](LICENSE).

## License

[MIT](LICENSE).

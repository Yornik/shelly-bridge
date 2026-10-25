# Security policy

There are two kinds of security problem you might find. They go to different
places:

| You found a problem in… | Report it to | Section |
| --- | --- | --- |
| **This project**: the bridge code, its CI, the container image or the dashboard | This repository, privately | [Reporting a vulnerability in this project](#reporting-a-vulnerability-in-this-project) |
| **Shelly's devices or services**: the firmware, the local API, the Shelly app or Shelly Cloud | Shelly. **Not** this repository. | [Vulnerabilities in Shelly's devices](#vulnerabilities-in-shellys-devices) |

This is an independent, unofficial hobby project. It isn't made, endorsed or
supported by Shelly Group SE, and it can't fix, and doesn't speak for,
anything on their side.

## Reporting a vulnerability in this project

**Don't open a public issue.** Report it privately through GitHub:
**Security → Report a vulnerability** on this repository
([direct link](https://github.com/Yornik/shelly-bridge/security/advisories/new)).

Please include:

- what's affected (file, version or image tag);
- how to reproduce it, ideally against the fake device in `tests/` rather
  than a real one;
- what an attacker could do with it.

**Never include real passwords, device ids, MAC addresses or IP addresses**,
not even your own. Redact them as `<redacted>`.

This is maintained in spare time, so handling is best effort:

- **Acknowledgement:** within 7 days.
- **Fix:** shipped as a new release, as soon as one is ready.
- **Disclosure:** coordinated with you, and a GitHub security advisory credits
  you unless you'd rather not be named.

### What's in scope

The things that matter most, given that the bridge can hold the password of a
device that switches mains power:

- **Bypassing read-only.** Any way to make the bridge send a request outside
  `READ_ONLY_METHODS`, with a parameter it doesn't allow, to a path other
  than `/rpc/<method>` or to another host, for example through `SHELLY_URL`;
  or to get past the CI read-only guard (`scripts/check_read_only.py`). This
  is the most important class.
- **Password exposure.** `SHELLY_PASSWORD` ending up in logs, metrics, error
  messages, the image or CI output.
- **Metrics leaking more than documented**, for example the Wi-Fi network
  name, the device's IP address or its location.
- **The bridge's HTTP endpoints** (`/metrics`, `/healthz`, `/readyz`) being
  usable for anything beyond what they document.
- **Supply chain:** the image, the GitHub Actions workflows, dependency
  pinning, or the release process.

### Supported versions

Only the latest release gets security fixes. Run the newest
`ghcr.io/yornik/shelly-bridge` tag.

### Known and accepted, not vulnerabilities

- **A Gen2 device with authentication off can be switched by anyone on its
  network**, with or without this bridge. The README's warning covers it.
- **The device password is powerful by design.** Gen2 has one user, `admin`,
  and its password allows every change. This is how Shelly's authentication
  works.
- **Digest authentication over plain HTTP.** The password itself is never
  sent, but the readings are readable on the network. The Plus 1PM's firmware
  (1.7.x) serves HTTP only.
- **The bridge's endpoints have no authentication**, like every Prometheus
  exporter. Keep them on a trusted network, or restrict who can reach them
  (for example with a NetworkPolicy).

## Vulnerabilities in Shelly's devices

If you find a weakness in a **Shelly device, its firmware or local API, the
Shelly app or Shelly Cloud**, this project can't fix it. Please:

1. **Don't publish it**: not in an issue, pull request, discussion or commit
   here. A public write-up could let people switch devices that aren't theirs.
2. **Report it to Shelly.** Shelly has a
   [security information and vulnerability reporting page](https://www.shelly.com/pages/security-information-and-vulnerability-reporting)
   with a reporting form and its vulnerability disclosure policy. Use that.
3. **Only test what you own.** Use your own device and your own account.
   Don't access other people's devices, and stop as soon as you can show the
   problem exists. This bridge is read-only on purpose and isn't a testing
   tool.

Reporting a Shelly issue here is welcome only as a heads-up that something in
this project should change. For example: "a read method the bridge calls now
returns the device's location, the bridge should stop calling it". Send that
privately as well.

## Disclaimer

This project and its maintainers aren't affiliated with Shelly Group SE.
**Shelly** is a trademark of Shelly Group SE. The software is provided "as
is", without warranty; see [LICENSE](LICENSE).

#!/usr/bin/env python3
"""Fail if the shipped package could do anything but read.

The relay switches a 1.1 kW boiler, so "read-only" is checked rather than
promised. Over every module in shelly_bridge/ this asserts:

1. The RPC allowlist (api.READ_ONLY_METHODS, method -> allowed parameters) is
   exactly the copy pinned below. Adding a method means changing both files,
   which a reviewer sees.
2. Every string that looks like an RPC method (Component.Method) is on that
   list, compared case-insensitively as the device does.
3. Every string that looks like a URL path is one the bridge needs: the /rpc/
   prefix the client builds on, and its own /metrics, /healthz and /readyz.
4. No string or name mentions a write method, a Gen1 control path, firmware
   or backup paths, script or webhook calls, the device's outbound HTTP calls,
   or a read that exposes private data (full config, location, scripts, KVS).
5. No other way to talk to the device: no HTTP, WebSocket or MQTT client
   library is imported, nothing but requests' auth, adapters and exceptions
   modules is imported from requests, requests' module-level calls are not
   used, and no new Session is made outside ShellyClient.__init__.
6. One HTTP choke point. `.session` appears only in ShellyClient.__init__
   (which sets the session up) and in ShellyClient._get, and there only as
   the receiver of exactly one `.get(..., allow_redirects=False)` call. The
   package holds no other session call: no other HTTP verb attribute
   anywhere, no send() except the read-only adapter's call to its parent, no
   prepare_request, no mount outside __init__.
7. No dynamic access that would hide any of the above from this check:
   getattr, setattr, eval, exec, compile, __import__, importlib.

Only syntax is inspected (strings, names, attributes, imports), never
comments, so the comments explaining what is forbidden do not trip the check.
The runtime checks in shelly_bridge/api.py (the allowlist in _get, and an
adapter that checks every request as it is sent) back this up.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "shelly_bridge"

# The pinned copy. Keep it in step with READ_ONLY_METHODS in shelly_bridge/api.py,
# and only ever with methods that read.
EXPECTED_METHODS = {
    "Shelly.GetDeviceInfo": frozenset(),
    "Shelly.GetStatus": frozenset(),
    "Switch.GetConfig": frozenset({"id"}),
}

# The only path-like strings the package may contain. A bare "/" is the
# separator the URL check strips, not a path.
ALLOWED_PATHS = frozenset({"/", "/rpc/", "/metrics", "/healthz", "/readyz"})

# Write side and privacy-sensitive reads of the Gen2 API (every component page
# of shelly-api-docs.shelly.cloud/gen2), plus Gen1-style control paths that
# Gen2 firmware still serves. Matched case-insensitively in strings and names.
FORBIDDEN_FRAGMENTS = (
    # RPC methods that change something: .set covers Set, SetConfig, SetAll,
    # SetMany, SetAuth, SetProfile and SetTime.
    ".set",
    "toggle",
    "reboot",
    "factoryreset",
    "resetcounters",
    "resetwificonfig",
    ".update",
    "checkforupdate",
    "detectlocation",
    ".create",
    ".delete",
    ".put",
    ".trigger",
    ".eval",
    ".start",
    ".stop",
    ".scan",
    ".calibrate",
    ".open",
    ".close",
    "gotoposition",
    "backup",
    "defaultconfig",
    # Whole components that run code, store data or make the device call out.
    "script.",
    "schedule.",
    "webhook.",
    "kvs.",
    "virtual.",
    "http.get",
    "http.post",
    "http.request",
    "sensoraddon.",
    "matter.",
    # Reads that expose more than metrics need: coordinates, Wi-Fi names,
    # broker users, script code, the cloud key.
    "shelly.getconfig",
    "sys.getconfig",
    "wifi.getconfig",
    "mqtt.getconfig",
    "cloud.getconfig",
    "getcode",
    "getsetupcode",
    "ident=",
    # Paths outside /rpc: Gen1-style control, firmware upload, backups, the
    # handlers scripts register, the debug log.
    "/relay",
    "/roller",
    "/light",
    "/color",
    "/white",
    "/cct",
    "/rgb",
    "/ota",
    "/script",
    "/debug",
    "/settings",
    "turn=",
    "ws://",
    "wss://",
    "mqtt://",
)

# Strings that are only suspicious on their own: a parameter that adds the
# cloud key to the device info, and HTTP verbs other than GET.
FORBIDDEN_STRINGS = frozenset({"ident", "post", "put", "patch", "delete", "turn"})

# Client libraries that could reach the device around the allowlist.
FORBIDDEN_IMPORTS = (
    "aiohttp",
    "aioshelly",
    "http.client",
    "httpx",
    "paho",
    "socket",
    "urllib.request",
    "urllib3",
    "websocket",
    "websockets",
)

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request", "head", "options"})
WRITE_VERBS = HTTP_METHODS - {"get"}

# Where the session may be touched: ShellyClient.__init__ sets it up, and
# ShellyClient._get makes the one call.
CLIENT_CLASS = "ShellyClient"
SETUP_FUNCTION = "__init__"
CALL_FUNCTION = "_get"

# requests modules the package may import from. Everything else (requests.api,
# requests.sessions, `from requests import get`) offers a way around _get.
ALLOWED_REQUESTS_MODULES = frozenset({"requests.adapters", "requests.auth", "requests.exceptions"})

# Attributes that reach the network around _get or swap out the send-time check.
SETUP_ONLY_ATTRIBUTES = frozenset({"Session", "mount"})
NEVER_ATTRIBUTES = frozenset(
    {"prepare_request", "adapters", "get_adapter", "api", "sessions", "urlopen", "Request"}
)

# Builtins and modules that reach attributes or code by a computed name.
DYNAMIC_NAMES = frozenset(
    {"getattr", "setattr", "eval", "exec", "compile", "__import__", "importlib"}
)

_RPC_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*")
_ALLOWED_METHODS = {method.casefold() for method in EXPECTED_METHODS}


def _no_redirects(node: ast.Call) -> bool:
    return any(
        kw.arg == "allow_redirects"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in node.keywords
    )


def _scopes(tree: ast.AST) -> dict[ast.AST, tuple[str | None, str | None]]:
    """Each node -> (enclosing class name, enclosing function name)."""
    scopes: dict[ast.AST, tuple[str | None, str | None]] = {}

    def visit(node: ast.AST, cls: str | None, func: str | None) -> None:
        scopes[node] = (cls, func)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, cls, child.name)
            else:
                visit(child, cls, func)

    visit(tree, None, None)
    return scopes


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _session_get_call(node: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> ast.Call | None:
    """For `.session` in `<x>.session.get(...)`, the call; else None."""
    get = parents.get(node)
    call = parents.get(get) if get is not None else None
    if (
        isinstance(get, ast.Attribute)
        and get.attr == "get"
        and get.value is node
        and isinstance(call, ast.Call)
        and call.func is get
    ):
        return call
    return None


def _is_super_send(node: ast.Attribute, func: str | None) -> bool:
    """`super().send` inside a method called send: the adapter handing on a checked request."""
    value = node.value
    return (
        func == "send"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "super"
    )


def _check_transport(node: ast.AST, scope, parents, filename: str, line) -> tuple[list[str], int]:
    """Rules 5 to 7: how the package may reach the network. Returns (problems, session GETs)."""
    problems: list[str] = []
    calls = 0
    cls, func = scope
    in_client = cls == CLIENT_CLASS
    where = f"{filename}:{line}"
    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr == "session":
            call = _session_get_call(node, parents)
            if in_client and func == SETUP_FUNCTION:
                pass
            elif in_client and func == CALL_FUNCTION and call is not None:
                calls += 1
                if not _no_redirects(call):
                    problems.append(f"{where}: the session call must not follow redirects")
            else:
                problems.append(
                    f"{where}: the session is used outside {CLIENT_CLASS}.{SETUP_FUNCTION} "
                    f"and a single .get() in {CLIENT_CLASS}.{CALL_FUNCTION}"
                )
        elif attr in WRITE_VERBS:
            problems.append(f"{where}: .{attr}() is not a read; only GET goes to the device")
        elif attr == "get" and isinstance(node.value, ast.Name) and node.value.id == "requests":
            problems.append(f"{where}: requests.get() bypasses _get")
        elif attr == "send" and not _is_super_send(node, func):
            problems.append(f"{where}: .send() bypasses _get")
        elif attr in SETUP_ONLY_ATTRIBUTES and not (in_client and func == SETUP_FUNCTION):
            problems.append(f"{where}: .{attr} is only allowed in {CLIENT_CLASS}.{SETUP_FUNCTION}")
        elif attr in NEVER_ATTRIBUTES:
            problems.append(f"{where}: .{attr} reaches the network around _get")
    elif isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
        problems.append(f"{where}: {node.id}() hides what is called from this check")
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "importlib" or alias.name.startswith("importlib."):
                problems.append(f"{where}: forbidden import {alias.name}")
            elif alias.name.startswith("requests.") and alias.name not in ALLOWED_REQUESTS_MODULES:
                problems.append(f"{where}: forbidden import {alias.name}")
    elif isinstance(node, ast.ImportFrom) and node.module:
        module = node.module
        if module == "importlib" or module.startswith("importlib."):
            problems.append(f"{where}: forbidden import {module}")
        elif module == "requests":
            for alias in node.names:
                if f"requests.{alias.name}" not in ALLOWED_REQUESTS_MODULES:
                    problems.append(f"{where}: forbidden import from requests: {alias.name}")
        elif module.startswith("requests.") and module not in ALLOWED_REQUESTS_MODULES:
            problems.append(f"{where}: forbidden import {module}")
    return problems, calls


def check_source(source: str, filename: str) -> list[str]:
    """Problems found in one module's source."""
    problems = []
    tree = ast.parse(source, filename)
    scopes = _scopes(tree)
    parents = _parents(tree)
    http_calls = 0
    for node in ast.walk(tree):
        texts: list[str] = []
        line = getattr(node, "lineno", "?")
        found, calls = _check_transport(node, scopes[node], parents, filename, line)
        problems += found
        http_calls += calls
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            texts.append(value)
            if _RPC_METHOD.fullmatch(value) and value.casefold() not in _ALLOWED_METHODS:
                problems.append(f"{filename}:{line}: RPC method not allowlisted: {value}")
            if value.startswith("/") and value not in ALLOWED_PATHS:
                problems.append(f"{filename}:{line}: path not allowed: {value[:60]}")
            if value.casefold() in FORBIDDEN_STRINGS:
                problems.append(f"{filename}:{line}: forbidden string {value!r}")
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{n}" for n in names]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    problems.append(f"{filename}:{line}: forbidden import {name}")
        for text in texts:
            lowered = text.lower()
            for fragment in FORBIDDEN_FRAGMENTS:
                if fragment in lowered:
                    problems.append(
                        f"{filename}:{line}: write-side or private reference "
                        f"{fragment!r} in {text[:60]!r}"
                    )
    if http_calls:
        problems.append(f"{filename}: __http_calls__={http_calls}")
    return problems


def check_package(package: Path = PACKAGE) -> list[str]:
    problems: list[str] = []
    http_calls = 0
    for path in sorted(package.rglob("*.py")):
        for problem in check_source(path.read_text(encoding="utf-8"), str(path.name)):
            if "__http_calls__=" in problem:
                http_calls += int(problem.rsplit("=", 1)[1])
            else:
                problems.append(problem)
    if http_calls != 1:
        problems.append(
            f"expected exactly one HTTP call site (ShellyClient._get), found {http_calls}"
        )

    sys.path.insert(0, str(package.parent))
    from shelly_bridge.api import READ_ONLY_METHODS

    if dict(READ_ONLY_METHODS) != EXPECTED_METHODS:
        added = sorted(set(READ_ONLY_METHODS) - set(EXPECTED_METHODS))
        removed = sorted(set(EXPECTED_METHODS) - set(READ_ONLY_METHODS))
        changed = sorted(
            m
            for m in set(READ_ONLY_METHODS) & set(EXPECTED_METHODS)
            if READ_ONLY_METHODS[m] != EXPECTED_METHODS[m]
        )
        problems.append(
            f"READ_ONLY_METHODS changed: added {added}, removed {removed}, "
            f"parameters changed {changed}"
        )
    return problems


def main() -> int:
    problems = check_package()
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(f"read-only guard: {len(problems)} problem(s)")
        return 1
    print(f"read-only guard: OK ({len(EXPECTED_METHODS)} allowlisted read methods)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""/metrics, /healthz and /readyz over real HTTP."""

from __future__ import annotations

import pytest
import requests

from shelly_bridge.server import start_server


@pytest.fixture
def server():
    state = {"ready": False}
    srv = start_server(0, lambda: state["ready"], addr="127.0.0.1")
    srv.state = state
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()
    srv.server_close()


def test_metrics(server):
    resp = requests.get(f"{server.url}/metrics", timeout=5)
    assert resp.status_code == 200
    assert "shelly_scrape_success 0.0" in resp.text.splitlines()
    assert 'shelly_scrape_errors_total{reason="timeout"} 0.0' in resp.text.splitlines()
    assert "_created" not in resp.text


def test_healthz_answers_while_the_process_runs(server):
    resp = requests.get(f"{server.url}/healthz", timeout=5)
    assert (resp.status_code, resp.text) == (200, "ok\n")


def test_readyz_follows_the_last_good_poll(server):
    assert requests.get(f"{server.url}/readyz", timeout=5).status_code == 503
    server.state["ready"] = True
    assert requests.get(f"{server.url}/readyz", timeout=5).status_code == 200


def test_unknown_path_and_other_methods(server):
    assert requests.get(f"{server.url}/", timeout=5).status_code == 404
    resp = requests.post(f"{server.url}/metrics", timeout=5)
    assert resp.status_code == 405
    assert resp.headers["Allow"] == "GET, HEAD"

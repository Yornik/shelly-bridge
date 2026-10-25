"""The dashboard is only useful if it asks for metrics the exporter publishes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from prometheus_client import REGISTRY

from scripts.dashboard_rules import expressions
from shelly_bridge import metrics

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboards" / "shelly.json"

# Queried from other exporters on purpose; promtool still parses them.
EXTERNAL_PREFIXES = ("tibber_",)


def load():
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def exported_names() -> set[str]:
    """Every sample name the bridge can serve: health metrics, plus each device
    family (counters with their _total suffix, as queries use them)."""
    health = {s.name for m in REGISTRY.collect() for s in m.samples if s.name.startswith("shelly_")}
    return health | set(metrics.FAMILIES)


def test_every_queried_metric_is_exported():
    queried = set()
    for _, expr in expressions(load()):
        queried |= set(re.findall(r"\bshelly_[a-z0-9_]+", expr))
    assert queried, "dashboard has no shelly queries"
    assert queried - exported_names() == set()


def test_only_known_external_metrics_are_queried():
    for where, expr in expressions(load()):
        for name in re.findall(r"\b([a-z][a-z0-9_]*)\{", expr):
            assert name.startswith(("shelly_", *EXTERNAL_PREFIXES)), (where, name)


def test_every_query_variable_is_replaced():
    # A variable that the dashboard does not define reaches Prometheus as text
    # and the panel shows an error.
    for where, expr in expressions(load()):
        assert "$" not in expr, where


def test_cost_panels_say_they_use_the_tibber_price():
    cost_panels = 0
    for panel in load()["panels"]:
        exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
        if "tibber_power_price" in exprs:
            cost_panels += 1
            assert "Tibber price" in panel["description"], panel["title"]
            # Empty can also mean no Shelly energy, so the panel must not blame Tibber.
            assert panel["fieldConfig"]["defaults"]["noValue"] == "No data"
            assert "No data means" in panel["description"], panel["title"]
    assert cost_panels == 2


def test_no_value_text_never_looks_healthy():
    for panel in load()["panels"]:
        no_value = panel.get("fieldConfig", {}).get("defaults", {}).get("noValue")
        assert no_value in (None, "No data", "No good poll"), panel["title"]


def test_stat_tiles_show_current_values_only():
    # A range query with lastNotNull keeps showing the last reading of an outage.
    for panel in load()["panels"]:
        if panel["type"] == "stat":
            for target in panel["targets"]:
                assert target.get("instant") is True, panel["title"]


def test_per_day_bars_are_drawn_over_their_own_day():
    # Each value covers the day after its point (offset -1d), so the bar must
    # be drawn after the point too.
    per_day = [p for p in load()["panels"] if p.get("interval") == "1d"]
    assert len(per_day) == 2
    for panel in per_day:
        assert panel["fieldConfig"]["defaults"]["custom"]["barAlignment"] == 1, panel["title"]
        assert "offset -1d" in panel["targets"][0]["expr"], panel["title"]


def test_bars_count_each_event_once():
    # $__rate_interval is wider than the step, so bars built on it overlap.
    for panel in load()["panels"]:
        custom = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
        if custom.get("drawStyle") == "bars":
            for target in panel["targets"]:
                assert "$__rate_interval" not in target["expr"], panel["title"]


def test_no_home_id_or_device_id_is_hardcoded():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "home_id" not in text
    assert "shellyplus" not in text
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)


def test_gaps_stay_gaps():
    for panel in load()["panels"]:
        custom = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
        if panel["type"] in ("timeseries", "state-timeline"):
            assert custom.get("spanNulls") is False, panel["title"]


def test_panel_ids_are_unique_and_layout_does_not_overlap():
    panels = load()["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))

    cells = set()
    for p in panels:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, f"{p['title']} overlaps another panel"
                cells.add((x, y))


def test_dashboard_is_portable():
    dashboard = load()
    assert dashboard["id"] is None
    assert dashboard["uid"] == "shelly-bridge"
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["datasource"]["type"] == "datasource"
    assert variables["device"]["query"]["query"] == "label_values(shelly_device_info, device)"
    for panel in dashboard["panels"]:
        if panel["type"] != "row":
            assert panel["datasource"]["uid"] == "${datasource}", panel["title"]
            for target in panel["targets"]:
                assert target["datasource"]["uid"] == "${datasource}", panel["title"]

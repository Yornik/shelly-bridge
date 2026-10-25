#!/usr/bin/env python3
"""Print a `promtool test rules` file that checks the dashboard's sums.

The cost queries multiply each 15-minute slot's energy by the price of that
same slot. An offset that is a little wrong still parses and still draws a
plausible number, so this evaluates the real panel queries on made-up series
with a known answer. A constant 60 W load uses 1.44 kWh a day, and every case
below must still add up to it:

- the price steps from 0.20 to 0.30 at noon;
- the device can't be read for 2 hours, and the bridge adds that energy in
  one step when it answers again;
- the Tibber exporter misses 45 minutes of prices;
- the bridge restarts, and its counter starts again at 0 (this one loses the
  30 s between the last sample before the restart and the first after).

It also checks that the Failed polls bars add up to the failures, at more than
one bar width.
"""

from __future__ import annotations

import json
import sys

from dashboard_rules import VARIABLES, expressions

# 60 W is 1800 J every 30 s, so one day is 2880 samples and 1.44 kWh.
ENERGY = 'shelly_switch_energy_joules_total{device="example",switch="0"}'
PRICE = 'tibber_power_price{type="total",home_id="example"}'
OTHER_PRICE = 'tibber_power_price{type="energy",home_id="example"}'
ERRORS = 'shelly_scrape_errors_total{reason="timeout"}'
DAY = 2880
HALF = DAY // 2
STEADY = f"0+1800x{2 * DAY}"


def _query(found: dict[str, str], title: str) -> str:
    return found[f"{title} / A"]


def _case(series: list[dict], checks: list[tuple[str, str, float]]) -> dict:
    return {
        "interval": "30s",
        "input_series": series,
        "promql_expr_test": [
            {
                # Rounded to 0.001: increase() extrapolates at the window edges.
                "expr": f"round({expr}, 0.001)",
                "eval_time": at,
                "exp_samples": [{"labels": "{}", "value": round(expected, 3)}],
            }
            for expr, at, expected in checks
        ],
    }


def build(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        dashboard = json.load(f)
    found = dict(expressions(dashboard))
    energy, cost = _query(found, "Energy"), _query(found, "Cost")
    energy_day, cost_day = _query(found, "Energy per day"), _query(found, "Cost per day")
    # The range panels see $__range as 1d, so they are checked at the end of day 1.
    # The per-day bars sit at the start of their day, so day 1's bar is at 0s.
    if VARIABLES["$__range"] != "1d":
        raise SystemExit("the checks below expect dashboard_rules to read $__range as 1d")

    cases = [
        # The price is 0.20 until noon and 0.30 after.
        _case(
            [
                {"series": ENERGY, "values": STEADY},
                {"series": PRICE, "values": f"0.2x{HALF - 1} 0.3x{HALF + DAY}"},
                # A different price type must not be used.
                {"series": OTHER_PRICE, "values": f"9x{2 * DAY}"},
            ],
            [
                (energy, "1d", 1.44),
                (cost, "1d", 0.72 * 0.20 + 0.72 * 0.30),
                (energy_day, "0s", 1.44),
                (cost_day, "0s", 0.72 * 0.20 + 0.72 * 0.30),
            ],
        ),
        # The device can't be read from 10:00 to 12:00. The bridge drops its
        # series, then adds the 2 hours' energy at the first poll after.
        _case(
            [
                {"series": ENERGY, "values": f"0+1800x1199 _x240 {1440 * 1800}+1800x{DAY}"},
                {"series": PRICE, "values": f"0.25x{2 * DAY}"},
            ],
            [
                (energy, "1d", 1.44),
                (cost, "1d", 1.44 * 0.25),
                (energy_day, "0s", 1.44),
                (cost_day, "0s", 1.44 * 0.25),
            ],
        ),
        # No price from 10:00 to 10:45: the latest earlier price fills in.
        _case(
            [
                {"series": ENERGY, "values": STEADY},
                {"series": PRICE, "values": f"0.25x1199 _x90 0.25x{DAY}"},
            ],
            [(cost, "1d", 1.44 * 0.25), (cost_day, "0s", 1.44 * 0.25)],
        ),
        # The bridge restarts at noon and its counter starts again at 0. Both
        # lose only the 30 s between the last sample before and the first after.
        _case(
            [
                {"series": ENERGY, "values": f"0+1800x{HALF - 1} 0+1800x{HALF + DAY}"},
                {"series": PRICE, "values": f"0.25x{2 * DAY}"},
            ],
            [
                (energy, "1d", 1.439),
                (cost, "1d", 1.439 * 0.25),
                (energy_day, "0s", 1.439),
                (cost_day, "0s", 1.439 * 0.25),
            ],
        ),
    ]

    # Failed polls: 120 failures between 1h and 2h. The bars must add up to 120
    # whatever width Grafana picks, so each failure is in exactly one bar.
    failed = next(
        t["expr"]
        for panel in dashboard["panels"]
        if panel.get("title") == "Failed polls"
        for t in panel["targets"]
    )
    bars = {
        "series": ERRORS,
        "values": "0x120 1+1x119 120x240",
    }
    failed_case = {
        "interval": "30s",
        "input_series": [bars],
        "promql_expr_test": [
            {
                "expr": "round(sum_over_time(("
                + failed.replace("$__interval", step)
                + f")[3h:{step}]))",
                "eval_time": "4h",
                "exp_samples": [{"labels": '{reason="timeout"}', "value": 120}],
            }
            for step in ("1m", "5m", "30m")
        ],
    }
    return {"rule_files": [], "evaluation_interval": "30s", "tests": [*cases, failed_case]}


if __name__ == "__main__":
    json.dump(build(sys.argv[1]), sys.stdout, indent=2)

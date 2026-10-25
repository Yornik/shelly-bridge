from __future__ import annotations

from shelly_bridge.energy import WH_TO_JOULES, EnergyCounter


def test_first_reading_is_a_baseline():
    counter = EnergyCounter()
    assert counter.observe(5000.0) is False
    assert counter.joules == 0


def test_adds_only_growth():
    counter = EnergyCounter()
    for total in (10.0, 10.5, 12.0, 12.0):
        counter.observe(total)
    assert counter.joules == 2.0 * WH_TO_JOULES
    assert counter.resets == 0


def test_a_drop_adds_nothing_and_counts_a_reset():
    counter = EnergyCounter()
    counter.observe(100.0)
    counter.observe(101.0)
    assert counter.observe(0.0) is True
    assert counter.joules == WH_TO_JOULES
    assert counter.resets == 1
    counter.observe(0.5)
    assert counter.joules == 1.5 * WH_TO_JOULES


def test_never_goes_back():
    counter = EnergyCounter()
    seen = []
    for total in (50.0, 51.0, 3.0, 2.0, 4.0, 60.0, 59.9, 61.0):
        counter.observe(total)
        seen.append(counter.joules)
    assert seen == sorted(seen)
    assert counter.resets == 3

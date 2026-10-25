"""Turn the device's energy total into a counter that never goes back.

A switch reports aenergy.total in Wh. Since firmware 1.1.0 it survives a power
cut, but it still goes back: to 0 after a counter reset or a factory reset, and
by a little when the last value saved before a power cut is older than the
last value read. Published as-is, Prometheus' increase() would read any drop as
a counter reset and count the whole remaining total as new energy.

So the bridge keeps its own counter and adds only what the device total grew
by between two polls:

- The first reading after the bridge starts is a baseline. The counter starts
  at 0, which is an ordinary counter reset to increase() and rate().
- A drop adds nothing. The new value becomes the baseline and `resets` grows.
  At most the energy of one poll interval before the drop is lost.
- Energy used while the device could not be read is added at the first poll
  after it answers again, in one step. Totals stay right.
"""

from __future__ import annotations

WH_TO_JOULES = 3600.0


class EnergyCounter:
    def __init__(self) -> None:
        self.previous_wh: float | None = None
        self.joules = 0.0
        self.resets = 0

    def observe(self, total_wh: float) -> bool:
        """Take one reading. Returns True if the device total went back."""
        if self.previous_wh is None:
            self.previous_wh = total_wh
            return False
        delta = total_wh - self.previous_wh
        self.previous_wh = total_wh
        if delta < 0:
            self.resets += 1
            return True
        self.joules += delta * WH_TO_JOULES
        return False

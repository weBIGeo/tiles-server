#############################################################################
# weBIGeo Tiles
# Copyright (C) 2026 Gerald Kimmersdorfer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#############################################################################
#
# Shared "tiles/s" throughput helper for tile_creators/* generation loops
# (see debug_ortho.py and cosmos_snow.py), so both report progress the same
# way instead of each reinventing a rate calculation.

import time
from collections import deque

# How far back the rate is averaged over. Windowed rather than cumulative
# since generation runs resume-skip already-cached tiles almost instantly
# before slowing down on freshly generated ones - a cumulative done/elapsed
# average would stay misleadingly high for a long time after such a burst.
DEFAULT_WINDOW_SECONDS = 5.0


class RateTracker:
    """Tracks recent throughput for a processes.py progress message. Create
    one per generation run and call sample(done) with the same cumulative
    count passed to processes.update()."""

    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS):
        self._window_seconds = window_seconds
        self._samples: deque[tuple[float, int]] = deque()

    def sample(self, done: int) -> str:
        """Record `done` (cumulative count) now, and return the current rate
        formatted as e.g. "8.3 tiles/s"."""
        now = time.monotonic()
        self._samples.append((now, done))
        cutoff = now - self._window_seconds
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            self._samples.popleft()

        t0, d0 = self._samples[0]
        dt = now - t0
        rate = (done - d0) / dt if dt > 0 else 0.0
        return f"{rate:.1f} tiles/s"

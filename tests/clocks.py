"""Clocks for deadline tests that move only when the test moves them.

A wall-clock budget makes a deadline test measure the runner's load: on a busy host
the budget runs out before the code reaches the wait it is meant to bound, and the
test either fails or passes without exercising anything. These clocks put the
budget boundary exactly where the test says. Database lock and statement timeouts
derived from them are still real, which is what the lock-wait tests exercise.
"""

import time


class ManualClock:
    """A monotonic clock frozen until the test advances it."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ShiftedClock:
    """Real monotonic time plus an offset the test can jump.

    Before the jump the budget is effectively unspent. After it the remainder runs
    down in real time, so post-deadline checks behave exactly as in production --
    which a frozen clock would defeat for work that completes late.
    """

    def __init__(self):
        self._offset = 0.0

    def __call__(self):
        return time.monotonic() + self._offset

    def jump_to(self, instant):
        self._offset = instant - time.monotonic()

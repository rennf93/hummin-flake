"""Tap vs hold for the push-to-talk key, driven by caller-supplied timestamps."""


class KeyTracker:
    def __init__(self, hold_ms=300):
        self.hold_s = hold_ms / 1000
        self._down_at = None
        self._holding = False

    def down(self, t):
        if self._down_at is None:  # ignore key auto-repeat
            self._down_at = t
            self._holding = False

    @property
    def pressed(self):
        return self._down_at is not None

    def tick(self, t):
        if self._down_at is not None and not self._holding and t - self._down_at >= self.hold_s:
            self._holding = True
            return "hold_start"
        return None

    def up(self, t):
        if self._down_at is None:
            return None
        held = self._holding
        self._down_at = None
        self._holding = False
        return "hold_end" if held else "tap"

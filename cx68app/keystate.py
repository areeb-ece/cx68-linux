"""Display model of the firmware's trigger logic, run on every polled frame.

The keyboard only reports travel, not whether it considers a key pressed, so this is an
estimate from the key's settings. Release is an absolute depth (verified: release 2.80 mm
-> OS release at ~2.84 mm after a full press). What happens when a key passes actuation but
never reaches a release depth set deeper than actuation is unknown; this model releases it
on rising back above actuation. The key test (OS events) is the ground truth.
"""
IDLE, TRAVEL, PRESSED, RESETTING = range(4)
REST_MM = 0.05      # below this a key counts as at rest
TURN_MM = 0.08      # movement needed to count as a change of direction


class KeyStates:
    def __init__(self, slots=128):
        self.state = [IDLE] * slots
        self._down = [False] * slots       # registered by the (modelled) firmware
        self._low = [0.0] * slots          # shallowest point since release
        self._high = [0.0] * slots         # deepest point since press
        self._released = [False] * slots   # released and not yet back at rest
        self.settings = None

    def configure(self, actuation, release, rt_press, rt_release, rapid):
        """Per-slot lists in mm; rapid: per-slot bools."""
        self.settings = list(zip(actuation, release, rt_press, rt_release, rapid))

    def update(self, depth):
        if self.settings is None:
            return self.state
        for s, d in enumerate(depth):
            act, rel, rt_p, rt_r, rapid = self.settings[s]
            if self._down[s]:
                self._high[s] = max(self._high[s], d)
                if rapid:
                    up = d < act or self._high[s] - d >= rt_r
                else:
                    up = d < (rel if self._high[s] >= rel else act)
                if up:
                    self._down[s], self._released[s], self._low[s] = False, True, d
            else:
                self._low[s] = min(self._low[s], d)
                if d >= act and (not rapid or self._low[s] < act or d - self._low[s] >= rt_p):
                    self._down[s], self._high[s] = True, d
            if self._down[s]:
                self.state[s] = PRESSED
            elif d < REST_MM:
                self.state[s], self._released[s], self._low[s] = IDLE, False, d
            elif self._released[s] and d - self._low[s] < TURN_MM:
                self.state[s] = RESETTING
            else:
                self.state[s], self._released[s] = TRAVEL, False
        return self.state

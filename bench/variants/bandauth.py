"""bandauth - band control with authority-scaled margins, capped (candidate for fanctl).

Each sensor's excess is divided by its authority weight w = rise / top_rise,
floored at W_MIN, so a weak sensor's margins shrink to at most W_MIN of the
normal ones (raise within keep_below * W_MIN of its target) but never to zero.
"""
import os

import fanctl

W_MIN = float(os.environ.get("BANDAUTH_WMIN", "0.25"))


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super().__init__(cfg, setting, now)
        self.scaled = []

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        rows = []
        for s in r.sensors:
            q = self.samples[s.name]
            m = sum(q) / len(q)
            rows.append((m - s.target, max(1.0, m - r.ambient)))
        top = max(rise for _x, rise in rows)
        smooth = max(x / max(W_MIN, rise / top) for x, rise in rows)
        self.scaled = [(t, x) for t, x in self.scaled if now - t <= max(c.down_interval, c.probe_interval)]
        self.scaled.append((now, smooth))
        recent = lambda w: max(x for t, x in self.scaled if now - t <= w)
        if e >= 0 or smooth > -c.keep_below:
            if self.speed < c.ceiling and (e >= 0 or now - self.last_change >= c.up_interval):
                return self._set(now, min(c.ceiling, self.speed + c.up_step), "up", r)
        elif self.speed > c.floor:
            elapsed = now - self.last_change
            low, probe_low = -(c.keep_below + c.band), -(c.keep_below + c.probe_margin)
            if elapsed >= c.down_interval and e < low * W_MIN and recent(c.down_interval) < low:
                return self._set(now, max(c.floor, self.speed - c.down_step), "down", r)
            if elapsed >= c.probe_interval and e < probe_low * W_MIN and recent(c.probe_interval / 2) < probe_low:
                return self._set(now, max(c.floor, self.speed - c.down_step), "probe", r)
        return fanctl.Decision(None, [])

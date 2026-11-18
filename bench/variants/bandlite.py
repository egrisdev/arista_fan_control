"""bandlite - stock band control, but each sensor's margin is scaled by its fan authority.

The fans move a sensor's temperature in proportion to its rise over the inlet
(fan law: rise ~ fan ** -k). Stock band holds every sensor 2-6 C under target,
so in a warm room it chases the front panel (rise ~4 C) the fans can hardly
cool. Here each sensor's excess is expressed in "hottest-rise degrees":
excess * top_rise / own_rise, so a weak sensor only counts once it is close
to its target. The raw over-target raise (e >= 0) is unchanged.
"""
import fanctl


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super().__init__(cfg, setting, now)
        self.scaled = []    # (time, authority-scaled smoothed hottest excess)

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        rows = []
        for s in r.sensors:
            q = self.samples[s.name]
            m = sum(q) / len(q)
            rows.append((m - s.target, max(1.0, m - r.ambient)))
        top = max(rise for _x, rise in rows)
        smooth = max(x * top / rise for x, rise in rows)
        self.scaled = [(t, x) for t, x in self.scaled if now - t <= c.probe_interval] + [(now, smooth)]
        recent = lambda w: max(x for t, x in self.scaled if now - t <= w)
        if e >= 0 or smooth > -c.keep_below:
            if self.speed < c.ceiling and (e >= 0 or now - self.last_change >= c.up_interval):
                return self._set(now, min(c.ceiling, self.speed + c.up_step), "up", r)
        elif self.speed > c.floor:
            elapsed = now - self.last_change
            low, probe_low = -(c.keep_below + c.band), -(c.keep_below + c.probe_margin)
            if elapsed >= c.down_interval and recent(c.down_interval) < low:
                return self._set(now, max(c.floor, self.speed - c.down_step), "down", r)
            if elapsed >= c.probe_interval and recent(c.probe_interval / 2) < probe_low:
                return self._set(now, max(c.floor, self.speed - c.down_step), "probe", r)
        return fanctl.Decision(None, [])

"""bandplus - fanctl's band control with four small changes, for the measured plant.

Stock band (fanctl.Controller.regulate) keeps the hottest sensor, by its
4-poll mean, between keep_below and keep_below + band C under its target: +5 %
when inside keep_below (at once when a raw reading reaches target), -2 % every
down_interval when band C cooler than that, a -2 % probe every probe_interval
when at least probe_margin cooler. On the step-test plant (2026-10-16) that
  (a) chases the front-panel sensor in warm spells: it mostly tracks the inlet
      (5.7 C rise over inlet at 30 %, 1.6 C at 70 %), so holding it 2-6 C under
      target takes 55-60 % fan at a 31 C inlet;
  (b) dithers at 30-35 %: at 25 C the hottest Phy sits 2.3 C under target at
      30 %, inside the noise of the 2 C raise threshold; the probe walks the
      fans back down to it, a noisy poll raises them 5 % again;
  (c) leaves a load step or warm spell 2 % per change (10 changes from 52 %).

Changes (raise rule, keep_below = 2, up_step, up_interval, smooth unchanged):
  1. Margins are in C for the sensor with the largest rise over inlet and
     shrink in proportion to a sensor's own rise: the fans move a sensor's
     temperature in proportion to its rise, so the front panel is held
     ~0.3 C under its target instead of 2-6 C.                         (a)
  2. A decrease goes to the lowest speed at which, by the fan law
     rise ~ fan ** -k, every sensor will still be keep_below + band under
     target (band = 1, for the hottest-rise sensor) and Z sd of its own
     reading noise under target; it is made only if it saves down_step %
     (or reaches the floor), down_interval (900 s, ~3.5 time constants at 30 %)
     after the last change and while every sensor stayed that cool over the
     wait. The probe is gone: a decrease never lands inside the raise
     threshold, so it does not come straight back up.                (b, c)
  3. k starts at 1 (measured 0.8-1.1 for the sensors that bind) and is
     re-measured on every decrease: the observed rise change of the sensor
     that limited it (taken at down_interval, or at once if a raise comes
     first), never below 1. A steeper plant than assumed costs one raise,
     not a cycle.
  4. No decrease while the inlet is warmer than probe_interval (30 min) ago:
     a rising inlet would take the room back and cost a raise.           (c)
"""
import math

import fanctl

K = 1.0            # fan-law exponent assumed until a decrease measures one (measured 0.8-1.1)
Z = 3.0            # a decrease leaves each sensor this many sd of its own reading noise under target
NOISE_T = 3600.0   # s over which each sensor's reading noise is averaged
TUNING = dict(band=1.0, down_interval=900)   # band: what a decrease leaves beyond keep_below;
# probe_interval (1800 s, unchanged) is now the inlet-trend window; probe_margin is unused


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        cfg = fanctl.Config(**vars(cfg))
        cfg.__dict__.update(TUNING)
        super().__init__(cfg, setting, now)
        self.var, self.prev = {}, {}      # sensor -> reading-noise variance, previous reading
        self.hist = []                    # (time, slack, inlet) over the last probe_interval
        self.k, self.trial = K, None      # fan law in use; last decrease (time, sensor, rise, from, to)

    def regulate(self, now, r, e, smooth, latest, binding):
        c, rows = self.cfg, []
        for s in r.sensors:
            q = self.samples[s.name]
            m = sum(q) / len(q)
            v = self.var.get(s.name, 0.25)          # until measured: a whole-degree sensor
            if s.name in self.prev:
                v += ((s.current - self.prev[s.name]) ** 2 / 2 - v) * c.poll / NOISE_T
            self.var[s.name], self.prev[s.name] = v, s.current
            rows.append((s, m, max(1.0, m - r.ambient), math.sqrt(v)))
        top = max(x[2] for x in rows)
        hot = max((m - s.target) * top / rise for s, m, rise, sd in rows)     # stock: smooth
        need = [(s, m, rise, max((c.keep_below + c.band) * rise / top, Z * sd)) for s, m, rise, sd in rows]
        slack = max(m - s.target + n for s, m, rise, n in need)               # < 0: room to lower
        self.hist = [h for h in self.hist if now - h[0] < c.probe_interval] + [(now, slack, r.ambient)]
        if self.trial and (now - self.trial[0] >= c.down_interval or e >= 0 or hot > -c.keep_below):
            t0, name, r1, f1, f2 = self.trial
            r2 = [rise for s, m, rise, sd in rows if s.name == name]
            ok = r2 and self.speed == f2 and r2[0] > r1
            self.k = max(K, min(3 * K, math.log(r2[0] / r1) / math.log(f1 / f2))) if ok else K
            self.trial = None
        if e >= 0 or hot > -c.keep_below:
            if self.speed < c.ceiling and (e >= 0 or now - self.last_change >= c.up_interval):
                return self._set(now, min(c.ceiling, self.speed + c.up_step), "up", r)
        elif (self.speed > c.floor and now - self.last_change >= c.down_interval
              and max(x for t, x, i in self.hist if now - t < c.down_interval) < 0
              and r.ambient <= self.hist[0][2]):
            new, lim = float(c.floor), None
            for s, m, rise, n in need:    # lowest speed leaving n: rise * (speed / f) ** k <= target - n - inlet
                f = self.speed / (1.0 + (s.target - n - m) / rise) ** (1.0 / self.k)
                if f > new:
                    new, lim = f, (s.name, rise)
            new = max(c.floor, int(math.ceil(new - 1e-9)))
            if new <= self.speed - c.down_step or c.floor == new < self.speed:
                self.trial = (now,) + lim + (float(self.speed), float(new)) if lim else None
                return self._set(now, new, "down", r)
        return fanctl.Decision(None, [])

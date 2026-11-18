"""pi - PI on the hottest sensor's predicted excess, tuned on the 2026-10-16 step test.

Per sensor s (4-poll mean m_s, its own reading noise sd_s, target T_s, inlet A):

    g_s = K * (m_s - A) / fan               authority, C per % (fan law)
    x_s = m_s - T_s + SP0 + Z * sd_s        > 0: above its setpoint
        + tau * (8-poll slope_s, the part more than Z sd of its noise)

A first-order sensor ends tau x slope above where it is, so x_s is where the
sensor is heading, counting only a trend that clears its noise. The setpoint
sits Z noise sd under target, so a noisier sensor keeps a wider margin.

    err = max x_s                  if > 0: warming, raise
        = max (x_s + band_s)       if < 0: lower (no sensor ends above its setpoint
                                   after BAND_STEPS commit steps down, nor within
                                   the noise of its 4-poll mean, Z sd / 2)
        = 0                        otherwise: rest
    fan = I + KP * err,  I += KP * poll / TI * err

Lambda (IMC) tuning for a first-order lag g / (1 + tau s): TI = tau, KP = tau /
(g * lambda), lambda = LAMBDA_UP warming, LAMBDA cooling. tau = TAU40 * 40 / fan
(measured ~1 / fan). g is the largest g_s, the most fan-sensitive sensor (Phy or
Trident, 0.6-1.1 C/% at 30-40 %), so a weak one such as the front panel (0.1-0.2
C/%, mostly the inlet) gets a slower loop instead of its noise times 1 / g.
An increase is committed once the output is COMMIT % above the fans. A decrease
needs err < 0 and down_interval since the last change, and goes COMMIT % at a time.

Urgent: the hottest raw reading at its target: raise at once by its predicted
excess over the setpoint (4-poll trend) / g, COMMIT..URGENT_MAX %; again when it
reads higher still, or after up_interval.
"""
import math

import fanctl

K = 0.8            # fan-law exponent, rise over inlet ~ fan ** -K (step test: 0.75-1.1, hot sensors)
TAU40 = 165.0      # s, sensor time constant at 40 % (step test: Phy/Trident 135-200 s), ~ 1 / fan
SP0 = 0.5          # C under target for each sensor's 4-poll mean, plus...
Z = 3.0            # ...Z sd of its own reading noise; a trend counts once Z sd clear of noise
LAMBDA_UP = 200.0  # s, closed-loop time to correct warming (~ tau)
LAMBDA = 600.0     # s, ...and to give fan back (slow: every change is a config commit)
COMMIT = 2         # %, smallest change worth a commit
BAND_STEPS = 1.5   # rest zone under the setpoint, in commit steps of each sensor
URGENT_MAX = 15    # %, largest single urgent raise
NOISE_T = 3600.0   # s, averaging time of the noise estimates
VAR0 = 0.25        # C^2, noise assumed until measured (a whole-degree sensor)


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super().__init__(cfg, setting, now)
        self.i = None          # integral part of the PI, %
        self.u_for = None      # the fan speed the PI was last synced with
        self.hist = {}         # sensor -> its last 8 raw readings in override (spans hand-offs)
        self.var = {}          # sensor -> noise variance, from successive readings
        self.urg = None        # raw reading at the last urgent raise

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        tau = TAU40 * 40.0 / self.speed
        up = land = -1e9
        g, xs = 0.05, {}
        for s in r.sensors:
            h = self.hist.setdefault(s.name, [])
            v = self.var.get(s.name, VAR0)
            if h:
                v = self.var[s.name] = v + ((s.current - h[-1]) ** 2 / 2 - v) * c.poll / NOISE_T
            h.append(s.current)
            del h[:-8]
            sd = math.sqrt(v)
            mean = sum(self.samples[s.name]) / len(self.samples[s.name])
            gs = K * max(0.0, mean - r.ambient) / self.speed
            g = max(g, gs)
            x = xs[s.name] = mean - s.target + SP0 + Z * sd
            if len(h) == 8:
                slope = (sum(h[4:]) - sum(h[:4])) / (16.0 * c.poll)
                x += tau * max(0.0, slope - Z * sd * math.sqrt(0.5) / (4 * c.poll))
            up = max(up, x)
            land = max(land, x + max(BAND_STEPS * COMMIT * gs, Z * sd / 2))
        err = up if up > 0 else min(0.0, land)
        lam = LAMBDA_UP if err > 0 else LAMBDA
        p = tau * err / (g * lam)                         # proportional part, KP * err
        if self.u_for != self.speed:                      # bumpless (re)start
            self.i, self.u_for = self.speed - p, self.speed
        if e >= 0 and self.speed < c.ceiling:             # urgent
            w = r.worst
            if self.urg is None or w.current > self.urg or now - self.last_change >= c.up_interval:
                h = self.hist[w.name][-4:]
                slope = (h[-1] + h[-2] - h[0] - h[1]) / (4.0 * c.poll) if len(h) == 4 else 0.0
                step = int(math.ceil((xs[w.name] + tau * max(0.0, slope)) / g))
                self.urg = w.current
                return self._move(now, min(c.ceiling, self.speed + max(COMMIT, min(URGENT_MAX, step))), p, r)
            return fanctl.Decision(None, [])
        self.urg = None
        hold = now - self.last_change < c.down_interval
        if not (err < 0 and hold):
            self.i += c.poll * err / (g * lam)            # integral part, KP * poll / TI * err
        self.i = min(c.ceiling - p, max(c.floor - p, self.i))
        u = self.i + p
        if u - self.speed >= COMMIT:
            return self._move(now, int(round(u)), p, r)
        if err < 0 and not hold and (self.speed - u >= COMMIT or int(round(u)) == c.floor < self.speed):
            return self._move(now, max(c.floor, self.speed - COMMIT), p, r)
        return fanctl.Decision(None, [])

    def _move(self, now, speed, p, r):
        self.i, self.u_for = speed - p, speed
        return self._set(now, speed, "up" if speed > self.speed else "down", r)

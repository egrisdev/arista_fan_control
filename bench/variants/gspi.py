"""gspi - gain-scheduled PI: a PI on y = (40 / fan) ** k, in which the plant is linear.

Step test (2026-10-16): each sensor s settles at inlet + R_s * y, the hottest Phy
groups with k = 0.8 within 2 % from 30 to 70 % fan, first order with tau ~ 165 s
* 40 / fan. Divide a sensor's distance to its setpoint by its own gain R_s =
rise_s / y_eq (y_eq: y lagged by tau, what the sensors have had time to follow)
and every sensor answers a change of y one for one at any speed, so one PI
tuning fits all of them:

    sp_s = target_s - Z * sd_s - SURGE * rise_s     setpoint for its 4-poll mean
    h    = min_s (sp_s - mean_s) / R_s               room, in y, of the sensor that binds
    err  = h if h < 0 (warm); h - step/2 if h > step (room for one step down); else 0
    I   += poll / LAMBDA * err ;  u = I + tau / LAMBDA * err      (lambda tuning)
    raise at once, lower LAMBDA after the last change, once u is COMMIT % away;
    a decrease never plans past y_eq + err (where the sensors would settle).

 * sd_s: the sensor's own reading noise, from successive readings.
 * SURGE: heat added suddenly (the +35 % Phy load step) climbs ~1 C a poll at
   30 %; it shows two polls later, so each sensor keeps this share of its rise
   in reserve.
 * Urgent: a raw reading inside that reserve while the 4-poll mean is over its
   setpoint: go at once to the y at which that sensor settles at its setpoint,
   taking its rise where it is heading (mean + tau x 4-poll trend); again when
   the hottest reading climbs further, or after up_interval.
 * Local sensitivity: k is learnt. Comparing the binding sensor's settled rise
   before a move with where it is heading after it (extrapolated with the share
   of the move y_eq has followed) gives the fan law over that move; k goes
   halfway to it, never below K (measured: Trident and CPU steepen below 35 %;
   a plant twice as steep as k would hunt, each linearised step going too far).
"""
import math

import fanctl

K = 0.8           # fan law: rise over inlet ~ fan ** -k, k at least K (hottest Phy groups)
TAU40 = 165.0     # s, sensor time constant at 40 % fan, ~ 1 / fan (Phy/Trident 135-200 s)
LAMBDA = 600.0    # s, closed-loop time constant: slow on purpose, every change is a commit
SURGE = 0.035     # share of each sensor's rise over inlet kept in reserve for a heat surge...
Z = 3.0           # ...plus this many sd of its own reading noise
COMMIT = 2        # % between the PI output and the fans before a change; also the rest band
NOISE_T = 3600.0  # s, averaging time of the noise estimates
VAR0 = 0.25       # C^2, noise assumed until measured (a whole-degree sensor)


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super().__init__(cfg, setting, now)
        self.k = K          # fan-law exponent in use
        self.i = None       # integral part of the PI, y
        self.u_for = None   # the fan speed the PI was last synced with
        self.y_eq = None    # y the sensors have settled to (lag tau)
        self.var = {}       # sensor -> noise variance
        self.last = {}      # sensor -> previous raw reading
        self.avg = {}       # sensor -> rise over inlet, low-passed by tau
        self.urg = None     # hottest raw excess at the last urgent raise
        self.bind = None    # (sensor, rise) that binds
        self.ref = None     # (sensor, speed, rise) last seen settled

    def _y(self, f):
        return (40.0 / f) ** self.k

    def _fan(self, y):
        return 40.0 / y ** (1.0 / self.k)

    def regulate(self, now, r, e, smooth, latest, binding):
        c, a = self.cfg, r.ambient
        y, tau = self._y(self.speed), TAU40 * 40.0 / self.speed
        lag = 1.0 - math.exp(-c.poll / tau)
        if self.u_for != self.speed:                       # (re)start: assume settled
            self.i = self.y_eq = y
            self.u_for = self.speed
        self.y_eq += (y - self.y_eq) * lag
        h = need = None
        for s in r.sensors:
            v = self.var.get(s.name, VAR0)
            if s.name in self.last:
                v = self.var[s.name] = v + ((s.current - self.last[s.name]) ** 2 / 2 - v) * c.poll / NOISE_T
            self.last[s.name] = s.current
            q = self.samples[s.name]
            m = sum(q) / len(q)
            rise = self.avg[s.name] = self.avg.get(s.name, m - a) * (1 - lag) + (s.current - a) * lag
            room = s.target - Z * math.sqrt(v) - a          # rise allowed, less noise
            sp = a + room - SURGE * (m - a)
            hs = (sp - m) / max(1.0, (m - a) / self.y_eq)   # gain >= 1 C per unit y
            if h is None or hs < h:
                h, self.bind = hs, (s.name, rise)
            if s.current > s.target - SURGE * (m - a) and m > sp:        # inside the reserve
                slope = (q[-1] + q[-2] - q[0] - q[1]) / (4.0 * c.poll) if len(q) == 4 else 0.0
                ys = y * room / ((1 + SURGE) * max(0.1, m - a + tau * max(0.0, slope)))
                need = ys if need is None else min(need, ys)
        lo, hi = self._y(c.ceiling), self._y(c.floor)
        if need is not None and self.speed < c.ceiling:
            if self.urg is None or e > self.urg or now - self.last_change >= c.up_interval:
                self.urg = e
                f = max(self.speed + COMMIT, self._fan(max(lo, need)))
                return self._move(now, min(c.ceiling, int(math.ceil(f))), 0.0, tau, r)
            return fanctl.Decision(None, [])
        self.urg = None
        step = self._y(self.speed - COMMIT) - y
        err = h if h < 0 else (h - step / 2 if h > step else 0.0)
        if err > 0:
            hi = min(hi, self.y_eq + err)                  # never plan past where it settles
        p = tau / LAMBDA * err
        self.i = min(hi, max(lo, self.i + c.poll / LAMBDA * err))
        u = self._fan(min(hi, max(lo, self.i + p)))
        if u >= self.speed + COMMIT or (u <= self.speed - COMMIT and now - self.last_change >= LAMBDA):
            return self._move(now, int(round(u)), p, tau, r)
        return fanctl.Decision(None, [])

    def _move(self, now, speed, p, tau, r):
        name, rise = self.bind
        if self.ref and self.ref[0] == name and self.ref[1] != self.speed and rise > 3.0:
            y0 = self._y(self.ref[1])
            done = (self.y_eq - y0) / (self._y(self.speed) - y0)   # share of the move followed
            if done >= 0.5:
                ss = self.ref[2] + (rise - self.ref[2]) / min(1.0, done)
                k = math.log(self.ref[2] / max(ss, 0.1)) / math.log(self.speed / self.ref[1])
                if k > 0:                                  # k <= 0: other heat moved it
                    f_eq, self.k = self._fan(self.y_eq), max(K, min(2.5 * K, (self.k + k) / 2))
                    self.y_eq = self._y(f_eq)
        if now - self.last_change >= 3 * tau:              # settled: new reference
            self.ref = (name, self.speed, rise)
        self.i, self.u_for = self._y(speed) - p, speed
        return self._set(now, speed, "up" if speed > self.speed else "down", r)

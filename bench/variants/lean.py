"""lean - integral control of y = (40 / fan) ** K, the variable in which the measured plant is linear.

Step test (2026-10-16): each sensor settles at inlet + R_s * y (K = 0.8 for the hot Phy and
Trident groups, 30-70 % fan), first order with tau ~ TAU40 * 40 / fan. A sensor's room to its
setpoint divided by its own gain R_s = rise_s / y is the change of y that brings it there;
relative to y that is just room / rise. So the error is the smallest relative room over all
sensors (the one that binds), and a sensor the fans barely move (front panel, rise ~4 C) asks
for a large change of y per C instead of being chased the way band control chases it.

    x   = min_s (target_s - NOISE - mean_s) / rise_s - MARGIN        mean: last 4 polls
    err = y * x if x < 0;  y * (x - step) if x > step;  else 0       step: y one down_step adds
    I  += poll / LAMBDA * err, never above y + err                   (anti-windup)
    raise once I is down_step % above the fans; lower once it is down_step % below and LAMBDA
    (~3 tau) after the last change, the first decrease after a raise only half as far.

Surge: heat added suddenly (the measured +35 % Phy load) climbs ~1 C a poll at 30 %, too fast
for a 4-poll mean. A sensor past its setpoint, reading inside its reserve and rising more than
its reserve over the last 4 polls sends the fans at once to the y at which it settles at its
setpoint, taking its rise where it is heading (mean + tau x trend). A reading at its target
raises up_step % at once, every poll (band's over-target raise).
"""
import math

import fanctl

K = 0.8          # fan law: a sensor settles at inlet + R * (40 / fan) ** K (Phy/Trident 0.75-1.1)
TAU40 = 165.0    # s, sensor time constant at 40 % fan, ~ 1 / fan (Phy/Trident 135-200 s)
LAMBDA = 600.0   # s, integral time, and the wait after any change before a decrease (~3 tau)
MARGIN = 0.05    # share of its rise each sensor keeps in reserve under its target...
NOISE = 0.5      # ...plus this, C: reading noise (Phy groups read whole degrees, sd 0.3-0.45)


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super().__init__(cfg, setting, now)
        self.i = self.synced = None     # integral, in y, and the speed it is synced with
        self.half = False               # last change was a raise: the next decrease goes halfway

    def regulate(self, now, r, e, smooth, latest, binding):
        c, a, d = self.cfg, r.ambient, self.cfg.down_step
        y, tau = (40.0 / self.speed) ** K, TAU40 * 40.0 / self.speed
        x, need = 1e9, y
        for s in r.sensors:
            q = self.samples[s.name]
            m = sum(q) / len(q)
            rise = max(1.0, m - a)
            xs = (s.target - NOISE - m) / rise - MARGIN                  # room, as a share of its rise
            x = min(x, xs)
            if xs < 0 and s.current >= s.target - MARGIN * rise and q[-1] - q[0] > MARGIN * rise:
                head = rise + tau * (q[-1] - q[0]) / ((len(q) - 1) * c.poll)   # where it is heading
                need = min(need, y * rise * (1 + xs) / head)               # y where it settles at setpoint
        f = 40.0 / need ** (1 / K)
        if self.speed < c.ceiling and (e >= 0 or f >= self.speed + d):
            return self._move(now, min(c.ceiling, max(self.speed + (c.up_step if e >= 0 else d), int(math.ceil(f)))), r)
        if self.synced != self.speed:
            self.i, self.synced = y, self.speed
        step = (self.speed / (self.speed - d)) ** K - 1
        err = y * (x if x < 0 else max(0.0, x - step))
        self.i = min(y + max(0.0, err), (40.0 / c.floor) ** K, max((40.0 / c.ceiling) ** K, self.i + c.poll / LAMBDA * err))
        u = 40.0 / self.i ** (1 / K)
        if u >= self.speed + d:
            return self._move(now, int(round(u)), r)
        if u <= self.speed - d and now - self.last_change >= LAMBDA:
            u = self.speed - (self.speed - u) / (2 if self.half else 1)
            return self._move(now, max(c.floor, min(self.speed - d, int(round(u)))), r)
        return fanctl.Decision(None, [])

    def _move(self, now, speed, r):
        self.i, self.synced, self.half = (40.0 / speed) ** K, speed, speed > self.speed
        return self._set(now, speed, "up" if speed > self.speed else "down", r)

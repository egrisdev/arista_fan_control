"""PID in-band fan control for fanctl (variant for bench.py).

The error is in fan %, not C. For each sensor: how far its filtered reading
plus NOISE_Z noise sd (so single readings stay under target too) sits outside
the dead zone SETPOINT +- DEADZONE, divided by how many C it moves per 1 % of
fan right now. The fan law rise ~ (1/fan)^k gives d(temp)/d(fan) =
k x rise / fan, so a Phy group (strong response) and the front panel (weak
response) see the same loop gain. The sensor needing the most fan sets X.

  u = KP x (X + TD x dX/dt) + (KP / TI) x integral(X dt)

D (on the measurement, upward only) uses ONE warming rate common to the whole
switch, not each sensor's own slope: g (1/s) is the least-squares fit of
slope_s = g x rise_s over all sensors (slope over the last 2 polls, rise =
filtered reading - inlet), counted only beyond D_Z sd of its own noise (from
the sensors' noise estimates), and ignored for SETTLE s after a decrease or a
resume from EOS (that warming is our own doing). Each sensor's derivative is
g x its own rise. Why pooled: the Phy sensors read in whole degrees, so in the
30 s a load step takes to carry one up to its target, its own slope cannot be
told from noise (a per-sensor D either never fires or fires on noise all day);
20+ sensors warming together can. KP x TD ~ the Phy time constant, so
X + TD x dX/dt is roughly where a sensor is heading at the current speed.

Every change is a config commit, so: a raise is committed at any whole %; a
decrease only when worth COMMIT %, by at most DOWN_MAX %, and SETTLE s after
the last change. The integral may not wind below the current speed meanwhile
(anti-windup), forgets inside the dead zone, and is reset to the committed
speed so a P/D kick sticks. A raw reading at or over its target (a Phy group
reading its 65 C target) raises by OVER_STEP at once.
"""
import math

import fanctl

SETPOINT = -1.0   # C vs target: hold the hottest sensor (+ NOISE_Z sd) here...
DEADZONE = 1.0    # C: ...give or take this much without acting
NOISE_Z = 2.5     # noise sd kept between the filtered reading and the zone
KP = 0.5          # share of the estimated needed change applied at once
TI = 120.0        # s integral time
TD = 500.0        # s derivative time (KP x TD = 250 s ~ the Phy time constant)
D_Z = 3.0         # sd of its own noise the common warming rate must exceed
FILTER = 30.0     # s low-pass on each sensor
COMMIT = 2        # % smallest decrease worth a config commit
DOWN_MAX = 3      # % largest single decrease (the fan law is steeper than K_FAN at low %)
SETTLE = 600      # s after any change before a decrease
OVER_STEP = 12    # % raise at once while a reading is at or over its target
K_FAN = 0.8       # fan-law exponent used to estimate C per % of fan
MIN_GAIN = 0.02   # C per %: assume every sensor moves at least this much


def _dead(z, width):
    return math.copysign(max(0.0, abs(z) - width), z)


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super(Variant, self).__init__(cfg, setting, now)
        self.integral = None    # % : the I part of u
        self.committed = None   # last speed this loop chose
        self.filt = {}          # sensor name -> (last 3 readings, filtered C)
        self.noise = {}         # sensor name -> poll-to-poll jitter variance (C^2)
        self.last_run = None
        self.last_down = None   # time of the last decrease (or resume)

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        dt = float(c.poll)
        a = dt / (FILTER + dt)
        if self.last_run is None or now - self.last_run > 3 * dt:
            self.filt = {}      # first call, or back from EOS auto: restart the filters,
            self.last_down = now    # and EOS may have run the fans faster than we resume
        self.last_run = now
        rows, num, den, var_num = [], 0.0, 0.0, 0.0
        for s in r.sensors:
            hist, level = self.filt.get(s.name, ([], s.current))
            var = self.noise.get(s.name, 0.0)
            if hist:
                var += a / 20 * ((s.current - hist[-1]) ** 2 / 2 - var)
            hist = (hist + [s.current])[-3:]
            level += a * (s.current - level)
            self.filt[s.name], self.noise[s.name] = (hist, level), var
            rise = max(0.0, level - r.ambient)
            if len(hist) == 3:  # common warming rate: fit slope_s = g x rise_s
                num += rise * (hist[2] - hist[0]) / (2 * dt)
                den += rise * rise
                var_num += rise * rise * var / (2 * dt * dt)
            rows.append((s, level, var, rise))
        g = max(0.0, (num - D_Z * math.sqrt(var_num)) / den) if den else 0.0
        if now - self.last_down < SETTLE:
            g = 0.0             # warming after our own decrease is expected
        x_p = x_i = -1e9
        for s, level, var, rise in rows:
            gain = max(MIN_GAIN, K_FAN * rise / self.speed)
            y = level + NOISE_Z * math.sqrt(var) - s.target - SETPOINT
            x_i = max(x_i, _dead(y, DEADZONE) / gain)
            x_p = max(x_p, _dead(y + TD * g * rise, DEADZONE) / gain)
        if self.committed != self.speed:     # first call, resume, floor: start from here
            self.integral = self.committed = self.speed
        settling = now - self.last_change < SETTLE
        if x_i == 0:    # inside the dead zone: forget what noise poking out of it added
            self.integral += (self.speed - self.integral) * dt / TI
        low = self.speed if settling else c.floor
        self.integral = min(c.ceiling, max(low, self.integral + KP / TI * x_i * dt))
        u = min(c.ceiling, max(c.floor, self.integral + KP * x_p))
        if e >= 0:
            u = min(c.ceiling, max(u, self.speed + OVER_STEP))
        want = self.speed
        if u >= self.speed + 1:
            want = int(u)
        elif not settling and (u <= self.speed - COMMIT or (u == c.floor and self.speed > c.floor)):
            want = max(int(math.ceil(u)), self.speed - DOWN_MAX)
        if want == self.speed:
            return fanctl.Decision(None, [])
        self.integral = self.committed = want    # the P/D kick sticks
        if want < self.speed:
            self.last_down = now
        return self._set(now, want, "up" if want > self.speed else "down", r)

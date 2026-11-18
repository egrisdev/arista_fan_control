"""Model-based fan control (variant for bench.py), tuned on the measured plant.

Model, from the step test of 2026-10-16: each sensor settles at

    T = inlet + R x (40 / fan%) ** K

and gets there with a first-order lag TAU = TAU40 x 40 / fan%. Measured: K 0.63-1.1
on the Phy and Trident groups that bind (0.8 fits them; the front panel is ~1.5),
tau ~100 s at 70 %, ~165 s at 40 %, ~250 s at 30 % (TAU40 = 180 s). K and TAU are
fixed; R is learned per sensor.

Estimator, every poll, per sensor:
  felt     (40 / fan) ** K, and the inlet, both lagged by TAU: what the sensors feel now
  x        (reading - lagged inlet) / felt: the R this sensor shows right now
  R        follows x with time constant SETTLE x TAU. This is the integral action: R
           is re-fitted wherever the fans run, so the model is right at the operating
           point even where K is off; K only sets how far one move goes. R is never
           below the mean x of the last two polls less ZN sd of its noise (a warming
           is believed within two polls).
  sd       reading noise, from poll-to-poll differences, averaged over NOISE s
  heading  where x is going: m2 + (TAU / 2 polls) x (m2 - m1), m1 and m2 the mean x
           of the two polls before and of the last two; z = (heading - R) / its sd

Load steps. One sensor's 2-poll heading is far too noisy to act on (the Phy groups
read in whole degrees), but a load warms many sensors at once: when the z's summed
over all n sensors exceed ZDET x sqrt(n), each sensor whose own z > ZN is taken at
its heading. On the measured plant a +35 % Phy load is caught 2 polls (30 s) after
it starts, ~1 C under target, and the heading is within a few % of the truth, so
one raise does it. The summed test is off for SETTLE x TAU after our own change:
real lags differ from TAU (CPU 90 s, board 240 s), so our own moves look like
warming for a while. Backstop for plants slower or noisier than measured: when the
mean of a sensor's last two readings is within one noise sd of its target, the fans
go up RESCUE % at once (a lone noisy reading at target does not count), at most
once per TAU so that each rescue has time to act.

Decisions:
  raise  when any sensor would settle within MARGIN + ZN sd of its target at the
         current inlet: at once, to that speed + STEP (a slowly rising inlet then
         costs few commits)
  lower  to the lowest speed that leaves 2 x MARGIN + ZN sd, if that saves STEP %
         (or reaches the floor), WAIT s after any change, and not while some R is
         still above its sensor's last two polls (it is cooling: wait for R).
         A raise within the wait after a decrease doubles the wait (up to
         hold_max): the model is off there (e.g. a steeper fan law than K), so the
         next decrease waits longer; a decrease that holds resets it.

Failure modes:
  - Sensors with little fan authority (small R, e.g. the front panel, which mostly
    tracks the inlet and binds above ~29.5 C): an error in R is a large error in
    fan %, since the speed needed scales as R ** (1 / K); there the controller
    steps up and down a few % around the peak of a warm day. If R ~ 0 and it still
    binds, no speed helps: the ceiling, and fanctl hands over to EOS, as band does.
  - A load that warms one sensor only has no summed evidence: R's floor follows it
    a poll or two behind, and RESCUE acts at target.
  - Plant much slower or noisier than measured: headings fall short or the summed
    test stays quiet; R's floor and RESCUE catch the load at target.
  - Plant with a faster load than measured (hotter plant at 40 %: Phy +1.5 C per
    poll): 2 polls is too late, one reading 1 C over target before the raise.
  - At 30 % and 25 C the Phy21-24 group settles only ~0.5 C below its raise
    threshold, so now and then noise costs a raise and two decreases (0-6 commits
    a day at a steady 25 C, depending on the noise draw).
  - Above ~45 % the Phy fan law is steeper (~0.95) than K: a large decrease from
    there can undershoot by a couple of %, costing one corrective raise.
  - An inlet reading that itself depends on fan speed is absorbed into R.
"""
import math

import fanctl

K = 0.8          # fan law: rise over inlet ~ (40 / fan%) ** K
TAU40 = 180.0    # s, sensor time constant at 40 %; grows as 1 / fan%
SETTLE = 3.0     # x TAU: R's learning time, and quiet time after our own change
MARGIN = 0.5     # C kept under every target (a decrease leaves twice this)...
ZN = 2.5         # ...plus this many sd of the sensor's own reading noise
ZDET = 4.0       # sd a warming summed over all sensors must stand out by
STEP = 2         # %: smallest decrease worth a commit; a raise adds this on top
RESCUE = 10      # %: raise at once when a sensor reaches its target (once per TAU)
WAIT = 1800      # s after any change before a decrease (doubles while undone)
NOISE = 3600     # s over which each sensor's reading noise is averaged


def need(rows):
    """Lowest fan % at which every (R, room) row's rise R x (40/fan)**K fits its room."""
    want = 0.0
    for R, room in rows:
        if room <= 0:
            return float("inf")
        want = max(want, 40.0 * (R / room) ** (1 / K))
    return want


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super(Variant, self).__init__(cfg, setting, now)
        self.R, self.var, self.hist = {}, {}, {}   # per sensor: R, noise variance, last 4 (reading, x)
        self.inlet = self.felt = self.last_t = self.last_down = None
        self.down_wait = WAIT
        self.rescued = -1e9      # time of the last RESCUE

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        dt = c.poll if self.last_t is None else now - self.last_t
        self.last_t = now
        tau = TAU40 * 40.0 / self.speed
        g, lag = (40.0 / self.speed) ** K, 1 - math.exp(-dt / tau)
        if self.felt is None or dt > 2 * c.poll:     # start, or back from EOS
            self.inlet, self.felt = r.ambient, g
            self.hist.clear()
        self.inlet += (r.ambient - self.inlet) * lag
        self.felt += (g - self.felt) * lag
        learn = 1 - math.exp(-dt / (SETTLE * tau))
        k = (tau + dt / 2.0) / (2 * dt)                           # heading = m2 + k (m2 - m1)
        hsd = math.sqrt(((1 + k) ** 2 + k * k) / 2) / self.felt   # its sd per reading sd
        rows, zsum, n, cooling, hot = [], 0.0, 0, False, False
        for s in r.sensors:
            h = self.hist.setdefault(s.name, [])
            if h:
                v = self.var.get(s.name, 0.25)
                self.var[s.name] = v + ((s.current - h[-1][0]) ** 2 / 2 - v) * dt / NOISE
            x = max(0.0, s.current - self.inlet) / self.felt
            h.append((s.current, x))
            del h[:-4]
            sd = math.sqrt(max(self.var.get(s.name, 0.25), 0.01))
            m2 = sum(y for _, y in h[-2:]) / len(h[-2:])
            slack = ZN * sd / self.felt / math.sqrt(2)         # m2's noise, in R units
            R = self.R.get(s.name, x)
            R = self.R[s.name] = max(R + (x - R) * learn, m2 - slack)
            cooling = cooling or R > m2 + slack
            hot = hot or sum(y for y, _ in h[-2:]) / len(h[-2:]) >= s.target - sd / math.sqrt(2)
            heading, z = R, 0.0
            if len(h) == 4:
                heading = m2 + k * (m2 - (h[0][1] + h[1][1]) / 2)
                z = (heading - R) / (hsd * sd)
                zsum, n = zsum + z, n + 1
            rows.append([R, s.target - MARGIN - ZN * sd - r.ambient, heading, z])
        if n and zsum > ZDET * math.sqrt(n) and now - self.last_change >= SETTLE * tau:
            for row in rows:              # a load: believe the headings that stand out
                if row[3] > ZN:
                    row[0] = max(row[0], row[2])
        goal = need([(R, room) for R, room, _, _ in rows])
        if hot and now - self.rescued >= tau:     # once per TAU: let the last one act
            goal = max(goal, self.speed + RESCUE - STEP)
            self.rescued = now
        goal = int(math.ceil(min(c.ceiling, goal) - 1e-6))
        if goal > self.speed:
            if self.last_down is not None and now - self.last_down < self.down_wait:
                self.down_wait = min(c.hold_max, 2 * self.down_wait)   # that decrease went too far
            return self._set(now, min(c.ceiling, goal + STEP), "up", r)
        low = need([(R, room - MARGIN) for R, room, _, _ in rows])
        low = max(c.floor, int(math.ceil(min(c.ceiling, low) - 1e-6)))
        if (not cooling and low < self.speed and (low <= self.speed - STEP or low == c.floor)
                and now - self.last_change >= self.down_wait):
            if self.last_down is not None and self.last_down == self.last_change:
                self.down_wait = WAIT      # the previous decrease held
            self.last_down = now
            return self._set(now, low, "down", r)
        return fanctl.Decision(None, [])

"""The fan algorithm from Arista's SONiC platform library, as a fanctl variant for bench.py.

This file is an independent reimplementation of the control law described
below, written against fanctl.Controller. No code from that project is
included or quoted: only the algorithm (a dead-zone integral controller) and
its published default parameters, which are ideas and numbers. That project,
aristanetworks/sonic, is free software: its README puts all of it except the
Linux kernel code, the files read here included, under the GNU GPL version 3
or (at your option) any later version (its LICENSE file carries the texts of
GPL versions 2 and 3).

Source read: https://github.com/aristanetworks/sonic/blob/4603a90fdf48f1486599b03e7a5f209d72f488b9/arista/core/cooling.py
(the master branch at commit 4603a90fdf48f1486599b03e7a5f209d72f488b9,
committed 2026-10-16), class CoolingLogicLegacy, with the defaults of
CoolingConfig there and of arista/core/config.py. CoolingLogicLegacy is CoolingConfig's default logic,
used by every platform that does not choose another; CoolingConfig's kp/ki/kd
(0.075/1/10) only feed the newer incpid/classicpid logics. Every loop
(cooling_loop_interval 10 s there, one 15 s poll here):

  delta = max over sensors of (temperature - (target + TARGET_OFFSET))
  if -NEG_HYST < delta < POS_HYST: leave the fans alone
  else: pwm += clamp(DELTA_SCALE x delta x (MAX_DECREASE or MAX_INCREASE),
                     +- that maximum) x min(elapsed / INTERVAL, 1)

i.e. an integral controller with a dead zone: the fans fall by delta % a
minute (at most 10) while the hottest sensor is more than NEG_HYST C under its
target, and rise by 2.5 x delta % a minute (at most 25) while it is POS_HYST C
or more over. pwm is a float, as in SONiC. The target is EOS's
setPointTemperature (every sensor on the reference switch has one, so Arista's
0.8 x overheat fallback never applies).

Arista's own knobs, retuned: TARGET_OFFSET -2 (cooling_target_offset, default
0) so it regulates 1..4 C under the setpoint rather than at it, and NEG_HYST 2
(cooling_hysteresis_negative, default 1) so the dead zone is wider than what
one 1 % step does to the most fan-sensitive sensor (else it hunts).

Added for fanctl, because every speed change here is a config commit:
SMOOTH judges each sensor by fanctl's mean of its last 4 polls (whole-degree
Phy readings would otherwise flick across the dead zone), a new speed is
committed only once the float is COMMIT % away from it, and a decrease only
SETTLE s after the last change (pwm may not fall below the fan speed meanwhile).
The delta is unweighted, as in SONiC: it is computed here from the 4-poll
means, not taken from regulate()'s `smooth`, which fanctl 1.3 weights by fan
authority. Arista as-is would be COMMIT 0.5 (any change of the rounded float),
SETTLE 0, SMOOTH False, TARGET_OFFSET 0 and NEG_HYST 1; the values below are
the retuned ones.
"""
import fanctl

# Arista's constants (cooling.py CoolingConfig / CoolingLogicLegacy, config.py)
NEG_HYST = 2.0        # C under the target inside which nothing changes (Arista: 1)
POS_HYST = 1.0        # C over the target inside which nothing changes
DELTA_SCALE = 0.1     # ThermalInfo.deltap = 0.1 x delta
MAX_DECREASE = 10.0   # % per INTERVAL at most (cooling_max_decrease): 1 %/min per C under
MAX_INCREASE = 25.0   # % per INTERVAL at most (cooling_max_increase): 2.5 %/min per C over
INTERVAL = 60.0       # s: CoolingAlgorithm.INTERVAL, the steps above are per this long
TARGET_OFFSET = -2.0  # C added to every target (cooling_target_offset; Arista: 0)

# Added for fanctl: every change is a running-config commit plus a syslog line
SMOOTH = True         # judge each sensor by its mean over fanctl's last 4 polls
COMMIT = 1            # % the float must move from the fan speed before it is committed
SETTLE = 600          # s after any change before a decrease (about 2.5 x the Phy lag)


class Variant(fanctl.Controller):
    def __init__(self, cfg, setting, now):
        super(Variant, self).__init__(cfg, setting, now)
        self.pwm = None         # Arista's zone speed, a float
        self.committed = None   # last speed this loop set
        self.last_run = None

    def regulate(self, now, r, e, smooth, latest, binding):
        c = self.cfg
        if self.committed != self.speed:    # first call, resume, floor: start from here
            self.pwm, self.committed = float(self.speed), self.speed
        elapsed = INTERVAL if self.last_run is None else now - self.last_run
        self.last_run = now
        hottest = max(sum(self.samples[s.name]) / len(self.samples[s.name]) - s.target for s in r.sensors)
        delta = (hottest if SMOOTH else e) - TARGET_OFFSET
        if not -NEG_HYST < delta < POS_HYST:
            if delta < 0:
                step = max(MAX_DECREASE * DELTA_SCALE * delta, -MAX_DECREASE)
            else:
                step = min(MAX_INCREASE * DELTA_SCALE * delta, MAX_INCREASE)
            self.pwm += step * min(elapsed / INTERVAL, 1.0)
        settling = now - self.last_change < SETTLE
        self.pwm = min(c.ceiling, max(self.speed if settling else c.floor, self.pwm))
        want = int(round(self.pwm))
        if (want != self.speed and (want > self.speed or not settling)
                and (abs(self.pwm - self.speed) >= COMMIT or want in (c.floor, c.ceiling))):
            self.committed = want
            return self._set(now, want, "up" if want > self.speed else "down", r)
        return fanctl.Decision(None, [])

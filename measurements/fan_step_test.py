#!/usr/bin/env python3
"""Fan step-response test for an Arista EOS switch (fan speeds 30..70 % only).

Run it only with the switch owner's approval: it changes the fan speed for about
three hours. Stop fanctl and fanctl-guard first (daemon fanctl / shutdown,
daemon fanctl-guard / shutdown) and start them again afterwards: the script
refuses to start while either runs, since each would change the fans under it.

The only configuration it sends is 'environment fan-speed override N' with
30 <= N <= 70, and at the end the hand-back to EOS that fanctl uses
('environment fan-speed auto', else 'no environment fan-speed'); its shell
(tools/eos.py) refuses to send anything else. However it ends (finished, an
equipment fault, too warm, the fans not at the speed it set, no inlet reading,
Ctrl-C), it hands the fans back to EOS's automatic control. Only if EOS refuses
both lines, or takes them without the fans going to automatic, does it leave
override 70, the top of its range, never a lower one. It never writes memory.

usage: SWITCH_HOST=.. SWITCH_USERNAME=.. SWITCH_PASSWORD=.. fan_step_test.py DATA.jsonl STATUS.json

If SWITCH_PASSWORD isn't set, the script prompts for it. The host key must
already be in ~/.ssh/known_hosts, unless ACCEPT_NEW_HOST_KEY=1 is set. In that
case the key is accepted on the first connection and pinned for the rest of the
run. The enable password, if one is asked for, is SWITCH_ENABLE_PASSWORD or else
SWITCH_PASSWORD. The exit status is 0 if the whole schedule ran and EOS has the
fans again.
"""
import json
import os
import re
import signal
import sys
import time
import traceback

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")))
import eos  # noqa: E402

LOW, HIGH = 30, 70
SCHEDULE = [(40, 15 * 60), (70, 25 * 60), (55, 25 * 60), (45, 25 * 60),
            (35, 25 * 60), (30, 30 * 60), (40, 25 * 60)]
POLL = 15
MAX_INLET = 30.0   # C: the test stops above this
# Everything the test may send: show commands, an override in LOW..HIGH, and the hand-back.
HAND_BACK = ("environment fan-speed auto", "no environment fan-speed")
ALLOWED = re.compile(r"^(show |terminal length 0$|configure$|end$|(%s)$|environment fan-speed override (%s)$)"
                     % ("|".join(map(re.escape, HAND_BACK)), "|".join(str(n) for n in range(LOW, HIGH + 1))))
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Stop(BaseException):
    """A stop signal. Not an Exception, so the retries' `except Exception` let it through."""


class Refused(Exception):
    """The test won't start; the fans weren't touched."""


class NotTaken(Exception):
    """EOS rejected a configuration line, or accepted it and the cooling status
    didn't show it: sending it again won't help."""


def on_signal(signum, frame):
    raise Stop("signal %d" % signum)


def check_schedule(schedule):
    bad = sorted(set(pct for pct, _dur in schedule if not LOW <= pct <= HIGH))
    if bad:
        raise Refused("the schedule's speeds must be %d..%d %%, not %s" % (LOW, HIGH, ", ".join(map(str, bad))))


def setting(cool):
    """The fan setting a cooling JSON shows, as the step test reports it."""
    if cool.get("coolingMode") == "manual":
        return "override %s" % cool.get("overrideFanSpeed")
    return "automatic fan control"


class Test(object):
    def __init__(self, data_path, status_path, shell):
        self.sh = shell
        self.data = open(data_path, "a")
        self.status_path = status_path
        self.t0 = time.time()
        self.current = None     # the override this test set and saw take effect
        self.touched = False    # it has sent a fan setting: it must hand the fans back
        self.note = "starting"

    def status(self, **kw):
        st = dict(kw, time=time.time(), elapsed_min=round((time.time() - self.t0) / 60, 1),
                  current=self.current, note=self.note)
        with open(self.status_path + ".tmp", "w") as f:
            json.dump(st, f, indent=1)
        os.replace(self.status_path + ".tmp", self.status_path)

    def set_fans(self, pct):
        self._configure("environment fan-speed override %d" % pct,
                        lambda cool: cool.get("coolingMode") == "manual" and cool.get("overrideFanSpeed") == pct)
        self.current = pct
        self.log({"event": "set", "pct": pct})

    def hand_back(self):
        """Hand the fans to EOS's automatic control, as fanctl does: 'auto', else
        'no environment fan-speed', else the top of the range. What it did, for the note."""
        for line in HAND_BACK:
            try:
                self._configure(line, lambda cool: cool.get("coolingMode") == "automatic")
                self.current = None
                self.log({"event": "auto"})
                return True, "fans handed back to EOS (automatic fan control)"
            except NotTaken:
                pass
        self.set_fans(HIGH)
        return False, ("EOS refused automatic fan control, so the fans are left at override %d: "
                       "run 'environment fan-speed auto' by hand" % HIGH)

    def _configure(self, line, took):
        """Send one configuration line and wait until the cooling status shows it
        took. A dropped session is retried; a line EOS rejects or doesn't apply is not."""
        self.touched = True
        for _attempt in range(5):
            try:
                return self._send(line, took)
            except Exception as e:
                self.log({"event": "set-error", "line": line, "error": str(e)})
                if isinstance(e, NotTaken):
                    raise
                time.sleep(5)
                self._reconnect()
        raise RuntimeError("could not send '%s'" % line)

    def _send(self, line, took):
        out = "".join(self.sh.run(cmd) for cmd in ("configure", line, "end"))
        if any(l.lstrip().startswith("% ") for l in out.splitlines()):
            raise NotTaken("EOS rejected '%s': %s" % (line, out[-300:]))
        for _ in range(5):
            if took(self.sh.json("show system environment cooling | json")):
                return
            time.sleep(2)
        raise NotTaken("'%s' did not show in cooling status" % line)

    def _reconnect(self):
        try:
            self.sh.connect()
        except Exception:
            pass     # the next command says whether the switch is back

    def log(self, rec):
        rec["t"] = round(time.time() - self.t0, 2)
        rec["wall"] = time.time()
        self.data.write(json.dumps(rec) + "\n")
        self.data.flush()

    def preflight(self):
        """Refuse to start while fanctl or its guard runs; the fan setting found."""
        for name, state in eos.fanctl_daemons(eos.daemons(self.sh.run("show daemon"))):
            if state.startswith("running"):
                raise Refused("%s is running. Stop it first (daemon %s / shutdown), and start it again "
                              "after the test" % (name, name))
        return setting(self.sh.json("show system environment cooling | json"))

    def poll(self, step, pct):
        t1 = time.time()
        temp = self.sh.json("show system environment temperature | json")
        cool = self.sh.json("show system environment cooling | json")
        power = self.sh.json("show system environment power | json")
        sensors = eos.sensors(temp)
        rec = dict(eos.poll_record(temp, cool, power), step=step, pct=pct, poll_s=round(time.time() - t1, 2))
        self.log(rec)
        fault = self._faults(rec, cool, sensors, pct)
        hot = []
        for s in sensors:
            cur, sp, alert = s.get("currentTemperature"), s.get("setPointTemperature"), s.get("overheatThreshold")
            if cur is None or sp is None or alert is None:
                continue
            if cur > sp + 3 or alert - cur < 15:
                hot.append("%s %.1f C (target %.1f, alert %s)" % (s["description"], cur, sp, alert))
        worst = max(((s["currentTemperature"] - s["setPointTemperature"], s["description"])
                     for s in sensors if s.get("currentTemperature") is not None
                     and s.get("setPointTemperature")), default=(None, None))
        self.status(step=step, pct=pct, ambient=rec["ambient"], worst=worst, fault=fault, hot=hot)
        return fault, hot, rec["ambient"]

    @staticmethod
    def _faults(rec, cool, sensors, pct):
        """What in one poll stops the test: equipment faults, a fan setting that
        isn't the test's, and no inlet reading (the inlet limit needs one)."""
        fault = []
        if rec["tstatus"] != "temperatureOk":
            fault.append("temperature status %s" % rec["tstatus"])
        if rec["cstatus"] != "coolingOk":
            fault.append("cooling status %s" % rec["cstatus"])
        fault += ["fan %s %s" % (l, v[2]) for l, v in rec["fans"].items() if v[2] != "ok"]
        fault += ["PSU %s %s" % (k, v[0]) for k, v in rec["psu"].items() if v[0] != "ok"]
        fault += ["%s %s" % (s["description"], s.get("hwStatus")) for s in sensors if s.get("hwStatus") != "ok"]
        if (rec["mode"], rec["override"]) != ("manual", pct):
            fault.append("the fans are at %s, not override %d (is fanctl, fanctl-guard or someone else "
                         "setting them?)" % (setting(cool), pct))
        if rec["ambient"] is None:
            fault.append("no inlet reading")
        return fault

    def verdict(self, pct, fault, hot, amb):
        """None to go on, or the note to stop the test on."""
        if fault:
            self.log({"event": "abort-fault", "why": fault})
            return "ABORT (fault): %s" % "; ".join(fault)
        if amb > MAX_INLET:
            self.log({"event": "stop-ambient", "ambient": amb})
            return "STOP (inlet %.1f C > %.0f)" % (amb, MAX_INLET)
        if hot:
            self.log({"event": "too-warm", "why": hot, "pct": pct})
            return "ABORT (too warm at %d%%): %s" % (pct, "; ".join(hot))
        return None

    def steps(self):
        """Run the schedule; the note to end on."""
        failures = 0
        for i, (pct, dur) in enumerate(SCHEDULE):
            if pct != self.current:
                self.set_fans(pct)
            end = time.time() + dur
            while time.time() < end:
                t1 = time.time()
                found, failures = self._read(i, pct, failures)
                stop = found and self.verdict(pct, *found)
                if stop:
                    return stop
                time.sleep(max(0, POLL - (time.time() - t1)))
        return "finished"

    def _read(self, step, pct, failures):
        """One poll: (fault, hot, inlet) or None if it failed, and the failures in a row."""
        if failures:
            self._reconnect()
        try:
            return self.poll(step, pct), 0
        except Exception as e:
            self.log({"event": "poll-error", "error": str(e)})
            if failures + 1 >= 4:
                raise RuntimeError("switch unreadable 4 times in a row")
            return None, failures + 1

    def run(self):
        """Run the test; True if the whole schedule ran and EOS has the fans again.
        Raises Refused, with the fans untouched, if it won't start."""
        ok, before = False, None
        try:
            check_schedule(SCHEDULE)
            self.sh.connect()
            before = self.preflight()
            self.note = "running"
            self.log({"event": "start", "schedule": SCHEDULE, "before": before})
            self.note = self.steps()
            ok = self.note == "finished"
        except Refused as e:
            self.note = "not starting: %s" % e
            raise
        except Stop as e:
            self.note = "stopped (%s)" % e
        except Exception as e:
            self.note = "error: %s" % e
            self.log({"event": "error", "error": traceback.format_exc()})
        finally:
            for sig in SIGNALS:
                signal.signal(sig, signal.SIG_IGN)
            ok = self.finish(before) and ok
            self.sh.close()
        return ok

    def finish(self, before):
        """Hand the fans back if the test touched them, and record the end; False
        if the fans couldn't be handed back."""
        handed = True
        if self.touched:
            try:
                handed, what = self.hand_back()
            except Exception as e:
                handed, what = False, "COULD NOT hand the fans back to EOS: %s" % e
            self.note += "; " + what
        if before:
            self.note += "; before the test they were at %s" % before
        self.log({"event": "end", "note": self.note})
        self.status(step=None, pct=self.current, done=True)
        return handed


def main(argv=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        sys.exit(__doc__)
    shell = eos.Shell(eos.login_from_env(os.environ if environ is None else environ, __doc__), ALLOWED)
    test = Test(argv[0], argv[1], shell)
    handlers = dict((sig, signal.signal(sig, on_signal)) for sig in SIGNALS)
    try:
        ok = test.run()
    except Refused as e:
        sys.exit("fan_step_test: not starting: %s" % e)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        test.data.close()
    print(test.note)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

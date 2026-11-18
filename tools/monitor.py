#!/usr/bin/env python3
"""Read-only field monitor for a switch running fanctl.

    SWITCH_HOST=.. SWITCH_USERNAME=.. SWITCH_PASSWORD=.. python3 monitor.py HOURS [OUTDIR]

Runs on a management host, not on the switch, and needs paramiko
(`pip install paramiko`).

Over SSH, every 30 s it records the temperature, cooling and power JSON. Every
5 min it also records fanctl's status file, the daemon states, and new
fanctl, guard, ProcMgr and environment log lines.

It writes three files to OUTDIR (default: the current directory):

  monitor.jsonl        one JSON record per poll
  alerts.log           anything a human should look at: heat, faults, EOS
                       taking the fans, and the notable log lines (every
                       warning and error from fanctl and the guard, fanctl
                       taking the fans back, daemon terminations; see
                       fanlog.py)
  monitor_status.json  the latest poll, for a quick look

It sends only `show` commands and `bash cat` of fanctl's status file, never
configuration.

If SWITCH_PASSWORD isn't set, the script prompts for it. The host key must
already be in ~/.ssh/known_hosts, unless ACCEPT_NEW_HOST_KEY=1 is set. In that
case the key is accepted on the first connection and pinned for the rest of the
run. The enable password, if one is asked for, is SWITCH_ENABLE_PASSWORD or else
SWITCH_PASSWORD.

A read that fails (a timeout, a dropped connection, a login cut short by a slow
AAA server, a shell the switch won't open, output it can't use) is written to
monitor.jsonl as an error and retried at the next poll, with an alert after 6
failures in a row. If the switch refuses the login (the password, the host key
or enable), the monitor alerts and stops at once: trying again every 30 s would
only lock the account. Any other error is a bug in the monitor: it alerts and
stops with a traceback.
"""
import json
import os
import re
import sys
import time

import eos
import fanlog

POLL, SLOW = 30, 300
ALLOWED = re.compile(r"^(show |bash cat /run/fanctl/fanctl\.status\.json; echo$|terminal length 0$)")


def _desc(s):
    return s.get("description") or s["name"]


def sensor_alerts(sensors):
    """Alerts for sensors over their EOS target or within 15 C of their alert limit."""
    alerts = []
    for s in sensors:
        cur, sp, al = s.get("currentTemperature"), s.get("setPointTemperature"), s.get("overheatThreshold")
        if cur is None or sp is None or al is None:
            continue
        if cur > sp:
            alerts.append("over target: %s %.1f C (target %.1f)" % (_desc(s), cur, sp))
        if al - cur < 15:
            alerts.append("near alert limit: %s %.1f C (alert %s)" % (_desc(s), cur, al))
    return alerts


def hardware_alerts(rec):
    """An alert if a fan or PSU isn't ok, or EOS's temperature or cooling status isn't."""
    bad = [l for l, v in rec["fans"].items() if v[2] != "ok"] + [k for k, v in rec["psu"].items() if v[0] != "ok"]
    if bad or rec["tstatus"] != "temperatureOk" or rec["cstatus"] != "coolingOk":
        return ["hardware status: %s %s %s" % (bad, rec["tstatus"], rec["cstatus"])]
    return []


def mode_alerts(rec, last_mode):
    """An alert when the fans pass to EOS's automatic control."""
    if last_mode and last_mode[0] != "automatic" and rec["mode"] == "automatic":
        return ["fan setting now %s %s" % (rec["mode"], rec["override"])]
    return []


def slow_alerts(rec):
    """Alerts from the 5-minute reads in rec: the status file, the daemons, new log lines."""
    alerts = []
    if "error" in rec.get("fanctl_status", {}):
        alerts.append("cannot read fanctl status file: %s" % rec["fanctl_status"]["error"])
    alerts += ["daemon %s is %s" % (name, state) for name, state in eos.fanctl_daemons(rec.get("daemons", []))
               if not state.startswith("running")]
    alerts += ["log: " + line.split(" ", 4)[-1][:300] for line in rec.get("log", []) if fanlog.NOTABLE.search(line)]
    return alerts


def worst(sensors):
    """(margin to the EOS target, description) of the sensor closest to its target, or None."""
    return max(((s["currentTemperature"] - s["setPointTemperature"], _desc(s)) for s in sensors
                if s.get("currentTemperature") is not None and s.get("setPointTemperature") is not None),
               default=None)


class Monitor(object):
    """One monitoring run: its output files, and what it remembers between polls."""

    def __init__(self, shell, outdir):
        self.sh = shell
        self.data = open(os.path.join(outdir, "monitor.jsonl"), "a", buffering=1)
        self.alerts = open(os.path.join(outdir, "alerts.log"), "a", buffering=1)
        self.status_path = os.path.join(outdir, "monitor_status.json")
        self.t0 = time.time()
        self.last_slow = None
        self.seen_logs = set()
        self.failures = 0
        self.last_mode = None

    def alert(self, msg):
        self.alerts.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))

    def hours(self):
        return (time.time() - self.t0) / 3600

    def run(self, hours):
        end = self.t0 + hours * 3600
        try:
            while time.time() < end:
                t1 = time.time()
                self.poll(t1)
                time.sleep(max(1.0, POLL - (time.time() - t1)))
        except BaseException as e:      # a refused login, ^C or a bug: say so, then stop
            self.alert("monitor stopped after %.1f h: %s" % (self.hours(), str(e)[:200] or type(e).__name__))
            raise
        finally:
            self.sh.close()
        self.alert("monitor finished after %.1f h" % self.hours())

    def poll(self, t1):
        try:
            rec, sensors = self.read(t1)
        except eos.ShellError as e:
            self.failures += 1
            self.data.write(json.dumps({"t": round(t1, 1), "event": "error", "error": str(e)[:300]}) + "\n")
            if self.failures == 6:
                self.alert("monitor cannot read the switch (6 failures in a row): %s" % str(e)[:200])
            self.sh.close()
            return
        self.failures = 0
        self.data.write(json.dumps(rec) + "\n")
        for msg in slow_alerts(rec) + sensor_alerts(sensors) + hardware_alerts(rec) + mode_alerts(rec, self.last_mode):
            self.alert(msg)
        self.last_mode = (rec["mode"], rec["override"])
        self.write_status(t1, rec, sensors)

    def read(self, t1):
        """(poll record, sensors) for this poll, or eos.ShellError."""
        if self.sh.chan is None:
            self.sh.connect()
        temp = self.sh.json("show system environment temperature | json")
        cool = self.sh.json("show system environment cooling | json")
        power = self.sh.json("show system environment power | json")
        rec = dict(eos.poll_record(temp, cool, power), t=round(t1, 1))
        if self.last_slow is None or time.time() - self.last_slow >= SLOW:
            self.read_slow(rec)
            self.last_slow = time.time()
        return rec, eos.sensors(temp)

    def read_slow(self, rec):
        try:
            rec["fanctl_status"] = self.sh.json("bash cat /run/fanctl/fanctl.status.json; echo")  # no trailing newline
        except eos.ShellError as e:
            rec["fanctl_status"] = {"error": str(e)[:200]}
        rec["daemons"] = eos.daemons(self.sh.run("show daemon"))
        new = [l for l in self.sh.run("show logging last 10 minutes").splitlines()
               if fanlog.RECORD.search(l) and l not in self.seen_logs]
        self.seen_logs.update(new)
        if new:
            rec["log"] = new

    def write_status(self, t1, rec, sensors):
        with open(self.status_path + ".tmp", "w") as f:
            json.dump({"time": t1, "elapsed_h": round((t1 - self.t0) / 3600, 2), "mode": rec["mode"],
                       "override": rec["override"], "ambient": rec["ambient"], "worst": worst(sensors)}, f)
        os.replace(self.status_path + ".tmp", self.status_path)


def parse_args(argv):
    """(hours, outdir) from the command line, or exit with the usage."""
    try:
        hours = float(argv[0])
    except (IndexError, ValueError):
        sys.exit(__doc__)
    if len(argv) > 2 or not hours > 0:
        sys.exit(__doc__)
    return hours, argv[1] if len(argv) == 2 else "."


def main(argv=None, environ=None):
    hours, outdir = parse_args(sys.argv[1:] if argv is None else argv)
    login = eos.login_from_env(os.environ if environ is None else environ, __doc__)
    try:
        Monitor(eos.Shell(login, ALLOWED), outdir).run(hours)
    except eos.LoginError as e:
        sys.exit("monitor stopped: cannot log in: %s" % e)


if __name__ == "__main__":
    main()

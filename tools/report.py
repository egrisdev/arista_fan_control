#!/usr/bin/env python3
"""Summarise a tools/monitor.py run.

    python3 report.py monitor.jsonl [--targets temp.json] [--since EPOCH] [--until EPOCH] [--status-lines]

--targets is a saved `show system environment temperature | json` from the same
switch. With it, the report shows each sensor's closest approach to its EOS
target (setPointTemperature; it counts a setpoint of 0 or less as none).

The report covers:

- the time under fanctl's override and under EOS automatic control
- the time-weighted fan speed, and the speed changes per day
- the inlet range
- each sensor's peak
- notable log lines: every warning and error from fanctl and the guard,
  fanctl taking the fans back, and daemon terminations (see fanlog.py)

fanctl's hourly "status: fans" lines are only counted, so that they don't drown
the notable lines on long runs. --status-lines lists them after the notable lines.

A line that isn't a whole monitor record, such as the last line of a run whose
monitor was killed mid-write, is skipped with a warning naming it.
"""
import argparse
import collections
import json
import sys
import time

import eos
from fanlog import NOTABLE, STATUS
MAX_GAP = 120.0          # s; a longer gap between polls counts as this, so monitor gaps don't dominate

# What load() read: the polls in the period, the number of error records,
# the notable and hourly status log lines (each once), and the number of
# lines skipped as unreadable.
Run = collections.namedtuple("Run", "polls errors notable status skipped")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_or_none(v):
    return v is None or _num(v)


# The fields of a poll record that the report reads, and what each must hold.
POLL_FIELDS = (
    ("mode", lambda v: v is None or isinstance(v, str)),
    ("override", _num_or_none),
    ("ambient", _num_or_none),
    ("temps", lambda v: isinstance(v, dict) and all(_num_or_none(x) for x in v.values())),
    ("fans", lambda v: isinstance(v, dict) and all(isinstance(x, list) and len(x) >= 2 and _num_or_none(x[1])
                                                   for x in v.values())),
    ("log", lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)),
)


def parse_record(line):
    """One monitor.jsonl line as a record, or ValueError saying what is wrong with it."""
    try:
        r = json.loads(line)
    except ValueError:
        r = None
    if not isinstance(r, dict) or not _num(r.get("t")) or r.get("event") not in ("poll", "error"):
        raise ValueError("not a monitor record")
    if r["event"] == "poll":
        r.setdefault("log", [])          # only polls that read the log carry one
        bad = [k for k, ok in POLL_FIELDS if k not in r or not ok(r[k])]
        if bad:
            raise ValueError("poll without " + ", ".join(bad))
    return r


def load(path, since, until):
    """The records of `path` between `since` and `until`, as a Run."""
    polls, errors, notable, status, skipped = [], 0, [], [], 0
    with open(path) as f:
        for n, line in enumerate(f, 1):
            try:
                r = parse_record(line)
            except ValueError as e:
                sys.stderr.write("%s line %d: %s, skipped\n" % (path, n, e))
                skipped += 1
                continue
            if not since <= r["t"] <= until:
                continue
            if r["event"] == "error":
                errors += 1
                continue
            polls.append(r)
            notable += [l for l in r["log"] if NOTABLE.search(l) and l not in notable]
            status += [l for l in r["log"] if STATUS.search(l) and l not in status]
    return Run(polls, errors, notable, status, skipped)


def load_targets(path):
    """{sensor name: (description or None, setPointTemperature)} from a saved
    temperature JSON, or exit saying what is wrong with the file. It counts a
    setpoint of 0 or less as none."""
    try:
        with open(path) as f:
            temp = json.load(f)
        sensors = eos.sensors(temp) if isinstance(temp, dict) else []
    except (OSError, ValueError, eos.ShellError) as e:
        sys.exit("--targets %s: %s" % (path, e))
    if not sensors:
        sys.exit("--targets %s: not a saved `show system environment temperature | json`" % path)
    return {s["name"]: (s.get("description"), s["setPointTemperature"])
            for s in sensors if (s.get("setPointTemperature") or 0) > 0}


def _tray_speed(p):
    """The mean actual speed of the fan-tray fans in poll p, or 0 if none reads."""
    speeds = [v[1] for k, v in p["fans"].items() if not k.startswith("PowerSupply") and v[1] is not None]
    return sum(speeds) / len(speeds) if speeds else 0.0


def summarize(polls):
    """The run's figures from its polls (at least two, in time order, not all at one time)."""
    in_mode, fan_area, changes = {}, 0.0, 0
    for p, q in zip(polls, polls[1:]):
        dt = min(q["t"] - p["t"], MAX_GAP)
        in_mode[p["mode"]] = in_mode.get(p["mode"], 0.0) + dt
        fan_area += dt * _tray_speed(p)
        changes += (p["mode"], p["override"]) != (q["mode"], q["override"])
    total = sum(in_mode.values())
    return {"t0": polls[0]["t"], "t1": polls[-1]["t"], "hours": (polls[-1]["t"] - polls[0]["t"]) / 3600.0,
            "in_mode": in_mode, "total": total, "fan_mean": fan_area / total if total else 0.0,
            "changes": changes, "last_override": polls[-1]["override"],
            "overrides": [p["override"] for p in polls if p["mode"] == "manual" and p["override"] is not None],
            "ambient": [p["ambient"] for p in polls if p["ambient"] is not None]}


def sensor_rows(polls, targets):
    """(margin to target, line) for every sensor seen in any poll; -99 without a target."""
    rows = []
    for n in sorted(set().union(*(p["temps"] for p in polls))):
        vals = [p["temps"][n] for p in polls if p["temps"].get(n) is not None]
        if not vals:
            continue
        peak = max(vals)
        if n in targets:
            desc, sp = targets[n]
            label = "%s (%s)" % (desc, n) if desc else n
            rows.append((peak - sp, "  %-42s peak %5.1f C  target %5.2f  margin %+5.1f C" % (label, peak, sp, peak - sp)))
        else:
            rows.append((-99, "  %-42s peak %5.1f C" % (n, peak)))
    return sorted(rows, reverse=True)


def _stamp(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def render_summary(run, s):
    print("period      %s to %s (%.1f h), %d polls, %d read errors"
          % (_stamp(s["t0"]), _stamp(s["t1"]), s["hours"], len(run.polls), run.errors))
    if run.skipped:
        print("            %d unreadable line%s skipped" % (run.skipped, "" if run.skipped == 1 else "s"))
    print("mode        " + ", ".join("%s %.1f%%" % (m, 100.0 * t / s["total"])
                                     for m, t in sorted(s["in_mode"].items(), key=lambda kv: str(kv[0]))))
    print("fan speed   %.1f%% time-weighted mean of actual tray speed" % s["fan_mean"])
    if s["overrides"]:
        print("override    min %d%%, max %d%%, last %s" % (min(s["overrides"]), max(s["overrides"]), s["last_override"]))
    print("changes     %d (%.1f per day)" % (s["changes"], s["changes"] / s["hours"] * 24))
    amb = s["ambient"]
    if amb:
        print("inlet       min %.2f, mean %.2f, max %.2f C" % (min(amb), sum(amb) / len(amb), max(amb)))
    else:
        print("inlet       n/a (no reading)")


def render_logs(run, status_lines):
    if run.notable:
        print("\nnotable log lines")
        for l in run.notable:
            print("  " + l[:220])
    if run.status and status_lines:
        print("\nhourly status lines")
        for l in run.status:
            print("  " + l[:220])
    elif run.status:
        print("\n%d hourly status lines omitted; --status-lines lists them" % len(run.status))


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("data")
    ap.add_argument("--targets")
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--until", type=float, default=float("inf"))
    ap.add_argument("--status-lines", action="store_true")
    a = ap.parse_args(argv)

    targets = load_targets(a.targets) if a.targets else {}
    run = load(a.data, a.since, a.until)
    if len(run.polls) < 2 or run.polls[-1]["t"] <= run.polls[0]["t"]:
        sys.exit("not enough polls")
    render_summary(run, summarize(run.polls))
    print("\nsensor peaks%s" % (" and closest approach to target" if targets else ""))
    for row in sensor_rows(run.polls, targets):
        print(row[1])
    render_logs(run, a.status_lines)


if __name__ == "__main__":
    main()

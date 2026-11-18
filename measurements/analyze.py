"""Analyse fan_step_test.py data: steady state, gain curve, time constants, noise, power.

    python3 analyze.py data.jsonl

The steady states, fits and time constants here are also what build_model.py
builds the benchmark's plant model from. Sensor descriptions, targets and alert
limits come from the reference switch's saved JSON (tests/fixtures/temp.json).
"""
import json
import os
import sys

import numpy as np

SETTLE_S = 300      # steady state = mean of the last 5 min of each step


def load(path):
    """The poll records of a fan_step_test.py data file."""
    with open(path) as f:
        return [r for r in map(json.loads, f) if r["event"] == "poll"]


def steps_of(polls):
    steps = {}
    for p in polls:
        steps.setdefault(p["step"], []).append(p)
    return [(k, v) for k, v in sorted(steps.items()) if k >= 0]


def fit_tau(ts, ys, y_end):
    """First-order step response y = y_end + (y0 - y_end) exp(-t/tau): grid-search tau."""
    ts, ys = np.array(ts) - ts[0], np.array(ys)
    y0 = ys[:2].mean()
    if abs(y0 - y_end) < 1.0:      # step too small to say anything
        return None
    taus = np.arange(15, 3600, 5)
    errs = [float(((y_end + (y0 - y_end) * np.exp(-ts / tau) - ys) ** 2).mean()) for tau in taus]
    return float(taus[np.argmin(errs)])     # the first of equal errors


def descriptions():
    """{sensor name: its entry} in the reference switch's saved temperature JSON."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixtures", "temp.json")) as f:
        temp = json.load(f)
    return {s["name"]: s for s in temp["tempSensors"] + [x for sl in temp["powerSupplySlots"] for x in sl["tempSensors"]]}


def steady_states(steps, names):
    """Per step, from its last SETTLE_S: (pct, inlet, {name: mean}, {name: sd}, PSU output W, tray fans actual %)."""
    ss = []
    for _k, ps in steps:
        tail = [p for p in ps if p["t"] >= ps[-1]["t"] - SETTLE_S]
        amb = np.mean([p["ambient"] for p in tail])
        means = {n: float(np.mean([p["temps"][n] for p in tail])) for n in names}
        stds = {n: float(np.std([p["temps"][n] for p in tail])) for n in names}
        pw = float(np.mean([sum(v[1] for v in p["psu"].values()) for p in tail]))
        act = float(np.mean([np.mean([v[1] for l, v in p["fans"].items() if "/" in l and not l.startswith("Power")])
                             for p in tail]))
        ss.append((ps[0]["pct"], float(amb), means, stds, pw, act))
    return ss


def fit_power_law(ss, n):
    """Sensor n's steady rise over the inlet fitted as R * (40/f)^k: (R, k, rms error)."""
    f = np.array([s[0] for s in ss], float)
    rise = np.array([s[2][n] - s[1] for s in ss])
    ok = rise > 0.3
    if ok.sum() < 3:
        return float(np.mean(rise)), 0.0, 0.0
    a = np.vstack([np.ones(ok.sum()), np.log(40.0 / f[ok])]).T
    (log_r, k), *_ = np.linalg.lstsq(a, np.log(rise[ok]), rcond=None)
    r = float(np.exp(log_r))
    pred = r * (40.0 / f[ok]) ** k
    return r, float(k), float(np.sqrt(((pred - rise[ok]) ** 2).mean()))


def step_taus(steps, ss, n):
    """Sensor n's first-order time constant over each step change: [(from %, to %, tau s)]."""
    taus = []
    for i in range(1, len(steps)):
        _k, ps = steps[i]
        tau = fit_tau([p["t"] for p in ps], [p["temps"][n] for p in ps], ss[i][2][n])
        if tau is not None:
            taus.append((ss[i - 1][0], ss[i][0], tau))
    return taus


def about(desc, n):
    """Sensor n's (description, target, alert limit), or its name for a sensor the saved JSON lacks."""
    return desc.get(n, (n, 0, 0))


def step_lines(steps, ss):
    out = ["STEPS"]
    for (k, ps), (pct, amb, _m, _sd, pw, act) in zip(steps, ss):
        dur = (ps[-1]["t"] - ps[0]["t"]) / 60
        out.append("step %d: %2d%% for %.0f min, inlet %.2f C, PSU output %.1f W, tray fans actual %.1f%%, polls %d"
                   % (k, pct, dur, amb, pw, act, len(ps)))
    return out


def steady_lines(ss, rows, desc):
    out = ["STEADY STATE (last 5 min of each step), C",
           "%-28s" % "sensor" + "".join("%7d%%" % s[0] for s in ss) + "   target  alert"]
    for n in rows:
        d = about(desc, n)
        out.append("%-28s" % d[0][:28] + "".join("%8.1f" % s[2][n] for s in ss) + "  %6.1f %6s" % (d[1], d[2]))
    return out


def gain_lines(ss, rows, desc):
    out = ["GAIN FIT  T - inlet = R * (40/f)^k   (per sensor, steady states)"]
    for n in rows:
        R, kexp, rms = fit_power_law(ss, n)
        sens = {pc: R * kexp * (40.0 ** kexp) * pc ** (-kexp - 1) for pc in (30, 40, 55, 70)}
        noise = float(np.median([s[3][n] for s in ss]))
        out.append("%-28s R40 %5.1f  k %5.2f  rms %4.2f  dT/df: %s  noise sd %.2f" % (
            about(desc, n)[0][:28], R, kexp, rms, " ".join("%d%%:%.2f" % (pc, -v) for pc, v in sens.items()), noise))
    return out


def tau_lines(steps, ss, rows, desc):
    out = ["TIME CONSTANTS (first-order fit per step change, s)"]
    for n in rows:
        taus = step_taus(steps, ss, n)
        if taus:
            out.append("%-28s median %5.0f s   %s" % (about(desc, n)[0][:28], np.median([t for _a, _b, t in taus]),
                                                     "  ".join("%d->%d:%.0f" % x for x in taus)))
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit(__doc__)
    polls = load(argv[0])
    steps = steps_of(polls)
    names = sorted(polls[0]["temps"])
    desc = {n: (s["description"], s["setPointTemperature"], s["overheatThreshold"])
            for n, s in descriptions().items()}
    ss = steady_states(steps, names)
    rows = sorted(names, key=lambda n: -(ss[0][2][n] - about(desc, n)[1]))
    sections = (step_lines(steps, ss), steady_lines(ss, rows, desc), gain_lines(ss, rows, desc),
                tau_lines(steps, ss, rows, desc))
    print("\n\n".join("\n".join(lines) for lines in sections))


if __name__ == "__main__":
    main()

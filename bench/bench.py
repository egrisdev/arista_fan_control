"""Benchmark fanctl in-band controllers on a plant model measured on the real switch.

    python3 bench.py VARIANT [VARIANT ...] [--set tuning|full] [--cfg JSON] [--seed N]
                     [--model MODEL.json] [--json OUT]

MODEL.json (default: measurements/measured_model.json) comes from
measurements/build_model.py (step test on the switch): per sensor the measured steady-state rise over the inlet at each fan % ("curve",
interpolated log-log; R40/k power-law fit as fallback), the time constant tau (s,
scaled by (40/fan)^tau_exponent) and noise_sd. measurements/validate_model.py
replays the real step test through it (bench/results/model_validation.txt).

VARIANT is 'band' (fanctl's built-in band control), 'static40' (today's fixed
40 %), or a .py file defining

    class Variant(fanctl.Controller):
        def regulate(self, now, r, e, smooth, latest, binding): ...   # -> fanctl.Decision

Only regulate() may differ, so every controller shares fanctl's safety layer
(hand-offs to EOS, holds, resume, pause, floor/ceiling, ceiling dwell).

--cfg overrides fanctl settings for every run, e.g. '{"min_authority": 1.0}';
--seed N adds N to every case's noise seed. Paths on the command line are
relative to the current directory, as usual; nothing else depends on it.
"""
import argparse
import importlib.util
import json
import math
import multiprocessing
import os
import random
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))
import fanctl  # noqa: E402
import sim  # noqa: E402

HOUR, DAY = 3600.0, 86400.0
MODEL = os.path.join(REPO, "measurements", "measured_model.json")   # the step test's plant model
BASE = fanctl.Controller      # the real band controller, whatever a worker loaded before

PLANTS = {   # departures from the measured model, to test robustness
    "measured": dict(k=1.0, tau=1.0, noise=1.0, rise=1.0),
    "noisy": dict(k=1.0, tau=1.0, noise=3.0, rise=1.0),
    "sensitive": dict(k=2.0, tau=1.0, noise=1.0, rise=1.0),
    "insensitive": dict(k=0.5, tau=1.0, noise=1.0, rise=1.0),
    "sluggish": dict(k=1.0, tau=3.0, noise=1.0, rise=1.0),
    "hotter": dict(k=1.0, tau=1.0, noise=1.0, rise=1.25),
}


def daily(t):
    return 25.0 + 5.0 * math.sin(2 * math.pi * t / DAY)


def warm_spell(t):   # 26 C, up to 31 over 6 h, hold 2 h, down to 24 over 8 h
    h = t / HOUR
    if h < 2:
        return 26.0
    if h < 8:
        return 26.0 + 5.0 * (h - 2) / 6
    if h < 10:
        return 31.0
    if h < 18:
        return 31.0 - 7.0 * (h - 10) / 8
    return 24.0


SCENARIOS = {
    "steady25": dict(ambient=25.0, duration=12 * HOUR),
    "steady28": dict(ambient=28.0, duration=12 * HOUR),
    "daily": dict(ambient_fn="daily", duration=2 * DAY),
    "warmspell": dict(ambient_fn="warm_spell", duration=22 * HOUR),
    "loadstep": dict(ambient=25.0, phy_step=(4 * HOUR, 6 * HOUR, 1.35), duration=10 * HOUR),
    "glitch": dict(ambient=25.0, glitch_every=2 * HOUR, duration=DAY),
}
TUNING_SET = [("measured", s) for s in ("steady25", "steady28", "daily", "warmspell")]
FULL_SET = [(p, s) for p in PLANTS for s in SCENARIOS]
SETS = {"tuning": TUNING_SET, "full": FULL_SET}


class Static(BASE):
    def regulate(self, now, r, e, smooth, latest, binding):
        return fanctl.Decision(None, [])


def load_variant(spec):
    fanctl.Controller = BASE  # variants must subclass the real base, not a previous variant
    if spec == "band":
        return BASE
    if spec == "static40":
        return Static
    ms = importlib.util.spec_from_file_location("variant", spec)
    mod = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(mod)
    return mod.Variant


def curve_at(c, x):
    """Log-log curve c at x: between its two points either side, or along its two end points beyond them."""
    i = max([0] + [j for j in range(len(c) - 1) if c[j][0] <= x])
    (x0, y0), (x1, y1) = c[i], c[i + 1]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def sensor_params(m, p):
    """One sensor's (rise at 40 %, fan-law exponent, tau s, noise sd) on plant p."""
    return (m["R40"] * p["rise"], max(0.0, m["k"]) * p["k"],
            max(15.0, m.get("tau", 240.0)) * p["tau"], m.get("noise_sd", 0.0) * p["noise"])


def log_curve(m):
    """The measured steady-state rise over inlet at each fan %, in log-log; None without one."""
    if not m.get("curve"):
        return None
    return [(math.log(f), math.log(max(r, 0.05))) for f, r in m["curve"]]


class MeasuredPhysics(object):
    """The measured per-sensor model, as the physics of a sim plant (install_model)."""

    def __init__(self, plant, model, p, seed):
        self.plant, self.p, self.rng = plant, p, random.Random(seed)
        self.tau_exp = model.get("tau_exponent", 0.0)
        self.params, self.curves, self.mult = {}, {}, {}
        for s in plant.sensors:
            m = model["sensors"][s[0]["name"]]
            self.params[id(s)] = sensor_params(m, p)
            self.curves[id(s)] = log_curve(m)
            self.mult[id(s)] = 1.0

    def rise(self, s, fan):
        c = self.curves[id(s)]
        if not c:
            R, k, _tau, _sd = self.params[id(s)]
            return R * self.mult[id(s)] * (40.0 / max(fan, 5.0)) ** k
        y = curve_at(c, math.log(max(fan, 5.0)))
        # robustness variants scale sensitivity (k) around 40 % and the overall rise
        if self.p["k"] != 1.0:
            y40 = curve_at(c, math.log(40.0))
            y = y40 + (y - y40) * self.p["k"]
        return math.exp(y) * self.p["rise"] * self.mult[id(s)]

    def steady(self, s, fan):
        return self.plant.ambient + self.rise(s, fan)

    def tau(self, s, fan):
        return self.params[id(s)][2] * (40.0 / max(fan, 5.0)) ** self.tau_exp

    def settle(self, fan):
        for s in self.plant.sensors:
            s[4] = self.steady(s, fan)

    def advance(self, dt, fan):
        for s in self.plant.sensors:
            ss = self.steady(s, fan)
            s[4] = ss + (s[4] - ss) * math.exp(-dt / self.tau(s, fan))

    def readings(self):
        for s in self.plant.sensors:
            raw, _r, _t, q, true = s
            val = true + self.rng.gauss(0.0, self.params[id(s)][3])
            yield raw, (round(val / q) * q if q else val)

    def scale_rise(self, pred, mul):   # extra heat in some components (load step)
        for s in self.plant.sensors:
            if pred(s[0]["description"]):
                self.mult[id(s)] *= mul


def install_model(sw, model, p, seed):
    """Replace the sim plant's physics with the measured per-sensor model."""
    m = MeasuredPhysics(sw.plant, model, p, seed)
    sw.plant.settle, sw.plant.advance, sw.plant.readings, sw.plant.scale_rise = (
        m.settle, m.advance, m.readings, m.scale_rise)
    m.settle(40)


def scenario_switch(model, plant_name, scen_name, seed):
    """The simulated switch on plant plant_name, at the start of scenario scen_name."""
    sc = SCENARIOS[scen_name]
    sw = sim.FakeSwitch(ambient=sc.get("ambient", 25.0), override=40)
    install_model(sw, model, PLANTS[plant_name], seed=zlib.crc32(("%s/%s" % (plant_name, scen_name)).encode()) + seed)
    if sc.get("ambient_fn"):
        sw.ambient_fn = globals()[sc["ambient_fn"]]
        sw.plant.ambient = sw.ambient_fn(0.0)
        sw.plant.settle(40)
    return sw


def phy_step(sc, sw, stepped):
    """Put on or take off the scenario's extra PHY heat for time sw.t; whether it is on."""
    if "phy_step" not in sc:
        return stepped
    t0, t1, mul = sc["phy_step"]
    want = t0 <= sw.t < t1
    if want != stepped:
        sw.plant.scale_rise(lambda d: d.startswith("Phy"), mul if want else 1.0 / mul)
    return want


def glitch(sc, sw):
    """The scenario's fan-status glitch: one failed fan at each multiple of glitch_every."""
    if "glitch_every" not in sc:
        return
    if sw.t > 0 and int(sw.t) % int(sc["glitch_every"]) == 0:
        sw.fan_status["2/1"] = "failed"
    else:
        sw.fan_status.clear()


def metrics(rec, lines, duration, poll):
    """A run's metrics from its record [(t, fan %, auto, excess, headroom)] and fan changes."""
    late = [x for x in rec if x[0] >= HOUR]
    return {
        "mean_fan": sum(x[1] for x in late) / len(late),
        "fan_power_vs_40": sum((x[1] / 40.0) ** 3 for x in late) / len(late),
        "changes_per_day": len(lines) * DAY / duration,
        "handoffs": sum(1 for l in lines if l.endswith("auto")),
        "min_over_target": sum(poll for x in rec if x[3] > 0) / 60.0,
        "max_excess": max(x[3] for x in rec),
        "min_headroom": min(x[4] for x in rec),
    }


def run_one(spec, model_path, plant_name, scen_name, seed, cfg):
    """One variant on one plant in one scenario: its metrics. seed is added to
    the case's noise seed; cfg ({setting: value}) overrides fanctl settings."""
    fanctl.Controller = load_variant(spec)      # Daemon.startup() builds fanctl.Controller
    try:
        with open(model_path) as f:
            model = json.load(f)
        sc = SCENARIOS[scen_name]
        sw = scenario_switch(model, plant_name, scen_name, seed)
        state = {"stepped": False}
        rec = []

        def on_tick(si):
            state["stepped"] = phy_step(sc, si.sw, state["stepped"])
            glitch(sc, si.sw)
            r = si.d.last_reading
            rec.append((si.sw.t, si.sw.fan_pct(), si.sw.override is None, r.excess, r.tightest.headroom))
        s = sim.Sim(sw, cfg=sim.make_cfg(**cfg))
        s.run(sc["duration"], on_tick=on_tick)
        return metrics(rec, [l for _t, l in s.changes()], sc["duration"], s.cfg.poll)
    finally:
        fanctl.Controller = BASE


KEYS = ["mean_fan", "fan_power_vs_40", "changes_per_day", "handoffs", "min_over_target",
        "max_excess", "min_headroom"]


def summarize(rows):
    n = len(rows)
    agg = {k: sum(m[k] for m in rows.values()) / n for k in KEYS}
    agg["max_excess"] = max(m["max_excess"] for m in rows.values())
    agg["min_headroom"] = min(m["min_headroom"] for m in rows.values())
    return agg


def table(variant, rows):
    """The lines for one variant's runs {case: metrics}: a blank line, its name, one row per case and the summary."""
    lines = ["", "== %s" % variant, "%-24s %8s %8s %9s %8s %9s %8s %8s" % (
        "case", "meanFan", "power40", "chg/day", "handoff", "overTgtM", "maxExc", "minHead")]
    for case, m in rows.items():
        lines.append("%-24s %8.1f %8.2f %9.1f %8d %9.1f %8.1f %8.1f" % ((case,) + tuple(m[k] for k in KEYS)))
    agg = summarize(rows)
    lines.append("%-24s %8.1f %8.2f %9.1f %8.1f %9.1f %8.1f %8.1f" % (("MEAN / WORST",) + tuple(agg[k] for k in KEYS)))
    return lines


def collect(jobs, results):
    """{variant: {"plant/scenario": metrics}} from the jobs and their results, in job order."""
    out = {}
    for (v, _model, p, s, _seed, _cfg), m in zip(jobs, results):
        out.setdefault(v, {})["%s/%s" % (p, s)] = m
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("variants", nargs="+")
    ap.add_argument("--set", choices=SETS, default="full")
    ap.add_argument("--cfg", type=json.loads, default={}, help="fanctl setting overrides, as JSON")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    jobs = [(v, a.model, p, s, a.seed, a.cfg) for v in a.variants for p, s in SETS[a.set]]
    with multiprocessing.Pool() as pool:
        out = collect(jobs, pool.starmap(run_one, jobs, chunksize=1))
    for v, rows in out.items():
        print("\n".join(table(v, rows)))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()

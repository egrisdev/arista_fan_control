"""The benchmark's sweep: for each candidate, the full matrix (bench.FULL_SET) at
the standard seed, plus the 20-seed load-step and warm-spell safety sweep on the
measured plant.

    python3 sweep.py CANDIDATES [--model MODEL.json] [--json OUT]

CANDIDATES is a JSON object {name: {"variant": V, "cfg": {...}, "env": {...}}},
every key optional: the variant as for bench.py (default "band", fanctl's own
controller), fanctl setting overrides, and environment variables set while each
run loads its variant (only bandauth.py reads one, BANDAUTH_WMIN). For example:

    python3 sweep.py '{"w0.5": {"cfg": {"min_authority": 0.5}}, "w1": {"cfg": {"min_authority": 1.0}}}'
    python3 sweep.py '{"pi.py": {"variant": "variants/pi.py"}}'
    python3 sweep.py '{"W_MIN 0.25": {"variant": "variants/bandauth.py", "env": {"BANDAUTH_WMIN": "0.25"}}}'

Prints, per candidate, one summary line (the measured plant, all 36 cases, and
the seed sweep) and one line per seed-sweep scenario. --json writes every run
as [name, plant, scenario, seed, metrics]; seed 0 is the standard seed.
"""
import argparse
import json
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

SEEDS = range(1, 21)
SEED_SCENARIOS = ("loadstep", "warmspell")


def jobs(candidates, model):
    """(name, variant, model, plant, scenario, seed, cfg, env) for every run of every candidate."""
    out = []
    for name, c in candidates.items():
        base = (c.get("variant", "band"), model)
        out += [(name,) + base + (p, s, 0, c.get("cfg", {}), c.get("env", {})) for p, s in bench.FULL_SET]
        out += [(name,) + base + ("measured", s, seed, c.get("cfg", {}), c.get("env", {}))
                for s in SEED_SCENARIOS for seed in SEEDS]
    return out


def run(job):
    """One run, with the candidate's environment set only while it lasts."""
    name, variant, model, plant, scen, seed, cfg, env = job
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return [name, plant, scen, seed, bench.run_one(variant, model, plant, scen, seed, cfg)]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def mean(ms, key):
    return sum(m[key] for m in ms) / len(ms)


def p_over(ms):
    return sum(1 for m in ms if m["min_over_target"] > 0) / len(ms)


def report(name, rows):
    """The lines for one candidate's runs [(plant, scenario, seed, metrics)]."""
    full = [m for p, s, seed, m in rows if seed == 0]
    meas = [m for p, s, seed, m in rows if seed == 0 and p == "measured"]
    sw = {sc: [m for p, s, seed, m in rows if seed > 0 and s == sc] for sc in SEED_SCENARIOS}
    lines = ["%-14s | measured fan %.1f chg %.1f over %.2f | all36 fan %.1f chg %.1f over %.2f maxExc %+.1f "
             "head %.1f | 20 seeds: loadstep P(over) %.2f chg %.1f, warmspell P(over) %.2f fan %.1f" % (
                 name, mean(meas, "mean_fan"), mean(meas, "changes_per_day"),
                 sum(m["min_over_target"] for m in meas), mean(full, "mean_fan"), mean(full, "changes_per_day"),
                 sum(m["min_over_target"] for m in full), max(m["max_excess"] for m in full),
                 min(m["min_headroom"] for m in full), p_over(sw["loadstep"]), mean(sw["loadstep"], "changes_per_day"),
                 p_over(sw["warmspell"]), mean(sw["warmspell"], "mean_fan"))]
    for sc, ms in sw.items():
        over = [m["min_over_target"] for m in ms]
        lines.append("%-26s %-10s P(over) %.2f  mean over %.2f min  max over %.2f min  worst excess %+.1f  "
                     "fan %.1f  chg/day %.1f" % (name, sc, p_over(ms), sum(over) / len(over), max(over),
                                                 max(m["max_excess"] for m in ms), mean(ms, "mean_fan"),
                                                 mean(ms, "changes_per_day")))
    return lines


def sweep(candidates, model):
    """Every run of every candidate, on every CPU: [[name, plant, scenario, seed, metrics]]."""
    with multiprocessing.Pool() as pool:
        return pool.map(run, jobs(candidates, model), chunksize=1)


def render(names, runs):
    """The report lines for each named candidate from runs [[name, plant, scenario, seed, metrics]]."""
    return [line for name in names
            for line in report(name, [(p, s, seed, m) for n, p, s, seed, m in runs if n == name])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidates", type=json.loads)
    ap.add_argument("--model", default=bench.MODEL)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    res = sweep(a.candidates, a.model)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f)
    print("\n".join(render(a.candidates, res)))


if __name__ == "__main__":
    main()

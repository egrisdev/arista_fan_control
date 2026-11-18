"""Build the benchmark plant model from the raw step test.

    python3 build_model.py step_test_2026-10-16.jsonl measured_model.json

Per sensor, from the last 5 min of each step: the steady-state rise over the
inlet at each fan % ("curve", the two 40 % steps averaged), a power-law fit
rise = R40 * (40/fan)^k (fallback only), the median first-order time constant
over the step changes ("tau"), and the median reading noise ("noise_sd").
Time constants scale as (40/fan)^tau_exponent; 1.0 fits the Phy/Trident fits
(~100 s ending at 70 %, ~165 s at 40 %, ~250 s at 30 %). The steady states and
fits are analyze.py's. Sensor descriptions, and the "source" line, are the
reference switch's: the benchmark only runs with its sensor set.
"""
import json
import sys

import numpy as np

from analyze import descriptions, fit_power_law, load, step_taus, steady_states, steps_of


def main(data_path, out_path):
    polls = load(data_path)
    steps = steps_of(polls)
    names = sorted(polls[0]["temps"])
    desc = {n: s["description"] for n, s in descriptions().items()}
    ss = steady_states(steps, names)
    model = {"source": "step test 2026-10-16, Arista DCS-7050TX-64-R, inlet ~24.5-25.5 C",
             "tau_exponent": 1.0, "sensors": {}}
    for n in names:
        by_pct = {}
        for pct, amb, means, _sd, _pw, _act in ss:
            by_pct.setdefault(pct, []).append(means[n] - amb)
        curve = sorted((pct, float(np.mean(v))) for pct, v in by_pct.items())
        r40, k, _rms = fit_power_law(ss, n)
        taus = [tau for _a, _b, tau in step_taus(steps, ss, n)]
        model["sensors"][n] = {"desc": desc.get(n, n), "curve": curve, "R40": r40, "k": k,
                               "tau": float(np.median(taus)) if taus else 120.0,
                               "noise_sd": float(np.median([s[3][n] for s in ss]))}
    with open(out_path, "w") as f:
        json.dump(model, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

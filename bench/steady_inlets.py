"""Settled fan speed per inlet on the measured plant model, v1.3 vs unweighted (min_authority 1).

    python3 bench/steady_inlets.py > bench/results/steady_inlets.txt

Two starts per inlet: from 40 % at that inlet, and from 40 % at 25 C with the
inlet ramping to it over 2 h (band control's hold zone makes the result depend
on history).
"""
import json
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402
import sim  # noqa: E402

HOUR = 3600.0
INLETS = [20, 22, 24, 25, 26, 27, 28, 29, 30, 31]
AUTHORITIES = (0.5, 1.0)
HEADER = ("inlet | min_authority 0.5 (v1.3): from 40% / ramped (changes in last 12 h; worst raw excess; binding) "
          "| min_authority 1 (unweighted): from 40% / ramped")


def jobs(inlets):
    """(inlet, min_authority, ramped) for every run of the table, in its order."""
    return [(i, a, ramp) for i in inlets for a in AUTHORITIES for ramp in (False, True)]


def inlet_switch(inlet, ramp, model):
    """The measured plant at 40 %, at the inlet or ramping to it from 25 C over 2 h."""
    sw = sim.FakeSwitch(ambient=25.0 if ramp else inlet, override=40)
    bench.install_model(sw, model, bench.PLANTS["measured"], 7)
    if ramp:
        sw.ambient_fn = lambda t: 25.0 + (inlet - 25.0) * min(1.0, t / (2 * HOUR))
    return sw


def run(job):
    """One day of fanctl: (inlet, min_authority, ramped, final override, changes in the last
    12 h, worst raw excess in the last 12 h, binding sensor's label)."""
    inlet, auth, ramp = job
    with open(bench.MODEL) as f:
        model = json.load(f)
    s = sim.Sim(inlet_switch(inlet, ramp, model), cfg=sim.make_cfg(min_authority=auth))
    s.run(24 * HOUR)
    late = [x for x in s.trace if x[0] > 12 * HOUR]
    worst = max(e for _t, _f, _m, e in late if e is not None)
    binding = s.d.ctrl.binding[0].label if s.d.ctrl.binding else "-"
    return inlet, auth, ramp, s.sw.override, len(s.changes(12 * HOUR)), worst, binding


def cell(r):
    return "%4s (%d; %+.1f; %s)" % (r[3] or "auto", r[4], r[5], r[6].split(" (")[0])


def table(res, inlets):
    """The header and one line per inlet from the runs' results."""
    lines = [HEADER]
    for i in inlets:
        cells = [cell([x for x in res if x[:3] == (i, a, ramp)][0]) for a in AUTHORITIES for ramp in (False, True)]
        lines.append("%4.0f C | %s / %s | %s / %s" % ((i,) + tuple(cells)))
    return lines


def main():
    with multiprocessing.Pool(4) as p:
        print("\n".join(table(p.map(run, jobs(INLETS)), INLETS)))


if __name__ == "__main__":
    main()

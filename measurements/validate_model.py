"""Replay the recorded step test through the benchmark plant model and report the error.

    python3 validate_model.py step_test_2026-10-16.jsonl measured_model.json

The model starts from the first recorded readings and is driven by the recorded
inlet and the recorded actual tray-fan speed at every poll. Errors are model
minus real, per sensor, for the raw reading and for the 4-poll mean fanctl acts on.
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bench"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
import bench  # noqa: E402
import sim  # noqa: E402


def recorded_polls(path):
    """The polls of the step test itself (step >= 0) in a fan_step_test.py data file."""
    with open(path) as f:
        return [r for r in map(json.loads, f) if r["event"] == "poll" and r["step"] >= 0]


def tray_fans(poll):
    """The mean actual speed of the fan-tray fans in one poll."""
    fans = [v[1] for k, v in poll["fans"].items() if "/" in k and not k.startswith("Power")]
    return sum(fans) / len(fans)


def mean4(xs):
    return sum(xs[-4:]) / len(xs[-4:])


def compare(plant, poll, hist, err):
    """Add each sensor's model-minus-real error at one poll, raw and on the 4-poll mean."""
    for s in plant.sensors:
        n = s[0]["name"]
        modelled, real = hist[n]
        modelled.append(s[4])
        real.append(poll["temps"][n])
        err[n].append((s[4] - poll["temps"][n], mean4(modelled) - mean4(real)))


def replay(polls, model):
    """The model driven by the recorded inlet and tray-fan speed: ({name: description},
    {name: [(raw error, 4-poll mean error)] per poll})."""
    sw = sim.FakeSwitch(override=40)
    plant = sw.plant
    bench.install_model(sw, model, bench.PLANTS["measured"], 1)
    for s in plant.sensors:
        s[4] = polls[0]["temps"][s[0]["name"]]
    names = {s[0]["name"]: s[0]["description"] for s in plant.sensors}
    hist = {n: ([], []) for n in names}
    err = {n: [] for n in names}
    prev = None
    for p in polls:
        if prev is not None:
            plant.ambient = prev["ambient"]
            plant.advance(p["t"] - prev["t"], tray_fans(prev))
        compare(plant, p, hist, err)
        prev = p
    return names, err


def table(names, err):
    """One line per sensor, the largest 4-poll mean error first."""
    rows = []
    for n, e in err.items():
        rms = math.sqrt(sum(a * a for a, _ in e) / len(e))
        rows.append((max(abs(b) for _, b in e), names[n], rms, max(abs(a) for a, _ in e)))
    return ["%-28s %8s %8s %10s" % ("sensor", "rms raw", "max raw", "max 4-mean")] + [
        "%-28s %8.2f %8.2f %10.2f" % (d[:28], rms, mx, m4) for m4, d, rms, mx in sorted(rows, reverse=True)]


def main(data_path, model_path):
    with open(model_path) as f:
        model = json.load(f)
    print("\n".join(table(*replay(recorded_polls(data_path), model))))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

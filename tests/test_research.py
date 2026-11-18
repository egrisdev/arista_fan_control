"""Tests for the research scripts: the controller benchmark in bench/ and the
step-test analysis in measurements/. They run the scripts the way a researcher
does, on the tracked step-test data and plant model, and check that they
reproduce the results recorded in bench/results/ and docs/VALIDATION.md. The
candidate controllers in bench/variants/ are recorded research and have no
tests of their own (bench/README.md, Scope). Run from the repo root:
python3 -m unittest discover -s tests -v"""

import contextlib
import copy
import io
import json
import math
import os
import re
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH = os.path.join(ROOT, "bench")
MEAS = os.path.join(ROOT, "measurements")
RESULTS = os.path.join(BENCH, "results")
DATA = os.path.join(MEAS, "step_test_2026-10-16.jsonl")
MODEL = os.path.join(MEAS, "measured_model.json")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, BENCH)
sys.path.insert(0, MEAS)

import bench  # noqa: E402
import fanctl  # noqa: E402
import sim  # noqa: E402
import steady_inlets  # noqa: E402
import sweep  # noqa: E402
import validate_model  # noqa: E402

try:
    import numpy  # noqa: F401
except ImportError:
    analyze = build_model = None
else:
    import analyze  # noqa: E402
    import build_model  # noqa: E402

NEEDS_NUMPY = unittest.skipIf(analyze is None, "the step-test analysis needs NumPy (pip install numpy)")


def recorded(name):
    with open(os.path.join(RESULTS, name)) as f:
        return f.read()


def recorded_json(name):
    return json.loads(recorded(name))


def printed(fn, *args):
    """What fn(*args) prints, and what it exits with (None if it returns)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            fn(*args)
            code = None
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code


def run_script(path, *args):
    """What `python3 path args...` prints, and what it exits with (None if it ends normally)."""
    with mock.patch.object(sys, "argv", [path] + list(args)):
        return printed(runpy.run_path, path, {}, "__main__")


def assert_as_recorded(test, got, want, where=""):
    """got is the recorded JSON value want, numbers to within float rounding: the
    last digits move with the Python (3.12's sum() adds floats more exactly) and
    the NumPy version."""
    if isinstance(want, dict):
        test.assertEqual(sorted(got), sorted(want), where)
        for k in want:
            assert_as_recorded(test, got[k], want[k], "%s/%s" % (where, k))
    elif isinstance(want, list):
        test.assertEqual(len(got), len(want), where)
        for i, (g, w) in enumerate(zip(got, want)):
            assert_as_recorded(test, g, w, "%s/%d" % (where, i))
    elif isinstance(want, float):
        test.assertAlmostEqual(got, want, places=9, msg=where)
    else:
        test.assertEqual(got, want, where)


def load_model():
    with open(MODEL) as f:
        return json.load(f)


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="fanctl-research-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def made_up_step_test(path, sensors, steps, poll=60.0, tau=120.0):
    """Write a fan_step_test.py data file of a step test on a made-up switch at a 25 C
    inlet: each sensor {name: (rise at 40 %, k)} heads for 25 + rise * (40/fan)^k with
    time constant tau, from 25 C at the start; steps [(fan %, polls)], polls every poll s."""
    t, temps, out = 0.0, dict.fromkeys(sensors, 25.0), []
    for k, (pct, polls) in enumerate(steps):
        start = dict(temps)
        for i in range(polls):
            temps = {}
            for n, (rise, kexp) in sensors.items():
                ss = 25.0 + rise * (40.0 / pct) ** kexp
                temps[n] = ss + (start[n] - ss) * math.exp(-i * poll / tau)
            out.append({"event": "poll", "step": k, "pct": pct, "t": t, "ambient": 25.0, "temps": temps,
                        "fans": {"1/1": [pct, pct, "ok"], "PowerSupply1/1": [pct, 99, "ok"]},
                        "psu": {"1": ["ok", 100.0, 1.0], "2": ["ok", 50.0, 0.5]}})
            t += poll
    with open(path, "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in out))


class ModelValidationTests(unittest.TestCase):
    """measurements/validate_model.py, as measurements/README.md runs it."""

    def test_replaying_the_step_test_reproduces_the_recorded_validation(self):
        out, code = run_script(os.path.join(MEAS, "validate_model.py"), DATA, MODEL)
        self.assertIsNone(code)
        self.assertEqual(out, recorded("model_validation.txt"))


@NEEDS_NUMPY
class StepTestAnalysisTests(unittest.TestCase):
    """measurements/analyze.py and build_model.py, as measurements/README.md runs them."""

    def test_build_model_rebuilds_the_tracked_plant_model(self):
        out = os.path.join(tmpdir(self), "model.json")
        _printed, code = run_script(os.path.join(MEAS, "build_model.py"), DATA, out)
        self.assertIsNone(code)
        with open(out) as f:
            written = f.read()
        assert_as_recorded(self, json.loads(written), load_model())
        # indented as the tracked model is, so the two compare sensor by sensor in a diff
        with open(MODEL) as f:
            self.assertEqual(written.splitlines()[1][:2], f.read().splitlines()[1][:2])

    def test_analysis_prints_the_step_test_numbers_validation_quotes(self):
        out, code = run_script(os.path.join(MEAS, "analyze.py"), DATA)
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("step 1: 70% for 25 min, inlet 24.57 C, PSU output 166.8 W, tray fans actual 69.1%, "
                      "polls 100", lines)
        # docs/VALIDATION.md's steady-state table: the hottest PHY at 40, 70, 55, 45, 35, 30, 40 %
        self.assertIn("Phy17-20                        54.9    43.3    47.1    52.0    59.0    63.1    55.0"
                      "    65.0  100.0", lines)
        self.assertIn("Phy17-20                     R40  30.0  k  0.84  rms 0.38  dT/df: 30%:-1.07 40%:-0.63 "
                      "55%:-0.35 70%:-0.23  noise sd 0.30", lines)
        # "the CPU's is about 90 s"
        self.assertIn("Cpu temp sensor              median    90 s   40->70:50  70->55:60  55->45:95  45->35:130"
                      "  35->30:175  30->40:85", lines)
        self.assertEqual(lines[0], "STEPS")
        self.assertIn("TIME CONSTANTS (first-order fit per step change, s)", lines)

    def test_analysis_without_a_data_file_prints_its_usage(self):
        out, code = run_script(os.path.join(MEAS, "analyze.py"))
        self.assertEqual(out, "")
        self.assertIn("python3 analyze.py data.jsonl", code)


@NEEDS_NUMPY
class MadeUpStepTestAnalysisTests(unittest.TestCase):
    """measurements/analyze.py on a step test with a known answer: 40, 70 and 30 %, 20 min
    each, polled every minute, on four sensors that follow the power law exactly."""

    SENSORS = {"TempSensor18": (30.0, 0.8),    # Phy17-20, target 65
               "TempSensor1": (20.0, 0.5),     # Cpu temp sensor, target 60
               "TempSensor5": (10.0, 1.0),     # Front-panel temp sensor, target 35.75
               "TempSensorX": (5.0, 1.0)}      # not in the reference switch's saved JSON

    def section(self, title):
        path = os.path.join(tmpdir(self), "step.jsonl")
        made_up_step_test(path, self.SENSORS, [(40, 21), (70, 21), (30, 21)])
        out, code = printed(analyze.main, [path])
        self.assertIsNone(code)
        return [s.splitlines() for s in out.split("\n\n") if s.startswith(title)][0]

    def test_each_step_lasts_from_its_first_poll_to_its_last(self):
        self.assertEqual(self.section("STEPS")[1:], [
            "step 0: 40% for 20 min, inlet 25.00 C, PSU output 150.0 W, tray fans actual 40.0%, polls 21",
            "step 1: 70% for 20 min, inlet 25.00 C, PSU output 150.0 W, tray fans actual 70.0%, polls 21",
            "step 2: 30% for 20 min, inlet 25.00 C, PSU output 150.0 W, tray fans actual 30.0%, polls 21"])

    def test_the_steady_states_list_the_sensors_nearest_their_target_first(self):
        # at 40 %: TempSensorX 30.0 over no target, Front-panel 0.75 under 35.75, Phy 10 under, Cpu 15 under
        self.assertEqual(self.section("STEADY STATE")[1:], [
            "sensor                           40%     70%     30%   target  alert",
            "TempSensorX                     30.0    27.9    31.7     0.0      0",
            "Front-panel temp sensor         35.0    30.7    38.3    35.8   55.0",
            "Phy17-20                        55.0    44.2    62.8    65.0  100.0",
            "Cpu temp sensor                 45.0    40.1    48.1    60.0   90.0"])

    def test_three_fan_speeds_are_enough_for_the_power_law_fit(self):
        self.assertIn("Phy17-20                     R40  30.0  k  0.80  rms 0.00  dT/df: 30%:-1.01 40%:-0.60 "
                      "55%:-0.34 70%:-0.22  noise sd 0.00", self.section("GAIN FIT"))

    def test_time_constants_are_fitted_to_the_step_changes_not_to_the_warm_up(self):
        for line in self.section("TIME CONSTANTS")[1:]:
            self.assertEqual(re.findall(r"(\d+)->(\d+):", line), [("40", "70"), ("70", "30")], line)


class PlantModelTests(unittest.TestCase):
    """bench.install_model: the measured model in place of the simulator's physics."""

    def plant(self, model=None, variant="measured"):
        sw = sim.FakeSwitch(ambient=25.0, override=40)
        bench.install_model(sw, model or load_model(), bench.PLANTS[variant], 0)
        return sw.plant

    @staticmethod
    def measured_rise(m, fan):
        """The measured rise at fan %: the curve's point, or log-log between or beyond its points."""
        c = [(math.log(f), math.log(max(r, 0.05))) for f, r in m["curve"]]
        x = math.log(fan)
        i = max([0] + [j for j in range(len(c) - 1) if c[j][0] <= x])
        i = min(i, len(c) - 2)
        (x0, y0), (x1, y1) = c[i], c[i + 1]
        return math.exp(y0 + (y1 - y0) * (x - x0) / (x1 - x0))

    def test_each_sensor_settles_at_its_measured_rise_over_the_inlet(self):
        model = load_model()
        plant = self.plant(model)
        for fan in (20, 30, 35, 40, 50, 55, 70, 90):
            plant.settle(fan)
            for s in plant.sensors:
                m = model["sensors"][s[0]["name"]]
                self.assertAlmostEqual(s[4] - 25.0, self.measured_rise(m, fan), places=9,
                                       msg="%s at %d %%" % (s[0]["description"], fan))

    def test_the_sensitive_plant_doubles_each_slope_around_40_percent(self):
        model = load_model()
        plant = self.plant(model, "sensitive")
        for fan in (30, 40, 70):
            plant.settle(fan)
            for s in plant.sensors:
                m = model["sensors"][s[0]["name"]]
                r40, r = self.measured_rise(m, 40), self.measured_rise(m, fan)
                self.assertAlmostEqual(s[4] - 25.0, r * r / r40, places=9)

    def test_the_hotter_plant_rises_a_quarter_more_at_every_fan_speed(self):
        model = load_model()
        plant = self.plant(model, "hotter")
        for fan in (30, 40, 70):
            plant.settle(fan)
            for s in plant.sensors:
                m = model["sensors"][s[0]["name"]]
                self.assertAlmostEqual(s[4] - 25.0, 1.25 * self.measured_rise(m, fan), places=9)

    def test_a_sensor_without_a_curve_follows_its_power_law_fit(self):
        model = copy.deepcopy(load_model())
        m = model["sensors"]["TempSensor18"]
        del m["curve"]
        # (rise x, k x): hotter rises a quarter more, sensitive doubles the fan-law exponent
        for variant, rise, k in (("hotter", 1.25, 1.0), ("sensitive", 1.0, 2.0)):
            plant = self.plant(model, variant)
            plant.settle(20)
            s = [s for s in plant.sensors if s[0]["name"] == "TempSensor18"][0]
            self.assertAlmostEqual(s[4] - 25.0, rise * m["R40"] * 2.0 ** (k * m["k"]), places=9, msg=variant)

    def test_the_load_step_heats_only_the_phys_and_only_while_it_lasts(self):
        curveless = copy.deepcopy(load_model())
        for m in curveless["sensors"].values():
            del m["curve"]
        for model, rise40 in ((load_model(), lambda m: self.measured_rise(m, 40)), (curveless, lambda m: m["R40"])):
            sw = bench.scenario_switch(model, "measured", "loadstep", 0)
            stepped = False
            for hours, phy in ((0, 1.0), (4, 1.35), (5.9, 1.35), (6, 1.0)):
                sw.t = hours * bench.HOUR
                stepped = bench.phy_step(bench.SCENARIOS["loadstep"], sw, stepped)
                sw.plant.settle(40)
                for s in sw.plant.sensors:
                    want = (phy if s[0]["description"].startswith("Phy") else 1.0) * rise40(model["sensors"][s[0]["name"]])
                    self.assertAlmostEqual(s[4] - 25.0, want, places=9, msg="%s at %g h" % (s[0]["description"], hours))

    def test_a_sensor_approaches_its_new_steady_state_with_tau_scaled_by_1_over_fan(self):
        model = load_model()
        for variant, slower in (("measured", 1.0), ("sluggish", 3.0)):
            plant = self.plant(model, variant)
            plant.settle(40)
            at40 = [s[4] for s in plant.sensors]
            plant.advance(60.0, 70)
            for s, before in zip(plant.sensors, at40):
                m = model["sensors"][s[0]["name"]]
                ss = 25.0 + self.measured_rise(m, 70)
                tau = slower * max(15.0, m["tau"]) * 40.0 / 70.0
                self.assertAlmostEqual(s[4], ss + (before - ss) * math.exp(-60.0 / tau), places=9, msg=variant)

    def test_the_noisy_plant_s_readings_scatter_three_times_as_far(self):
        def cpu_noise(variant):   # the CPU sensor reads to the bit, so its noise is all there is
            plant = self.plant(variant=variant)
            cpu = [s for s in plant.sensors if s[0]["description"] == "Cpu temp sensor"][0]
            return [v - cpu[4] for _ in range(20) for raw, v in plant.readings() if raw is cpu[0]]
        measured, noisy = cpu_noise("measured"), cpu_noise("noisy")
        self.assertGreater(min(abs(d) for d in measured), 0.0)
        for m, n in zip(measured, noisy):   # the same seed draws the same numbers
            self.assertAlmostEqual(n, 3.0 * m, places=9)

    def test_whole_degree_sensors_read_in_whole_degrees(self):
        plant = self.plant()
        phy = [v for raw, v in plant.readings() if raw["description"].startswith("Phy")]
        self.assertEqual(len(phy), 12)
        self.assertTrue(all(v == round(v) for v in phy), phy)


class ScenarioTests(unittest.TestCase):
    """bench.py's scenarios: the inlet each gives the simulated switch over time."""

    def inlet(self, scen, hours):
        sw = bench.scenario_switch(load_model(), "measured", scen, 0)
        self.assertEqual(sw.plant.ambient, sw.ambient_fn(0.0))   # it starts at the scenario's first inlet
        return sw.ambient_fn(hours * bench.HOUR)

    def test_the_daily_cycle_swings_between_20_and_30_c(self):
        for hours, want in ((0, 25.0), (6, 30.0), (12, 25.0), (18, 20.0), (24, 25.0)):
            self.assertAlmostEqual(self.inlet("daily", hours), want, places=9, msg="%g h" % hours)

    def test_the_warm_spell_climbs_from_26_to_31_c_holds_and_falls_to_24(self):
        for hours, want in ((0, 26.0), (2, 26.0), (5, 28.5), (8, 31.0), (9, 31.0), (10, 31.0), (14, 27.5),
                            (18, 24.0), (22, 24.0)):
            self.assertAlmostEqual(self.inlet("warmspell", hours), want, places=9, msg="%g h" % hours)


class BenchTests(unittest.TestCase):
    """bench/bench.py, as bench/README.md runs it."""

    def test_a_run_leaves_fanctl_s_own_controller_in_place(self):
        band = fanctl.Controller
        m = bench.run_one("static40", bench.MODEL, "measured", "glitch", 0, {})
        self.assertIs(fanctl.Controller, band)
        # bench/results/canonical.json: a fixed 40 % on the measured plant with fan-status glitches
        assert_as_recorded(self, m, recorded_json("canonical.json")["static40"]["measured/glitch"])

    def test_a_fixed_40_percent_through_the_load_step_spends_71_minutes_over_target(self):
        # bench/results/canonical.json: every poll over target counts, here each by at most 1 C
        m = bench.run_one("static40", bench.MODEL, "measured", "loadstep", 0, {})
        assert_as_recorded(self, m, recorded_json("canonical.json")["static40"]["measured/loadstep"])
        self.assertEqual((m["min_over_target"], m["max_excess"]), (71.25, 1.0))

    def test_the_tuning_set_reproduces_the_recorded_pi_controller(self):
        # bench/results/canonical.json, at the standard seed (no --seed): the PI candidate acts on
        # every reading, so its numbers move with the noise
        pi = os.path.join(BENCH, "variants", "pi.py")
        out = os.path.join(tmpdir(self), "out.json")
        text, code = printed(bench.main, [pi, "--set", "tuning", "--json", out])
        self.assertIsNone(code)
        want = recorded_json("canonical.json")["variants/pi.py"]
        with open(out) as f:
            written = f.read()
        got = json.loads(written)
        self.assertEqual(list(got), [pi])
        self.assertEqual(sorted(got[pi]), ["measured/daily", "measured/steady25", "measured/steady28",
                                           "measured/warmspell"])
        for case, m in got[pi].items():
            assert_as_recorded(self, m, want[case], case)
        # laid out as the recorded results are, so the two compare line by line
        self.assertEqual(written.splitlines()[:3], ["{", " %s: {" % json.dumps(pi), '  "measured/steady25": {'])
        self.assertTrue(written.splitlines()[3].startswith('   "mean_fan": '), written[:200])
        lines = text.splitlines()
        self.assertEqual(lines[1:3], ["== " + pi,
                                      "case                      meanFan  power40   chg/day  handoff  overTgtM"
                                      "   maxExc  minHead"])
        self.assertIn("measured/steady28            34.8     0.66       6.0        0       0.0     -1.0     21.2", lines)
        self.assertEqual(lines[-1], "MEAN / WORST                 33.7     0.62       9.7      0.0       0.0     -0.2     19.5")

    def test_bench_py_runs_as_a_script(self):
        out, code = run_script(os.path.join(BENCH, "bench.py"), "--help")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("usage: bench.py "), out)
        self.assertIn("--set {tuning,full}", out)


class SweepTests(unittest.TestCase):
    """bench/sweep.py, as bench/README.md runs it."""

    RECORDED = ("final", "sweep_cfg", "rejected_debounce", "rejected_backoff_all_decreases")

    def test_the_summary_lines_reproduce_the_recorded_sweeps(self):
        for name in self.RECORDED:
            runs = recorded_json(name + ".json")
            want = [l for l in recorded(name + ".txt").splitlines() if l != "done"]
            names = list(dict.fromkeys(r[0] for r in runs))
            self.assertEqual([l for l in sweep.render(names, runs) if " | " in l], want, name)

    def test_the_seed_sweep_lines_of_fanctl_1_3_0(self):
        self.assertEqual(sweep.render(["final"], recorded_json("final.json"))[1:], [
            "final                      loadstep   P(over) 0.15  mean over 0.10 min  max over 1.00 min  "
            "worst excess +1.0  fan 35.8  chg/day 54.5",
            "final                      warmspell  P(over) 0.00  mean over 0.00 min  max over 0.00 min  "
            "worst excess -0.6  fan 36.6  chg/day 24.0"])

    def test_each_candidate_gets_the_full_matrix_and_the_seed_sweep(self):
        js = sweep.jobs({"a": {}, "b": {"variant": "v.py", "cfg": {"min_authority": 0.5}, "env": {"X": "1"}}}, "m.json")
        self.assertEqual(len(js), 2 * (36 + 40))
        self.assertEqual(js[0], ("a", "band", "m.json", "measured", "steady25", 0, {}, {}))
        self.assertEqual(js[-1], ("b", "v.py", "m.json", "measured", "warmspell", 20, {"min_authority": 0.5},
                                  {"X": "1"}))
        self.assertEqual(sorted({(j[3], j[4]) for j in js if j[5] == 0}), sorted(bench.FULL_SET))

    def test_a_run_sets_the_candidate_s_environment_only_while_it_lasts(self):
        os.environ.pop("BANDAUTH_WMIN", None)
        os.environ["FANCTL_RESEARCH_TEST"] = "before"
        self.addCleanup(os.environ.pop, "FANCTL_RESEARCH_TEST", None)
        job = ("W_MIN 0.25", os.path.join(BENCH, "variants", "bandauth.py"), bench.MODEL, "measured", "loadstep", 1,
               {}, {"BANDAUTH_WMIN": "0.25", "FANCTL_RESEARCH_TEST": "during"})
        got = sweep.run(job)
        self.assertNotIn("BANDAUTH_WMIN", os.environ)                       # unset again
        self.assertEqual(os.environ["FANCTL_RESEARCH_TEST"], "before")      # and back to what it was
        self.assertEqual(got[:4], ["W_MIN 0.25", "measured", "loadstep", 1])
        # bench/results/sweep_wmin.json: {W_MIN: [[plant, scenario, seed, metrics]]}
        assert_as_recorded(self, got[4], [m for p, s, seed, m in recorded_json("sweep_wmin.json")["0.25"]
                                           if (p, s, seed) == ("measured", "loadstep", 1)][0])

    def test_a_sweep_prints_and_writes_the_report_of_every_run(self):
        # W_MIN 0.25 of bench/results/sweep_wmin.json, on the matrix cut down to the measured load step
        # and the safety sweep to seed 1: three runs, all recorded
        cand = {"W_MIN 0.25": {"variant": os.path.join(BENCH, "variants", "bandauth.py"),
                               "env": {"BANDAUTH_WMIN": "0.25"}}}
        out = os.path.join(tmpdir(self), "sweep.json")
        with mock.patch.object(bench, "FULL_SET", [("measured", "loadstep")]), \
                mock.patch.object(sweep, "SEEDS", range(1, 2)):
            text, code = printed(sweep.main, [json.dumps(cand), "--json", out])
        self.assertIsNone(code)
        runs = {(p, s, seed): m for p, s, seed, m in recorded_json("sweep_wmin.json")["0.25"]}
        want = [["W_MIN 0.25", p, s, seed, runs[p, s, seed]]
                for p, s, seed in (("measured", "loadstep", 0), ("measured", "loadstep", 1), ("measured", "warmspell", 1))]
        with open(out) as f:
            assert_as_recorded(self, json.load(f), want)
        self.assertEqual(text.splitlines(), sweep.render(["W_MIN 0.25"], want))
        self.assertEqual(len(text.splitlines()), 3)      # the summary line and one per seed-sweep scenario

    def test_sweep_py_runs_as_a_script(self):
        out, code = run_script(os.path.join(BENCH, "sweep.py"), "--help")
        self.assertEqual(code, 0)
        self.assertIn("python3 sweep.py CANDIDATES [--model MODEL.json] [--json OUT]", out)   # its docstring


class SteadyInletTests(unittest.TestCase):
    """bench/steady_inlets.py, which wrote bench/results/steady_inlets.txt."""

    def recorded_row(self, inlet):
        lines = recorded("steady_inlets.txt").splitlines()
        return lines[0], [l for l in lines if l.startswith("%4d C |" % inlet)][0]

    def test_a_25_c_inlet_settles_at_31_percent_as_recorded(self):
        _header, row = self.recorded_row(25)
        self.assertEqual(steady_inlets.cell(steady_inlets.run((25, 0.5, False))), row.split(" | ")[1].split(" / ")[0])
        self.assertEqual(row.split(" | ")[1].split(" / ")[0], "  31 (0; -2.0; Phy17-20)")

    def test_the_28_c_row_as_recorded(self):
        with mock.patch.object(steady_inlets, "INLETS", [28]):
            out, code = printed(steady_inlets.main)
        self.assertIsNone(code)
        self.assertEqual(out.splitlines(), list(self.recorded_row(28)))

    def test_a_ramped_run_starts_at_25_c_and_reaches_its_inlet_in_2_hours(self):
        sw = steady_inlets.inlet_switch(28, True, load_model())
        self.assertEqual(sw.plant.ambient, 25.0)
        self.assertEqual([sw.ambient_fn(hours * 3600.0) for hours in (0, 1, 2, 3)], [25.0, 26.5, 28.0, 28.0])
        sw = steady_inlets.inlet_switch(28, False, load_model())
        self.assertEqual((sw.plant.ambient, sw.ambient_fn), (28.0, None))

    def test_a_run_that_ends_under_eos_control_shows_auto(self):
        self.assertEqual(steady_inlets.cell((31, 1.0, True, None, 2, 0.5, "-")), "auto (2; +0.5; -)")


if __name__ == "__main__":
    unittest.main()

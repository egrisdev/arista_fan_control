"""Tests for the off-box tools: tools/report.py, tools/monitor.py and the SSH
shell they share with measurements/fan_step_test.py. Run from the repo root:
python3 -m unittest discover -s tests -v"""

import ast
import contextlib
import io
import json
import os
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "measurements"))

import eos  # noqa: E402
import fake_paramiko  # noqa: E402
import fan_step_test  # noqa: E402
import fanctl  # noqa: E402
import fanlog  # noqa: E402
import monitor  # noqa: E402
import report  # noqa: E402


def run_main(main, argv):
    """Run a tool's main(argv) as its command line would: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, (int, type(None))):
                err.write("%s\n" % e.code)
    return code, out.getvalue(), err.getvalue()


def poll(t, mode="manual", override=31, ambient=25.0, temps=None, fans=None, log=()):
    """One poll record as tools/monitor.py writes it."""
    rec = {"t": t, "event": "poll", "mode": mode, "override": override, "ambient": ambient,
           "temps": {"TempSensor4": 40.0, "TempSensor18": 60.0} if temps is None else temps,
           "fans": {"1/1": [31, 30, "ok"], "2/1": [31, 32, "ok"], "PowerSupply1/1": [31, 50, "ok"]}
           if fans is None else fans,
           "psu": {"1": ["ok", 80.0, 0.8]}, "tstatus": "temperatureOk", "cstatus": "coolingOk"}
    if log:
        rec["log"] = list(log)
    return rec


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fanctl-tools-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, records, tail=""):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            f.write(tail)
        return path


class ReportTests(ToolTest):
    """tools/report.py run the way an operator runs it on a monitor.jsonl."""

    def report(self, *args):
        return run_main(report.main, list(args))

    def test_summarises_a_run(self):
        hand_off = "Oct 19 07:00:00 switch fanctl[42]: handing fan control to EOS (auto) -> auto: x"
        status = "Oct 19 07:30:00 switch fanctl[42]: status: fans override 31%, 0 change(s)"
        path = self.write("monitor.jsonl", [
            poll(1000.0, ambient=24.0, temps={"TempSensor4": 41.0, "TempSensor18": 61.0}),
            {"t": 1030.0, "event": "error", "error": "timeout"},
            poll(1060.0, ambient=26.0, log=[hand_off, status]),
            poll(1120.0, mode="automatic", override=None, ambient=25.0,
                 temps={"TempSensor4": 40.0, "TempSensor18": 63.0}),
        ])
        code, out, err = self.report(path, "--targets", os.path.join(FIX, "temp.json"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(0.0 h), 3 polls, 1 read errors\n", out)
        self.assertIn("mode        manual 100.0%\n", out)
        self.assertIn("fan speed   31.0% time-weighted mean of actual tray speed\n", out)
        self.assertIn("override    min 31%, max 31%, last None\n", out)
        self.assertIn("changes     1 (", out)
        self.assertIn("inlet       min 24.00, mean 25.00, max 26.00 C\n", out)
        self.assertIn("  Phy17-20 (TempSensor18)                    peak  63.0 C  target 65.00  "
                      "margin  -2.0 C\n", out)
        self.assertLess(out.index("Phy17-20"), out.index("Board sensor"))
        self.assertIn("\nnotable log lines\n  " + hand_off + "\n\n1 hourly status lines omitted; "
                      "--status-lines lists them\n", out)
        code, out, err = self.report(path, "--status-lines", "--since", "1060", "--until", "1120")
        self.assertIn("hourly status lines\n  " + status + "\n", out)
        self.assertIn(", 2 polls, 0 read errors\n", out)

    def test_a_day_long_run(self):
        # 6 h: 3 h at override 31 % then 3 h under EOS, one 10-minute monitor gap,
        # sensors with and without a target, the same log line read twice
        line = "Oct 19 07:00:00 switch fanctl[42]: handing fan control to EOS (auto) -> auto: x"
        polls, t = [], 0.0
        while t <= 6 * 3600:
            auto = t >= 3 * 3600
            polls.append(poll(t, mode="automatic" if auto else "manual", override=None if auto else 31,
                              fans={"1/1": [31, 60 if auto else 30, "ok"], "2/1": [31, 60 if auto else 34, "ok"],
                                    "PowerSupply1/1": [31, 90, "ok"]},
                              temps={"TempSensor1": 50.0, "TempSensor4": 41.0, "TempSensor18": 63.0,
                                     "TempSensorX": 70.0},
                              log=[line] if t in (3 * 3600, 3 * 3600 + 300) else ()))
            t += 600.0 if t == 3600 else 60.0
        path = self.write("monitor.jsonl", polls)
        code, out, err = self.report(path, "--targets", os.path.join(FIX, "temp.json"))
        self.assertIn("period      %s to %s (6.0 h), %d polls, 0 read errors\n"
                      % (time.strftime("%Y-%m-%d %H:%M", time.localtime(0)),
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(6 * 3600)), len(polls)), out)
        self.assertIn("mode        automatic 51.1%, manual 48.9%\n", out)
        self.assertIn("fan speed   46.3% time-weighted mean of actual tray speed\n", out)
        self.assertIn("override    min 31%, max 31%, last None\n", out)
        self.assertIn("changes     1 (4.0 per day)\n", out)
        names = [l.split()[0] for l in out.split("closest approach to target\n")[1].split("\n\n")[0].splitlines()]
        self.assertEqual(names, ["Phy17-20", "Board", "Cpu", "TempSensorX"])
        self.assertEqual(out.count(line), 1)

    def test_lists_fanctl_refusing_to_start(self):
        lines = ["Oct 19 07:00:00 switch fanctl[42]: invalid settings, not starting: --floor must be 10..100 %",
                 "Oct 19 07:00:01 switch fanctl: bad command line, not starting: unrecognized arguments: -x",
                 "Oct 19 07:00:02 switch fanctl[43]: returning to automatic fan control failed: x"]
        path = self.write("monitor.jsonl", [poll(1000.0, log=lines), poll(1030.0)])
        code, out, err = self.report(path)
        self.assertIn("notable log lines\n" + "".join("  %s\n" % l for l in lines), out)

    def test_a_truncated_last_line_is_skipped(self):
        # The normal end of a long run: the monitor host was killed mid-write.
        path = self.write("monitor.jsonl", [poll(1000.0), poll(1030.0), poll(1060.0)],
                          tail='{"t": 1090.0, "event": "po')
        code, out, err = self.report(path)
        self.assertEqual(code, 0, err)
        self.assertIn(", 3 polls, 0 read errors\n", out)
        self.assertIn("line 4: not a monitor record", err)
        self.assertIn("1 unreadable line skipped", out)

    def test_a_record_without_the_fields_it_needs_is_skipped(self):
        bad = poll(1030.0)
        del bad["fans"]
        path = self.write("monitor.jsonl", [poll(1000.0), bad, ["not", "a", "record"],
                                            poll(1060.0), poll(1090.0)])
        code, out, err = self.report(path)
        self.assertEqual(code, 0, err)
        self.assertIn(", 3 polls, 0 read errors\n", out)
        self.assertIn("line 2: poll without fans", err)
        self.assertIn("line 3: not a monitor record", err)
        self.assertIn("2 unreadable lines skipped", out)

    def test_a_tray_fan_that_does_not_read_is_left_out_of_the_mean(self):
        fans = {"1/1": [31, None, "failed"], "2/1": [31, 32, "ok"], "PowerSupply1/1": [31, 50, "ok"]}
        path = self.write("monitor.jsonl", [poll(1000.0, fans=fans), poll(1030.0, fans=fans)])
        code, out, err = self.report(path)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("fan speed   32.0% time-weighted mean of actual tray speed\n", out)

    def test_the_override_range_counts_only_polls_under_an_override(self):
        # an overrideFanSpeed EOS reports while it has the fans is not fanctl's override
        path = self.write("monitor.jsonl", [poll(1000.0, override=35), poll(1030.0, mode="automatic", override=0),
                                            poll(1060.0, mode="automatic", override=0)])
        code, out, err = self.report(path)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("override    min 35%, max 35%, last ", out)

    def test_other_records_are_skipped(self):
        path = self.write("monitor.jsonl", [poll(1000.0), {"t": 1010.0, "event": "set", "pct": 40},
                                            dict(poll(1020.0), t="soon"), dict(poll(1030.0), t=True),
                                            poll(1060.0)])
        code, out, err = self.report(path)
        self.assertIn(", 2 polls, 0 read errors\n", out)
        self.assertEqual(err.count("not a monitor record"), 3)

    def test_polls_all_at_one_time_are_not_enough(self):
        path = self.write("monitor.jsonl", [poll(1000.0), poll(1000.0), poll(1000.0)])
        self.assertEqual(self.report(path)[0], 1)
        path = self.write("monitor.jsonl", [poll(1000.0), poll(1000.0), poll(1030.0)])
        self.assertEqual(self.report(path)[0], 0)

    def test_a_run_without_an_inlet_reading_says_so(self):
        path = self.write("monitor.jsonl", [poll(1000.0, ambient=None), poll(1030.0, ambient=None)])
        code, out, err = self.report(path)
        self.assertEqual(code, 0, err)
        self.assertIn("inlet       n/a (no reading)\n", out)

    def test_a_sensor_is_listed_if_it_reads_in_any_poll(self):
        path = self.write("monitor.jsonl", [poll(1000.0, temps={"TempSensor4": 40.0, "TempSensorP2/1": None}),
                                            poll(1030.0, temps={"TempSensor4": 40.0, "TempSensorP1/1": 33.0,
                                                                "TempSensorP2/1": None})])
        code, out, err = self.report(path)
        self.assertEqual(code, 0, err)
        self.assertIn("  TempSensorP1/1                             peak  33.0 C\n", out)
        self.assertNotIn("TempSensorP2/1", out)

    def test_a_sensor_without_a_description_is_named(self):
        with open(os.path.join(FIX, "temp.json")) as f:
            temp = json.load(f)
        del temp["tempSensors"][0]["description"]
        name = temp["tempSensors"][0]["name"]
        targets = self.write("temp.json", [temp])
        path = self.write("monitor.jsonl", [poll(1000.0, temps={name: 50.0}), poll(1030.0, temps={name: 51.0})])
        code, out, err = self.report(path, "--targets", targets)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("\n  %-42s peak  51.0 C  target " % name, out)

    def test_only_a_setpoint_above_0_is_a_target(self):
        # fanctl's pick_target counts only a setpoint above 0 as EOS's own
        with open(os.path.join(FIX, "temp.json")) as f:
            temp = json.load(f)
        zero, one = temp["tempSensors"][0], temp["tempSensors"][1]
        zero["setPointTemperature"], one["setPointTemperature"] = 0, 1
        targets = self.write("temp.json", [temp])
        temps = {zero["name"]: 50.0, one["name"]: 30.0, "TempSensor18": 63.0}
        path = self.write("monitor.jsonl", [poll(1000.0, temps=temps), poll(1030.0, temps=temps)])
        code, out, err = self.report(path, "--targets", targets)
        self.assertEqual((code, err), (0, ""))
        rows = out.split("closest approach to target\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual(rows, ["  %-42s peak  30.0 C  target  1.00  margin +29.0 C"
                                % ("%s (%s)" % (one["description"], one["name"])),
                                "  Phy17-20 (TempSensor18)                    peak  63.0 C  target 65.00  "
                                "margin  -2.0 C",
                                "  %-42s peak  50.0 C" % zero["name"]])

    def test_targets_that_are_not_a_temperature_json_are_an_error_naming_the_file(self):
        path = self.write("monitor.jsonl", [poll(1000.0), poll(1030.0)])
        with open(os.path.join(FIX, "temp.json")) as f:
            nameless = json.load(f)
        del nameless["powerSupplySlots"][0]["tempSensors"][0]["name"]
        for targets, why in ((os.path.join(FIX, "cooling.json"), "not a saved `show system environment "
                                                                  "temperature | json`"),
                             (self.write("list.json", [[1, 2]]), "not a saved"),
                             (self.write("cut.json", [], tail='{"tempSensors": ['), "Expecting value"),
                             (self.write("nameless.json", [nameless]), "an environment JSON entry without a name"),
                             (os.path.join(self.dir, "missing.json"), "No such file")):
            code, out, err = self.report(path, "--targets", targets)
            self.assertEqual(code, 1, targets)
            self.assertIn("--targets %s: " % targets, err)
            self.assertIn(why, err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(len(err.splitlines()), 1, err)

    def test_too_few_polls_is_an_error_not_a_traceback(self):
        path = self.write("monitor.jsonl", [poll(1000.0)], tail="garbage\n")
        code, out, err = self.report(path)
        self.assertEqual(code, 1)
        self.assertIn("not enough polls", err)
        self.assertNotIn("Traceback", err)

    def test_help_says_what_it_does(self):
        code, out, err = self.report("--help")
        self.assertEqual(code, 0)
        self.assertIn("\n\nSummarise a tools/monitor.py run.\n", out)

    def test_runs_as_a_script_without_paramiko(self):
        path = self.write("monitor.jsonl", [poll(1000.0), poll(1030.0)], tail='{"t": 10')
        absent = os.path.join(self.dir, "absent", "paramiko")     # shadows an installed paramiko
        os.makedirs(absent)
        with open(os.path.join(absent, "__init__.py"), "w") as f:
            f.write("raise ImportError('No module named paramiko')\n")
        p = subprocess.run([sys.executable, os.path.join(TOOLS, "report.py"), path,
                            "--targets", os.path.join(FIX, "temp.json")],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           env=dict(os.environ, PYTHONPATH=os.path.dirname(absent)), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(", 2 polls, 0 read errors\n", p.stdout)
        self.assertIn("closest approach to target\n", p.stdout)
        self.assertIn("line 3: not a monitor record, skipped", p.stderr)


class FakeClock(object):
    """Stands in for time.time and time.sleep: sleeping moves the clock on at once
    and runs whatever the test scheduled for that time."""

    def __init__(self, now=1.8e9 + 0.5):
        self.start = self.now = now
        self.events = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s
        due = [e for e in self.events if e[0] <= self.now - self.start]
        self.events = [e for e in self.events if e not in due]
        for _, fn in due:
            fn()

    def at(self, t, fn):
        """Run fn once the clock has moved t s on from the start."""
        self.events.append((t, fn))


def _format(node):
    """The literal text of a log message expression, or None if it isn't one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _format(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _format(node.left)
        return None if left is None else left + "X"
    return None


def fanctl_messages():
    """([(level, first line of the message as logged)], [what isn't literal]) for
    every log call in fanctl.py: self.log(level, msg), log.limited(key, level, msg),
    log(level, msg) and syslog.syslog(syslog.LOG_ERR, msg)."""
    with open(os.path.join(ROOT, "fanctl.py")) as f:
        tree = ast.parse(f.read())
    found, dynamic = [], []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or len(call.args) < 2:
            continue
        name = getattr(call.func, "attr", getattr(call.func, "id", None))
        if name not in ("log", "limited", "syslog"):
            continue
        level, msg = call.args[1:3] if name == "limited" else call.args[:2]
        if isinstance(level, ast.Attribute) and level.attr.startswith("LOG_"):   # syslog.LOG_ERR
            level = ast.Constant(level.attr[4:].lower())
        text = _format(msg)
        if not isinstance(level, ast.Constant) or text is None:
            dynamic.append(ast.unparse(level if not isinstance(level, ast.Constant) else msg))
            continue
        text = re.sub(r"%[-+ #0-9.]*[sdgfr]", "X", text).replace("%%", "%")
        found.append((level.value, text.splitlines()[0]))
    return found, dynamic


def guard_messages():
    """[(level, message)] for every `log LEVEL "message"` in fanctl-guard.sh."""
    with open(os.path.join(ROOT, "fanctl-guard.sh")) as f:
        return [(level, re.sub(r"\$\{?\w+\}?", "X", text))
                for level, text in re.findall(r'\blog (\w+) "([^"]*)"', f.read())]


def syslog_line(tag, msg):
    return "Oct 19 07:00:00 switch %s: %s" % (tag, msg)


class FanlogTests(unittest.TestCase):
    """One definition of which log lines matter (tools/fanlog.py), checked
    against every message fanctl and the guard can log."""

    def assertNotable(self, line, notable):
        self.assertEqual(bool(fanlog.NOTABLE.search(line)), notable, line)
        self.assertTrue(fanlog.RECORD.search(line), line)

    def test_every_fanctl_warning_and_error_is_notable_and_nothing_else(self):
        found, dynamic = fanctl_messages()
        self.assertGreaterEqual(len(found), 40)
        for level, msg in found:
            self.assertNotable(syslog_line("fanctl[42]", msg), level in ("err", "warning"))
        # the calls whose level or text isn't literal, each covered below
        self.assertEqual(sorted(dynamic), ["d.why[0]", "guessed", "level", "prio"])

    def test_the_messages_built_at_run_time(self):
        for event in list(fanctl.CHANGES) + [fanctl.EV_RETRY]:  # level: warning for a hand-off, else notice
            line = fanctl.change_line(fanctl.Decision(event, []), ("override", 40))
            self.assertNotable(syslog_line("fanctl[42]", line), event in (fanctl.EV_RELEASE, fanctl.EV_RESUME))
        guessed = fanctl.describe_guessed_targets(       # guessed: a warning
            mock.Mock(sensors=[mock.Mock(label="Board sensor", target_src="alert")]), mock.Mock(target_factor=0.65))
        self.assertNotable(syslog_line("fanctl[42]", guessed), True)
        cfg = fanctl.make_config(fanctl.parse_args([]))
        retry = fanctl.Controller(cfg, fanctl.AUTO, 0.0)._read_failed(0.0).why[0]     # d.why[0]: info
        self.assertNotable(syslog_line("fanctl[42]", retry), False)

    def test_every_guard_warning_and_error_is_notable_and_nothing_else(self):
        found = guard_messages()
        self.assertEqual(sorted(set(level for level, _ in found)), ["err", "notice", "warning"])
        for level, msg in found:
            self.assertNotable(syslog_line("fanctl-guard", msg), level in ("err", "warning"))

    def test_procmgr_and_environment_lines(self):
        for line, notable in [
                ("%PROCMGR-6-PROCESS_TERMINATED: 'fanctl' (PID=4242, status=9) has terminated.", True),
                ("%PROCMGR-6-PROCESS_TERMINATED: 'fanctl-guard' (PID=4243, status=9) has terminated.", True),
                ("%PROCMGR-3-PROCESS_DELAYRESTART: 'fanctl' (PID=4242) restarted too often", True),
                ("%PROCMGR-6-PROCESS_STARTED: 'fanctl' starting with PID=4244", False),
                ("%PROCMGR-6-PROCESS_RESTART: Restarting 'fanctl' immediately", False),
                ("%ENVMON-4-FAN_SPEED_LOW: fan 1/1 is running too slowly", True)]:
            self.assertNotable(syslog_line("ProcMgr-worker", line), notable)
        self.assertFalse(fanlog.RECORD.search(syslog_line("Ebra", "%LINEPROTO-5-UPDOWN: Ethernet1 is up")))
        self.assertTrue(fanlog.STATUS.search(syslog_line("fanctl[42]", "status: fans override 31%, 0 change(s)")))


class MonitorTests(ToolTest):
    """tools/monitor.py run the way an operator runs it, against a fake switch."""

    def setUp(self):
        ToolTest.setUp(self)
        self.sw = fake_paramiko.Switch()
        self.clock = FakeClock()
        self.env = {"SWITCH_HOST": self.sw.host, "SWITCH_USERNAME": "admin", "SWITCH_PASSWORD": "secret"}
        for p in (mock.patch.dict(sys.modules, {"paramiko": fake_paramiko}),
                  mock.patch.object(fake_paramiko, "SWITCH", self.sw),
                  mock.patch("time.time", self.clock.time), mock.patch("time.sleep", self.clock.sleep)):
            p.start()
            self.addCleanup(p.stop)

    def monitor(self, hours):
        """Run the monitor for `hours`: (exit code, stdout, stderr)."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            return run_main(monitor.main, [str(hours), self.dir])

    def records(self):
        with open(os.path.join(self.dir, "monitor.jsonl")) as f:
            return [json.loads(l) for l in f]

    def alerts(self):
        with open(os.path.join(self.dir, "alerts.log")) as f:
            return [l.rstrip("\n").split(" ", 2)[2] for l in f]

    def status(self):
        with open(os.path.join(self.dir, "monitor_status.json")) as f:
            return json.load(f)

    def test_records_every_poll_and_the_slow_reads_every_5_min(self):
        self.assertEqual(self.monitor(0.1)[0], 0)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["poll"] * 12)
        self.assertEqual([r["t"] - recs[0]["t"] for r in recs], [30.0 * i for i in range(12)])
        first = recs[0]
        self.assertEqual(first["temps"]["TempSensor18"], 55.0)
        self.assertEqual(first["temps"]["TempSensorP2/2"], 32.0)
        self.assertEqual((first["ambient"], first["mode"], first["override"]), (25.25, "manual", 40))
        self.assertEqual(first["fans"]["1/1"], [40, 39, "ok"])
        self.assertEqual(first["fans"]["PowerSupply1/1"], [40, 40, "ok"])
        self.assertEqual(first["psu"]["1"][:2], ["ok", 84.125])
        self.assertEqual((first["tstatus"], first["cstatus"]), ("temperatureOk", "coolingOk"))
        self.assertEqual(first["fanctl_status"], self.sw.status_file)
        self.assertEqual(first["daemons"], [["fanctl", "running with PID 4242"],
                                            ["fanctl-guard", "running with PID 4243"]])
        self.assertEqual([i for i, r in enumerate(recs) if "daemons" in r], [0, 10])
        self.assertEqual(self.alerts(), ["monitor finished after 0.1 h"])
        st = self.status()
        self.assertEqual((st["mode"], st["override"], st["ambient"], st["worst"][1]),
                         ("manual", 40, 25.25, "Front-panel temp sensor"))
        self.assertAlmostEqual(st["worst"][0], -6.6, 1)
        self.assertEqual(st["elapsed_h"], 0.09)
        # read-only: one login, enable, then only show commands and the status file
        self.assertEqual(self.sw.logins, 1)
        self.assertEqual(self.sw.sent[:2], ["enable", "terminal length 0"])
        self.assertEqual(set(self.sw.sent[2:]), {
            "show system environment temperature | json", "show system environment cooling | json",
            "show system environment power | json", "bash cat /run/fanctl/fanctl.status.json; echo",
            "show daemon", "show logging last 10 minutes"})

    def test_keeps_fanctl_log_lines_once_and_alerts_on_the_notable_ones(self):
        hand_off = "Oct 19 07:00:00 switch fanctl[42]: handing fan control to EOS (auto) -> auto: x"
        lowering = "Oct 19 07:01:00 switch fanctl[42]: lowering fans -> override 38%: binding: x"
        restart = "Oct 19 07:02:00 switch ProcMgr-worker: %PROCMGR-3-PROCESS_DELAYRESTART: 'fanctl'"
        self.sw.logging = [hand_off, "Oct 19 07:01:30 switch Ebra: Ethernet1 is up", lowering, restart]
        self.monitor(0.1)
        recs = self.records()
        self.assertEqual(recs[0]["log"], [hand_off, lowering, restart])
        self.assertNotIn("log", recs[10])
        self.assertEqual(self.alerts(), [
            "log: fanctl[42]: handing fan control to EOS (auto) -> auto: x",
            "log: ProcMgr-worker: %PROCMGR-3-PROCESS_DELAYRESTART: 'fanctl'",
            "monitor finished after 0.1 h"])

    def test_alerts_when_eos_will_not_take_the_fans_back(self):
        self.sw.logging = [
            "Oct 19 07:00:00 switch fanctl[42]: EOS would not return to automatic fan control; fans set to "
            "override 100% instead (retrying auto every 5 min)",
            "Oct 19 07:05:00 switch fanctl[42]: fanctl stopped by signal 15: EOS refused automatic fan control; "
            "fans left at override 31%",
            "Oct 19 07:06:00 switch fanctl[42]: returning to automatic fan control failed: x"]
        self.monitor(0.01)
        self.assertEqual(self.alerts(), ["log: " + l.split(" ", 4)[-1] for l in self.sw.logging]
                         + ["monitor finished after 0.0 h"])

    def test_alerts_on_heat_and_hardware_faults_at_every_poll(self):
        self.sw.sensor("TempSensor18")["currentTemperature"] = 66.0
        self.sw.sensor("TempSensor4")["currentTemperature"] = 56.0
        self.sw.cooling["fanTraySlots"][0]["fans"][0]["status"] = "failed"
        self.monitor(0.05)
        alerts = self.alerts()
        self.assertEqual(alerts.count("over target: Phy17-20 66.0 C (target 65.0)"), 6)
        self.assertEqual(alerts.count("over target: Board sensor 56.0 C (target 45.5)"), 6)
        self.assertEqual(alerts.count("near alert limit: Board sensor 56.0 C (alert 70.0)"), 6)
        self.assertEqual(alerts.count("hardware status: ['1/1'] temperatureOk coolingOk"), 6)
        self.assertEqual(len(alerts), 25)
        self.assertEqual(self.status()["worst"], [10.5, "Board sensor"])

    def test_alerts_only_past_the_limits(self):
        self.sw.sensor("TempSensor18")["currentTemperature"] = 65.0           # at its target
        self.sw.sensor("TempSensor4")["currentTemperature"] = 55.0            # 15 C under its alert limit
        del self.sw.sensor("TempSensor3")["overheatThreshold"]                # no alert limit...
        self.sw.sensor("TempSensor3")["currentTemperature"] = 60.0            # ...but over target
        del self.sw.sensor("TempSensor5")["setPointTemperature"]              # no target
        self.monitor(0.005)
        self.assertEqual(self.alerts(), ["over target: Board sensor 55.0 C (target 45.5)",
                                         "monitor finished after 0.0 h"])
        self.assertEqual(self.status()["worst"], [10.0, "Back-panel temp sensor"])
        self.assertEqual(self.records()[0]["t"], round(self.clock.start, 1))

    def test_alerts_on_an_eos_status_that_is_not_ok(self):
        self.sw.temp["systemStatus"] = "temperatureCritical"
        self.monitor(0.01)
        self.assertEqual(self.alerts()[0], "hardware status: [] temperatureCritical coolingOk")

    def test_alerts_once_when_eos_gets_the_fans(self):
        def auto():
            self.sw.cooling["coolingMode"], self.sw.cooling["overrideFanSpeed"] = "automatic", None
        self.clock.at(100, auto)
        self.monitor(0.1)
        self.assertEqual(self.alerts(), ["fan setting now automatic None", "monitor finished after 0.1 h"])
        self.assertEqual([r["mode"] for r in self.records()], ["manual"] * 4 + ["automatic"] * 8)

    def test_alerts_on_a_stopped_daemon_and_a_missing_status_file(self):
        self.sw.daemons["fanctl"] = "shutdown"
        self.sw.status_file = None
        self.monitor(0.05)
        alerts = self.alerts()
        self.assertEqual(alerts[1], "daemon fanctl is shutdown")
        self.assertTrue(alerts[0].startswith("cannot read fanctl status file: "), alerts)
        self.assertIn("error", self.records()[0]["fanctl_status"])

    def test_alerts_once_after_6_failed_polls_in_a_row(self):
        self.sw.down = True
        self.clock.at(250, lambda: setattr(self.sw, "down", False))
        self.monitor(0.1)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["error"] * 9 + ["poll"] * 3)
        self.assertIn("timed out", recs[0]["error"])
        self.assertEqual(len(self.alerts()), 2)
        self.assertTrue(self.alerts()[0].startswith("monitor cannot read the switch (6 failures in a row): "))

    def test_alerts_again_only_after_6_more_failures(self):
        down = [(0, True), (155, False), (235, True), (385, False)]       # 6 failed polls, 2 good, 5 failed
        for t, state in down:
            self.clock.at(t, lambda state=state: setattr(self.sw, "down", state))
        self.sw.down = True
        self.monitor(0.15)
        self.assertEqual([r["event"] for r in self.records()],
                         ["error"] * 6 + ["poll"] * 2 + ["error"] * 5 + ["poll"] * 5)
        self.assertEqual(len([a for a in self.alerts() if a.startswith("monitor cannot read")]), 1)
        self.assertEqual(self.records()[0]["t"], round(self.clock.start, 1))

    def test_five_failures_are_not_yet_an_alert(self):
        self.sw.down = True
        self.clock.at(125, lambda: setattr(self.sw, "down", False))
        self.monitor(0.05)
        self.assertEqual([r["event"] for r in self.records()], ["error"] * 5 + ["poll"])
        self.assertEqual(self.alerts(), ["monitor finished after 0.1 h"])

    def test_logs_in_with_the_password_only(self):
        self.monitor(0.01)
        self.assertEqual((self.sw.options["look_for_keys"], self.sw.options["allow_agent"]), (False, False))

    def test_a_hung_command_times_out_and_reconnects(self):
        self.sw.hang = {"show system environment power | json"}
        self.monitor(0.05)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["error"] * 3)     # each waits 60 s
        self.assertEqual([round(r["t"] - recs[0]["t"]) for r in recs], [0, 61, 122])
        self.assertIn("timeout waiting for the switch ('show system environment power | json", recs[0]["error"])
        self.assertEqual(self.sw.logins, 3)
        self.assertEqual(len(self.alerts()), 1)

    def reconnect_fails_at(self, t, **errors):
        """At t s, drop the session; the logins after it fail with errors (Switch fields)."""
        def drop():
            self.sw.drop = {"show system environment temperature | json"}
            for name, errs in errors.items():
                setattr(self.sw, name, list(errs))
        self.clock.at(t, drop)
        self.clock.at(t + 30, lambda: setattr(self.sw, "drop", set()))

    def test_a_connection_lost_mid_answer_is_a_failed_read(self):
        cmd = "show system environment cooling | json"
        self.clock.at(100, lambda: self.sw.lost.add(cmd))
        self.clock.at(130, lambda: self.sw.lost.discard(cmd))
        self.assertEqual(self.monitor(0.1)[0], 0)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["poll"] * 4 + ["error"] + ["poll"] * 7)
        self.assertEqual(recs[4]["error"], "connection lost: ")
        self.assertEqual(self.sw.logins, 2)
        self.assertEqual(self.alerts(), ["monitor finished after 0.1 h"])

    def test_a_session_that_will_not_close_cleanly_is_dropped_all_the_same(self):
        self.reconnect_fails_at(100, close_errors=[OSError(9, "Bad file descriptor")])
        self.assertEqual(self.monitor(0.1)[0], 0)
        self.assertEqual([r["event"] for r in self.records()], ["poll"] * 4 + ["error"] + ["poll"] * 7)
        self.assertEqual(self.sw.logins, 2)
        self.assertEqual(self.alerts(), ["monitor finished after 0.1 h"])

    def test_a_shell_that_will_not_open_is_a_failed_read(self):
        # the switch's session limit, or a session cut right after the login
        self.reconnect_fails_at(100, shell_errors=[
            fake_paramiko.SSHException("Timeout opening channel."),
            fake_paramiko.ChannelException(1, "Administratively prohibited")])
        self.assertEqual(self.monitor(0.1)[0], 0)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["poll"] * 4 + ["error"] * 3 + ["poll"] * 5)
        self.assertIn("cannot connect to switch.example: Timeout opening channel.", recs[5]["error"])
        self.assertIn("Administratively prohibited", recs[6]["error"])
        self.assertEqual(self.sw.logins, 4)
        self.assertEqual(self.alerts(), ["monitor finished after 0.1 h"])

    def test_a_login_cut_short_is_a_failed_read_and_a_refusal_still_stops_it(self):
        # a slow TACACS or RADIUS server, or an sshd that hangs up during the login
        self.reconnect_fails_at(100, login_errors=[
            fake_paramiko.AuthenticationException("Authentication timeout."),
            fake_paramiko.AuthenticationException("Authentication failed: transport shut down or saw EOF"),
            fake_paramiko.AuthenticationException("Authentication failed.")])
        code, out, err = self.monitor(1)
        self.assertEqual(code, 1)
        self.assertIn("cannot log in: switch.example: Authentication failed.", err)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["poll"] * 4 + ["error"] * 3)
        self.assertIn("cannot log in to switch.example: Authentication timeout.", recs[5]["error"])
        self.assertIn("transport shut down or saw EOF", recs[6]["error"])
        self.assertEqual(self.alerts(), ["monitor stopped after 0.1 h: switch.example: Authentication failed."])

    def test_a_refused_password_stops_the_monitor_at_once(self):
        self.sw.password = "changed"
        code, out, err = self.monitor(1)
        self.assertEqual(code, 1)
        self.assertIn("cannot log in: switch.example: Authentication failed.", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.sw.logins, 1)          # not every 30 s for the whole run
        self.assertEqual(self.alerts(), ["monitor stopped after 0.0 h: switch.example: Authentication failed."])

    def test_an_unknown_host_key_stops_the_monitor(self):
        self.sw.known_hosts = {}
        code, out, err = self.monitor(1)
        self.assertEqual(code, 1)
        self.assertIn("the host key of switch.example is not in ~/.ssh/known_hosts", err)
        self.assertEqual(self.sw.logins, 0)

    def test_a_new_host_key_is_accepted_if_asked_and_then_pinned(self):
        self.sw.known_hosts = {}
        self.env["ACCEPT_NEW_HOST_KEY"] = "1"

        def replace_switch():
            self.sw.key = fake_paramiko.Key("SHA256:someone-else")
            self.sw.drop = {"show system environment temperature | json"}
        self.clock.at(100, replace_switch)
        code, out, err = self.monitor(1)
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["poll"] * 4 + ["error"])
        self.assertIn("closed the session ('show system environment temperature | json", recs[-1]["error"])
        self.assertEqual(self.sw.logins, 1)

    def test_logs_in_with_the_enable_password(self):
        self.sw.enable_password = "enable-secret"
        self.env["SWITCH_ENABLE_PASSWORD"] = "enable-secret"
        self.assertEqual(self.monitor(0.01)[0], 0)
        self.assertEqual(self.records()[0]["event"], "poll")
        self.env["SWITCH_ENABLE_PASSWORD"] = "wrong"
        code, out, err = self.monitor(0.01)
        self.assertEqual(code, 1)
        self.assertIn("switch.example: enable failed", err)

    def test_prompts_for_a_password_that_is_not_set(self):
        del self.env["SWITCH_PASSWORD"]
        with mock.patch("getpass.getpass", return_value="secret") as prompt:
            self.assertEqual(self.monitor(0.01)[0], 0)
        prompt.assert_called_once_with("password for switch.example: ")
        self.assertEqual(self.records()[0]["event"], "poll")

    def test_without_a_host_or_user_prints_the_usage(self):
        for name in ("SWITCH_HOST", "SWITCH_USERNAME"):
            env = dict(self.env)
            del self.env[name]
            code, out, err = self.monitor(1)
            self.env = env
            self.assertEqual(code, 1)
            self.assertIn("Read-only field monitor", err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "monitor.jsonl")))
        with mock.patch.dict(os.environ, self.env, clear=True):
            for argv in (["soon"], ["0"], ["1", self.dir, "extra"], []):
                self.assertEqual(run_main(monitor.main, argv)[0], 1, argv)
            with mock.patch.object(sys, "argv", ["monitor.py", "0.01", self.dir]):
                self.assertEqual(run_main(lambda argv: monitor.main(), [])[0], 0)
        self.assertEqual(self.records()[0]["event"], "poll")

    def test_runs_as_a_script(self):
        p = subprocess.run([sys.executable, os.path.join(TOOLS, "monitor.py")], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Read-only field monitor", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_output_it_cannot_use_is_a_failed_read(self):
        del self.sw.sensor("TempSensor3")["name"]
        self.monitor(0.05)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["error"] * 6)
        self.assertIn("an environment JSON entry without a name", recs[0]["error"])
        self.assertEqual(self.alerts()[0], "monitor cannot read the switch (6 failures in a row): "
                         "an environment JSON entry without a name")

    def test_a_reading_that_is_not_a_number_is_a_failed_read(self):
        self.sw.sensor("TempSensor4")["currentTemperature"] = "hot"
        self.monitor(0.01)
        self.assertIn("unexpected currentTemperature", self.records()[0]["error"])

    def test_text_where_json_was_asked_for_is_a_failed_read(self):
        with mock.patch.object(self.sw, "answer", lambda cmd, mode: ("% Not supported", mode)):
            self.monitor(0.01)
        self.assertIn("printed no JSON object: '% Not supported'", self.records()[0]["error"])

    def test_json_of_another_shape_is_a_failed_read(self):
        for doc, key in ((self.sw.temp, "tempSensors"), (self.sw.power, "powerSupplies")):
            saved = doc[key]
            doc[key] = [42] if key == "tempSensors" else {"1": "ok"}
            self.monitor(0.01)
            doc[key] = saved
            self.assertIn("unexpected %s in the environment JSON" % key, self.records()[-1]["error"])

    def test_its_own_bug_stops_it_with_an_alert(self):
        with mock.patch.object(monitor, "sensor_alerts", side_effect=TypeError("a bug")):
            self.assertRaises(TypeError, self.monitor, 1)
        self.assertEqual(self.alerts(), ["monitor stopped after 0.0 h: a bug"])
        self.assertEqual(self.sw.logins, 1)


class StepTestTests(ToolTest):
    """measurements/fan_step_test.py's switch session, against a fake switch:
    the parts it shares with the monitor. StepTestRunTests runs the whole test."""

    def setUp(self):
        ToolTest.setUp(self)
        self.sw = fake_paramiko.Switch()
        for p in (mock.patch.dict(sys.modules, {"paramiko": fake_paramiko}),
                  mock.patch.object(fake_paramiko, "SWITCH", self.sw)):
            p.start()
            self.addCleanup(p.stop)
        login = eos.Login(self.sw.host, "admin", "secret", "secret", False)
        self.test = fan_step_test.Test(os.path.join(self.dir, "data.jsonl"), os.path.join(self.dir, "status.json"),
                                       eos.Shell(login, fan_step_test.ALLOWED))
        self.addCleanup(self.test.data.close)
        self.test.sh.connect()

    def test_sets_an_override_and_records_a_poll(self):
        self.test.set_fans(55)
        self.assertEqual((self.sw.cooling["coolingMode"], self.sw.cooling["overrideFanSpeed"]), ("manual", 55))
        self.assertEqual(self.test.poll(3, 55), ([], [], 25.25))
        with open(os.path.join(self.dir, "data.jsonl")) as f:
            recs = [json.loads(l) for l in f]
        self.assertEqual([r["event"] for r in recs], ["set", "poll"])
        self.assertEqual((recs[1]["step"], recs[1]["pct"], recs[1]["override"]), (3, 55, 55))
        self.assertEqual(recs[1]["temps"]["TempSensor18"], 55.0)
        self.assertEqual(recs[1]["psu"]["1"], ["ok", 84.125, 0.806640625])

    def test_sends_no_other_configuration(self):
        for cmd in ("environment fan-speed override 71", "environment fan-speed override 29",
                    "environment fan-speed override 100", "write memory", "bash rm -f x",
                    "environment fan-speed auto; write memory", "no environment fan-speed x"):
            self.assertRaises(ValueError, self.test.sh.run, cmd)
        self.assertNotIn("write memory", self.sw.sent)
        for pct in (30, 70):
            self.test.set_fans(pct)
            self.assertEqual(self.sw.cooling["overrideFanSpeed"], pct)


class StepTestRunTests(ToolTest):
    """measurements/fan_step_test.py run the way a contributor runs it, on a fake
    switch with fanctl and its guard stopped, and a fake clock: the three-hour
    schedule takes a few seconds."""

    def setUp(self):
        ToolTest.setUp(self)
        self.sw = fake_paramiko.Switch()
        self.sw.daemons = {"fanctl": "shutdown", "fanctl-guard": "shutdown"}
        self.clock = FakeClock()
        self.env = {"SWITCH_HOST": self.sw.host, "SWITCH_USERNAME": "admin", "SWITCH_PASSWORD": "secret"}
        for p in (mock.patch.dict(sys.modules, {"paramiko": fake_paramiko}),
                  mock.patch.object(fake_paramiko, "SWITCH", self.sw),
                  mock.patch("time.time", self.clock.time), mock.patch("time.sleep", self.clock.sleep)):
            p.start()
            self.addCleanup(p.stop)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def step_test(self):
        """Run the step test: (exit code, stdout, stderr)."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            return run_main(fan_step_test.main, [os.path.join(self.dir, "data.jsonl"),
                                                 os.path.join(self.dir, "status.json")])

    def records(self):
        with open(os.path.join(self.dir, "data.jsonl")) as f:
            return [json.loads(l) for l in f]

    def setting(self):
        return self.sw.cooling["coolingMode"], self.sw.cooling["overrideFanSpeed"]

    def configured(self):
        """Every fan-speed line sent, in order."""
        return [l for l in self.sw.sent if "fan-speed" in l]

    def at_minute(self, minute, fn):
        self.clock.at(minute * 60, fn)

    def overrides(self, *pcts):
        return ["environment fan-speed override %d" % p for p in pcts]

    def test_runs_the_schedule_then_hands_the_fans_to_eos(self):
        seen = []
        self.at_minute(10, lambda: seen.append(self.status()))
        code, out, _err = self.step_test()
        self.assertEqual(code, 0)
        self.assertEqual(self.setting(), ("automatic", None))
        self.assertEqual(self.configured(), self.overrides(40, 70, 55, 45, 35, 30, 40)
                         + ["environment fan-speed auto"])
        recs = self.records()
        self.assertEqual(recs[0]["before"], "override 40")
        polls = [r for r in recs if r["event"] == "poll"]
        self.assertEqual(len(polls), 170 * 4)
        self.assertTrue(all(p["mode"] == "manual" and p["override"] == p["pct"] for p in polls))
        self.assertEqual(recs[-1]["note"], "finished; fans handed back to EOS (automatic fan control); "
                                           "before the test they were at override 40")
        self.assertIn(recs[-1]["note"], out)
        self.assertEqual(seen[0], {"step": 0, "pct": 40, "ambient": 25.25, "worst": [-6.625, "Front-panel temp sensor"],
                                   "fault": [], "hot": [], "time": self.clock.start + 585, "elapsed_min": 9.8,
                                   "current": 40, "note": "running"})
        self.assertEqual(self.status(), {"step": None, "pct": None, "done": True, "time": self.clock.start + 10200,
                                         "elapsed_min": 170.0, "current": None, "note": recs[-1]["note"]})

    def status(self):
        with open(os.path.join(self.dir, "status.json")) as f:
            return json.load(f)

    def test_runs_on_at_the_limits(self):
        def limits():
            self.sw.sensor("TempSensor18").update(currentTemperature=68.0)          # target + 3
            self.sw.sensor("TempSensor4").update(currentTemperature=45.0, overheatThreshold=60.0)   # alert - 15
            self.sw.sensor("TempSensor2").update(setPointTemperature=None)
            self.sw.sensor("TempSensor3").update(currentTemperature=None)
            self.sw.cooling.update(ambientTemperature=30.0)
        self.at_minute(5, limits)
        code, out, _err = self.step_test()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("finished; fans handed back to EOS"))
        self.assertEqual(self.status()["note"], out.rstrip("\n"))

    def test_polls_every_15_s_however_long_the_switch_takes_to_answer(self):
        """A poll is three commands. Each records how long it took; the next starts
        15 s after it began, or at once if it took longer."""
        for answer_s, apart in ((1, 15.0), (6, 18.0)):
            with self.subTest(answer_s=answer_s):
                self.setUp()
                answer = self.sw.answer

                def slow(cmd, mode, answer=answer, answer_s=answer_s):
                    self.clock.now += answer_s
                    return answer(cmd, mode)
                self.sw.answer = slow
                code, _out, _err = self.step_test()
                self.assertEqual(code, 0)
                polls = [r for r in self.records() if r["event"] == "poll"]
                self.assertEqual({p["poll_s"] for p in polls}, {3.0 * answer_s})
                first = [p["t"] for p in polls if p["step"] == 0]
                self.assertEqual({round(b - a, 2) for a, b in zip(first, first[1:])}, {apart})

    def test_a_warm_inlet_stops_the_test(self):
        self.at_minute(5, lambda: self.sw.cooling.update(ambientTemperature=30.5))
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertEqual(self.setting(), ("automatic", None))
        self.assertTrue(out.startswith("STOP (inlet 30.5 C > 30); fans handed back to EOS"))

    def test_an_equipment_fault_hands_the_fans_to_eos(self):
        faults = [("fan 2/1 failed", lambda: self.sw.cooling["fanTraySlots"][1]["fans"][0].update(status="failed")),
                  ("PSU 1 powerLoss", lambda: self.sw.power["powerSupplies"]["1"].update(state="powerLoss")),
                  ("cooling status coolingKo", lambda: self.sw.cooling.update(systemStatus="coolingKo")),
                  ("temperature status temperatureKo", lambda: self.sw.temp.update(systemStatus="temperatureKo")),
                  ("Phy17-20 failed", lambda: self.sw.sensor("TempSensor18").update(hwStatus="failed"))]
        for why, fault in faults:
            with self.subTest(why):
                self.setUp()
                self.at_minute(20, fault)
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), ("automatic", None))
                self.assertEqual(self.configured(), self.overrides(40, 70) + ["environment fan-speed auto"])
                self.assertIn("ABORT (fault): %s; fans handed back to EOS" % why, out)
                self.assertLessEqual(self.records()[-4]["t"], 20 * 60 + 15)   # last poll; abort, auto, end

    def test_too_warm_hands_the_fans_to_eos(self):
        cases = [(5, 40, [], "TempSensor18", dict(currentTemperature=68.5), "Phy17-20 68.5 C (target 65.0, alert 100.0)"),
                 (120, 30, [40, 70, 55, 45, 35], "TempSensor18", dict(currentTemperature=70.0),   # 5 min into 30 %
                  "Phy17-20 70.0 C (target 65.0, alert 100.0)"),
                 (5, 40, [], "TempSensor4", dict(currentTemperature=45.5, overheatThreshold=60.0),
                  "Board sensor 45.5 C (target 45.5, alert 60.0)")]
        for minute, pct, done, name, change, shown in cases:
            with self.subTest(shown):
                self.setUp()
                self.at_minute(minute, lambda: self.sw.sensor(name).update(change))
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), ("automatic", None))
                self.assertEqual(self.configured(), self.overrides(*(done + [pct])) + ["environment fan-speed auto"])
                self.assertIn("ABORT (too warm at %d%%): %s; fans handed back" % (pct, shown), out)

    def test_ctrl_c_hands_the_fans_to_eos(self):
        self.at_minute(30, lambda: os.kill(os.getpid(), signal.SIGINT))
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertEqual(self.setting(), ("automatic", None))
        self.assertEqual(self.configured(), self.overrides(40, 70) + ["environment fan-speed auto"])
        self.assertIn("stopped (signal %d); fans handed back to EOS" % signal.SIGINT, out)

    def test_ctrl_c_while_the_switch_answers_hands_the_fans_to_eos(self):
        """Ctrl-C during a poll or while waiting to see an override take stops the
        test at once: it is not a failed command to retry."""
        for when, at in (("a poll", lambda cmd: cmd.endswith("power | json")
                          and self.clock.now >= self.clock.start + 30 * 60),
                         ("a setting", lambda cmd: cmd.endswith("cooling | json")
                          and self.sw.cooling["overrideFanSpeed"] == 70)):
            with self.subTest(when):
                self.setUp()
                answer = self.sw.answer

                def ctrl_c(cmd, mode, answer=answer, at=at):
                    if at(cmd):
                        os.kill(os.getpid(), signal.SIGINT)
                    return answer(cmd, mode)
                self.sw.answer = ctrl_c
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), ("automatic", None))
                self.assertEqual(self.configured(), self.overrides(40, 70) + ["environment fan-speed auto"])
                self.assertIn("stopped (signal %d); fans handed back to EOS" % signal.SIGINT, out)

    def test_stops_when_something_else_sets_the_fans(self):
        for name, mode, override in (("the guard", "automatic", None), ("fanctl", "manual", 35)):
            with self.subTest(name):
                self.setUp()
                self.at_minute(5, lambda: self.sw.cooling.update(coolingMode=mode, overrideFanSpeed=override))
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), ("automatic", None))
                self.assertEqual(self.configured(), self.overrides(40) + ["environment fan-speed auto"])
                self.assertIn("ABORT (fault): the fans are at %s, not override 40 (is fanctl, fanctl-guard or "
                              "someone else setting them?)" % ("automatic fan control" if override is None
                                                               else "override 35"), out)
                polls = [r for r in self.records() if r["event"] == "poll"]
                self.assertEqual(len(polls), 5 * 4 + 1)

    def test_no_inlet_reading_hands_the_fans_to_eos(self):
        self.at_minute(5, lambda: self.sw.cooling.update(ambientTemperature=None))
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertEqual(self.setting(), ("automatic", None))
        self.assertIn("ABORT (fault): no inlet reading; fans handed back to EOS", out)

    def test_an_override_eos_rejects_or_does_not_apply_ends_the_test_saying_so(self):
        line = "environment fan-speed override 70"
        for how, why in (("refuse", "EOS rejected '%s': %% Invalid input" % line),
                         ("ignore", "'%s' did not show in cooling status" % line)):
            with self.subTest(how):
                self.setUp()
                setattr(self.sw, how, {line})
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), ("automatic", None))
                self.assertEqual(self.configured(), self.overrides(40, 70) + ["environment fan-speed auto"])
                self.assertEqual(out, "error: %s; fans handed back to EOS (automatic fan control); before the test "
                                      "they were at override 40\n" % why)

    def test_falls_back_to_the_top_of_its_range_if_eos_refuses_auto(self):
        self.sw.refuse = {"environment fan-speed auto", "no environment fan-speed"}
        self.at_minute(5, lambda: self.sw.cooling["fanTraySlots"][1]["fans"][0].update(status="failed"))
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertEqual(self.setting(), ("manual", 70))
        self.assertEqual(self.configured(), self.overrides(40) + [
            "environment fan-speed auto", "no environment fan-speed"] + self.overrides(70))
        self.assertIn("; EOS refused automatic fan control, so the fans are left at override 70: "
                      "run 'environment fan-speed auto' by hand", out)

    def test_goes_on_to_the_next_way_back_if_eos_takes_auto_without_effect(self):
        """EOS accepting a line that doesn't change the fans is no better than EOS
        rejecting it: the test tries the next way back, never leaving its last override."""
        auto, no = "environment fan-speed auto", "no environment fan-speed"
        for ignored, setting, end in (({auto}, ("automatic", None), "fans handed back to EOS (automatic fan control)"),
                                      ({auto, no}, ("manual", 70), "EOS refused automatic fan control, so the fans "
                                                                   "are left at override 70")):
            with self.subTest(sorted(ignored)):
                self.setUp()
                self.sw.ignore = ignored
                self.at_minute(120, lambda: self.sw.sensor("TempSensor18").update(currentTemperature=70.0))
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(self.setting(), setting)
                self.assertEqual(self.configured(), self.overrides(40, 70, 55, 45, 35, 30) + [auto, no]
                                 + (self.overrides(70) if no in ignored else []))
                self.assertIn("ABORT (too warm at 30%%): Phy17-20 70.0 C (target 65.0, alert 100.0); %s" % end, out)

    def test_sends_an_override_again_after_a_dropped_session(self):
        line = "environment fan-speed override 70"
        self.at_minute(14.9, lambda: self.sw.drop.add(line))
        self.at_minute(15.05, lambda: self.sw.drop.discard(line))
        code, _out, _err = self.step_test()
        self.assertEqual(code, 0)
        self.assertEqual(self.configured(), self.overrides(40, 70, 70, 55, 45, 35, 30, 40)
                         + ["environment fan-speed auto"])
        self.assertEqual([r["line"] for r in self.records() if r["event"] == "set-error"], [line])
        self.assertEqual(self.sw.logins, 2)

    def test_rides_out_a_short_outage(self):
        self.at_minute(30, lambda: setattr(self.sw, "down", True))
        self.at_minute(30.6, lambda: setattr(self.sw, "down", False))
        code, _out, _err = self.step_test()
        self.assertEqual(code, 0)
        self.assertEqual(self.setting(), ("automatic", None))
        self.assertEqual(len([r for r in self.records() if r["event"] == "poll-error"]), 3)

    def test_a_switch_unreadable_4_times_ends_the_test(self):
        for back, end in ((31, "fans handed back to EOS (automatic fan control)"),
                          (None, "COULD NOT hand the fans back to EOS: could not send 'environment fan-speed auto'")):
            with self.subTest(back):
                self.setUp()
                self.at_minute(30, lambda: setattr(self.sw, "down", True))
                if back:
                    self.at_minute(back, lambda: setattr(self.sw, "down", False))
                code, out, _err = self.step_test()
                self.assertEqual(code, 1)
                self.assertEqual(out, "error: switch unreadable 4 times in a row; %s; before the test they were "
                                      "at override 40\n" % end)
                self.assertEqual(self.setting(), ("automatic", None) if back else ("manual", 70))

    def test_a_finished_test_fails_if_eos_keeps_the_override(self):
        self.sw.refuse = {"environment fan-speed auto", "no environment fan-speed"}
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertEqual(self.setting(), ("manual", 70))
        self.assertTrue(out.startswith("finished; EOS refused automatic fan control"))

    def test_a_finished_test_fails_if_the_switch_is_lost_at_the_end(self):
        self.at_minute(169.9, lambda: setattr(self.sw, "down", True))
        code, out, _err = self.step_test()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("finished; COULD NOT hand the fans back to EOS: could not send "
                                       "'environment fan-speed auto'"))

    def test_refuses_to_start_while_fanctl_or_its_guard_runs(self):
        for name in ("fanctl", "fanctl-guard"):
            with self.subTest(name):
                self.setUp()
                self.sw.daemons[name] = "running with PID 4242"
                code, out, err = self.step_test()
                self.assertEqual(code, 1)
                self.assertIn("not starting: %s is running. Stop it first (daemon %s / shutdown), and "
                              "start it again after the test" % (name, name), err)
                self.assertNotIn("configure", self.sw.sent)
                self.assertEqual(self.setting(), ("manual", 40))

    def test_refuses_a_schedule_outside_its_range(self):
        with mock.patch.object(fan_step_test, "SCHEDULE", [(40, 60), (75, 60)]):
            code, _out, err = self.step_test()
        self.assertEqual(code, 1)
        self.assertIn("not starting: the schedule's speeds must be 30..70 %, not 75", err)
        self.assertEqual(self.sw.sent, [])

    def test_without_two_files_prints_the_usage_and_touches_nothing(self):
        data, status = os.path.join(self.dir, "data.jsonl"), os.path.join(self.dir, "status.json")
        for argv in ([], [data], [data, status, "extra"]):
            with mock.patch.dict(os.environ, self.env, clear=True):
                code, _out, err = run_main(fan_step_test.main, argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("Fan step-response test for an Arista EOS switch", err)
            self.assertEqual((self.sw.logins, self.sw.sent), (0, []))
        self.assertFalse(os.path.exists(data))

    def test_runs_as_a_script(self):
        script = fan_step_test.__file__
        argv = [script, os.path.join(self.dir, "data.jsonl"), os.path.join(self.dir, "status.json")]
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, "argv", argv):
            code, out, _err = run_main(lambda _argv: runpy.run_path(script, run_name="__main__"), [])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("finished; fans handed back to EOS"))
        self.assertEqual(self.setting(), ("automatic", None))


if __name__ == "__main__":
    unittest.main()

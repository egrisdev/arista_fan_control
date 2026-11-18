"""Tests for fanctl. Run from the repo root:  python3 -m unittest discover -s tests -v"""

import contextlib
import fcntl
import functools
import gc
import hashlib
import importlib.util
import io
import json
import math
import os
import platform
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fake_fastcli  # noqa: E402
import fanctl  # noqa: E402
import sim  # noqa: E402

HOUR = 3600.0
DAY = 24 * HOUR


@functools.lru_cache(maxsize=None)
def gnu_coreutils():
    """True if stat and readlink take the GNU options fanctl-guard.sh uses."""
    try:
        for cmd in (["stat", "-c", "%u %a", "/"], ["readlink", "-f", "/"]):
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def setUpModule():
    # fanctl and the guard refuse runtime directories that any ancestor leaves
    # open to others, so keep the tests' files under one that passes.
    d = tempfile.mkdtemp(prefix="fanctl-tests-")
    if fanctl.check_dir(d) is not None:              # e.g. TMPDIR inside a group-writable home
        os.rmdir(d)
        d = tempfile.mkdtemp(prefix="fanctl-tests-", dir="/tmp")
    tempfile.tempdir = d


def tearDownModule():
    shutil.rmtree(tempfile.tempdir, ignore_errors=True)
    tempfile.tempdir = None


def modes(s, since=0.0):
    return [m for t, _f, m, _e in s.trace if t >= since]


def find_sensor(sw, desc):
    return [x for x in sw.plant.sensors if x[0]["description"] == desc][0]


def pin_sensor(sw, desc, reading):
    """The sensor, pinned at `reading` whatever the fans do; set its [4] to move it."""
    x = find_sensor(sw, desc)
    x[2], x[4] = 1e9, reading
    return x


def hand_offs(cfg, change, undo, waits):
    """Fans at 40 %; after each wait (s), change(switch) for one poll, then
    undo(switch). Returns how long EOS kept the fans after each hand-off:
    s from its 'auto' to the next override."""
    s = sim.Sim(sim.FakeSwitch(override=40), cfg=cfg).run(60)
    held = []
    for wait in waits:
        s.run(wait)
        start = s.sw.t
        change(s.sw)
        s.run(cfg.poll)
        undo(s.sw)
        s.run(2000)                                 # longer than any hold these tests expect
        lines = s.changes(start)
        auto = [t for t, line in lines if line == "environment fan-speed auto"][0]
        held.append([t for t, line in lines if t > auto and "override" in line][0] - auto)
    return held


def too_hot(sw):
    sw.plant.ambient = 32.0                         # at ambient_max: too hot


def cool_again(sw):
    sw.plant.ambient = 25.25


def fan_fails(sw):
    sw.fan_status["2/1"] = "failed"


def fan_ok(sw):
    sw.fan_status.clear()


def lowerings(s, since=0.0):
    """The polls that set the fans lower than the override before (probes and steps down)."""
    return [t for (_t0, f0, m0, _e0), (t, f, m, _e) in zip(s.trace, s.trace[1:])
            if t >= since and m0 == m == "override" and f < f0]


def gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


def no_readback(sw):
    """From now on nothing can be read back after a change: every cooling or
    running-config read at the time of the last config change fails."""
    real = sw.run

    def run(commands, timeout=None):
        if commands in ([fanctl.COOLING_CMD], [fanctl.RUNNING_CMD]) and sw.config_log \
                and sw.config_log[-1][0] == sw.t:
            raise fanctl.CliError("FastCli timed out after 30s")
        return real(commands, timeout)
    sw.run = run


def daemon(test, sw, where=None, **kw):
    """A Daemon on sw that logs to a string, with its lock, heartbeat and memory
    in directory `where` (by default a new one, removed when test ends), and
    its lock file, for d.run(lock). The stop signals run() takes over are
    restored when test ends. kw goes to Daemon: sleep (which may raise
    Shutdown to stop the daemon), clock, wall."""
    for sig in fanctl.STOP_SIGNALS:
        test.addCleanup(signal.signal, sig, signal.getsignal(sig))
    if where is None:
        where = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, where)
    d = fanctl.Daemon(sim.make_cfg(), sw, fanctl.Log(use_syslog=False, stream=io.StringIO()),
                      state_file=os.path.join(where, "status.json"),
                      memory_file=os.path.join(where, "memory.json"), **kw)
    return d, os.path.join(where, "lock")


def fake_syslog(test):
    """Stand in for the syslog module until `test` ends. Returns it: .opened
    holds openlog()'s arguments and .records each (priority, message) sent."""
    class FakeSyslog(object):
        LOG_PID, LOG_LOCAL4 = 1, 160
        LOG_ERR, LOG_WARNING, LOG_NOTICE, LOG_INFO = 3, 4, 5, 6

        def __init__(self):
            self.opened, self.records = [], []

        def openlog(self, *a):
            self.opened.append(a)

        def syslog(self, prio, msg):
            self.records.append((prio, msg))
    test.addCleanup(setattr, fanctl, "syslog", fanctl.syslog)
    fanctl.syslog = FakeSyslog()
    return fanctl.syslog


def alive(pid):
    """True if process pid exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True                                  # no /proc: it exists


def stat_mode(path):
    return os.stat(path).st_mode & 0o7777


def mkdir(parent, name, mode):
    """parent/name, a new directory with exactly `mode`, whatever the umask."""
    path = os.path.join(parent, name)
    os.mkdir(path)
    os.chmod(path, mode)
    return path


class CapturedSwitch(object):
    """Answers the environment reads with the text captured from the switch, verbatim."""

    def run(self, commands, timeout=None):
        name = {fanctl.TEMP_CMD: "temp.json", fanctl.COOLING_CMD: "cooling.json",
                fanctl.POWER_CMD: "power.json"}[commands[0]]
        with open(os.path.join(sim.FIX, name)) as f:
            return f.read()


class ParsingTests(unittest.TestCase):
    def test_real_switch_json_is_healthy(self):
        r = fanctl.assess(fanctl.read_environment(CapturedSwitch()), sim.make_cfg())
        self.assertEqual(r.faults, [])
        self.assertEqual(r.hot, [])
        self.assertEqual(len(r.sensors), 29)          # 25 system + 2x2 PSU
        self.assertEqual(r.fan_count, 6)              # 4 fan-tray fans + 2 PSU fans
        self.assertEqual(r.setting, ("override", 40))  # coolingMode manual / overrideFanSpeed 40
        board = [s for s in r.sensors if s.name == "TempSensor4"][0]
        self.assertEqual((board.target, board.target_src), (45.5, "setPointTemperature"))
        self.assertEqual(r.worst.name, "TempSensor5")  # Front-panel, 29.1 vs 35.75

    def test_setting_from_cooling(self):
        self.assertEqual(fanctl.setting_from_cooling({"coolingMode": "automatic", "overrideFanSpeed": 0}),
                         fanctl.AUTO)
        self.assertEqual(fanctl.setting_from_cooling({"coolingMode": "manual", "overrideFanSpeed": 30}),
                         ("override", 30))                     # EOS's lowest override
        for unclear in ({}, {"overrideFanSpeed": 40}, {"coolingMode": "manual", "overrideFanSpeed": 0}):
            self.assertIsNone(fanctl.setting_from_cooling(unclear), unclear)

    def test_target_fallbacks(self):
        cfg = sim.make_cfg()
        self.assertEqual(fanctl.pick_target({"targetTemperature": 60}, 90, cfg), (60, "targetTemperature"))
        # Only a plausible temperature (1-150 C, as a reading must be) under the alert limit is a target.
        for setpoint, target in ((0, (65.0, "0.65 x alert")), (0.5, (65.0, "0.65 x alert")),
                                 (1, (1, "setPointTemperature")), (100, (65.0, "0.65 x alert"))):
            self.assertEqual(fanctl.pick_target({"setPointTemperature": setpoint}, 100, cfg), target, setpoint)
        # Both at 60 C, as on the reference switch's CPU sensor: --probe names the first.
        self.assertEqual(fanctl.pick_target({"setPointTemperature": 60, "targetTemperature": 60}, 90, cfg),
                         (60, "setPointTemperature"))
        self.assertEqual(fanctl.pick_target({"setPointTemperature": 50, "targetTemperature": 48}, 75, cfg),
                         (48, "targetTemperature"))
        cfg = sim.make_cfg(target_offset=3.0)
        self.assertEqual(fanctl.pick_target({"setPointTemperature": 50}, 75, cfg)[0], 53.0)

    def test_parse_json_doc(self):
        self.assertEqual(fanctl.parse_json_doc('\n! note\n{"a": 1}\n'), {"a": 1})
        for bad in ("", "! a comment and nothing else", "Traceback...", '{"a": 1} trailing', "[1, 2]",
                    '{"errors": ["This is an unconverted command"]}'):
            with self.assertRaises(ValueError):
                fanctl.parse_json_doc(bad)

    def test_odd_json_shapes_do_not_crash(self):
        sw = sim.FakeSwitch()
        cool = sw.cooling_doc()
        cool["powerSupplySlots"][0]["fans"] = "garbage"
        cool["fanTraySlots"][1]["fans"] = None
        r = fanctl.assess([sw.temp_doc(), cool, sw.power_doc()], sim.make_cfg())
        self.assertTrue(r.faults)

    def test_each_odd_document_part_is_named_as_a_fault(self):
        sw = sim.FakeSwitch()
        docs = [sw.temp_doc(), sw.cooling_doc(), sw.power_doc()]
        cases = (   # (which document, change, the fault it gives)
            (2, lambda d: d.update(powerSupplies={}), "no power-supply data"),
            (0, lambda d: d.update(systemStatus="temperatureCritical"),
             "temperature status is 'temperatureCritical'"),
            (0, lambda d: d.update(shutdownOnOverheat=False),
             "EOS overheat shutdown is disabled ('environment overheat action'); fanctl will not lower the fans"),
            (0, lambda d: d.update(cardSlots="garbage"), "unexpected cardSlots data"),
            (0, lambda d: d["powerSupplySlots"].append(3), "unexpected powerSupplySlots entry"),
            (0, lambda d: d["powerSupplySlots"][0].update(tempSensors=None), "unexpected PSU1 temperature data"),
            (0, lambda d: d["tempSensors"].append("x"), "unexpected temperature entry"),
            (0, lambda d: d["tempSensors"][0].update(overheatThreshold=0),
             "Cpu temp sensor (TempSensor1) has no usable alert limit (overheatThreshold 0)"),
            (0, lambda d: d["tempSensors"][0].update(overheatThreshold=200),     # no plausible reading reaches it
             "Cpu temp sensor (TempSensor1) has no usable alert limit (overheatThreshold 200)"),
            (0, lambda d: d.update(tempSensors=[]), "no usable system temperature sensors"),
            (1, lambda d: d.update(systemStatus="coolingFailed"), "cooling status is 'coolingFailed'"),
            (1, lambda d: d.update(shutdownOnInsufficientFans=False),
             "EOS insufficient-fans shutdown is disabled; fanctl will not lower the fans"),
            (1, lambda d: d.update(airflowDirection="unknownAirflow"), "airflow direction is 'unknownAirflow'"),
            (1, lambda d: d.update(ambientTemperature=10 ** 400),     # an int no float can hold
             "inlet (ambient) temperature %r is not plausible" % 10 ** 400),
            (1, lambda d: d.pop("fanTraySlots"), "no fan tray data"),
            (1, lambda d: d.update(fanTraySlots=[], powerSupplySlots=[]), "no fan tray data"),   # no fans at all
            (1, lambda d: d["fanTraySlots"].append(None), "unexpected fan tray entry"),
            (1, lambda d: d["fanTraySlots"][0].update(status="failed"), "fan tray 1 status is 'failed'"),
            (1, lambda d: d["powerSupplySlots"][1].update(status="failed"), "PowerSupply2 fan status is 'failed'"),
        )
        for which, change, fault in cases:
            with self.subTest(fault=fault):
                odd = sim.json_copy(docs)
                change(odd[which])
                self.assertEqual(fanctl.assess(odd, sim.make_cfg()).faults, [fault])

    def test_odd_psu_fan_data_is_a_fault(self):
        sw = sim.FakeSwitch()

        def faults(change, cfg=None):
            cool = sw.cooling_doc()
            change(cool)
            return fanctl.assess([sw.temp_doc(), cool, sw.power_doc()], cfg or sim.make_cfg()).faults
        self.assertEqual(faults(lambda c: None), [])
        for change in (lambda c: c.update(powerSupplySlots="garbage"),
                       lambda c: c.update(powerSupplySlots={"1": {}}),
                       lambda c: c["powerSupplySlots"].append("garbage"),
                       lambda c: c["powerSupplySlots"][0].update(fans=[]),
                       lambda c: c["powerSupplySlots"][0].pop("fans"),
                       lambda c: c["powerSupplySlots"][0].update(fans="garbage")):
            self.assertTrue(faults(change))
        self.assertEqual(faults(lambda c: c["powerSupplySlots"][1].update(fans=[]),
                                sim.make_cfg(ignore_psu=["2"])), [])

    def test_config_validation(self):
        args = fanctl.parse_args(["--ambient-max", "28"])       # safer: must not stop the daemon starting
        self.assertEqual(fanctl.check_config(fanctl.make_config(args)), [])
        for bad in (["--floor", "25"], ["--release-excess", "nan"], ["--release-excess", "99"],
                    ["--alert-margin", "0"], ["--ambient-max", "60"], ["--ambient-resume", "40"],
                    ["--keep-below", "inf"], ["--probe-interval", "0"], ["--probe-interval", "100"]):
            cfg = fanctl.make_config(fanctl.parse_args(bad))
            self.assertTrue(fanctl.check_config(cfg), bad)
        for edge, problems in ((["--floor", "70", "--ceiling", "70"], []),
                               (["--probe-margin", "4", "--band", "4"], ["probe_margin must be less than band"]),
                               (["--resume-excess", "-2"], []),
                               (["--resume-excess", "-1"], ["resume_excess must be at most -keep_below"]),
                               (["--ambient-resume", "17"], []),             # 15 C under ambient_max
                               (["--ambient-resume", "32"], []),             # at ambient_max
                               (["--hot-hold", "3600", "--hold-max", "3600"], []),
                               (["--fault-hold", "3600", "--hold-max", "3600"], [])):
            self.assertEqual(fanctl.check_config(fanctl.make_config(fanctl.parse_args(edge))), problems, edge)

    def test_every_tunable_has_a_range(self):
        for key in fanctl.DEFAULTS:
            for value in ("-1000000", "1000000"):
                with self.subTest(option=key, value=value):
                    cfg = fanctl.make_config(fanctl.parse_args(["--" + key.replace("_", "-"), value]))
                    self.assertTrue(fanctl.check_config(cfg))
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            fanctl.parse_args(["--help"])
        self.assertIn("--poll INT            (default 15; 5..120 s)", out.getvalue())
        self.assertIn("--floor INT           (default 30; 30..100 %)", out.getvalue())

    def test_bad_command_line_reaches_syslog(self):
        syslog = fake_syslog(self)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            fanctl.parse_args(["--floor", "35%"])
        self.assertTrue(syslog.records and "bad command line" in syslog.records[0][1], syslog.records)

    def test_log_lines_reach_syslog_as_local4_one_record_per_line(self):
        # local4 is what 'show logging' shows; a traceback must not be one record with newlines in it
        syslog = fake_syslog(self)
        log = fanctl.Log()
        log("err", "internal error:\nTraceback (most recent call last):\n  oops")
        log("notice", "")
        self.assertEqual(syslog.opened, [("fanctl", 1, 160)])
        self.assertEqual(syslog.records, [(3, "internal error:"), (3, "Traceback (most recent call last):"),
                                          (3, "  oops"), (5, "")])

    def test_fanctl_runs_where_python_has_no_syslog(self):
        # Only off the switch (EOS's Python has it): messages then go to stdout and stderr only.
        spec = importlib.util.spec_from_file_location("fanctl_without_syslog", os.path.join(ROOT, "fanctl.py"))
        bare = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"syslog": None}):     # import syslog fails
            spec.loader.exec_module(bare)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(bare.main(["--reconcile", "0"]), 2)
            with self.assertRaises(SystemExit):
                bare.main(["--floor", "35%"])
        self.assertRegex(out.getvalue(), r" ERR invalid settings, not starting: reconcile must be 60\.\.86400 s\n$")
        self.assertIn("argument --floor: invalid int value: '35%'", err.getvalue())

    def test_runtime_paths_with_dotdot_are_refused(self):
        # The directory checks read a path as text; after a symlink, '..'
        # leads elsewhere (v1.3.1).
        real, fanctl.syslog = fanctl.syslog, None
        self.addCleanup(setattr, fanctl, "syslog", real)
        for opt in ("--state-file", "--memory-file", "--lock-file"):
            err = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
                fanctl.parse_args([opt, "/tmp/link/../run/fanctl.x"])
            self.assertIn("%s must not contain '..'" % opt, err.getvalue())
        args = fanctl.parse_args(["--lock-file", "/run/fanctl/..lock"])   # only a whole '..' component
        self.assertEqual(args.lock_file, "/run/fanctl/..lock")

    def test_cli_errors(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake = os.path.join(tmp, "cli")
        with open(fake, "w") as f:          # called as: cli -p 15 -c COMMANDS
            f.write("#!/bin/sh\ncase \"$4\" in\n"
                    "  ok) echo fine ;;\n"
                    "  stderr) echo '% Invalid input' >&2 ;;\n"
                    "  stdout) echo 'banner'; echo '  % Incomplete command' ;;\n"
                    "  fail) echo 'broken' >&2; exit 3 ;;\n"
                    "  300) printf '%0300d' 0 >&2; exit 3 ;;\n"
                    "  301) printf '%0301d' 0 >&2; exit 3 ;;\n"
                    "  wedge) trap '' TERM; sleep 30 ;;\n"
                    "esac\n")
        os.chmod(fake, 0o755)
        cli = fanctl.Cli(fake, 5)
        self.assertEqual(cli.run(["ok"]), "fine\n")
        for cmd, error in (("stderr", "% Invalid input"), ("stdout", "% Incomplete command"),
                           ("fail", "exit status 3: broken"), ("300", "exit status 3: " + "0" * 300),
                           ("301", "exit status 3: " + "0" * 300 + "...")):   # what it said, up to 300 characters
            with self.subTest(cmd=cmd), self.assertRaisesRegex(fanctl.CliError, "^%s$" % re.escape(error)):
                cli.run([cmd])
        start = time.monotonic()
        with self.assertRaisesRegex(fanctl.CliError, "^cli timed out after 1s$"):
            cli.run(["wedge"], timeout=1)               # SIGKILL: SIGTERM is ignored
        self.assertLess(time.monotonic() - start, 10)
        with self.assertRaisesRegex(fanctl.CliError, "^cannot run %s: " % re.escape(fake + "-missing")):
            fanctl.Cli(fake + "-missing", 5).run(["ok"])
        with self.assertRaisesRegex(fanctl.CliError, "^neither FastCli nor Cli was found$"):
            fanctl.Cli(None, 5).run(["ok"])

    def fake_cli(self, script):
        """An executable CLI stand-in running `script` in Python; its pid file."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake, pids = os.path.join(tmp, "cli"), os.path.join(tmp, "pids")
        with open(fake, "w") as f:
            f.write("#!%s\nimport os, sys, time\nPIDS = %r\n%s" % (sys.executable, pids, script))
        os.chmod(fake, 0o755)
        return fake, pids

    def pids(self, path):
        deadline = time.monotonic() + 10
        while not os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.01)
        with open(path) as f:
            return [int(pid) for pid in f.read().split()]

    def test_a_stop_signal_during_a_cli_call_kills_the_call(self):
        # A config command left running could land after fanctl's final 'auto'.
        for sig in fanctl.STOP_SIGNALS:
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
            signal.signal(sig, fanctl.on_stop_signal)
        fake, pids = self.fake_cli("open(PIDS + '.tmp', 'w').write(str(os.getpid()))\n"
                                   "os.rename(PIDS + '.tmp', PIDS)\ntime.sleep(30)\n")

        def stop_once_it_runs():
            self.pids(pids)
            os.kill(os.getpid(), signal.SIGTERM)
        t = threading.Thread(target=stop_once_it_runs)
        t.start()
        self.addCleanup(t.join)
        start = time.monotonic()
        with self.assertRaises(fanctl.Shutdown):
            fanctl.Cli(fake, 60).run(["configure", "environment fan-speed override 30", "end"])
        self.assertLess(time.monotonic() - start, 10)
        self.assertFalse(alive(self.pids(pids)[0]))

    def test_a_stop_signal_while_the_cli_starts_kills_it_once_it_exists(self):
        # Held back while FastCli is being started, so the call is killed, not orphaned.
        for sig in fanctl.STOP_SIGNALS:
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
            signal.signal(sig, fanctl.on_stop_signal)
        fake, _pids = self.fake_cli("time.sleep(30)\n")
        real_popen, started = subprocess.Popen, []

        def popen_then_a_stop(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            os.kill(os.getpid(), signal.SIGTERM)    # before fanctl has the call in hand
            return started[-1]
        with mock.patch.object(subprocess, "Popen", popen_then_a_stop), self.assertRaises(fanctl.Shutdown) as stop:
            fanctl.Cli(fake, 60).run(["configure", "environment fan-speed override 30", "end"])
        self.assertEqual(stop.exception.signum, signal.SIGTERM)
        self.assertFalse(alive(started[0].pid))
        # Nothing is held back after that, nor after a call that could not start:
        # the next stop is at once, and no left-over one aborts the final 'auto'.
        with self.assertRaisesRegex(fanctl.CliError, "^cannot run "):
            fanctl.Cli(fake + "-missing", 5).run(["configure", "environment fan-speed auto", "end"])
        with self.assertRaises(fanctl.Shutdown):
            os.kill(os.getpid(), signal.SIGTERM)

    def test_a_cli_call_whose_helper_keeps_its_output_open_still_times_out(self):
        # fanctl kills the call's process group; a helper that left it holds the
        # output open, and fanctl waits for that at most 5 s more.
        fake, pids = self.fake_cli("if os.fork() == 0:\n    os.setsid()\n"
                                   "    open(PIDS + '.tmp', 'w').write(str(os.getpid()))\n"
                                   "    os.rename(PIDS + '.tmp', PIDS)\n    time.sleep(30)\n    os._exit(0)\n"
                                   "time.sleep(30)\n")
        start = time.monotonic()
        with warnings.catch_warnings():             # about the pipes and the child fanctl gave up on
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaisesRegex(fanctl.CliError, "^cli timed out after 1s$"):
                fanctl.Cli(fake, 60).run([fanctl.TEMP_CMD], timeout=1)
            gc.collect()
        self.assertLess(time.monotonic() - start, 15)
        helper = self.pids(pids)[0]
        self.assertTrue(alive(helper))              # out of fanctl's reach, as the test meant
        os.kill(helper, signal.SIGKILL)


class SteadyStateTests(unittest.TestCase):
    def test_settles_without_hunting(self):
        for amb in (18.0, 22.0, 25.25, 28.0, 30.0):
            s = sim.Sim(sim.FakeSwitch(ambient=amb, override=40)).run(10 * HOUR)
            self.assertLessEqual(len(s.changes(6 * HOUR)), 2, "still changing at %.1f C" % amb)
            self.assertNotIn("auto", modes(s), "released at %.1f C" % amb)
            self.assertLess(s.sw.override, 40, "never came down from 40 %% at %.1f C" % amb)
            worst = max(e for t, _f, _m, e in s.trace if t > 6 * HOUR)
            self.assertLessEqual(worst, -1.0, "too warm at %.1f C" % amb)

    def test_today_on_the_real_switch(self):
        # Inlet 25.25 C, static 40 %: down 2 % per 5 min to 34 %, then slow probes.
        s = sim.Sim(sim.FakeSwitch(ambient=25.25, override=40)).run(1200)
        self.assertEqual(s.sw.override, 34)
        gaps = [b[0] - a[0] for a, b in zip(s.changes(), s.changes()[1:])]
        self.assertTrue(all(g >= 300 for g in gaps), gaps)
        s.run(6 * HOUR)
        self.assertIn(s.sw.override, (30, 32, 34))
        self.assertLessEqual(len(s.changes()), 8)

    def test_a_step_down_needs_the_latest_and_every_poll_of_down_interval_more_than_6_c_under(self):
        # Phy17-20 binds, with full fan authority.
        # Exactly keep_below + band (6 C) under its 65 C target until 285 s, then 7 C under. The poll
        # exactly down_interval ago still counts, so the step down waits for the next poll after 585 s.
        sw = sim.FakeSwitch(override=45)
        phy = pin_sensor(sw, "Phy17-20", 59.0)
        s = sim.Sim(sw).run(300)
        phy[4] = 58.0
        s.run(600)
        self.assertEqual(s.changes()[0], (600, "environment fan-speed override 43"))
        # 10 C under for down_interval, then a latest reading exactly 6 C under: not yet
        sw = sim.FakeSwitch(override=45)
        phy = pin_sensor(sw, "Phy17-20", 55.0)
        s = sim.Sim(sw).run(300)
        phy[4] = 59.0
        s.run(15)
        self.assertEqual(s.changes(), [])
        phy[4] = 58.0
        s.run(15)
        self.assertEqual(s.changes(), [(315, "environment fan-speed override 43")])

    def test_cold_room_reaches_floor(self):
        s = sim.Sim(sim.FakeSwitch(ambient=18.0, override=40)).run(3 * HOUR)
        self.assertEqual(s.sw.override, 30)

    def test_takes_over_from_auto_at_current_speed(self):
        sw = sim.FakeSwitch(ambient=22.0, override=None, startup_override=None)
        s = sim.Sim(sw).run(15)
        self.assertEqual(sw.override, 60)               # EOS auto was running 60 %
        s.run(4 * HOUR)
        self.assertEqual(sw.override, 30)

    def test_fans_come_back_down_after_a_warm_spell(self):
        def run(spike):
            sw = sim.FakeSwitch(ambient=28.0, override=40)
            if spike:
                sw.ambient_fn = lambda t: 31.5 if 2 * HOUR <= t < 2.5 * HOUR else 28.0
            return sim.Sim(sw).run(14 * HOUR).sw.override
        settled = run(False)
        self.assertLess(settled, 40)                    # came down from where it started
        self.assertLessEqual(run(True), settled + 4)

    def test_noisy_sensors_and_a_daily_cycle(self):
        sw = sim.FakeSwitch(ambient=25.0, override=40, jitter=1.0)
        sw.ambient_fn = lambda t: 25.0 + 5.0 * math.sin(2 * math.pi * t / DAY)   # 20..30 C
        s = sim.Sim(sw).run(2 * DAY)
        per_day = len(s.changes(DAY))
        self.assertLessEqual(per_day, 60, "%d config changes/day" % per_day)
        self.assertNotIn("auto", modes(s, DAY))
        self.assertLessEqual(max(e for t, _f, _m, e in s.trace if t > DAY), 0.0)
        fans = [f for t, f, _m, _e in s.trace if t > DAY]
        self.assertLess(sum(fans) / len(fans), 36)      # about 33 %: well under the 40 % it started at

    def test_override_below_floor_is_raised(self):
        sw = sim.FakeSwitch(override=30)
        sim.Sim(sw, cfg=sim.make_cfg(floor=35)).run(15)
        self.assertEqual(sw.override, 35)

    def test_override_above_ceiling_creeps_down(self):
        sw = sim.FakeSwitch(override=90)
        s = sim.Sim(sw).run(10)
        self.assertEqual(sw.override, 90)
        s.run(HOUR)
        self.assertLess(sw.override, 90)


class ThermalSafetyTests(unittest.TestCase):
    def test_inlet_rise_hands_back_to_eos_and_resumes(self):
        sw = sim.FakeSwitch(ambient=25.0, override=40)

        def amb(t):
            if t < HOUR:
                return 25.0
            if t < 2 * HOUR:
                return 25.0 + 10.0 * (t - HOUR) / HOUR    # 25 -> 35 C in an hour
            if t < 3 * HOUR:
                return 35.0
            return 24.0
        sw.ambient_fn = amb
        s = sim.Sim(sw).run(6 * HOUR)
        m = modes(s)
        first_auto = [t for t, _f, mm, _e in s.trace if mm == "auto"][0]
        self.assertLess(first_auto, 2 * HOUR)
        self.assertEqual(modes(s, 2 * HOUR + 600)[:4], ["auto"] * 4)
        self.assertEqual(m[-1], "override")             # took over again once cool
        self.assertIn("handing fan control to EOS", s.logs())

    def test_sensor_over_alert_with_cool_max_hands_back(self):
        # A single threshold on max(all sensors), e.g. >= 62 C, misses this: Front-panel alerts at 55 C.
        sw = sim.FakeSwitch(override=40)
        find_sensor(sw, "Front-panel temp sensor")[1] = 56.0 - 25.25
        sw.plant.settle(40)
        s = sim.Sim(sw).run(30)
        self.assertIsNone(sw.override)
        self.assertIn("Front-panel", s.logs())

    def test_sensor_within_its_alert_margin_hands_back_even_under_target_plus_release_excess(self):
        # Targets moved up (--target-offset) can sit closer to the alert limit than release_excess.
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, cfg=sim.make_cfg(target_offset=10)).run(60)
        front = find_sensor(sw, "Front-panel temp sensor")         # target 45.75 C, alert 55 C
        front[1] = 45.0 - 25.25                                     # 10 C under: not yet within it
        sw.plant.settle(40)
        s.run(15)
        self.assertIsNotNone(sw.override)
        front[1] = 46.0 - 25.25
        sw.plant.settle(40)
        s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("Front-panel temp sensor (TempSensor5) at 46.0 C is within 10 C of its 55 C alert limit",
                      s.logs())
        self.assertNotIn("C over its", s.logs())

    def test_sensor_too_far_over_target_hands_back_before_its_alert_margin(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        front = find_sensor(sw, "Front-panel temp sensor")         # target 35.75 C, alert 55 C
        front[1] = 40.75 - 25.25                                    # 5 C over: not yet too far
        sw.plant.settle(40)
        s.run(15)
        self.assertIsNotNone(sw.override)
        front[1] = 41.5 - 25.25
        sw.plant.settle(40)
        s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("Front-panel temp sensor (TempSensor5) at 41.5 C is 5.8 C over its 35.8 C target", s.logs())
        self.assertNotIn("of its 55 C alert limit", s.logs())

    def test_heat_during_a_fault_hold_drops_the_speed_to_resume_at(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.fan_status["2/1"] = "failed"            # a fault: fanctl would come back at 40 %...
        s.run(15)
        sw.fan_status.clear()
        sw.plant.ambient = 33.0                    # ...but it got too hot while EOS had the fans
        s.run(15)
        sw.plant.ambient = 25.25
        s.run(HOUR)
        self.assertIn("taking fan control -> override 60%", s.logs())   # EOS's speed, not the old 40 %

    def test_at_target_at_the_ceiling_waits_then_hands_back_as_heat(self):
        sw = sim.FakeSwitch(override=70)
        phy = pin_sensor(sw, "Phy17-20", 65.0)          # exactly at its 65 C target, whatever the fans do
        sw.advance(HOUR)                                # fanctl's clock doesn't start at 0
        s = sim.Sim(sw, cfg=sim.make_cfg(fault_hold=1200)).run(60)
        self.assertEqual(sw.override, 70)               # dwell: give the heat sinks a chance
        s.run(15)
        self.assertIsNone(sw.override)                  # ceiling_dwell after the first poll at the ceiling
        self.assertIn(" WARNING handing fan control to EOS (auto) -> auto: fans at the 70% ceiling for 60 s and "
                      "hottest vs target: Phy17-20 (TempSensor18) 65.0 C (target 65.0 C, +0.0 C); alert limit "
                      "100 C\n", s.logs())
        handed = s.changes()[-1][0]
        phy[4] = 60.0
        s.run(HOUR)
        self.assertEqual(s.changes(handed + 1)[0][0], handed + 900)    # hot_hold, though fault_hold is longer

    def test_no_raise_at_the_ceiling(self):
        sw = sim.FakeSwitch(override=70)
        pin_sensor(sw, "Phy17-20", 64.0)                # 1 C under its target: within keep_below
        events = []
        sim.Sim(sw).run(600, on_tick=lambda si: events.append(si.d.last_decision.event))
        self.assertEqual(sw.config_log, [])
        self.assertEqual(set(events), {None})           # no raise decided (nor reported) at any poll

    def test_no_resume_while_eos_runs_above_the_ceiling(self):
        sw = sim.FakeSwitch(ambient=29.5, override=70, auto_floor=90)
        find_sensor(sw, "Phy17-20")[1] = 58.0          # ~66.6 C at 70 %, ~59.8 C at EOS's 90 %
        sw.plant.settle(70)
        s = sim.Sim(sw).run(4 * HOUR)                    # EOS at 90 % keeps it well under target
        self.assertIsNone(sw.override)
        self.assertEqual([line for _t, line in s.changes() if "override" in line], [])
        self.assertIn("above fanctl's 70% ceiling", s.d.last_decision.why[0])

    def test_no_takeover_while_eos_runs_above_the_ceiling(self):
        # EOS may see heat fanctl can't (optics, a skipped sensor): its speed is evidence.
        sw = sim.FakeSwitch(ambient=22.0, override=None, startup_override=None, auto_floor=85)
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sw, out)
        self.assertIn("EOS runs the fans at 85%, above fanctl's 70% ceiling", out.getvalue())
        s = sim.Sim(sw).run(HOUR)
        self.assertEqual(sw.config_log, [])
        self.assertIn("EOS runs the fans at 85%, above fanctl's 70% ceiling", s.d.last_decision.why[0])
        sw.auto_floor = 60                              # EOS no longer needs more than fanctl allows
        s.run(30)
        self.assertEqual(sw.override, 60)

    def test_alert_count_rise_hands_back(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(30)
        sw.plant.temp["tempSensors"][3]["alertCount"] = 1
        s.run(15)
        self.assertIsNone(sw.override)

    def test_a_hot_hand_off_soon_after_taking_over_doubles_the_hold(self):
        # hot_hold; doubled less than flap_window (2 h) after taking over again; back to hot_hold at
        # exactly flap_window after it (a 6975 s wait); doubled again soon after the next take-over
        self.assertEqual(hand_offs(sim.make_cfg(), too_hot, cool_again, (0, 600, 6975, 600)),
                         [900, 1800, 900, 1800])

    def test_hot_and_fault_together_use_the_longer_hold(self):
        def both(sw):
            too_hot(sw)
            fan_fails(sw)

        def neither(sw):
            cool_again(sw)
            fan_ok(sw)
        self.assertEqual(hand_offs(sim.make_cfg(fault_hold=1200), both, neither, (0,)), [1200])

    def eos_keeps_the_fans(self, cfg):
        sw = sim.FakeSwitch(override=None, startup_override=None)
        s = sim.Sim(sw, cfg=cfg).run(120)
        self.assertIsNone(sw.override)
        self.assertEqual(sw.config_log, [])
        with open(s.state_file) as f:
            status = json.load(f)
        self.assertEqual(status["event"], "hold")
        return status["why"]

    def test_eos_keeps_the_fans_until_each_sensor_is_its_weighted_resume_excess_under_target(self):
        why = self.eos_keeps_the_fans(sim.make_cfg(resume_excess=-30))
        # The front panel, with half the fan authority, needs only half of the 30 C: 35.75 - 15 C.
        self.assertRegex(why[0], r"^Front-panel temp sensor \(TempSensor5\) at [\d.]+ C must be at most "
                                 r"20\.8 C \(target -15\.0 C, fan authority 0\.50\) before fanctl takes over$")

    def test_fanctl_takes_over_with_each_resume_condition_exactly_at_its_limit(self):
        def eos(pin=(), **kw):
            sw = sim.FakeSwitch(override=None, startup_override=None, **kw)
            if pin:
                pin_sensor(sw, *pin)
            return sw
        for limit, sw, cfg, speed in (
                ("hottest weighted excess -2 C: resume_excess", eos(pin=("Phy17-20", 63.0)), sim.make_cfg(), 60),
                ("inlet 30 C: ambient_resume", eos(ambient=30.0), sim.make_cfg(), 60),
                ("front panel 27 C under its alert limit: alert_margin + keep_below",
                 eos(pin=("Front-panel temp sensor", 28.0)), sim.make_cfg(alert_margin=23, keep_below=4), 60),
                ("EOS running the fans at 70 %: the ceiling", eos(auto_floor=70), sim.make_cfg(), 70)):
            with self.subTest(limit=limit):
                sim.Sim(sw, cfg=cfg).run(15)
                self.assertEqual(sw.override, speed)

    def test_eos_keeps_the_fans_while_a_sensor_is_near_its_alert_limit(self):
        # 27 C under its alert limit: not a hand-off (alert_margin 25), but
        # within alert_margin + keep_below (29), too close to take over.
        why = self.eos_keeps_the_fans(sim.make_cfg(alert_margin=25, keep_below=4))
        self.assertEqual(why, ["Front-panel temp sensor (TempSensor5) is only 27.0 C below its alert limit"])


class AuthorityTests(unittest.TestCase):
    """Margins weighted by how much the fans can move each sensor (v1.3)."""

    def status_after_a_poll(self, sw):
        """The status file (what --status shows) after one poll."""
        s = sim.Sim(sw).run(15)
        with open(s.state_file) as f:
            st = json.load(f)
        return st, {x["label"]: x["weight"] for x in st["sensors"]}

    PHY17_BINDS = ("binding: Phy17-20 (TempSensor18) 55.0 C (target 65.0 C, -10.0 C, fan authority 1.00); "
                   "alert limit 100 C")

    def test_weights_follow_rise_over_inlet(self):
        st, weights = self.status_after_a_poll(sim.FakeSwitch(override=40))
        # Inlet 25.25 C. Phy17-20 has the largest rise (29.75 C): full authority, and it sets the
        # pace, although the front panel (-6.6 C) and the board (-8.5 C) are closer to their targets.
        self.assertEqual(st["binding"], self.PHY17_BINDS)
        self.assertEqual(weights["Front-panel temp sensor (TempSensor5)"], 0.5)   # 3.85 / 29.75, floored
        self.assertEqual(weights["Board sensor (TempSensor4)"], 0.5)              # 11.75 / 29.75, floored
        self.assertEqual(weights["Phy21-24 (TempSensor19)"], round(28.75 / 29.75, 2))

    def test_a_hot_psu_sensor_does_not_shrink_the_weights(self):
        sw = sim.FakeSwitch(override=40)
        psu = [x for x in sw.plant.sensors if x[0]["name"] == "TempSensorP1/1"][0]
        psu[1] = 70.0 - 25.25                      # a larger rise than any fan-cooled sensor's
        sw.plant.settle(40)
        st, weights = self.status_after_a_poll(sw)
        self.assertEqual(weights["PSU1 Hotspot (TempSensorP1/1)"], 1.0)   # PSU sensors are not weighted
        self.assertEqual(st["binding"], self.PHY17_BINDS)                 # nor do they set the largest rise

    def test_warm_room_does_not_chase_the_front_panel(self):
        old = sim.make_cfg(min_authority=1.0)                  # = v1.2: every sensor alike
        runs = {}
        for name, cfg in (("weighted", sim.make_cfg()), ("unweighted", old)):
            s = sim.Sim(sim.FakeSwitch(ambient=30.5, override=40), cfg=cfg).run(10 * HOUR)
            runs[name] = s
            self.assertNotIn("auto", modes(s))
            self.assertLess(max(e for t, _f, _m, e in s.trace if t > HOUR), 0.0)   # never over target
        self.assertLessEqual(runs["weighted"].sw.override, runs["unweighted"].sw.override - 10)

    def test_min_authority_floor_sets_the_weak_sensor_margin(self):
        # A cool Phy plant makes the front panel the only constraint at a 30.5 C inlet.
        def settle(min_auth):
            sw = sim.FakeSwitch(ambient=30.5, override=50)
            for x in sw.plant.sensors:
                if x[0]["description"].startswith(("Phy", "Trident", "Cpu temp", "Board")):
                    x[1] *= 0.5
            sw.plant.settle(50)
            s = sim.Sim(sw, cfg=sim.make_cfg(min_authority=min_auth)).run(12 * HOUR)
            fp = [x for x in s.d.last_reading.sensors if x.name == "TempSensor5"][0]
            self.assertEqual(s.d.ctrl.binding[0].name, "TempSensor5")
            return fp.excess
        self.assertTrue(-3.0 <= settle(0.5) <= -1.0)        # held 1-3 C under its target
        self.assertGreater(settle(0.1), -1.0)              # a lower floor lets it come closer

    def test_weak_sensor_at_target_still_raises_at_once_and_is_named(self):
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        s = sim.Sim(sw).run(60)
        find_sensor(sw, "Front-panel temp sensor")[1] = 35.8 - 25.25 + 0.3   # reads just over its target
        sw.plant.settle(40)
        s.run(15)
        self.assertEqual(sw.override, 45)
        self.assertIn("raising fans -> override 45%: at target: hottest vs target: Front-panel", s.logs())

    def test_at_target_raise_waits_for_the_fans_unless_it_gets_hotter(self):
        # As after a --target-offset restart: a sensor sits at its target, steady.
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        fp = find_sensor(sw, "Front-panel temp sensor")
        fp[1] = 35.8 - 25.25 + 0.3                       # reads just over its target
        sw.plant.settle(40)
        s = sim.Sim(sw).run(15)
        self.assertEqual(sw.override, 45)                # the first raise is immediate
        s.run(45)                                        # still at target, not hotter: no raise per poll
        self.assertEqual(sw.override, 45)
        fp[4] += 1.0                                     # hotter than at the last raise
        s.run(15)
        self.assertEqual(sw.override, 50)                # raises again at once

    def test_at_target_raise_waits_after_a_whole_degree_step_onto_target(self):
        # A PHY sensor (whole degrees) reaches its target by a 1 C step and
        # stays there. Its moving average keeps rising for a few polls behind
        # the flat reading; that is not "hotter".
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        phy = pin_sensor(sw, "Phy17-20", 64.0)           # 1 C under its 65 C target
        s = sim.Sim(sw).run(120)
        before = sw.override
        phy[4] = 65.0
        s.run(15)
        self.assertEqual(sw.override, before + 5)        # reaching the target raises at once
        s.run(45)                                        # flat at target: the fans get up_interval
        self.assertEqual(sw.override, before + 5)
        s.run(15)
        self.assertEqual(sw.override, before + 10)

    def test_at_target_raise_repeats_at_once_while_a_load_builds(self):
        # The PHY sensor climbs 2 C a poll onto its target, then sits there.
        # A climb of more than 1 C within the smoothing window isn't rounding:
        # the heat is still building, so the fans don't wait until it is gone.
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        phy = pin_sensor(sw, "Phy17-20", 61.0)
        s = sim.Sim(sw).run(120)
        self.assertEqual(sw.override, 40)
        speeds = []
        for reading in (63.0, 65.0, 65.0, 65.0, 65.0):
            phy[4] = reading
            s.run(15)
            speeds.append(sw.override)
        # at 65 the window holds 61 61 63 65, 61 63 65 65, 63 65 65 65, then 65 65 65 65
        self.assertEqual(speeds, [40, 45, 50, 55, 55])

    def test_at_target_raise_repeats_at_once_after_a_late_climb_in_the_window(self):
        # Raised at 67 C, back down to 64 C, then 65, 66: the latest reading is 2 C above the
        # oldest in the smoothing window, a load building again, though not hotter than at that raise.
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        phy = pin_sensor(sw, "Phy17-20", 60.0)
        s = sim.Sim(sw, cfg=sim.make_cfg(up_interval=120)).run(120)
        speeds = []
        for reading in (67.0, 64.0, 64.0, 64.0, 64.0, 65.0, 66.0):
            phy[4] = reading
            s.run(15)
            speeds.append(sw.override)
        self.assertEqual(speeds, [45, 45, 45, 45, 45, 45, 50])

    def test_at_target_raise_still_repeats_every_up_interval(self):
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        fp = find_sensor(sw, "Front-panel temp sensor")
        fp[1] = 12.8                                     # over target at 40 %, and still at 50 %
        sw.plant.settle(40)
        s = sim.Sim(sw).run(15)
        self.assertEqual(sw.override, 45)
        s.run(60)
        self.assertEqual(sw.override, 50)

    P = 600                                                  # probe_interval for the probe tests

    def probe_switch(self, hot_below=45, reading=61.0):
        """Fans at 45 %, and Phy17-20, which binds, at `reading` (61 C: 4 C
        under its 65 C target, inside the band, so only probes lower the
        fans). Below hot_below % it reads its target instead, so a probe from
        there is undone by a raise at the next poll. Returns the Sim, the
        on_tick function that does this, and the plant's settings to change."""
        sw = sim.FakeSwitch(ambient=25.25, override=45)
        phy = pin_sensor(sw, "Phy17-20", reading)            # follows only the test, not the fans
        plant = {"hot_below": hot_below, "reading": reading}

        def on_tick(si):
            low = si.sw.override is not None and si.sw.override < plant["hot_below"]
            phy[4] = 65.0 if low else plant["reading"]
        cfg = sim.make_cfg(up_step=2, probe_interval=self.P, hold_max=8 * self.P)   # back-off at most 8x
        return sim.Sim(sw, cfg=cfg), on_tick, plant

    def test_undone_probes_back_off_and_probes_that_hold_come_sooner_again(self):
        s, on_tick, plant = self.probe_switch()
        s.run(14000, on_tick=on_tick)
        # Each probe to 43 % is undone at the next poll; the wait before the next one (from that
        # raise) doubles each time, up to hold_max.
        self.assertEqual(gaps(lowerings(s)), [b * self.P + 15 for b in (2, 4, 8, 8)])
        restart = s.sw.t
        s.kill().start()                                     # SIGKILL and a restart keep the back-off
        plant["hot_below"] = 35                              # from now on a probe holds
        s.run(10000, on_tick=on_tick)
        held = lowerings(s, restart)
        self.assertEqual(held[0] - restart, 8 * self.P)
        self.assertEqual(gaps(held)[:3], [4 * self.P, 2 * self.P, self.P])   # each probe that held halves it

    def test_a_step_down_undone_by_a_raise_does_not_back_off_the_probes(self):
        s, on_tick, plant = self.probe_switch(reading=58.0)  # 7 C under: a step down after down_interval
        s.run(300, on_tick=on_tick)
        plant["reading"] = 61.0                              # then only probes
        s.run(1200, on_tick=on_tick)
        # down, raised at once, and the next probe after one probe_interval, not two
        self.assertEqual([(t, line[-2:]) for t, line in s.changes()][:3], [(300, "43"), (315, "45"), (915, "43")])

    def test_a_restored_back_off_is_never_below_the_normal_wait(self):
        s, on_tick, _plant = self.probe_switch(hot_below=0)
        with open(s.memory_file, "w") as f:
            json.dump({"wall": 1.7e9 + s.sw.t, "backoff": 0}, f)
        s.start().run(2 * self.P, on_tick=on_tick)
        self.assertEqual(lowerings(s), [self.P])

    def test_probe_reports_a_weighted_resume_blocker(self):
        sw = sim.FakeSwitch(ambient=31.0, override=None, startup_override=None)
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sw, out)
        self.assertIn("leave the fans under EOS automatic control for now", out.getvalue())

    def test_measured_plant_warm_spell(self):
        spec = importlib.util.spec_from_file_location("bench", os.path.join(ROOT, "bench", "bench.py"))
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        m = bench.run_one("band", bench.MODEL, "measured", "warmspell", 0, {})
        self.assertEqual(m["min_over_target"], 0.0)
        self.assertLessEqual(m["mean_fan"], 38.0)               # v1.2 band: 42.3 %


class FaultTests(unittest.TestCase):
    def run_with(self, inject, cfg=None, before=60, after=30):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, cfg=cfg).run(before)
        inject(sw)
        s.run(after)
        return s, sw

    def test_fan_failure_releases_at_once(self):
        s, sw = self.run_with(lambda sw: sw.fan_status.update({"2/1": "failed"}), after=15)
        self.assertIsNone(sw.override)
        self.assertIn("fan 2/1 status is 'failed'", s.logs())

    def test_fault_hold_then_resume_at_the_previous_speed(self):
        s, sw = self.run_with(lambda sw: sw.fan_status.update({"2/1": "failed"}), after=60)
        sw.fan_status.clear()
        s.run(300)
        self.assertIsNone(sw.override)           # still holding (fault_hold 600 s)
        s.run(600)
        lines = [line for _t, line in s.changes()]
        self.assertEqual(lines[:2], ["environment fan-speed auto", "environment fan-speed override 40"])
        # resumed at its old 40 %, not at EOS's 60 %

    def test_resumes_at_the_previous_speed_only_if_the_hottest_and_the_inlet_are_at_most_a_little_warmer(self):
        # within 0.5 C and 1 C of where they were before the fault hand-off
        for warmer, inlet_warmer, resumed_at in ((0.5, 0.0, 40), (0.625, 0.0, 60), (0.0, 1.0, 40)):
            with self.subTest(warmer=warmer, inlet_warmer=inlet_warmer):
                sw = sim.FakeSwitch(override=40)
                fp = pin_sensor(sw, "Front-panel temp sensor", 30.0)   # the hottest: 5.75 C under its 35.75 C target
                s = sim.Sim(sw).run(60)
                fan_fails(sw)
                s.run(15)
                fan_ok(sw)
                fp[4] += warmer
                sw.plant.ambient += inlet_warmer
                s.run(HOUR)
                self.assertIn("taking fan control -> override %d%%" % resumed_at, s.logs())

    def test_a_zero_hold_holds_for_no_time(self):
        for cfg, change, undo in ((sim.make_cfg(fault_hold=0), fan_fails, fan_ok),
                                  (sim.make_cfg(hot_hold=0), too_hot, cool_again)):
            with self.subTest(change=change.__name__):
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw, cfg=cfg).run(60)
                change(sw)
                s.run(15)
                with open(s.state_file) as f:
                    st = json.load(f)
                self.assertEqual((st["event"], st["hold_remaining"]), ("release", 0))
                undo(sw)
                s.run(15)
                self.assertIsNotNone(sw.override)  # taken back at the next poll

    def test_an_intermittent_glitch_does_not_churn(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw)
        s.run(2 * HOUR)

        def glitch(si):
            if int(si.sw.t) % 7200 == 0:
                si.sw.fan_status["2/1"] = "failed"
            else:
                si.sw.fan_status.clear()
        s.run(DAY, on_tick=glitch)
        per_day = len(s.changes(2 * HOUR))
        self.assertLessEqual(per_day, 40, "%d config changes/day" % per_day)
        # Each time, fanctl takes the fans back at the floor it had reached, not at 40 % or EOS's 60 %.
        self.assertEqual({line for _t, line in s.changes(2 * HOUR) if "override" in line},
                         {"environment fan-speed override 30"})

    def test_a_fault_right_after_resuming_saves_the_inlet_at_the_resume(self):
        sw = sim.FakeSwitch(ambient=30.0, override=40)
        s = sim.Sim(sw).run(60)
        sw.fan_status["2/1"] = "failed"                # fault hand-off with the inlet at 30 C
        s.run(60)
        sw.fan_status.clear()
        sw.ambient_fn = lambda t: 22.0

        def fault_after_resume(si):
            if si.d.last_decision.event == "resume":
                si.sw.fan_status["2/1"] = "failed"
        s.run(HOUR, on_tick=fault_after_resume)
        self.assertIn("taking fan control -> override 40%", s.logs())   # resumed at 22 C
        with open(s.memory_file) as f:
            m = json.load(f)
        self.assertEqual((m["prev_speed"], m["prev_ambient"]), (40, 22.0))

    def test_repeated_faults_lengthen_the_hold(self):
        self.assertEqual(hand_offs(sim.make_cfg(), fan_fails, fan_ok, (0, 600, 7200)), [600, 1200, 600])

    def test_psu_power_loss_releases(self):
        s, sw = self.run_with(lambda sw: sw.psu_state.update({"2": "powerLoss"}))
        self.assertIsNone(sw.override)
        self.assertIn("PSU 2 state is 'powerLoss'", s.logs())

    def test_ignored_psu_keeps_control_and_skips_its_zero_readings(self):
        s, sw = self.run_with(lambda sw: sw.psu_state.update({"2": "powerLoss"}),
                              cfg=sim.make_cfg(ignore_psu=["2"]))
        self.assertEqual(sw.override, 40)
        self.assertFalse([x for x in s.d.last_reading.sensors if x.name.startswith("TempSensorP2")])

    def test_readings_outside_1_to_150_c_are_not_plausible(self):
        # An unpowered PSU reads 0.0 C: never treat that as cool.
        sw = sim.FakeSwitch(override=40)
        for value, plausible in ((0.0, False), (1.0, True), (150.0, True), (150.125, False)):
            with self.subTest(value=value):
                doc = sw.temp_doc()
                doc["powerSupplySlots"][0]["tempSensors"][0]["currentTemperature"] = value
                r = fanctl.assess([doc, sw.cooling_doc(), sw.power_doc()], sim.make_cfg())
                self.assertEqual(any("not plausible" in f for f in r.faults), not plausible, r.faults)

    def test_failed_sensor_releases_unless_ignored(self):
        s, sw = self.run_with(lambda sw: sw.sensor_hw.update({"Phy9-12": "failed"}))
        self.assertIsNone(sw.override)
        s, sw = self.run_with(lambda sw: sw.sensor_hw.update({"Phy9-12": "failed"}),
                              cfg=sim.make_cfg(ignore_sensor=["TempSensor16"]))
        self.assertEqual(sw.override, 40)

    def test_missing_reading_releases(self):
        s, sw = self.run_with(lambda sw: sw.drop_current.add("Board sensor"))
        self.assertIsNone(sw.override)

    def test_sensor_losing_its_alert_limit_releases(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        del sw.plant.temp["tempSensors"][4]["overheatThreshold"]     # Front-panel
        s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("no longer usable: TempSensor5", s.logs())

    def test_sensor_without_an_alert_limit_releases_unless_ignored(self):
        # A sensor fanctl can't judge is a fault from the first poll, not
        # silently left out; leaving it out is the operator's decision.
        def no_limit():
            sw = sim.FakeSwitch(override=40)
            del sw.plant.temp["tempSensors"][13]["overheatThreshold"]   # Phy1-4
            return sw
        sw = no_limit()
        s = sim.Sim(sw).run(15)
        self.assertIsNone(sw.override)
        self.assertIn("Phy1-4 (TempSensor14) has no usable alert limit", s.logs())
        sw = no_limit()
        s = sim.Sim(sw, cfg=sim.make_cfg(ignore_sensor=["TempSensor14"])).run(60)
        self.assertEqual(sw.override, 40)
        self.assertIn("not using Phy1-4 (TempSensor14): ignored (--ignore-sensor)", s.logs())

    def test_alarms_count_on_a_sensor_without_an_alert_limit(self):
        sw = sim.FakeSwitch(override=40)
        doc = sw.temp_doc()
        raw = doc["tempSensors"][0]                                  # Cpu temp sensor
        del raw["overheatThreshold"]
        raw.update(inAlertState=True, alertCount=3)
        r = fanctl.assess([doc, sw.cooling_doc(), sw.power_doc()], sim.make_cfg(),
                          prev_alert_counts={"TempSensor1": 0})
        self.assertIn("Cpu temp sensor (TempSensor1) is in alert at %.1f C" % raw["currentTemperature"],
                      r.hot)
        self.assertIn("Cpu temp sensor (TempSensor1) alert count rose to 3", r.hot)
        self.assertEqual(r.skipped, [])

    def test_targets_without_a_usable_eos_setpoint_are_flagged(self):
        sw = sim.FakeSwitch(override=40)
        real = sw.temp_doc

        def no_usable_setpoint():
            doc = real()
            phy1, phy5 = doc["tempSensors"][13:15]
            phy1.pop("setPointTemperature")                         # none
            phy1.pop("targetTemperature", None)
            phy5["setPointTemperature"] = phy5["targetTemperature"] = phy5["overheatThreshold"] + 5
            return doc
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=40), out)
        self.assertNotIn("EOS setpoint", out.getvalue())
        sw.temp_doc = no_usable_setpoint
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sw, out)
        warning = ("2 sensor(s) report no usable EOS setpoint (none, or not between 1 C and the alert limit), "
                   "so their target is 0.65 x alert limit, a ratio measured on a DCS-7050TX-64-R: "
                   "Phy1-4 (TempSensor14), Phy5-8 (TempSensor15)")
        self.assertIn("WARNING: " + warning, out.getvalue())
        s = sim.Sim(sw).run(120)
        self.assertEqual(s.logs().count(warning), 1)                # at the first poll, then daily

    def test_missing_inlet_reading_releases(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        real = sw.cooling_doc

        def no_ambient():
            doc = real()
            del doc["ambientTemperature"]
            return doc
        sw.cooling_doc = no_ambient
        s.run(15)
        self.assertIsNone(sw.override)

    def test_temperature_status_not_ok_releases(self):
        s, sw = self.run_with(lambda sw: setattr(sw, "temp_status", "temperatureOverheat"))
        self.assertIsNone(sw.override)

    def test_one_failed_read_is_tolerated_two_are_a_fault(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw)
        sw.fail_reads = 1                          # from the very first poll
        s.run(15)
        self.assertEqual(sw.override, 40)
        self.assertIn(" INFO read failed (1 of 1 allowed); keeping current setting\n", s.logs())
        s.run(45)
        sw.fail_reads = 2
        s.run(30)
        self.assertIsNone(sw.override)
        handed = s.changes()[-1][0]
        s.run(HOUR)
        # A fault, not heat: EOS keeps the fans for fault_hold, then fanctl takes them back at its old speed.
        self.assertEqual(s.changes(handed + 1)[0], (handed + 600, "environment fan-speed override 40"))

    def test_a_bug_while_judging_the_readings_counts_as_a_failed_read(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        with mock.patch.object(fanctl, "assess", side_effect=KeyError("tempSensors")):
            s.run(15)
            self.assertEqual(sw.override, 40)      # one failed read is tolerated
            s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("error while judging switch environment:\nTraceback", s.logs())
        self.assertIn("handing fan control to EOS (auto) -> auto: switch environment unreadable 2 times in a row",
                      s.logs())

    def test_garbage_output_releases(self):
        s, sw = self.run_with(lambda sw: setattr(sw, "garbage", True))
        self.assertIsNone(sw.override)

    def test_rejected_override_falls_back_to_auto_in_the_same_poll_with_the_fault_hold(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(300)
        sw.reject_override = True
        s.run(15)                                  # the step down to 38, rejected
        self.assertEqual(s.changes(), [(300, "environment fan-speed override 38"),
                                       (300, "environment fan-speed auto")])
        self.assertIn(" ERR could not set override 38%: 'environment fan-speed override 38' did not take effect: "
                      "% Error: fan speed override is not supported; fans are override 40%; handing control "
                      "to EOS\n", s.logs())
        self.assertIn(" WARNING handing fan control to EOS (auto) -> auto: could not apply override 38%", s.logs())
        with open(s.state_file) as f:
            self.assertEqual(json.load(f)["hold_remaining"], s.cfg.fault_hold)   # not heat's longer hot_hold

    def test_status_after_a_rejected_override_shows_the_hand_off(self):
        sw = sim.FakeSwitch(override=None, startup_override=None)
        sw.reject_override = True
        s = sim.Sim(sw).run(15)                    # one poll: take over, rejected, hand back
        with open(s.state_file) as f:
            st = json.load(f)
        self.assertEqual((st["event"], st["want"], st["applied"]), ("release", "auto", "auto"))
        self.assertIn("could not apply override 60%", st["why"][0])
        self.assertGreater(st["hold_remaining"], 0)

    def test_refused_auto_means_full_speed_not_a_low_override(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.reject_auto = True
        sw.fan_status["2/1"] = "failed"
        s.run(15)
        self.assertEqual(sw.override, 100)
        self.assertIn("fans set to override 100%", s.logs())
        refused = s.changes()[-1][0]
        s.run(120)
        self.assertEqual(s.changes(refused + 1), [])   # no retry storm
        sw.reject_auto = False                     # EOS takes auto again
        s.run(300)
        self.assertIsNone(sw.override)
        self.assertEqual(s.changes(refused + 1), [(refused + 300, "environment fan-speed auto")])   # asked every 5 min

    def test_unusable_config_cli_is_logged_not_fatal(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.fail_config = True
        sw.fan_status["2/1"] = "failed"
        s.run(60)
        self.assertIn("could not set automatic fan control", s.logs())
        sw.fail_config = False
        s.run(15)
        self.assertIsNone(sw.override)

    def test_a_stop_eos_refuses_auto_for_leaves_the_fans_flat_out(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.reject_auto = True
        s.stop()
        self.assertEqual(sw.override, 100)
        self.assertIn(" ERR fanctl stopped by signal 15: EOS refused automatic fan control; fans left at "
                      "override 100%\n", s.logs())

    def test_a_stop_that_cannot_change_the_fans_says_to_restore_auto_by_hand(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.fail_config = True
        s.stop()
        self.assertEqual(sw.override, 40)
        self.assertRegex(s.logs(), r" ERR fanctl stopped by signal 15 and could NOT restore automatic fan control "
                                   r"\(.*FastCli timed out after 30s.*\)\. Run 'configure / environment fan-speed "
                                   r"auto' by hand\.\n$")

    def test_protection_disabled_means_hands_off(self):
        sw = sim.FakeSwitch(override=None, startup_override=None)
        sw.plant.temp["shutdownOnOverheat"] = False
        s = sim.Sim(sw).run(HOUR)
        self.assertIsNone(sw.override)
        self.assertIn("overheat shutdown is disabled", s.logs())

    def test_sensor_disappearing_releases(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        del sw.plant.temp["tempSensors"][-1]
        sw.plant.sensors = [x for x in sw.plant.sensors if x[0]["description"] != "Phy45-48"]
        s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("no longer usable", s.logs())

    def test_alert_counts_that_stop_coming_or_are_not_integers_are_no_fault(self):
        # EOS's alert counts are used when it gives them as integers. One that stops
        # coming, or comes as anything else (text, true), is no count: no fault, no rise.
        for change in (lambda raw: raw.pop("alertCount"), lambda raw: raw.update(alertCount="1"),
                       lambda raw: raw.update(alertCount=True)):
            with self.subTest():
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(60)
                for raw in sw.plant._all_raw():
                    change(raw)
                s.run(600)
                self.assertEqual(set(modes(s)), {"override"})
                self.assertLess(sw.override, 40)
                self.assertNotIn("error while judging", s.logs())

    def test_a_fan_disappearing_releases(self):
        for vanish in (lambda cool: cool["fanTraySlots"].pop(3),
                       lambda cool: cool["powerSupplySlots"].pop(1),
                       lambda cool: cool.pop("powerSupplySlots")):
            with self.subTest():
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(60)
                vanish(sw.cool)
                s.run(15)
                self.assertIsNone(sw.override)
                self.assertIn("fan count dropped from 6 to", s.logs())


class OperatorTests(unittest.TestCase):
    def test_operator_raise_is_left_alone(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 100                          # someone typed it by hand
        s.run(HOUR)
        self.assertEqual(sw.override, 100)
        self.assertIn("changed outside fanctl", s.logs())
        s.stop()
        self.assertEqual(sw.override, 100)         # and stopping fanctl doesn't undo it

    def test_operator_auto_is_left_alone(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = None
        s.run(HOUR)
        self.assertIsNone(sw.override)

    def test_paused_still_hands_back_to_eos_when_something_breaks(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 35                           # operator chose a low static speed
        s.run(60)
        sw.fan_status["3/1"] = "failed"
        s.run(15)
        self.assertIsNone(sw.override)

    def test_paused_hand_back_is_retried_after_a_failed_write(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 35                           # operator chose a low static speed
        s.run(60)
        sw.fan_status["3/1"] = "failed"
        sw.fail_config = True                      # FastCli times out on the hand-off...
        s.run(15)
        self.assertEqual(sw.override, 35)
        self.assertIn("could not set automatic fan control", s.logs())
        sw.fail_config = False                     # ...and is healthy again by the next poll
        s.run(15)
        self.assertIsNone(sw.override)
        self.assertIn("setting fans -> auto", s.logs().split("could not set automatic")[-1])
        n = len(sw.config_log)
        s.run(HOUR)                                # once EOS has the fans, nothing more is written
        self.assertIsNone(sw.override)
        self.assertEqual(len(sw.config_log), n)

    def test_a_hand_back_still_being_retried_is_not_called_leaving_them_alone(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 35                           # operator chose a low static speed
        s.run(60)
        sw.fan_status["3/1"] = "failed"
        sw.fail_config = True                      # every hand-off write fails for 10 min
        s.run(600)
        self.assertEqual(sw.override, 35)
        logs = s.logs()
        self.assertIn("could not set automatic fan control", logs)
        self.assertIn("fans are override 35% (set outside fanctl) and: fan 3/1 status is 'failed'. "
                      "fanctl is handing them to EOS", logs)
        self.assertNotIn("leaving them alone", logs)


class SettingTrackingTests(unittest.TestCase):
    """Telling someone else's change from a status that lags or can't be read."""

    def status(self, s):
        with open(s.state_file) as f:
            return json.load(f)

    def test_a_status_lagging_behind_fanctls_change_is_not_someone_elses(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(285)                   # next poll (t=300) steps down to 38
        sw.status_shows = ("override", 40)         # the cooling status won't show it
        s.run(120)
        self.assertEqual(sw.override, 38)
        self.assertNotIn("changed outside fanctl", s.logs())
        self.assertFalse(self.status(s)["paused"])
        self.assertEqual(self.status(s)["applied"], "override 38%")   # running-config's word

    def test_running_config_confirming_fanctls_setting_ends_the_doubt(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(315)                   # the poll at t=300 steps down to 38
        self.assertEqual(sw.override, 38)
        sw.status_shows = ("override", 40)         # the cooling status won't show it
        for running_config_fails in (1, 0, 1):     # in doubt, confirmed (a lag), in doubt
            sw.fail_running = running_config_fails
            s.run(15)
        self.assertEqual(sw.override, 38)
        self.assertNotIn("changed outside fanctl", s.logs())
        self.assertFalse(self.status(s)["paused"])

    def test_an_unconfirmed_change_writes_nothing_then_counts_on_the_next_poll(self):
        for before in (300, 0):                    # the poll that would step down to 38; the first poll
            with self.subTest(before=before):
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(before)
                sw.override = 100
                sw.fail_running = 2                # running-config can't confirm it twice
                s.run(15)
                self.assertEqual(sw.config_log, [])    # not 38 over someone's 100
                self.assertIn("cannot read running-config", s.logs())
                self.assertNotIn("changed outside fanctl", s.logs())
                self.assertFalse(self.status(s)["paused"])
                s.run(15)                          # the second poll in doubt: the status is believed
                self.assertIn("fan setting changed outside fanctl (override 40% -> override 100%)", s.logs())
                self.assertTrue(self.status(s)["paused"])
                s.run(HOUR)
                self.assertEqual(sw.override, 100)
                self.assertEqual(sw.config_log, [])

    def test_a_setting_unreadable_at_startup_is_taken_from_the_first_poll(self):
        sw = sim.FakeSwitch(override=40)
        sw.fail_reads, sw.fail_running = 1, 2      # startup can read neither, the first poll
        s = sim.Sim(sw)                            # only the cooling status
        self.assertIn("cannot read the fan setting yet", s.logs())
        self.assertIn("current fan setting: unknown", s.logs())
        s.run(15)
        self.assertEqual(self.status(s)["applied"], "override 40%")
        s.run(270)
        self.assertEqual(sw.config_log, [])        # carried on at 40, nothing written
        self.assertEqual(self.status(s)["applied"], "override 40%")
        s.run(30)
        self.assertEqual(sw.override, 38)          # and manages the fans as usual
        self.assertNotIn("changed outside fanctl", s.logs())

    def test_running_config_is_read_every_reconcile_s_and_not_on_a_poll_that_reads_nothing(self):
        sw = sim.FakeSwitch(override=40)
        read, real = [], sw.run

        def logged(commands, timeout=None):
            if commands == [fanctl.RUNNING_CMD]:
                read.append(sw.t)
            return real(commands, timeout)
        sw.run = logged
        s = sim.Sim(sw).run(1215)
        sw.fail_reads = 1                          # the poll at 1215 s reads nothing
        s.run(HOUR - 1215)
        self.assertEqual(read, [300, 600, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000, 3300])

    def test_running_config_disagreeing_with_the_status_is_logged(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 50
        sw.status_shows = ("override", 40)
        s.run(300)                                 # running-config is checked every 300 s
        self.assertIn("running-config says override 50% but the cooling status says override 40%",
                      s.logs())
        self.assertNotIn("changed outside fanctl", s.logs())

    def test_a_dry_run_notes_someone_elses_change_and_carries_on(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, dry_run=True).run(60)
        sw.override = 100
        s.run(60)
        self.assertNotIn("changed outside fanctl", s.logs())
        self.assertEqual(self.status(s)["applied"], "override 100%")
        self.assertFalse(self.status(s)["paused"])
        self.assertEqual(sw.config_log, [])

    def test_every_combination_of_status_running_config_and_last_set(self):
        a, b, auto = ("override", 40), ("override", 100), fanctl.AUTO
        # (status, running-config, last set or seen, mismatches)
        #     -> (verdict, actual, mismatches, the running-config overruled by the status)
        table = [
            ((None, None, None, 0), ("adopt", None, 0, None)),
            ((a, None, None, 0), ("adopt", a, 0, None)),
            ((None, b, None, 0), ("adopt", b, 0, None)),
            ((a, b, None, 1), ("adopt", a, 1, b)),        # the status first; the count is kept
            ((a, a, None, 1), ("adopt", a, 1, None)),
            ((a, None, a, 1), ("ok", None, 0, None)),
            ((a, b, a, 1), ("ok", None, 0, b)),           # running-config's word is only logged
            ((None, None, a, 1), ("ok", None, 0, None)),  # nothing says otherwise
            ((None, a, a, 1), ("ok", None, 0, None)),
            ((None, b, a, 1), ("changed", b, 0, None)),
            ((b, a, a, 1), ("lag", None, 0, None)),       # running-config confirms: no doubt left
            ((b, b, a, 1), ("changed", b, 0, None)),
            ((b, auto, a, 0), ("changed", auto, 0, None)),  # running-config over the status
            ((b, None, a, 0), ("unsure", None, 1, None)),
            ((b, None, a, 1), ("changed", b, 0, None)),   # the second in doubt: the status
            ((a, None, auto, 0), ("unsure", None, 1, None)),
        ]
        for args, want in table:
            with self.subTest(args=args):
                self.assertEqual(fanctl.judge_setting(*args), want)

    def test_a_second_change_while_paused_is_logged(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 100
        s.run(60)
        sw.override = None
        s.run(60)
        self.assertIn("fan setting changed outside fanctl again: override 100% -> auto", s.logs())
        self.assertIsNone(sw.override)
        self.assertEqual(sw.config_log, [])


class OperatorEmergencyTests(unittest.TestCase):
    def test_emergency_100_during_an_eos_hold_is_left_alone(self):
        sw = sim.FakeSwitch(ambient=31.0, override=None, startup_override=None)
        s = sim.Sim(sw).run(120)                   # inlet > 30 C: fanctl waits in EOS auto
        self.assertIsNone(sw.override)
        sw.override = 100
        s.run(HOUR)
        self.assertEqual(sw.override, 100)

    def test_an_override_at_or_above_the_ceiling_survives_heat_and_faults(self):
        for manual in (70, 100):                   # at the 70 % ceiling, or an emergency 100 %
            with self.subTest(manual=manual):
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(60)
                sw.override = manual
                s.run(60)
                sw.ambient_fn = lambda t: 33.0     # above ambient_max
                sw.fan_status["2/1"] = "failed"
                s.run(600)
                self.assertEqual(sw.override, manual)
                self.assertIn("fanctl is leaving them alone", s.logs())

    def test_manual_change_on_the_poll_fanctl_would_change_speed(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(285)                   # next poll (t=300) would step down to 38
        sw.override = 100
        s.run(60)
        self.assertEqual(sw.override, 100)
        self.assertEqual([l for _t, l in s.changes()], [])

    def test_low_manual_override_is_still_handed_to_eos_when_hot(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.override = 35
        s.run(60)
        sw.ambient_fn = lambda t: 33.0
        s.run(60)
        self.assertIsNone(sw.override)


class VerificationTests(unittest.TestCase):
    def test_slow_status_after_a_change_is_not_a_failure(self):
        sw = sim.FakeSwitch(override=40)
        sw.fail_cooling_after_config = 3           # status reads time out right after each change
        s = sim.Sim(sw).run(2 * HOUR)
        self.assertNotIn(100, [sw.override])
        self.assertNotIn("changed outside fanctl", s.logs())
        self.assertLess(sw.override, 40)

    def test_unconfirmable_change_is_assumed(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        no_readback(sw)
        s.run(HOUR)
        self.assertNotIn("fans set to override 100%", s.logs())
        self.assertIn("could not be confirmed", s.logs())

    def test_an_unconfirmable_change_that_printed_an_error_counts_as_failed(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(300)
        no_readback(sw)
        sw.config_error = "% Error: simulated"
        s.run(15)                                  # the step down to 38
        self.assertIn(" ERR could not set override 38%: 'environment fan-speed override 38' did not take effect: "
                      "% Error: simulated; fans are unknown; handing control to EOS\n", s.logs())
        self.assertNotIn("lowering fans", s.logs())

    def test_a_change_is_confirmed_from_the_cooling_status_while_running_config_cannot_be_read(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(300)
        real, slept = sw.run, []

        def lagging(commands, timeout=None):       # the status shows a change 2 s late
            out = real(commands, timeout)
            if commands[0] == "configure":
                sw.status_shows, sw.fail_running = ("override", 40), 1
            return out

        def sleep(sec):
            slept.append(sec)
            if sum(slept) >= 2:
                sw.status_shows = None
        sw.run, s.d.sleep = lagging, sleep
        s.run(15)                                  # the step down to 38, confirmed within 3 s
        self.assertEqual(sw.override, 38)
        self.assertIn(" NOTICE lowering fans -> override 38%", s.logs())
        self.assertNotIn("handing control to EOS", s.logs())
        sw.run, sw.fail_running = real, 2
        s.stop()                                   # on the way out: one look at the status
        self.assertIsNone(sw.override)
        self.assertTrue(s.logs().endswith(" WARNING fanctl stopped by signal 15: fan control handed back to "
                                          "EOS (auto)\n"), s.logs())
        self.assertNotIn("could not be confirmed", s.logs())

    def test_a_change_that_prints_an_error_but_takes_effect_counts(self):
        sw = sim.FakeSwitch(override=40)
        sw.config_error = "% Warning: simulated"
        s = sim.Sim(sw).run(330)                   # the first step down, at 300 s
        self.assertEqual(sw.override, 38)
        self.assertIn(" NOTICE 'environment fan-speed override 38' printed an error but took effect: "
                      "% Warning: simulated\n", s.logs())
        self.assertIn("lowering fans -> override 38%", s.logs())
        self.assertNotIn("handing control to EOS", s.logs())


class RobustnessTests(unittest.TestCase):
    def test_an_internal_error_hands_the_fans_to_eos(self):
        sw = sim.FakeSwitch(override=40)

        def bug(_s):                                # a bug in the loop, here after the first poll
            raise ZeroDivisionError("a bug")
        d, lock = daemon(self, sw, sleep=bug)
        self.assertEqual(d.run(lock), 1)
        out = d.log.stream.getvalue()
        self.assertIn("internal error:\nTraceback", out)
        self.assertIn("ZeroDivisionError: a bug", out)
        self.assertIn("fanctl internal error: fan control handed back to EOS (auto)", out)
        self.assertIsNone(sw.override)

    def test_a_poll_starts_every_poll_s_however_long_the_last_took(self):
        sw = sim.FakeSwitch(override=40)
        clock, starts = [1000.0], []                # a monotonic clock is far from 0
        took = iter([2.0, 14.5, 20.0, 1.0])         # each poll's FastCli time; the third overruns
        real = sw.run

        def slow(commands, timeout=None):
            if commands == [fanctl.TEMP_CMD]:       # a poll's first read
                if len(starts) == 4:
                    raise fanctl.Shutdown(signal.SIGTERM)
                starts.append(clock[0])
                clock[0] += next(took)
            return real(commands, timeout)

        def sleep(s):
            clock[0] += s
        sw.run = slow
        d, lock = daemon(self, sw, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(d.run(lock), 0)
        self.assertEqual(gaps(starts), [15.0, 15.0, 20.0])

    def test_malformed_memory_is_ignored(self):
        inf, nan = float("inf"), float("nan")      # json.dump writes Infinity / NaN, json.load reads them
        for memory in ({"wall": 1.7e9, "hot_hold": "abc", "hold_left": "x"},
                       {"wall": 1.7e9, "hot_hold": inf},
                       {"wall": 1.7e9, "hot_hold": 10 ** 400},    # an int no float can hold
                       {"wall": 1.7e9, "fault_hold": -inf},
                       {"wall": 1.7e9, "hold_left": inf, "reasons": ["too hot"]},
                       {"wall": 1.7e9, "since_resume": nan},
                       {"wall": nan, "hold_left": 900},
                       {"hold_left": 900, "reasons": ["too hot"]},          # no time: no age either
                       {"wall": 1.7e9, "hold_left": 900, "reasons": "too hot"},
                       {"wall": 1.7e9, "prev_speed": "30", "prev_excess": -6.0, "prev_ambient": 25.0},
                       {"wall": 1.7e9, "backoff": True},
                       [1.7e9, 900]):
            with self.subTest(memory=memory):
                sw = sim.FakeSwitch(override=None, startup_override=None)
                s = sim.Sim(sw)
                with open(s.memory_file, "w") as f:
                    json.dump(memory, f)
                s.start().run(30)
                self.assertEqual(sw.override, 60)  # took over at EOS's speed, as without a memory
                self.assertIn("ignoring unreadable", s.logs())

    def test_a_restored_hold_is_never_longer_than_hold_max(self):
        s = sim.Sim(sim.FakeSwitch(override=None, startup_override=None))
        with open(s.memory_file, "w") as f:
            json.dump({"wall": 1.7e9, "hold_left": 1e300, "reasons": ["too hot"]}, f)
        s.start()
        m = re.search(r"keeping the hand-off hold from before the restart: (\d+) s more", s.logs())
        self.assertTrue(m, s.logs())
        self.assertLessEqual(int(m.group(1)), s.cfg.hold_max)

    def test_clock_stepping_back_keeps_the_hold(self):
        s = sim.Sim(sim.FakeSwitch(override=None, startup_override=None))
        with open(s.memory_file, "w") as f:
            json.dump({"wall": 1.7e9 + 500, "hold_left": 900, "reasons": ["too hot"]}, f)
        s.start().run(30)
        self.assertIsNone(s.sw.override)
        self.assertIn("keeping the hand-off hold", s.logs())

    def test_runtime_files_fanctl_cannot_use_are_reported_hourly_and_the_fans_still_run(self):
        tmp = tempfile.mkdtemp()
        memory, status = os.path.join(tmp, "memory.json"), os.path.join(tmp, "missing", "status.json")
        os.mkdir(memory)                           # where the memory file should be
        s = sim.Sim(sim.FakeSwitch(override=40), state_file=status, tmp=tmp)
        for seconds in (HOUR + 1, HOUR - 30):      # polls at 0, 15, ..., 3600 s, then on to 7170 s
            s.run(seconds)
            self.assertLess(s.sw.override, 40)
            out = s.logs()
            self.assertEqual(out.count(" WARNING cannot read %s: Is a directory\n" % memory), 1, out)
            self.assertEqual(out.count(" WARNING cannot write %s: " % memory), 2, out)   # at 0 and 3600 s
            self.assertEqual(out.count(" WARNING cannot write %s: " % status), 2, out)

    def test_a_log_that_cannot_be_written_does_not_stop_fan_control(self):
        class Full(object):                         # e.g. /var/log full
            def write(self, _text):
                raise OSError("No space left on device")

            def flush(self):
                pass
        closed = io.StringIO()                      # e.g. stdout gone
        closed.close()
        for stream in (Full(), closed):
            with self.subTest(stream=stream):
                sw = sim.FakeSwitch(override=40)
                sim.Sim(sw, log_stream=stream).run(330)    # the first step down, at 300 s
                self.assertEqual(sw.override, 38)

    def test_no_resume_at_the_old_speed_if_the_room_warmed(self):
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        s = sim.Sim(sw).run(60)
        sw.fan_status["2/1"] = "failed"            # a fault hand-off at 40 % and a 25.25 C inlet
        s.run(15)
        sw.fan_status.clear()
        sw.ambient_fn = lambda t: 26.5             # the inlet rises 1.25 C while EOS has the fans;
        s.run(HOUR)                                # at EOS's 60 % no sensor is closer to its target
        # 40 % held only at a cooler inlet: fanctl takes over at EOS's speed instead
        self.assertIn("taking fan control -> override 60%", s.logs())


class RestartTests(unittest.TestCase):
    def test_restart_resumes_where_it_was(self):
        sw = sim.FakeSwitch(ambient=25.25, override=40)
        s = sim.Sim(sw).run(HOUR)
        before = sw.override
        self.assertLess(before, 40)
        s.stop()                                   # e.g. a tuning restart: auto on the way out
        self.assertIsNone(sw.override)
        s.start().run(30)
        self.assertEqual(sw.override, before)      # not an hour back down from EOS's 60 %

    def test_restart_keeps_the_hot_hold(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        front = find_sensor(sw, "Front-panel temp sensor")
        front[1] = 56.0 - 25.25
        sw.plant.settle(40)
        s.run(30)
        self.assertIsNone(sw.override)
        front[1] = 29.1 - 25.25
        sw.plant.settle(60)
        s.stop()
        s.start().run(300)
        self.assertIsNone(sw.override)             # the 15 min hold survived the restart
        self.assertIn("keeping the hand-off hold", s.logs())
        with open(s.state_file) as f:              # ...and still says what it is for
            self.assertEqual(json.load(f)["why"], [
                "holding EOS auto control for 600 s more (after: Front-panel temp sensor (TempSensor5) is in "
                "alert at 55.2 C; Front-panel temp sensor (TempSensor5) at 55.2 C is within 10 C of its 55 C "
                "alert limit; Front-panel temp sensor (TempSensor5) at 55.2 C is 19.5 C over its 35.8 C target)"])
        s.run(HOUR)
        self.assertIsNotNone(sw.override)

    def test_sigkill_keeps_a_hold_extended_since_the_hand_off(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        sw.ambient_fn = lambda t: 33.0             # over ambient_max: hand-off at t=75...
        s.run(300)                                 # ...and the hold is extended every poll
        self.assertIsNone(sw.override)
        hold_end = s.d.ctrl.hold_until             # last poll + hot_hold
        s.kill()                                   # 'daemon fanctl / shutdown'
        sw.ambient_fn = lambda t: 25.25            # cleared by the time fanctl is back
        gap = 60
        sw.advance(gap)
        restart = sw.t
        s.start()
        m = re.search(r"keeping the hand-off hold from before the restart: (\d+) s more", s.logs())
        self.assertTrue(m, s.logs())
        left, poll = int(m.group(1)), s.cfg.poll
        self.assertTrue(s.cfg.hot_hold - gap - 2 * poll <= left <= s.cfg.hot_hold - gap, left)
        s.run(HOUR)
        back = [t for t, line in s.changes(restart) if "override" in line]
        self.assertTrue(back, s.logs())
        self.assertAlmostEqual(back[0], hold_end, delta=poll)   # EOS kept the fans until then

    def test_sigkill_after_hours_without_a_change_resumes_at_the_old_speed(self):
        # Hours at one speed, then 'daemon fanctl / shutdown' and the guard restores auto.
        sw = sim.FakeSwitch(ambient=18.0, override=40)
        s = sim.Sim(sw).run(3 * HOUR)
        self.assertEqual(sw.override, 30)          # at the floor: no more changes
        s.run(s.cfg.hold_max + HOUR)
        s.kill()
        sw.override = None                         # the guard's restore
        sw.advance(60)
        s.start().run(30)
        self.assertEqual(sw.override, 30)          # not EOS's 60 % and an hour back down

    def test_sigkill_keeps_a_hold_extended_while_paused(self):
        # Restarting is how an operator un-pauses fanctl (v1.3.1).
        for manual in (45, None):                  # a low override (fanctl hands it to EOS), or auto
            with self.subTest(manual=manual):
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(60)
                sw.override = manual               # set by hand: fanctl pauses
                s.run(60)
                self.assertIn("changed outside fanctl", s.logs())
                sw.ambient_fn = lambda t: 33.0     # over ambient_max for longer than the hold
                s.run(1200)
                self.assertIsNone(sw.override)
                sw.ambient_fn = lambda t: 25.25
                s.run(60)
                hold_end = s.d.ctrl.hold_until     # extended to the last hot poll + hot_hold
                self.assertIsNotNone(hold_end)
                s.kill()
                sw.advance(15)
                restart = sw.t
                self.assertGreater(hold_end - restart, s.cfg.hot_hold - 120)
                s.start()
                self.assertIn("keeping the hand-off hold", s.logs())
                s.run(HOUR)
                back = [t for t, line in s.changes(restart) if "override" in line]
                self.assertTrue(back, s.logs())
                self.assertAlmostEqual(back[0], hold_end, delta=s.cfg.poll)

    def test_a_hot_hand_off_soon_after_taking_over_doubles_the_hold_across_a_sigkill(self):
        # The flap window counts from the take-over, through the time fanctl was down.
        for up, down, hold in ((600, 60, 1800),    # well within flap_window (2 h)
                               (7000, 300, 900)):  # past it only with the time down
            with self.subTest(up=up, down=down):
                sw = sim.FakeSwitch(override=None, startup_override=None)
                sw.advance(HOUR)                   # fanctl's clock doesn't start at 0
                s = sim.Sim(sw).run(up)            # takes over from EOS at the first poll
                s.kill()
                sw.advance(down)
                s.start().run(30)
                start = sw.t
                too_hot(sw)
                s.run(s.cfg.poll)
                cool_again(sw)
                s.run(2000)
                lines = s.changes(start)
                auto = [t for t, line in lines if line == "environment fan-speed auto"][0]
                self.assertEqual([t for t, line in lines if t > auto and "override" in line][0] - auto, hold)

    def test_a_memory_older_than_hold_max_is_not_used(self):
        # A fault hand-off at 40 %, then fanctl down: the speed it held is worth resuming at only
        # while the memory is no older than hold_max (e.g. not after a day with the switch off).
        # The memory is from the poll at 60 s; fanctl restarts at 75 s + down.
        for down, resumed_at in ((585, 40),        # the fault hold ends as fanctl restarts
                                 (14385, 40),      # exactly hold_max old
                                 (14460, 60)):     # older
            with self.subTest(down=down):
                sw = sim.FakeSwitch(override=40)
                s = sim.Sim(sw).run(60)
                fan_fails(sw)
                s.run(15)
                self.assertIsNone(sw.override)
                fan_ok(sw)
                s.kill()
                sw.advance(down, step=60)
                s.start().run(15)
                self.assertEqual(sw.override, resumed_at)
                self.assertNotIn("keeping the hand-off hold", s.logs())


class OperationsTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, dry_run=True).run(2 * HOUR)
        self.assertEqual(sw.config_log, [])
        self.assertIn(" NOTICE [dry run] would set override 40% (no change)\n", s.logs())   # the first poll
        self.assertIn(" NOTICE [dry run] would set override 38% (binding: Phy17-20 (TempSensor18) ", s.logs())
        self.assertFalse(os.path.exists(s.memory_file))   # must not steer the real daemon later
        s.stop()
        self.assertEqual(sw.config_log, [])
        self.assertIn(" NOTICE dry run stopped by signal 15; fan settings were not touched\n", s.logs())

    def test_status_file(self):
        s = sim.Sim(sim.FakeSwitch(override=40)).run(30)
        lock = os.path.join(s.tmp, "lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            shown = {}
            for age in (15, 300, 301):             # since the last poll's heartbeat, at 15 s
                out = io.StringIO()
                shown[age] = (fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 15 + age, out=out),
                              out.getvalue())
        finally:
            os.close(fd)
        rc, text = shown[15]
        self.assertEqual(rc, 0, text)
        self.assertIn("\nfans: override 40% in effect; fanctl wants override 40%\ninlet/ambient: 25.2 C\n",
                      text)                         # no hold while fanctl has the fans
        self.assertIn("\n  Front-panel temp sensor (TempSensor5)         29.1 C  target  35.8  (-6.6)  alert 55  "
                      "weight 0.50\n", text)
        self.assertEqual(shown[300], (0, text.replace("updated 15 s ago", "updated 300 s ago")))   # as the guard
        self.assertTrue(shown[301][1].startswith("STALE: fanctl is hung (heartbeat 301 s old)."), shown[301])
        self.assertEqual(shown[301][0], 1)
        out = io.StringIO()                         # nobody holds the lock now: dead daemon
        self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 30, out=out), 1)
        self.assertIn("STALE", out.getvalue())
        os.unlink(lock)                             # nor is there a lock file
        out = io.StringIO()
        self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 30, out=out), 1)
        self.assertIn("STALE: fanctl is not running (heartbeat 15 s old)", out.getvalue())

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fanctl.main(list(argv))
        return rc, out.getvalue()

    def test_bad_settings_stop_the_start_in_syslog_for_the_daemon_only(self):
        # --probe and --status answer at the terminal; --no-syslog asks for stdout only.
        syslog = fake_syslog(self)
        refusal = "invalid settings, not starting: reconcile must be 60..86400 s"
        for argv, syslogged in (((), [(3, refusal)]), (("--no-syslog",), []), (("--probe",), []),
                                (("--status",), [])):
            with self.subTest(argv=argv):
                del syslog.records[:]
                rc, out = self.run_main(*argv, "--reconcile", "0")
                self.assertEqual(rc, 2, out)
                self.assertTrue(out.endswith(" ERR %s\n" % refusal), out)
                self.assertEqual(syslog.records, syslogged)

    def test_fanctl_py_runs_as_a_script(self):
        missing = os.path.join(tempfile.mkdtemp(), "status.json")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["fanctl.py", "--status", "--state-file", missing]), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            runpy.run_path(os.path.join(ROOT, "fanctl.py"), run_name="__main__")
        self.assertEqual(stop.exception.code, 1)
        self.assertEqual(out.getvalue(), "fanctl is not running (no %s)\n" % missing)

    def test_probe_off_the_switch_finds_no_cli(self):
        if any(os.path.exists(path) for path in fanctl.CLI_CANDIDATES):
            self.skipTest("this host has an EOS CLI")
        rc, out = self.run_main("--probe")
        self.assertEqual(rc, 1, out)
        self.assertTrue(out.endswith("\n\nERROR: cannot read the switch environment: neither FastCli nor Cli "
                                     "was found\n"), out)

    def test_the_start_logs_the_settings_and_a_startup_config_it_can_read(self):
        sw = sim.FakeSwitch(override=40)           # and 40 in startup-config, which fanctl notes when it can
        self.assertIn(" NOTICE fanctl %s starting: python %s, cli %s; fans 30..70%%, raise within 2 C of EOS "
                      "targets (+0 C), lower when 6 C under; EOS takes over at target +5 C, alert -10 C or inlet "
                      "32 C\n" % (fanctl.VERSION, platform.python_version(), sw.path), sim.Sim(sw).logs())
        self.assertIn(" NOTICE startup-config holds 'environment fan-speed override 40'; after a reload the "
                      "fans run at 40% until fanctl starts\n", sim.Sim(sw).logs())
        sw.fail_startup = True
        s = sim.Sim(sw).run(330)
        self.assertEqual(sw.override, 38)
        self.assertNotIn("startup-config", s.logs())

    def test_status_says_why_it_cannot_show_one(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        status, lock = os.path.join(tmp, "status.json"), os.path.join(tmp, "lock")

        def status_of(write):
            if os.path.isdir(status) and not os.path.islink(status):
                os.rmdir(status)
            elif os.path.lexists(status):
                os.unlink(status)
            write()
            return self.run_main("--status", "--state-file", status, "--lock-file", lock)
        self.assertEqual(status_of(lambda: None), (1, "fanctl is not running (no %s)\n" % status))
        with open(lock, "w"):
            pass
        rc, out = status_of(lambda: os.symlink(lock, status))     # never followed
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("cannot read %s: " % status), out)
        self.assertEqual(status_of(lambda: os.mkdir(status)), (1, "cannot read %s: not a regular file\n" % status))

        for text in ("{", "[]", "42", '{"pid": 1}', '{"time": "1700000000"}', '{"time": NaN}'):
            def garbage():
                with open(status, "w") as f:
                    f.write(text)
            self.assertEqual(status_of(garbage), (1, "cannot parse %s\n" % status), text)

    def test_status_shows_a_hold_and_why(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        status, lock = os.path.join(tmp, "status.json"), os.path.join(tmp, "lock")
        fanctl.write_json(status, {"version": "1.2.3", "pid": 7, "time": time.time(), "dry_run": True,
                                   "paused": True, "applied": "auto", "want": "auto", "hold_remaining": 120,
                                   "ambient": None, "why": ["inlet too warm"]})
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.run_main("--status", "--state-file", status, "--lock-file", lock),
                         (0, "fanctl 1.2.3 pid 7, updated 0 s ago [DRY RUN] [PAUSED: fans set outside fanctl]\n"
                             "fans: auto in effect; fanctl wants auto\n"
                             "holding EOS control for 120 s more\n"
                             "why: inlet too warm\n"))

    def test_status_shows_no_hold_once_it_has_run_out(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw).run(60)
        lock = os.path.join(s.tmp, "lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def status():
            out = io.StringIO()
            self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + sw.t, out=out), 0)
            return out.getvalue()
        sw.ambient_fn = lambda t: 33.0             # above ambient_max: a hand-off and a hot_hold of 900 s
        s.run(60)
        self.assertIsNone(sw.override)
        self.assertIn("\nholding EOS control for 900 s more\n", status())   # from the last hot poll
        sw.ambient_fn = lambda t: 31.0             # below ambient_max, above the 30 C at which fanctl takes over
        s.run(960)
        self.assertIsNone(sw.override)
        self.assertNotIn("holding EOS control", status())
        with open(s.state_file) as f:
            self.assertEqual(json.load(f)["hold_remaining"], 0)
        self.assertIn("why: inlet 31.0 C must be at most 30.0 C before fanctl takes over\n", status())

    def test_probe_matches_what_the_daemon_does(self):
        out = io.StringIO()
        self.assertEqual(fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=40), out), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("fanctl %s probe (read-only): python %s (%s), cli %s\n\n"
                                        % (fanctl.VERSION, platform.python_version(), sys.executable,
                                           sim.FakeSwitch.path)), text)
        self.assertIn("headroom\nFront-panel temp sensor (TempSensor5)          29.1    35.8 (setPointTemperature) "
                      "   -6.6     55     25.9\nBoard sensor", text)     # hottest vs target first
        self.assertIn("\nPSUs: 1=ok, 2=ok\n", text)
        self.assertIn("running-config fan setting: override 40%", text)
        self.assertIn("verdict: fanctl would carry on from the current override 40%.\n", text)
        self.assertIn("binding: Phy17-20", text)
        self.assertRegex(text, r"\n  after 300 s: lowering fans -> override 38%: binding: Phy17-20 .*\n"
                               r"  after 600 s: lowering fans -> override 36%: binding: Phy17-20 ")
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=None, startup_override=None), out)
        self.assertIn("verdict: fanctl would take over at 60% (the speed EOS is running now).", out.getvalue())
        self.assertIn("\n  after 300 s: lowering fans -> override 58%: ", out.getvalue())

    def test_probe_prints_the_lines_the_daemon_logs(self):
        for at_target in (False, True):                         # lowering; raising
            sw = sim.FakeSwitch(override=40)
            if at_target:
                find_sensor(sw, "Phy17-20")[1] += 10.0          # 65.0 C: at its 65 C target
                sw.plant.settle(40)
            sw.plant.advance = lambda dt, fan: None             # the readings stay as they are
            out = io.StringIO()
            fanctl.probe(sim.make_cfg(), sw, out)
            printed = re.findall(r"\n  (?:now|after (\d+) s): (.*)", out.getvalue())
            self.assertEqual(len(printed), 2, out.getvalue())
            s = sim.Sim(sw).run(int(printed[-1][0]) + 1)
            logged = re.findall(r" (?:NOTICE|WARNING) (.* -> .*)", s.logs())
            self.assertEqual(logged, [line for _t, line in printed])

    def test_probe_at_target_raises_now_then_waits_up_interval(self):
        # Since 1.3.2 a repeat raise waits up_interval unless it is getting hotter.
        sw = sim.FakeSwitch(override=40)
        find_sensor(sw, "Phy17-20")[1] += 10.0      # 65.0 C: at its 65 C target
        sw.plant.settle(40)
        for up_interval in (60, 120):
            out = io.StringIO()
            fanctl.probe(sim.make_cfg(up_interval=up_interval), sw, out)
            self.assertRegex(out.getvalue(), r"\n  now: raising fans -> override 45%%: at target: hottest vs target: "
                                             r"Phy17-20 .*\n  after %d s: raising fans -> override 50%%: at target"
                                             % up_interval)

    def test_probe_names_what_stops_it(self):
        sw = sim.FakeSwitch(override=40)
        sw.fan_status = {"2/1": "failed"}
        out = io.StringIO()
        self.assertEqual(fanctl.probe(sim.make_cfg(), sw, out), 0)
        self.assertIn("\nFAULTS (fanctl would hand the fans to EOS and wait):\n  - fan 2/1 status is 'failed'\n",
                      out.getvalue())
        self.assertIn("verdict: fanctl would leave the fans under EOS automatic control until the problems "
                      "above clear.", out.getvalue())
        sw.fail_reads = 1
        out = io.StringIO()
        self.assertEqual(fanctl.probe(sim.make_cfg(), sw, out), 1)
        self.assertIn("ERROR: cannot read the switch environment: FastCli timed out after 30s", out.getvalue())

    def test_probe_lists_the_sensors_left_out_and_a_config_it_cannot_read(self):
        sw = sim.FakeSwitch(override=40)
        sw.fail_running = 1
        out = io.StringIO()
        self.assertEqual(fanctl.probe(sim.make_cfg(ignore_sensor=["Cpu temp sensor"]), sw, out), 0)
        text = out.getvalue()
        self.assertIn("\n%-44s not used: ignored (--ignore-sensor)\n" % "Cpu temp sensor (TempSensor1)", text)
        self.assertIn("\nrunning-config fan setting: unreadable (FastCli timed out after 30s)\n"
                      "startup-config fan setting: override 40%\n", text)
        self.assertIn("verdict: fanctl would carry on from the current override 40%.\n", text)

    def test_probe_at_the_floor_keeps_the_speed(self):
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=30, ambient=18.0), out)   # cool enough to lower
        self.assertRegex(out.getvalue(), r"it would keep the fans at override 30% \(no change within \d+ s\)")

    def test_probe_says_how_far_it_looked_after_its_last_change(self):
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=32, ambient=18.0), out)   # one step to the floor
        self.assertRegex(out.getvalue(), r"\n  after 300 s: lowering fans -> override 30%: .*\n"
                                         r"  \(no other change within 3600 s\)\n$")
        out = io.StringIO()                         # a take-over, then one step to the floor
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=None, startup_override=None, auto_floor=32,
                                                    ambient=18.0), out)
        self.assertRegex(out.getvalue(), r"take over at 32% .*\n(.*\n)*"
                                         r"  after 300 s: lowering fans -> override 30%: .*\n"
                                         r"  \(no other change within 3600 s\)\n$")

    def test_probe_looks_exactly_its_horizon_ahead(self):
        # At the ceiling, exactly at target: a hand-off after ceiling_dwell (60 s), then the
        # take-over at the end of the hot hold. The probe looks 2 x probe_interval (3600 s) ahead.
        for hot_hold, taking_over in ((3540, "  after 3600 s: taking fan control -> override 70%: "),
                                      (3555, "  (no other change within 3600 s)\n")):
            with self.subTest(hot_hold=hot_hold):
                sw = sim.FakeSwitch(override=70)
                pin_sensor(sw, "Phy17-20", 65.0)
                out = io.StringIO()
                fanctl.probe(sim.make_cfg(keep_below=0, resume_excess=0, hot_hold=hot_hold), sw, out)
                self.assertRegex(out.getvalue(), r"\n  after 60 s: handing fan control to EOS \(auto\) -> auto: "
                                                 r"fans at the 70% ceiling for 60 s and .*\n" + re.escape(taking_over))

    def test_probe_that_stops_at_its_second_change_does_not_say_none_came_after(self):
        out = io.StringIO()
        fanctl.probe(sim.make_cfg(), sim.FakeSwitch(override=40), out)
        self.assertRegex(out.getvalue(), r"\n  after 600 s: lowering fans -> override 36%: [^\n]*\n$")

    def test_probe_with_the_longest_legal_waits_finishes_and_says_how_far_it_looked(self):
        cfg = sim.make_cfg(poll=5, up_interval=600, down_interval=86400, probe_interval=86400,
                           ceiling_dwell=600, hold_max=86400)
        out = io.StringIO()
        start = time.monotonic()
        fanctl.probe(cfg, sim.FakeSwitch(override=40), out)
        self.assertLess(time.monotonic() - start, 5)    # it took a minute
        self.assertIn("it would keep the fans at override 40% (no change within 10000 s)", out.getvalue())

    def test_summary_gives_the_alert_limit_once(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, cfg=sim.make_cfg(summary=60)).run(75)

        def summary():
            return [line for line in s.logs().splitlines() if "status: fans" in line][-1]
        line = summary()                            # hottest: Front-panel; binding: Phy17-20
        self.assertIn("hottest vs target: Front-panel", line)
        self.assertIn("binding: Phy17-20", line)
        self.assertEqual(line.count("alert limit"), 2)
        find_sensor(sw, "Phy17-20")[1] += 5.0       # now the hottest and the binding sensor
        sw.plant.settle(40)
        s.run(60)
        line = summary()
        self.assertNotIn("hottest vs target", line)
        self.assertEqual(line.count("alert limit"), 1)
        self.assertRegex(line, r"min; inlet [\d.]+ C; binding: Phy17-20 \(TempSensor18\) 60\.0 C")

    def test_summary_on_a_poll_that_could_not_read_gives_no_readings(self):
        sw = sim.FakeSwitch(override=40)
        s = sim.Sim(sw, cfg=sim.make_cfg(summary=60)).run(30)
        sw.fail_reads = 1000
        s.run(45)                                   # summary due at 60 s
        line = [line for line in s.logs().splitlines() if "status: fans" in line][-1]
        self.assertTrue(line.endswith(" INFO status: fans auto, 1 change(s) in the last 1 min"), line)

    def test_a_summary_every_summary_s_counts_each_change_once(self):
        s = sim.Sim(sim.FakeSwitch(override=40), cfg=sim.make_cfg(summary=60)).run(375)   # a step down at 300 s
        self.assertEqual(re.findall(r" INFO status: fans .*?, (\d) change\(s\) in the last 1 min", s.logs()),
                         ["0", "0", "0", "0", "1", "0"])   # at 60, 120, ... 360 s


class RuntimeDirTests(unittest.TestCase):
    """The lock, heartbeat and memory must be where no other user can touch them (v1.3.1)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()               # 0700, in the module's safe directory (setUpModule)
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_private_dir_is_safe(self):
        self.assertIsNone(fanctl.check_dir(self.tmp))
        self.assertIsNone(fanctl.check_dir(mkdir(self.tmp, "run", 0o755)))

    def test_group_or_world_writable_dir_is_not(self):
        for name, mode in (("group", 0o775), ("world", 0o777), ("sticky", 0o1777)):
            self.assertIn("writable by group or others", fanctl.check_dir(mkdir(self.tmp, name, mode)), name)

    def test_symlinked_dir_is_not(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(mkdir(self.tmp, "real", 0o755), link)
        self.assertEqual(fanctl.check_dir(link), "it is a symlink")

    def test_sticky_world_writable_parent_is_fine(self):
        sticky = mkdir(self.tmp, "sticky", 0o1777)  # like /tmp
        self.assertIsNone(fanctl.check_dir(mkdir(self.tmp, "sticky/run", 0o755)))
        self.assertIsNotNone(fanctl.check_dir(mkdir(self.tmp, "open", 0o777)))
        self.assertIn("%s is writable" % os.path.join(self.tmp, "open"),
                      fanctl.check_dir(mkdir(self.tmp, "open/run", 0o755)))
        os.symlink(sticky, os.path.join(self.tmp, "alias"))       # an ancestor symlink is fine
        self.assertIsNone(fanctl.check_dir(os.path.join(self.tmp, "alias", "run")))

    def test_run_creates_a_missing_dir_0755(self):
        self.addCleanup(os.umask, os.umask(0o077))
        run = os.path.join(self.tmp, "run")

        def stop(_s):
            raise fanctl.Shutdown(signal.SIGTERM)
        sw = sim.FakeSwitch(override=40)
        d, lock = daemon(self, sw, run, sleep=stop)
        self.assertEqual(d.run(lock), 0, d.log.stream.getvalue())
        self.assertEqual(stat_mode(run), 0o755)     # whatever the umask: --status for anyone
        self.assertEqual(stat_mode(lock), 0o644)    # so are its files
        self.assertEqual(stat_mode(os.path.join(run, "memory.json")), 0o644)
        self.assertIn("handed back to EOS", d.log.stream.getvalue())
        deep = os.path.join(self.tmp, "a", "b")     # one level only, never its parents
        d, lock = daemon(self, sw, deep, sleep=stop)
        self.assertEqual(d.run(lock), 1)
        self.assertIn("cannot create runtime directory %s" % deep, d.log.stream.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a")))

    @unittest.skipIf(os.geteuid() == 0, "root can create and write to any directory")
    def test_a_runtime_dir_this_user_cannot_create_or_write_to_stops_the_start(self):
        self.addCleanup(os.umask, os.umask(0o022))
        ro = mkdir(self.tmp, "ro", 0o555)          # this user's, but read-only
        for d, why in ((os.path.join(ro, "run"), "cannot create runtime directory %s: Permission denied"),
                       (ro, "unsafe runtime directory %s: this user cannot write to it")):
            with self.subTest(dir=d):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = fanctl.main(["--no-syslog", "--cli", os.path.join(self.tmp, "no-cli"),
                                      "--state-file", os.path.join(d, "status.json"),
                                      "--memory-file", os.path.join(d, "memory.json"),
                                      "--lock-file", os.path.join(d, "lock")])
                self.assertEqual(rc, 1)
                self.assertRegex(out.getvalue(), r"^\S+ \S+ ERR %s; %s; not starting\n$"
                                 % (re.escape(why % d), re.escape(fanctl.NOT_ROOT_HINT)))

    def test_status_in_an_unsafe_dir_is_not_trusted(self):
        s = sim.Sim(sim.FakeSwitch(override=40), tmp=mkdir(self.tmp, "run", 0o755)).run(30)
        lock = os.path.join(s.tmp, "lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = io.StringIO()
        self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 30, out=out), 0,
                         out.getvalue())
        os.chmod(s.tmp, 0o555)                     # read-only, as root's /run/fanctl is to an operator
        self.addCleanup(os.chmod, s.tmp, 0o700)
        out = io.StringIO()
        self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 30, out=out), 0,
                         out.getvalue())
        os.chmod(s.tmp, 0o777)
        out = io.StringIO()
        self.assertEqual(fanctl.show_status(s.state_file, lock, wall=lambda: 1.7e9 + 30, out=out), 1)
        self.assertIn("UNTRUSTED: unsafe runtime directory %s" % s.tmp, out.getvalue())

    def test_status_never_hangs_on_a_planted_fifo(self):
        run = mkdir(self.tmp, "run", 0o777)         # unsafe: anyone could plant these
        status, lock = os.path.join(run, "status.json"), os.path.join(run, "lock")
        for fifo in (status, lock):
            with self.subTest(fifo=os.path.basename(fifo)):
                for path in (status, lock):
                    if os.path.lexists(path):
                        os.unlink(path)
                with open(status if fifo == lock else lock, "w") as f:
                    json.dump({"time": time.time(), "dry_run": False}, f)
                os.mkfifo(fifo)
                p = subprocess.run([sys.executable, os.path.join(ROOT, "fanctl.py"), "--status",
                                    "--state-file", status, "--lock-file", lock],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                out = p.stdout.decode()
                self.assertEqual(p.returncode, 1, out)
                self.assertTrue(out.startswith("UNTRUSTED: unsafe runtime directory %s" % run), out)
                if fifo == status:
                    self.assertIn("cannot read %s: not a regular file" % status, out)


class LockTests(unittest.TestCase):
    """Only one fanctl controls the fans, run in-process (EndToEndTests run
    two real ones). The test's own flock stands in for the other process."""

    def setUp(self):
        self.sw = sim.FakeSwitch(override=40)
        # One wall clock for the daemon and --status: a heartbeat's age is exact.
        self.d, self.lock = daemon(self, self.sw, sleep=self.sleep, wall=lambda: 1.7e9)
        self.state, self.log = self.d.state_file, self.d.log
        self.polls = 0

    def sleep(self, _s):
        self.polls += 1
        self.between_polls(self.polls)

    def run_daemon(self, between_polls):
        """Daemon.run(); between_polls(n) is called after poll n and may
        raise Shutdown to stop it."""
        self.between_polls = between_polls
        return self.d.run(self.lock)

    def hold(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def wait_logged(self, text, count=1):
        deadline = time.monotonic() + 10
        while self.log.stream.getvalue().count(text) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    def when_logged(self, text, action):
        """Run action() in a thread once the log shows text (or after 10 s)."""
        def wait():
            self.wait_logged(text)
            action()
        t = threading.Thread(target=wait)
        t.start()
        self.addCleanup(t.join)

    def stop(self, _n):
        raise fanctl.Shutdown(signal.SIGTERM)

    def test_status_while_fanctl_starts_says_so(self):
        shown, real = [], self.sw.run

        def status_first(commands, timeout=None):   # before startup's first FastCli call
            if not shown:
                out = io.StringIO()
                rc = fanctl.show_status(self.state, self.lock, wall=lambda: 1.7e9 + 2, out=out)
                shown.append((rc, out.getvalue()))
            return real(commands, timeout)
        self.sw.run = status_first
        self.assertEqual(self.run_daemon(self.stop), 0)
        self.assertRegex(shown[0][1], r"^fanctl %s pid %d, updated 2 s ago\nfans: not read yet \(fanctl is "
                                      r"starting\)\n$" % (re.escape(fanctl.VERSION), os.getpid()))
        self.assertEqual(shown[0][0], 0)

    def test_a_stop_signal_while_waiting_for_the_lock_ends_the_wait_and_changes_nothing(self):
        fd = self.hold(self.lock)
        waiting = "%s is locked by another fanctl or by fanctl-guard; waiting" % self.lock
        stopped = threading.Event()

        def stop_then_let_go():
            os.kill(os.getpid(), signal.SIGTERM)
            stopped.wait(10)                        # a fanctl that kept waiting starts then
            os.close(fd)
        self.when_logged(waiting, stop_then_let_go)
        rc = self.run_daemon(self.stop)
        stopped.set()
        self.assertEqual(rc, 0)
        self.assertRegex(self.log.stream.getvalue(), r"^\S+ \S+ WARNING %s\n$" % re.escape(waiting))
        self.assertEqual(self.sw.calls, 0)          # FastCli never ran
        self.assertFalse(os.path.exists(self.state))

    def test_waits_while_another_holds_the_lock_and_starts_once_it_is_free(self):
        fd = self.hold(self.lock)
        waiting = "%s is locked by another fanctl or by fanctl-guard; waiting" % self.lock
        self.when_logged(waiting, lambda: os.close(fd))
        self.assertEqual(self.run_daemon(self.stop), 0)
        out = self.log.stream.getvalue()
        self.assertLess(out.index(waiting), out.index("fanctl %s starting" % fanctl.VERSION))
        self.assertIn("handed back to EOS", out)

    def test_a_lock_on_a_file_removed_while_waiting_does_not_count(self):
        fd = self.hold(self.lock)
        waiting = "%s is locked by another fanctl or by fanctl-guard; waiting" % self.lock

        def replace_then_let_go():
            os.unlink(self.lock)                    # an admin's rm while fanctl waits,
            new = self.hold(self.lock)              # and the other fanctl locks a new file
            os.close(fd)                            # fanctl now gets the deleted file's lock
            self.wait_logged(waiting, 2)            # which must not count: it waits again
            os.close(new)
        self.when_logged(waiting, replace_then_let_go)
        self.assertEqual(self.run_daemon(self.stop), 0)
        out = self.log.stream.getvalue()
        self.assertEqual(out.count(waiting), 2, out)
        self.assertLess(out.rindex(waiting), out.index("fanctl %s starting" % fanctl.VERSION))
        self.assertNotIn("lock taken again", out)

    def test_a_lock_file_removed_while_running_is_locked_again(self):
        held = []

        def remove_then_check(n):
            if n == 1:
                os.unlink(self.lock)
                return
            held.append(fanctl.lock_is_held(self.lock))
            self.stop(n)
        self.assertEqual(self.run_daemon(remove_then_check), 0)
        self.assertEqual(held, [True])
        self.assertIn("%s had been removed; lock taken again" % self.lock, self.log.stream.getvalue())

    def test_a_lock_file_another_process_holds_now_stops_fanctl_and_leaves_everything(self):
        fds = []

        def replace(n):
            self.assertEqual(n, 1)                  # the next poll must stop it
            self.sw.override = 38                   # whatever the new holder set
            os.unlink(self.lock)
            fds.append(self.hold(self.lock))
        try:
            rc = self.run_daemon(replace)
        finally:
            for fd in fds:
                os.close(fd)
        out = self.log.stream.getvalue()
        self.assertEqual(rc, 1, out)
        self.assertIn("%s was replaced and is held by another process; stopping, and leaving the "
                      "fans, heartbeat and memory to it" % self.lock, out)
        self.assertNotIn("handed back to EOS", out)
        self.assertEqual(self.sw.override, 38)      # the fans are the new holder's
        self.assertTrue(os.path.exists(self.state))  # so is the heartbeat


class EndToEndTests(unittest.TestCase):
    """Runs fanctl.py and fanctl-guard.sh as real processes against tests/fake_fastcli.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "switch.json")
        self.status = os.path.join(self.tmp, "status.json")
        self.lock = os.path.join(self.tmp, "lock")
        self.fake = os.path.join(HERE, "fake_fastcli.py")
        self.env = dict(os.environ, FAKE_FASTCLI_STATE=self.state, FANCTL_STATE=self.status,
                        FANCTL_LOCK=self.lock, FANCTL_CLI=self.fake, FANCTL_LOGGER="echo")
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        for pid in self.wedged():                    # left behind only if a test failed
            if alive(pid):
                os.kill(pid, signal.SIGKILL)

    def wedged(self):
        """The pids of every wedged fake FastCli call and of the process each started."""
        try:
            return [pid for call in self.sw().get("wedged", []) for pid in call]
        except (OSError, ValueError):
            return []

    def switch(self, **st):
        """A fresh fake switch: override 40 % in running- and startup-config, unless `st` says otherwise."""
        with fake_fastcli.state(self.state) as cur:
            cur.clear()
            cur.update(override=40, startup_override=40)
            cur.update(st)

    def change(self, **st):
        """Change the fake switch while fanctl may be calling it."""
        with fake_fastcli.state(self.state) as cur:
            cur.update(st)

    def sw(self):
        return fake_fastcli.load(self.state)

    def start(self, *extra):
        cmd = [sys.executable, os.path.join(ROOT, "fanctl.py"), "--cli", self.fake,
               "--no-syslog", "--poll", "5", "--state-file", self.status,
               "--memory-file", os.path.join(self.tmp, "memory.json"),
               "--lock-file", self.lock, "--cli-timeout", "5"] + list(extra)
        # Output goes to a file, so a test can wait for a line while fanctl runs.
        with open(os.path.join(self.tmp, "fanctl-%d.out" % len(self.procs)), "wb") as out:
            p = subprocess.Popen(cmd, env=self.env, stdout=out, stderr=subprocess.STDOUT)
        p.out = out.name
        self.procs.append(p)
        return p

    def output(self, p):
        with open(p.out, errors="replace") as f:
            return f.read()

    def stop(self, p):
        """SIGTERM, as ProcMgr sends; fanctl's output once it has exited."""
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=60)
        return self.output(p)

    def needs(self, *tools):
        """Skip unless this host has what fanctl-guard.sh uses here, as EOS does."""
        for tool in ("bash",) + tools:
            if not shutil.which(tool):
                self.skipTest("needs %s, as on EOS" % tool)
        if not gnu_coreutils():
            self.skipTest("fanctl-guard.sh needs GNU coreutils (stat -c, readlink -f), as on EOS")

    def guard(self, env=None):
        self.needs()
        p = subprocess.run(["bash", os.path.join(ROOT, "fanctl-guard.sh")], env=env or self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return p.stdout.decode()

    def guard_eval(self, script, *args, env=None):
        """Run `script` in bash after the guard's settings and functions (not its main block)."""
        self.needs()
        with open(os.path.join(ROOT, "fanctl-guard.sh")) as f:
            defs, _main = f.read().split('\nif [ "$1" = "loop" ]; then\n')
        p = subprocess.run(["bash", "-c", defs + "\n" + script, "_"] + list(args), env=env or self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return p.stdout.decode()

    @contextlib.contextmanager
    def fanctl_holds(self, status, lock, mtime=None, version=fanctl.VERSION):
        """What a running fanctl leaves for the guard: its exclusive lock on the
        file at `lock` (held here, in-process) and a heartbeat in `status`, last
        touched at `mtime`. The file is a new one, locked before it is moved to
        `lock`, so no one can take it first and nothing here waits."""
        fanctl.write_json(status, {"version": version, "dry_run": False})   # as fanctl writes it
        if mtime is not None:
            os.utime(status, (mtime, mtime))
        fd = os.open(lock + ".new", os.O_RDONLY | os.O_CREAT | os.O_EXCL)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.rename(lock + ".new", lock)              # replaces any file there
            yield
        finally:
            os.close(fd)

    def wait_for(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.2)
        return False

    def test_sigterm_restores_auto(self):
        self.switch()
        p = self.start("--down-interval", "1")
        self.assertTrue(self.wait_for(lambda: self.sw()["override"] == 38), "never lowered the fans")
        out = self.stop(p)
        self.assertEqual(p.returncode, 0, out)
        self.assertIsNone(self.sw()["override"], out)
        self.assertIn("handed back to EOS", out)
        self.assertFalse(os.path.exists(self.status))

    def test_a_lock_and_heartbeat_the_guard_does_not_watch_are_named_at_startup(self):
        self.switch()
        warning = ("WARNING --lock-file %s and --state-file %s: fanctl-guard.sh watches %s and %s unless "
                   "its FANCTL_LOCK and FANCTL_STATE name others" % (self.lock, self.status, fanctl.LOCK_FILE,
                                                                     fanctl.STATE_FILE))
        p = self.start()
        self.assertTrue(self.wait_for(self.polled), "never polled")
        self.assertIn(warning, self.stop(p))
        p = self.start("--dry-run")                 # the guard never trusts a dry run's heartbeat
        self.assertTrue(self.wait_for(self.polled), "never polled")
        self.assertNotIn("fanctl-guard.sh watches", self.stop(p))

    def polled(self):
        """True once fanctl has finished starting up and written a poll's status."""
        try:
            with open(self.status) as f:
                return json.load(f).get("event") != "starting"
        except (OSError, ValueError):
            return False

    def test_sigterm_while_cli_is_wedged(self):
        self.switch()
        p = self.start("--cli-timeout", "60")
        # Wedge the CLI only after start-up: a stop during start-up rightly
        # leaves the fans as fanctl found them.
        self.assertTrue(self.wait_for(self.polled))
        self.change(hang=["show system environment"])
        self.assertTrue(self.wait_for(self.wedged), "the next poll never got stuck")
        t0 = time.time()
        out = self.stop(p)
        # The wedged read is cut off at once; handing back to EOS then waits
        # out one quick read-back of the cooling status (running-config answers).
        self.assertLess(time.time() - t0, fanctl.QUICK_CLI_TIMEOUT + 5, out)
        self.assertIsNone(self.sw()["override"], out)
        self.assertTrue(self.wait_for(lambda: not [pid for pid in self.wedged() if alive(pid)], 5),
                        self.sw()["wedged"])             # killed with the process it had started

    def test_a_fastcli_call_that_times_out_is_killed_and_counts_as_a_failed_read(self):
        self.switch()
        p = self.start()                                 # --cli-timeout 5; read_failures 2
        self.assertTrue(self.wait_for(self.polled))
        self.change(hang=[fanctl.TEMP_CMD])              # each poll's first read wedges, the rest answer
        # Wait for the log line, which comes after the write: stopping fanctl
        # as soon as the switch shows auto can cut the line off.
        handed = "handing fan control to EOS (auto) -> auto: switch environment unreadable 2 times in a row"
        self.assertTrue(self.wait_for(lambda: handed in self.output(p), 60), self.output(p))
        self.assertIsNone(self.sw()["override"])
        self.assertGreaterEqual(len(self.sw()["wedged"]), 2)
        out = self.stop(p)
        self.assertIn("could not read switch environment: fake_fastcli.py timed out after 5s", out)
        # Each timed-out call was killed with the process it had started.
        self.assertTrue(self.wait_for(lambda: not [pid for pid in self.wedged() if alive(pid)], 5),
                        self.sw()["wedged"])

    def test_second_instance_waits_and_leaves_fans_alone(self):
        self.switch()
        p1 = self.start()
        # In control, not still starting: a stop during start-up rightly
        # leaves the fans as fanctl found them.
        self.assertTrue(self.wait_for(self.polled))
        p2 = self.start()
        self.assertTrue(self.wait_for(lambda: "is locked by another fanctl or by fanctl-guard; waiting" in self.output(p2)),
                        self.output(p2))
        out2 = self.stop(p2)
        self.assertEqual(self.sw()["override"], 40, out2)   # p2 must not have restored auto
        self.stop(p1)
        self.assertIsNone(self.sw()["override"])

    def test_a_waiting_instance_does_not_start_on_a_removed_lock_file(self):
        self.switch()
        p1 = self.start()
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.status)))
        p2 = self.start()
        waiting = "is locked by another fanctl or by fanctl-guard; waiting"
        starting = "fanctl %s starting" % fanctl.VERSION
        self.assertTrue(self.wait_for(lambda: waiting in self.output(p2)), self.output(p2))
        os.unlink(self.lock)                          # an admin's rm, or a cleanup
        self.assertTrue(self.wait_for(lambda: "lock taken again" in self.output(p1)), self.output(p1))
        # p1 let go of the deleted file, so p2 got its lock: p2 must see that
        # it is no longer the lock file and wait on the new one.
        self.assertTrue(self.wait_for(lambda: self.output(p2).count(waiting) == 2
                                      or starting in self.output(p2)), self.output(p2))
        self.assertNotIn(starting, self.output(p2))
        self.assertEqual(self.heartbeat()["pid"], p1.pid)
        self.stop(p1)                                 # p2 takes over once p1 is gone
        self.assertTrue(self.wait_for(lambda: starting in self.output(p2)), self.output(p2))
        self.stop(p2)

    def test_a_fanctl_that_lost_its_lock_file_stops_and_leaves_everything_alone(self):
        self.switch()
        p = self.start("--down-interval", "1")
        self.assertTrue(self.wait_for(lambda: self.sw()["override"] == 38))
        # Another fanctl, or the guard, puts a file it holds at the lock's path.
        with self.fanctl_holds(self.status, self.lock):
            self.assertTrue(self.wait_for(lambda: p.poll() is not None), self.output(p))
            out = self.output(p)
            self.assertEqual(p.returncode, 1, out)
            self.assertIn("was replaced and is held by another process", out)
            self.assertNotIn("handed back to EOS", out)
            self.assertIsNotNone(self.sw()["override"], out)    # the fans are the new holder's
            self.assertTrue(os.path.exists(self.status), out)   # so is the heartbeat

    def test_probe_end_to_end_finds_the_cli(self):
        self.switch()
        out = io.StringIO()
        with mock.patch.dict(os.environ, FAKE_FASTCLI_STATE=self.state), contextlib.redirect_stdout(out), \
                mock.patch.object(fanctl, "CLI_CANDIDATES", (os.path.join(self.tmp, "FastCli"), self.fake)):
            rc = fanctl.main(["--probe"])           # no FastCli here, so the next candidate
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("verdict: fanctl would carry on from the current override 40%.", out.getvalue())
        self.assertEqual([c for c in self.sw()["log"] if c[0] == "configure"], [])

    def test_guard_leaves_a_live_fanctl_alone(self):
        self.switch()
        p = self.start()
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.status)))
        out = self.guard()
        self.assertEqual(self.sw()["override"], 40, out)
        self.assertEqual(out, "")                     # a matching pair: nothing to say
        self.stop(p)

    def test_guard_acts_at_once_after_a_sigkill(self):
        self.needs("flock")                              # else the fresh heartbeat looks alive
        self.switch()
        p = self.start("--down-interval", "1")
        self.assertTrue(self.wait_for(lambda: self.sw()["override"] == 38))
        p.kill()                                      # no handler runs; heartbeat stays fresh
        p.wait(timeout=20)
        self.assertEqual(self.sw()["override"], 38)
        out = self.guard()
        self.assertIsNone(self.sw()["override"], out)
        self.assertIn("restored automatic fan control", out)

    def test_guard_does_not_trust_a_dry_run(self):
        self.switch()
        p = self.start("--dry-run")
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.status)))
        out = self.guard()
        self.assertIsNone(self.sw()["override"], out)
        self.stop(p)

    def test_guard_catches_a_hung_fanctl(self):
        self.switch()
        with self.fanctl_holds(self.status, self.lock, time.time() - 600):   # lock held, no heartbeat for 10 min
            out = self.guard()
        self.assertIsNone(self.sw()["override"], out)

    def test_guard_leaves_a_starting_fanctl_alone(self):
        self.switch(slow_reads=2)                    # each environment read takes 2 s
        p = self.start()
        self.assertTrue(self.wait_for(lambda: os.path.exists(self.status)))
        out = self.guard()
        self.assertFalse(self.polled(), "the first poll ended before the guard ran")
        self.assertEqual(self.sw()["override"], 40, out)
        self.assertTrue(self.wait_for(self.polled, 60), "the first poll never ended")
        out = self.stop(p)
        self.assertNotIn("changed outside fanctl", out)

    def heartbeat(self):
        """The status fanctl last wrote, or {}."""
        try:
            with open(self.status) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def test_a_starting_fanctl_waits_for_the_guard_s_restore(self):
        self.needs("flock")
        self.switch(slow_configure=3)                # a killed fanctl left override 40
        guard = subprocess.Popen(["bash", os.path.join(ROOT, "fanctl-guard.sh")], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.procs.append(guard)
        self.assertTrue(self.wait_for(lambda: ["configure", "environment fan-speed auto", "end"]
                                      in self.sw().get("log", [])), "the guard never restored auto")
        p = self.start()                             # while the guard's 'auto' is still on its way
        out = guard.communicate(timeout=60)[0].decode()
        self.assertIn("restored automatic fan control", out)
        done = time.time()
        # A whole poll after the guard finished: fanctl has seen what the guard left.
        self.assertTrue(self.wait_for(lambda: self.heartbeat().get("time", 0) > done + 6, 30),
                        self.output(p))
        self.assertFalse(self.heartbeat()["paused"], self.output(p))
        out = self.stop(p)
        self.assertIn("is locked by another fanctl or by fanctl-guard; waiting", out)
        self.assertNotIn("changed outside fanctl", out)

    def test_guard_takes_the_lock_before_fanctl_has_run(self):
        self.switch()                                # a reload left the saved override 40
        run = os.path.join(self.tmp, "run")          # no fanctl since boot: no runtime directory
        lock = os.path.join(run, "lock")
        out = self.guard(dict(self.env, FANCTL_STATE=os.path.join(run, "status.json"), FANCTL_LOCK=lock))
        self.assertIsNone(self.sw()["override"], out)
        self.assertEqual((stat_mode(run), stat_mode(lock)), (0o755, 0o644))   # as fanctl makes them

    def configured(self):
        """The configuration lines sent to the fake switch, in order."""
        return [c[1] for c in self.sw().get("log", []) if c[0] == "configure"]

    def test_guard_falls_back_as_fanctl_does(self):
        """EOS refusing both ways back to auto: the guard, like fanctl, leaves the fans flat out."""
        refuse = ["environment fan-speed auto", "no environment fan-speed"]
        self.switch()
        p = self.start()
        self.assertTrue(self.wait_for(self.polled))
        self.change(refuse=refuse, log=[])
        out = self.stop(p)                           # fanctl restoring auto as it stops
        self.assertEqual(self.sw()["override"], 100, out)
        fanctl_steps = self.configured()
        self.switch(refuse=refuse)
        out = self.guard()
        self.assertEqual(self.sw()["override"], 100, out)
        self.assertIn("EOS refused automatic fan control; fans set to override 100%", out)
        self.assertEqual(self.configured(), fanctl_steps)
        self.assertEqual(fanctl_steps, refuse + ["environment fan-speed override 100"])

    def test_guard_retries_auto_at_every_check(self):
        """Flat out after EOS refused auto: each check tries again, and the first one EOS accepts restores it."""
        refuse = ["environment fan-speed auto", "no environment fan-speed"]
        self.switch(refuse=refuse)
        out = self.guard_eval("check; check")        # two minutes of the guard's loop
        self.assertEqual(self.configured(), 2 * (refuse + ["environment fan-speed override 100"]), out)
        self.assertIn("fans set to override 100% (the setting was override 40%)", out)
        self.assertIn("fans set to override 100% (the setting was override 100%)", out)
        self.change(refuse=[])                       # the cause is fixed
        out = self.guard()
        self.assertIsNone(self.sw()["override"], out)
        self.assertIn("the fan setting was override 100%; restored automatic fan control", out)

    def test_guard_reads_the_fan_setting_as_fanctl_does(self):
        cli = fanctl.Cli(self.fake, 10)
        for st in ({"override": None}, {"override": 40}, {"override": 100}, {"fail_all_shows": True}):
            with self.subTest(**st):
                self.switch(**st)
                with mock.patch.dict(os.environ, FAKE_FASTCLI_STATE=self.state):
                    try:
                        setting = fanctl.current_setting(cli)
                    except fanctl.CliError:
                        setting = None
                self.assertEqual(self.guard_eval("fan_setting"), fanctl.fmt_setting(setting) + "\n")

    def test_guard_survives_a_wedged_cli(self):
        self.needs("timeout")                            # else FastCli hangs the guard
        self.switch(hang=["show"])
        cli_timeout = 1
        t0 = time.time()
        out = self.guard(dict(self.env, FANCTL_CLI_TIMEOUT=str(cli_timeout)))
        # At most 9 FastCli calls, each cut off after cli_timeout, and 3 pauses of
        # 2 s; the rest is slack for a loaded machine. A hung call would take an hour.
        self.assertLess(time.time() - t0, 9 * cli_timeout + 3 * 2 + 20, out)
        self.assertIn("FAILED", out)                 # could not confirm: says so loudly

    def test_guard_acts_when_it_cannot_read_the_setting(self):
        self.switch(fail_all_shows=True)
        out = self.guard()
        self.assertIsNone(self.sw()["override"], out)

    def test_guard_distrusts_a_future_heartbeat(self):
        self.switch()
        with self.fanctl_holds(self.status, self.lock, time.time() + 3600):
            out = self.guard()
        self.assertIsNone(self.sw()["override"], out)

    def test_refuses_to_start_in_an_unsafe_dir(self):
        self.switch()
        shared = mkdir(self.tmp, "shared", 0o777)
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(ROOT, "fanctl.py"), "--cli", self.fake, "--no-syslog",
                            "--state-file", os.path.join(shared, "status.json"),
                            "--memory-file", os.path.join(shared, "memory.json"),
                            "--lock-file", os.path.join(shared, "lock")],
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        out = p.stdout.decode()
        self.assertLess(time.time() - t0, 10, out)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("ERR unsafe runtime directory %s: it is writable by group or others (mode 0777); "
                      "not starting" % shared, out)
        self.assertEqual(self.sw()["override"], 40)
        self.assertNotIn("log", self.sw())               # FastCli never ran
        self.assertEqual(os.listdir(shared), [])         # no lock, heartbeat or memory

    def test_guard_distrusts_a_lock_and_heartbeat_others_could_fake(self):
        safe, shared = mkdir(self.tmp, "safe", 0o755), mkdir(self.tmp, "shared", 0o777)
        for state_dir, lock_dir, trusted in ((safe, safe, True), (shared, safe, False),
                                             (safe, shared, False), (shared, shared, False)):
            with self.subTest(state=os.path.basename(state_dir), lock=os.path.basename(lock_dir)):
                self.switch()
                status, lock = os.path.join(state_dir, "status.json"), os.path.join(lock_dir, "lock")
                with self.fanctl_holds(status, lock):                 # a live-looking fanctl
                    out = self.guard(dict(self.env, FANCTL_STATE=status, FANCTL_LOCK=lock))
                # private: trusted; anyone could have faked one of them: auto restored
                self.assertEqual(self.sw()["override"], 40 if trusted else None, out)

    def assert_dir_rules_match(self, cases):
        """The guard's dir_ok and fanctl's check_dir agree with `cases`: [(dir, trusted)]."""
        got = self.guard_eval('for d in "$@"; do dir_ok "$d" && echo ok || echo refused; done',
                              *[d for d, _ok in cases]).split()
        self.assertEqual(got, ["ok" if ok else "refused" for _d, ok in cases])
        for d, ok in cases:
            self.assertEqual(fanctl.check_dir(d, writable=False) is None, ok, d)

    def test_guard_directory_rules_match_fanctl(self):
        t = self.tmp                                     # 0700, in a safe directory (setUpModule)
        os.symlink(mkdir(t, "run", 0o755), os.path.join(t, "link"))
        os.symlink(mkdir(t, "sticky", 0o1777), os.path.join(t, "alias"))
        mkdir(t, "world", 0o777)
        cases = [(t, True), (os.path.join(t, "run"), True),
                 (mkdir(t, "group", 0o775), False), (os.path.join(t, "world"), False),
                 (os.path.join(t, "sticky"), False),        # /tmp can hold the directory, not be it
                 (os.path.join(t, "link"), False),          # a symlink
                 (mkdir(t, "world/run", 0o755), False),     # inside a non-sticky writable one
                 (mkdir(t, "sticky/run", 0o755), True),     # inside a sticky one
                 (os.path.join(t, "alias", "run"), True),   # an ancestor symlink is fine
                 (os.path.join(t, "missing"), False)]
        self.assert_dir_rules_match(cases)

    @unittest.skipUnless(os.geteuid() == 0, "only root can give a directory to another user")
    def test_guard_and_fanctl_refuse_another_user_s_directory(self):
        other = mkdir(self.tmp, "other", 0o755)
        os.chown(other, 12345, -1)
        self.assert_dir_rules_match([(other, False), (mkdir(self.tmp, "other/run", 0o755), False)])

    def test_guard_defaults_are_fanctl_s(self):
        """fanctl.py is the source of truth for what the guard has to copy."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("FANCTL_")}
        out = self.guard_eval('printf "%s\\n" "$STATE" "$LOCK" "$STALE" "$VERSION"', env=env)
        self.assertEqual(out.splitlines(), [fanctl.STATE_FILE, fanctl.LOCK_FILE, str(fanctl.STALE_AFTER),
                                            fanctl.VERSION])

    def test_guard_warns_once_about_a_fanctl_from_another_release(self):
        self.switch()
        with self.fanctl_holds(self.status, self.lock, version="1.2.0"):
            out = self.guard_eval("check; check")
        self.assertEqual(self.sw()["override"], 40, out)          # still left alone
        self.assertEqual(out.count("fanctl 1.2.0 is running with fanctl-guard %s; install both files "
                                   "from the same release" % fanctl.VERSION), 1, out)

    def test_guard_logs_its_version_when_it_starts(self):
        self.needs()
        with open(os.path.join(self.tmp, "guard.out"), "wb") as out:
            g = subprocess.Popen(["bash", os.path.join(ROOT, "fanctl-guard.sh"), "loop"],
                                 env=dict(self.env, FANCTL_GUARD_GRACE="3600"), stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(lambda: g.poll() is None and os.killpg(g.pid, signal.SIGKILL))
        self.procs.append(g)
        self.assertTrue(self.wait_for(lambda: "fanctl-guard %s started" % fanctl.VERSION
                                      in fake_fastcli.read(out.name)), fake_fastcli.read(out.name))

    def test_guard_does_nothing_when_already_auto(self):
        self.switch(override=None)
        self.guard()
        self.assertEqual([c for c in self.sw()["log"] if c[0] == "configure"], [])


class ReleaseTests(unittest.TestCase):
    def test_sha256sums_matches_the_files_installed_on_the_switch(self):
        """The install check is 'sha256sum -c SHA256SUMS': it must cover both
        on-box files and pass for the files in this tree."""
        listed = {}
        with open(os.path.join(ROOT, "SHA256SUMS")) as f:
            for line in f.read().splitlines():
                digest, name = line.split(None, 1)
                listed[name] = digest
        actual = {}
        for name in ("fanctl.py", "fanctl-guard.sh"):
            with open(os.path.join(ROOT, name), "rb") as f:
                actual[name] = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(listed, actual, "regenerate it: sha256sum fanctl.py fanctl-guard.sh > SHA256SUMS")

    def test_validation_gives_the_suite_s_size(self):
        """docs/VALIDATION.md is the one place that says how many tests there are."""
        with open(os.path.join(ROOT, "docs", "VALIDATION.md")) as f:
            stated = re.findall(r"\b(\d+) tests run against", f.read())
        # A fresh loader: `-k` filters the default one, which would count only the selected tests.
        found = unittest.TestLoader().discover(os.path.join(ROOT, "tests")).countTestCases()
        self.assertEqual(stated, [str(found)], "update the count in docs/VALIDATION.md's Off-box test suite")


if __name__ == "__main__":
    unittest.main()

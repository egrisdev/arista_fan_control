"""A simulated DCS-7050TX-64-R for testing fanctl off the switch.

FakeSwitch answers the same CLI commands fanctl sends, starting from the real
JSON captured from the switch (tests/fixtures, EOS 4.28.13.1M, fans at a static
40 % override, inlet 25.25 C). A simple thermal model moves each sensor:

    steady state = inlet + rise_at_40% * (40 / fan%) ** 0.8
    first-order lag toward it with a per-sensor time constant

rise_at_40% comes from the capture. EOS "auto" is modelled as Arista's curve
on this family: about 60 %, more when sensors pass their setpoints, 100 % at
an alert limit. Faults can be injected by flipping attributes.
"""

import atexit
import json
import math
import os
import random
import shutil
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fanctl  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
BANNER = """====================================================================
WARNING: Overriding the system fan speed is unsupported and should
only be done under the direction of an Arista Networks engineer.
====================================================================
"""


def load_fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def json_copy(doc):
    """A deep copy of parsed JSON (dicts, lists and scalars), several times
    faster than copy.deepcopy, which the simulator would call every poll."""
    if isinstance(doc, dict):
        return {k: json_copy(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [json_copy(v) for v in doc]
    return doc


def _quantum(desc):
    if desc.startswith("Phy") or desc in ("Hotspot", "Inlet"):
        return 1.0
    if desc.startswith("Trident"):
        return 0.5424
    if desc.startswith("Cpu temp"):
        return 0.0
    return 0.125


def _tau(desc):
    if desc.startswith("Cpu temp"):
        return 90.0
    if desc in ("Hotspot", "Inlet"):
        return 120.0
    if desc.startswith(("Front", "Back")):
        return 300.0
    return 240.0


class Plant(object):
    def __init__(self, ambient=25.25, fan=40.0, jitter=0.0, seed=1):
        self.temp = load_fixture("temp.json")
        self.jitter = jitter
        self._rng = random.Random(seed)
        base_amb = 25.25
        self.sensors = []   # (raw dict, rise_at_40, tau, quantum, true temperature)
        for raw in self._all_raw():
            desc = raw["description"]
            rise = max(0.5, raw["currentTemperature"] - base_amb)
            if desc.startswith("Back-panel"):   # on -R units this is the inlet side
                rise = 0.0
            self.sensors.append([raw, rise, _tau(desc), _quantum(desc), raw["currentTemperature"]])
        self.ambient = ambient
        self.settle(fan)

    def _all_raw(self):
        out = list(self.temp["tempSensors"])
        for slot in self.temp["powerSupplySlots"]:
            out.extend(slot["tempSensors"])
        return out

    def steady(self, rise, fan):
        return self.ambient + rise * (40.0 / max(fan, 5.0)) ** 0.8

    def settle(self, fan):
        for s in self.sensors:
            s[4] = self.steady(s[1], fan)

    def advance(self, dt, fan):
        for s in self.sensors:
            ss = self.steady(s[1], fan)
            s[4] = ss + (s[4] - ss) * math.exp(-dt / s[2])

    def readings(self):
        for raw, _rise, _tau, q, true in self.sensors:
            val = true + (self._rng.uniform(-self.jitter, self.jitter) if self.jitter else 0.0)
            yield raw, (round(val / q) * q if q else val)


class FakeSwitch(object):
    """Duck-types fanctl.Cli: run(commands) -> stdout, raising fanctl.CliError."""

    path = "/usr/bin/FastCli (simulated)"

    def __init__(self, ambient=25.25, override=40, startup_override=40, jitter=0.0, auto_floor=60):
        self.plant = Plant(ambient=ambient, fan=override or auto_floor, jitter=jitter)
        self.auto_floor = auto_floor
        self.override = override            # None = auto
        self.startup_override = startup_override
        self.cool = load_fixture("cooling.json")
        self.power = load_fixture("power.json")
        self.t = 0.0
        self.ambient_fn = None              # optional f(t) -> ambient
        # fault injection
        self.fail_reads = 0                 # next N environment reads raise CliError
        self.garbage = False                # environment reads return non-JSON
        self.reject_override = False
        self.reject_auto = False            # 'environment fan-speed auto' and 'no ...' both fail
        self.fail_config = False            # every config command fails
        self.fail_cooling_after_config = 0  # this many cooling reads fail after each config change
        self.fail_running = 0               # next N running-config reads raise CliError
        self.fail_startup = False           # startup-config reads raise CliError
        self.config_error = None            # config changes take effect but print this error
        self.status_shows = None            # a setting the cooling status shows instead (a lagging status)
        self.fan_status = {}                # label -> status override, e.g. {"2/1": "failed"}
        self.psu_state = {}                 # "1" -> "powerLoss"
        self.sensor_hw = {}                 # description -> hwStatus
        self.drop_current = set()           # descriptions whose currentTemperature is omitted
        self.temp_status = "temperatureOk"
        self.config_log = []                # (t, line)
        self.calls = 0
        self._cooling_failures = 0
        self.auto_speed = auto_floor

    # -- physics --
    def fan_pct(self):
        return float(self.override) if self.override is not None else float(self.auto_speed)

    def _update_auto(self):
        worst, alert = -99.0, False
        for raw, val in self.plant.readings():
            worst = max(worst, val - raw["setPointTemperature"])
            alert = alert or val >= raw.get("overheatThreshold", 999)
        floor = self.auto_floor
        self.auto_speed = 100 if alert else int(min(100, max(floor, floor + 10 * max(0.0, worst))))

    def advance(self, dt, step=5.0):
        end = self.t + dt
        while self.t < end - 1e-9:
            h = min(step, end - self.t)
            if self.ambient_fn:
                self.plant.ambient = self.ambient_fn(self.t)
            if self.override is None:
                self._update_auto()
            self.plant.advance(h, self.fan_pct())
            self.t += h

    # -- what EOS would print --
    def temp_doc(self):
        values = {id(raw): val for raw, val in self.plant.readings()}
        doc = json_copy(self.plant.temp)
        orig = list(self.plant._all_raw())
        copies = list(doc["tempSensors"]) + [s for slot in doc["powerSupplySlots"] for s in slot["tempSensors"]]
        for o, c in zip(orig, copies):
            v = values[id(o)]
            psu = c["name"].startswith("TempSensorP")
            if psu and self.psu_state.get(c["name"][11]) not in (None, "ok"):
                v = 0.0
            c["currentTemperature"] = v
            c["inAlertState"] = v >= c.get("overheatThreshold", 999)
            if c["description"] in self.sensor_hw:
                c["hwStatus"] = self.sensor_hw[c["description"]]
            if c["description"] in self.drop_current:
                del c["currentTemperature"]
        doc["systemStatus"] = self.temp_status
        return doc

    def cooling_doc(self):
        doc = json_copy(self.cool)
        pct = int(round(self.fan_pct()))
        shown = self.override if self.status_shows is None else self.status_shows[1]
        doc["coolingMode"] = "manual" if shown is not None else "automatic"
        doc["overrideFanSpeed"] = shown or 0
        doc["ambientTemperature"] = round(self.plant.ambient * 4) / 4
        for slot in doc["fanTraySlots"] + doc.get("powerSupplySlots", []):
            slot["speed"] = pct
            for fan in slot["fans"]:
                fan["configuredSpeed"] = pct
                fan["actualSpeed"] = pct
                if fan["label"] in self.fan_status:
                    fan["status"] = self.fan_status[fan["label"]]
        return doc

    def power_doc(self):
        doc = json_copy(self.power)
        for pid, state in self.psu_state.items():
            doc["powerSupplies"][pid]["state"] = state
        return doc

    # -- the CLI --
    def run(self, commands, timeout=None):
        self.calls += 1
        if commands == [fanctl.COOLING_CMD] and self._cooling_failures:
            self._cooling_failures -= 1
            raise fanctl.CliError("FastCli timed out after 30s")
        if commands == [fanctl.TEMP_CMD] or commands == [fanctl.COOLING_CMD] or commands == [fanctl.POWER_CMD]:
            if self.fail_reads:
                self.fail_reads -= 1
                raise fanctl.CliError("FastCli timed out after 30s")
            if self.garbage:
                return "Traceback (most recent call last):\n  oops\n"
            doc = {fanctl.TEMP_CMD: self.temp_doc, fanctl.COOLING_CMD: self.cooling_doc,
                   fanctl.POWER_CMD: self.power_doc}[commands[0]]()
            # Compact: the indented form EOS prints costs the pure-Python
            # encoder. ParsingTests read the captured, indented text.
            return json.dumps(doc) + "\n"
        if commands == [fanctl.RUNNING_CMD]:
            if self.fail_running:
                self.fail_running -= 1
                raise fanctl.CliError("FastCli timed out after 30s")
            return "environment fan-speed override %d\n" % self.override if self.override is not None else ""
        if commands == [fanctl.STARTUP_CMD]:
            if self.fail_startup:
                raise fanctl.CliError("FastCli timed out after 30s")
            return ("environment fan-speed override %d\n" % self.startup_override
                    if self.startup_override is not None else "")
        if len(commands) == 3 and commands[0] == "configure" and commands[2] == "end":
            line = commands[1]
            self.config_log.append((self.t, line))
            self._cooling_failures = self.fail_cooling_after_config
            if self.fail_config:
                raise fanctl.CliError("FastCli timed out after 30s")
            if line in ("environment fan-speed auto", "no environment fan-speed"):
                if self.reject_auto:
                    raise fanctl.CliError("% Error: simulated refusal")
                self.override = None
                return self._config_done("")
            parts = line.split()
            if parts[:3] == ["environment", "fan-speed", "override"] and len(parts) == 4:
                n = int(parts[3])
                if self.reject_override:
                    raise fanctl.CliError("% Error: fan speed override is not supported")
                if not 30 <= n <= 100:
                    raise fanctl.CliError("% Invalid input")
                self.override = n
                return self._config_done(BANNER)
        raise fanctl.CliError("% Invalid input (simulated): %r" % (commands,))

    def _config_done(self, out):
        if self.config_error:
            raise fanctl.CliError(self.config_error)
        return out


_TMPDIRS = []
atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _TMPDIRS])


def make_cfg(**overrides):
    argv = []
    for k, v in overrides.items():
        if k in ("ignore_psu", "ignore_sensor"):
            for item in v:
                argv += ["--" + k.replace("_", "-"), str(item)]
        else:
            argv += ["--" + k.replace("_", "-"), str(v)]
    cfg = fanctl.make_config(fanctl.parse_args(argv))
    assert not fanctl.check_config(cfg), fanctl.check_config(cfg)
    return cfg


class Sim(object):
    """Drives a fanctl.Daemon against a FakeSwitch in simulated time."""

    def __init__(self, switch=None, cfg=None, state_file=None, dry_run=False, log_stream=None, tmp=None):
        import io
        import tempfile
        self.sw = switch or FakeSwitch()
        self.cfg = cfg or make_cfg()
        self.tmp = tmp or tempfile.mkdtemp(prefix="fanctl-sim-")
        _TMPDIRS.append(self.tmp)
        self.state_file = state_file or os.path.join(self.tmp, "status.json")
        self.memory_file = os.path.join(self.tmp, "memory.json")
        self.log_stream = log_stream if log_stream is not None else io.StringIO()
        self.log = fanctl.Log(use_syslog=False, stream=self.log_stream)
        self.dry_run = dry_run
        self.trace = []   # (t, fan%, mode, hottest excess)
        self.start()

    def start(self):
        """(Re)start fanctl against the same switch, as ProcMgr would."""
        self.d = fanctl.Daemon(self.cfg, self.sw, self.log, state_file=self.state_file,
                               memory_file=self.memory_file, dry_run=self.dry_run,
                               clock=lambda: self.sw.t, wall=lambda: 1.7e9 + self.sw.t,
                               sleep=lambda s: None)
        self.d.startup(self.sw.t)
        self.d.controlling = True
        return self

    def stop(self):
        """SIGTERM, as ProcMgr sends: Daemon.stop(), which Daemon.run() ends
        with. The simulator holds no lock, so the heartbeat stays (Daemon.stop
        removes only its own)."""
        self.d.stop("stopped by signal %d" % signal.SIGTERM)
        return self

    def kill(self):
        """SIGKILL, which is how EOS stops a daemon: nothing in Daemon.run()'s
        finally happens (no memory save, no restore to auto, the heartbeat
        stays behind)."""
        self.d = None
        return self

    def run(self, seconds, on_tick=None):
        end = self.sw.t + seconds
        while self.sw.t < end - 1e-9:
            self.d.tick(self.sw.t)
            r = self.d.last_reading
            self.trace.append((self.sw.t, self.sw.fan_pct(), "auto" if self.sw.override is None else "override",
                               r.excess if r is not None and r.sensors else None))
            if on_tick:
                on_tick(self)
            self.sw.advance(self.cfg.poll)
        return self

    def changes(self, since=0.0):
        return [(t, line) for t, line in self.sw.config_log if t >= since]

    def logs(self):
        return self.log_stream.getvalue()

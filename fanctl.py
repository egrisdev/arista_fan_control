#!/usr/bin/env python3
"""fanctl - run an Arista switch's fans as slowly as is safe.

Built for a DCS-7050TX-64-R on EOS 4.28.13.1M (Python 3.9: run it with
/usr/bin/python3; on that release arista-python is Python 2.7).
It should work on other fixed-configuration EOS switches that support
'environment fan-speed override'.

Every --poll seconds it reads, through FastCli:

    show system environment temperature | json
    show system environment cooling | json
    show system environment power | json

Each healthy sensor is compared with EOS's own control target for it
(setPointTemperature; on a DCS-7050TX-64-R that is 0.65 x its alert limit
when the sensor has no explicit target; where EOS reports none, fanctl uses
that same ratio and warns, at the first poll, daily and in --probe, that the
target is a guess). Its "excess" (C over (+) or under (-)
that target) is weighted by how much the fans can move it: divided by its rise
over the inlet relative to the most fan-sensitive sensor's (at least
min_authority). So a sensor the fans can barely cool, like the front panel
that mostly follows the room, is held closer to its target instead of being
chased with fan speed. fanctl keeps the hottest weighted excess in a band:

    excess > -keep_below            -> raise the fans up_step % every up_interval
                                       (at once at target, and sooner while a
                                       reading at target is still getting hotter)
    excess < -(keep_below + band)   -> lower them down_step % every down_interval
    excess < -(keep_below + probe_margin)
                                    -> still try one down_step every probe_interval
    otherwise                       -> no change

With the defaults the fans creep down 2 % every 5 min while every sensor is at
least 6 C under its target, try 2 % lower every 30 min while it is at least
3 C under, and go up 5 % at a time once one is within 2 C (for full-authority
sensors such as the hottest Phy groups; proportionally less for weaker ones,
but never less than min_authority of it). PSU sensors, cooled by the PSUs'
own fans, are not weighted. Any reading at or over its target raises the fans
at once, unweighted; the next raise then waits up_interval unless the hottest
reading is higher, or one at target is still climbing. A probe (the trial
decrease; not --probe, the read-only report) that has to be undone by a raise
within probe_interval doubles the wait before the next probe, and one that
isn't undone halves it again (never below probe_interval, at most hold_max);
the back-off survives a restart. The band is wider than the temperature change
one down-step causes, so the fans settle instead of hunting; the slow probe
stops them getting stuck high after a warm spell.

It hands control back to EOS ('environment fan-speed auto') whenever anything
is not clearly fine, and stays in auto for a hold time before taking over
again:

    * a sensor more than release_excess C over its target, less than
      alert_margin C under its alert limit, in alert, or its alert count rising
    * still over target after ceiling_dwell s at the ceiling
    * inlet (ambient) temperature at or above ambient_max, or unreadable
    * temperature or cooling status not Ok; a fan, fan tray, PSU or sensor
      not ok; a sensor or fan disappearing; an implausible reading; a sensor
      without an alert limit (unless left out with --ignore-sensor)
    * the switch environment can't be read read_failures times in a row
    * EOS's overheat or insufficient-fans shutdown protection is disabled
    * an override it sets is rejected or doesn't take effect
    * fanctl itself is stopped (SIGTERM/SIGINT/SIGHUP) or fails

If someone else changes the fan setting while it runs, fanctl leaves the fans
to them (it still hands control to EOS if anything goes wrong) until it is
restarted. It never runs 'write memory'. fanctl-guard.sh covers fanctl dying
or hanging. Install, uninstall and tuning: see README.md.
"""

import argparse
import errno
import fcntl
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import traceback

try:
    import syslog
except ImportError:  # always present on EOS; only matters off-box
    syslog = None

VERSION = "1.4.0"      # fanctl-guard.sh keeps a copy (a test pins it): a pair is one release
PROG = "fanctl"

EOS_MIN_OVERRIDE = 30
EOS_MAX_OVERRIDE = 100

# Tunables: name -> (default, lowest, highest, unit). Every key is also a
# command-line option (poll -> --poll, keep_below -> --keep-below, and so on);
# check_config refuses a value outside its range, and checks the rules that
# relate two of them.
TUNABLES = {
    "poll": (15, 5, 120, "s"),                 # between reads
    "floor": (30, EOS_MIN_OVERRIDE, EOS_MAX_OVERRIDE, "%"),     # lowest override fanctl sets
    "ceiling": (70, EOS_MIN_OVERRIDE, EOS_MAX_OVERRIDE, "%"),   # highest override fanctl sets itself
    "keep_below": (2.0, 0, 15, "C"),           # raise the fans once the hottest sensor is this close to its target
    "band": (4.0, 0, 20, "C"),                 # lower them every down_interval when it is keep_below + band C under
    "up_step": (5, 1, 50, "%"),                # per increase
    "up_interval": (60, 0, 600, "s"),          # between increases (at target too, unless still getting hotter)
    "down_step": (2, 1, 20, "%"),              # per decrease
    "down_interval": (300, 1, 86400, "s"),     # since the last change before a decrease
    "probe_margin": (1.0, 0, 20, "C"),         # inside the band, still try a decrease when keep_below + this C under...
    "probe_interval": (1800, 1, 86400, "s"),   # ...and nothing has changed for this long
    "ceiling_dwell": (60, 0, 600, "s"),        # at the ceiling, still over target, before handing to EOS
    "smooth": (4, 1, 20, "polls"),             # in-band decisions use each sensor's mean over this many polls
    "min_authority": (0.5, 0.05, 1.0, ""),     # a weak sensor's margins shrink to no less than this share (see _weighted)
    "release_excess": (5.0, 0, 15, "C"),       # over target -> hand control back to EOS
    "resume_excess": (None, -30, 0, "C"),      # hottest sensor must be <= target + this to take over (default -keep_below)
    "alert_margin": (10.0, 3, 40, "C"),        # below an alert limit -> hand control back to EOS
    "ambient_max": (32.0, 15, 40, "C"),        # inlet -> hand control back to EOS (the switch is rated to 40 C)
    "ambient_resume": (None, 0, 40, "C"),      # inlet at or below which fanctl may take over again (default ambient_max - 2)
    "target_factor": (0.65, 0.3, 0.9, ""),     # target = factor x alert limit when EOS gives none
    "target_offset": (0.0, -20, 10, "C"),      # added to every target (+ = quieter, - = cooler)
    "hot_hold": (900, 0, 86400, "s"),          # in auto after the last too-hot reading
    "fault_hold": (600, 0, 86400, "s"),        # in auto after the last fault / unreadable poll
    "hold_max": (14400, 1, 86400, "s"),        # cap for holds that double because hand-offs keep repeating
    "flap_window": (7200, 0, 86400, "s"),      # a hand-off this soon after taking over doubles that kind's hold
    "read_failures": (2, 1, 10, ""),           # consecutive failed reads before handing back
    "cli_timeout": (30, 5, 120, "s"),          # allowed per FastCli call
    "reconcile": (300, 60, 86400, "s"),        # between running-config cross-checks
    "summary": (3600, 60, 86400, "s"),         # between syslog status summaries
}
DEFAULTS = {key: t[0] for key, t in TUNABLES.items()}
OPTION_TYPES = {"resume_excess": float, "ambient_resume": float}

SANE_MIN_C = 1.0       # an unpowered PSU reports 0.0 C; never treat that as "cool"
SANE_MAX_C = 150.0
STALE_AFTER = 300      # s without a heartbeat before --status and fanctl-guard.sh (a copy) call fanctl hung

RUN_DIR = "/run/fanctl"                                     # RAM, never flash; root-owned (check_dir)
STATE_FILE = os.path.join(RUN_DIR, "fanctl.status.json")    # heartbeat + status
LOCK_FILE = os.path.join(RUN_DIR, "fanctl.lock")            # held for life; fanctl-guard.sh checks it
MEMORY_FILE = os.path.join(RUN_DIR, "fanctl.memory.json")   # hold/back-off state that survives a restart
NOT_ROOT_HINT = ("run fanctl as root ('bash sudo ...') or point --state-file, --memory-file and "
                 "--lock-file at a directory you own")
CLI_CANDIDATES = ("/usr/bin/FastCli", "/usr/bin/Cli")
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

TEMP_CMD = "show system environment temperature | json"
COOLING_CMD = "show system environment cooling | json"
POWER_CMD = "show system environment power | json"
RUNNING_CMD = "show running-config | include ^environment fan-speed"
STARTUP_CMD = "show startup-config | include ^environment fan-speed"
OVERRIDE_RE = re.compile(r"^\s*environment fan-speed override (\d+)\s*$")

AUTO = ("auto", None)
QUICK_CLI_TIMEOUT = 5          # s, at most, per read confirming a fan change while stopping
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Shutdown(BaseException):
    """Raised from the signal handler so a blocking call stops at once.

    A BaseException (like KeyboardInterrupt) so that no 'except Exception'
    error handler can swallow a stop request."""

    def __init__(self, signum):
        BaseException.__init__(self, signum)
        self.signum = signum


class CliError(Exception):
    pass


# A stop signal that lands while a child is being spawned is held back until
# the child exists, so it can be killed instead of being orphaned.
_spawning = [False]
_pending_signals = []


def on_stop_signal(signum, frame):
    if _spawning[0]:
        _pending_signals.append(signum)
        return
    raise Shutdown(signum)


def _raise_pending():
    if _pending_signals:
        signum = _pending_signals[0]
        del _pending_signals[:]
        raise Shutdown(signum)


def _short(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _num(value):
    """A finite float from a JSON number, else None (bools are not numbers,
    nor is an int too large for any float)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _psu_id(label):
    """'PowerSupply1' -> '1' (the key used by the power and temperature JSON)."""
    m = re.search(r"\d+$", str(label or ""))
    return m.group() if m else str(label)


def fmt_setting(setting):
    if not setting:
        return "unknown"
    return "auto" if setting[0] == "auto" else "override %d%%" % setting[1]


def write_json(path, data):
    """Atomic write that won't follow a planted symlink."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class LockLost(Exception):
    """Another process holds the file now at fanctl's lock path."""


def is_file_at(fd, path):
    """True if open `fd` is the file at `path` (not one deleted or replaced since)."""
    try:
        a, b = os.lstat(path), os.fstat(fd)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def lock_is_held(path):
    """True if a running fanctl holds the lock, False if not, None if unknown."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | O_NOFOLLOW)   # a FIFO must not hang us
    except OSError as e:
        return False if e.errno == errno.ENOENT else None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError as e:
        return True if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) else None
    finally:
        os.close(fd)


def runtime_dirs(*paths):
    """The distinct directories that hold these files."""
    return sorted(set(os.path.dirname(os.path.abspath(p)) for p in paths))


def make_dir(path):
    """Create a missing runtime directory, one level only, mode 0755 whatever
    the umask (so any user can run --status). None if it exists now, else
    why not."""
    if os.path.lexists(path):
        return None
    parent, name = os.path.split(path)
    try:
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.mkdir(name, 0o755, dir_fd=pfd)
            # chmod through an fd: never follow a symlink swapped in meanwhile
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | O_NOFOLLOW, dir_fd=pfd)
            try:
                os.fchmod(fd, 0o755)
            finally:
                os.close(fd)
        finally:
            os.close(pfd)
    except OSError as e:
        if e.errno == errno.EEXIST:
            return None
        if e.errno in (errno.EACCES, errno.EPERM):
            return "%s; %s" % (e.strerror, NOT_ROOT_HINT)
        return e.strerror
    return None


def check_dir(path, writable=True):
    """None if `path` is a safe directory for the runtime files, else why not.

    Safe: a real directory (not a symlink) owned by root or this user that no
    one else can write to, inside directories owned by root or this user that
    no one else can write to or that are sticky (like /tmp: others can't
    rename or delete what isn't theirs). Checked both as written (where a
    root-owned symlink such as /var/run -> ../run may lead to it) and with
    every symlink resolved. Both read '..' as text, so parse_args refuses
    runtime paths with '..'. writable: this user must also be able to write
    to it."""
    euid = os.geteuid()
    path = os.path.abspath(path)
    for top in (path, os.path.realpath(path)):
        for d in _self_and_parents(top):
            problem = _unsafe_dir(d, d == top, "it" if d == path else d, euid)
            if problem:
                return problem
    if writable and not os.access(path, os.W_OK):
        return "this user cannot write to it; " + NOT_ROOT_HINT
    return None


def _self_and_parents(d):
    """d, its parent, and so on up to /."""
    while True:
        yield d
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def _unsafe_dir(d, top, name, euid):
    """Why d (`name` in the message) makes a runtime directory unsafe, or
    None. top: d is that directory itself, not one of its parents."""
    try:
        st = os.lstat(d)
    except OSError as e:
        return "cannot check %s: %s" % (name, e.strerror)
    if st.st_uid not in (0, euid):
        return "%s is owned by another user (uid %d)" % (name, st.st_uid)
    if top and not stat.S_ISDIR(st.st_mode):
        return "%s is %s" % (name, "a symlink" if stat.S_ISLNK(st.st_mode) else "not a directory")
    if _open_to_others(st.st_mode, top):
        return "%s is writable by group or others (mode %04o)" % (name, stat.S_IMODE(st.st_mode))
    return None


def _open_to_others(mode, top):
    """Others can write to it: a directory with group or other write
    permission, unless it is a parent with the sticky bit (like /tmp: others
    can't rename or delete what isn't theirs)."""
    return bool(stat.S_ISDIR(mode) and mode & 0o022 and (top or not mode & stat.S_ISVTX))


# --------------------------------------------------------------------------
# Logging: syslog facility local4 so messages show in 'show logging'.

class Log(object):
    def __init__(self, use_syslog=True, stream=None):
        self.stream = stream
        self.use_syslog = bool(use_syslog and syslog)
        self._last = {}
        if self.use_syslog:
            syslog.openlog(PROG, syslog.LOG_PID, syslog.LOG_LOCAL4)

    def __call__(self, level, msg):
        if self.use_syslog:
            prio = getattr(syslog, "LOG_" + level.upper())
            for line in str(msg).splitlines() or [""]:
                syslog.syslog(prio, line)
        if self.stream is not None:
            try:
                self.stream.write("%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                  level.upper(), msg))
                self.stream.flush()
            except (OSError, ValueError):
                pass   # a full /var/log must not stop the fan control

    def limited(self, key, level, msg, now, every=600):
        """Log msg unless the same key was logged less than `every` s ago."""
        last = self._last.get(key)
        if last is None or now - last >= every:
            self._last[key] = now
            self(level, msg)


# --------------------------------------------------------------------------
# Talking to EOS

class Cli(object):
    """Run EOS CLI commands through FastCli at privilege 15."""

    def __init__(self, path, timeout):
        self.path = path
        self.timeout = timeout

    def run(self, commands, timeout=None):
        """Run newline-joined commands; return stdout.

        Raises CliError on a timeout, a non-zero exit, or an EOS '% ...'
        error line on stdout or stderr."""
        timeout = timeout or self.timeout
        if not self.path:
            raise CliError("neither FastCli nor Cli was found")
        env = dict(os.environ)
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
        _spawning[0] = True
        try:
            proc = subprocess.Popen([self.path, "-p", "15", "-c", "\n".join(commands)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env,
                                    start_new_session=True)
        except OSError as e:
            _spawning[0] = False
            _raise_pending()
            raise CliError("cannot run %s: %s" % (self.path, e))
        try:
            # Stop signals deferred while spawning are raised here, inside
            # the try, so the child is killed rather than orphaned.
            _spawning[0] = False
            _raise_pending()
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise CliError("%s timed out after %ss" % (os.path.basename(self.path), timeout))
        except BaseException:
            # A stop signal arrived mid-call: never leave a config command
            # running behind us that could land after our final 'auto'.
            _kill(proc)
            raise
        return _cli_output(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _cli_output(returncode, out, err):
    """A finished FastCli call's stdout, or CliError if it failed."""
    if returncode != 0:
        raise CliError("exit status %s: %s" % (returncode, _short(err or out)))
    for line in (out + "\n" + err).splitlines():
        if line.lstrip().startswith("% "):
            raise CliError(_short(line))
    return out


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def find_cli():
    for path in CLI_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


_DECODER = json.JSONDecoder()
_LEADING_COMMENTS = re.compile(r"(?:\s|![^\n]*)*")    # whitespace and EOS '!' comment lines


def parse_json_doc(text):
    """The single JSON object in a '| json' command's output."""
    pos = _LEADING_COMMENTS.match(text).end()
    if pos >= len(text):
        raise ValueError("empty output")
    if text[pos] != "{":
        raise ValueError("not JSON: %s" % _short(text[pos:], 120))
    doc, end = _DECODER.raw_decode(text, pos)
    if text[end:].strip():
        raise ValueError("unexpected output after JSON: %s" % _short(text[end:], 120))
    if set(doc) == {"errors"}:
        raise ValueError("EOS returned errors: %s" % _short(doc["errors"], 120))
    return doc


def read_environment(cli):
    return [parse_json_doc(cli.run([cmd])) for cmd in (TEMP_CMD, COOLING_CMD, POWER_CMD)]


def read_fan_config(cli, cmd=RUNNING_CMD, timeout=None):
    """('override', N) if the config has 'environment fan-speed override N', else AUTO."""
    for line in cli.run([cmd], timeout=timeout).splitlines():
        m = OVERRIDE_RE.match(line)
        if m:
            return ("override", int(m.group(1)))
    return AUTO


def setting_from_cooling(cool):
    """The fan setting in effect per the cooling JSON, or None if it doesn't say.

    On EOS 4.28 (DCS-7050TX-64-R) an override shows as coolingMode 'manual'
    with overrideFanSpeed N; automatic control as coolingMode 'automatic'."""
    mode = cool.get("coolingMode")
    speed = _num(cool.get("overrideFanSpeed"))
    if mode == "automatic":
        return AUTO
    if mode == "manual" and speed is not None and speed >= EOS_MIN_OVERRIDE:
        return ("override", int(speed))
    return None


def current_setting(cli):
    """The fan setting in effect: the cooling JSON if it says, else running-config."""
    try:
        setting = setting_from_cooling(parse_json_doc(cli.run([COOLING_CMD])))
        if setting is not None:
            return setting
    except (CliError, ValueError):
        pass
    return read_fan_config(cli)


# How judge_setting sees the fan setting in effect at a poll.
SET_ADOPT = "adopt"       # fanctl didn't know it yet: take what the status (else running-config) shows
SET_OK = "ok"             # as fanctl last set or saw it, or nothing says otherwise
SET_LAG = "lag"           # the status differs, but running-config is as fanctl set it: the status lags
SET_UNSURE = "unsure"     # the status differs and running-config can't say: write nothing this poll
SET_CHANGED = "changed"   # someone else changed it


def _disagreeing(status, configured):
    """running-config's setting if it differs from a known status, else None."""
    return configured if status is not None and configured not in (None, status) else None


def judge_setting(status, configured, applied, mismatches):
    """(verdict, actual, mismatches, overruled) for a poll: status is the
    setting the cooling JSON shows, configured the one running-config holds,
    applied the one fanctl last set or saw (each None if unknown).
    mismatches counts the polls in doubt (SET_UNSURE) since the status or
    running-config last showed fanctl's setting; the second one believes the
    status. actual is the setting in effect for SET_ADOPT and SET_CHANGED,
    else None. overruled is the running-config setting the verdict set
    aside for the status, else None."""
    if applied is None:
        return SET_ADOPT, status or configured, mismatches, _disagreeing(status, configured)
    if status == applied or (status is None and configured in (None, applied)):
        return SET_OK, None, 0, _disagreeing(status, configured)
    if configured == applied:
        return SET_LAG, None, 0, None      # running-config just confirmed it
    if configured is not None:
        return SET_CHANGED, configured, 0, None
    mismatches += 1                        # running-config can't confirm it: write nothing
    if mismatches < 2:                     # this poll, and believe the status at the next
        return SET_UNSURE, None, mismatches, None
    return SET_CHANGED, status, 0, None


# --------------------------------------------------------------------------
# Turning raw JSON into a judgement

class Sensor(object):
    def __init__(self, name, label, kind, current, alert, target, target_src):
        self.name = name
        self.label = label
        self.kind = kind
        self.current = current
        self.alert = alert
        self.target = target
        self.target_src = target_src

    @property
    def excess(self):
        """C above (+) or below (-) this sensor's target."""
        return self.current - self.target

    @property
    def headroom(self):
        """C left before this sensor's alert limit."""
        return self.alert - self.current


class Reading(object):
    """One poll's worth of switch state, already judged."""

    def __init__(self):
        self.sensors = []       # usable Sensors
        self.skipped = []       # (label, why) not used for control
        self.faults = []        # equipment/data problems -> auto
        self.hot = []           # thermal problems -> auto
        self.ambient = None
        self.airflow = None
        self.setting = None     # fan setting in effect per the cooling JSON
        self.tray_speeds = []   # configuredSpeed of each fan-tray fan
        self.fan_count = 0      # fan-tray and PSU fans
        self.psus = {}          # id -> state
        self.alert_counts = {}
        self.seen = set()       # names of sensors used for control

    @property
    def worst(self):
        return max(self.sensors, key=lambda s: s.excess) if self.sensors else None

    @property
    def tightest(self):
        return min(self.sensors, key=lambda s: s.headroom) if self.sensors else None

    @property
    def excess(self):
        w = self.worst
        return w.excess if w else None

    @property
    def healthy(self):
        return not (self.faults or self.hot)


EOS_TARGET_KEYS = ("setPointTemperature", "targetTemperature")


def pick_target(raw, alert, cfg):
    """EOS's own control target for a sensor, plus the configured offset."""
    best = None
    for key in EOS_TARGET_KEYS:
        v = _celsius(raw.get(key))
        if v is not None and v < alert and (best is None or v < best[0]):
            best = (v, key)
    if best is None:
        best = (cfg.target_factor * alert, "%g x alert" % cfg.target_factor)
    return best[0] + cfg.target_offset, best[1]


def describe_guessed_targets(r, cfg):
    """A warning naming the sensors whose target is not EOS's own, or None."""
    guessed = [s.label for s in r.sensors if s.target_src not in EOS_TARGET_KEYS]
    if not guessed:
        return None
    return ("%d sensor(s) report no usable EOS setpoint (none, or not between 1 C and the alert limit), "
            "so their target is %g x alert limit, a ratio measured on a DCS-7050TX-64-R: %s. "
            "Check that those targets suit this model (--target-offset moves every target)"
            % (len(guessed), cfg.target_factor, ", ".join(guessed)))


def _list(value):
    return value if isinstance(value, list) else None


def _int(value):
    """An int from JSON, else None (bools are not numbers)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _celsius(value):
    """A plausible temperature from JSON, else None."""
    value = _num(value)
    return value if value is not None and SANE_MIN_C <= value <= SANE_MAX_C else None


def assess(docs, cfg, prev_alert_counts=None, baseline=None):
    """Judge one poll's temperature, cooling and power JSON. The Reading's
    faults and hot lists say why EOS should have the fans, if it should.
    prev_alert_counts: each sensor's alertCount at the last poll; baseline:
    the sensors and fan count of the last poll without a fault (Daemon.read)."""
    temp, cool, power = docs
    r = Reading()
    baseline = baseline or {}
    ignore_psus = set(cfg.ignore_psu)
    bad_psus = _assess_power(r, power, ignore_psus)
    _assess_temperatures(r, temp, cfg, ignore_psus, bad_psus, prev_alert_counts or {})
    missing = baseline.get("sensors", set()) - r.seen
    if missing:
        r.faults.append("sensor(s) no longer usable: %s" % ", ".join(sorted(missing)[:5]))
    _assess_cooling(r, cool, cfg)
    # Fans: the fan trays', and each PSU's own (it cools that PSU). Every fan
    # counts toward r.fan_count, so one vanishing from either is a fault.
    _assess_tray_fans(r, cool)
    _assess_psu_fans(r, cool, ignore_psus)
    if r.fan_count < baseline.get("fans", 0):
        r.faults.append("fan count dropped from %d to %d" % (baseline["fans"], r.fan_count))
    return r


def _assess_power(r, power, ignore_psus):
    """Record each PSU's state; return the ids of those not ok. A PSU without
    input power still reports its fan as ok and its sensors as 0.0 C, so its
    'state' here is what counts."""
    psus = power.get("powerSupplies")
    if not isinstance(psus, dict) or not psus:
        r.faults.append("no power-supply data")
        return set()
    bad = set()
    for pid in sorted(psus):
        if pid in ignore_psus:
            continue
        p = psus[pid]
        state = p.get("state") if isinstance(p, dict) else None
        r.psus[pid] = state
        if state != "ok":
            bad.add(pid)
            r.faults.append("PSU %s state is %r" % (pid, state))
    return bad


def _assess_temperatures(r, temp, cfg, ignore_psus, bad_psus, prev_alert_counts):
    if temp.get("systemStatus") != "temperatureOk":
        r.faults.append("temperature status is %r" % temp.get("systemStatus"))
    if temp.get("shutdownOnOverheat") is False:
        r.faults.append("EOS overheat shutdown is disabled ('environment overheat action'); "
                        "fanctl will not lower the fans")
    for kind, prefix, unusable, sensors in _temp_groups(r, temp, ignore_psus, bad_psus):
        if not isinstance(sensors, list):
            r.faults.append("unexpected %stemperature data" % prefix)
            continue
        for raw in sensors:
            _assess_sensor(r, raw, kind, prefix, unusable, cfg, prev_alert_counts)
    if not any(s.kind == "system" for s in r.sensors):
        r.faults.append("no usable system temperature sensors")


def _temp_groups(r, temp, ignore_psus, bad_psus):
    """(kind, label prefix, why its sensors can't be used or None, sensors)
    for the system's sensors and each PSU's and card's, minus ignored PSUs."""
    groups = [("system", "", None, temp.get("tempSensors"))]
    for kind, key, name, ignored, bad in (("psu", "powerSupplySlots", "PSU", ignore_psus, bad_psus),
                                          ("card", "cardSlots", "Card", (), ())):
        slots = temp.get(key, [])
        if not isinstance(slots, list):
            r.faults.append("unexpected %s data" % key)
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                r.faults.append("unexpected %s entry" % key)
                continue
            pos = str(slot.get("relPos"))
            if pos in ignored:
                continue
            groups.append((kind, "%s%s " % (name, pos), "its PSU is not ok" if pos in bad else None,
                           slot.get("tempSensors")))
    return groups


def _assess_sensor(r, raw, kind, prefix, unusable, cfg, prev_alert_counts):
    """Judge one temperature sensor; use it for control if it can be."""
    if not isinstance(raw, dict):
        r.faults.append("unexpected %stemperature entry" % prefix)
        return
    name = str(raw.get("name") or "?")
    desc = str(raw.get("description") or "")
    label = "%s%s (%s)" % (prefix, desc or name, name)
    skip = unusable or _ignored(cfg, name, desc)
    if skip:
        r.skipped.append((label, skip))
        return
    cur = _sensor_reading(r, raw, label)
    if cur is None:
        return
    _count_alarms(r, raw, name, label, cur, prev_alert_counts)
    alert = _alert_limit(r, raw, label)
    if alert is None:
        return
    s = Sensor(name, label, kind, cur, alert, *pick_target(raw, alert, cfg))
    r.sensors.append(s)
    r.seen.add(name)
    r.hot.extend(_margin_problems(s, cfg))


def _ignored(cfg, name, desc):
    """Why the operator's --ignore-sensor leaves this sensor out, or None."""
    return "ignored (--ignore-sensor)" if name in cfg.ignore_sensor or desc in cfg.ignore_sensor else None


def _alert_limit(r, raw, label):
    """A sensor's alert limit, or None once the fault is recorded."""
    alert = _celsius(raw.get("overheatThreshold"))
    if alert is None:
        # Without a limit fanctl can't tell how close it is to trouble.
        # Leaving it out is for the operator to decide (--ignore-sensor).
        r.faults.append("%s has no usable alert limit (overheatThreshold %r)" % (label, raw.get("overheatThreshold")))
        return None
    return alert


def _sensor_reading(r, raw, label):
    """A sensor's current temperature, or None once the fault is recorded."""
    hw = raw.get("hwStatus")
    if hw != "ok":
        r.faults.append("%s status is %r" % (label, hw))
        return None
    cur = _celsius(raw.get("currentTemperature"))
    if cur is None:
        r.faults.append("%s reading %r is not plausible" % (label, raw.get("currentTemperature")))
    return cur


def _count_alarms(r, raw, name, label, cur, prev_alert_counts):
    """EOS's own alarms, which count for every sensor with a reading."""
    if raw.get("inAlertState") is True:
        r.hot.append("%s is in alert at %.1f C" % (label, cur))
    count = _int(raw.get("alertCount"))
    if count is None:
        return
    r.alert_counts[name] = count
    prev = prev_alert_counts.get(name)
    if prev is not None and count > prev:
        r.hot.append("%s alert count rose to %d" % (label, count))


def _margin_problems(s, cfg):
    """Why Sensor s is too hot for fanctl to keep the fans (empty if it isn't)."""
    hot = []
    if s.headroom < cfg.alert_margin:
        hot.append("%s at %.1f C is within %.0f C of its %.0f C alert limit"
                   % (s.label, s.current, cfg.alert_margin, s.alert))
    if s.excess > cfg.release_excess:
        hot.append("%s at %.1f C is %.1f C over its %.1f C target" % (s.label, s.current, s.excess, s.target))
    return hot


def _assess_cooling(r, cool, cfg):
    """Cooling status, EOS's own protection, airflow, inlet, and the fan setting in effect."""
    if cool.get("systemStatus") != "coolingOk":
        r.faults.append("cooling status is %r" % cool.get("systemStatus"))
    if cool.get("shutdownOnInsufficientFans") is False:
        r.faults.append("EOS insufficient-fans shutdown is disabled; fanctl will not lower the fans")
    r.airflow = cool.get("airflowDirection")
    if not isinstance(r.airflow, str) or "unknown" in r.airflow.lower():
        r.faults.append("airflow direction is %r" % r.airflow)
    r.ambient = _celsius(cool.get("ambientTemperature"))
    if r.ambient is None:
        r.faults.append("inlet (ambient) temperature %r is not plausible" % cool.get("ambientTemperature"))
    elif r.ambient >= cfg.ambient_max:
        r.hot.append("inlet (ambient) %.1f C is at or above %.1f C" % (r.ambient, cfg.ambient_max))
    r.setting = setting_from_cooling(cool)


def _check_fans(r, owner, fans):
    """Count and check one fan tray's or PSU's fans; return the ok ones."""
    fans = _list(fans)
    if not fans:
        r.faults.append("%s reports no fans" % owner)
        return []
    ok = []
    for fan in fans:
        r.fan_count += 1
        if isinstance(fan, dict) and fan.get("status") == "ok":
            ok.append(fan)
        else:
            r.faults.append("fan %s status is %r" % (
                fan.get("label") if isinstance(fan, dict) else "?",
                fan.get("status") if isinstance(fan, dict) else fan))
    return ok


def _assess_tray_fans(r, cool):
    trays = _list(cool.get("fanTraySlots"))
    if not trays:
        r.faults.append("no fan tray data")
        return
    for tray in trays:
        if not isinstance(tray, dict):
            r.faults.append("unexpected fan tray entry")
            continue
        if tray.get("status") != "ok":
            r.faults.append("fan tray %s status is %r" % (tray.get("label"), tray.get("status")))
        for fan in _check_fans(r, "fan tray %s" % tray.get("label"), tray.get("fans")):
            speed = _num(fan.get("configuredSpeed"))
            if speed is not None:
                r.tray_speeds.append(speed)


def _assess_psu_fans(r, cool, ignore_psus):
    slots = cool.get("powerSupplySlots", [])   # as in the temperature JSON: absent is no PSU fans
    if not isinstance(slots, list):
        r.faults.append("unexpected powerSupplySlots fan data")
        return
    for slot in slots:
        if not isinstance(slot, dict):
            r.faults.append("unexpected PSU fan entry")
            continue
        if _psu_id(slot.get("label")) in ignore_psus:
            continue
        if slot.get("status") != "ok":
            r.faults.append("%s fan status is %r" % (slot.get("label"), slot.get("status")))
        _check_fans(r, str(slot.get("label")), slot.get("fans"))


# --------------------------------------------------------------------------
# The decision logic (no I/O, so it can be tested and simulated)

MEMORY_NUMBERS = ("wall", "hold_left", "hot_hold", "fault_hold", "since_resume",
                  "prev_speed", "prev_excess", "prev_ambient", "backoff")


def parse_memory(m):
    """The memory file's contents (Controller.snapshot plus 'wall'), checked.

    Each number is finite or None (missing: a file from another version);
    'reasons' is a list of strings. Raises ValueError naming the first bad
    field, so a damaged file is ignored whole, never half-used."""
    if not isinstance(m, dict):
        raise ValueError("not a JSON object")
    out = {key: _memory_number(m, key) for key in MEMORY_NUMBERS}
    if out["wall"] is None:
        raise ValueError("no wall time")
    out["hold_left"] = out["hold_left"] or 0.0
    reasons = m.get("reasons") or []
    if not (isinstance(reasons, list) and all(isinstance(x, str) for x in reasons)):
        raise ValueError("reasons is %r, not a list of strings" % (reasons,))
    out["reasons"] = list(reasons)
    return out


def _memory_number(m, key):
    value = m.get(key)
    if value is None:
        return None
    number = _num(value)
    if number is None:
        raise ValueError("%s is %r, not a finite number" % (key, value))
    return number

# What a poll's decision did: Decision.event, and "event" in the status file.
EV_UP = "up"                       # raised the override
EV_DOWN = "down"                   # lowered it
EV_PROBE = "probe"                 # lowered it on trial (see regulate)
EV_RESUME = "resume"               # took the fans over from EOS
EV_RELEASE = "release"             # handed them to EOS
EV_HOLD = "hold"                   # EOS has them: a hold, or waiting for the resume conditions
EV_RETRY = "retry"                 # a read failed; the setting stays as it is
EV_PAUSED = "paused"               # someone else set the fans: left alone
EV_PAUSED_ALARM = "paused-alarm"   # ...and something is hot or faulty
EV_STARTING = "starting"           # status file only: before the first poll

# The events that change the fan setting, as the log (and --probe) names them.
CHANGES = {EV_RELEASE: "handing fan control to EOS (auto)", EV_RESUME: "taking fan control",
           EV_UP: "raising fans", EV_DOWN: "lowering fans", EV_PROBE: "trying fans lower"}


class Decision(object):
    """What a poll decided and why. The setting it wants is the Controller's
    (Controller.setting), which only the Controller changes."""

    def __init__(self, event, why):
        self.event = event    # None (nothing to do) or an EV_ constant
        self.why = why        # list of strings


def change_line(d, want):
    """The line the daemon logs when Decision d puts `want` into effect, and
    --probe prints for it."""
    return "%s -> %s: %s" % (CHANGES.get(d.event, "setting fans"), fmt_setting(want),
                             "; ".join(d.why[:4]) or "re-applying")


def describe_hottest(r):
    w = r.worst
    return "hottest vs target: %s %.1f C (target %.1f C, %+.1f C); alert limit %.0f C" % (
        w.label, w.current, w.target, w.excess, w.alert)


def describe_binding(b):
    s, weight = b
    return "binding: %s %.1f C (target %.1f C, %+.1f C, fan authority %.2f); alert limit %.0f C" % (
        s.label, s.current, s.target, s.excess, weight, s.alert)


class Controller(object):
    def __init__(self, cfg, setting, now):
        self.cfg = cfg
        self.setting = setting if setting and setting[0] == "override" else AUTO   # wanted
        self.last_change = now     # startup counts as a change: no instant decrease
        self.up_excess = None      # hottest excess at the last raise, if the last change was one
        self.failures = 0
        self.hold_until = None
        self.hot_hold = cfg.hot_hold
        self.fault_hold = cfg.fault_hold
        self.last_resume = None
        self.release_reasons = []
        self.ceiling_since = None
        self.healthy = None            # (hottest excess, inlet) at the latest healthy override poll
        self.resume_from = None        # (speed, hottest excess, inlet) before a fault hand-off:
                                       # where to resume if nothing got hotter
        self.paused = False            # someone else changed the fans: leave them alone
        self.manual = None             # ...to this setting
        self.samples = {}              # sensor name -> its last `smooth` readings
        self.recent = []               # (time, weighted smoothed excess), last probe_interval s
        self.binding = None            # (Sensor, authority weight) setting the pace, last poll
        self.weights = {}              # sensor name -> authority weight, last poll
        self.backoff = 1.0             # x probe_interval; doubles while probes keep being undone
        self.last_probe = None         # time of the last probe not yet known to have held

    # -- persistence across restarts (fanctl.memory.json) --
    def snapshot(self, now):
        prev_speed, prev_excess, prev_ambient = self.resume_point() or (None, None, None)
        return {"hold_left": self.hold_left(now),
                "hot_hold": self.hot_hold, "fault_hold": self.fault_hold,
                "since_resume": None if self.last_resume is None else now - self.last_resume,
                "prev_speed": prev_speed, "prev_excess": prev_excess, "prev_ambient": prev_ambient,
                "reasons": self.release_reasons[:3],
                "backoff": self.backoff}

    def resume_point(self):
        """(speed, hottest excess, inlet) to resume from after a fault hand-off,
        or None: the one saved at the hand-off while EOS has the fans, else the
        current speed and the latest healthy poll's readings."""
        if self.setting == AUTO or self.paused:   # paused: the speed isn't fanctl's
            return self.resume_from
        return (self.speed,) + self.healthy if self.healthy else None

    def restore(self, m, now, age):
        """Carry on from a memory checked by parse_memory, `age` s old."""
        c = self.cfg
        self.hold_until = now + min(m["hold_left"], c.hold_max) - age   # past, if it ran out meanwhile
        self.release_reasons = m["reasons"] or ["before restart"]
        self.hot_hold = min(c.hold_max, max(c.hot_hold, int(m["hot_hold"] or c.hot_hold)))
        self.fault_hold = min(c.hold_max, max(c.fault_hold, int(m["fault_hold"] or c.fault_hold)))
        if m["since_resume"] is not None:
            self.last_resume = now - m["since_resume"] - age
        if None not in (m["prev_speed"], m["prev_excess"], m["prev_ambient"]):
            self.resume_from = (int(m["prev_speed"]), m["prev_excess"], m["prev_ambient"])
        if m["backoff"] is not None:
            self.backoff = min(self.max_backoff(), max(1.0, m["backoff"]))

    # -- decisions --
    def start_speed(self, r):
        """Take over from EOS at the speed EOS is running now (within floor..ceiling)."""
        c = self.cfg
        current = max(r.tray_speeds) if r.tray_speeds else c.ceiling
        return int(min(c.ceiling, max(c.floor, math.ceil(current))))

    def _weighted(self, now, r):
        """The hottest excess, each sensor's weighted by its fan authority.

        The fans move a sensor's temperature in proportion to its rise over
        the inlet (measured on a DCS-7050TX-64-R: rise ~ fan ** -0.8 for all
        that heats). A sensor with a small rise, such as the front panel, which
        mostly follows the inlet, can hardly be cooled by them. Each sensor's
        excess is divided by w = its rise / the largest rise (but w is at least
        min_authority), so every excess is in degrees of the most fan-sensitive
        sensor. The band thresholds then ask the same fan effort of every
        sensor, and the fans no longer chase one they can't move. A weak
        sensor is held within keep_below * w of its target instead of
        keep_below, never closer than keep_below * min_authority.
        PSU sensors are cooled by the PSUs' own fans: they neither set the
        largest rise nor get weighted. Over-target readings still act
        unweighted: the immediate raise at target, and every hand-off in assess().

        Each sensor's mean over the last `smooth` polls is used, because sensor
        noise (the Phy sensors read in whole degrees) would otherwise trigger
        a raise on a single high sample.

        Returns (latest, smooth, binding): the weighted hottest excess of the
        latest readings and of the means, and the (Sensor, weight) with that
        hottest mean. binding and the weights are also kept, as the last
        poll's, for the status file and summary."""
        c = self.cfg
        rows = self._smoothed(r)
        fan_cooled = [rise for s, _x, rise in rows if s.kind != "psu"]
        top = max(fan_cooled) if fan_cooled else 1.0
        rows = [(s, x, 1.0 if s.kind == "psu" else max(c.min_authority, min(1.0, rise / top)))
                for s, x, rise in rows]
        self.weights = {s.name: weight for s, _x, weight in rows}
        hottest, x, weight = max(rows, key=lambda row: row[1] / row[2])   # the first, on a tie
        smooth, self.binding = x / weight, (hottest, weight)
        latest = max(s.excess / w for s, _x, w in rows)
        self.recent.append((now, smooth))
        self.recent = self._recent(now, max(c.down_interval, c.probe_interval))
        return latest, smooth, self.binding

    def _smoothed(self, r):
        """(sensor, mean excess, mean rise over the inlet, at least 1 C) for
        each sensor, from its mean over the last `smooth` polls."""
        rows = []
        for s in r.sensors:
            q = self.samples.setdefault(s.name, [])
            q.append(s.current)
            del q[:-self.cfg.smooth]
            mean = sum(q) / len(q)
            rows.append((s, mean - s.target, max(1.0, mean - r.ambient)))
        return rows

    def max_backoff(self):
        return max(1.0, self.cfg.hold_max / float(self.cfg.probe_interval))

    def _recent(self, now, window):
        """The (time, weighted smoothed excess) of the polls in the last `window`
        s, the one exactly `window` s ago included."""
        return [(t, e) for t, e in self.recent if now - t <= window]

    def _max_recent(self, now, window):
        return max((e for _t, e in self._recent(now, window)), default=float("inf"))

    @property
    def speed(self):
        """The override % wanted, or None for EOS automatic control."""
        return self.setting[1]

    def pause(self, setting):
        """Someone else set the fans: from now on leave them as set (until restart)."""
        self.paused, self.manual, self.setting = True, setting, setting

    def decide(self, now, r, outside=None):
        """This poll's Decision, from the reading r (None: unreadable) and the
        fan setting someone else has just put in effect, if any."""
        if outside is not None:
            self.pause(outside)
        if r is None:
            return self._read_failed(now)
        self.failures = 0
        if not r.healthy:
            return self._problem(now, r.faults + r.hot, hot=bool(r.hot), fault=bool(r.faults))
        latest, smooth, binding = self._weighted(now, r)
        if self.paused:
            return Decision(EV_PAUSED,
                            ["fans %s, set outside fanctl; fanctl leaves them alone until restarted"
                             % fmt_setting(self.manual)])
        if self.setting == AUTO:
            blocker = self._hold_blocker(now) or self._resume_blocker(r, smooth, binding)
            return Decision(EV_HOLD, [blocker]) if blocker else self._resume(now, r)
        return self._in_control(now, r, smooth, latest, binding)

    def _read_failed(self, now):
        self.failures += 1
        if self.failures < self.cfg.read_failures:
            return Decision(EV_RETRY,
                            ["read failed (%d of %d allowed); keeping current setting"
                             % (self.failures, self.cfg.read_failures - 1)])
        return self._problem(now, ["switch environment unreadable %d times in a row"
                                   % self.failures], hot=False, fault=True)

    def _in_control(self, now, r, smooth, latest, binding):
        """fanctl has the fans and the reading is healthy: keep them within
        floor..ceiling, hand them to EOS after ceiling_dwell s at the ceiling
        still at target, else regulate."""
        c = self.cfg
        e = r.excess
        self.healthy = (e, r.ambient)
        if self.speed < c.floor:
            return self._set(now, c.floor, EV_UP, r, describe_binding(binding))
        if self._dwelt_at_ceiling(now, e):
            return self.release(now, ["fans at the %d%% ceiling for %d s and %s" % (
                self.speed, now - self.ceiling_since, describe_hottest(r))], hot=True, fault=False)
        return self.regulate(now, r, e, smooth, latest, binding)

    def _dwelt_at_ceiling(self, now, e):
        """Track how long the fans have been at the ceiling: True once they
        have been there ceiling_dwell s and a reading is at or over target."""
        if self.speed < self.cfg.ceiling:
            self.ceiling_since = None
            return False
        if self.ceiling_since is None:
            self.ceiling_since = now
        return e >= 0 and now - self.ceiling_since >= self.cfg.ceiling_dwell

    def regulate(self, now, r, e, smooth, latest, binding):
        """The in-band speed decision (band control). Everything that hands
        control to EOS happens before this is called.

        e: hottest raw excess (C vs target, unweighted); smooth, latest,
        binding: the authority-weighted hottest excess of each sensor's recent
        mean and of the latest readings, and the sensor setting the pace (see
        _weighted)."""
        c = self.cfg
        if self.last_probe is not None and now - self.last_probe >= c.probe_interval:
            # The last probe wasn't undone: let the next one come sooner again.
            self.backoff = max(1.0, self.backoff / 2.0)
            self.last_probe = None
        if e >= 0 or smooth > -c.keep_below:
            return self._raise(now, r, e, binding)
        if self.speed > c.floor:
            return self._lower(now, r, latest, binding)
        return Decision(None, [])

    def _raise(self, now, r, e, binding):
        """At target, or within keep_below of it: raise at once unless the last
        change was a raise and it hasn't got hotter since (_hotter); then give
        the fans up_interval to work."""
        c = self.cfg
        if self.speed < c.ceiling and (self._hotter(r, e) or now - self.last_change >= c.up_interval):
            why = "at target: " + describe_hottest(r) if e >= 0 else describe_binding(binding)
            return self._set(now, min(c.ceiling, self.speed + c.up_step), EV_UP, r, why)
        return Decision(None, [])

    def _lower(self, now, r, latest, binding):
        """Lower only if it is that cool now and stayed so over the whole wait:
        a step down, or a probe (a step down on trial) after a longer wait."""
        c = self.cfg
        lower = max(c.floor, self.speed - c.down_step)
        if self._cool(now, latest, c.keep_below + c.band, c.down_interval, c.down_interval):
            return self._set(now, lower, EV_DOWN, r, describe_binding(binding))
        if self._cool(now, latest, c.keep_below + c.probe_margin, c.probe_interval * self.backoff,
                      c.probe_interval / 2):
            return self._set(now, lower, EV_PROBE, r, describe_binding(binding))
        return Decision(None, [])

    def _hotter(self, r, e):
        """At target and hotter than at the last raise, if the last change was
        one: a hottest excess above the one at that raise, or a reading at
        target that rose more than 1 C over the last `smooth` polls. Rounding
        to whole degrees can't explain that, so a load is still building even
        if the reading now sits still. A single 1 C step can be rounding, and
        the means still catching up with it don't count."""
        if e < 0:
            return False
        if self.up_excess is None or e > self.up_excess:
            return True
        return any(s.excess >= 0 and self.samples[s.name][-1] - self.samples[s.name][0] > 1.0
                   for s in r.sensors)

    def _cool(self, now, latest, under, wait, window):
        """At least `under` C below target (weighted) in the latest readings and
        in every poll of the last `window` s, and `wait` s since the last change."""
        return (now - self.last_change >= wait and latest < -under
                and self._max_recent(now, window) < -under)

    def _set(self, now, speed, event, r, why=None):
        if event == EV_UP and self.last_probe is not None:
            # A raise before the last probe has held (regulate): it went too
            # far. Wait longer before the next one, as holds do, so the fans
            # don't keep probing into the same raise.
            self.backoff = min(2 * self.backoff, self.max_backoff())
            self.last_probe = None
        elif event == EV_PROBE:
            self.last_probe = now
        self.setting, self.last_change = ("override", int(speed)), now
        self.up_excess = r.excess if event == EV_UP else None
        if why is None:
            why = describe_hottest(r)
        return Decision(event, [why])

    def _resume(self, now, r):
        c = self.cfg
        speed = self.start_speed(r)
        if self.resume_from is not None:
            prev_speed, prev_excess, prev_ambient = self.resume_from
            if r.excess <= prev_excess + 0.5 and r.ambient <= prev_ambient + 1.0:
                # back after a fault: the old speed was holding these temperatures fine
                speed = max(c.floor, min(speed, prev_speed))
        self.resume_from = None
        self.setting = ("override", speed)
        self.last_change = self.last_resume = now
        self.up_excess = None
        self.ceiling_since, self.last_probe = None, None
        self.healthy = (r.excess, r.ambient)
        return Decision(EV_RESUME, [describe_hottest(r)])

    def _problem(self, now, reasons, hot, fault):
        if not self.paused:
            return self.release(now, reasons, hot=hot, fault=fault)
        # Someone else set the fans. Respect a high override (an operator's
        # emergency 100 %) or auto; only a low override is handed to EOS.
        if self.manual[0] == "override" and self.manual[1] < self.cfg.ceiling:
            self.manual = AUTO
            return self.release(now, reasons, hot=hot, fault=fault)
        if self.setting == AUTO:
            # Already in auto (that hand-off, or an operator's auto): keep the
            # hold running from the latest problem, for the restart that
            # un-pauses fanctl. In auto, release() only extends the hold.
            self.release(now, reasons, hot=hot, fault=fault)
        return Decision(EV_PAUSED_ALARM, list(reasons))

    def release(self, now, reasons, *, hot, fault):
        """Go to (or stay in) EOS automatic control and (re)start the hold
        for a hot reading, a fault, or both."""
        assert hot or fault, "a hand-off needs a hot or a fault reason"
        event = EV_HOLD
        if self.setting != AUTO:
            self._hand_off(now, hot, fault)
            event = EV_RELEASE
        if hot:
            self.resume_from = None   # that speed was not enough
        hold = max(self.hot_hold if hot else 0, self.fault_hold if fault else 0)
        if self.hold_until is None or now + hold > self.hold_until:
            self.hold_until = now + hold
        self.release_reasons = list(reasons)
        return Decision(event, list(reasons))

    def _hand_off(self, now, hot, fault):
        """Give the fans to EOS. A hand-off of the same kind less than
        flap_window after taking over doubles that kind's hold; a fault keeps
        the speed to resume from (resume_point)."""
        c = self.cfg
        recent = self.last_resume is not None and now - self.last_resume < c.flap_window
        if hot:
            self.hot_hold = min(self.hot_hold * 2, c.hold_max) if recent else c.hot_hold
        if fault:
            self.fault_hold = min(self.fault_hold * 2, c.hold_max) if recent else c.fault_hold
            self.resume_from = self.resume_point()
        self.setting, self.last_change = AUTO, now
        self.up_excess = None

    def _hold_blocker(self, now):
        """Why fanctl must leave the fans to EOS for now, if a hand-off's hold
        is still running, else None."""
        left = self.hold_left(now)
        if left:
            return "holding EOS auto control for %d s more (after: %s)" % (
                left, "; ".join(self.release_reasons[:3]))
        return None

    def hold_left(self, now):
        """s until the hand-off hold ends: 0 once it has, or if there is none."""
        return max(0.0, self.hold_until - now) if self.hold_until is not None else 0.0

    def _resume_blocker(self, r, smooth, binding):
        """Why the readings don't let fanctl take the fans over from EOS yet,
        or None."""
        c = self.cfg
        if smooth > c.resume_excess:
            s, weight = binding
            return ("%s at %.1f C must be at most %.1f C (target %+.1f C, fan authority %.2f) "
                    "before fanctl takes over" % (s.label, s.current, s.target + c.resume_excess * weight,
                                                  c.resume_excess * weight, weight))
        if r.ambient > c.ambient_resume:
            return "inlet %.1f C must be at most %.1f C before fanctl takes over" % (
                r.ambient, c.ambient_resume)
        t = r.tightest
        if t.headroom < c.alert_margin + c.keep_below:
            return "%s is only %.1f C below its alert limit" % (t.label, t.headroom)
        if r.setting in (None, AUTO) and r.tray_speeds and max(r.tray_speeds) > c.ceiling:
            # EOS's own evidence that the switch needs more cooling than fanctl
            # allows, perhaps for heat fanctl can't see (optics, a skipped sensor)
            return "EOS runs the fans at %d%%, above fanctl's %d%% ceiling" % (
                max(r.tray_speeds), c.ceiling)
        return None


# --------------------------------------------------------------------------
# The daemon: read -> decide -> apply -> verify, forever

class Daemon(object):
    def __init__(self, cfg, cli, log, state_file=STATE_FILE, memory_file=MEMORY_FILE,
                 dry_run=False, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.cli = cli
        self.log = log
        self.state_file = state_file
        self.memory_file = memory_file
        self.dry_run = dry_run
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.applied = None        # fan setting in effect, as last observed
        self.dry_setting = None
        self.ctrl = None
        self.controlling = False
        self.alert_counts = {}
        self.baseline = None
        self.last_reconcile = None
        self.mismatches = 0        # polls in doubt about the setting in effect (judge_setting)
        self.lock_path = None
        self.last_summary = None
        self.auto_refused_until = None
        self.changes = []          # monotonic times of config changes
        self.last_reading = None
        self.last_decision = None
        self._lock_fd = None

    # -- lifecycle --
    def run(self, lock_file=LOCK_FILE):
        # ProcMgr's umask, also for a run by hand (sudo keeps a stricter one):
        # the runtime files must be readable by anyone's --status.
        os.umask(0o022)
        if not self._runtime_dirs_ready(lock_file):
            return 1
        for sig in STOP_SIGNALS:
            signal.signal(sig, on_stop_signal)
        why, rc = "exiting", 0
        try:
            self._lock(lock_file)
            # A heartbeat before the first (slow) poll, so fanctl-guard never
            # mistakes a starting fanctl for a dead one.
            self._write_status_dict({"version": VERSION, "pid": os.getpid(), "time": self.wall(),
                                     "dry_run": self.dry_run, "paused": False, "event": EV_STARTING})
            self.startup(self.clock())
            self._warn_unwatched(lock_file)
            self.controlling = True
            self._poll_forever()
        except Shutdown as e:
            why = "stopped by signal %d" % e.signum
        except LockLost as e:
            # The new holder owns the fans and the runtime files now.
            self.log("err", "%s; stopping, and leaving the fans, heartbeat and memory to it" % e)
            os.close(self._lock_fd)
            self._lock_fd, self.controlling, rc = None, False, 1
        except Exception:
            why, rc = "internal error", 1
            self.log("err", "internal error:\n" + traceback.format_exc())
        finally:
            self._shut_down(why)
        return rc

    def _warn_unwatched(self, lock_file):
        """Say so if the guard won't see this fanctl's lock or heartbeat:
        under the daemon it would then restore auto every minute."""
        moved = ["--%s %s" % (opt, path) for opt, path, default in (
            ("lock-file", lock_file, LOCK_FILE), ("state-file", self.state_file, STATE_FILE)) if path != default]
        if moved and not self.dry_run:
            self.log("warning", "%s: fanctl-guard.sh watches %s and %s unless its FANCTL_LOCK and FANCTL_STATE "
                     "name others; if it doesn't see this fanctl, it restores automatic control every minute "
                     "and fanctl pauses. These options are for tests and runs by hand"
                     % (" and ".join(moved), LOCK_FILE, STATE_FILE))

    def _runtime_dirs_ready(self, lock_file):
        """Before anything else: the lock, heartbeat and memory must be where
        no other user can plant, fake or tamper with them."""
        for d in runtime_dirs(lock_file, self.state_file, self.memory_file):
            problem = make_dir(d)
            if problem:
                self.log("err", "cannot create runtime directory %s: %s; not starting" % (d, problem))
                return False
            problem = check_dir(d)
            if problem:
                self.log("err", "unsafe runtime directory %s: %s; not starting" % (d, problem))
                return False
        return True

    def _poll_forever(self):
        while True:
            start = self.clock()
            self.tick(start)
            delay = self.cfg.poll - (self.clock() - start)
            if delay > 0:
                self.sleep(delay)

    def _shut_down(self, why):
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        del _pending_signals[:]
        self.stop(why)

    def stop(self, why):
        """Leave the heartbeat, memory and fans as a stopping fanctl must:
        run()'s last step, and the simulator's stop."""
        if self._lock_fd is not None:
            # Heartbeat first: if we are killed during a slow restore, the
            # guard must see "not running", not a fresh heartbeat.
            for path in (self.state_file, self.state_file + ".tmp"):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if self.controlling:
            self.save_memory()
            self._leave_fans(why)

    def _leave_fans(self, why):
        if self.dry_run:
            self.log("notice", "dry run %s; fan settings were not touched" % why)
        elif self.ctrl.paused:
            self.log("warning", "fanctl %s; it leaves the fans as someone else set them (%s); "
                     "fanctl-guard, if running, restores auto within a minute"
                     % (why, fmt_setting(self.applied)))
        else:
            self.restore_auto(why)

    def _lock(self, path):
        self._lock_fd = self._take_lock(path, wait=True)   # kept open for the life of the process
        self.lock_path = path

    def _take_lock(self, path, wait):
        """An open fd with the exclusive lock on the file now at `path`, or None
        if another process holds it and not `wait`. A lock on a file that has
        since been deleted or replaced shuts nobody out, so it doesn't count:
        it is dropped and the path tried again."""
        while True:
            # Read-only open: works for a non-root user even if root created the file.
            fd = os.open(path, os.O_RDONLY | os.O_CREAT | O_NOFOLLOW, 0o644)
            try:
                if not self._flock(fd, path, wait):
                    os.close(fd)
                    return None
                if is_file_at(fd, path):
                    return fd
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)

    def _flock(self, fd, path, wait):
        """Lock fd exclusively: True once locked, False if another process
        holds the lock and not `wait`."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
        if not wait:
            return False
        # Another fanctl, or fanctl-guard, which holds the lock while it
        # restores auto so that it can't change the setting under us.
        self.log("warning", "%s is locked by another fanctl or by fanctl-guard; waiting" % path)
        fcntl.flock(fd, fcntl.LOCK_EX)   # a stop signal still ends the wait (Shutdown)
        return True

    def _check_lock(self):
        """Re-take the lock if someone deleted or replaced the lock file; stop
        (LockLost) if another process has taken the file now at its path."""
        if self._lock_fd is None or is_file_at(self._lock_fd, self.lock_path):   # None: the simulator
            return
        fd = self._take_lock(self.lock_path, wait=False)
        if fd is None:
            raise LockLost("%s was replaced and is held by another process" % self.lock_path)
        os.close(self._lock_fd)
        self._lock_fd = fd
        self.log("warning", "%s had been removed; lock taken again" % self.lock_path)

    def startup(self, now):
        c = self.cfg
        self.log("notice", "fanctl %s starting%s: python %s, cli %s; fans %d..%d%%, raise within %g C "
                 "of EOS targets (%+g C), lower when %g C under; EOS takes over at target +%g C, "
                 "alert -%g C or inlet %g C"
                 % (VERSION, " (DRY RUN: no changes)" if self.dry_run else "",
                    sys.version.split()[0], self.cli.path, c.floor, c.ceiling, c.keep_below,
                    c.target_offset, c.keep_below + c.band, c.release_excess, c.alert_margin,
                    c.ambient_max))
        try:
            self.applied = current_setting(self.cli)
            self.last_reconcile = now
        except CliError as e:
            self.log("warning", "cannot read the fan setting yet: %s" % e)
        try:
            saved = read_fan_config(self.cli, STARTUP_CMD)
            if saved[0] == "override":
                self.log("notice", "startup-config holds 'environment fan-speed override %d'; after a "
                         "reload the fans run at %d%% until fanctl starts" % (saved[1], saved[1]))
        except CliError:
            pass
        self.log("notice", "current fan setting: %s" % fmt_setting(self.applied))
        self.ctrl = Controller(c, self.applied, now)
        memory = None if self.dry_run else self.load_memory()
        if memory:
            m, age = memory
            self.ctrl.restore(m, now, age)
            left = self.ctrl.hold_left(now)
            if left:
                self.log("notice", "keeping the hand-off hold from before the restart: %d s more" % left)

    def tick(self, now):
        self._check_lock()
        reading = self.read(now)
        verdict, outside = self.reconcile(now, reading)
        decision = self.apply(now, self.ctrl.decide(now, reading, outside), sure=verdict != SET_UNSURE)
        self.report(now, reading, decision)
        self.write_status(reading, decision)
        # Every poll, like the heartbeat (both in RAM): EOS stops a daemon
        # with SIGKILL, so there is no save on the way out, and a restart then
        # loses at most this poll's changes.
        self.save_memory()
        self.last_reading, self.last_decision = reading, decision

    # -- steps --
    def read(self, now):
        try:
            docs = read_environment(self.cli)
            r = assess(docs, self.cfg, self.alert_counts, self.baseline)
        except (CliError, ValueError) as e:
            self.log.limited("read", "warning", "could not read switch environment: %s" % e, now, 300)
            return None
        except Exception:
            self.log.limited("read-bug", "err", "error while judging switch environment:\n"
                             + traceback.format_exc(), now, 3600)
            return None
        self.alert_counts.update(r.alert_counts)
        if not r.faults:   # then it has every sensor and fan the baseline has (assess)
            self.baseline = {"sensors": r.seen, "fans": r.fan_count}
        for label, why in r.skipped:
            self.log.limited("skip " + label, "notice",
                             "not using %s: %s" % (label, why), now, 86400)
        guessed = describe_guessed_targets(r, self.cfg)
        if guessed:
            self.log.limited("guessed-targets", "warning", guessed, now, 86400)
        return r

    def reconcile(self, now, r):
        """Track the fan setting in effect; notice when someone else changes it.

        The cooling JSON (read every poll) says what is in effect. When it
        disagrees with what fanctl last set, running-config decides at once
        whether someone else changed it or the status simply lags. Returns
        (verdict, outside): judge_setting's verdict, and the setting someone
        else put in effect, for the Controller, or None."""
        status = r.setting if r is not None else None
        configured = self._read_config(now) if self._config_check_due(now, status) else None
        verdict, actual, self.mismatches, overruled = judge_setting(status, configured, self.applied,
                                                                    self.mismatches)
        if overruled is not None:
            self.log.limited("xcheck", "warning", "running-config says %s but the cooling status says %s"
                             % (fmt_setting(overruled), fmt_setting(status)), now, 3600)
        if verdict == SET_ADOPT:
            self.applied = actual
        if verdict != SET_CHANGED:
            return verdict, None
        old, self.applied = self.applied, actual
        return verdict, self._changed_outside(old, actual)

    def _config_check_due(self, now, status):
        """Read running-config this poll? Every `reconcile` s, and at once when
        the status disagrees with the setting fanctl last set or saw."""
        if self.last_reconcile is None or now - self.last_reconcile >= self.cfg.reconcile:
            return True
        if status is None or self.applied is None:
            return status == self.applied      # neither known: running-config is all there is
        return status != self.applied

    def _read_config(self, now):
        """The setting running-config holds, or None if it can't be read."""
        try:
            configured = read_fan_config(self.cli)
        except CliError as e:
            self.log.limited("reconcile", "warning", "cannot read running-config: %s" % e, now, 900)
            return None
        self.last_reconcile = now
        return configured

    def _changed_outside(self, old, actual):
        """Log someone else's change. Returns the setting for the Controller
        to leave alone, or None in a dry run (nothing of fanctl's to stop)."""
        if self.dry_run:
            return None
        if self.ctrl.paused:
            self.log("notice", "fan setting changed outside fanctl again: %s -> %s"
                     % (fmt_setting(old), fmt_setting(actual)))
            return actual
        self.log("warning", "fan setting changed outside fanctl (%s -> %s): leaving the fans as set "
                 "until fanctl is restarted (daemon fanctl / shutdown / no shutdown). If something "
                 "goes wrong while the fans are below %d%%, fanctl still hands them to EOS"
                 % (fmt_setting(old), fmt_setting(actual), self.cfg.ceiling))
        return actual

    def apply(self, now, d, sure=True):
        """Put the setting the Controller wants into effect. Returns the
        Decision that stands: d, or the hand-off to EOS when an override
        could not be set. sure: False when the setting in effect is in doubt
        this poll (SET_UNSURE): then nothing is written."""
        want = self.ctrl.setting
        if self.dry_run:
            self._dry_apply(want, d)
            return d
        # Write whenever the fans aren't as wanted, so a failed write is retried
        # at the next poll. While paused the controller wants the operator's
        # setting, which is in effect, or AUTO once it has handed a low
        # override to EOS (Controller.pause, _problem).
        if not sure or want == self.applied or self._auto_refused(now, want):
            return d
        try:
            got = self.set_fans(want)
        except CliError as e:
            return self._write_failed(now, want, e, d)
        self.changes.append(now)
        if got != want:
            self.auto_refused_until = now + 300
            self.log("err", "EOS would not return to automatic fan control; fans set to override "
                     "%d%% instead (retrying auto every 5 min)" % EOS_MAX_OVERRIDE)
            return d
        level = "warning" if want == AUTO else "notice"
        self.log(level, change_line(d, want))
        return d

    def _dry_apply(self, want, d):
        if want != self.dry_setting:
            self.log("notice", "[dry run] would set %s (%s)" % (fmt_setting(want),
                                                               "; ".join(d.why[:3]) or "no change"))
            self.dry_setting = want

    def _auto_refused(self, now, want):
        """EOS refused auto less than 5 min ago and runs the fans flat out
        instead: don't ask again yet."""
        return (want == AUTO and self.auto_refused_until is not None and now < self.auto_refused_until
                and self.applied == ("override", EOS_MAX_OVERRIDE))

    def _write_failed(self, now, want, e, d):
        """A write that failed: hand the fans to EOS if it was an override,
        else (auto) leave it for the next poll to retry."""
        if want[0] == "override":
            self.log("err", "could not set %s: %s; handing control to EOS" % (fmt_setting(want), e))
            return self.apply(now, self.ctrl.release(
                now, ["could not apply %s: %s" % (fmt_setting(want), e)], hot=False, fault=True))
        self.log.limited("auto-fail", "err", "could not set automatic fan control: %s "
                         "(retrying every poll)" % e, now, 300)
        return d

    def set_fans(self, want, quick=False):
        """Apply a fan setting and confirm it took. Returns the setting now in
        effect: for AUTO that is override 100 if EOS refuses every way back
        (fanctl-guard.sh's restore takes the same steps; a test pins them).
        quick: verify with short timeouts (used while stopping)."""
        if want != AUTO:
            return self._configure("environment fan-speed override %d" % want[1], want, quick)
        errors = []
        for line in ("environment fan-speed auto", "no environment fan-speed"):
            try:
                return self._configure(line, AUTO, quick)
            except CliError as e:
                errors.append(str(e))
        # Never leave a low override in place: flat out is the safe fallback.
        maxed = ("override", EOS_MAX_OVERRIDE)
        self._configure("environment fan-speed override %d" % EOS_MAX_OVERRIDE, maxed, quick)
        self.log("err", "returning to automatic fan control failed: %s" % "; ".join(errors))
        return maxed

    def _configure(self, line, expect, quick):
        error = None
        try:
            self.cli.run(["configure", line, "end"])
        except CliError as e:
            error = e
        actual = self._read_back(expect, quick)
        if actual is None and error is None:
            # The command went through but nothing could be read back: assume
            # it took; the next poll's cooling status will tell.
            self.log("warning", "'%s' sent but could not be confirmed" % line)
            actual = expect
        if actual is not None:
            self.applied = actual
        if actual != expect:
            raise CliError("'%s' did not take effect%s; fans are %s"
                           % (line, ": %s" % error if error else "", fmt_setting(actual)))
        if error:
            self.log("notice", "'%s' printed an error but took effect: %s" % (line, error))
        return actual

    def _read_back(self, expect, quick):
        """The setting in effect after a change, or None if unreadable:
        from the cooling status, which may lag (retried for 3 s), then from
        running-config. quick: one try with short timeouts (while stopping)."""
        timeout = min(self.cfg.cli_timeout, QUICK_CLI_TIMEOUT) if quick else self.cfg.cli_timeout
        actual = None
        for attempt in range(1 if quick else 3):
            try:
                actual = setting_from_cooling(parse_json_doc(self.cli.run([COOLING_CMD], timeout=timeout)))
            except (CliError, ValueError):
                actual = None
            if actual == expect:
                return actual
            if not quick:
                self.sleep(1)
        try:
            return read_fan_config(self.cli, timeout=timeout)
        except CliError:
            return actual

    def restore_auto(self, why):
        err = None
        for attempt in range(2):
            try:
                got = self.set_fans(AUTO, quick=True)
                if got == AUTO:
                    self.log("warning", "fanctl %s: fan control handed back to EOS (auto)" % why)
                else:
                    self.log("err", "fanctl %s: EOS refused automatic fan control; fans left at %s"
                             % (why, fmt_setting(got)))
                return
            except CliError as e:
                err = e
        self.log("err", "fanctl %s and could NOT restore automatic fan control (%s). Run "
                 "'configure / environment fan-speed auto' by hand." % (why, err))

    # -- bookkeeping --
    def load_memory(self):
        """(checked memory, its age in s), or None if there is none to use."""
        try:
            with open(self.memory_file) as f:
                m = parse_memory(json.load(f))
        except OSError as e:
            if e.errno != errno.ENOENT:
                self.log("warning", "cannot read %s: %s" % (self.memory_file, e.strerror))
            return None
        except ValueError as e:    # also bad JSON
            self.log("warning", "ignoring unreadable %s: %s" % (self.memory_file, e))
            return None
        age = self.wall() - m["wall"]
        if age > self.cfg.hold_max:
            return None
        return m, max(0.0, age)   # a clock stepped back must not drop a hold

    def save_memory(self):
        if self.ctrl is None or self.dry_run:   # a dry run's speeds are imaginary
            return
        m = self.ctrl.snapshot(self.clock())
        m["wall"] = self.wall()
        try:
            write_json(self.memory_file, m)
        except OSError as e:
            self.log.limited("memory", "warning", "cannot write %s: %s" % (self.memory_file, e),
                             self.clock(), 3600)

    def report(self, now, r, d):
        self._log_event(now, d)
        if self.last_summary is None:
            self.last_summary = now
        elif now - self.last_summary >= self.cfg.summary:
            self.last_summary = now
            self.changes = [t for t in self.changes if now - t < self.cfg.summary]
            self.log("info", "status: fans %s%s%s, %d change(s) in the last %d min%s" % (
                fmt_setting(self.applied), " [dry run]" if self.dry_run else "",
                " [paused: set outside fanctl]" if self.ctrl.paused else "", len(self.changes),
                self.cfg.summary // 60, self._summary_readings(r)))

    def _log_event(self, now, d):
        """Log the events that change nothing now, at most every so often."""
        if d.event == EV_HOLD:
            self.log.limited("hold", "info", "fans under EOS control: %s" % "; ".join(d.why[:3]), now, 900)
        elif d.event == EV_RETRY:
            self.log.limited("retry", "info", d.why[0], now, 900)
        elif d.event == EV_PAUSED:
            self.log.limited("paused", "info", "fans %s, set outside fanctl; not managing them until "
                             "restarted" % fmt_setting(self.applied), now, 3600)
        elif d.event == EV_PAUSED_ALARM:
            # From state, as apply() writes: until the fans are as the
            # controller wants, apply() is still handing them to EOS.
            doing = "is leaving them alone" if self.ctrl.setting == self.applied else "is handing them to EOS"
            self.log.limited("paused-alarm", "err", "fans are %s (set outside fanctl) and: %s. fanctl %s"
                             % (fmt_setting(self.applied), "; ".join(d.why[:3]), doing), now, 300)

    def _summary_readings(self, r):
        """The status summary's account of the readings, if there are any."""
        if r is None or not r.sensors:
            return ""
        extra = ""
        b = self.ctrl.binding
        if not (b and b[0] is r.worst):   # same sensor, this poll: the binding says it all
            extra = "; " + describe_hottest(r)
        extra += "; inlet %s C" % ("%.1f" % r.ambient if r.ambient is not None else "?")
        if b:
            extra += "; " + describe_binding(b)
        return extra

    def write_status(self, r, d):
        ctrl = self.ctrl
        status = {
            "version": VERSION, "pid": os.getpid(), "time": self.wall(),
            "dry_run": self.dry_run, "paused": ctrl.paused,
            "want": fmt_setting(ctrl.setting),
            "applied": fmt_setting(self.applied),
            "event": d.event, "why": d.why[:6],
            "hold_remaining": int(ctrl.hold_left(self.clock())) if ctrl.setting == AUTO else 0,
            "read_failures": ctrl.failures,
        }
        if r is not None:
            status.update({
                "ambient": r.ambient, "faults": r.faults, "hot": r.hot,
                "sensors": [{"label": s.label, "temp": round(s.current, 1), "target": round(s.target, 1),
                             "excess": round(s.excess, 1), "alert": s.alert,
                             "weight": round(ctrl.weights.get(s.name, 1.0), 2)}
                            for s in sorted(r.sensors, key=lambda s: -s.excess)[:5]],
            })
            if ctrl.binding:
                status["binding"] = describe_binding(ctrl.binding)
        self._write_status_dict(status)

    def _write_status_dict(self, status):
        try:
            write_json(self.state_file, status)
        except OSError as e:
            self.log.limited("status", "warning", "cannot write %s: %s" % (self.state_file, e),
                             self.clock(), 3600)


# --------------------------------------------------------------------------
# One-shot modes

def probe(cfg, cli, out=None):
    """Read-only report of what fanctl sees and would do. Changes nothing."""
    w = (out or sys.stdout).write
    w("fanctl %s probe (read-only): python %s (%s), cli %s\n\n"
      % (VERSION, sys.version.split()[0], sys.executable, cli.path))
    try:
        docs = read_environment(cli)
    except (CliError, ValueError) as e:
        w("ERROR: cannot read the switch environment: %s\n" % e)
        return 1
    r = assess(docs, cfg)
    _probe_readings(w, r)
    for label, cmd in (("running-config", RUNNING_CMD), ("startup-config", STARTUP_CMD)):
        try:
            w("%s fan setting: %s\n" % (label, fmt_setting(read_fan_config(cli, cmd))))
        except CliError as e:
            w("%s fan setting: unreadable (%s)\n" % (label, e))
    for title, items in (("FAULTS (fanctl would hand the fans to EOS and wait)", r.faults),
                         ("TOO HOT (fanctl would hand the fans to EOS)", r.hot)):
        if items:
            w("\n%s:\n%s" % (title, "".join("  - %s\n" % item for item in items)))
    guessed = describe_guessed_targets(r, cfg)
    if guessed:
        w("\nWARNING: %s.\n" % guessed)
    w("\nverdict: ")
    if not r.healthy:
        w("fanctl would leave the fans under EOS automatic control until the problems above clear.\n")
    else:
        _probe_verdict(w, cfg, r)
    return 0


def _probe_readings(w, r):
    w("%-44s %6s %7s %-20s %7s %6s %8s\n" % ("sensor", "temp", "target", "(source)", "vs tgt",
                                            "alert", "headroom"))
    for s in sorted(r.sensors, key=lambda s: -s.excess):
        w("%-44s %6.1f %7.1f %-20s %+7.1f %6.0f %8.1f\n" % (
            s.label[:44], s.current, s.target, "(%s)" % s.target_src, s.excess, s.alert, s.headroom))
    for label, why in r.skipped:
        w("%-44s not used: %s\n" % (label[:44], why))
    w("\ninlet/ambient: %s C   airflow: %s   fan setting in effect: %s\n" % (
        r.ambient, r.airflow, fmt_setting(r.setting)))
    w("fans (fan trays and PSUs): %d; fan-tray configured speeds %s\n"
      % (r.fan_count, sorted(set(r.tray_speeds))))
    w("PSUs: %s\n" % (", ".join("%s=%s" % kv for kv in sorted(r.psus.items())) or "none"))


def preview(cfg, r, horizon):
    """What a fanctl started now (without a memory file) would do in the
    next `horizon` s if the readings stayed as in Reading r: (its
    Controller, its first Decision, [(s from now, Decision, setting)]
    for its first two changes of the fan setting after any take-over,
    and whether those are all the changes within `horizon`).
    The Controller makes every decision, poll by poll, as in the daemon."""
    ctrl = Controller(cfg, r.setting, 0.0)
    first = d = ctrl.decide(0.0, r)
    wanted = 3 if first.event == EV_RESUME else 2
    now, changes = 0.0, []
    while True:
        if d.event in CHANGES:
            changes.append((now, d, ctrl.setting))
        if len(changes) == wanted:
            return ctrl, first, changes, False
        if now >= horizon:
            return ctrl, first, changes, True
        now += cfg.poll
        d = ctrl.decide(now, r)


# The most polls --probe's preview simulates. A poll's decision costs more
# the longer the controller's recent-excess window, so with the longest
# legal waits (a day, polled every 5 s) a full preview took a minute. The
# probe looks no further than this and says how far it looked.
PREVIEW_POLLS = 2000


def _probe_verdict(w, cfg, r):
    horizon = min(2 * max(cfg.up_interval, cfg.down_interval, cfg.probe_interval, cfg.ceiling_dwell),
                  PREVIEW_POLLS * cfg.poll)
    ctrl, first, changes, all_of_them = preview(cfg, r, horizon)
    if first.event == EV_HOLD:
        w("fanctl would leave the fans under EOS automatic control for now: %s.\n" % "; ".join(first.why))
        return
    if first.event == EV_RESUME:
        w("fanctl would take over at %d%% (the speed EOS is running now).\n" % changes.pop(0)[2][1])
    else:
        w("fanctl would carry on from the current %s.\n" % fmt_setting(r.setting))
    w("%s.\n%s.\n" % (describe_hottest(r), describe_binding(ctrl.binding)))
    if not changes:
        w("If the readings stayed as they are, it would keep the fans at %s (no change within %d s).\n"
          % (fmt_setting(ctrl.setting), horizon))
        return
    w("If the readings stayed as they are, it would then log:\n")
    for t, d, setting in changes:
        w("  %s: %s\n" % ("after %d s" % t if t else "now", change_line(d, setting)))
    if all_of_them:
        w("  (no other change within %d s)\n" % horizon)


def show_status(state_file=STATE_FILE, lock_file=LOCK_FILE, wall=time.time, out=None):
    out = out or sys.stdout
    if not os.path.lexists(state_file):
        out.write("fanctl is not running (no %s)\n" % state_file)
        return 1
    rc = 0
    for d in runtime_dirs(state_file, lock_file):   # before opening anything in them
        problem = check_dir(d, writable=False)
        if problem:
            out.write("UNTRUSTED: unsafe runtime directory %s: %s. The status below can't be "
                      "trusted.\n" % (d, problem))
            rc = 1
    out.flush()
    st, problem = _read_status(state_file)
    if problem:
        out.write(problem + "\n")
        return 1
    age = wall() - st["time"]
    held = lock_is_held(lock_file)
    if held is False or abs(age) > STALE_AFTER:
        out.write("STALE: fanctl is %s (heartbeat %.0f s old). The values below are old; "
                  "fanctl-guard may have changed the fans since.\n"
                  % ("not running" if held is False else "hung", age))
        rc = 1
    _print_status(out, st, age)
    return rc


def _read_status(path):
    """(the status file's contents, None), or (None, why it can't be read)."""
    try:
        # Non-blocking, no symlinks, regular files only: nothing planted here may hang us.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None, "fanctl is not running (no %s)" % path
        return None, "cannot read %s: %s" % (path, e)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None, "cannot read %s: not a regular file" % path
    try:
        with os.fdopen(fd) as f:
            st = json.load(f)
    except OSError as e:
        return None, "cannot read %s: %s" % (path, e)
    except ValueError:
        return None, "cannot parse %s" % path
    if not isinstance(st, dict) or _num(st.get("time")) is None:   # not a status fanctl wrote
        return None, "cannot parse %s" % path
    return st, None


def _status_header(st, age):
    return "fanctl %s pid %s, updated %.0f s ago%s%s\n" % (
        st.get("version"), st.get("pid"), age, " [DRY RUN]" if st.get("dry_run") else "",
        " [PAUSED: fans set outside fanctl]" if st.get("paused") else "")


def _print_status(out, st, age):
    out.write(_status_header(st, age))
    if st.get("event") == EV_STARTING:
        out.write("fans: not read yet (fanctl is starting)\n")
    else:
        out.write("fans: %s in effect; fanctl wants %s\n" % (st.get("applied"), st.get("want")))
    if st.get("hold_remaining"):
        out.write("holding EOS control for %s s more\n" % st["hold_remaining"])
    if st.get("ambient") is not None:
        out.write("inlet/ambient: %.1f C\n" % st["ambient"])
    if st.get("binding"):
        out.write("%s\n" % st["binding"])
    for s in st.get("sensors", []):
        out.write("  %-44s %5.1f C  target %5.1f  (%+.1f)  alert %.0f  weight %.2f\n" % (
            s["label"][:44], s["temp"], s["target"], s["excess"], s["alert"], s.get("weight", 1.0)))
    for key in ("faults", "hot", "why"):
        for item in st.get(key) or []:
            out.write("%s: %s\n" % (key, item))


# --------------------------------------------------------------------------

class Config(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Parser(argparse.ArgumentParser):
    """Usage errors also go to syslog: under 'daemon' nobody sees stderr."""

    def error(self, message):
        if syslog and "--no-syslog" not in sys.argv:
            syslog.openlog(PROG, syslog.LOG_PID, syslog.LOG_LOCAL4)
            syslog.syslog(syslog.LOG_ERR, "bad command line, not starting: %s" % message)
        argparse.ArgumentParser.error(self, message)


def parse_args(argv=None):
    p = _Parser(prog=PROG, description="Run an Arista switch's fans as slowly as is safe. "
                "See the module docstring and README.md.")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--probe", action="store_true", help="read-only: show what fanctl sees and would do")
    modes.add_argument("--dry-run", action="store_true", help="run the loop and log decisions, change nothing")
    modes.add_argument("--status", action="store_true", help="show the running daemon's status")
    p.add_argument("--ignore-sensor", action="append", default=[], metavar="NAME",
                   help="sensor name (TempSensorN, as --probe lists it) or description to leave out "
                   "(repeatable)")
    p.add_argument("--ignore-psu", action="append", default=[], metavar="N",
                   help="PSU slot to leave out, e.g. one that is deliberately unpowered (repeatable)")
    p.add_argument("--cli", default=None, help="CLI binary (default: FastCli, else Cli)")
    # Under the daemon the guard, which can't follow these, would restore auto every minute
    # (Daemon._warn_unwatched).
    fixed = " (for tests and runs by hand; fanctl-guard.sh watches the default unless told otherwise)"
    p.add_argument("--state-file", default=STATE_FILE, help="heartbeat and status file" + fixed)
    p.add_argument("--memory-file", default=MEMORY_FILE, help="restart memory file")
    p.add_argument("--lock-file", default=LOCK_FILE, help="lock file" + fixed)
    p.add_argument("--no-syslog", action="store_true", help="log to stdout only")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    for key, (default, lo, hi, unit) in sorted(TUNABLES.items()):
        kind = OPTION_TYPES.get(key, type(default))
        p.add_argument("--" + key.replace("_", "-"), type=kind, default=default,
                       metavar=kind.__name__.upper(),
                       help="(default %s; %s)" % ("derived" if default is None else default,
                                                 _range(lo, hi, unit).replace("%", "%%")))
    args = p.parse_args(argv)
    for opt in ("state_file", "memory_file", "lock_file"):
        # The directory checks read a path as text: '..' after a symlink
        # would make them check a different directory than the one used.
        if ".." in getattr(args, opt).split("/"):
            p.error("--%s must not contain '..'" % opt.replace("_", "-"))
    return args


def make_config(args):
    cfg = Config(**{k: getattr(args, k) for k in DEFAULTS})
    if cfg.resume_excess is None:
        cfg.resume_excess = -cfg.keep_below
    if cfg.ambient_resume is None:
        cfg.ambient_resume = cfg.ambient_max - 2.0
    cfg.ignore_psu = [str(x) for x in args.ignore_psu]
    cfg.ignore_sensor = list(args.ignore_sensor)
    return cfg


def _range(lo, hi, unit):
    return ("%g..%g %s" % (lo, hi, unit)).rstrip()


def check_config(cfg):
    """Why these settings can't be used, or [] if they can."""
    problems = ["%s must be %s" % (key, _range(lo, hi, unit))
                for key, (_default, lo, hi, unit) in TUNABLES.items()
                if not lo <= getattr(cfg, key) <= hi]    # also refuses NaN
    if problems:
        return problems
    rules = ((cfg.floor <= cfg.ceiling, "floor must be at most ceiling"),
             (cfg.probe_margin < cfg.band, "probe_margin must be less than band"),
             (cfg.resume_excess <= -cfg.keep_below, "resume_excess must be at most -keep_below"),
             (cfg.ambient_max - 15 <= cfg.ambient_resume <= cfg.ambient_max,
              "ambient_resume must be at most ambient_max (and within 15 C of it)"),
             (cfg.down_interval <= cfg.probe_interval <= cfg.hold_max,
              "need down_interval <= probe_interval <= hold_max"),
             (max(cfg.hot_hold, cfg.fault_hold) <= cfg.hold_max, "hot_hold and fault_hold must be at most hold_max"))
    return [why for ok, why in rules if not ok]


def main(argv=None):
    args = parse_args(argv)
    cfg = make_config(args)
    interactive = args.probe or args.status
    log = Log(use_syslog=not (args.no_syslog or interactive), stream=sys.stdout)
    problems = check_config(cfg)
    if problems:
        log("err", "invalid settings, not starting: " + "; ".join(problems))
        return 2
    if args.status:
        return show_status(args.state_file, args.lock_file)
    cli = Cli(args.cli or find_cli(), cfg.cli_timeout)
    if args.probe:
        return probe(cfg, cli)
    return Daemon(cfg, cli, log, state_file=args.state_file, memory_file=args.memory_file,
                  dry_run=args.dry_run).run(args.lock_file)


if __name__ == "__main__":
    sys.exit(main())

"""An SSH session to an Arista EOS switch's CLI, its environment JSON as a
poll record, and its daemons, for the off-box tools: tools/monitor.py and
measurements/fan_step_test.py. tools/report.py reads its --targets file with
sensors().

Needs paramiko (`pip install paramiko`) for the SSH session only: it is
imported when a Shell connects, so report.py runs without it.

login_from_env() reads the login from the environment, once, at start-up:

  SWITCH_HOST, SWITCH_USERNAME  required
  SWITCH_PASSWORD               prompted for if not set
  SWITCH_ENABLE_PASSWORD        the enable password, if one is asked for;
                                default SWITCH_PASSWORD
  ACCEPT_NEW_HOST_KEY=1         accept a host key that isn't in
                                ~/.ssh/known_hosts on the first connection

Whichever key the first connection sees is pinned for the rest of the run.

A Shell sends only the commands its `allowed` pattern matches. It raises
LoginError when the switch refuses the login (a rejected password or host key,
or no enable): trying again won't help. It raises ShellError when reading the
switch failed in a way that reconnecting may cure (a timeout, a dropped
connection, a login cut short or a shell that won't open, output that isn't
the JSON asked for).
"""
import collections
import getpass
import json
import re
import time

PROMPT = re.compile(r"\n[\w.-]+(\([^)\n]*\))?[#>]\s*$")

# What paramiko's AuthenticationException says when the login was cut short
# rather than refused (paramiko/auth_handler.py): the switch's AAA server took
# longer than auth_timeout, or sshd hung up during the login.
CUT_SHORT = ("Authentication timeout.", "Authentication failed: transport shut down or saw EOF")

Login = collections.namedtuple("Login", "host username password enable_password accept_new_host_key")


class ShellError(Exception):
    """Reading the switch failed; reconnecting may cure it."""


class LoginError(Exception):
    """The switch refused the login; retrying won't help."""


def login_from_env(environ, usage):
    """The Login the environment describes; exits with `usage` if it describes none."""
    if not environ.get("SWITCH_HOST") or not environ.get("SWITCH_USERNAME"):
        raise SystemExit(usage)
    password = environ.get("SWITCH_PASSWORD")
    if password is None:
        password = getpass.getpass("password for %s: " % environ["SWITCH_HOST"])
    return Login(environ["SWITCH_HOST"], environ["SWITCH_USERNAME"], password,
                 environ.get("SWITCH_ENABLE_PASSWORD", password),
                 environ.get("ACCEPT_NEW_HOST_KEY") == "1")


class _RejectUnknown(object):
    """paramiko's missing-host-key policy: refuse, as a LoginError."""

    def missing_host_key(self, client, hostname, key):
        raise LoginError("the host key of %s is not in ~/.ssh/known_hosts (ACCEPT_NEW_HOST_KEY=1 "
                         "accepts it on the first connection)" % hostname)


class Shell(object):
    """An enabled CLI session that sends only commands matching `allowed`."""

    def __init__(self, login, allowed, timeout=60.0):
        self.login = login
        self.allowed = allowed
        self.timeout = timeout          # s for each command; logging in takes up to 20 s a step
        self.client = self.chan = None
        self.host_key = None

    def connect(self):
        import paramiko                 # here, so that a tool that only reads files runs without it
        self.close()
        c = paramiko.SSHClient()
        try:
            chan = self._open(paramiko, c)
        except Exception:
            c.close()
            raise
        self.client, self.chan = c, chan
        self._read(r"[#>]\s*$", 20)
        self._enable()
        self.run("terminal length 0")

    def _open(self, paramiko, c):
        """Log c in and open its shell: the channel, LoginError or ShellError."""
        c.load_system_host_keys()
        if self.host_key is not None:      # pinned at the first connection: reconnects must match
            c.get_host_keys().add(self.login.host, self.host_key.get_name(), self.host_key)
            c.set_missing_host_key_policy(_RejectUnknown())
        elif self.login.accept_new_host_key:
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            c.set_missing_host_key_policy(_RejectUnknown())
        try:
            c.connect(self.login.host, username=self.login.username, password=self.login.password,
                      look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
            if self.host_key is None:
                self.host_key = c.get_transport().get_remote_server_key()
            return c.invoke_shell(width=250, height=1000)
        except paramiko.AuthenticationException as e:
            if str(e) in CUT_SHORT:
                raise ShellError("cannot log in to %s: %s" % (self.login.host, e))
            raise LoginError("%s: %s" % (self.login.host, e))
        except paramiko.BadHostKeyException as e:
            raise LoginError("%s: %s" % (self.login.host, e))
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise ShellError("cannot connect to %s: %s" % (self.login.host, e))

    def _enable(self):
        self.chan.send("enable\n")
        b = self._read(r"([Pp]assword:\s*$)|([#>]\s*$)", 20)
        if re.search(r"[Pp]assword:\s*$", b):
            self.chan.send(self.login.enable_password + "\n")
            b = self._read(r"[#>]\s*$", 20)
        if not re.search(r"#\s*$", b):
            raise LoginError("%s: enable failed" % self.login.host)

    def _read(self, pattern, timeout):
        buf, end = "", time.time() + timeout
        try:
            while time.time() < end:
                if self.chan.recv_ready():
                    buf += self.chan.recv(1 << 20).decode("utf-8", "replace")
                    if re.search(pattern, buf):
                        return buf
                elif self.chan.closed:
                    raise ShellError("the switch closed the session (%r)" % buf[-200:])
                else:
                    time.sleep(0.05)
        except (OSError, EOFError) as e:
            raise ShellError("connection lost: %s" % e)
        raise ShellError("timeout waiting for the switch (%r)" % buf[-200:])

    def run(self, cmd):
        """The output of `cmd`, without its echo and the prompt after it."""
        if not self.allowed.match(cmd):
            raise ValueError("refused to send %r" % cmd)      # a bug in the caller, not the switch
        if self.chan is None:
            raise ShellError("not connected")
        try:
            self.chan.send(cmd + "\n")
        except (OSError, EOFError) as e:
            raise ShellError("connection lost: %s" % e)
        out = self._read(PROMPT.pattern, self.timeout)
        return "\n".join(out.splitlines()[1:-1])

    def json(self, cmd):
        """The JSON object `cmd` prints."""
        out = self.run(cmd)
        try:
            doc = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except ValueError:
            raise ShellError("%r printed no JSON object: %r" % (cmd, out[-200:]))
        return doc

    def close(self):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.client = self.chan = None


def _dicts(doc, key):
    """doc[key] as a list of objects ([] if absent), or ShellError."""
    v = doc.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise ShellError("unexpected %s in the environment JSON" % key)
    return v


def _named(items, key):
    if not all(isinstance(x.get(key), str) for x in items):
        raise ShellError("an environment JSON entry without a %s" % key)
    return items


def sensors(temp):
    """Every sensor of a `show system environment temperature | json`, PSU sensors last."""
    found = list(_dicts(temp, "tempSensors"))
    for slot in _dicts(temp, "powerSupplySlots"):
        found += _dicts(slot, "tempSensors")
    for key in ("currentTemperature", "setPointTemperature", "overheatThreshold"):
        if not all(_num_or_none(s.get(key)) for s in found):
            raise ShellError("unexpected %s in the environment JSON" % key)
    return _named(found, "name")


def _num_or_none(v):
    return v is None or isinstance(v, (int, float)) and not isinstance(v, bool)


def fans(cool):
    """Every fan of a `show system environment cooling | json`, PSU fans last."""
    found = []
    for slot in _dicts(cool, "fanTraySlots") + _dicts(cool, "powerSupplySlots"):
        found += _dicts(slot, "fans")
    return _named(found, "label")


def daemons(text):
    """(name, state) of each daemon a `show daemon` lists, e.g. ("fanctl", "running with PID 4242")."""
    return re.findall(r"(?:Agent|Process): (\S+) \(([^)]*)\)", text)


def fanctl_daemons(found):
    """The (name, state) pairs of fanctl and its guard: the daemons named fanctl*."""
    return [(name, state) for name, state in found if name.startswith("fanctl")]


def poll_record(temp, cool, power):
    """The readings the tools keep from one poll of the three environment JSONs."""
    supplies = power.get("powerSupplies", {})
    if not isinstance(supplies, dict) or not all(isinstance(v, dict) for v in supplies.values()):
        raise ShellError("unexpected powerSupplies in the environment JSON")
    return {"event": "poll",
            "temps": {s["name"]: s.get("currentTemperature") for s in sensors(temp)},
            "ambient": cool.get("ambientTemperature"), "mode": cool.get("coolingMode"),
            "override": cool.get("overrideFanSpeed"),
            "fans": {f["label"]: [f.get("configuredSpeed"), f.get("actualSpeed"), f.get("status")]
                     for f in fans(cool)},
            "psu": {k: [v.get("state"), v.get("outputPower"), v.get("inputCurrent")] for k, v in supplies.items()},
            "tstatus": temp.get("systemStatus"), "cstatus": cool.get("systemStatus")}

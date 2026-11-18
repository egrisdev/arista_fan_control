"""A stand-in for paramiko that plays an Arista switch's CLI over SSH, for
testing the off-box tools (tools/eos.py and the scripts that use it) without
a switch or paramiko.

A test sets SWITCH to a Switch and puts this module in sys.modules as
"paramiko". SSHClient.connect() then logs into that Switch, checking the host
key and password as paramiko and sshd would, and invoke_shell() returns a
channel that answers like the EOS CLI: an echo of each command, its output and
the next prompt. The Switch starts from the JSON captured from the reference
switch (tests/fixtures) and records every command it was sent.
"""

import copy
import json
import os
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")

SWITCH = None     # the Switch that SSHClient connects to


class SSHException(Exception):
    pass


class AuthenticationException(SSHException):
    pass


class ChannelException(SSHException):
    def __init__(self, code, text):
        SSHException.__init__(self, code, text)


class BadHostKeyException(SSHException):
    def __init__(self, hostname, got_key, expected_key):
        SSHException.__init__(self, "Host key for server %r does not match: got %r, expected %r"
                              % (hostname, got_key.fingerprint, expected_key.fingerprint))


class Key(object):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def get_name(self):
        return "ssh-ed25519"

    def __eq__(self, other):
        return isinstance(other, Key) and other.fingerprint == self.fingerprint

    def __ne__(self, other):
        return not self == other


class AutoAddPolicy(object):
    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)


class RejectPolicy(object):
    def missing_host_key(self, client, hostname, key):
        raise SSHException("Server %r not found in known_hosts" % hostname)


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class Switch(object):
    """What the tests' switch holds and how it behaves; tests change the fields."""

    def __init__(self):
        self.host = "switch.example"
        self.prompt = "switch"
        self.password = "secret"
        self.enable_password = None            # None: enable asks for no password
        self.key = Key("SHA256:switch-key")
        self.known_hosts = {self.host: self.key}
        self.temp = _load("temp.json")
        self.cooling = _load("cooling.json")
        self.power = _load("power.json")
        self.status_file = {"version": "1.3.2", "event": "hold", "setting": "override 40%"}
        self.daemons = {"fanctl": "running with PID 4242", "fanctl-guard": "running with PID 4243"}
        self.logging = []                      # lines `show logging last 10 minutes` prints
        self.down = False                      # the switch is unreachable: logins time out, sessions drop
        self.hang = set()                      # commands that never finish
        self.drop = set()                      # commands that close the session
        self.lost = set()                      # commands whose answer the connection dies under
        self.refuse = set()                    # configuration lines EOS rejects
        self.ignore = set()                    # configuration lines EOS accepts without effect
        self.login_errors = []                 # what the next logins raise after the host key check, one each
        self.shell_errors = []                 # what opening the next shells raises, one each
        self.close_errors = []                 # what closing the next sessions raises, one each
        self.logins = 0
        self.options = None                    # the last login's options other than the password
        self.sent = []                         # every command line, in order

    def sensor(self, name):
        for s in self.temp["tempSensors"] + [x for sl in self.temp["powerSupplySlots"]
                                             for x in sl["tempSensors"]]:
            if s["name"] == name:
                return s
        raise KeyError(name)

    def answer(self, cmd, mode):
        """(output, new mode) for one command line in CLI mode `mode`."""
        shows = {"show system environment temperature | json": self.temp,
                 "show system environment cooling | json": self.cooling,
                 "show system environment power | json": self.power}
        if cmd in shows:
            return json.dumps(copy.deepcopy(shows[cmd]), indent=2), mode
        if cmd == "bash cat /run/fanctl/fanctl.status.json; echo":
            if self.status_file is None:
                return "cat: /run/fanctl/fanctl.status.json: No such file or directory\n", mode
            return json.dumps(self.status_file) + "\n", mode
        if cmd == "show daemon":
            return "\n".join("Agent: %s (%s)\nConfiguration:\n  Option   Value" % kv
                             for kv in sorted(self.daemons.items())), mode
        if cmd == "show logging last 10 minutes":
            return "\n".join(self.logging), mode
        if cmd == "terminal length 0":
            return "Pagination disabled.", mode
        return self._configure(cmd, mode)

    def _configure(self, cmd, mode):
        if cmd == "configure" and mode == "exec":
            return "", "config"
        if cmd == "end":
            return "", "exec"
        if mode == "config" and cmd in self.refuse:
            return "% Invalid input", mode
        if mode == "config" and cmd in self.ignore:
            return "", mode
        words = cmd.split()
        if mode == "config" and words[:3] == ["environment", "fan-speed", "override"] and len(words) == 4:
            self.cooling["coolingMode"], self.cooling["overrideFanSpeed"] = "manual", int(words[3])
            return "", mode
        if mode == "config" and cmd in ("environment fan-speed auto", "no environment fan-speed"):
            self.cooling["coolingMode"], self.cooling["overrideFanSpeed"] = "automatic", None
            return "", mode
        return "% Invalid input", mode


class Channel(object):
    """An interactive shell on SWITCH: send() lines, recv() what it prints."""

    def __init__(self, switch):
        self.sw = switch
        self.out = "Last login: Mon Oct 19 06:00:00 2026\r\n%s>" % switch.prompt
        self.pending = ""
        self.mode = "login"           # login, then exec after enable, config in configure
        self.closed = False
        self.lost = False

    def _prompt(self):
        return self.sw.prompt + {"login": ">", "exec": "#", "config": "(config)#", "password": ""}[self.mode]

    def send(self, data):
        if self.sw.down:
            self.closed = True
        if self.closed:
            raise OSError("Socket is closed")
        self.pending += data
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._line(line)
        return len(data)

    def _line(self, line):
        if self.mode == "password":
            self.mode = "exec" if line == self.sw.enable_password else "login"
            self.out += "\r\n" + ("" if self.mode == "exec" else "% Access denied\r\n") + self._prompt()
            return
        self.sw.sent.append(line)
        self.out += line + "\r\n"
        if line in self.sw.drop:
            self.closed = True
            return
        if line in self.sw.lost:
            self.lost = True
            return
        if line in self.sw.hang:
            return
        if line == "enable":
            self.mode = "exec" if self.sw.enable_password is None else "password"
            self.out += "Password: " if self.mode == "password" else self._prompt()
            return
        text, self.mode = self.sw.answer(line, self.mode)
        self.out += (text.rstrip("\n").replace("\n", "\r\n") + "\r\n" if text else "") + self._prompt()

    def recv_ready(self):
        return bool(self.out)

    def recv(self, n):
        if self.lost:                 # paramiko's recv can't acknowledge the data on a dead connection
            raise EOFError()
        data, self.out = self.out[:n], self.out[n:]
        return data.encode("utf-8")


class HostKeys(object):
    def __init__(self, known):
        self.known = dict(known)

    def add(self, hostname, keytype, key):
        self.known[hostname] = key

    def lookup(self, hostname):
        return self.known.get(hostname)


class Transport(object):
    def __init__(self, key):
        self.key = key

    def get_remote_server_key(self):
        return self.key


class SSHClient(object):
    def __init__(self):
        self.keys = HostKeys({})
        self.policy = RejectPolicy()
        self.sw = None

    def load_system_host_keys(self):
        self.keys = HostKeys(SWITCH.known_hosts)

    def get_host_keys(self):
        return self.keys

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, hostname, username=None, password=None, **kwargs):
        sw = SWITCH
        if sw.down or hostname != sw.host:
            raise socket.timeout("timed out")
        known = self.keys.lookup(hostname)
        if known is None:
            self.policy.missing_host_key(self, hostname, sw.key)
        elif known != sw.key:
            raise BadHostKeyException(hostname, sw.key, known)
        sw.logins += 1
        sw.options = kwargs
        if sw.login_errors:
            raise sw.login_errors.pop(0)
        if password != sw.password:
            raise AuthenticationException("Authentication failed.")
        self.sw = sw

    def get_transport(self):
        return Transport(self.sw.key)

    def invoke_shell(self, width=80, height=24):
        if self.sw.shell_errors:
            raise self.sw.shell_errors.pop(0)
        return Channel(self.sw)

    def close(self):
        if SWITCH.close_errors:
            raise SWITCH.close_errors.pop(0)
        self.sw = None

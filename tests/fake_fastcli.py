#!/usr/bin/env python3
"""Stand-in for /usr/bin/FastCli in end-to-end tests: `fake_fastcli.py -p 15 -c "cmds"`.

Serves the real switch JSON from tests/fixtures and keeps the fan setting in
the JSON state file named by $FAKE_FASTCLI_STATE:
  {"override": 40 | null, "startup_override": 40 | null,
   "hang": [command prefixes], "slow_configure": 0, "refuse": [config lines],
   "log": [...], "wedged": [[pid, child pid], ...]}
A call whose command starts with one of "hang" wedges (see wedge()); "wedged"
lists the processes of each such call. Tests change the state with state(),
the same locked read-modify-write the fake uses, and read it with load().
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def main():
    args = sys.argv[1:]
    if args[:2] != ["-p", "15"] or args[2] != "-c":
        print("% bad invocation: %r" % (args,))
        return 1
    cmds = args[3].split("\n")
    path = os.environ["FAKE_FASTCLI_STATE"]
    with state(path) as st:
        st.setdefault("log", []).append(cmds)
        delay = st.get("slow_configure") if cmds[0] == "configure" else None
        if not delay:
            out = respond(st, cmds)
    if delay:   # the change lands this long after the call is logged
        time.sleep(delay)
        with state(path) as st:
            out = respond(st, cmds)
    if out is None:
        wedge(path)
    if cmds[0].startswith("show system environment") and st.get("slow_reads"):
        time.sleep(st["slow_reads"])
    sys.stdout.write(out)
    return 0


@contextlib.contextmanager
def state(path):
    """The switch state, locked against other writers; saved atomically on exit
    so that load() never sees a half-written file."""
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load(path) if os.path.exists(path) else {}
        yield st
        with open(path + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(path + ".tmp", path)


def load(path):
    with open(path) as f:
        return json.load(f)


def read(path):
    with open(path) as f:
        return f.read()


def wedge(path):
    """A wedged CLI call that has started a process of its own. Both ignore
    SIGTERM, so only SIGKILL to the whole process group ends them, as
    fanctl's timeout sends. Their pids go to "wedged" for the tests to check."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # the child inherits it
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    with state(path) as st:
        st.setdefault("wedged", []).append([os.getpid(), child.pid])
    time.sleep(3600)


def respond(st, cmds):
    """The output of a call, or None if it wedges."""
    if any(cmds[0].startswith(prefix) for prefix in st.get("hang", ())):
        return None
    if cmds[0].startswith("show") and st.get("fail_all_shows"):
        return "% Internal error\n"
    if cmds == ["show system environment temperature | json"]:
        return read(os.path.join(FIX, "temp.json"))
    if cmds == ["show system environment cooling | json"]:
        doc = json.loads(read(os.path.join(FIX, "cooling.json")))
        ov = st.get("override")
        doc["coolingMode"] = "manual" if ov else "automatic"
        doc["overrideFanSpeed"] = ov or 0
        for slot in doc["fanTraySlots"] + doc["powerSupplySlots"]:
            for fan in slot["fans"]:
                fan["configuredSpeed"] = ov or 60
        return json.dumps(doc)
    if cmds == ["show system environment power | json"]:
        return read(os.path.join(FIX, "power.json"))
    if cmds[0].startswith("show running-config | include ^environment fan-speed"):
        return "environment fan-speed override %d\n" % st["override"] if st.get("override") else ""
    if cmds == ["show startup-config | include ^environment fan-speed"]:
        ov = st.get("startup_override")
        return "environment fan-speed override %d\n" % ov if ov else ""
    if len(cmds) == 3 and cmds[0] == "configure" and cmds[2] == "end":
        if cmds[1] in st.get("refuse", ()):
            return "% Unavailable command (not supported on this hardware platform)\n"
        if cmds[1] in ("environment fan-speed auto", "no environment fan-speed"):
            st["override"] = None
            return ""
        if cmds[1].startswith("environment fan-speed override "):
            st["override"] = int(cmds[1].split()[-1])
            return "=====\nWARNING: Overriding the system fan speed is unsupported\n=====\n"
    return "% Invalid input\n"


if __name__ == "__main__":
    sys.exit(main())

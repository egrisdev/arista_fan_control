"""Which syslog lines from a switch running fanctl matter, for tools/monitor.py
(which records and alerts on them) and tools/report.py (which lists them).

fanctl and fanctl-guard log every problem at warning or err severity, but
`show logging` doesn't print the severity, so the lines are told apart by their
text. NOTABLE matches every warning and err message the two can log (the table
in docs/OPERATIONS.md#monitoring lists them), fanctl taking the fans back from
EOS, ProcMgr stopping fanctl or holding back its restarts, and EOS's own
environment messages; none of fanctl's other notice or info messages.
tests/test_tools.py checks it against every message in fanctl.py and
fanctl-guard.sh, so a new warning or error there fails the tests until it is
added here.
"""
import re

# The lines worth recording: fanctl's and the guard's, ProcMgr's about a
# daemon, and EOS's environment and thermostat messages.
RECORD = re.compile(r"fanctl|PROCMGR-[0-9]-PROCESS_(TERMINATED|DELAYRESTART|RESTART|STARTED)|ENVMON|THERMOSTAT",
                    re.I)

NOTABLE = re.compile("|".join([
    # fanctl, err
    r"could not set ", r"could not restore automatic fan control", r"automatic fan control failed",
    r"EOS would not return to automatic fan control", r"EOS refused automatic fan control",
    r"\(set outside fanctl\) and: ", r"internal error", r"error while judging switch environment",
    r"not starting", r"stopping, and leaving the fans",
    # fanctl, warning
    r"handing fan control to EOS", r"fan control handed back to EOS", r"changed outside fanctl \(",
    r"it leaves the fans as someone else set them", r"could not read switch environment",
    r"sent but could not be confirmed", r"running-config says ", r"\bcannot (read|write|create) ",
    r"ignoring unreadable ", r"is locked by another fanctl", r"had been removed; lock taken again",
    r"report no usable EOS setpoint", r": fanctl-guard\.sh watches ",
    # fanctl, notice: the end of a hand-off
    r"taking fan control",
    # fanctl-guard, warning and err
    r"fanctl is not managing the fans", r"install both files from the same release",
    # ProcMgr and EOS
    r"PROCESS_TERMINATED: 'fanctl", r"PROCESS_DELAYRESTART", r"ENVMON",
]), re.I)

# fanctl's hourly summary, which report.py counts rather than lists.
STATUS = re.compile(r"status: fans")

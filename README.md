# fanctl

**Quiet, fail-safe fan control for Arista EOS switches.**

fanctl runs on the switch as an EOS daemon. It keeps the fans as slow as the switch's own thermal targets allow, and hands control straight back to EOS automatic fan control whenever anything is not clearly fine.

It is meant for fixed-configuration Arista switches where fan noise matters. So far it has been tested on one model, a DCS-7050TX-64-R running EOS 4.28.13.1M. On anything else, start with `--probe` and a dry run.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform: Arista EOS](https://img.shields.io/badge/platform-Arista%20EOS-informational.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

> [!WARNING]
> Overriding switch fan speed is **not supported by Arista**. EOS prints a warning that doing so may void your warranty. fanctl is provided **as is, without warranty of any kind** (see [LICENSE](LICENSE)). Read [Safety](#safety) and validate it on your own hardware before you rely on it.

## Contents

- [Why](#why)
- [Features](#features)
- [How it works](#how-it-works)
- [Safety](#safety)
- [Results](#results)
- [Compatibility](#compatibility)
- [Installation](#installation)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Uninstall](#uninstall)
- [Development](#development)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

## Why

In automatic mode, EOS runs the fans of fixed-configuration switches such as the DCS-7050TX-64 at about 60 %, even in a cool room. Operators who want them quieter typically set a static `environment fan-speed override`. A static override can't react: if a fan fails or the cooling fails, the fans stay at that speed until the switch reaches its overheat shutdown.

fanctl closes that gap:

- It regulates the fans against EOS's own per-sensor targets.
- It adapts to the room.
- It returns the switch to EOS's automatic control as soon as anything looks wrong.

On the reference switch, EOS automatic control ran the fans at 60 % with a 25 °C inlet. fanctl settled at 31 % there, as predicted by a thermal model fitted to that switch. In two field runs, with fanctl's hourly logs covering the gap between them, it then held that speed for more than 39 hours without a change. While it held 31 %, the hottest sensor stayed at least 2 °C under its target.

## Features

- **Closed-loop control against EOS's own thermal targets.** It reads every sensor's `setPointTemperature`, so nothing is hard-coded per platform.
- **Fan-authority weighting.** Sensors the fans can barely cool, such as a front-panel sensor that mostly tracks the room, are not chased with fan speed.
- **Fail-safe by default.** Any fault, implausible reading, unreadable switch, heat excursion, or stop of fanctl itself returns the fans to `environment fan-speed auto`.
- **An independent watchdog.** `fanctl-guard.sh` is a small bash daemon that restores automatic control if fanctl dies, hangs or never starts. It needs neither Python nor `fanctl.py`.
- **Low config churn.** Speed changes are rate-limited, with hysteresis and back-off: zero changes at a steady inlet once settled, and about 15–25 a day with a daily temperature swing.
- **Respects operators.** A manual fan setting is left alone; fanctl only steps back in to protect the hardware.
- **Observable.** It logs to syslog (facility local4), and `--status` and `--probe` explain every decision, including the *binding sensor*: the one currently setting the fan speed.
- **No dependencies.** It uses the Python 3 standard library only, and writes nothing to flash while running. It never runs `write memory`.
- **Tested.** Unit and end-to-end tests run against a simulator built from real switch data, on the switch's Python 3.9 and bash 4.2. The controller was chosen in a benchmark against PI, PID and model-based controllers, and the fan algorithm from Arista's SONiC platform library (see [docs/VALIDATION.md](docs/VALIDATION.md)).

## How it works

Every 15 s fanctl reads `show system environment temperature | json`, `… cooling | json` and `… power | json` through FastCli. The *inlet* is EOS's `ambientTemperature`, the temperature of the air entering the switch.

**Per-sensor targets.** For each sensor, fanctl computes how far it is from EOS's own control target, its "excess". The target is the lower of `setPointTemperature` and `targetTemperature`, counting only a plausible temperature (1 °C or more, as for a reading) below the sensor's alert limit (`overheatThreshold`). On the reference switch, EOS sets it to 0.65 × the alert limit for sensors without an explicit target. If neither key gives a usable value for a sensor (missing, below 1 °C, or at or above the alert limit), fanctl uses that same 0.65 × alert limit, a ratio measured only on the reference switch, and says so: a warning naming those sensors at the first poll and daily after, and in `--probe`.

**Authority weighting.** The fans move a sensor's temperature roughly in proportion to its rise over the inlet. So each excess is divided by that sensor's rise relative to the largest rise. The weight is floored at 0.5, and PSU sensors, which have their own fans, are not weighted. A sensor that mostly follows the room is held 1–3 °C under its target instead of the full 2–6 °C.

**Band control on the hottest weighted excess.** Decisions use each sensor's 1-minute mean:

| Hottest sensor vs. its target (weighted) | Action |
|---|---|
| more than 6 °C under, for the last 5 min | lower the fans 2 % every 5 min (floor 30 %) |
| more than 3 °C under, for the last 15 min | try a 2 % step down every 30 min, waiting longer if these trial steps keep being undone |
| within 2 °C | raise 5 % per minute |
| otherwise | no change |
| any raw reading at or over its target | raise 5 % at once; again within the minute only if the hottest raw excess is higher than at that raise, or one at target rose more than 1 °C over the last 4 polls (a load still building) |
| still over target after 60 s at the 70 % ceiling | hand control to EOS |

The full algorithm, the safety rules and the reasoning behind each parameter are in [docs/DESIGN.md](docs/DESIGN.md).

## Safety

fanctl hands control back to EOS (`environment fan-speed auto`) when any of these happens:

- a sensor is more than 5 °C over its target, less than 10 °C under its alert limit, in alert, or its alert count rises
- the inlet reaches 32 °C or can't be read
- temperature or cooling status is not Ok
- any fan, fan tray, PSU or sensor is not ok or disappears, or the airflow direction is unknown
- a reading is implausible, or a sensor has no usable alert limit (`overheatThreshold`)
- two reads in a row fail
- EOS's overheat or insufficient-fans shutdown protection is disabled
- an override it sets doesn't take effect

After a hand-off it holds EOS in control for 10–15 min (longer if hand-offs repeat). It only takes over again when the room and every sensor are comfortably cool, and never while EOS runs the fans above fanctl's ceiling. If EOS refuses to return to automatic control, fanctl sets 100 % rather than leave a low override in place.

**When fanctl itself stops**, the guard covers it. EOS's `shutdown` of a daemon is a SIGKILL, and fanctl could also crash or hang. `fanctl-guard.sh` watches fanctl's lock and heartbeat, and restores automatic control if an override is in effect but no live fanctl manages it. That takes about a minute if fanctl is stopped or dies. If fanctl hangs, the guard waits until its heartbeat is 5 minutes old, so it can take up to about 6 minutes. The SIGKILL and the guard's restore were both verified on hardware. The guard trusts the lock and heartbeat only in a directory no other user can write to; by default that is `/run/fanctl`, owned by root.

**Reloads.** After a reload, the fans run at whatever override was last saved to startup-config until fanctl starts. If it doesn't start, the guard restores automatic control after a 3-minute grace period. If the guard can't run either (for example, its file was deleted from flash), the saved override stays.

**Limitations.**

- fanctl doesn't read transceiver temperatures.
- A sensor excluded with `--ignore-sensor` gets no safety checks at all.
- On hardware, fanctl's own hand-off to EOS has been tested twice, both times triggered by its inlet limit (set below the room temperature for the test). The second test, on 1.3.1, also confirmed that a hold survives a SIGKILL restart with its extensions. The other triggers, and the fallbacks for when EOS refuses automatic control, have been tested only off-box.
- It is undocumented whether EOS still forces full fan speed at an alert limit while an override is set. fanctl doesn't rely on it.
- 1.4.0 has run on hardware only in a steady state, for 24 hours with nothing to react to. Most of what 1.3.2 and 1.4.0 changed acts only when something goes wrong, and has been tested only off-box and in the benchmark.

More in [DESIGN.md](docs/DESIGN.md#known-limitations) and [VALIDATION.md](docs/VALIDATION.md#not-verified).

## Results

These are from the reference switch, a DCS-7050TX-64-R with 48 × 10GBASE-T ports. The details, method and raw data are in [docs/VALIDATION.md](docs/VALIDATION.md).

| Inlet | EOS automatic | fanctl 1.3 | Binding sensor |
|---|---|---|---|
| 25 °C | 60 % (measured) | **31 %** (simulated; measured in the field) | 10GBASE-T PHY groups |
| 28 °C | n/a | **35 %** (simulated) | 10GBASE-T PHY groups |
| 30 °C | n/a | **40 %** (simulated) | front-panel sensor |

- **Measured with a step test on the switch:**
  - At 30 % fan in a 25.5 °C room, the hottest PHY sensors settle at 63 °C. Their target is 65 °C and their alert limit 100 °C.
  - Total switch power was flat at 163–167 W across 30–70 % fan, so the benefit is **acoustic**. By the fan laws, 31 % instead of EOS's 60 % is about 14 dB less fan noise. That is an estimate; noise was not measured.
- **Simulated with a thermal model fitted to that step test** (RMS error 0.2–0.6 °C):
  - With the standard noise seed, no scenario went over a target.
  - In 20 runs of a sudden heat load, 3 read up to 1 °C over target for at most a minute. Hand-off to EOS is at 5 °C over.
  - At a steady inlet, it settles and stops changing.
- **Fan-authority weighting** (new in 1.3) runs the fans 2–16 percentage points slower than unweighted band control in rooms at 28 °C and above.

## Compatibility

| | |
|---|---|
| **Tested on** | DCS-7050TX-64-R (QSFP+ cages empty), EOS 4.28.13.1M, Python 3.9.17 |
| **Should work on** | fixed-configuration EOS switches that support `environment fan-speed override`, report `setPointTemperature` / `overheatThreshold` for every sensor in `show system environment temperature \| json`, and report `ambientTemperature` in `show system environment cooling \| json`. Run `--probe` first; other platforms are untested. |
| **Not for** | modular chassis (untested); switches whose optics run hot (fanctl doesn't read transceiver temperatures) |

On-box requirements:

- `/usr/bin/python3`, 3.9 or newer. The switch has 3.9.17, and the off-box tests run on 3.9, 3.10 and 3.12; older versions are untested. On EOS 4.28, `/usr/bin/arista-python` can be Python 2.7, which won't work.
- `FastCli`, and EOS's `daemon … exec` configuration.
- For the guard: bash with `flock`, `stat`, `date`, `timeout`, `readlink`, `dirname`, `grep` and `logger`. All were checked on EOS 4.28; the [pre-flight checks](docs/OPERATIONS.md#2-pre-flight-checks-read-only) confirm them.

## Installation

The short version is below. The full procedure, with what to expect at each step and the on-switch tests to run, is in [docs/OPERATIONS.md](docs/OPERATIONS.md).

1. **Check the files, copy them to flash, and check them again on the switch.** The hashes on the switch must match [`SHA256SUMS`](SHA256SUMS). That shows the copy is intact, not where it came from, so download from a source you trust.

   ```text
   $ sha256sum -c SHA256SUMS
   $ scp fanctl.py fanctl-guard.sh admin@switch:/mnt/flash/
   switch# bash sha256sum /mnt/flash/fanctl.py /mnt/flash/fanctl-guard.sh
   ```

   `scp` needs a login that lands at privilege 15. Otherwise, pull the files from the switch with `copy`; see [OPERATIONS](docs/OPERATIONS.md#1-copy-the-files).

2. **Probe.** This is read-only: it shows every sensor, its target and margin, and what fanctl would do. On a model other than the reference switch, work through [Trying another model](docs/OPERATIONS.md#trying-another-model) first.

   ```text
   switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --probe
   ```

3. **Start fanctl, then the guard.**

   ```text
   switch# configure
   switch(config)# daemon fanctl
   switch(config-daemon-fanctl)# exec /usr/bin/python3 /mnt/flash/fanctl.py
   switch(config-daemon-fanctl)# no shutdown
   switch(config-daemon-fanctl)# exit
   switch(config)# daemon fanctl-guard
   switch(config-daemon-fanctl-guard)# exec /usr/bin/bash /mnt/flash/fanctl-guard.sh loop
   switch(config-daemon-fanctl-guard)# no shutdown
   switch(config-daemon-fanctl-guard)# end
   ```

4. **Check that both daemons are running.**

   ```text
   switch# show daemon fanctl
   switch# show daemon fanctl-guard
   switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --status
   switch# show logging | include fanctl
   ```

5. **Test the stop path, then save.** The test is in [OPERATIONS step 7](docs/OPERATIONS.md#7-test-the-stop-path), with the checklist before `write memory` in [step 8](docs/OPERATIONS.md#8-save).

Upgrading is covered in [OPERATIONS](docs/OPERATIONS.md#upgrading).

## Configuration

Settings are command-line options on the daemon's `exec` line. Write them without quotes, and name sensors by their `TempSensorN` name rather than their description, which contains spaces. Then run `shutdown` and `no shutdown` under `daemon fanctl`. `bash /usr/bin/python3 /mnt/flash/fanctl.py --help` lists every option; [docs/DESIGN.md](docs/DESIGN.md#parameters) explains each.

| Option | Default | Effect |
|---|---|---|
| `--floor` / `--ceiling` | 30 / 70 | Lowest and highest override fanctl sets. EOS accepts 30–100. |
| `--target-offset` | 0 | Added to every target. Negative means cooler, positive quieter. |
| `--keep-below` / `--band` | 2 / 4 | Raise within `keep-below` °C of a target; lower when more than `keep-below + band` °C under. |
| `--min-authority` | 0.5 | Floor on a weak sensor's weight. `1` treats every sensor alike (unweighted band control). |
| `--ambient-max` | 32 | Inlet temperature at which control goes back to EOS. |
| `--ignore-sensor NAME` | | Leave out a sensor whose hardware status is permanently failed, or that reports no usable alert limit (none, or outside 1–150 °C). An ignored sensor gets **no** safety checks. |
| `--ignore-psu N` | | Leave out a PSU that is deliberately unpowered. |

## Monitoring

fanctl and the guard log to syslog facility **local4**, tagged `fanctl` and `fanctl-guard`. Forward them with `logging host <collector>`, and **alert on every err-severity message from either tag**.

Day to day:

- `fanctl.py --status` shows the current setting, the binding sensor, and the hottest sensors with their weights. It returns non-zero if fanctl is not running or hung, or if its runtime directory is unsafe.
- fanctl logs an hourly summary.
- Every speed change is logged with its reason.

The message reference is in [docs/OPERATIONS.md](docs/OPERATIONS.md#monitoring).

## Uninstall

```text
switch# configure
switch(config)# no daemon fanctl-guard
switch(config)# no daemon fanctl
switch(config)# environment fan-speed auto
switch(config)# end
switch# write memory
switch# delete flash:fanctl.py
switch# delete flash:fanctl-guard.sh
```

Stopping fanctl does not restore automatic control by itself, because EOS kills daemons with SIGKILL. Hence the explicit `environment fan-speed auto`. If you ran a fixed override before fanctl, set that `environment fan-speed override N` instead.

## Development

```text
python3 -m unittest discover -s tests -v      # ~4 min, no switch needed
```

The suite runs on Linux. The tests of `fanctl-guard.sh` also need what the switch has: bash, GNU coreutils and util-linux `flock`. Where something is missing, the tests that need it are skipped with the reason, and so is the one that only root can run. [CONTRIBUTING.md](CONTRIBUTING.md) says how to run it on the switch's Python 3.9 and bash 4.2, and what a change must keep. [VALIDATION.md](docs/VALIDATION.md#off-box-test-suite) lists what the tests cover. The tests keep their files in a private directory under `$TMPDIR`, or under `/tmp` if `$TMPDIR` is inside a directory that others can write to, since fanctl and the guard refuse such directories.

| Path | Contents |
|---|---|
| `fanctl.py` | the controller daemon |
| `fanctl-guard.sh` | the watchdog daemon |
| `tests/` | unit and end-to-end tests: `sim.py` simulates the switch from real JSON fixtures, `fake_fastcli.py` stands in for FastCli, and `fake_paramiko.py` for the SSH session the off-box tools open |
| `measurements/` | step-test data from a real switch, plus analysis and model-building scripts |
| `bench/` | the controller benchmark, candidate controllers and results |
| `tools/` | `monitor.py`, a read-only off-box data collector (SSH) for field validation; `report.py`, which summarises its data; `eos.py`, the SSH session to the switch that `monitor.py` and the step test share, and the environment-JSON checks `report.py` also uses for its `--targets` file (`report.py` needs no paramiko); `fanlog.py`, which log lines both tools treat as notable |
| `docs/` | design, operations and validation documents |
| `SHA256SUMS` | checksums of the two files installed on the switch |
| `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md` | release history; security model and how to report a vulnerability; how to test and change the code |
| `.github/workflows/` | CI: the suite on Python 3.9 with bash 4.2 (`tests/eosenv/Dockerfile`) and on 3.10 and 3.12, `SHA256SUMS` and shellcheck |

## FAQ

**Why not a PID controller?**
Benchmarked on a model of the real switch, the derivative term added nothing: whole-degree sensor readings swamp it. A PI controller only worked after the same non-textbook additions band control already has, and it wasn't meaningfully quieter. Details are in [docs/VALIDATION.md](docs/VALIDATION.md#controller-comparison).

**Does running the fans slower save power?**
Not measurably. The test switch's total draw stayed within 163–167 W from 30 to 70 % fan, because the chips draw more when they run warmer. The benefit is noise. What did save power was shutting the unused 10GBASE-T ports: about 0.8 W each, 30 W in all, and the PHY chips with no live port ran up to 20 °C cooler ([details](docs/VALIDATION.md#shutting-unused-ports-2026-10-21-to-2026-10-23)).

**What happens if fanctl crashes, hangs or is stopped?**
The guard restores `environment fan-speed auto` (or, like fanctl, sets `override 100` if EOS refuses every way back to automatic control). That takes about a minute after a crash or stop (up to 4 if the guard itself started less than 3 minutes earlier). A hang takes up to about 6 minutes, because the guard waits until fanctl's heartbeat is 5 minutes old.

**What happens after a reload?**
The saved override applies at boot, then fanctl takes over within seconds of starting. If fanctl doesn't start, the guard restores automatic control after its 3-minute grace period.

**Can I still set the fans by hand?**
Yes. fanctl notices a manual change on its next poll and leaves the fans alone until it is restarted. The one exception: a manual override below 70 % is still handed to EOS if something gets hot or fails. For long-term manual control, stop both daemons.

**Will it fight my configuration management?**
fanctl changes only the `environment fan-speed` line, and only in running-config. Tools that diff or enforce running-config will see that line change. Anything that runs `write memory` saves the current value, which is harmless.

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) says how to run the tests and what the on-box files must stay compatible with.

Results from other platforms are especially useful. When opening an issue, include:

- the switch model and EOS version
- the output of `fanctl.py --probe`
- `show logging | include fanctl`
- ideally, a step test (`measurements/fan_step_test.py`; stop fanctl and fanctl-guard first, see [measurements/README.md](measurements/README.md#running-a-step-test-on-your-own-switch))

Remove hostnames and serial numbers first.

Please report security problems privately, as described in [SECURITY.md](SECURITY.md), not in a public issue.

## License

[MIT](LICENSE). Use it for anything you like. It comes with **no warranty**.

Arista and EOS are trademarks of Arista Networks, Inc. SONiC is a trademark of its respective owner. This project is not affiliated with or endorsed by Arista Networks or the SONiC project.

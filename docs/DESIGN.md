# fanctl design

This document describes how fanctl decides what to do and why. It covers the version in this repository, 1.4.0. For installation see [OPERATIONS.md](OPERATIONS.md); for the evidence behind the choices see [VALIDATION.md](VALIDATION.md). The [glossary](#glossary) defines the terms and gives the names the code, the log and the status file use for them.

- [Goals](#goals)
- [Architecture](#architecture)
- [The control loop](#the-control-loop)
- [The safety layer](#the-safety-layer)
- [Operator changes](#operator-changes)
- [State and restarts](#state-and-restarts)
- [The guard](#the-guard)
- [Parameters](#parameters)
- [Design decisions](#design-decisions)
- [Known limitations](#known-limitations)
- [Glossary](#glossary)

## Goals

1. **Quiet.** Run the fans as slowly as the switch's own thermal targets allow.
2. **Never worse than EOS.** When anything is uncertain, give the fans back to EOS's automatic control (`environment fan-speed auto`). Never leave a low override in place unattended.
3. **Calm.** Each speed change is a running-config commit and a syslog line, so settle and stay put rather than hunt.
4. **Simple enough to trust.** Use the standard library only, keep the parameters few and physically meaningful, and log every decision with its reason.

Power is explicitly *not* a goal. On the reference switch, total power was flat from 30 to 70 % fan ([VALIDATION.md](VALIDATION.md#step-test)).

## Architecture

EOS's process manager (ProcMgr) runs both scripts as daemons.

```text
            +--------------------------- switch ---------------------------+
            |                                                              |
 ProcMgr -->|  fanctl.py (daemon)            fanctl-guard.sh (daemon)      |
            |    every 15 s:                   every 60 s:                 |
            |    FastCli: show ... | json      lock held? heartbeat fresh? |
            |    assess -> decide -> apply     not a dry run? safe dir?    |
            |    write /run/fanctl heartbeat   else: environment fan-speed |
            |    holds /run/fanctl/fanctl.lock       auto                  |
            |                                                              |
            |  EOS Thermostat: obeys the override, or runs its own         |
            |  automatic control when there is none                        |
            +--------------------------------------------------------------+
```

`fanctl.py` is built in layers. Each layer is a separate class or function, and all of them are tested off-box.

| Layer | Code | Job |
|---|---|---|
| Input | `Cli`, `read_environment` | Runs FastCli with timeouts, treats `% ` error lines as failures, and parses the three JSON documents. |
| Judgement | `assess()` → `Reading` | Turns raw JSON into usable sensors (current, target, alert limit), equipment and data **faults**, and thermal **hot** conditions. |
| Decision | `Controller.decide()` | Pure logic with no I/O: the safety layer, holds and resume, then `regulate()`, the in-band speed decision. The Controller alone changes its state, including the setting it wants (`Controller.setting`): `decide()` takes the reading and any setting someone else has put in effect, and a rejected override comes back to it through `release()`. Each poll's `Decision` (event and reasons) is what the log and status file report. |
| Action | `Daemon.apply()` / `set_fans()` | Writes the setting the Controller wants through FastCli and confirms it from the cooling JSON, with running-config as a fallback. If an override doesn't take, it hands the fans to EOS in the same poll, and that hand-off is the poll's decision. |
| Bookkeeping | `Daemon` | Status heartbeat, restart memory, syslog, hourly summary, reconciliation with changes made outside fanctl (`reconcile()` reads running-config and logs; the pure `judge_setting()` decides). |

## The control loop

### Targets

For every usable sensor, fanctl uses EOS's own control target: `setPointTemperature` or `targetTemperature`, whichever is lower. A value is usable when it is a plausible temperature (1–150 °C, the range every reading and alert limit must be in too) below the sensor's alert limit. If neither is usable, it uses `target_factor × overheatThreshold` (0.65 by default, which matches the targets EOS 4.28 reports on the reference switch). That ratio has been measured on one model only, so fanctl treats it as a guess it must show: it logs a warning naming those sensors at the first poll and daily after, and `--probe` prints the same warning before its verdict. It still controls against them; the release at target + `release_excess` and alert − `alert_margin` still protects them. The **excess** is `current − target`; a negative number means under target.

### Fan-authority weighting

The fans move a sensor's temperature in proportion to its **rise over the inlet**. Measured on the reference switch, rise ≈ R × (40 / fan%)^0.8 for everything that heats, where R is the sensor's rise at 40 % fan ([VALIDATION.md](VALIDATION.md#step-test)). A sensor with a small rise, such as a front-panel sensor that mostly follows the room, can hardly be cooled by the fans at all. Holding it 2–6 °C under its target just drives the fans up for nothing.

So each sensor's excess is divided by its weight:

```text
weight = max(min_authority, rise / largest_rise)     rise = 4-poll mean − inlet  (at least 1 °C)
weighted excess = excess / weight
```

- `largest_rise` is taken over switch sensors only.
- PSU sensors are cooled by the PSUs' own fans, so they're neither weighted nor allowed to set the reference.
- With `min_authority = 0.5`, a weak sensor is held between 1 and 3 °C under its target instead of 2 and 6 °C.
- With `min_authority = 1`, every sensor is treated alike. That is unweighted band control, the behaviour of the pre-release 1.2.

### Band control

Decisions use the hottest weighted excess:

- `smooth`: computed from each sensor's mean over the last `smooth` (4) polls, which filters whole-degree sensor noise.
- `latest`: computed from the latest readings.

| Condition | Action |
|---|---|
| Any raw (unweighted) reading at or over its target | +`up_step` (5 %) at once. After a raise, again at once only if the hottest raw excess is higher than at that raise, or a reading at or over its target rose more than 1 °C within the last `smooth` polls (a load still building, even if that climb came before the raise); otherwise after `up_interval` (60 s) |
| `smooth > −keep_below` (within 2 °C) | +`up_step` at most every `up_interval` (60 s) |
| `latest` and every `smooth` over the last `down_interval` are below `−(keep_below + band)` (6 °C under) | −`down_step` (2 %) every `down_interval` (300 s) |
| `latest` and every `smooth` over the last `probe_interval / 2` are below `−(keep_below + probe_margin)` (3 °C under) | a *probe*: −`down_step` after `probe_interval × backoff` (30 min) with no change |
| otherwise | no change |

"Over the last N s" includes the poll exactly N s ago. With 15 s polls, the fans step down at the first poll more than `down_interval` after the last one that wasn't cool enough.

Why the rules are shaped this way:

- **The band** (4 °C) is wider than the temperature change one 2 % step causes on the reference switch. That's what lets the fans settle instead of hunting.
- **The probe** keeps the fans from getting stuck high after a warm spell. Without it, the no-change zone inside the band would keep whatever speed the warm spell left behind.
- **Probe back-off.** If a probe is undone by a raise within `probe_interval`, `backoff` doubles, up to `hold_max / probe_interval`. A probe that isn't undone within `probe_interval` halves it. The back-off survives a restart.
- **At the ceiling** (70 %), if a reading is still at or over target after `ceiling_dwell` (60 s), fanctl hands control to EOS, which can run the fans faster.

### Taking over from EOS

When fanctl takes (or retakes) control, it starts at the speed EOS is running (at least the floor) and creeps down from there. It doesn't take over while EOS runs the fans above the ceiling, whether at startup or after a hand-off. EOS's speed is its own evidence that the switch needs more cooling than fanctl allows, possibly for heat fanctl can't see (optics, or a sensor it skips or ignores). Taking over would cut that speed to the ceiling in one step on the strength of only the sensors fanctl reads. On the reference switch EOS runs about 60 %, under the ceiling, so this costs nothing there; on a model whose automatic speed is normally higher, raise `--ceiling` or leave the fans to EOS.

After a *fault* hand-off, it resumes at its previous speed (or EOS's current speed, if lower), provided nothing has got hotter (excess within 0.5 °C, inlet within 1 °C). A fault is something like a brief fan-status glitch, where the old speed was evidently fine.

## The safety layer

`assess()` classifies every reading. Anything not clearly fine hands the fans to EOS.

| Category | Conditions |
|---|---|
| **hot** | a sensor more than `release_excess` (5 °C) over its target, less than `alert_margin` (10 °C) under its alert limit, `inAlertState`, or its `alertCount` rising; inlet at or above `ambient_max` (32 °C) |
| **fault** | temperature or cooling status not Ok; any fan, fan tray, PSU or sensor status not ok; airflow direction unknown; a missing reading, or one outside 1–150 °C (an unpowered PSU reads 0.0 °C); a sensor or fan disappearing (fan-tray and PSU fans both count); sensor or fan data in an unexpected shape; a sensor with no usable alert limit (`overheatThreshold` missing, or outside 1–150 °C) unless excluded with `--ignore-sensor`; the inlet unreadable; overheat or insufficient-fans shutdown disabled; no usable sensors |
| **unreadable** | two reads in a row fail (timeout, error, garbled output) |
| **write failure** | an override that doesn't show up afterwards |

Every safety check uses **raw, unweighted** readings. Weighting only affects how close to a target the in-band logic lets a sensor sit.

### Holds and resume

After a hand-off, fanctl holds EOS in control:

- **For 10 minutes after a fault, 15 after a hot reading.** Each further problem extends the hold.
- **Doubling on repeats.** A hold of either kind doubles, up to `hold_max` (4 h), if hand-offs keep coming less than `flap_window` (2 h) after taking over.

To take over again, all of these must be true:

- the hold has expired
- the inlet is at or below `ambient_resume` (30 °C)
- the hottest weighted excess is at or below `resume_excess` (−2 °C)
- every sensor is at least `alert_margin + keep_below` (12 °C) under its alert limit
- EOS is running the fans at or below the ceiling (see [Taking over from EOS](#taking-over-from-eos))

### Writing the setting

`set_fans()` sends `configure / environment fan-speed … / end` in one FastCli call. It then confirms the result from the cooling JSON (`coolingMode`, `overrideFanSpeed`), falling back to `show running-config | include ^environment fan-speed`.

- **Reads back differently:** the change failed.
- **Command went through cleanly, but nothing can be read back:** the change is assumed to have worked, and the next poll checks. If the command itself reported an error, it counts as a failure.
- **Returning to automatic control:** fanctl tries `environment fan-speed auto`, then `no environment fan-speed`. If EOS refuses both, it sets `override 100` and retries auto every 5 minutes. A low override never stays in place. The guard takes the same three steps (a test pins them to fanctl's) at every check while fanctl is down, so it tries auto again every 60 s.

fanctl never runs `write memory`.

### Signals

On SIGTERM, SIGINT or SIGHUP, fanctl deletes its heartbeat, saves its restart memory and restores automatic control. It leaves the fans alone if they were set outside fanctl, or if it is a dry run. Signals that arrive while a FastCli child is starting are deferred, so the child is killed rather than orphaned.

EOS's `daemon … shutdown` sends **SIGKILL**, which can't be caught. That case is the guard's job.

## Operator changes

The cooling JSON shows the setting in effect every poll. When it differs from what fanctl last set, fanctl checks running-config in the same poll:

- If running-config still shows fanctl's setting, the status merely lagged, and fanctl carries on.
- If running-config can't be read, fanctl writes nothing in that poll. At the next poll in doubt it believes the cooling JSON, unless running-config has shown fanctl's setting in between: that poll was a lag, and the doubt starts over.
- If someone else changed it, fanctl **pauses** until it is restarted. It writes nothing, with one exception: if the manual setting is an override below the ceiling and something becomes hot or faulty, it hands the fans to EOS, and if that write fails it tries again at every poll until EOS has them. While paused with the fans in auto (after that hand-off, or a manual `auto`), each problem still extends the [hold](#holds-and-resume), so the restart that ends the pause keeps it.
- A manual `auto`, or a manual override at or above the ceiling such as an emergency `override 100`, is never touched.

## State and restarts

| File (in `/run/fanctl`, RAM) | Purpose |
|---|---|
| `fanctl.lock` | `flock` held for the life of the process. The guard and `--status` use it to tell whether fanctl is alive. It is the one token for writing the fan setting: the guard holds it while it checks and restores, and a second instance, or a fanctl that starts while the guard holds it, waits on it. Only a lock on the file now at the path counts: a fanctl that gets the lock checks that the file it locked is still the one at the path, and tries again if not. A running fanctl checks at every poll: if the file was removed it takes the lock on a new one, and if another process already holds the new one, it exits without touching the fans, heartbeat or memory, which are that process's now. |
| `fanctl.status.json` | Heartbeat and status, rewritten every poll and at startup. `--status` reads it. |
| `fanctl.memory.json` | Hand-off holds, probe back-off and the last speed, so a restart doesn't drop a hold in progress and, if it finds the fans in auto, can take them back at its last speed. Written at every poll, like the heartbeat (both live in RAM), and whenever fanctl exits on its own (a stop signal or an internal error). So a SIGKILL restart (EOS's daemon `shutdown`) loses at most one poll: of an extended hold, of a back-off change, or of the readings that back the last speed, even after hours without a change. While paused, the speed is someone else's, so it is not recorded as fanctl's own. Ignored once older than `hold_max` (4 h). A damaged file (a field of the wrong type, a number that isn't finite or is too large for a float) is ignored whole with a warning; a restored hold is never longer than `hold_max`. Dry runs don't write it. |

Nothing is written to flash while running.

`--state-file`, `--memory-file` and `--lock-file` move these files, for the tests and for runs by hand without root. Leave the heartbeat and lock where they are under the daemon: the guard watches `/run/fanctl` (unless its `FANCTL_LOCK` and `FANCTL_STATE` name other paths) and can't learn other paths from fanctl's `exec` line, so it would find no live fanctl and restore automatic control every minute, and fanctl would pause. A fanctl started with either option, except in a dry run, logs `WARNING …: fanctl-guard.sh watches …` at startup, naming the moved files, so the cause shows next to the `fan setting changed outside fanctl` that follows. It warns rather than refuses because the end-to-end tests and runs by hand without root need these options outside a dry run. `--status` needs the same options as the fanctl it reads.

fanctl creates `/run/fanctl` if it is missing, owned by root with mode 0755 (its files 0644). Before anything else, it checks each directory that holds these files. If another user could plant or change files in one, fanctl logs `unsafe runtime directory …; not starting` and exits ([SECURITY.md](../SECURITY.md#runtime-files)). `--status` makes the same check and prints `UNTRUSTED` if it fails.

## The guard

`fanctl-guard.sh loop` runs as its own EOS daemon. After a 180 s boot grace period, it checks every 60 s. If fanctl isn't alive and an override is in effect (or the setting can't be read), it restores automatic control the way fanctl does ([Writing the setting](#writing-the-setting)): `environment fan-speed auto`, then `no environment fan-speed`, then `override 100` if EOS refuses both, verifying each step. It logs the result. If it can't read the setting back, it stops and logs that it failed rather than guess.

It holds fanctl's lock from deciding to restoring, so a fanctl that starts meanwhile waits for it, then reads the setting the guard left, instead of finding it changed under it and pausing. When a hung fanctl or a dry run holds the lock, or the lock's directory can't be trusted, the guard restores without it.

The guard carries fanctl's version number (a test pins it), since only a fanctl and a guard from the same release are known to work together. It logs the number when it starts, and warns once if a live fanctl's heartbeat names another version.

fanctl counts as alive only if all four hold:

- the directories of its lock and heartbeat pass fanctl's ownership and permission checks, applied with every symlink resolved, so no other user can have faked them (unlike fanctl, the guard doesn't also check the path as written; see [SECURITY.md](../SECURITY.md#runtime-files))
- it holds the lock
- its heartbeat is at most 300 s old, and no more than 300 s in the future
- it isn't a dry run

The guard needs only bash, coreutils, `grep`, `logger` and FastCli. It uses `flock` if present, and wraps every FastCli call in `timeout` if that is present, which kills a call that runs too long with SIGKILL, as fanctl does. It keeps working if `fanctl.py` or Python is broken.

Because the guard waits for a stale heartbeat, a hung fanctl (one that still holds the lock) is caught after about 5–6 minutes. A stopped or crashed one is caught within about a minute.

## Parameters

All of these are command-line options (`--keep-below`, and so on).

| Parameter | Default | Meaning |
|---|---|---|
| `poll` | 15 s | time from the start of one poll to the next (at once after a poll that took longer) |
| `floor` / `ceiling` | 30 / 70 % | override range fanctl uses (EOS accepts 30–100) |
| `keep_below` | 2.0 °C | raise when the weighted hottest is within this of its target |
| `band` | 4.0 °C | lower when more than `keep_below + band` under |
| `up_step` / `up_interval` | 5 % / 60 s | increase size, and the wait between increases (at target too, except as [Band control](#band-control) says) |
| `down_step` / `down_interval` | 2 % / 300 s | decrease size, and the wait since the last change |
| `probe_margin` / `probe_interval` | 1.0 °C / 1800 s | probe when more than `keep_below + probe_margin` under and nothing has changed for this long |
| `ceiling_dwell` | 60 s | time at the ceiling, still over target, before handing to EOS |
| `smooth` | 4 polls | readings averaged for in-band decisions |
| `min_authority` | 0.5 | floor on a sensor's authority weight (1 = unweighted, as in 1.2) |
| `release_excess` | 5.0 °C | over target → hand to EOS |
| `resume_excess` | −`keep_below` | the hottest weighted excess must be at or below this to take over |
| `alert_margin` | 10.0 °C | within this of an alert limit → hand to EOS |
| `ambient_max` / `ambient_resume` | 32 / `ambient_max − 2` °C | inlet limits for handing back and taking over |
| `target_factor` / `target_offset` | 0.65 / 0 °C | target fallback (× alert limit), and an offset added to every target |
| `hot_hold` / `fault_hold` | 900 / 600 s | hold after the last hot or faulty reading |
| `hold_max` / `flap_window` | 14400 / 7200 s | cap for doubling holds; window in which a repeat doubles them |
| `read_failures` | 2 | consecutive failed reads before handing back |
| `cli_timeout` | 30 s | per FastCli call |
| `reconcile` | 300 s | running-config cross-check interval |
| `summary` | 3600 s | syslog status summary interval; each counts the changes since the one before |

Every parameter has a safe range, kept next to its default in fanctl.py's `TUNABLES` table and shown by `--help` (`--poll INT (default 15; 5..120 s)`). `check_config()` refuses a value outside its range, for example `release_excess` above 15 °C, `alert_margin` under 3 °C or `ambient_max` above 40 °C. It also checks the rules that relate two parameters: `floor` ≤ `ceiling`, `probe_margin` < `band`, `resume_excess` ≤ −`keep_below`, `ambient_resume` within 15 °C below `ambient_max`, `down_interval` ≤ `probe_interval` ≤ `hold_max`, and neither hold longer than `hold_max`. A daemon with bad options logs `invalid settings, not starting` or `bad command line` and doesn't run.

## Design decisions

**Hand back to EOS, not 100 %.** EOS's automatic control is Arista's validated algorithm and will go to full speed when needed. Leaving it in charge after a problem is safer than any fixed speed, and quieter than 100 %. 100 % is used only if EOS refuses to go back to automatic.

**Band control rather than PID.** This was tested rather than assumed; see [VALIDATION.md](VALIDATION.md#controller-comparison). On this plant:

- every speed change has a cost
- sensors read in whole degrees
- fan authority varies about 3× across the speed range and widely between sensors

A derivative term contributed nothing. A PI controller needed the same non-textbook additions band control has, and then performed about the same. Band control is easier to reason about and to explain in a log line.

**Weighting instead of per-sensor tuning.** Scaling margins by each sensor's measured rise over the inlet needs no per-platform tables. It adapts as conditions change, and it fixed band control's one measurable flaw: chasing an inlet-following sensor in warm rooms.

**Rejected refinements.** Both were tried in the benchmark and documented in [bench/README.md](../bench/README.md):

- **A 2-reading debounce on the immediate raise.** It made sudden heat loads go over target more often, and didn't reduce hunting. 1.3.2's wait after a raise is different: the first raise is still immediate, and so is every later one while the temperatures are still rising. A first version that judged "rising" from the latest whole-degree reading alone failed the same way (18 of 20 load steps over target), because readings can sit still while a load builds. 1.3.2 added "the hottest `smooth` is higher than at that raise", but a mean lags: after a 1 °C step onto the target it kept rising for three more polls with the reading flat, so the fans still went up 5 % at every poll. Comparing the latest weighted excess instead brought back the 18 of 20. What counts since 1.4.0 is a climb of more than 1 °C within the `smooth` window by a reading at target: whole-degree rounding can't produce that, so the load is still building (4 of 20 load steps over target, against 3 for 1.3.1 and 1.3.2).
- **Back-off on every decrease.** It made hunting worse on exaggerated test plants.

## Known limitations

- **Optics.** Transceiver temperatures aren't read. On a switch with hot optics, a front-panel sensor is only a rough proxy.
- **Exaggerated test plants.** On a simulated switch with three times the reference switch's sensor noise, or twice its fan sensitivity, 1.3 makes 30–36 speed changes a day at a steady 28 °C, where unweighted band control makes 2. `--min-authority 1` restores unweighted control.
- **Ignored sensors.** A sensor excluded with `--ignore-sensor` gets no checks at all, including the over-temperature hand-off.
- **Reloads without the guard.** If neither daemon can start after a reload, the override saved in startup-config stays in place.
- **Alert-limit behaviour under an override.** Whether EOS still forces full fan speed at an alert limit while an override is set is undocumented. fanctl assumes it doesn't, and hands control back 10 °C before any alert limit.
- **Daemon shutdown.** `shutdown` of the daemon (SIGKILL) leaves the last override in place until the guard acts, within about a minute.
- **Field validation.** Only one model has been tested, and the long-run field data is in [VALIDATION.md](VALIDATION.md#field-validation).

1.3.1 fixed two limitations of 1.3.0: runtime files in `/tmp`, which any local user could tamper with, and holds that lost their extensions across a SIGKILL restart ([found on hardware](VALIDATION.md#hand-off-test-2026-10-20), and the fix [confirmed there](VALIDATION.md#hand-off-test-on-131-2026-10-21)). 1.3.2 makes the at-target raise wait for the fans while the temperatures aren't rising, and 1.4.0 makes that wait hold after a 1 °C step onto the target. 1.4.0 also hands the fans to EOS for a sensor without an alert limit, checks PSU fans like fan-tray fans, retries a failed hand-off while paused, waits under EOS control while EOS runs the fans above the ceiling, and makes the guard take turns with fanctl. See the [changelog](../CHANGELOG.md).

## Glossary

"Hold" means only time under EOS control, and "probe" without dashes only the trial step down. Where the code uses another name, it is given.

| Term | Meaning | In the code, log and status |
|---|---|---|
| target | A sensor's control target: the lower of EOS's `setPointTemperature` and `targetTemperature`, else `target_factor` × its alert limit, plus `target_offset`. | `Sensor.target`, `pick_target()` |
| alert limit | EOS's `overheatThreshold` for a sensor. | `Sensor.alert` |
| excess | A sensor's reading minus its target, in °C. Negative means under target. | `Sensor.excess` |
| inlet | The temperature of the air entering the switch, EOS's `ambientTemperature`. | `Reading.ambient`, `ambient_max` |
| authority, weight | How strongly the fans move a sensor: its rise over the inlet relative to the largest rise, at least `min_authority`. | `Controller._weighted()` |
| weighted excess | Excess divided by the sensor's weight. Band control acts on the hottest one, both `smooth` (the 4-poll mean) and `latest`. | `smooth`, `latest` |
| binding sensor | The sensor with the hottest weighted excess, the one setting the fan speed. | `Controller.binding`; `binding: …` in the log, the status file and `--probe` |
| band | The range under target in which band control leaves the speed alone: from `keep_below` to `keep_below + band` °C under (2–6 °C). Leaving the speed alone there is "no change" (no event), not a hold. | `keep_below`, `band` |
| raise, at-target raise | A step of `up_step` up. The at-target raise is the immediate one when a raw reading reaches its target. | event `up`, `raising fans`; `Controller._raise()` |
| step down | A step of `down_step` down after `down_interval`, when the hottest weighted excess is more than `keep_below + band` under. | event `down`, `lowering fans` |
| probe | A trial step down inside the band after `probe_interval` × back-off with no change. | event `probe`, `trying fans lower` |
| `--probe` | The read-only report of what fanctl sees and would do next. It changes nothing and has nothing to do with the trial step down. | `probe()` |
| back-off | The factor on `probe_interval`: it doubles when a probe is undone by a raise within `probe_interval`, and halves when one isn't. | `Controller.backoff` |
| hand-off | Giving the fans to EOS automatic control (`environment fan-speed auto`). The code calls it a release. | event `release`, `handing fan control to EOS (auto)`; `Controller.release()` |
| hold | The time fanctl leaves the fans to EOS after a hand-off: 10 min after a fault, 15 after a hot reading, doubling on repeats. The `hold` event means EOS has the fans, during a hold or while waiting for the resume conditions. | event `hold`; `Controller.hold_until`, `hot_hold`, `fault_hold` |
| resume | fanctl taking the fans back from EOS. | event `resume`, `taking fan control`; `Controller._resume()` |
| hot | A heat condition that hands the fans to EOS ([the safety layer](#the-safety-layer)). | `Reading.hot` |
| fault | An equipment or data problem that hands the fans to EOS. | `Reading.faults` |
| ceiling dwell | Time at the ceiling with a reading still at or over target; after `ceiling_dwell` fanctl hands the fans to EOS. | `Controller.ceiling_since` |
| paused | Someone else changed the fan setting. fanctl writes nothing until it is restarted, except to hand the fans to EOS if something gets hot or faulty under a low override. | `Controller.paused`; events `paused`, `paused-alarm` |
| heartbeat | The status file, rewritten every poll. | `fanctl.status.json`, `STATE_FILE` |
| memory | The restart memory: holds, back-off and the last speed. | `fanctl.memory.json`, `MEMORY_FILE` |
| guard | The watchdog daemon `fanctl-guard.sh`. | |

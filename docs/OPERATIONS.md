# Operating fanctl

This guide covers installing, verifying, monitoring, tuning, troubleshooting and removing fanctl on an Arista EOS switch.

Prompts are shown as `switch#`. Run `enable` first if your login lands at privilege 1.

- [Before you start](#before-you-start)
- [Installation](#installation)
- [Trying another model](#trying-another-model)
- [Upgrading](#upgrading)
- [Monitoring](#monitoring)
- [Day-to-day operation](#day-to-day-operation)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

## Before you start

- **Read the warning in the [README](../README.md).** Fan-speed override is unsupported by Arista, and fanctl comes with no warranty.
- **Check the airflow.** The intake side must face the cold aisle:
  - On `-R` models (port-side exhaust), that's the fan/PSU side.
  - On `-F` models, it's the port side.

  EOS's `ambientTemperature` is measured at the intake. If it reads well above your cold aisle, fix recirculation first; no script makes up for it.
- **Check your existing config.** Note your current `environment fan-speed` line, if any (`show running-config | include fan-speed`), so you can go back to it.
- **Two EOS CLI quirks to know about:**
  - `?` anywhere on a command line is a help request, so don't type `$?` in `bash …` commands.
  - `bash timeout N …` is parsed as EOS's own option and kills the command after N seconds.

## Installation

### 1. Copy the files

First check the downloaded files against `SHA256SUMS` from the repository or release (`sha256sum -c SHA256SUMS`). `SHA256SUMS` comes from the same place as the files, so it shows that your copy is complete and unchanged, not where it came from: download from a source you trust, not from an unknown fork or mirror. Then copy `fanctl.py` and `fanctl-guard.sh` to `/mnt/flash/`, using either method:

- **With `scp`**, if your login lands at privilege 15:

  ```text
  $ scp fanctl.py fanctl-guard.sh admin@switch:/mnt/flash/
  ```

- **Pulled from the switch.** This works for any login that can run `enable`, which every step below needs anyway:

  ```text
  switch# copy scp://user@yourhost/path/fanctl.py flash:fanctl.py
  switch# copy scp://user@yourhost/path/fanctl-guard.sh flash:fanctl-guard.sh
  ```

  `copy` also accepts `http://`, `https://`, `ftp://` and `tftp://` URLs. If your host is reachable only through a management VRF, set the CLI's VRF first; on recent EOS releases that's `cli vrf <name>`.

On the switch, the hashes must match `SHA256SUMS`. A mismatch usually means CRLF line endings from a copy through Windows.

```text
switch# bash sha256sum /mnt/flash/fanctl.py /mnt/flash/fanctl-guard.sh
```

### 2. Pre-flight checks (read-only)

```text
switch# bash /usr/bin/python3 -c 'import sys; print(sys.version)'
switch# bash FastCli -p 15 -c 'show bogus-command' && echo exit-zero || echo exit-nonzero
switch# bash which flock stat date timeout readlink dirname grep logger
```

What to expect:

- **Python 3.9 or newer.** Use `/usr/bin/python3`. Older versions are untested. On EOS 4.28, `/usr/bin/arista-python` can be Python 2.7, which won't run fanctl.
- **FastCli** prints `% Invalid input` and exits non-zero. fanctl treats either a non-zero exit or a line starting with `% ` as a failed command, so it would still catch the error if your release exits zero here.
- **All eight tools are present.** Without `flock`, the guard only notices a dead fanctl once its heartbeat is 5 min old. Without `timeout`, a wedged FastCli can stall the guard. Without `readlink` or `dirname`, the guard can't check fanctl's runtime directory, so it never trusts fanctl's lock and restores automatic control.

### 3. Probe (read-only)

```text
switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --probe
```

The probe lists every sensor with its temperature, target, distance to target, alert limit and headroom. It also shows the fans, the PSUs and the current setting in running- and startup-config, then its verdict: what a fanctl started now would do and why, including the binding sensor and its fan-authority weight, and the log lines of its next fan changes if the readings stayed as they are, with how far ahead it looked (twice the longest wait, at most 2000 polls: 3600 s with the defaults; a change exactly that far ahead is included). The verdict comes from fanctl's own controller, run poll by poll on the readings the probe took, so it always matches the daemon (apart from holds and back-off a restart would carry over from the memory file).

If the probe reports a **fault** you know about, fanctl will keep handing the fans to EOS. Typical examples are a sensor whose hardware status is permanently failed, a sensor that reports no usable alert limit (none, or one outside 1–150 °C), so fanctl can't tell how close it is to trouble, or a PSU that is deliberately unpowered. Exclude it on the daemon's `exec` line with `--ignore-sensor TempSensorN` or `--ignore-psu N`, using the sensor's *name* (no spaces, no quotes).

An ignored sensor is left out of **every** check, including the over-temperature hand-off. Ignore only a sensor that is genuinely broken, never one that is merely warm.

### 4. Smoke test (optional)

```text
switch# bash sudo /usr/bin/python3 /mnt/flash/fanctl.py --dry-run --no-syslog
```

This runs the real loop against the switch, logs what it *would* do, and changes nothing. Stop it with Ctrl-C after a minute. It's a smoke test, not a prediction: the fans don't change, so the temperatures don't respond.

It needs `sudo` because fanctl's runtime files live in `/run/fanctl`, which only root can write to. `--probe` and `--status` don't need it.

### 5. Start fanctl

```text
switch# configure
switch(config)# daemon fanctl
switch(config-daemon-fanctl)# exec /usr/bin/python3 /mnt/flash/fanctl.py
switch(config-daemon-fanctl)# no shutdown
switch(config-daemon-fanctl)# end
switch# show daemon fanctl
switch# show logging | include fanctl
switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --status
```

What you should see:

- `show daemon fanctl` reports `running with PID …`.
- The log shows `fanctl 1.4.0 starting …` and `current fan setting: …`.
- If an override was already set, fanctl continues from it. After 5 min, it steps down 2 % every 5 min while every sensor stays more than 6 °C (weighted) under its target.
- If the fans were automatic, fanctl takes over at EOS's current speed (`taking fan control -> override N%`) and steps down from there. If EOS runs them above the ceiling (70 %), fanctl waits under EOS control (`fans under EOS control: EOS runs the fans at N%, above fanctl's 70% ceiling`).

If `exec` is rejected, your release may call it `command`; check with `?`. If the daemon keeps restarting, see [Troubleshooting](#troubleshooting).

### 6. Start the guard

```text
switch# configure
switch(config)# daemon fanctl-guard
switch(config-daemon-fanctl-guard)# exec /usr/bin/bash /mnt/flash/fanctl-guard.sh loop
switch(config-daemon-fanctl-guard)# no shutdown
switch(config-daemon-fanctl-guard)# end
switch# show daemon fanctl-guard
```

`show daemon fanctl-guard` should report `running with PID …`, and `show logging | include fanctl-guard` should show `fanctl-guard <version> started: first check in 180 s, then every 60 s`, with the same version as fanctl's `starting` line. The guard waits 180 s, then checks every 60 s. If it finds a fanctl of another version running, it logs a warning once.

### 7. Test the stop path

This also tests the guard.

```text
switch# configure
switch(config)# daemon fanctl
switch(config-daemon-fanctl)# shutdown
switch(config-daemon-fanctl)# end
switch# show logging | include fanctl
```

1. EOS kills the daemon with SIGKILL (`%PROCMGR-6-PROCESS_TERMINATED: 'fanctl' (…, status=9)`), so the override stays for now.
2. Within about a minute (up to 4 if the guard started less than 3 minutes ago), the guard logs `fanctl is not managing the fans … restored automatic fan control`.
3. `show running-config | include fan-speed` then prints nothing, meaning automatic control.

Then start fanctl again with `no shutdown` under `daemon fanctl`. It takes over at EOS's current speed.

### 8. Save

Before saving, confirm all of these:

- `show daemon fanctl` and `show daemon fanctl-guard` both say `running`.
- `fanctl.py --status` is neither `STALE` nor `not running`.
- `show running-config | section daemon` shows both `exec` lines.
- `show running-config | include fan-speed` shows an override between the floor and the ceiling.

```text
switch# write memory
```

This also saves fanctl's current override to startup-config. After a reload the fans run at that speed until fanctl starts, which takes seconds. If fanctl doesn't start, the guard restores automatic control after its grace period.

**To back out instead**, run `no daemon fanctl-guard` and `no daemon fanctl` in configuration mode, then restore the fan-speed line you noted in [Before you start](#before-you-start). Startup-config is unchanged until you save.

### 9. Alerting

Forward syslog to your collector, e.g. `logging host <collector>`, and alert on err-severity messages from `fanctl` and `fanctl-guard`. See the next section.

## Trying another model

fanctl reads every threshold from EOS, but it has only been tested on a DCS-7050TX-64-R. On any other model:

1. **Probe.** In the `--probe` output, check that:
   - every sensor shows a target and an alert limit
   - there is no `WARNING: … report no usable EOS setpoint` line; if there is, those sensors' targets are 0.65 × their alert limit, a ratio measured on the reference switch only, so check them against what EOS aims for on this model (`--target-offset` moves every target). A setpoint EOS does report counts as unusable when it is below 1 °C or at or above the sensor's alert limit, so `show system environment temperature` may still list one
   - there is an inlet reading (`ambientTemperature`)
   - no faults are reported
   - you agree with the verdict
2. **Dry run.** Run `--dry-run --no-syslog` with `sudo`, as in [step 4](#4-smoke-test-optional), for at least 10 minutes and read what it would have done.
3. **Install, then watch it.** Check `--status` regularly during the first day, especially through the warmest part of it.
4. **Report back.** Open an issue with the model, the EOS version and the probe output, with hostnames and serial numbers removed.

## Upgrading

**Upgrading fanctl.** Upgrade `fanctl.py` and `fanctl-guard.sh` together, in this order. From 1.3.0, first read the notes for 1.3.0 to 1.3.1 below: they add checks before step 2.

1. On your host, check the new files against `SHA256SUMS` (`sha256sum -c SHA256SUMS`).
2. Stop the guard first, so that it doesn't act while fanctl is being replaced:

   ```text
   switch# configure
   switch(config)# daemon fanctl-guard
   switch(config-daemon-fanctl-guard)# shutdown
   switch(config-daemon-fanctl-guard)# end
   ```

3. Copy both files to `/mnt/flash/` and check their hashes on the switch, as in [step 1](#1-copy-the-files).
4. Restart fanctl, then start the guard again:

   ```text
   switch# configure
   switch(config)# daemon fanctl
   switch(config-daemon-fanctl)# shutdown
   switch(config-daemon-fanctl)# no shutdown
   switch(config-daemon-fanctl)# exit
   switch(config)# daemon fanctl-guard
   switch(config-daemon-fanctl-guard)# no shutdown
   switch(config-daemon-fanctl-guard)# end
   ```

   The `shutdown` is a SIGKILL, so the fan setting stays as it was, and the new fanctl carries on from it: from the current override, if fanctl was in control. The guard waits 180 s before its first check.

5. Check the result:

   ```text
   switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --status
   switch# show daemon fanctl
   switch# show daemon fanctl-guard
   switch# show logging | include fanctl
   ```

   `--status`, the `fanctl … starting` log line and the `fanctl-guard … started` log line show the new version, and both daemons say `running`.

The daemon configuration ends up as it was, so an upgrade needs no `write memory` unless you also changed an `exec` line.

**From 1.3.2 to 1.4.0:** both files changed, so follow the steps above. Also:

- Before step 2, check the options on fanctl's `exec` line against the new ranges: `--flap-window`, `--reconcile`, `--summary` and `--hold-max` have one now, and a value outside it makes fanctl log `invalid settings, not starting: …` and not run. `--help` of the new `fanctl.py` shows every range.
- On a model other than the reference switch, run the new `--probe` after step 3 and before step 4 (it is read-only; the old fanctl keeps running). A sensor without an alert limit, or PSU-fan data in an odd shape, now hands the fans to EOS, and the probe lists them under `FAULTS`. Leave such a sensor out with `--ignore-sensor` only if you accept that it then gets no checks at all.
- Holds and back-off carry over: 1.4.0 reads 1.3.2's memory file, so you needn't wait for fanctl to be in control.
- The guard now logs `fanctl-guard 1.4.0 started: …` when it starts, and warns if the running fanctl is from another release.

**From 1.3.1 to 1.3.2:** only `fanctl.py` changed; `fanctl-guard.sh` is the same file. The steps above still apply. No other checks are needed.

**From 1.3.0 to 1.3.1:**

- The runtime files moved from `/tmp` to `/run/fanctl`. A 1.3.0 guard can't see a 1.3.1 fanctl, and a 1.3.1 guard can't see a 1.3.0 one. With mixed versions, the guard restores automatic control and fanctl then pauses (`fan setting changed outside fanctl`). Hence both files, and the guard stopped first.
- Before step 2, re-run the `which` line of the [pre-flight checks](#2-pre-flight-checks-read-only). It now also lists `readlink` and `dirname`, which the 1.3.1 guard needs.
- Before step 2, check that fanctl is in control: `--status` shows `fans: override N% in effect; fanctl wants override N%` and no `holding EOS control` line. Hold and back-off memory isn't carried over from `/tmp`, so if fanctl is waiting under EOS control, wait until it has taken over again. Otherwise the new fanctl could take over before the old one's hold has run out.
- The old files in `/tmp` are no longer used, and vanish at the next reload. To remove them after the upgrade: `bash sudo rm -f /tmp/fanctl.lock /tmp/fanctl.status.json /tmp/fanctl.memory.json`.
- The dry run now needs `sudo` ([step 4](#4-smoke-test-optional)).
- If you use `tools/monitor.py`, use the copy from the same release as fanctl on the switch. Each reads its own version's status file.

**After an EOS upgrade,** re-run the [pre-flight checks](#2-pre-flight-checks-read-only) and `--probe`, because the Python version and the tools on the switch can change.

## Monitoring

Messages go to syslog facility **local4**, tagged `fanctl` and `fanctl-guard`. **Alert on every err-severity message from either tag**, matching case-insensitively.

| Severity | Message | Meaning / action |
|---|---|---|
| err | `could NOT restore automatic fan control` | Set `environment fan-speed auto` by hand now. |
| err | `could not set automatic fan control` | Set `environment fan-speed auto` by hand now. |
| err | `restoring automatic fan control FAILED` (guard) | Set `environment fan-speed auto` by hand now. |
| err | `EOS would not return to automatic fan control; fans set to override 100%` | The fans are at full speed. fanctl retries automatic control every 5 min. |
| err | `EOS refused automatic fan control` | The same, while fanctl was stopping, or from the guard (`… fans set to override 100% (the setting was …)`). While fanctl is down the guard tries automatic control again at every check and logs this each time EOS refuses, so it repeats every minute; within a minute of EOS accepting `auto` the guard logs `restored automatic fan control` instead. |
| err | `returning to automatic fan control failed` | Detail logged alongside the two above. |
| err | `could not set override N%: …; handing control to EOS` | An override didn't take; EOS is back in control. |
| err | `fans are … (set outside fanctl) and: …` | Someone set the fans by hand while something is hot or faulty. An `auto` or an override at or above the ceiling fanctl leaves alone (`fanctl is leaving them alone`). A lower override it hands to EOS, and until EOS has the fans the line ends `fanctl is handing them to EOS`. |
| err | `internal error`, `error while judging switch environment` | A bug. Please open an issue with the log. |
| err | `invalid settings, not starting`, `bad command line, not starting` | Fix the daemon's `exec` line. |
| err | `… was replaced and is held by another process; stopping, and leaving the fans, heartbeat and memory to it` | Someone removed `/run/fanctl/fanctl.lock` and another fanctl, or the guard, took the new one. This fanctl exits without touching the fans; ProcMgr restarts it and it waits for the lock. Find out what removed the file, and whether a second fanctl runs by hand. |
| err | `unsafe runtime directory …; not starting` | Another user could tamper with fanctl's runtime files, or fanctl was run by hand without `sudo`. The message gives the reason. `/run/fanctl` should be a real directory, owned by root, mode 0755. |
| err | `cannot create runtime directory …; not starting` | fanctl couldn't create `/run/fanctl`, usually because it was run by hand without `sudo` ([step 4](#4-smoke-test-optional)). |
| warning | `handing fan control to EOS (auto) -> auto: <reason>` | Expected on faults or heat. Look into it if it's frequent. |
| warning | `fanctl-guard: fanctl is not managing the fans … restored automatic fan control` | fanctl was stopped, killed, hung or only a dry run, or its runtime directory is unsafe. |
| warning | `fan setting changed outside fanctl` | fanctl is hands-off until it is restarted. |
| warning | `fanctl X is running with fanctl-guard Y; install both files from the same release` | Mixed versions; they may not see each other. [Upgrade](#upgrading) both files. |
| warning | `… is locked by another fanctl or by fanctl-guard; waiting` | fanctl is starting and waits for the lock. Seconds are normal if the guard was restoring auto. If it goes on, a second fanctl is running. Logged again if the lock file is removed while it waits. |
| warning | `could not read switch environment` | Repeated failures lead to a hand-off. |
| warning | `ignoring unreadable /run/fanctl/fanctl.memory.json: …` | The restart memory was damaged (the message names the field); fanctl starts as if it had none, so a hold from before the restart is lost. |
| info | `status: fans override N%, …` | Hourly summary. |

Also watch for these:

- `show daemon fanctl` and `show daemon fanctl-guard` should both say `running`.
- `%PROCMGR-3-PROCESS_DELAYRESTART` means a daemon is crash-looping.

`tools/monitor.py` alerts on, and `tools/report.py` lists, every warning and err message above, fanctl's `taking fan control` line, ProcMgr terminations and crash loops, and EOS's `ENVMON` messages. The pattern lives in `tools/fanlog.py`, and the tests check it against every message fanctl and the guard can log.

## Day-to-day operation

### Status

```text
switch# bash /usr/bin/python3 /mnt/flash/fanctl.py --status
```

Shown:

- the setting in effect and the setting fanctl wants
- any hold
- the inlet temperature
- the binding sensor, with the hottest sensors and their weights

Until fanctl's first poll ends, it shows `fans: not read yet (fanctl is starting)` in place of the settings. If fanctl is dead or hung, it says `not running` or `STALE` and returns non-zero. If its runtime directory is unsafe, it starts with an `UNTRUSTED` line and returns non-zero.

Every speed change in the log names its reason:

- `binding: <sensor> …` for normal band moves
- `at target: hottest vs target: <sensor> …` for the immediate raise

### Tuning

Add options to the `exec` line, unquoted, then run `shutdown` and `no shutdown` under `daemon fanctl`. fanctl resumes at the same speed. Its start-up log line shows the floor, ceiling, margins and inlet limit in effect. Common choices:

| Goal | Option |
|---|---|
| More margin everywhere | `--target-offset -3` |
| Never slower than 35 % | `--floor 35` |
| Hand back to EOS earlier on a warm day | `--ambient-max 30` (the resume level follows) |
| Treat every sensor alike (unweighted band control) | `--min-authority 1` |

All parameters are described in [DESIGN.md](DESIGN.md#parameters).

### Setting the fans by hand

- **Briefly**, e.g. an emergency `environment fan-speed override 100`: just set it. fanctl notices on its next poll and leaves the fans alone until restarted. A manual override below 70 % is still handed to EOS if something gets hot or fails. To give control back, restart fanctl (`shutdown` / `no shutdown`).
- **For longer:** stop both daemons, then set what you want. Stopping fanctl leaves its last override in place, and with the guard still running, the guard would restore automatic control within a minute.

### Configuration churn

Each speed change is one running-config edit and one `SYS-5-CONFIG_I` log line. Expect none at a steady inlet once settled, and about 15–25 a day with a daily temperature swing. Config-compliance tools will see the `environment fan-speed override N` line change.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Daemon restarts in a loop (`PROCESS_DELAYRESTART`) | Look at `bash ls /var/log/agents/ \| grep fanctl` and the newest file. Common causes: the wrong interpreter (Python 2.7 via `arista-python`), CRLF line endings, a bad option (`bad command line` in `show logging`), or an unsafe runtime directory (`unsafe runtime directory` in `show logging`). |
| fanctl never takes over (`fans under EOS control: …` every 15 min) | The log line gives the blocker: a hold, the inlet above `ambient-resume`, a sensor near its target or alert limit, EOS running the fans above the ceiling, or a fault. Run `--probe` to see which. If EOS's automatic speed on your model is normally above the ceiling, raise `--ceiling`. |
| Constant hand-offs with `… status is …` faults | A sensor, fan or PSU reports a non-ok status. Fix the hardware, or exclude a known-bad sensor or unpowered PSU with `--ignore-sensor` / `--ignore-psu`. |
| `fan setting changed outside fanctl` without anyone touching it | Automation reapplied a fan-speed line. Exclude that line from your config management. |
| A dry run by hand stops at once with `… run fanctl as root …` | `/run/fanctl` is root's. Run it with `bash sudo /usr/bin/python3 …` (step 4). |
| `scp` fails with `only available in privileged mode` | Your login lands at privilege 1. Use `copy` from the switch instead (step 1). |
| `--status` says `STALE` or `not running` | fanctl isn't running or is hung. Check `show daemon fanctl` and the logs. The guard restores automatic control within about a minute of a stop, or about 6 minutes of a hang. |

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

The explicit `environment fan-speed auto` is needed because EOS kills daemons with SIGKILL, so fanctl can't restore automatic control as it stops. Use your previous `environment fan-speed override N` instead if you want that back.

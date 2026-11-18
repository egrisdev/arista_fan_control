# Validation

This document covers how fanctl was tested and what the results were: the step test on the reference switch, the plant model fitted to it, the simulated results, the controller comparison, and the tests on hardware.

The reference switch is a DCS-7050TX-64-R (48 × 10GBASE-T, 4 × QSFP+, port-side exhaust) running EOS 4.28.13.1M. Its four QSFP+ cages were empty. The installation checks, field runs and hand-off test below ran fanctl 1.3.0; the [upgrade to 1.3.1](#upgrade-to-131-2026-10-21) and a [hand-off test on 1.3.1](#hand-off-test-on-131-2026-10-21) follow them, and everything after that on the switch ran 1.3.1 until the [upgrade to 1.4.0](#upgrade-to-140-2026-10-29). 1.4.0 then ran for [24 hours](#run-3-24-hours-on-140-2026-10-29-to-2026-10-30) with nothing to react to, so most of what 1.3.2 and 1.4.0 changed is still covered only by the off-box tests and the benchmark ([Not verified](#not-verified)).

- [Baseline](#baseline)
- [Step test](#step-test)
- [Plant model](#plant-model)
- [Simulated results](#simulated-results)
- [Controller comparison](#controller-comparison)
- [Off-box test suite](#off-box-test-suite)
- [On-switch verification](#on-switch-verification)
- [Field validation](#field-validation)
- [Not verified](#not-verified)

## Baseline

Before fanctl, the reference switch had run a static `environment fan-speed override 40`. The table shows the peak readings since its last boot, months earlier:

| Sensor | Peak at 40 % | EOS target | Alert limit |
|---|---|---|---|
| Front-panel | 31.5 °C | 35.75 °C | 55 °C |
| Board | 40 °C | 45.5 °C | 70 °C |
| PHY 17–24 | 57 °C | 65 °C | 100 °C |

Every sensor stayed at least 4.25 °C under its target, so 40 % had real headroom. At a 25 °C inlet, EOS's automatic control runs these fans at 60 %, as measured during installation ([below](#on-switch-verification)).

## Step test

The fan override was stepped through 40 → 70 → 55 → 45 → 35 → 30 → 40 %. Each step lasted 15–30 min, with a poll every 15 s.

- **Inlet:** 24.5–25.5 °C as 5-minute means; raw readings ranged from 24.0 to 25.9 °C.
- **Script:** [`measurements/fan_step_test.py`](../measurements/fan_step_test.py). It sends only `environment fan-speed override N` with 30 ≤ N ≤ 70, and at the end hands the fans back to EOS.
- **Raw data:** [`measurements/step_test_2026-10-16.jsonl`](../measurements/step_test_2026-10-16.jsonl).

Steady state, the mean of the last 5 minutes of each step, in °C:

| | 70 % | 55 % | 45 % | 40 % | 35 % | 30 % | target / alert |
|---|---|---|---|---|---|---|---|
| hottest PHY (17–20) | 43.3 | 47.1 | 52.0 | 54.9 | 59.0 | 63.1 | 65 / 100 |
| Trident (hottest) | 42.1 | 45.2 | 48.6 | 51.0 | 54.5 | 60.4 | 65 / 100 |
| Board | 33.0 | 34.5 | 36.1 | 37.2 | 39.0 | 40.8 | 45.5 / 70 |
| Front-panel | 26.2 | 27.2 | 28.2 | 29.1 | 30.3 | 31.3 | 35.75 / 55 |
| CPU | 37.8 | 39.2 | 40.8 | 42.3 | 44.8 | 51.8 | 60 / 90 |
| switch power (PSU output) | 166.8 W | 163.7 W | 162.9 W | 163.2 W | 163.8 W | 165.2 W | |

Findings:

- **Fan authority varies widely between sensors.** The PHY and Trident groups' rise over the inlet follows roughly fan^−0.8. The front-panel sensor moved only 5 °C across the whole range: it mostly follows the room. This is what led to fan-authority weighting.
- **Trident and CPU jump more than that fan law predicts between 35 and 30 %.**
- **Time constants:** about 100 s at 70 %, 165 s at 40 % and 250 s at 30 % (≈ 1/fan); the CPU's is about 90 s.
- **Sensor noise is 0.1–0.45 °C.** The PHY sensors read in whole degrees, so fanctl averages 4 polls.
- **The inlet reading rises about 0.65 °C at 30 %**, because some of the switch's own heat reaches it.
- **Power is flat.** Total draw was 163–167 W at every speed: lower fan power is offset by the chips' leakage as they run warmer. The benefit of slower fans is acoustic only.

[`measurements/analyze.py`](../measurements/analyze.py) produces these numbers from the raw data.

## Plant model

[`measurements/build_model.py`](../measurements/build_model.py) fits a model of every sensor from the step test:

- **Steady-state rise over the inlet** at each measured fan speed, interpolated log-log, with a power-law fallback.
- **A first-order time constant**, scaled with 1/fan.
- **Measured noise.**

The result is [`measurements/measured_model.json`](../measurements/measured_model.json). The benchmark and the "measured plant" tests use it.

[`measurements/validate_model.py`](../measurements/validate_model.py) replays the real step test through the model, driven by the recorded inlet and fan speeds ([`bench/results/model_validation.txt`](../bench/results/model_validation.txt)):

- **RMS error per sensor:** 0.2–0.6 °C.
- **Largest error on the 4-reading mean fanctl acts on:** up to about 2 °C, for 10–15 min after a step. The real switch keeps warming with a slower tail than one time constant can follow.
- **Persistent errors in the 30 % step:** about 1–1.3 °C on a few sensors, for the whole step.

## Simulated results

The simulated results below are from [`bench/`](../bench/README.md), run on the measured plant model. They are model results, not measurements.

"v1.2" below is the pre-release 1.2.0: the same band control, without fan-authority weighting. It behaves like 1.3 with `--min-authority 1`.

### Settled speed per inlet

Source: [`bench/steady_inlets.py`](../bench/steady_inlets.py) → [`bench/results/steady_inlets.txt`](../bench/results/steady_inlets.txt).

| Inlet | v1.3 settles at | v1.2 (≈ `--min-authority 1`) | Binding sensor (v1.3) |
|---|---|---|---|
| 20–24 °C | 30 % (floor) | 30 % | PHY |
| 25 °C | 31 % | 31 % | PHY, 2 °C under its 65 °C target |
| 26–27 °C | 32–34 % | 32–34 % | PHY |
| 28 °C | 35 % | 37–38 % | PHY |
| 29 °C | 36 % | 40–44 % | PHY (front panel close behind) |
| 30 °C | 40 % | 51–53 % | front panel |
| 31 °C | 49 % | 56–65 % | front panel |
| 32 °C and up | EOS auto | EOS auto | — |

Each inlet was tested twice: starting at that inlet, and with the room ramped up to it. Where the two runs settled at different speeds, the table gives the range. Once settled at a constant inlet, the fans don't change again.

By the fan laws, sound power scales with roughly 50 × log10 of the speed ratio:

- 31 % against EOS's 60 % is about 14 dB quieter.
- 31 % against a static 40 % is about 6 dB quieter.
- At 30 °C, v1.3's 40 % against v1.2's 51–53 % is about 5–6 dB quieter.

### Scenarios

The scenarios are on the measured plant, with the standard noise seed ([`bench/results/final.txt`](../bench/results/final.txt), [`final.json`](../bench/results/final.json)):

| Scenario | v1.3 mean fan | v1.2 mean fan | v1.3 changes/day | minutes over target |
|---|---|---|---|---|
| steady 25 °C | 31.2 % | 31.3 % | none once settled | 0 |
| steady 28 °C | 35.1 % | 38.0 % | none once settled | 0 |
| daily 20–30 °C cycle | 33.3 % | 35.7 % | 15 | 0 |
| warm spell (26 → 31 → 24 °C) | 36.6 % | 42.3 % | 25 | 0 |
| sudden +35 % PHY heat load | 35.9 % | 36.0 % | 53 | 0 |
| fan-status glitches (5 hand-offs to EOS) | 51.9 % | 51.9 % | 19 | 0 |

The changes-per-day figures for the steady and load-step scenarios are dominated by the initial descent from the starting speed. The scenarios are hours long, so a single descent extrapolates to a high daily rate. In the glitch scenario, the mean includes the time EOS ran the fans at about 60 % after each hand-off.

### Safety sweeps

Each sweep ran 20 noise seeds:

- **Sudden +35 % PHY heat load:** 3 of 20 runs had readings up to 1 °C over target, for at most a minute. v1.2 had 2 of 20.
- **Warm spell:** no run went over target.

### Robustness: the trade-off

The benchmark also runs five deliberately exaggerated plants:

- 3× the sensor noise
- 2× and ½× fan sensitivity
- 3× slower
- 25 % hotter

Across all 36 plant × scenario cases, v1.3 was over target for 3.0 minutes in total, against v1.2's 0.75:

- **2.5 minutes** are single readings at most 1 °C over target, on the 3×-noise plant.
- **0.5 minutes** is a start-up transient that v1.2 has too.

At a steady 28 °C on the noisy and over-sensitive plants, v1.3 makes 30–36 changes a day, where v1.2 makes 2. There, one 2 % step moves the PHY groups nearly across the whole band. The measured switch doesn't behave like that. On a switch that does, `--min-authority 1` restores v1.2's behaviour.

## Controller comparison

Is band control really the right algorithm, or would a textbook controller do better? To find out, several candidates replaced **only** `regulate()`, the in-band decision. So every candidate kept the same safety layer: hand-offs, holds, resume, operator pause, floor, ceiling and ceiling dwell. All of them ran the same 36 cases (6 plants × 6 scenarios) and the 20-seed safety sweeps.

The candidates' code is in [`bench/variants/`](../bench/variants/).

| Controller | steady 25 °C | steady 28 °C | daily cycle | changes/day (daily) | warm spell | 36 cases: min over target | 20 seeds: P(over), load step |
|---|---|---|---|---|---|---|---|
| **fanctl 1.3** (band + authority) | 31.2 % | 35.1 % | 33.3 % | 15 | 36.6 % | 3.0 | 0.15 |
| fanctl 1.2 (band) | 31.3 % | 38.0 % | 35.7 % | 15 | 42.3 % | 0.75 | 0.10 |
| band + extras (`bandplus.py`) | 31.7 % | 36.0 % | 33.5 % | 6 | 35.5 % | 1.25 | 0.15 |
| PI (`pi.py`) | 32.0 % | 34.8 % | 32.8 % | 8.5 | 35.5 % | 1.0 | 0.00 |
| PID (`pid.py`) | 30.0 % | 33.1 % | 32.9 % | 11.5 | 35.2 % | 1.25 | 0.10 |
| PI on linearised fan law (`gspi.py`) | 31.0 % | 36.0 % | 33.3 % | 9 | 35.2 % | 1.0 | 0.00 |
| integral-only, linearised (`lean.py`) | 31.0 % | 35.0 % | 33.7 % | 8.5 | 35.8 % | 6.25 | 0.00 |
| model-based (`model.py`) | 30.0 % | 34.0 % | 32.7 % | 9.5 | 35.1 % | 1.0 | 0.05 |
| SONiC fan algorithm (`CoolingLogicLegacy`), retuned (`sonic.py`) | 31.0 % | 40.0 % | 33.4 % | 19.5 | 36.8 % | 27.0 | — |
| static 40 % | 40 % | 40 % | 40 % | 0 | 40 % | 2275 | — |

The fan columns are mean fan speed on the measured plant.

Sources:

- [`bench/results/canonical.json`](../bench/results/canonical.json), [`canonical_lean.json`](../bench/results/canonical_lean.json) and [`final.json`](../bench/results/final.json)
- [`seeds.txt`](../bench/results/seeds.txt), [`seeds_lean.txt`](../bench/results/seeds_lean.txt) and [`final.txt`](../bench/results/final.txt)

Conclusions:

- **The derivative term adds almost nothing.** Whole-degree sensor readings swamp it. In an ad-hoc run (not saved in `bench/`), turning D off changed 9 of 36 cases slightly, and total time over target went from 1.25 to 1.5 min.
- **A PI controller only works after non-textbook additions.** On a plant where every change is a config commit, it needs all of these to be safe:
  - a commit dead zone
  - protection against chasing sensors the fans can't move
  - noise margins
  - a fast raise

  With those, the working PI variants were 1.2–2× the code of band control. The integral-only variant is about the same size as band control, matches it on the measured plant, and does worse on the noisy plants.
- **At a 25 °C inlet, the best alternative was about 1 dB quieter than band control** (30.0 % against 31.2–31.3 %).
- **In warm rooms, v1.2 band control trailed the best alternative by about 4 dB.** Authority weighting (v1.3) narrows that to about 1 dB. On the measured plant, v1.3 is never more than 0.5 percentage points faster than the PI controllers (`pi.py`, `gspi.py`) at a steady inlet or over the daily cycle. It is 1.1–1.4 points faster in the warm spell and 0.7–1.5 points faster in the load step. The PID ran 1–2 points slower than v1.3 at a steady inlet.
- **The model-based controller did worst on loads it wasn't tuned for.** In ad-hoc runs heating only two PHY groups by 50 %, it went 3–4 °C over target, where band control went 1 °C over. Those runs are not saved in `bench/`.
- **The SONiC fan algorithm**, even retuned for config-commit cost, made more changes and overshot load steps. It was over target for 27 minutes across the 36 cases, 17 of them on the noisy plant.
- **Static 40 %** can't react. It was over target for hours on the 25 %-hotter plant and during every load step.

Band control with authority weighting was kept because it is nearly as quiet as the best alternative, simpler to reason about, and every decision fits in one log line.

### Rejected refinements

| Change | Result | Files |
|---|---|---|
| 2-reading debounce on the immediate raise | load-step overs rose from 3 to 7 of 20 seeds, max excess +2 °C; hunting not reduced | [`rejected_debounce.*`](../bench/results/rejected_debounce.txt) |
| probe back-off applied to every decrease | more hunting on the exaggerated plants: 28 against 24 changes/day averaged over the 36 cases, and 82/day at a steady 28 °C on the 2×-sensitive plant | [`rejected_backoff_all_decreases.*`](../bench/results/rejected_backoff_all_decreases.txt) |
| minimum authority 0.02–0.35 instead of 0.5 | 0.2–0.3 percentage points slower fans on average (up to 1.4 in the warm spell), but 4.0–4.5 against 3.0 min over target across the 36 cases, all of it on the noisy plant | [`sweep_wmin.*`](../bench/results/sweep_wmin.txt) |

## Off-box test suite

```text
python3 -m unittest discover -s tests -v
```

The 298 tests run against [`tests/sim.py`](../tests/sim.py), a simulated switch built from the reference switch's real JSON ([`tests/fixtures/`](../tests/fixtures/)) plus a thermal model, with optional sensor noise. [`tests/fake_fastcli.py`](../tests/fake_fastcli.py) stands in for FastCli in end-to-end runs of both scripts. The off-box tools `tools/monitor.py` and the step test's switch session are tested against [`tests/fake_paramiko.py`](../tests/fake_paramiko.py), which plays the same switch's CLI over SSH; `tools/report.py` is tested on monitor files and saved temperature JSON, without paramiko.

They cover:

- **Steady state and weighting.**
  - settling without hunting at an 18–30 °C inlet, with noise and over a daily cycle
  - the authority weights, PSU sensors excluded, and not chasing the front panel in a warm room
  - the minimum-authority floor
  - a weak sensor at target still raising at once, and named in the log
  - a raise after a raise only once the fans have had `up_interval`, unless it is hotter or a reading at target climbed more than 1 °C within the smoothing window, late in it too
  - a step down only once the latest reading and every poll of the last `down_interval`, the one exactly that long ago included, are more than `keep_below + band` under
- **Probes.** Back-off doubling while probes are undone and halving while they aren't, its cap, a step down not counting as a probe, and back-off kept across a restart but never below the normal wait.
- **Room temperature.**
  - the warm spell on the measured plant model
  - coming back down after a warm spell
  - an inlet rise leading to a hand-off and then resume
- **Too hot.**
  - a sensor within its alert margin, or too far over its target, each on its own, but not one exactly `alert_margin` under its limit or `release_excess` over its target
  - an inlet at `ambient_max`
  - a sensor in alert, or its alert count rising
  - heat during a fault hold, after which fanctl resumes at EOS's speed rather than its old one
- **Faults.**
  - fan, PSU and sensor faults
  - a fan tray or PSU fan disappearing
  - a sensor losing its limit
  - a missing inlet reading, and readings outside 1–150 °C
  - alert counts that stop coming or aren't integers, which are no fault
  - unreadable or garbled output, and a bug while judging it; one failed read is tolerated, even at the first poll, and two are a fault, not heat
  - an internal error, which hands the fans to EOS as fanctl exits
  - a log fanctl can't write to, such as a full `/var/log`, which doesn't stop it
- **Writing the setting.**
  - rejected overrides, handed to EOS in the same poll with the fault hold
  - auto refused, leading to 100 % and asked again every 5 min
  - slow or unconfirmable read-back, a status 2 s late while running-config can't be read, a change that prints an error but takes effect, and one that prints an error and can't be read back, which counts as failed
- **Holds.**
  - churn from status glitches
  - hold escalation, which stops at exactly `flap_window` after taking over, and a hot or fault hold of 0 s, which holds for no time
  - the ceiling dwell, to the poll, with a reading exactly at its target, and no raise at the ceiling
  - no resume while EOS runs above 70 %
  - resuming with each resume condition exactly at its limit, and at the old speed after a fault only if the hottest is at most 0.5 °C and the inlet at most 1 °C warmer
- **Operator changes**, including an emergency 100 % during holds, an override at the ceiling or above through heat and faults, and on the exact poll fanctl would have changed speed. A change is told from a status that lags or can't be read, at the first poll as at any other; running-config is read every `reconcile` s and at once when the status disagrees, but not on a poll that read nothing.
- **Restarts** after a clean (SIGTERM) stop that keep their hold, with its reasons, and speed, a SIGKILL restart (what EOS's daemon `shutdown` does) that keeps a hold extended since the hand-off, also one made while paused, malformed memory, a future-dated memory file, and one older than `hold_max`, which is ignored (one exactly that old is used); a hold that ends as fanctl restarts isn't kept. A hand-off soon after taking over doubles its hold across a SIGKILL restart too, with the time fanctl was down counted.
- **Modes and signals.**
  - the settings and startup-config fanctl logs as it starts, and polls that start every `poll` s however long one takes
  - dry run with its reasons, `--status`/STALE (a heartbeat exactly 300 s old is still fresh, as for the guard), with a hold shown only while it runs and what it shows while fanctl starts, the summary counting each change once, and `--probe`, which finds FastCli or else Cli, lists the sensors hottest against their targets first and looks exactly as far ahead as it says
  - syslog for the daemon only: `--probe`, `--status` and `--no-syslog` log to the terminal alone
  - SIGTERM, including while FastCli is wedged, while it is being started or during its call, or while waiting for the lock
  - a stop when EOS refuses auto (override 100) or the CLI fails (told to restore auto by hand)
  - a FastCli call that times out: its process group killed, and counted as a failed read; a helper that left the group is not killed and delays fanctl at most 5 s more
  - SIGKILL with the guard
  - a hung process, a process still starting, and a second instance
- **Runtime directory.**
  - group- or world-writable and symlinked directories refused, and a private one inside a sticky one such as `/tmp` accepted
  - a missing directory created with mode 0755 and its files 0644 whatever the umask, but never its parents
  - fanctl refusing to start in an unsafe directory, before it runs FastCli or creates a file, or in one this user can't create or write to
  - a memory or status file fanctl can't read or write, reported once an hour while the fans run on
  - runtime paths with `..` refused
  - `--status` trusting a safe directory it can't write to and flagging an unsafe one, without hanging on a FIFO planted there
- **The guard** ignoring dry runs, a wedged FastCli (one that ignores SIGTERM), an unreadable setting, a future-dated heartbeat, and a lock or heartbeat in a directory other users can write to; holding fanctl's lock while it restores, so that a fanctl starting meanwhile waits and doesn't pause; falling back to `override 100` with the same steps as fanctl when EOS refuses auto, and trying auto again at the next check; reading the fan setting as fanctl does; logging its version and warning about a fanctl from another release; its directory rules giving the same verdicts as fanctl's on a table of directories, and its default paths, heartbeat limit and version matching fanctl's.
- **The research scripts**, each run as its README runs it. The step-test analysis printing the numbers above and rebuilding `measured_model.json` to within float rounding, and giving the known answer for a made-up step test (step lengths, the steady-state table's order, the power-law fit, time constants for the step changes only); the replay reproducing `model_validation.txt`; the measured plant settling at the measured rise at every fan speed, each robustness plant scaling it as named, the load step heating only the PHYs while it lasts, and the scenarios' inlet curves; and the benchmark scripts reproducing recorded rows of `canonical.json` (including minutes over target and the standard seed), `steady_inlets.txt` and `sweep_wmin.json`, the summary lines of every recorded sweep, and `sweep.py`'s report and `--json` for a cut-down sweep. Without NumPy the analysis tests are skipped. The candidates in `bench/variants/` have no tests of their own ([bench/README.md](../bench/README.md#scope)).
- **Packaging.** `SHA256SUMS` matching `fanctl.py` and `fanctl-guard.sh`, and the test count stated here matching the suite.

## On-switch verification

These were verified on the reference switch.

**During the step test:**

- `environment fan-speed override N` for N from 30 to 70 takes effect within seconds.
- It shows in the cooling JSON as `coolingMode: manual`, `overrideFanSpeed: N`.
- It produces no output line starting with `% `.

**During installation on the reference switch:**

- **Interpreter.** `/usr/bin/python3` is 3.9.17, and `/usr/bin/arista-python` is Python 2.7.18. The daemon must use `python3`.
- **FastCli.** An invalid command prints `% Invalid input` on stdout and exits non-zero. An `| include` with no match exits 0 with empty output.
- **Guard tools.** `flock`, `stat`, `date`, `timeout`, `grep` and `logger` are all present.
- **Daemon config.** `daemon … exec` works on 4.28.
- **Logging.** Syslog lines from fanctl and the guard appear in `show logging`.
- **Daemon `shutdown` is a SIGKILL** (`status=9`), so fanctl gets no chance to restore auto. The guard saw the free lock and restored automatic control 2 s after its boot grace period ended.
- **EOS automatic mode runs the fans at 60 %** at a 25 °C inlet.
- **Taking over.** fanctl took over from EOS at 60 %, wrote the override through FastCli, and confirmed it in the cooling JSON (`manual` / N).

## Field validation

[`tools/monitor.py`](../tools/monitor.py) collects data from the reference switch read-only over SSH:

- every 30 s: the temperature, cooling and power JSON
- every 5 min: fanctl's status, the daemon states, and the relevant log lines

[`tools/report.py`](../tools/report.py) summarises the data.

### Run 1: 10.6 hours, 2026-10-18

This run ended early, when the off-box monitor had to stop. fanctl kept running on the switch.

- **Coverage:** 1,272 polls over 10.6 h, with no read errors and no gaps.
- **Inlet:** 24.0–25.9 °C, mean 25.3 °C.
- **Take-over:** fanctl took over from EOS's 60 % at 11:10. It stepped down 2 % every 5 minutes to 34 % by 12:16, then made two trial steps, to 32 % and 30 %.
- **The trial step to 30 % was undone.** At 30 %, the hottest PHY group came within 2 °C of its target, so fanctl raised to 35 % and doubled the wait before the next trial step. It then came back down to **31 %**, and it was still there 6 h 50 min later, with no further changes.
- **The model predicted 31 % at a 25 °C inlet** ([settled speed per inlet](#settled-speed-per-inlet)), and its prediction that 30 % sits right at the edge also held.
- **Closest approach to a target:** the PHY 17–20 group peaked at 64 °C, 1 °C under its 65 °C target, at the 30 % trial step. No sensor reached its target, and there were no hand-offs to EOS.
- **Setting changes:** 19 in total, counting the take-over. Thirteen were the initial descent from 60 % to 34 %; there were none in the last 6 h 50 min.

### Run 2: 24.2 hours, 2026-10-19 to 2026-10-20

This run used the released `tools/monitor.py`. It was planned for 24–48 h and was stopped after 24.2 h, once the 24-hour minimum was met. fanctl kept running on the switch.

- **Coverage:** 2,903 polls from 06:02 on 2026-10-19 to 06:15 on 2026-10-20. There were no read errors, and no gap between polls was longer than 30.1 s.
- **Inlet:** 24.8–25.9 °C, mean 25.4 °C.
- **Fans:** override 31 % for the whole run, with the trays turning at 30–31 %. There were no setting changes and no hand-offs, and fanctl never paused for an outside change. In all 291 of fanctl's status files the monitor read, there were no faults and no over-heat conditions.
- **Closest approach to a target:** the PHY 17–20 and PHY 21–24 groups peaked at 63 °C, 2 °C under their 65 °C target. They read 62 °C for most of the run (84 % and 76 % of polls). Every other sensor stayed at least 3.5 °C under its target; the closest was the board sensor, at 42.0 °C against 45.5 °C.
- **Daemons:** fanctl and fanctl-guard ran as the same processes throughout, the ones started during installation on 2026-10-18. Neither restarted, and the guard never acted.
- **Power:** 164.8 W on average at the PSUs (5-minute means 164.1–165.4 W; single readings 162–168 W), in line with the step test.
- **Hardware status:** every poll showed `temperatureOk` and `coolingOk`, with all fans and PSUs `ok`.

Between the two runs, fanctl's hourly syslog summaries covered the roughly 8¼ hours the monitor was off (21:45 to 06:02), and each one reported no changes. So, from 14:55 on 2026-10-18 to the end of this run, fanctl held 31 % for **39 h 20 min without a single change**.

Neither run had a fault or a heat condition, so fanctl's own hand-off to EOS was tested separately afterwards ([below](#hand-off-test-2026-10-20)).

### Hand-off test, 2026-10-20

This test triggered fanctl's own hand-off to EOS on purpose, with the monitor running throughout.

1. **Trigger.** fanctl was at override 31 %. At 14:00:24, `--ambient-max 20` was added to the `exec` line, below the room's 25 °C, and the daemon restarted (`shutdown` / `no shutdown`).
2. **Hand-off.** At its first poll, 2 s after starting, fanctl ran `environment fan-speed auto`, confirmed it, and logged `handing fan control to EOS (auto) -> auto: inlet (ambient) 25.1 C is at or above 20.0 C`. The override line left running-config, and EOS ran the fans at 60 %. fanctl's status file showed `applied: auto` with a 900 s hold, extended back to 900 s at every poll while the inlet stayed over the limit.
3. **The guard stayed out of it.** fanctl was alive and the fans were under automatic control, so the guard had nothing to do, and it logged nothing.
4. **The hold carried over a restart.** At 14:01:36 the normal `exec` line was restored. The new fanctl found the fans under automatic control, read its memory file, and logged `keeping the hand-off hold from before the restart: 825 s more`.
5. **Taking over again.** At 14:15:26, 900 s after the hand-off, it took control at EOS's 60 % (`taking fan control -> override 60%`).
6. **Back to 31 %.** It stepped down 2 % every 5 minutes to 34 % by 15:20, then made trial steps every 30 minutes. The trial step to 30 % at 16:20 was undone at 16:31, when PHY 17–20 read 64 °C against its 65 °C target. fanctl raised to 35 %, doubled the wait, then tried 33 % at 17:31 and 31 % at 18:01. The 31 % step held: there were no further changes over the next 30 minutes, and PHY 17–20 and 21–24 peaked at 63 °C. This repeated Run 1 step for step. From taking over to settling at 31 % took 3 h 46 min, against 3 h 44 min in Run 1.

**Finding: a restart loses hold extensions.** The hold that carried over counted from the hand-off at 14:00:26. In the old process it had been extended at every poll, to about 14:16:25. fanctl 1.3.0 writes its memory file when it changes the fans and whenever it exits on its own (a stop signal or an internal error), but not when a hold is extended, and EOS's daemon `shutdown` is a SIGKILL. So a restart during a hold resumes the hold as it was set at the hand-off, without its later extensions. Here that cost about a minute. If the problem had lasted longer than the hold itself (15 min after a heat hand-off, 10 min after a fault), a restart could skip the rest of the hold, though the [resume conditions](DESIGN.md#holds-and-resume) still apply. 1.3.0's off-box tests simulated only a clean (SIGTERM) stop, which saves the extended hold, so they couldn't catch this.

**Fixed in 1.3.1.** fanctl now also writes its memory file at every poll while EOS has control (during a hold and while waiting for the resume conditions), so a SIGKILL restart loses at most one poll of an extended hold. A new off-box test simulates a SIGKILL restart during an extended hold: the inlet goes over its limit for 5 minutes, so fanctl hands off and extends the hold at every poll. fanctl is then killed, and restarts a minute later with the inlet back to normal. The restarted fanctl keeps the extended hold and takes the fans back within a poll of its end. With 1.3.0's behaviour the same test fails: the hold comes out 270 s short, the length of its extensions. The [hand-off test on 1.3.1](#hand-off-test-on-131-2026-10-21) confirmed the fix on the reference switch.

### Upgrade to 1.3.1, 2026-10-21

The reference switch was upgraded from 1.3.0 to 1.3.1 with the [upgrade procedure](OPERATIONS.md#upgrading), while 1.3.0 held 31 % with no hold running.

- **Tools.** The switch has GNU bash 4.2.46 and coreutils 8.30, and `readlink` and `dirname` are present. Off-box, the full suite also passes under Python 3.9 and bash 4.2, as root and as an ordinary user.
- **Files.** The SHA-256 of both files on the switch matched `SHA256SUMS`.
- **Start-up.** After `shutdown` / `no shutdown` at 08:32:16, fanctl logged `fanctl 1.3.1 starting …` and `current fan setting: override 31%`, and carried on at 31 % without a change.
- **Runtime directory.** fanctl created `/run/fanctl` owned by root with mode 0755, and its lock and status files with mode 0644. `--status` works for the non-root login user. Run by hand, fanctl's `check_dir` and the guard's `dir_ok` both judged the directory safe, the guard's as root and as the login user.
- **Guard.** The 1.3.1 guard started at 08:32:40. After its 180 s grace period it found fanctl alive at every check and logged nothing, and the fans stayed at 31 %.
- **Clean-up.** The old files in `/tmp` were removed. The daemon configuration didn't change, so the upgrade needed no `write memory`.

### Hand-off test on 1.3.1, 2026-10-21

This repeated the [2026-10-20 hand-off test](#hand-off-test-2026-10-20) on 1.3.1, with the monitor running throughout. This time the problem lasted 5 minutes before the restart, so that the hold's extensions would show.

1. **Trigger.** fanctl was at override 31 %. At 08:47:00, `--ambient-max 20` was added to the `exec` line, below the room's 25 °C, and the daemon restarted (`shutdown` / `no shutdown`).
2. **Hand-off.** At 08:47:03 fanctl logged `handing fan control to EOS (auto) -> auto: inlet (ambient) 25.6 C is at or above 20.0 C`, and EOS took over the fans. The inlet stayed over the limit, so fanctl extended the 900 s hold at every poll, and wrote its memory file each time: the file read at 08:52:05 had been written at 08:52:04, with 898.6 s of hold left.
3. **SIGKILL during the hold.** At 08:52:07 the normal `exec` line was restored. ProcMgr killed fanctl (`status=9`) and started a new one.
4. **The extended hold carried over.** The new fanctl logged `keeping the hand-off hold from before the restart: 894 s more`. 1.3.0 would have kept about 595 s, counted from the hand-off five minutes earlier.
5. **Taking over again.** At 09:07:10, 15 minutes after the last poll before the kill, fanctl took control at EOS's 60 % (`taking fan control -> override 60%`). 1.3.0 would have done so at about 09:02.
6. **Back to 31 %.** It stepped down 2 % every 5 minutes to 34 % by 10:12, then made trial steps every 30 minutes. The trial step to 30 % at 11:12 was undone at 11:23, when PHY 17–20 read 64 °C against its 65 °C target. fanctl raised to 35 %, doubled the wait, then tried 33 % at 12:23 and 31 % at 12:53. The 31 % step held: there were no further changes over the next 30 minutes, and PHY 17–20 and 21–24 peaked at 63 °C. From the take-over on, that is the same 19 setting changes as in Run 1 and the 2026-10-20 test. From taking over to settling at 31 % took 3 h 46 min, as on 2026-10-20 (Run 1: 3 h 44 min).

The guard logged nothing during the test, so it never acted. fanctl was down for only about a second at each restart. The monitor polled every 30 s from 08:46 to the end, with no read errors and no gap longer than 30.1 s, and every poll showed `temperatureOk` and `coolingOk`.

### Target-offset trial, 2026-10-21

`--target-offset -3` was tried for 2 h 14 min (13:56–16:10) to see what 3 °C of extra margin costs. At start-up the PHY sensors read exactly their new 62 °C target, so the at-target raise fired at every 15 s poll: 31 % became 56 % within a minute, before the temperatures had time to respond. fanctl then stepped down 2 % every 5 minutes and settled at 36 % from 15:14. It was held there by the front-panel sensor, not the PHYs: with the offset, the front panel's target is 32.75 °C, it reads 30–31 °C, and with its 0.5 authority weight that is too close to lower further. The offset was removed afterwards. It is a coarse way to buy margin on this switch. Raising `--floor` (below) does it without the burst. Since 1.3.2, a reading that sits at its target without getting hotter gets one immediate raise and then waits `up_interval` (60 s) for the fans to work, so this burst would have been one or two raises, not five.

### Shutting unused ports, 2026-10-21 to 2026-10-23

On a 10GBASE-T switch, each PHY chip serves four ports and heats its group's sensor whether or not the ports are linked. The reference switch had 7 linked ports, on 5 of its 12 PHY chips, and 41 idle ports that had never linked since boot. All QSFP+ cages were empty.

1. **One chip, by hand (fans 34–36 %).** One idle port was shut (`shutdown` under the interface) for 15 minutes. Its PHY group's sensor went from 0.7 °C above to 0.7 °C below a neighbouring group's, so about 1.5 °C cooler. With all four ports on that chip shut for 16 minutes, the gap was −4.8 °C: the chip ran about 5.5 °C cooler, and power read about 2 W lower (noisy). The ports were then brought back up.
2. **Every chip with no linked port (2026-10-21 20:30).** 26 idle ports were shut: all of six chips with no linked port, and two ports of a seventh (its other two were kept for step 3), plus the 16 QSFP+ lanes. 32 minutes later the fully shut PHY groups read 6–9 °C cooler, and the linked groups hadn't changed. Over the next two days fanctl moved from 31 % to its 30 % floor and stayed there, and total draw read 143–145 W, against 162–168 W in Run 2.
3. **Moving three links (2026-10-23 13:41).** Three idle links were moved off the two chips they had to themselves, to the two ports kept free in step 2 and a free port on another linked chip. The new ports were first given the old ports' configuration. Each link was down for a few seconds during the move. Then the two vacated chips' eight ports were shut. After 30 minutes the vacated groups read 17–20 °C cooler, the receiving groups 9–12 °C warmer, the hottest PHY went from 60–61 °C to 59 °C, and power went from 144.9 to 138.8 W.
4. **The remaining idle ports (2026-10-23 14:28).** The 7 idle ports next to linked ones were shut in two stages, with the live links checked in between: none flapped, and the log showed no link or spanning-tree events. So shutting a port doesn't disturb a linked port on the same chip. After 30 minutes their groups read 4–7 °C cooler, and power read 133.0 W.

| At 30 % fan, 25 °C inlet | Before | After all four steps |
|---|---|---|
| Ports up | 48 copper + 16 QSFP+ lanes (7 linked) | the 7 linked ports |
| Total draw | 162–168 W (Run 2, 31 % fan) | 133.0 W |
| Hottest PHY group | 61–63 °C | 56 °C |
| Hottest sensor | a PHY group | the switch chip, 58 °C |

The saving is about 30 W, or about 0.8 W per shut 10GBASE-T port. The before and after figures are a few single readings, not averages. Shutting a port is undone with `no shutdown`, and changes only the running-config until `write memory`.

### Raising the floor to 35 %, 2026-10-23

With every idle port shut, fanctl sat at its 30 % floor with 7–9 °C of margin. The floor was raised to 35 % for more margin at a small cost in noise: `--floor 35` on the `exec` line, then `shutdown` / `no shutdown`. fanctl restarted at 15:13:29, logged `fans 35..70%`, and raised the fans to 35 % two seconds later. 30 minutes later, against the 30 % readings an hour earlier:

| Sensor | 30 % fan, 25 °C inlet | 35 % fan, 24 °C inlet |
|---|---|---|
| Switch chip | 54.7–58.0 °C | 50.4–53.1 °C |
| CPU | 52.2 °C | 45.0 °C |
| Hottest PHY group | 56 °C | 52 °C |
| Total draw | 133.0 W | 132.3 W |

About 1 °C of that is the cooler room. The switch then held 35 % for 65 hours with no changes, hand-offs or errors. At the end, with a 25 °C inlet, the hottest PHY groups and the switch chip read 52 °C, 13 °C under their 65 °C target.

### Upgrade to 1.4.0, 2026-10-29

The reference switch went from 1.3.1 straight to 1.4.0 with the [upgrade procedure](OPERATIONS.md#upgrading), while 1.3.1 held its 35 % floor with no hold running. The `exec` line's one option, `--floor 35`, is within 1.4.0's range.

- **Files.** The SHA-256 of both files on the switch matched `SHA256SUMS`.
- **Probe.** Run by hand after the copy, with 1.3.1 still in control, the new `--probe` listed no faults and no sensor too hot, and its verdict was that fanctl would carry on at 35 %.
- **Start-up.** After `shutdown` / `no shutdown` at 06:42:42, fanctl logged `fanctl 1.4.0 starting …` with `fans 35..70%`, then that startup-config holds `environment fan-speed override 35`, so after a reload the fans run at 35 % until fanctl starts, then `current fan setting: override 35%`. It carried on at 35 % without a change.
- **Guard.** The 1.4.0 guard started in the same second and logged `fanctl-guard 1.4.0 started: first check in 180 s, then every 60 s`. It logged nothing else over the next 24 hours, so it never acted.
- **Configuration.** The daemon configuration didn't change, so the upgrade needed no `write memory`.

An hour later `fanctl.py` on the switch was replaced with the final 1.4.0 file, which differs only in a docstring and in what `--status` says about a status file fanctl didn't write. The daemon wasn't restarted, so the run below used the file installed at 06:42. After the run, on 2026-10-30 at 10:39, fanctl alone was restarted (`shutdown` / `no shutdown`) to run the final file, with the guard left running. The new fanctl logged `current fan setting: override 35%` and carried on at 35 % without a change, and the guard logged nothing.

### Run 3: 24 hours on 1.4.0, 2026-10-29 to 2026-10-30

The monitor started a minute after the upgrade and ran its planned 24 hours, with every idle port shut and the floor at 35 % ([above](#raising-the-floor-to-35--2026-10-23)). fanctl kept running on the switch afterwards.

- **Coverage:** 2,878 polls from 06:43 on 2026-10-29 to 06:43 on 2026-10-30. There were no read errors, and no gap between polls was longer than 30.1 s.
- **Inlet:** 24.1–25.9 °C, mean 25.1 °C.
- **Fans:** override 35 % for the whole run, with the trays turning at 34–35 %. There were no setting changes and no hand-offs, and fanctl never paused for an outside change. In all 262 of fanctl's status files the monitor read, there were no faults, no over-heat conditions, no holds and no read failures.
- **The floor set the speed.** Every sensor stayed at least 4.5 °C under its target. The closest were the front-panel sensor, at 31.2 °C against 35.8 °C, and the board sensor, at 41.0 °C against 45.5 °C. With its 0.5 authority weight the front panel counts as 9 °C under, past the 6 °C at which fanctl lowers the fans, so without the floor fanctl would have gone lower.
- **Hottest sensors:** the switch chip peaked at 54.2 °C and the hottest PHY group (PHY 41–44) at 53 °C, against their 65 °C target. In Run 2, at 31 % with every port up, the PHY groups peaked at 63 °C.
- **Daemons:** fanctl and fanctl-guard ran as the same processes throughout, the ones started during the upgrade.
- **Power:** 130.4 W on average at the PSUs (5-minute means 129.9–131.2 W; single readings 127–134 W), against 164.8 W in Run 2.
- **Hardware status:** every poll showed `temperatureOk` and `coolingOk`, with all fans and PSUs `ok`.

## Not verified

- **What 1.3.2 and 1.4.0 changed, on hardware.** 1.4.0 has run on the reference switch only in a steady state: it [replaced 1.3.1](#upgrade-to-140-2026-10-29) and held its floor for [24 hours](#run-3-24-hours-on-140-2026-10-29-to-2026-10-30) with nothing to react to. 1.3.2's wait after an at-target raise, and the 1.4.0 changes that act only when something goes wrong, are covered only by the off-box tests and, for the raise, the controller benchmark. Among 1.4.0's changes that is the guard holding fanctl's lock while it restores and its fallback to override 100, the hand-off for a sensor without an alert limit, the stricter PSU-fan checks, the memory file written at every poll, and waiting under EOS control while EOS runs the fans above the ceiling. The reference switch reports an alert limit and a setpoint for every sensor, and EOS runs it at about 60 %, so several of these would never trigger there.
- **Other hand-off triggers and the fallbacks on hardware.** fanctl's own hand-off has run on hardware twice, both times triggered by its inlet limit (set below the room temperature; see the hand-off tests [on 1.3.0](#hand-off-test-2026-10-20) and [on 1.3.1](#hand-off-test-on-131-2026-10-21)). The other triggers (a sensor over or near its limits, the ceiling dwell, a fault, unreadable data or a failed write) use the same write path but are covered only by the off-box tests. So are the fallbacks for when EOS refuses automatic control (`no environment fan-speed`, then override 100).
- **Away from a 25 °C inlet.** Every field run had a steady 24–26 °C inlet, so warm rooms and daily swings are covered only by the model. The warm-room numbers assume each sensor's rise over the inlet doesn't depend on the inlet. If the front panel heats more in a warm room, the weighting matters more, not less.
- **Alert limits under an override.** It's undocumented whether EOS still forces full fan speed at an alert limit while an override is set. fanctl assumes it doesn't, and hands control back 10 °C before any alert limit.
- **Other platforms.** Only the DCS-7050TX-64-R has been tested. Run `--probe` on anything else first.
- **Optics.** Transceiver temperatures aren't read. The reference switch had no optics installed.
- **1.3.1's other fixes on hardware.** The [upgrade to 1.3.1](#upgrade-to-131-2026-10-21) and the [hand-off test on 1.3.1](#hand-off-test-on-131-2026-10-21), including the fix for holds across a SIGKILL restart, ran on the reference switch. fanctl's and the guard's refusals of an unsafe runtime directory, and the fix for holds while paused, are covered only by the off-box tests.

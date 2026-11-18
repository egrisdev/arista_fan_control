# bench: controller benchmark

This directory compares fanctl's in-band controller against alternatives on a plant model measured on a real switch. Everything runs off-box, with no switch needed.

The plant model, [`../measurements/measured_model.json`](../measurements/measured_model.json), is built from the 2026-10-16 step test by [`../measurements/build_model.py`](../measurements/build_model.py). [`../measurements/validate_model.py`](../measurements/validate_model.py) checks it against the real data; the output is in [`results/model_validation.txt`](results/model_validation.txt).

The results and conclusions are summarised in [docs/VALIDATION.md](../docs/VALIDATION.md#controller-comparison).

## Scope

This directory is an archive of the research behind fanctl's controller: the scripts, the candidate controllers and their results as recorded. fanctl does not use any of it.

The candidates in `variants/` are the only code in the repository that [CONTRIBUTING.md](../CONTRIBUTING.md#making-a-change)'s rules for changes don't cover. They keep the control logic they were benchmarked with, so their recorded results stay reproducible, and for the same reason they have no tests of their own and are not refactored: they are research code that no switch runs, and splitting them or pinning them with tests would change or freeze what was measured without making fanctl any safer. They are checked instead by reproducing the recorded results (see [Results](#results-results)).

The scripts that produce those results are different: every number in `results/` and in docs/VALIDATION.md passes through them, so a bug in one would change results without anyone noticing. `bench.py`, `sweep.py`, `steady_inlets.py` and the step-test analysis in `../measurements/` follow those rules like the rest of the code, and [`../tests/test_research.py`](../tests/test_research.py) runs them on the tracked step-test data and plant model and checks that they reproduce what is recorded, such as `measured_model.json`, `model_validation.txt`, rows of `canonical.json`, `steady_inlets.txt` and `sweep_wmin.json`, and the summary lines of the recorded sweeps. A full benchmark or sweep takes minutes on every CPU, so the suite runs single cases and the smallest set; rerun the commands below to check whole results. The tests of `../measurements/analyze.py` and `build_model.py` need NumPy and are skipped without it.

The benchmark works only with a model of the reference switch's sensors. The simulated switch's sensors, with their descriptions, targets and alert limits, come from that switch's saved JSON in [`../tests/fixtures/`](../tests/fixtures/) (DCS-7050TX-64-R), and `install_model()` looks each one up in the model by name, so a model built from another platform's step test stops with a `KeyError` at the first sensor name that differs. The same holds for `../measurements/validate_model.py`, and `../measurements/analyze.py` and `build_model.py` take sensor descriptions, targets and alert limits from the same fixtures. Using another platform's data needs that platform's saved JSON as the simulated switch.

## Requirements

Python 3.9 or newer. `bench.py` uses only the standard library. The model-building scripts in `../measurements/` need NumPy.

## How a variant works

Every variant subclasses `fanctl.Controller` and overrides only `regulate()`, the in-band speed decision. So every candidate runs behind fanctl's real safety layer: hand-offs to EOS, holds, resume, operator pause, floor, ceiling and ceiling dwell. Each runs on six plants:

- `measured`: the fitted model
- `noisy`: 3× the noise
- `sensitive` and `insensitive`: 2× and ½× fan sensitivity
- `sluggish`: 3× slower
- `hotter`: 25 % more rise

and in six scenarios:

- steady 25 °C
- steady 28 °C
- a daily 20–30 °C cycle
- a warm spell (26 → 31 → 24 °C)
- a sudden +35 % PHY load
- fan-status glitches

## Scripts

| Script | What it does |
|---|---|
| `bench.py VARIANT... [--set tuning\|full] [--cfg JSON] [--seed N] [--model M] [--json OUT]` | Runs controllers across the plants and scenarios. `band` is fanctl's own controller, `static40` is a fixed 40 %, and a `.py` path loads a variant. `--cfg '{"min_authority": 1.0}'` overrides fanctl settings; `--seed N` changes the noise seed. |
| `sweep.py CANDIDATES [--model M] [--json OUT]` | For each candidate, the full matrix plus the 20-seed load-step and warm-spell safety sweep on the measured plant. A candidate is a variant (default `band`), fanctl setting overrides and environment variables, for example `'{"w0.5": {"cfg": {"min_authority": 0.5}}, "pi": {"variant": "variants/pi.py"}}'`. It prints a summary line and one line per seed-sweep scenario for each, and `--json` writes every run as `[name, plant, scenario, seed, metrics]`. |
| `steady_inlets.py` | Reports the settled speed per inlet, for 1.3 and unweighted. |

They find the model and fanctl from their own location and write only to stdout and the `--json` file you name; paths you give are relative to where you run them. For example, from the repository root:

```text
python3 bench/bench.py band bench/variants/pi.py --json /tmp/out.json
python3 bench/sweep.py '{"w0.5": {"cfg": {"min_authority": 0.5}}}' --json /tmp/sweep.json
```

## Variants (`variants/`)

| Variant | What it is |
|---|---|
| `pi.py` | PI controller |
| `pid.py` | PID controller (D pooled across sensors) |
| `gspi.py` | PI on a linearised fan-law variable |
| `lean.py` | integral-only version of `gspi.py` |
| `model.py` | model-based controller |
| `sonic.py` | The fan algorithm (`CoolingLogicLegacy`) from Arista's SONiC platform library, reimplemented and retuned for config-commit cost. No code from that project is included. |
| `bandplus.py`, `bandlite.py`, `bandauth.py` | band-control variants. Historical: `bandauth.py` became fanctl 1.3, and its `W_MIN` (environment variable `BANDAUTH_WMIN`) is fanctl's `--min-authority`, so new sweeps use `band` with `{"cfg": {"min_authority": W}}`. |

## Results (`results/`)

| File | Contents |
|---|---|
| `final.txt`, `final.json` | **fanctl 1.3.0 as released**: the full matrix and 20-seed sweeps |
| `steady_inlets.txt` | the per-inlet table |
| `model_validation.txt` | the model replayed against the real step test |
| `canonical*.json`, `seeds*.txt` | the controller comparison, including 1.2 band control (`band`) |
| `sweep_cfg.*` | minimum authority 1.0, 0.5 and 0.25 with the probe back-off |
| `sweep_wmin.*` | the first minimum-authority sweep |
| `v1.3.2.txt` | 1.3.2's wait after an at-target raise against 1.3.1, and a rejected first attempt that judged "still getting hotter" from the latest whole-degree reading only (load-step overs 18 of 20 seeds). Below it, 1.4.0's fix (`climb`: a reading at target rose more than 1 °C within the smoothing window; overs 4 of 20) and a rejected one that compared the latest weighted excess (overs 18 of 20). |
| `rejected_debounce.*` | **rejected**: a 2-reading debounce on the immediate raise. Load-step overs rose from 3 to 7 of 20 seeds, and hunting didn't drop. |
| `rejected_backoff_all_decreases.*` | **rejected**: the back-off applied to every decrease. It made hunting on the exaggerated plants worse, up to 82 changes/day. |

The `canonical*` results predate 1.3's final tuning. They were produced by the pre-release 1.2 code with each variant's `regulate()`. `final.*` and `steady_inlets.txt` are from the released 1.3.0. Every variant in `canonical*.json` except `band` (1.2's band control) still reproduces exactly with `python3 bench/bench.py VARIANT`, and `steady_inlets.py` reproduces `steady_inlets.txt`. On Python 3.12 or newer, whose `sum()` adds floats more exactly, averages such as `fan_power_vs_40` can differ from the recorded JSON in their last digits.

The sweeps were written by three scripts that `sweep.py` has replaced: `seeds.py` (`seeds*.txt`), `sweep_wmin.py` (`sweep_wmin.*`) and `sweep_cfg.py` (the rest). On the same fanctl, `sweep.py` gives the same numbers, run for run; its summary line is `sweep_cfg.py`'s and its per-scenario lines are `seeds.py`'s. For example, `sweep_wmin.txt`'s 0.25 row is `sweep.py '{"W_MIN 0.25": {"variant": "variants/bandauth.py", "env": {"BANDAUTH_WMIN": "0.25"}}}'`. The old JSON shapes stay as recorded: `sweep_wmin.json` is `{W_MIN: [[plant, scenario, seed, metrics]]}`, and the other sweep files are `sweep.py`'s `[name, plant, scenario, seed, metrics]`. Each result is from the fanctl of its day, so a rerun on today's fanctl matches only where nothing it depends on has changed since.

In the `rejected_*` files, the labels `v1.3.1`, `v1.3.1-no-debounce` and `v1.3.2` are internal names for 1.3.0 plus the rejected change. They predate, and are unrelated to, the 1.3.1 release.

# measurements: step test on a real switch

This directory holds the raw data and analysis from a fan step test on the reference switch, a DCS-7050TX-64-R on EOS 4.28.13.1M, run on 2026-10-16. The results are summarised in [docs/VALIDATION.md](../docs/VALIDATION.md#step-test).

| File | What it is |
|---|---|
| `step_test_2026-10-16.jsonl` | Raw data: one record per 15 s poll (every sensor, the inlet, the fan speeds, PSU output) and one per speed change. The override went 40 → 70 → 55 → 45 → 35 → 30 → 40 %, 15–30 min per step. |
| `fan_step_test.py` | The test script. It runs off-box over SSH, needs paramiko and `../tools/eos.py` (the SSH session it shares with `tools/monitor.py`), and sends only `environment fan-speed override N` with 30 ≤ N ≤ 70, the range the script allows, and at the end the hand-back to EOS; the session refuses to send anything else. However it ends, it hands the fans back to EOS's automatic control. It never writes memory. |
| `analyze.py` | Steady state per step, the gain curve, time constants, noise and power. Its fits are the ones `build_model.py` uses. |
| `build_model.py` | Builds the benchmark's plant model, `measured_model.json`, which `../bench/` reads from here. |
| `validate_model.py` | Replays the step test through the model and reports the error per sensor. |
| `measured_model.json` | The fitted plant model. |

The analysis scripts need NumPy. `../tests/test_research.py` runs them on this data and checks that they give the tracked model (to within float rounding, which varies with the NumPy version), the validation in `../bench/results/model_validation.txt` and the numbers in docs/VALIDATION.md. Run them from this directory:

```text
python3 analyze.py step_test_2026-10-16.jsonl
python3 build_model.py step_test_2026-10-16.jsonl measured_model.json
python3 validate_model.py step_test_2026-10-16.jsonl measured_model.json
```

## Running a step test on your own switch

A step test changes the fan speed for about three hours. Run one only on a switch you are allowed to change, preferably during a quiet period.

```text
export SWITCH_HOST=switch.example.net SWITCH_USERNAME=admin
python3 fan_step_test.py data.jsonl status.json     # prompts for the password
```

If `SWITCH_PASSWORD` isn't set, the script prompts for it, which keeps it out of your shell history. `SWITCH_ENABLE_PASSWORD` is used if `enable` asks for a different password. The switch's host key must already be in `~/.ssh/known_hosts`; `ACCEPT_NEW_HOST_KEY=1` accepts it on first connection and then pins it for the rest of the run.

**Stop fanctl and fanctl-guard first**, and start them again after the test: either would change the fans under the test (the guard restores automatic control within a minute of an override with no fanctl running, and fanctl pauses on a change it didn't make). The script refuses to start while a daemon whose name starts with `fanctl` is running (`show daemon`); if you named yours differently, stop it yourself.

```text
switch# configure
switch(config)# daemon fanctl-guard
switch(config-daemon-fanctl-guard)# shutdown
switch(config-daemon-fanctl-guard)# exit
switch(config)# daemon fanctl
switch(config-daemon-fanctl)# shutdown
switch(config-daemon-fanctl)# end
```

Afterwards, `no shutdown` under both starts them again; fanctl takes over at EOS's current speed.

Watch the run, and keep the switch's own alarms in place: the script is a measurement tool, not a safety layer. It stops early on an equipment fault (fan, PSU, sensor or EOS status), on a sensor more than 3 °C over its target or within 15 °C of its alert limit, on an inlet over 30 °C or no inlet reading, and when the fans are not at the speed it set (something else changed them). However it ends, also on Ctrl-C or an error, it hands the fans back to EOS's automatic control (`environment fan-speed auto`, else `no environment fan-speed`) and prints how it ended, with the fan setting it found before the test; set that again afterwards if you want it back. A line EOS accepts but that doesn't put the fans in automatic control counts as refused, and it tries the next. Only if EOS refuses both does it leave `override 70`, the top of its range, and say so. It exits with status 0 only if the whole schedule ran and EOS has the fans again. Step-test data from other platforms is a welcome contribution. The analysis scripts and the benchmark here only work with the reference switch's sensors for now: sensor descriptions, targets and alert limits come from its saved JSON (`../tests/fixtures/`), and the benchmark's simulated switch has its sensors, so a model from another platform stops with a `KeyError` (see [../bench/README.md](../bench/README.md#scope)). The raw data is useful all the same; include a saved `show system environment temperature | json` from the switch with it. Before sharing a data file, delete any `error` or `poll-error` records, since they can contain hostnames, addresses or local paths.

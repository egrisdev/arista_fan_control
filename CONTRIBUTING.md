# Contributing

Issues and pull requests are welcome. Results from other platforms are especially useful; the [README](README.md#contributing) says what to include. Report security problems as described in [SECURITY.md](SECURITY.md), not in a public issue.

## What runs on the switch

Two files are installed on the switch, and each is deployed on its own to `/mnt/flash`:

- **`fanctl.py`** runs under EOS's `/usr/bin/python3`, which is Python 3.9 on EOS 4.28. It must stay one file that uses only the standard library and runs on Python 3.9. So no `match` statements, no `X | Y` type unions, no parenthesized context managers, and nothing added to the standard library after 3.9, such as `zip(strict=True)` or `int.bit_count()`.
- **`fanctl-guard.sh`** runs under bash 4.2, with coreutils, `grep`, `logger` and FastCli, and `flock` if present. So no namerefs (`declare -n`), no `wait -n`, no `mapfile -d` and no `${var@Q}`: they all came after 4.2. It must keep working when Python or `fanctl.py` is broken, so it never calls either.

`SHA256SUMS` holds the two files' hashes for the install check (`sha256sum -c SHA256SUMS`). Every commit that changes either file regenerates it in the same commit; a test and CI fail until it matches:

```text
sha256sum fanctl.py fanctl-guard.sh > SHA256SUMS
```

The two files carry the same `VERSION` (a test pins the guard's copy to fanctl's). Only a release changes it, in both files at once.

The off-box tools in `tools/` and `measurements/` run on a management host and may use other packages: paramiko for `tools/monitor.py` and `measurements/fan_step_test.py`, and NumPy for the analysis scripts in `measurements/`. The suite needs neither: the tests of the analysis scripts are skipped without NumPy, with the reason, and CI installs it for the Python 3.10 and 3.12 runs.

## Running the tests

```text
python3 -m unittest discover -s tests -v
```

The suite runs on Linux, takes about 4 minutes and never contacts a switch: `tests/sim.py` and `tests/fake_fastcli.py` stand in for it, and `tests/fake_paramiko.py` for the SSH session of the off-box tools. [VALIDATION.md](docs/VALIDATION.md#off-box-test-suite) lists what it covers. The tests of the guard need bash, GNU coreutils and util-linux `flock`; where something is missing, those tests are skipped with the reason. One test only runs as root.

To test on the switch's software, run the suite in the image that [`tests/eosenv/Dockerfile`](tests/eosenv/Dockerfile) builds (Python 3.9, bash 4.2 first on `PATH`), as an ordinary user and as root:

```text
docker build -t fanctl-eosenv tests/eosenv
docker run --rm -v "$PWD:/src:ro" -w /src fanctl-eosenv python3 -m unittest discover -s tests
docker run --rm --user root -v "$PWD:/src:ro" -w /src fanctl-eosenv python3 -m unittest discover -s tests
```

CI ([`.github/workflows/test.yml`](.github/workflows/test.yml)) runs these two, the suite on Python 3.10 and 3.12 with NumPy, `sha256sum -c SHA256SUMS` and `shellcheck fanctl-guard.sh`.

## Making a change

- **Test first.** Write a test that fails without your change, and check that it fails for the reason you expect. Drive fanctl the way its users do: the daemon through the simulator, the command line, or the end-to-end tests with the fake FastCli. Check what an operator would see: the fan setting, the log line, the status file or the `--probe` output. Adding or removing a test also means updating the count in [docs/VALIDATION.md's off-box test suite](docs/VALIDATION.md#off-box-test-suite); a test fails until it matches.
- **Keep it small and readable.** The code has one place for each decision, such as `TUNABLES` for every option's default and range, and pure functions for the decisions, with I/O at the edges. Keep it that way rather than adding a second copy.
- **Keep the docs true.** If the change alters what an operator can see, update README.md, [docs/DESIGN.md](docs/DESIGN.md) and [docs/OPERATIONS.md](docs/OPERATIONS.md) in the same pull request, and add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) (start that section at the top if there is none). [DESIGN.md's glossary](docs/DESIGN.md#glossary) gives the terms.
- **Keep recorded results reproducible.** The benchmark scripts in `bench/` and the analysis in `measurements/` are tested by reproducing recorded results; after changing one, rerun its documented command and check the output is unchanged. Only the candidate controllers in `bench/variants/` stay as recorded, untested ([bench/README.md](bench/README.md#scope)).
- **Never test against a switch you depend on.** `measurements/fan_step_test.py` changes fan speed on real hardware; read [measurements/README.md](measurements/README.md) first.

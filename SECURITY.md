# Security policy

## Reporting a vulnerability

Please don't describe security problems in public issues. Report them privately instead:

1. Use GitHub's private vulnerability reporting: on the repository's **Security** tab, choose **Report a vulnerability**.
2. If that button isn't there, open a public issue titled `Security contact request`, with no details of the problem, and the maintainer will reply with a private way to send them.

Include the fanctl version, the EOS release, and the steps to reproduce. This is a volunteer project with no response-time commitment, but reports are taken seriously.

## Supported versions

Only the latest release receives fixes.

## Security model

fanctl and its guard run on the switch as EOS daemons, with the right to change the fan configuration. They:

- run only fixed `show` and `environment fan-speed` commands through FastCli, with no shell and no user-supplied text on the command line
- accept options only from the daemon's `exec` line, which needs configuration rights to change
- write nothing to flash, and never run `write memory`

### Runtime files

The lock, status (heartbeat) and memory files live in `/run/fanctl` (`fanctl.lock`, `fanctl.status.json`, `fanctl.memory.json`). `/run` is in RAM. fanctl creates the directory if it is missing, owned by root with mode 0755, and its files with mode 0644, whatever the umask. So only root can create or change files in it, and any user can run `--status`.

Before it starts, fanctl checks every directory that holds these files, both as written and with every symlink resolved. It logs `unsafe runtime directory …; not starting` and exits, before it takes the lock or runs FastCli, if:

- the directory is a symlink or not a directory
- the directory or a directory above it is owned by a user other than root or the one fanctl runs as
- the directory is writable by group or others
- a directory above it is writable by group or others and isn't sticky (`/tmp` can hold the directory, but can't be it)

These checks read a path as text, so fanctl refuses `--state-file`, `--memory-file` or `--lock-file` values that contain `..` (`bad command line, not starting`).

The guard applies the same ownership and permission rules to the directories of the lock and the heartbeat, with every symlink resolved, and refuses a directory that is itself a symlink. Unlike fanctl, it doesn't also check the path as written. That matters only for a non-default location reached through another user's symlink; the default `/run/fanctl` has none. If the directories fail, the guard doesn't trust the lock and heartbeat: it treats fanctl as not managing the fans and restores automatic fan control. `--status` checks the directories before it opens anything in them, prints an `UNTRUSTED` line and returns non-zero.

1.3.0 kept these files in `/tmp`, where any local user could interfere with them. See the [changelog](CHANGELOG.md).

These checks keep other local accounts away from the files. They don't protect against root, or against a privileged (`enable`) user, who can change the fan configuration directly anyway. Only give privileged access to administrators on switches running fanctl.

### The installed files

`SHA256SUMS` lists the hashes of `fanctl.py` and `fanctl-guard.sh`, and the off-box tests check that it matches them. It ships beside the files it covers, so it proves that a copy is complete and unchanged, not who wrote it: anyone who can change a fork or mirror can change both. Both files run as root, so read them, or take them only from a source you trust.

### Off-box tools

`tools/monitor.py` and `measurements/fan_step_test.py` connect over SSH with a password:

- If `SWITCH_PASSWORD` isn't set, they prompt for it, so it stays out of shell history.
- They refuse unknown host keys unless `ACCEPT_NEW_HOST_KEY=1` is set. With it, they pin the key from the first connection for the rest of the run.

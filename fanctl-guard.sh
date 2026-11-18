#!/bin/bash
# fanctl-guard - restore EOS automatic fan control when an
# 'environment fan-speed override' is in effect but no live fanctl manages it
# (fanctl stopped, killed, hung, broken, never started after a reload, or only
# running as --dry-run).
#
# Runs as its own EOS daemon and needs only bash, coreutils (stat, date,
# timeout, readlink, dirname, mkdir), grep and FastCli, plus flock if present.
# It keeps working if fanctl.py or Python breaks, and writes nothing to flash.
#
# fanctl's lock is the one token for writing the fan setting: fanctl holds it
# for as long as it runs, and the guard holds it from deciding to restoring,
# so a fanctl that starts meanwhile waits rather than finding its setting
# changed under it. The guard writes without it only when the lock can't be
# trusted or a hung fanctl or a dry run holds it.
#
#   fanctl-guard.sh          check once (by hand: bash /mnt/flash/fanctl-guard.sh)
#   fanctl-guard.sh loop     check every 60 s after a 180 s boot grace period (the daemon)

# VERSION, STATE, LOCK and STALE copy fanctl.py's VERSION, STATE_FILE, LOCK_FILE
# and STALE_AFTER, the source of truth; a test pins the copies to it. Only a
# fanctl and a guard from the same release are known to work together.
VERSION=1.4.0
STATE=${FANCTL_STATE:-/run/fanctl/fanctl.status.json}
LOCK=${FANCTL_LOCK:-/run/fanctl/fanctl.lock}
CLI=${FANCTL_CLI:-FastCli}
LOGGER=${FANCTL_LOGGER:-logger}
STALE=${FANCTL_STALE:-300}          # s without a heartbeat before fanctl counts as hung
INTERVAL=${FANCTL_GUARD_INTERVAL:-60}
GRACE=${FANCTL_GUARD_GRACE:-180}
CLI_TIMEOUT=${FANCTL_CLI_TIMEOUT:-60}

log() {   # log PRIORITY MESSAGE
    "$LOGGER" -p "local4.$1" -t fanctl-guard "$2" || true
}

cli() {   # cli "COMMANDS": FastCli at privilege 15, never allowed to hang the guard
    # 9<&-: a FastCli left behind must not go on holding fanctl's lock (check)
    if command -v timeout >/dev/null 2>&1; then
        # SIGKILL, as fanctl's Cli: a wedged FastCli may ignore SIGTERM
        timeout -s KILL "$CLI_TIMEOUT" "$CLI" -p 15 -c "$1" 9<&-
    else
        "$CLI" -p 15 -c "$1" 9<&-
    fi
}

dir_ok() {   # dir_ok DIR: only root (or we) can have planted or changed what is in it
    local d=$1 top=1 st owner mode
    [ -d "$d" ] && [ ! -L "$d" ] || return 1
    d=$(readlink -f -- "$d") || return 1
    while true; do
        # owned by root or us; not group/other-writable, unless a sticky dir above it (/tmp)
        st=$(stat -c '%u %a' -- "$d" 2>/dev/null) || return 1
        owner=${st% *}
        mode=$(( 8#${st#* } ))
        [ "$owner" -eq 0 ] || [ "$owner" -eq "$EUID" ] || return 1
        (( (mode & 8#022) && (top || !(mode & 8#1000)) )) && return 1
        [ "$d" = / ] && return 0
        d=$(dirname -- "$d")
        top=0
    done
}

heartbeat_ok() {   # a fanctl that holds the lock is alive: fresh heartbeat, not a dry run
    local mtime now age
    mtime=$(stat -c %Y "$STATE" 2>/dev/null) || return 1          # no heartbeat
    now=$(date +%s)
    age=$(( now - mtime ))
    [ "$age" -le "$STALE" ] && [ "$age" -ge -"$STALE" ] || return 1   # hung (or clock nonsense)
    grep -q '"dry_run": false' "$STATE" 2>/dev/null || return 1     # a dry run controls nothing
    return 0
}

cli_ok() {   # like cli, but also fails on an EOS '% ...' error line
    local out
    out=$(cli "$1" 2>&1) || return 1
    case "$out" in "% "*|*$'\n% '*) return 1 ;; esac
    printf '%s\n' "$out"
}

cooling_setting() {   # cooling_setting JSON: auto, override N% (override if no N) or nothing
    local speed='"overrideFanSpeed": *([0-9]+)'
    case "$1" in
        *'"coolingMode": "automatic"'*) echo auto ;;
        *'"coolingMode": "manual"'*)
            if [[ $1 =~ $speed ]]; then echo "override ${BASH_REMATCH[1]}%"; else echo override; fi ;;
    esac
}

fan_setting() {   # prints auto, override N% or unknown, as fanctl's fmt_setting(current_setting())
    # Unlike fanctl, an override that either source shows wins: the guard errs towards acting.
    local rc cool num='override ([0-9]+)'
    if rc=$(cli_ok 'show running-config | include ^environment fan-speed override'); then
        if [[ $rc =~ $num ]]; then echo "override ${BASH_REMATCH[1]}%"; return; fi
        # No override line. Double-check with the cooling status if it answers.
        cool=$(cli_ok 'show system environment cooling | json') && cool=$(cooling_setting "$cool")
        case "$cool" in override*) echo "$cool"; return ;; esac
        echo auto
        return
    fi
    cool=$(cli_ok 'show system environment cooling | json') && cool=$(cooling_setting "$cool")
    echo "${cool:-unknown}"
}

settled() {   # settled WANT: the fan setting, read until it is WANT, at most 3 times 2 s apart
    local setting i
    for i in 1 2 3; do
        setting=$(fan_setting)
        [ "$setting" = "$1" ] && break
        [ "$i" -lt 3 ] && sleep 2
    done
    echo "$setting"
}

check() {
    local dir
    dir=$(dirname -- "$LOCK")
    [ -e "$dir" ] || mkdir -m 0755 -- "$dir" 2>/dev/null   # no fanctl since boot: as fanctl would
    # Trust the lock and heartbeat only where no other user can fake them.
    if dir_ok "$dir" && dir_ok "$(dirname -- "$STATE")" \
            && { [ -e "$LOCK" ] || : >>"$LOCK"; } 2>/dev/null && [ -r "$LOCK" ]; then
        check_locked 9<"$LOCK"
    else
        restore
    fi
}

check_locked() {   # with fanctl's lock file open on fd 9
    # fanctl holds an exclusive flock on $LOCK for as long as it runs. If we
    # get it, no fanctl runs, and one that starts now waits until we are done.
    if command -v flock >/dev/null 2>&1 && flock -n -x 9; then
        restore
    elif heartbeat_ok; then
        check_pair
    else
        restore        # hung, a dry run, or (without flock) not running
    fi
}

WARNED=   # the fanctl version check_pair last warned about
check_pair() {   # a live fanctl from another release: say so, once per version
    local version='"version": "([^"]*)"'
    [[ $(< "$STATE") =~ $version ]] 2>/dev/null || return 0
    version=${BASH_REMATCH[1]}
    [ "$version" = "$VERSION" ] || [ "$version" = "$WARNED" ] && return 0
    WARNED=$version
    log warning "fanctl $version is running with fanctl-guard $VERSION; install both files from the same release"
}

restore() {   # hand the fans to EOS; the same steps as fanctl's set_fans(AUTO)
    local before after step want line
    before=$(fan_setting)
    [ "$before" = auto ] && return 0
    # auto, else the other way back, else flat out: never leave a low override in place
    for step in 'auto:environment fan-speed auto' 'auto:no environment fan-speed' \
                'override 100%:environment fan-speed override 100'; do
        want=${step%%:*} line=${step#*:}
        if cli_ok $'configure\n'"$line"$'\nend' >/dev/null; then
            after=$(settled "$want")
        else
            after=$(fan_setting)      # refused (or timed out): did it take anyway?
        fi
        [ "$after" = "$want" ] || [ "$after" = unknown ] && break   # done, or can't tell: stop
    done
    case "$after" in
        auto) log warning "fanctl is not managing the fans (stopped, hung, dry run or unsafe runtime directory) but the fan setting was $before; restored automatic fan control" ;;
        "override 100%") log err "fanctl is not managing the fans and EOS refused automatic fan control; fans set to override 100% (the setting was $before)" ;;
        *) log err "fanctl is not managing the fans and restoring automatic fan control FAILED (setting: $after); run 'environment fan-speed auto' by hand" ;;
    esac
}

umask 022   # as fanctl: anyone's --status can read a lock file we create
if [ "$1" = "loop" ]; then
    log notice "fanctl-guard $VERSION started: first check in $GRACE s, then every $INTERVAL s"
    sleep "$GRACE"
    while true; do
        check
        sleep "$INTERVAL"
    done
else
    check
fi

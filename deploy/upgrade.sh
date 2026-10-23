#!/usr/bin/env bash
# Upgrade a running House Helper deployment to the version checked out in this repository,
# failing closed: NO schema change happens unless a backup of the current database has been
# made, checked and verified first.
#
#   bash deploy/upgrade.sh --old-tag OLD_TAG [--new-tag NEW_TAG] [--backup-dir DIR] [--dry-run]
#
#   --old-tag     the image that is running now (house-helper:OLD_TAG). Required, no default:
#                 the pre-upgrade backup must be made, and checked, by the image that wrote the
#                 database. The script checks that the running container really is that image.
#   --new-tag     the image to upgrade to (default: the 12-character commit id of HEAD). It must
#                 already be built (`docker compose -f deploy/compose.yaml build`) from the
#                 commit that is checked out: its revision label is compared with HEAD.
#   --backup-dir  as for backup.sh (default $HOME/backups/house-helper).
#   --dry-run     do every check that can be done without stopping anything, then stop.
#
# Order, and what stops the script:
#   0  checks that change nothing (compose file, both images, the running version, the backup
#      directory). Any failure: stop, nothing was touched.
#   1  stop the web service and confirm it is stopped.
#   2  backup.sh with the OLD image: backup, size, SHA-256, disposable verification with
#      `--require-head`, published as a pair (the database file and its checksum file). Then THE
#      GATE (backup_problem, below) judges the files themselves, not backup.sh's word: the selected
#      file's name must match NAME_PATTERN, and its checksum file must be byte for byte the ONE line
#      "<SHA-256 of that file, which this script computes itself>  <its base name>" and a newline.
#      ANY failure, or a pair that is not exactly that: stop here. The database has not been
#      touched; the web service stays stopped and the script prints the command that starts the
#      old version.
#   3  only now: the migration, with the NEW image, under the exclusive lock.
#   4  the schema check, then start the new version and wait for its health check.
#
# The gate does not use `sha256sum -c`: that checks whichever file the checksum file NAMES, so a
# checksum file for another file would pass for the one selected. Here the expected content is built
# from the selected file's own base name and its own freshly computed digest, and compared exactly.
#
# From step 3 on there is ONE way out that is not success, finish_failed, and every failure takes
# it: a command that is checked (the migration, the schema check, `up`, finding the container,
# `docker inspect`, a container that is unhealthy, reports no health or never becomes healthy), a
# command nothing checks (the EXIT trap catches what `set -e` ends the script on), Ctrl-C,
# SIGTERM, SIGHUP. It
#   * names the operation that failed and its exit status;
#   * if the new version may have been started, stops the web service ONLY (`docker compose stop
#     house-helper`: this stack's one service, never another container) and says whether it
#     could, and whether it could confirm that nothing is running;
#   * prints the recovery for this exact backup and the pre-upgrade image, and exits 1.
# It never retries a migration on a file that may be half-migrated, never restores a backup by
# itself and never downgrades: that is the operator's decision, with the commands it prints.
#
# The shell's own variables are not read: not an old $F, not HOUSE_HELPER_IMAGE_TAG (this
# script sets it for each command). It reads what Compose reads: HOUSE_HELPER_ENV_FILE,
# HOUSE_HELPER_HOST, HOUSE_HELPER_HSTS_SECONDS. Exit status: 0 upgraded, 1 stopped, 2 usage.
set -euo pipefail
umask 077

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname -- "$HERE")
COMPOSE_FILE=$HERE/compose.yaml
SERVICE=house-helper
HEALTH_WAIT_SECONDS=${HOUSE_HELPER_UPGRADE_HEALTH_WAIT:-120}   # a test hook; the default is the real value
HEALTH_POLL_SECONDS=2
CONTAINER_ID_PATTERN='^[0-9a-f]{12,64}$'
# The one rule for the file name of a backup, the same text as in backup.sh and RUNBOOK section 10: ASCII
# letters, digits, _ . - only, not starting with . or -, ending in .db. Checked in the C locale so that no
# locale can widen a range or count a multi-byte character as one.
NAME_PATTERN='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db$'
name_ok() { local LC_ALL=C; [[ $1 =~ $NAME_PATTERN ]]; }

say() { printf '%s\n' "$*" >&2; }
fail() { say "upgrade.sh: $*"; exit 1; }
usage() {
    say "usage: bash deploy/upgrade.sh --old-tag OLD_TAG [--new-tag NEW_TAG] [--backup-dir DIR] [--dry-run]"
    exit 2
}

unset HOUSE_HELPER_IMAGE_TAG HOUSE_HELPER_GIT_SHA   # never inherit a stale one
old_tag= new_tag= backup_dir=${HOME:-/nonexistent}/backups/house-helper dry_run=0
while [ $# -gt 0 ]; do
    case $1 in
        --old-tag) [ $# -ge 2 ] || usage; old_tag=$2; shift 2 ;;
        --new-tag) [ $# -ge 2 ] || usage; new_tag=$2; shift 2 ;;
        --backup-dir) [ $# -ge 2 ] || usage; backup_dir=$2; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$old_tag" ] || usage
tag_pattern='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
[[ $old_tag =~ $tag_pattern ]] || fail "the old tag has characters an image tag cannot have."
[[ $HEALTH_WAIT_SECONDS =~ ^[0-9]{1,5}$ ]] || fail "HOUSE_HELPER_UPGRADE_HEALTH_WAIT must be a whole number of seconds."

head_sha=$(git -C "$REPO" rev-parse HEAD) || fail "this is not a git checkout: cannot tell which commit is being installed."
[ -n "$new_tag" ] || new_tag=$(git -C "$REPO" rev-parse --short=12 HEAD) || fail "cannot read the commit id of this checkout."
[[ $new_tag =~ $tag_pattern ]] || fail "the new tag has characters an image tag cannot have."
[ "$old_tag" != "$new_tag" ] || fail "the old and the new tag are the same ($old_tag): there is nothing to upgrade, and the backup would not be of the old version."

# Compose runs for one image tag at a time. $1 = tag; the rest is the Compose command line.
compose() { local tag=$1; shift; HOUSE_HELPER_IMAGE_TAG=$tag docker compose -f "$COMPOSE_FILE" "$@"; }
ops() { local tag=$1; shift; compose "$tag" --profile ops run --rm -T house-helper-ops "$@"; }

backup_path=
old_image_id=unknown
stop_state=not_started   # after a failure: not_started, stopped (confirmed), running, or unknown
recovery() {
    local q_backup q_dir q_name q_sum
    if [ -z "$backup_path" ]; then
        cat >&2 <<EOF

RECOVERY: no backup was taken or accepted, so the database is exactly as it was. Start the old version again with
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml up -d $SERVICE
and find out why (the messages above) before trying again.
EOF
    else
        q_backup=$(printf '%q' "$backup_path")
        q_dir=$(printf '%q' "$(dirname -- "$backup_path")")
        q_name=$(printf '%q' "${backup_path##*/}")   # the gate has already limited it to letters, digits, _ . -
        q_sum=$(printf '%q' "${backup_path##*/}.sha256")
        cat >&2 <<EOF

RECOVERY: do NOT retry the migration on this file. The pre-upgrade backup, made and verified by the OLD image, is
    $backup_path
    checksum file: $backup_path.sha256
The matching pre-upgrade image is house-helper:$old_tag (image id $old_image_id). Keep both; do not run docker image prune -a.
Restore the backup with that image and start the OLD version, in this order, and go on only when each step has worked
(deploy/RUNBOOK.md section 10, Restore):
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml stop $SERVICE
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml ps --status running --quiet $SERVICE   # must print NOTHING: it is stopped
    ( cd $q_dir && echo "\$(sha256sum < $q_name | cut -d' ' -f1)  $q_name" | cmp - $q_sum && echo "the checksum file is exactly the one line for $q_name" )
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops check-idle   # must exit 0
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops restore --input - --replace < $q_backup
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops check-schema   # must exit 0
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml up -d $SERVICE
    HOUSE_HELPER_IMAGE_TAG=$old_tag docker compose -f deploy/compose.yaml ps $SERVICE   # State: running, Health: healthy (about 10 s)
(deploy/RUNBOOK.md, Rollback and recovery.) Nothing was restored or downgraded for you.
EOF
        case $stop_state in
            running | unknown)
                say "!! The new version may STILL BE RUNNING on the migrated database. Run the first command above (the stop) NOW, and confirm that it stopped (the second command must print nothing)."
                ;;
        esac
    fi
}

# A backup is accepted only as the exact pair backup.sh publishes, and this function judges the files,
# not backup.sh's word. Contract (RUNBOOK section 10), for the selected file F = <dir>/<base>:
#   * <base> matches NAME_PATTERN (ASCII letters, digits, _ . -; not first . or -; ends in .db), and F has a
#     directory part;
#   * F and F.sha256 are regular, non-empty files, neither of them a symbolic link;
#   * F.sha256 is, byte for byte, ONE line: the SHA-256 of F (64 lower-case hex digits, computed HERE, from
#     F's own bytes), two spaces, <base>, a newline. So a checksum for another file name, more than one
#     record, an empty or malformed file, a wrong digest, a path (./F, a directory, an absolute path),
#     binary-mode or CRLF or any other variation: all refused.
# The expected line is built here and compared with cmp, never read back through `sha256sum -c`, which
# would check whatever file the line happens to name. (backup.sh writes exactly this line.) Prints the
# reason and returns 1 if the pair is not that.
backup_problem() {
    local file=$1 base sidecar digest shown
    case $file in
        '' | *$'\n'*) echo "backup.sh did not print exactly one path"; return 1 ;;
        */*) ;;
        *) printf -v shown '%q' "${file:0:80}"; echo "backup.sh reported $shown, which has no directory part"; return 1 ;;
    esac
    base=${file##*/}
    sidecar=$file.sha256
    if ! name_ok "$base"; then
        printf -v shown '%q' "${base:0:80}"
        echo "the backup's file name $shown is not one backup.sh makes (ASCII letters, digits, _ . - only, ending in .db)"
        return 1
    fi
    if [ -L "$file" ] || [ ! -f "$file" ] || [ ! -s "$file" ]; then
        echo "there is no regular, non-empty backup file $file (a symbolic link is not accepted)"
        return 1
    fi
    if [ -L "$sidecar" ] || [ ! -f "$sidecar" ] || [ ! -s "$sidecar" ]; then
        echo "$file has no regular, non-empty checksum file ($sidecar): an incomplete, uncommitted backup"
        return 1
    fi
    digest=$(sha256sum < "$file" | cut -d' ' -f1) || { echo "cannot read $file to compute its SHA-256"; return 1; }
    [[ $digest =~ ^[0-9a-f]{64}$ ]] || { echo "could not compute a SHA-256 for $file"; return 1; }
    printf '%s  %s\n' "$digest" "$base" | cmp -s -- - "$sidecar" \
        || { echo "the checksum file $sidecar is not exactly the one line '<SHA-256 of $base>  $base' and a newline (it is empty or malformed, names another file or a path, has more than one record, or the digest is wrong)"; return 1; }
}

# --- 0. checks that change nothing ---------------------------------------------------------------

say "== 0. checks (nothing is changed) =="
command -v cmp > /dev/null \
    || fail "cmp (diffutils) is not installed: the backup gate compares the checksum file exactly, so nothing was started."
compose "$new_tag" config -q > /dev/null 2>&1 \
    || fail "the Compose file does not resolve: is HOUSE_HELPER_ENV_FILE set to an existing file? (docker compose -f deploy/compose.yaml config -q)"
old_image_id=$(docker image inspect -f '{{ .Id }}' "house-helper:$old_tag" 2> /dev/null) || fail "there is no image house-helper:$old_tag."
docker image inspect "house-helper:$new_tag" > /dev/null 2>&1 \
    || fail "there is no image house-helper:$new_tag: build it first (HOUSE_HELPER_IMAGE_TAG=$new_tag HOUSE_HELPER_GIT_SHA=$head_sha docker compose -f deploy/compose.yaml build)."
built_from=$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "house-helper:$new_tag") \
    || fail "cannot read the revision label of house-helper:$new_tag."
[ "$built_from" = "$head_sha" ] \
    || fail "house-helper:$new_tag was built from '$built_from', but the checkout is at $head_sha: rebuild it from this commit."

running=$(compose "$old_tag" ps --status running --quiet "$SERVICE") \
    || fail "cannot ask Compose whether the web service is running (docker compose ps failed); nothing was changed."
if [ -n "$running" ]; then
    running_image=$(docker inspect -f '{{ .Config.Image }}' "$running") \
        || fail "cannot inspect the running web service container; nothing was changed."
    [ "$running_image" = "house-helper:$old_tag" ] \
        || fail "the web service is running $running_image, not house-helper:$old_tag: --old-tag must name the image that is running."
fi
if [ ! -d "$backup_dir" ]; then mkdir -p -m 700 -- "$backup_dir" || fail "cannot create the backup directory $backup_dir."; fi
[ -w "$backup_dir" ] && [ -x "$backup_dir" ] || fail "the backup directory $backup_dir is not writable."
say "ok: house-helper:$old_tag (current) -> house-helper:$new_tag (built from $head_sha); backups go to $backup_dir"
if [ "$dry_run" = 1 ]; then say "dry run: stopping here; nothing was changed."; exit 0; fi

# --- 1. stop the web service ----------------------------------------------------------------------------

say "== 1. stop the web service =="
if ! compose "$old_tag" stop "$SERVICE" > /dev/null 2>&1; then
    say "upgrade.sh: could not stop the web service."
    recovery
    exit 1
fi
if ! still=$(compose "$old_tag" ps --status running --quiet "$SERVICE"); then
    say "upgrade.sh: cannot confirm that the web service is stopped (docker compose ps failed); nothing else was done."
    recovery
    exit 1
fi
if [ -n "$still" ]; then
    say "upgrade.sh: the web service is still running; nothing else was done."
    recovery
    exit 1
fi

# --- 2. the backup, and the gate --------------------------------------------------------------------------

say "== 2. backup and verification, with the OLD image (house-helper:$old_tag) =="
if ! reported=$(bash "$HERE/backup.sh" --tag "$old_tag" --dir "$backup_dir" --label "pre-upgrade-$old_tag-to-$new_tag"); then
    say "upgrade.sh: backup.sh did not report a verified backup. The migration was NOT run and the database was not changed. The web service is stopped."
    say "upgrade.sh: if backup.sh was interrupted just after it published, a complete pair may be in $backup_dir (backup.sh says so above); this script does not use a backup it was not given."
    recovery
    exit 1
fi
if ! why=$(backup_problem "$reported"); then
    say "upgrade.sh: backup.sh reported success, but its result cannot be trusted: $why. The migration was NOT run and the database was not changed. The web service is stopped."
    recovery
    exit 1
fi
backup_path=$reported
say "gate passed: $backup_path (its checksum file, $backup_path.sha256, is exactly the one line for it)"

# --- failure handling from here on ----------------------------------------------------------------------------
# The migration changes the database. Past this point the way out is finish_failed, always (see the
# header), and `success` is the only other way out.

current_op="the gate (just passed)"
new_version_may_run=0
upgraded=0

# Stop the new version if it may be running, and say, in words, what is known about it. Only the
# web service of this Compose project is named: nothing else is stopped.
stop_new_version() {
    local stop_status=0 ps_status=0 stop_output= left=
    if [ "$new_version_may_run" != 1 ]; then
        stop_state=not_started
        say "The new version was never started by this script: the web service is still stopped (step 1)."
        return 0
    fi
    say "The new version cannot be trusted. Stopping the web service ($SERVICE, and nothing else) ..."
    stop_output=$(compose "$new_tag" stop "$SERVICE" 2>&1) || stop_status=$?
    left=$(compose "$new_tag" ps --status running --quiet "$SERVICE" 2> /dev/null) || ps_status=$?
    if [ "$stop_status" != 0 ]; then
        say "upgrade.sh: 'docker compose stop $SERVICE' FAILED (exit status $stop_status):"
        printf '%s\n' "$stop_output" | tail -n 5 >&2
    fi
    if [ "$ps_status" != 0 ]; then
        stop_state=unknown
        say "upgrade.sh: COULD NOT CONFIRM that the web service is stopped (docker compose ps failed, exit status $ps_status)."
        say "upgrade.sh: its state is UNKNOWN: treat the new version as STILL RUNNING until you have stopped it yourself and seen it stop."
    elif [ -n "$left" ]; then
        stop_state=running
        say "upgrade.sh: the web service is STILL RUNNING on the migrated database, unverified. Stop it yourself first (command 1 of the recovery below)."
    else
        stop_state=stopped
        say "The web service is stopped, and confirmed: Compose lists no running container for it."
    fi
}

# The one failure handler. Reports, stops the new version if it may be running, prints the recovery,
# and exits 1. Safe to reach from a trap: it turns errexit off and the traps (it cannot be entered twice).
finish_failed() {
    set +e
    trap - EXIT INT TERM HUP
    say ""
    say "upgrade.sh: FAILED while: ${current_op}."
    [ $# -eq 0 ] || say "upgrade.sh: $*"
    say "upgrade.sh: the upgrade did NOT complete, and nothing here reports it as done."
    stop_new_version
    recovery
    exit 1
}
abort() { finish_failed "$*"; }
on_exit() {
    local status=$?
    if [ "$upgraded" != 1 ]; then
        finish_failed "the script stopped (exit status $status) on a command that had no check of its own. Nothing is assumed about the new version."
    fi
}
trap on_exit EXIT
trap 'finish_failed "interrupted (SIGINT)."' INT
trap 'finish_failed "terminated (SIGTERM)."' TERM
trap 'finish_failed "the terminal went away (SIGHUP)."' HUP

# --- 3. the migration, 4. the check and the start --------------------------------------------------------

say "== 3. migrate with house-helper:$new_tag (exclusive lock) =="
current_op="the migration (alembic upgrade head, house-helper:$new_tag)"
ops "$new_tag" exec --exclusive -- alembic upgrade head >&2 \
    || abort "the migration FAILED (exit status $?). The database may be half-migrated: do not retry it."

say "== 4. check the schema, start the new version =="
current_op="the schema check (check-schema, house-helper:$new_tag)"
ops "$new_tag" check-schema >&2 \
    || abort "the schema is not at the new image's head after the migration (exit status $?)."

current_op="starting the new version (docker compose up -d $SERVICE)"
new_version_may_run=1   # from here a container may exist, even if this command reports an error
up_output=$(compose "$new_tag" up -d "$SERVICE" 2>&1) \
    || { up_status=$?; printf '%s\n' "$up_output" | tail -n 10 >&2; abort "the new version could not be started (exit status $up_status)."; }

current_op="finding the new container (docker compose ps --status running --quiet $SERVICE)"
container=$(compose "$new_tag" ps --status running --quiet "$SERVICE") \
    || abort "docker compose ps failed (exit status $?): the new container cannot be found, so its health cannot be read."
[ -n "$container" ] \
    || abort "Compose lists no running container for $SERVICE after the start: it did not start, or it exited at once (docker compose -f deploy/compose.yaml logs $SERVICE)."
if ! [[ $container =~ $CONTAINER_ID_PATTERN ]]; then
    printf -v shown '%q' "${container:0:80}"
    abort "Compose printed $shown where exactly one container id was expected."
fi

current_op="waiting for the new container's health check (docker inspect ${container:0:12}, up to ${HEALTH_WAIT_SECONDS}s)"
waited=0
while :; do
    status=$(docker inspect -f '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}none{{ end }}' "$container") \
        || abort "docker inspect of the new container failed (exit status $?): its health cannot be read."
    case $status in
        healthy) break ;;
        starting) ;;
        unhealthy) abort "the new version reports 'unhealthy': its health check is failing (docker compose -f deploy/compose.yaml logs $SERVICE)." ;;
        none | '') abort "Docker reports no health status for the new container, so it cannot be verified." ;;
        *) printf -v shown '%q' "${status:0:80}"; abort "Docker reports a health status this script does not know: $shown." ;;
    esac
    [ "$waited" -lt "$HEALTH_WAIT_SECONDS" ] \
        || abort "the new version is still 'starting' after ${HEALTH_WAIT_SECONDS}s: its health was never verified (docker compose -f deploy/compose.yaml logs $SERVICE)."
    sleep "$HEALTH_POLL_SECONDS" || abort "sleep failed (exit status $?)."
    waited=$((waited + HEALTH_POLL_SECONDS))
done

upgraded=1
trap - EXIT INT TERM HUP
say "upgraded: house-helper:$old_tag -> house-helper:$new_tag, healthy."
say "Still to do by hand (RUNBOOK section 8): logs, TLS, one real sign-in, one chat message."
say "Keep house-helper:$old_tag and $backup_path until the new version is accepted."

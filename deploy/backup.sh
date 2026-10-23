#!/usr/bin/env bash
# Take a backup of the live House Helper database, verify it, and publish it. Nothing else.
#
#   bash deploy/backup.sh --tag IMAGE_TAG [--dir BACKUP_DIR] [--label TEXT | --name FILE.db]
#
#   --tag    the image that matches the database (house-helper:IMAGE_TAG). For a backup before
#            an upgrade that is the image that is RUNNING now, not the new one. Required: there
#            is no default and no environment variable, so a stale shell cannot supply it.
#   --dir    where backups go (default $HOME/backups/house-helper). Created 0700 if missing.
#   --label  part of the file name (default: the tag).
#   --name   the exact file name instead (letters, digits, _ . -, ending in .db). If a file of
#            that name exists the script stops and does not touch it.
#
# What it does, and refuses to do:
#   * the backup is made by `dbtool backup` (SQLite's online backup, checked for integrity and
#     for this application's schema before it is released) and streamed to the host;
#   * it is written to a private, exclusively created `.part` file. Nothing is published until the
#     size, the SHA-256 (against the one the tool reported) and a verification by a disposable
#     container (`dbtool verify --require-head`) have all passed, and the checksum file has been
#     written and read back.
#
# THE FORMAT OF A COMPLETED BACKUP is a pair, NAME.db and NAME.db.sha256 (NAME is
# house-helper-<UTC time>-<random>-<label>, or the --name given; NAME.db matches NAME_PATTERN
# below, the one rule for a backup's file name here, in upgrade.sh and in the runbook). The checksum
# file is exactly ONE line, byte for byte:
#     <64 lower-case hex digits: the SHA-256 of NAME.db>, two spaces, NAME.db, a newline.
# Each name is made with a hard link, which is atomic and fails instead of replacing anything, and
# in this order:
#     1. NAME.db.sha256   the checksum file, complete;
#     2. NAME.db          LAST. Its appearance is the commit.
# So the database file is never visible without its checksum file: not at any instant, and not
# after this script is killed (the directory is synced between the steps, for a power cut). If
# step 2 fails, this script removes the checksum file it made in step 1; if the script is killed
# between the two, what is left is a checksum file with no database file, which is not a backup
# (it blocks only its own name until it is deleted by hand). A reader treats NAME.db as a backup
# only if NAME.db.sha256 is beside it and is exactly that one line for NAME.db (upgrade.sh checks
# it byte for byte; `sha256sum -c` alone is not enough, it checks whatever file the line names).
# The checksum file is also the claim on the name: two runs for the same name cannot both get past
# step 1, and an existing backup or checksum file is never replaced.
#
# COMMITTED IS NOT THE SAME AS REPORTED. The commit is the creation of the NAME.db link. The script
# only learns that it happened one statement later, and reports it last of all, by printing the path
# (the acknowledgment). A signal (SIGTERM, SIGINT, SIGHUP) can arrive in between, or after the
# commit and before the path is printed. The clean-up therefore decides from the FILES, never from
# the script's memory: if NAME.db is a hard link to the very .part file this run made, and the
# checksum file is this run's own and is the one line above for it, and NAME.db hashes to that
# digest, the complete pair is KEPT. The run still ends non-zero, prints nothing on standard output
# and says on standard error that the pair was kept, so a caller (upgrade.sh) does not use a path it
# was never given; a person may use the pair, which is verifiable like any other. Otherwise the
# clean-up removes the names this run made, and only those (below), the database file first.
#
# OWNERSHIP IS IDENTITY. A file in the backup directory is this run's only if it is the very file
# (same device and inode, from stat) that this run created, directly or as a hard link. The name is
# never enough: a file another publisher put under the same name is not touched, and neither is
# anything a run that lost the name finds there.
#
# SIGKILL and a power cut run nothing, so they can leave temporary files even when the committed
# pair is valid: NAME.db.part and NAME.db.sha256.part are then hard links of the same two files (the
# data is not stored twice), they block that name until they are deleted by hand, and deleting them
# does not touch the backup. Killed before the commit, what can be left is NAME.db.part, a
# NAME.db.sha256.part and a NAME.db.sha256 with no NAME.db: not a backup.
#
# Standard output is exactly one line, the path of NAME.db, and only when the pair is committed
# AND reported; everything else goes to standard error. Exit status: 0 published, verified and
# reported; 1 anything else (normally nothing is published and this run's own files are gone; the
# one exception is the kept pair above, which a signal ends with the signal's own status); 2 usage.
# It works with the web service running or stopped.
set -euo pipefail
umask 077

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE=$HERE/compose.yaml
DBTOOL=/opt/house-helper/ops/dbtool.py
MAX_BYTES=$((1 << 30))   # dbtool.MAX_DATABASE_BYTES: a backup larger than this cannot be restored
# The one rule for the file name of a backup (also in upgrade.sh and RUNBOOK section 10): ASCII letters,
# digits, _ . - only, not starting with . or -, ending in .db. Checked in the C locale so that no locale
# can widen a range or count a multi-byte character as one.
NAME_PATTERN='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db$'
name_ok() { local LC_ALL=C; [[ $1 =~ $NAME_PATTERN ]]; }

say() { printf '%s\n' "$*" >&2; }
fail() { say "backup.sh: $*"; exit 1; }
usage() { say "usage: bash deploy/backup.sh --tag IMAGE_TAG [--dir BACKUP_DIR] [--label TEXT | --name FILE.db]"; exit 2; }

tag= dir=${HOME:-/nonexistent}/backups/house-helper label= name=
while [ $# -gt 0 ]; do
    case $1 in
        --tag) [ $# -ge 2 ] || usage; tag=$2; shift 2 ;;
        --dir) [ $# -ge 2 ] || usage; dir=$2; shift 2 ;;
        --label) [ $# -ge 2 ] || usage; label=$2; shift 2 ;;
        --name) [ $# -ge 2 ] || usage; name=$2; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$tag" ] || usage
[[ $tag =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || fail "the image tag has characters an image tag cannot have."
: "${label:=$tag}"
[[ $label =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || fail "the label may contain only letters, digits, _ . -"
if [ -n "$name" ]; then
    name_ok "$name" || fail "--name may contain only letters, digits, _ . - (not first) and must end in .db"
fi

# --- what this run has made, and the clean-up that knows it --------------------------------------

# Ownership is identity. file_id prints "device:inode"; this run records the id of each private .part
# file when it creates it, and a name in the backup directory is "mine" only if it is a regular file
# (not a symbolic link) with a recorded id: the .part itself, or the hard link made from it.
file_id() { stat -c '%d:%i' -- "$1" 2> /dev/null; }
is_mine() { local id; [ -n "$2" ] && [ -f "$1" ] && [ ! -L "$1" ] && id=$(file_id "$1") && [ "$id" = "$2" ]; }

final= part= hash_final= hash_part= base= report= digest= part_id= hash_part_id=
committed=0      # the NAME.db link was made (set the statement after it; the clean-up also checks the files)
acknowledged=0   # the path was printed: the caller has been told

# The one thing the clean-up asks of the files: is the pair this run published complete and its own?
pair_is_complete() {
    [ -n "$digest" ] && is_mine "$final" "$part_id" && is_mine "$hash_final" "$hash_part_id" \
        && printf '%s  %s\n' "$digest" "$base" | cmp -s -- - "$hash_final" \
        && [ "$(sha256sum < "$final" | cut -d' ' -f1)" = "$digest" ]
}

cleanup() {
    set +e                   # a clean-up that stops at its first failure is not one; and a trap must not change the exit status
    trap '' INT TERM HUP     # a second signal must not cut it short
    if [ "$committed" != 1 ] && pair_is_complete; then committed=1; fi   # the signal came between the link and the note of it
    if [ "$committed" != 1 ]; then
        # Not committed: take back what this run made, the database file first so that it is never visible without its checksum.
        if is_mine "$final" "$part_id"; then rm -f -- "$final"; fi
        if is_mine "$hash_final" "$hash_part_id"; then rm -f -- "$hash_final"; fi
    elif [ "$acknowledged" != 1 ]; then
        say "backup.sh: stopped after the backup was published, before it was reported: $final and $hash_final are a complete, verified pair and were kept."
    fi
    if is_mine "$part" "$part_id"; then rm -f -- "$part"; fi          # the private names: the data stays in the links above
    if is_mine "$hash_part" "$hash_part_id"; then rm -f -- "$hash_part"; fi
    if [ -n "$report" ]; then rm -f -- "$report"; fi
}
trap cleanup EXIT

# --- everything that can be checked before anything is written ------------------------------------

command -v cmp > /dev/null && command -v stat > /dev/null \
    || fail "cmp (diffutils) and stat (coreutils) are required: the checksum file is compared exactly and ownership is checked by inode."
docker image inspect "house-helper:$tag" > /dev/null 2>&1 \
    || fail "there is no image house-helper:$tag on this host (docker image ls house-helper)."

if [ ! -d "$dir" ]; then
    mkdir -p -m 700 -- "$dir" || fail "cannot create the backup directory $dir."
fi
[ -w "$dir" ] && [ -x "$dir" ] || fail "the backup directory $dir is not writable by this user."

stamp=$(date -u +%Y%m%dT%H%M%SZ)
unique=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
base=${name:-house-helper-$stamp-$unique-$label.db}
name_ok "$base" || fail "the file name $base is not one this kit accepts (letters, digits, _ . - and ending in .db)."
final=$dir/$base
part=$final.part
hash_final=$final.sha256
hash_part=$hash_final.part
report=$(mktemp "${TMPDIR:-/tmp}/hh-backup-report.XXXXXX")
for path in "$final" "$part" "$hash_final" "$hash_part"; do
    if [ -e "$path" ] || [ -L "$path" ]; then fail "$path already exists; nothing was written."; fi
done

# --- the backup ----------------------------------------------------------------------------------

# Exclusive creation: under noclobber `>` is open(O_CREAT|O_EXCL), so a file that is already
# there (or appears now) stops the script instead of being overwritten.
( set -C; : > "$part" ) 2> /dev/null || fail "could not create $part exclusively."
part_id=$(file_id "$part")   # from here on this file is this run's, by identity

say "backing up with house-helper:$tag ..."
if ! HOUSE_HELPER_IMAGE_TAG=$tag docker compose -f "$COMPOSE_FILE" --profile ops \
        run --rm -T house-helper-ops backup --output - > "$part" 2> "$report"; then
    sed -n '/^\(ok\|refused\|error\|usage\) \[/p' "$report" >&2 || true
    fail "the backup command failed; nothing was published."
fi

bytes=$(wc -c < "$part" | tr -d ' ')
[ "$bytes" -gt 0 ] || fail "the backup is empty; nothing was published."
[ "$bytes" -le "$MAX_BYTES" ] || fail "the backup is larger than the $MAX_BYTES bytes this kit restores."
[ "$(head -c 16 "$part" | od -An -tx1 | tr -d ' \n')" = "53514c69746520666f726d6174203300" ] \
    || fail "the backup does not start like a SQLite database; nothing was published."

digest=$(sha256sum < "$part" | cut -d' ' -f1)   # from stdin: with a file name, a '\' in it would be prefixed to the hash
reported=$(grep -o 'sha256 [0-9a-f]\{64\}' "$report" | cut -d' ' -f2 || true)
[ "$(printf '%s\n' "$reported" | grep -c .)" = 1 ] || fail "could not read the checksum the backup tool reported."
[ "$digest" = "$reported" ] \
    || fail "the file on the host ($digest) is not what the tool wrote ($reported): the stream was damaged; nothing was published."

# --- verification, by a container that has no volume of the data and no network -----------------

say "verifying $bytes bytes (integrity, foreign keys, this application's schema, revision = head) ..."
if ! docker run --rm -i --init --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --log-driver none \
        --tmpfs /tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001 -v /staging \
        "house-helper:$tag" python "$DBTOOL" verify --input - --require-head < "$part" >&2; then
    fail "verification FAILED; nothing was published."
fi

# --- publication: the checksum first, the backup last (its appearance is the commit) --------------

# Nothing public exists yet. First the last look: the file is still the one that was hashed and
# verified, and the checksum file, built under its own .part name, says so.
[ "$(sha256sum < "$part" | cut -d' ' -f1)" = "$digest" ] \
    || fail "the backup file changed after it was verified; nothing was published."
( set -C; : > "$hash_part" ) 2> /dev/null || fail "could not create $hash_part exclusively."
hash_part_id=$(file_id "$hash_part")
printf '%s  %s\n' "$digest" "$base" > "$hash_part" || fail "could not write $hash_part; nothing was published."
printf '%s  %s\n' "$digest" "$base" | cmp -s -- - "$hash_part" \
    || fail "the checksum file is not exactly the one line that was written; nothing was published."
sync -- "$part" "$hash_part" 2> /dev/null || sync || true

# 1. The checksum file. ln fails if the name exists, so another backup's checksum file, or one that
#    appeared meanwhile, is never replaced; and holding this name is what keeps a second run for
#    the same name from going on.
ln -- "$hash_part" "$hash_final" 2> /dev/null \
    || fail "$hash_final already exists or cannot be created; it was not replaced; nothing was published."
sync -- "$dir" 2> /dev/null || sync || true   # the checksum file's name is durable before the backup's can be

# 2. The backup itself: the commit. If this fails, cleanup removes the checksum file made in step 1.
#    A signal can arrive after the link exists and before the next line: cleanup then finds the pair
#    on disk, complete and this run's own, and keeps it (see the header).
ln -- "$part" "$final" 2> /dev/null \
    || fail "$final could not be published (it appeared meanwhile, or this file system has no hard links); its checksum file was removed again; nothing was published."
committed=1
sync -- "$dir" 2> /dev/null || sync || true
say "published $final ($bytes bytes, sha256 $digest), verified."
printf '%s\n' "$final"
acknowledged=1

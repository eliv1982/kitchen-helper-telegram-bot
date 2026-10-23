#!/usr/bin/env bash
# Create the application's environment file (the one that will hold the Telegram client secret
# and the OpenAI key) from the template, and refuse to touch one that already exists.
#
#   bash deploy/new-env-file.sh /path/to/house-helper.env
#
#   * Refuses (exit 3) if the path exists in any form, including a symbolic link or a dangling
#     one. The file is created with an exclusive open (bash's noclobber turns `>` into
#     open(O_CREAT|O_EXCL)), so it cannot overwrite a file that appears while it runs either.
#   * Mode 0600 from the first byte (the umask is set before the file exists), owned by whoever
#     runs it. A missing parent directory is created 0700; an existing one is left as it is.
#   * It copies the template, which holds no value, and prints only the path and the mode:
#     never the content. The values are filled in by hand afterwards.
#   * To CHANGE a value in a file that exists, use the update procedure in deploy/RUNBOOK.md
#     ("Changing a secret"); this script will not do it.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TEMPLATE=$HERE/house-helper.env.example

if [ $# -ne 1 ] || [ -z "$1" ]; then
    echo "usage: bash deploy/new-env-file.sh /path/to/house-helper.env" >&2
    exit 2
fi
destination=$1
[ -f "$TEMPLATE" ] || { echo "new-env-file.sh: the template $TEMPLATE is missing." >&2; exit 1; }

if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "refused: $destination already exists and was not touched." >&2
    echo "To change a value in it, follow \"Changing a secret\" in deploy/RUNBOOK.md." >&2
    exit 3
fi

umask 077
directory=$(dirname -- "$destination")
if [ ! -d "$directory" ]; then
    mkdir -p -m 700 -- "$directory"
fi

# Exclusive creation: if the file appeared since the check above, this fails and changes nothing.
if ! ( set -C; cat -- "$TEMPLATE" > "$destination" ) 2> /dev/null; then
    echo "refused: could not create $destination exclusively (it appeared, or the directory is not writable)." >&2
    exit 3
fi
chmod 600 -- "$destination"
echo "created $destination (mode $(stat -c '%a' -- "$destination" 2> /dev/null || echo 600), no value in it yet): fill it in by hand."

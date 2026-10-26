# House Helper web application: deployment runbook (R4C-D1)

**Status of this document: the kit was released as repository-only (R4C-D1) and has since been used for the
technical deployment of 2026-10-24** (commit `6370bf7`, image `house-helper:6370bf78980e`, database at
`0006_oidc_state_invitation`; sign-in and OpenAI disabled, no real users; see `docs/STATUS.md`, "Current deployment
state"). Section 14 (scheduled cleanup, R4C-D2) was added after the kit's release: its code is RELEASED in Git as
`489ff30` (independent audit passed, owner accepted, CI green). Its systemd timer is installed and active on the VPS;
its first calendar-triggered execution, on 2026-10-25, was verified and accepted by the owner (owner-reported: it found
no expired record and deleted nothing). Failure reporting, the encrypted off-host backup and a restore rehearsal are
separate and not yet completed.
The commands were exercised locally on disposable resources before the deployment (Docker 29.8, Compose 5.6,
Traefik 3.6.25 on loopback); see section 13 for what that does and does not prove. This
revision answers an independent audit of the first version (restore while the database is in
use, scratch space smaller than the database, a migration without a verified backup, and six
further findings); sections 2, 9, 10 and 12 changed most. Every step
marked **[OWNER]** changes something outside the repository (DNS, the VPS, BotFather, real
users, a real database, money) and needs the owner's explicit approval first. Do not run them
on your own judgement.

Target: `https://house.elivcloud.org`, on the existing Netherlands VPS (`eliv-server`), behind
the Traefik that already runs there. No nginx, Caddy, second Traefik, certificate manager,
VPS or domain is introduced, and the shared Traefik and the other applications are not touched.

## 1. What is in the kit

| File | Purpose |
|---|---|
| `Dockerfile` | the web application image (Python 3.12, non-root, no credentials) |
| `.dockerignore` | an **allowlist** build context: the real `.env`, databases and tests can never enter an image |
| `deploy/compose.yaml` | the `house-helper` service (Traefik labels) and the `house-helper-ops` one-shot service |
| `deploy/house-helper.env.example` | the environment template: names only, no value |
| `deploy/logging.json` | uvicorn's log configuration (application INFO lines become visible) |
| `deploy/ops/dbtool.py` | the operator tool: the maintenance lock, start-up, check, backup, verify and restore for the SQLite database |
| `deploy/backup.sh` | the one way a backup is taken: made, size- and checksum-checked, verified by a disposable container, then published as a pair (`.db.sha256` first, `.db` last = the commit) without overwriting anything |
| `deploy/upgrade.sh` | the one way a version is upgraded: no schema change unless the backup above succeeded first |
| `deploy/new-env-file.sh` | creates the secret environment file; refuses to touch one that exists |
| `deploy/.gitattributes` | keeps the scripts' and units' line endings LF on a Windows checkout |
| `deploy/systemd/house-helper-purge.service`, `.timer`, `.env.example` | **R4C-D2, opt-in, installed by nothing:** the systemd oneshot service and daily timer that run the cleanup (section 14) |
| `deploy/ops/dbtool.py sanitize`, `deploy/offsite_backup.py`, `deploy/systemd/house-helper-offsite-backup.service`, `.timer`, `house-helper-offsite.env.example` | **opt-in, installed by nothing:** the encrypted off-host backup (section 15): a snapshot rewritten without deleted rows, packed, encrypted with `age` for a public key, uploaded with `rclone`, read back, and rotated (7 daily + 4 weekly) |
| `deploy/RUNBOOK.md` | this document |

## 2. Design

- **Image.** `python:3.12-slim-trixie`, pinned by digest; dependencies exactly as
  `requirements.txt` pins them (the set CI tests); only `house_helper/`, `alembic.ini` and
  `deploy/` are copied. Runs as uid/gid 10001. The bot (`bot.py` and its root modules) is not
  in the image: the web application does not import it (enforced by `tests/test_web_boundary.py`).
- **One worker.** The chat turn runner and the database work queue are in-process and SQLite has
  one writer. Do not add `--workers` or run two replicas.
- **Start-up.** `dbtool.py serve`: it takes the shared maintenance lock (below), checks the schema
  (read only), and then **becomes** uvicorn (`exec`: the same process, the new program), which
  keeps the lock until it ends. If the database is missing,
  behind the image's Alembic head, or ahead of it, the container **stops with a message** and
  Docker restarts it at increasing intervals until an operator fixes the cause. Nothing migrates
  at start-up. The application itself would start against such a database and fail later.
  While a container is not healthy (starting, restarting, failing its health check) Traefik
  drops it and answers **404** for the host (observed with Traefik 3.6.25), so a 404 on the
  public address means "look at `dc ps` and `dc logs house-helper`" first.
- **Filesystem.** Read-only root; the web service writes only `/data` (the named volume
  `house-helper-data`) and `/tmp` (64 MB memory, `noexec`). SQLite runs in WAL mode, so the
  *directory* `/data` must be writable, not only the file. A new named volume takes the
  ownership of the image's `/data` (10001, mode 0700); a **host bind mount** would need
  `chown 10001:10001` and is not used. The operator service additionally has **`/staging`**, a
  disk-backed volume of its own (below).
- **Scratch space for database copies.** Backup, verify and restore stage a full copy of the
  database, and a database may be up to **1 GiB** (`MAX_DATABASE_BYTES`, the limit every command
  enforces, so that a backup the tool writes is one it can restore). That cannot live in the 64 MB
  `/tmp`, and a 1 GiB memory-backed `/tmp` would not fit the 256 MB container. So the ops service
  mounts an **anonymous Docker volume at `/staging`**: on disk, owned by uid 10001 (copied from the
  image), visible to no other container, new for every run and deleted with the container by
  `--rm` (verified: the count of dangling volumes is unchanged after runs). `HOUSE_HELPER_STAGING_DIR`
  (set to `/staging` by the Dockerfile and again, explicitly, in the compose file) names it; SQLite's
  own temporary files are pointed there too. Inside it each command makes a private `0700`
  directory and removes it on success, failure and interrupt. The tool refuses a staging directory
  that is relative, missing, a symbolic link, or the data directory or inside it, and **checks the free
  space before it copies anything**: the database's size (or the full 1 GiB when the size cannot be
  known, as for a stream on stdin) plus 64 MiB; a restore needs the same again, once, on the data
  volume for the file it installs. Not enough: it stops with `staging_space` / `data_dir_space`
  and has changed nothing. On one host disk that is about **2.2 GiB free** to restore a 1 GiB
  database (about 1.1 GiB each for scratch and for the installed file), on top of the old database,
  which is moved aside and kept in `pre-restore-*`, not copied. With a plain
  `docker run` (the disposable verify) add `-v /staging`; without it the tool refuses rather than
  falling back to a small tmpfs. A container killed mid-copy (SIGKILL, with the Compose client alive)
  takes its partial scratch with it (verified: 127 MB gone, no volume left). If the **client** dies too
  (a dropped ssh session; not tested) a stopped one-off container can remain with its `/staging`: list
  `docker ps -a --filter label=com.docker.compose.project=house-helper --filter label=com.docker.compose.oneoff=True`
  and remove only those, with `docker rm -v <id>`. Never run `docker volume prune` on this host: it deletes
  the unused volumes of the other applications too.
- **Network.** The only network is the external `n8n_n8n_network` (the one Traefik uses).
  No port is published. The service is named `house-helper` on that flat network, so avoid
  reusing that name elsewhere. Any other container on that network can still reach it directly
  on 8000 (bypassing the rate limits, not the application's authentication).
- **Traefik** (labels only; section 6). **Logs:** Docker `json-file`, 10 MB x 5; uvicorn's access
  log is off (it would write the OIDC callback's query string) and the application's own INFO
  lines are on. After `docker compose stop`, exit status 143 is normal: uvicorn re-raises SIGTERM
  once its clean shutdown has finished (the log ends with "Finished server process").
- **Limits:** 1 CPU, 512 MB, 256 processes. `stop_grace_period: 75s`: uvicorn drains for 30 s,
  then the application waits up to 20 s for chat turns and 10 s for the database.
- **`house-helper-ops`** (profile `ops`, not started by `up`): same image and volume, **no network,
  no environment file, no log driver**. Migrations, backups, the enrollment commands and the
  cleanup of expired data (section 14) run here. The log driver is off because `enroll-user` prints a secret and a backup is streamed on
  stdout; Docker's log files must hold neither. Its **entrypoint is the operator tool**, so what
  you type after the service name is a `dbtool` command: `check-config`, `check-schema`, `backup`,
  `verify`, `check-idle`, `restore`, or `exec --shared|--exclusive -- <program>` (an enrollment,
  a migration). There is no way to run a raw `alembic` or `python` here by accident, and every
  command that touches the database therefore goes through the lock below.

### The maintenance lock

Restoring replaces the database file under whatever has it open, and anything running at that
moment writes into a file that is no longer the database (the audit reproduced exactly that: a
restore "succeeded" while another container was mid-transaction, and the writer's later commit
went into the moved-aside copy). Two layers prevent it.

**Layer 1: a lock on the data volume** (`/data/.house-helper.lock`, `flock(2)`, never deleted).

| Holder | Mode | For how long |
|---|---|---|
| the web service | shared | as long as **uvicorn** runs: `serve` takes it, then `exec`s uvicorn, which inherits the descriptor; the kernel drops it when the process ends, so it cannot go stale (verified in a real container: it is held by the uvicorn process, not by the start-up check) |
| `exec --shared` (enrollment commands) | shared | while the command runs; runs beside the web service, as before |
| `backup` | shared | while it runs; **works while the web service runs** (verified) |
| `exec --exclusive` (the migration) and `restore`, `check-idle` | exclusive | while they run; **refused** (`maintenance_lock_busy`, exit 3) if the web service or any other command holds the lock, and nothing can start while they hold it, including the web service restarting by itself |

**Layer 2: SQLite itself**, in `restore` and `check-idle`, for a process that never asked for the
lock (a `docker run` with another entrypoint, the `sqlite3` shell, a host process). The tool opens the
old database and takes SQLite's write lock: if another connection holds it (an uncommitted write)
the restore is refused with `database_busy` and nothing is moved. After closing, it looks for the
`-wal`/`-shm` files that SQLite deletes when the *last* connection closes: if they remain, another
process still has the database open (`database_in_use`). Opening also recovers the log of a crashed
writer into the file, so what is set aside is complete (verified). A file that is not a SQLite database
is not opened.

**What this does not cover, so that no stronger claim is made.** The lock is cooperative. It stops
everything started through the ops service or the web service, and nothing else: it does not protect against
a process that deliberately ignores it, such as a `docker run` with the volume and another entrypoint, the
`sqlite3` shell, a host process, or any privileged (root) process. Layer 2 narrows the gap for those; it does
not close it, and how much it narrows depends on the target:

- **Replacing an existing database.** Layer 2 looks at the old database as late as possible, right before
  its files are moved aside. What remains is a narrow race: a process that ignores the lock opens or writes to
  the database after that look and before the rename. A damaged file (no SQLite header) cannot be probed at
  all; there the lock is the only guard.
- **Restoring into an empty target** (no database file when the restore began). There is nothing to probe,
  and the target is not looked at again before the restored file is renamed into place, so the race with a
  process that ignores the lock and *creates* a database there spans the whole staging and validation of the
  input, which takes longer for a larger input. That file is replaced unseen. If its process is still running,
  its `-wal`/`-shm` files stay beside the restored file and SQLite replays them onto it: `restore` reports
  success, yet the database SQLite then reads is not the restored one (reproduced on Linux). The `check-schema`
  that the procedure of section 10 runs straight after a restore refuses it (`schema_not_head`, exit 4);
  do not skip that step.
- Anything that writes to the volume outside Docker (a host process, a bind mount) is outside both layers.

So the requirement stays what the rule below says: **exclusive maintenance access, with every managed service
stopped.** The tool can enforce it only for what goes through the tool.

**The rule, in order:** stop the web service, confirm it is stopped (`dc ps --status running --quiet
house-helper` prints nothing), confirm nothing else uses the database (`hhops check-idle` exits 0), and
only then restore (section 10). If `restore` fails after moving the old files aside it moves them back; if
that fails too, the message names the directory they are in.

## 3. Configuration contract

Names are the application's own (`house_helper/web/settings.py`,
`house_helper/core/provider_settings.py`). `tests/test_deploy_kit.py` fails if the template
invents one.

| Variable | Set by | Required | Meaning |
|---|---|---|---|
| `HOUSE_HELPER_DATABASE_URL` | `compose.yaml` | yes | `sqlite:////data/house_helper.db` (absolute; the tools refuse a relative path) |
| `HOUSE_HELPER_STAGING_DIR` | `Dockerfile` (`/staging`) and the ops service in `compose.yaml` | operator tool only | where `dbtool` puts its scratch copies of the database (section 2); the application does not read it |
| `HOUSE_HELPER_PUBLIC_URL` | `compose.yaml`, from `HOUSE_HELPER_HOST` | yes | `https://house.elivcloud.org`; redirect URI, cookie attributes, allowed Origin all derive from it |
| `HOUSE_HELPER_TELEGRAM_CLIENT_ID` / `_CLIENT_SECRET` | env file | for sign-in | both or neither; from @BotFather Login Widget |
| `HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG` | env file | no | only if BotFather is not RS256 |
| `OPENAI_API_KEY` | env file | for chat | without it a message is answered 503 |
| `OPENAI_MODEL`, `TEMPERATURE`, `MAX_TOKENS`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES` | env file | no | defaults `gpt-4.1-mini`, 0.7, 1000, 15, 1 |
| `OPENAI_REASONING_EFFORT` | env file | for a `gpt-6*` model | unset (the request every earlier model takes: `temperature`, `max_tokens`); `low` for GPT-6 Luna: `reasoning_effort` and `max_completion_tokens` (= `MAX_TOKENS`, reasoning included), no `temperature`; a `gpt-6*` model without it does not start |

Read by Compose only: `HOUSE_HELPER_HOST` (default `house.elivcloud.org`), `HOUSE_HELPER_ENV_FILE`
(default `deploy/house-helper.env`), `HOUSE_HELPER_IMAGE_TAG` (default `local`),
`HOUSE_HELPER_GIT_SHA`, `HOUSE_HELPER_HSTS_SECONDS` (default 86400).

Deliberately absent: `BOT_TOKEN`, and all `HOUSE_HELPER_SMTP_*` / `HOUSE_HELPER_EMAIL_FROM`
(email sign-in stays off: its routes answer 503 and the page draws no email form). The env file
holds secrets: keep it outside the checkout, mode 0600, never paste a value anywhere. A missing
env file makes Compose refuse to start (fail closed). A `$` in a value must be written `$$`.

### Creating the secret file (once) and changing a secret (controlled)

**Create** it with the script, never with a bare `install`/`cp`/`>`, which silently replace a file that
already holds the live secrets:

```bash
bash deploy/new-env-file.sh "$HOUSE_HELPER_ENV_FILE"     # exit 3, file untouched, if the path exists in any form
```

It refuses if the path exists (a symbolic link, even a dangling one, counts), creates the file with an
exclusive open (a file that appears while it runs is not overwritten either), mode 0600 from the first
byte, a missing parent directory 0700, copies only the template (which holds no value) and prints the
path and mode, never the content. Then fill the values in by hand.

**Change** a value in the file that exists, and only this way:

```bash
stat -c '%a %U %n' "$HOUSE_HELPER_ENV_FILE"      # expect: 600 <you> <path>; if not, stop and find out why
nano "$HOUSE_HELPER_ENV_FILE"                     # nano writes no backup file; vim would leave .swp/~ copies of the secrets
stat -c '%a %U %n' "$HOUSE_HELPER_ENV_FILE"      # still 600 <you>
ls -A "$(dirname "$HOUSE_HELPER_ENV_FILE")"      # no ~, .swp or .save copy beside it
dc config -q && echo "compose file valid"        # prints nothing of the values
dc up -d --force-recreate house-helper            # the container reads the file only when it is created
```

No `>` redirect, no `sed -i`, no `cat` of the file; do not paste it, do not run any of this under `tee`
or a screen recorder, and never copy it into the checkout or a backup directory. If the old value leaked,
rotating it at the provider (BotFather, OpenAI) comes first (section 11, *Secrets exposed*).

### Shell helpers

Shell helpers used below (define them once per session, from the repository root):

```bash
cd ~/apps/house_helper                     # the checkout; ~/apps/<repo> is the host's convention
export HOUSE_HELPER_ENV_FILE=$HOME/deploy-overlays/secrets/house-helper.env   # [OWNER: confirm the location]
export HOUSE_HELPER_GIT_SHA=$(git rev-parse HEAD)
export HOUSE_HELPER_IMAGE_TAG=$(git rev-parse --short=12 HEAD)
dc()    { docker compose -f deploy/compose.yaml "$@"; }
hhops() { docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops "$@"; }
```

`hhops` runs a `dbtool` command in the ops service (`hhops check-schema`, `hhops backup --output -`,
`hhops exec --exclusive -- alembic upgrade head`). **The scripts (`backup.sh`, `upgrade.sh`) never read
`HOUSE_HELPER_IMAGE_TAG` or any variable you set earlier**: they take the tags as arguments, so a stale
shell cannot change what they do. They read only what Compose reads (`HOUSE_HELPER_ENV_FILE` and the
other `HOUSE_HELPER_*` names above).

Never `docker compose run` the **web** service: `run` copies its Traefik labels onto a second
container that Traefik would route to. Use `up`, `stop`, `exec`, and `hhops` for the one-shots. Do not
bypass the ops service's entrypoint (`--entrypoint`, or a plain `docker run` with the data volume) for
anything that touches the database: that is exactly what the maintenance lock cannot see.

## 4. Prerequisites on the VPS **[OWNER]**

Verified read-only by R4C-0 (2026-10-19) and to be re-confirmed immediately before go-live:
Ubuntu 24.04, Docker 29 / Compose 5, Traefik v3.6 owning 80/443 with entrypoints `web` and
`websecure`, a global HTTP-to-HTTPS redirect, the resolver `letsencrypt` (HTTP-01), the external
network `n8n_n8n_network`, UTC with NTP, 4 vCPU / 7.9 GB.

1. **DNS.** An **A** record `house.elivcloud.org` -> `72.56.124.172` (Namecheap zone; the name is
   `NXDOMAIN` as of R4C-0). **No AAAA** unless IPv6 is deliberately served: Let's Encrypt and
   browsers would try it. The local resolver on the owner's PC returns fake IPs; check with DoH:
   `curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=house.elivcloud.org&type=A'`
   (and `type=AAAA`, which must be empty). **Do not start the container before the record resolves:**
   Traefik requests the certificate as soon as it sees the labels, failed HTTP-01 attempts count
   against Let's Encrypt's per-host limit, and the shared Traefik offers no staging resolver.
2. **No log sink of the shared Traefik may ever receive a query string.** With the access log on,
   the request path in the log contains the whole query, so the OIDC callback's `code` and `state`
   would be written (measured, section 4.1). Labels cannot change this (static, shared
   configuration). R4C-0 saw no `--accesslog` flag, but that did not look at a static file, the
   environment, a file or OTLP sink. Do the **read-only preflight in section 4.1** before the first real
   login. Editing the shared Traefik is a separate owner decision and is not part of this kit.
3. **Outbound HTTPS** from containers on that network to `oauth.telegram.org` and `api.openai.com`.
4. **A dedicated OpenAI project with an enforced hard spend limit** (section 4.2). A spend *alert*
   does not stop anything, and nothing in the application caps cost beyond the per-address limit below.

### 4.1 Traefik access-log preflight **[REQUIRES LIVE; read-only]**

Purpose: show that no sink of the shared Traefik can receive the `code` and `state` of the Telegram
callback. Nothing below edits, restarts or reconfigures Traefik: it inspects, and sends one harmless
request. `T` is the Traefik container (R4C-0 identified it).

**Why every source must be read.** Traefik takes its static configuration from **one** source, in this
order: a configuration file if one is found, otherwise command-line flags, otherwise `TRAEFIK_*`
environment variables. Measured with Traefik 3.6.25 on a throwaway instance:

| Case | Result |
|---|---|
| a file at `/etc/traefik/traefik.yml`, `/etc/traefik/traefik.yaml`, `/traefik.yml` (the working directory) or `$HOME/.config/traefik.yml`, or named by `--configFile` | it is the configuration (also documented: `$XDG_CONFIG_HOME/`, not tested) |
| a file **without** an access log, plus the flag `--accesslog=true` | **no access log**: the flag was ignored |
| a file **with** an access log, plus `TRAEFIK_ACCESSLOG_FIELDS_NAMES_RequestPath=drop` | the query **was logged**: the variable was ignored |
| the official image | contains no static file; one exists only if somebody mounted it |
| `--accesslog` (or `TRAEFIK_ACCESSLOG=true`) with the default `common` format | the callback's `code` and `state` are written |
| `--accesslog.fields.names.RequestPath=drop`, `common` **and** `json` format; or `TRAEFIK_ACCESSLOG_FIELDS_NAMES_RequestPath=drop` | the line is written without the path: nothing leaks |
| `--accesslog.fields.queryparameters.defaultmode=drop` | the query is removed and the path kept: nothing leaks |
| `RequestPath=redact`, or `queryparameters.defaultmode=redact` | **still leaks** (do not use) |
| `--accesslog.fields.headers.defaultmode=keep` | the `Referer` and `Cookie` headers are logged (the default is `drop`) |
| `--accesslog.filepath=...` | the line goes to that file and **not** to `docker logs` |

So reading the command line proves nothing while a file exists, and an environment variable proves
nothing while a file exists. Inspect all of the following, then confirm with the canary.

```bash
T=<traefik container name>
# 1. Command-line arguments (and an explicit --configFile)
docker inspect "$T" --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'
# 2. Environment: print the NAMES only first (values can be credentials, for example a DNS-challenge token)
docker inspect "$T" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed 's/=.*//' | grep -i '^TRAEFIK_'
#    then read the VALUES of the logging ones only: ACCESSLOG*, LOG*, TRACING*, METRICS*
docker inspect "$T" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^TRAEFIK_(ACCESSLOG|LOG|TRACING|METRICS)'
#    (an OTLP header variable can hold a token: do not paste its value anywhere)
# 3. Mounted files: every mount, then the static configuration among them
docker inspect "$T" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
# 4. The default locations INSIDE the container (read only). A file found here is the configuration
#    and the flags and variables above are then ignored.
docker exec "$T" sh -c 'pwd; echo "HOME=$HOME"; ls -l /etc/traefik/ ./traefik.* "$HOME"/.config/traefik.* 2>&1'
#    Read only the logging parts of whichever file applies, never the whole file (it holds resolver settings):
docker exec "$T" sh -c 'grep -n -i -A12 "^accessLog\|^\[accessLog\|^log:\|^tracing\|^metrics" /etc/traefik/traefik.yml' 2>&1
```

From the effective configuration write down every **sink** that is enabled:

1. *standard output*: `docker logs "$T"` (Docker's own `json-file` copy is what that command reads);
2. *a file*: `accessLog.filePath` (and its rotated copies) on the host or in the container;
3. *OTLP* or another exporter: `accessLog.otlp`, and also `log.otlp`, `tracing` and `metrics.otlp`, each
   with its endpoint. The place that **receives** it (a collector, a vendor) is a sink too, and its own
   storage must be inspected;
4. the general log: `log.level` (the default is `ERROR`). A `DEBUG` log is a request-detail sink; not tested.

**Decision.** Access log off and no OTLP/tracing sink: the canary below must still be clean. Access
log on: it must carry `RequestPath=drop` **or** `queryparameters.defaultmode=drop` (the canary must
confirm it for the format actually in use), `fields.headers.defaultmode` must not be `keep`, and
`redact` is not a suppression. Anything else: stop, tell the owner; do not hold the first login.

**The canary** (harmless; covers every sink you listed). Two kinds of token, both invented for the
occasion and never real. `Q` goes where `code` and `state` go: it must appear **nowhere**. `M` is an
unusual HTTP method (`HHM...`): an access log that is on writes the method even when the path or the
query is suppressed, so `M` is the **control**. It must appear in every sink that is an access log, which
proves that sink would have shown `Q` if it leaked (a check that cannot fail proves nothing). Verified
against Traefik 3.6.25: `M` is present and `Q` absent with `RequestPath=drop` (both formats) and with
`queryparameters.defaultmode=drop`; both are present with no suppression. `IP` is the VPS address.

```bash
IP=72.56.124.172
Q=hhq$(date -u +%s)$RANDOM; M=HHM$RANDOM$RANDOM
# Before go-live: an unrouted host name. Traefik answers 404 and an active access log still writes the request.
curl -sk -o /dev/null -w '%{http_code}\n' -X "$M" --resolve canary.house.invalid:443:$IP \
  "https://canary.house.invalid/auth/telegram/callback?code=$Q-code&state=$Q-state" \
  -H "Referer: https://oauth.telegram.org/auth?x=$Q-referer" -H "Cookie: probe=$Q-cookie"
# After go-live, once: the real host. The application answers 4xx (unknown state) and logs the reason only.
curl -sk -o /dev/null -w '%{http_code}\n' "https://house.elivcloud.org/auth/telegram/callback?code=$Q-code2&state=$Q-state2"
sleep 10                                           # a buffered access log (bufferingsize) writes late
for sink in 'docker logs "$T" 2>&1' 'cat <each access-log file and its rotated copies>' 'dc logs house-helper 2>&1'; do
  echo "$sink -> control $(eval "$sink" | grep -c "$M"), leaked $(eval "$sink" | grep -c "$Q")"
done
#   OTLP and any other exporter: look in the receiver's own output or storage for "$Q" and "$M" in the same way
```

Result: `leaked` is `0` in **every** sink, and `control` is at least 1 in every sink that is an access
log (stdout, a file, an OTLP receiver). A non-zero `leaked` anywhere is a failure: stop and tell the
owner. A sink that should be an access log but shows `control` 0 is not receiving it (or the
configuration you read is not the one in force; look again at step 4), so its `leaked` 0 proves nothing.
The application's own log (`dc logs`) is not an access log: expect control 0 and leaked 0 there. Record
the date, the Traefik version, the effective source and the result. Repeat after any change to Traefik.

### 4.2 OpenAI spend limits **[OWNER]**

The assistant's cost is the one expense the application does not bound. Per the current OpenAI
documentation (<https://developers.openai.com/api/docs/guides/spend-limits>), two different things
exist and only one of them protects the budget:

| | What it does |
|---|---|
| **Spend alert** | sends a notification at a threshold; **it does not stop API traffic** ("Spend alerts do not enforce a cap") |
| **Hard spend limit** (project or organization) | **rejects** further requests with HTTP 429 (`project_spend_limit_exceeded` / `organization_spend_limit_exceeded`) once the limit is reached; applies to API traffic billed to that project (or all projects); enforcement is **not instantaneous**, so recorded spend can slightly exceed the amount while the state propagates |

Required for House Helper: a **dedicated OpenAI project** for this application only, with a **monthly
spend limit and the switch `Enforce a hard limit` turned ON** (Project settings -> Limits -> Spend ->
*Edit spend limit* -> enter the monthly amount -> turn on *Enforce a hard limit* -> Save), and **additional
alerts at lower thresholds** as an early warning (they stay active beside the hard limit). Setting an
amount without turning the switch on gives alerts only. The amounts are the owner's decision; this
document sets none, and the repository configures nothing at OpenAI. The limit resets monthly, or earlier if the
owner raises it. Hard limits can interrupt service: when it is reached the chat fails until then, and what
a user sees in that case has not been tested (section 8.6). Put the project's key, and no other, in the
environment file.

## 5. First install **[OWNER: each phase]**

```bash
git fetch && git checkout <approved commit> && git rev-parse HEAD     # must print the approved SHA
bash deploy/new-env-file.sh "$HOUSE_HELPER_ENV_FILE"   # refuses if it exists; then fill it in by hand (section 3)
dc config -q && echo "compose file valid"        # prints nothing secret; a missing env file fails here
dc build                                         # the image, from this checkout
hhops check-config                               # URL absolute, /data writable by uid 10001
hhops exec --exclusive -- alembic upgrade head   # 0001 ... 0006_oidc_state_invitation; creates the database
hhops check-schema                               # "the database schema is at revision 0006_oidc_state_invitation"
```

DNS (section 4) must resolve now. BotFather (section 8, step 2) does not need the site to be live,
so register the redirect URI and put the Client ID/Secret in the env file **before** the first
start if you can; otherwise start without them (Telegram sign-in is then off, the page says so)
and recreate the container once they are in. Then:

```bash
dc up -d house-helper
dc ps                                            # State: running, Health: healthy (about 10 s)
dc logs --no-log-prefix house-helper | tail -n 8
```

Expect `Web application started (database backend: sqlite, Telegram login: on, email login: off,
assistant: on)`. Migrations, `check-schema` and `up` are separate commands on purpose: nothing
can start serving between them. A migration holds the exclusive maintenance lock, so it is refused
while the web service or an enrollment command is running, and nothing can start during it.

## 6. Traefik: what the labels do

Four routers on `websecure` (TLS, resolver `letsencrypt`), with **explicit priorities**:

| Priority | Match | Limit (per client address, token bucket) |
|---|---|---|
| 30 | `/auth/telegram/login`, `/auth/invite/start`, `/auth/email/*` (they begin a sign-in) | 10 per minute, burst 5 |
| 20 | the rest of `/auth/` (callback, invitation page, `/auth/me`, logout) | 60 per minute, burst 30 |
| 20 | `/api/` (each message is a paid provider call) | 60 per minute, burst 30 |
| 10 | everything else (page, `/static/`, `/healthz`) | none |

All four add `Strict-Transport-Security` (`max-age` 86400 to start; **raise
`HOUSE_HELPER_HSTS_SECONDS` to 31536000 once HTTPS has been proven in real browsers**, because
a browser keeps the commitment), `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`.
The application's own CSP, `Referrer-Policy` (which differs by route) and cross-origin headers
are **not** replaced: no CSP or Referrer-Policy is set at the proxy. A 429 carries the headers too.
The rate-limit key is the connection address (Traefik is the edge); a forged `X-Forwarded-For`
does not change it. Behind a CDN this would need `sourceCriterion`. A client that exceeds a bucket
gets 429 with `Retry-After`; tune the numbers in `deploy/compose.yaml` if real use needs it.
Not included: a global cap on concurrent provider calls and a request-body size limit (section 12).

## 7. Enrolling the two users **[OWNER]**

Onboarding is closed; the only way in is an invitation an operator issues. Each person links
their own Telegram account once. Do this only after Telegram sign-in works (section 8, steps 1-3).

```bash
hhops exec --shared -- python -m house_helper.admin enroll-user --display-name "<name>"      # twice, once per person
hhops exec --shared -- python -m house_helper.admin enrollment-status                       # users, linked or not, invitation state
hhops exec --shared -- python -m house_helper.admin reissue-invitation --user-id <id>       # a new code if one expired
hhops exec --shared -- python -m house_helper.admin revoke-invitation  --user-id <id>
```

The output ends with `invitation code: <id>.<secret>`, **shown once and not stored** (only a
hash is). The link to give is `https://house.elivcloud.org/auth/invite#<id>.<secret>` (the part
after `#` is never sent to the server, so it is in no access log, proxy or application).

Safe delivery:
- Issue the invitation **immediately before** handing it over: it is valid for **2 hours** and
  works **once**. Issue and deliver one person at a time.
- Hand it over to that person only: in person, or in a private Telegram message from the owner.
  A pasted link remains in the message history and the clipboard until used, so treat it as a
  password until the person has signed in; delete the message afterwards.
- Run the command in a terminal whose scrollback you control; clear it afterwards. Do not run it
  under `tee`, a CI job, a screen recorder, or anywhere output is kept. `hhops` keeps it out of
  Docker's log files because that service has no log driver (verified, with a control showing that
  a container on the default driver *does* keep its output there). Use `hhops`, not another container.
- The person opens it in a **normal browser** (Safari/Chrome), not a messenger's in-app browser:
  the return from Telegram may land in a different browser and the login cannot finish.
- The limit of two users is a policy of `enroll-user`, not of the database.

## 8. Live verification **[REQUIRES LIVE ENVIRONMENT]**

1. **TLS.** `curl -sSI https://house.elivcloud.org/healthz` -> `200` with `strict-transport-security`;
   `echo | openssl s_client -connect house.elivcloud.org:443 -servername house.elivcloud.org 2>/dev/null | openssl x509 -noout -issuer -dates`
   -> a Let's Encrypt issuer; `curl -sSI http://house.elivcloud.org/` -> a redirect to `https://`.
2. **BotFather** **[OWNER]**: Login Widget -> allowed origin `https://house.elivcloud.org` and redirect
   URI `https://house.elivcloud.org/auth/telegram/callback`; put the Client ID/Secret in the env file
   (and the algorithm if not RS256); `dc up -d --force-recreate house-helper` applies them.
3. **Real Telegram sign-in** with the owner's invitation. Never exercised before: the `nonce` claim
   and the `sub` format are unknown until now. A failure is logged by reason (for example
   `wrong_nonce`) in `dc logs house-helper`; no code or token is in it.
4. **Desktop.** The page loads, no CSP errors in the console, sign in, create a conversation, send a
   message, reload, sign out. Devtools -> cookies: `__Host-hh_session` is `Secure`, `HttpOnly`,
   `SameSite=Lax`.
5. **Smartphone** (real iOS and Android): open the invitation link from the messenger -> Safari/Chrome;
   sign in, chat, rotate, background and return (bfcache), sign out. Test an in-app browser to see
   the documented failure, not to fix it.
6. **OpenAI quality** (a few real kitchen questions in Russian, one long, one off-topic, one
   food-safety edge case): review answers, the "cut off" notice, latency, and the OpenAI usage dashboard.
   The log shows only model, limits and conversation id, never the text.

   *GPT-6 Luna, one request.* Needs an image built from the commit that added `OPENAI_REASONING_EFFORT`
   (an older image would send `temperature` to the model and fail). In the env file: `OPENAI_MODEL=gpt-6-luna`,
   `OPENAI_REASONING_EFFORT=low`, `MAX_TOKENS=6000`, `OPENAI_TIMEOUT_SECONDS=50`, `OPENAI_MAX_RETRIES=1`, the
   project's key; `dc up -d --force-recreate house-helper`. Send ONE short question from the owner's session
   ("Что приготовить из яиц и сыра?"), then `dc logs house-helper | grep 'OpenAI'`. Expected: `OpenAI request:
   model=gpt-6-luna, reasoning_effort=low, max_completion_tokens=6000 ...` and `OpenAI usage: ...
   reasoning_tokens=N`. Read N against `MAX_TOKENS`: well under it is healthy; near it means answers will end
   empty or cut off, so raise `MAX_TOKENS`. A failure line carries the provider's own code, for example
   `status=400, code=unsupported_parameter, param=...` or `status=429, code=project_spend_limit_exceeded`.
   Stop at the first failure and report the line; do not retry in a loop (each request is billed).
7. **Log hygiene after the first real login:** repeat the canary of section 4.1 against the real host
   (it must still show `leaked 0` in every sink), then `dc logs house-helper | grep -E 'code=|state='`
   and the same grep over every Traefik sink you listed there -> all empty.
8. **Rate limits:** from outside, `for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code} ' https://house.elivcloud.org/auth/telegram/login; done`
   -> five redirects (303) then 429.

## 9. Upgrade **[OWNER]**

An upgrade changes the database schema, and a schema change cannot be undone by running it backwards
(section 11). So the only supported way is `deploy/upgrade.sh`, which **refuses to migrate unless a backup of
the running version has been made, checked and verified first**. Do not type the steps by hand. (The first
version of this runbook listed them as separate lines: after a failed backup the next lines still ran, and
the migration went ahead with no backup. The audit reproduced that.)

```bash
git fetch && git checkout <approved commit> && git rev-parse HEAD          # must print the approved SHA
# The tag of the version that is RUNNING now: the script will check that it really is.
docker inspect --format '{{.Config.Image}}' "$(dc ps --quiet house-helper)"    # for example house-helper:a1b2c3d4e5f6
# Build the new image next to the old one (the script compares its revision label with this checkout).
HOUSE_HELPER_GIT_SHA=$(git rev-parse HEAD) HOUSE_HELPER_IMAGE_TAG=$(git rev-parse --short=12 HEAD) dc build
bash deploy/upgrade.sh --old-tag a1b2c3d4e5f6 --dry-run     # every check that changes nothing
bash deploy/upgrade.sh --old-tag a1b2c3d4e5f6               # the upgrade (about a minute of downtime)
```

If the cleanup timer of section 14 is enabled, stop it first (section 14.4): a cleanup that holds the maintenance lock when
step 3 starts makes the migration refuse, and the script's message then says more than is true.

It reads no variable from your shell (not a stale `$F`, not `HOUSE_HELPER_IMAGE_TAG`): the old tag is an
argument, the new tag is the commit of the checkout. In order, with `set -euo pipefail`:

| Step | What | If it fails |
|---|---|---|
| 0 | checks that change nothing: the Compose file resolves, both images exist, the new image was built from **this** commit, the running container is really `--old-tag`, the backup directory is writable | stop; nothing was touched, the service is still up |
| 1 | stop the web service and confirm it is stopped (exit 143 afterwards is normal) | stop; no backup, no migration |
| 2 | `backup.sh` with the **old** image: backup, non-empty, SQLite header, SHA-256 equal to the one the tool reported, disposable-container `verify --require-head`, published as a complete pair (`.db` + `.db.sha256`, section 10); then `upgrade.sh` itself judges the **files**, not `backup.sh`'s word: the name must match the name rule, and the checksum file must be, byte for byte, the one line for **that** file, with a digest `upgrade.sh` computes itself (a checksum for another file, two records, an empty or malformed file, a wrong digest or a path in it are all refused; `sha256sum -c` is not the gate) | **stop. The migration is not run, the database is unchanged**, the web service stays stopped, and the exact command that starts the old version is printed. A `.db` without its checksum file, or with one that does not match, is refused the same way |
| 3 | only now: `exec --exclusive -- alembic upgrade head` with the **new** image | stop; prints the recovery for this exact backup; never retries |
| 4 | `check-schema`; start the new version; find its container; ask Docker for its health until it says `healthy` (up to 120 s) | stop, with the **new web service stopped again** (see below), and the same recovery printed |

**Every failure after step 3 takes one path.** Whatever goes wrong from the migration on, whether a checked
command (the migration, `check-schema`, `up`, finding the container, `docker inspect`, a container that is
`unhealthy`, reports no health status at all, or is still `starting` after 120 s), a command nothing checked
(`set -e` ends the script and a trap still runs the same handler), or `Ctrl-C` / `SIGTERM` / `SIGHUP`, the script:

1. says which operation failed and with what exit status;
2. if the new version may have been started, runs `docker compose stop house-helper` (**that one service**;
   no other container is touched) and says whether the stop worked and whether it could **confirm** that
   nothing is running (`stopped, and confirmed`), or says loudly that the stop failed, that the web service
   is `STILL RUNNING`, or that its state is `UNKNOWN` (then stop it yourself first);
3. prints the recovery for this exact backup and for the matching pre-upgrade image (its tag and image id),
   and exits 1. It does not print `upgraded`.

It never restores the backup, downgrades or retries the migration for you. The recovery text does not
depend on a container id, so it is the same when Docker could not even find the container.

`--require-head` is checked by the **old** image, so the backup must be at the schema that image
expects: a database that is behind its own image, or ahead of it, fails here (also reproduced: with the
database one revision behind, the script stopped at step 2 and the schema stayed unchanged).

**If it stops.** Before step 3: the database is as it was; start the old version with the command it
prints, fix the cause, run it again. From step 3 on: Alembic reports "non-transactional DDL" for SQLite, so
a failed migration can leave a partly changed schema; **do not retry it on that file.** Restore the
pre-upgrade backup with the old image (the script prints the eight steps with the real file name, in this
order: stop, confirm it is stopped, the exact checksum-file check, `check-idle`, `restore --replace`,
`check-schema`, `up`, and a look at the health; they are the *Restore* procedure of section 10) and start the
old version. The backup
file, its `.sha256` and the old image stay until the new version has been accepted: do not run
`docker image prune -a`.

Afterwards, by hand: logs, TLS, one real sign-in, one chat message (section 8).

## 10. Backup and restore

**What a backup is.** The database runs in WAL mode: committed data lives in the file *and* its
`-wal` file. Copying the file alone can miss committed work or be torn. `dbtool backup` uses
SQLite's online backup API through a read-only connection (safe while the application runs),
writes one self-contained file, **refuses a result that is not sound or does not have this application's
schema**, and prints the SHA-256 and what is inside (row counts per table, never content). The file
contains **users' conversations**: treat it as personal data. A backup it could not restore is not made:
a database over 1 GiB is refused with `database_too_large`.

**Take one, with the script, always:**

```bash
bash deploy/backup.sh --tag a1b2c3d4e5f6                 # the tag of the RUNNING version; prints the path
bash deploy/backup.sh --tag a1b2c3d4e5f6 --dir ~/backups/house-helper --label before-experiment
bash deploy/backup.sh --tag a1b2c3d4e5f6 --name house-helper-2026-10-24.db      # an exact name
```

The tag is required (no default, no environment variable). It works with the web service running or
stopped. What it guarantees:

- The file is first written to `<name>.part`, created **exclusively** (an existing file stops the script),
  mode 0600, in a mode-0700 directory. Nothing is published until: the tool exited 0, the size is
  between 1 byte and 1 GiB, it starts like a SQLite database, its SHA-256 on the host equals the one the tool
  computed inside the container (a damaged stream is caught), a **disposable container has verified it**
  (below), the checksum file has been written and read back, and the file has been hashed once more.
- **A completed backup is a pair:** `<name>.db` and `<name>.db.sha256`, where `<name>` is
  `house-helper-<UTC time>-<random>-<label>` (or the `--name` you gave). **The file name has one rule**, the same
  text in `backup.sh`, `upgrade.sh` and here: `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db$` (ASCII letters, digits,
  `_ . -`, not starting with `.` or `-`, ending in `.db`, checked in the C locale). **The checksum file is exactly one
  line, byte for byte:** the SHA-256 of the `.db` as 64 lower-case hex digits, **two spaces**, the `.db`'s own base
  name, a newline. No path, no `*` marker, no CRLF, no second record. Both files are made with a hard link, which
  is atomic and **fails instead of replacing**, in this order: **the checksum file first, the `.db` last. The
  appearance of the `.db` is the commit.** Consequences, each tested with real filesystem operations:
  - the `.db` is never visible without its checksum file, at any instant and after the script is killed;
  - if publishing the `.db` fails (another file took its name meanwhile, `ln` fails), the script removes
    the checksum file it made: a failed attempt leaves no `.db`, no checksum and no `.part` of its own, and
    an existing backup or checksum file is never touched. (The audit's finding: the first version linked
    the `.db` first, and when the checksum's name was then taken by another file it left the `.db` behind.)
  - **committed is not the same as reported.** The commit is the creation of the `.db` link; the script learns
    of it one statement later and reports it last, by printing the path. If `SIGTERM`, `SIGINT` or `SIGHUP`
    arrives in between (or after the commit and before the path is printed), the clean-up decides from the
    **files**, not from its memory: when the `.db` is a hard link to the very `.part` this run made, the checksum
    file is this run's own and is the exact line for it, and the `.db` still hashes to that digest, it **keeps the
    complete pair**, says so on standard error, prints nothing on standard output and exits with the signal's
    status. A caller such as `upgrade.sh` therefore does not use a path it was never given; a person can use the
    pair, which is verifiable like any other. Before the commit it removes what this run made: the `.db` first,
    then the checksum file, so the `.db` is never visible without it.
  - **ownership is identity, not a name.** A file is this run's only if it is the very file (same device and
    inode) that this run created, directly or as a hard link to its `.part`. Another publisher's file under the same
    name, and everything a run that lost the name finds there, is never removed or replaced.
  - if the script is *killed* between the two links (`SIGKILL`, power loss), what remains is a `.sha256`
    with no `.db`. That is **not** a backup. It blocks only its own name until you delete it by hand, as an
    older `.part` does.
  - `SIGKILL` and a power cut run no clean-up, so they can leave temporary files **even when the committed pair is
    valid**: killed after the commit, `<name>.db.part` and `<name>.db.sha256.part` remain beside the pair. They
    are hard links of the same two files (the data is not stored twice), they block that name until you delete
    them by hand, and deleting them does not touch the backup.
  - two runs for the same name cannot both publish: one holds the `.part`, and whoever holds the checksum
    name is the only one that can link the `.db`.
- **A file is a backup only if** `<name>.db.sha256` is beside it, both are regular files (not symbolic links),
  and the checksum file is exactly the one line above for **that** file name. `upgrade.sh` makes that check
  itself, byte for byte, with a digest it computes from the selected file, before it migrates, and refuses
  anything else, including a checksum file that is valid but for **another** file. `sha256sum -c` alone is
  **not** that check: it verifies whichever file the checksum file names, so a valid checksum for `other.db`
  inside `claimed.db.sha256` passes it for `claimed.db` (the audit's reproduction). The exact check, by hand:

  ```bash
  F=/path/to/the/backup.db; D=$(dirname "$F"); B=$(basename "$F")
  ( cd "$D" && echo "$(sha256sum < "$B" | cut -d' ' -f1)  $B" | cmp - "$B.sha256" && echo "exactly the one line for $B" )
  ```

  Anything else you write (the off-host copy, retention) must do the same, copy the pair together, and delete
  the `.db` first and its `.sha256` after, so that a half-deleted pair is never mistaken for a backup.
- Standard output is the one line with the path of the `.db`, and only when the pair is committed **and** reported;
  everything else goes to standard error. Exit status 0 means published, verified and reported.

`dbtool backup --output FILE` (inside a container) has the same exclusive-create and no-replace
publication and is tested directly; the script streams to the host instead because the container's user
(uid 10001) cannot, and should not, write into an operator's home directory.

**Verify, disposably, whenever you hold a backup** (it needs no data volume and no network; use the
image tag that matches the backup; `-v /staging` is the scratch space, section 2):

```bash
docker run --rm -i --init --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --log-driver none --tmpfs /tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001 -v /staging \
  house-helper:a1b2c3d4e5f6 python /opt/house-helper/ops/dbtool.py verify --input - --require-head < "$F"
```

It checks the SQLite header, `integrity_check`, `foreign_key_check`, that exactly one Alembic revision is
present and known to this image (`--require-head` also refuses an older one), and **the schema itself**: it
builds the database that *this image's own migrations* produce at that revision and compares tables,
columns (type, NOT NULL, primary key), foreign keys, indexes, and any trigger or view. A database that
only advertises the right revision, an incomplete one, one with an extra table or a missing column, is
refused (`schema_mismatch`, exit 3): the first version accepted a file holding nothing but an
`alembic_version` row. **Deliberately not compared:** column defaults and CHECK constraints (and the text of
triggers and views). That is a choice of this check, not a limit of SQLite: it does expose column defaults
(`PRAGMA table_info` / `table_xinfo`) and every table's full DDL (`sqlite_schema`), but it has no pragma that
lists CHECK constraints, which exist only inside that DDL text, and comparing DDL text would need either an
exact match, which one schema spelled two ways (for example after an `ALTER TABLE`) defeats, or an SQL parser.
This kit has neither, so a database that differs from the reference only in those passes. Exit 0 sound, 3 refused (wrong revision or schema, foreign database), 4 damaged. It prints
row counts. Ordinary `restore` applies the same checks.

**Off-host copy is required before real conversations are stored [OWNER: choose the target].** A copy
on the same VPS does not survive its loss. The RU and FI hosts exist but their role is undecided.
Encrypt before it leaves the host, for example with `age -r <recipient public key> "$F" > "$F.age"`
then `rclone`/`scp` the `.age` file; keep the private key off the VPS. Neither tool is installed
here and no schedule is configured. A nightly job would be a systemd timer or cron entry running
`backup.sh` (which already verifies), then the copy, alerting on any non-zero exit.
The repository now holds the mechanism for this copy, not installed and not run against a real account: **section 15**.
**Retention [OWNER: decide]:** a deleted conversation survives in every older backup, so the backup
retention bounds how real the 365-day deletion promise is (section 12). A common starting point is
7 daily and 4 weekly copies.

**Rehearse a restore before relying on it** (the steps below, onto a scratch volume or a spare
host), and record the date. A backup nobody has restored is a hope.

**Restore.** The order is mandatory; each step is a check for the next:

```bash
F=/path/to/the/backup.db; D=$(dirname "$F"); B=$(basename "$F")           # a backup only with F.sha256 beside it (section 10)
( cd "$D" && echo "$(sha256sum < "$B" | cut -d' ' -f1)  $B" | cmp - "$B.sha256" && echo "exactly the one line for $B" )   # must print the message: the copy you hold is the one that was verified
dc stop house-helper
dc ps --status running --quiet house-helper                                # must print NOTHING: the service is stopped
hhops check-idle                                                           # must exit 0: no lock held, nobody has the database open or is writing
HOUSE_HELPER_IMAGE_TAG=a1b2c3d4e5f6 hhops restore --input - --replace < "$F"   # the image that wrote F; --replace only if a database exists
HOUSE_HELPER_IMAGE_TAG=a1b2c3d4e5f6 hhops check-schema
HOUSE_HELPER_IMAGE_TAG=a1b2c3d4e5f6 dc up -d house-helper
dc ps house-helper                                                         # State: running, Health: healthy (about 10 s)
```

(After a failed upgrade, `upgrade.sh` prints these same steps for the exact backup and image, with the stop first
because the new version may still be running; the checksum check then follows the confirmation that it stopped.)

What `restore` does and refuses (section 2 describes the lock):

1. takes the **exclusive** maintenance lock: refused (`maintenance_lock_busy`) while the web service or any other
   ops command runs, and the web service cannot start while it holds it; then, if a database exists and
   `--replace` was given, checks it with SQLite itself: refused (`database_busy`, `database_in_use`) if another process is writing
   to it or has it open, even one that never asked for the lock;
2. copies the input to **scratch space on disk** and verifies it there, with the same checks as `verify` plus
   a requirement that its revision is one this image knows. Not enough scratch or data-volume space:
   refused (`staging_space`, `data_dir_space`) before anything is written;
3. copies it into `/data` under a private temporary name, moves the old database **together with its `-wal`,
   `-shm` and `-journal`** into `/data/pre-restore-<UTC time>/`, and renames the new file into place. That
   matters: tested locally, leaving the old `-wal` beside a copied-in file made SQLite replay it and
   resurrect data written after the backup, with `integrity_check` still `ok`;
4. **if anything fails after the old files were moved aside, moves them back** (the previous database is
   unchanged); if even that fails, the message names the directory that holds them and says to move them back.
   A failed restore never leaves you without the previous database.

Without `--replace` it refuses to touch an existing database. Delete `pre-restore-*` only after the
restored service has been accepted (each can be as large as the database).

**An exceptional recovery, outside the tool, by the owner.** `restore` has no switch for a backup that fails the
schema check, by design: such a file is incomplete, foreign or altered, and installing it would run the
application on it. If it is the only copy left, work on a *copy of it outside the data volume* (for
example `sqlite3 copy.db .recover` on a workstation), create a new database from the migrations on an empty volume
(`hhops exec --exclusive -- alembic upgrade head`), load what can be trusted into it, and take a normal
backup of the result. This is not rehearsed, has no tooling here, and needs the owner's decision each time.
A raw copy of the live database file, without its `-wal`, is the same case: it can be missing committed data.

## 11. Rollback and recovery

`alembic downgrade` is **not** a rollback. Downgrading `0006` deletes in-flight invitation-bound
logins; downgrading `0005` drops the invitations table and everything in it; a downgrade also
cannot restore data a newer version changed or removed. The rollback is **the saved database plus
the application version that wrote it**. In the table, `OLD` is the tag of that version:

| Situation | Action |
|---|---|
| A bad release, no schema change | `HOUSE_HELPER_IMAGE_TAG=OLD dc up -d house-helper` (the image still exists) |
| A bad release after a migration | what `upgrade.sh` printed: `dc stop house-helper`; confirm it is stopped; the exact checksum-file check; `hhops check-idle`; with `HOUSE_HELPER_IMAGE_TAG=OLD` restore the **pre-upgrade backup** (section 10); `check-schema`; `HOUSE_HELPER_IMAGE_TAG=OLD dc up -d house-helper`; `dc ps house-helper` until `healthy` |
| A failed or interrupted migration | same as above: restore the pre-upgrade backup, do not retry on the damaged file |
| Container exits at start (`schema_not_head`) | read the message: *behind* -> upgrade with `deploy/upgrade.sh` (section 9); *ahead* -> start the image that matches (`OLD`), or restore a backup of that version. Never migrate down to fit |
| Container exits at start (`maintenance_lock_busy`) | a restore or migration is running, or just ran; Docker retries by itself. If nothing is running, `hhops check-idle` says what holds the lock |
| Database corrupt / volume lost | `dc stop house-helper`; `hhops check-idle`; restore the newest verified backup (section 10); `dc up -d house-helper`. On a new host: clone the same commit, `dc build`, create the env file (`new-env-file.sh`), `restore` into the empty volume (no `--replace`), `check-schema`, `up` |
| A leaked invitation | `hhops exec --shared -- python -m house_helper.admin revoke-invitation --user-id <id>` (it stops working at once) |
| Secrets exposed | rotate the Telegram Client Secret in BotFather and the OpenAI key, change the env file (section 3, *Changing a secret*), `dc up -d --force-recreate house-helper`. The application has no session-signing key to rotate: sessions are random tokens stored only as hashes in the database |

Sign-in state is in the database, so a restore signs users out only if the backup predates their
session. After any restore, check the schema, sign in, and send one message.

## 12. Security and retention gates (status at commit `ddba682` + this kit; the two retention rows updated by R4C-D2)

None of these is waived. "Verified" means locally, on disposable resources: on 2026-10-22 for the first
version of the kit and on 2026-10-23 for the remediation of its audit.

### Pre-live deployment gates (built by R4C-D2, code released in Git; timer installed, first scheduled run verified and accepted 2026-10-25)

Two product requirements are **gates, not options**, and nothing in this document (a short beta, two users,
an owner-only trial, a time limit on the trial) substitutes for either of them. R4C-D2 built them (section 14;
code `489ff30`, audited and accepted), and a gate stays open until the work is **scheduled on the host and seen to run**.
That condition was met on 2026-10-25 (section 14.9), so the activation of both is closed; the backup row below is not:

1. **Scheduled cleanup of authentication artifacts** must be implemented, tested and running **before real use**,
   that is before any real person signs in.
2. **365-day physical deletion of conversation history** must be implemented, tested and running **before any
   real user data is stored** (the rule in `docs/STATUS.md`). Hiding expired rows from reads is not deletion.

Both are now running (the checklist is section 14.9). That does not make real use safe: the backup retention and
off-host copy below are a separate BLOCKER before real data, failure reporting is not configured, and the deployment
stays a technical instance with disposable data only (no real users are enrolled and no real conversation is stored).
Building them changed the application (the command
`purge-expired`, `house_helper/admin/retention.py`), which is outside the kit R4C-D1 released; it was audited on its
own and is not part of what D1's audits covered.

| Gate | Status |
|---|---|
| **Scheduled cleanup** of expired sessions, OIDC states, invitations, email challenges | **CODE RELEASED IN GIT (R4C-D2, `489ff30`); TIMER INSTALLED AND ACTIVE ON THE VPS (2026-10-24); FIRST CALENDAR-TRIGGERED RUN (2026-10-25) VERIFIED AND ACCEPTED. Pre-live gate 1: required before real use.** `python -m house_helper.admin purge-expired` deletes each of them past its own expiry, in short batches, beside the web service (`hhops exec --shared`); `--dry-run` only counts. The opt-in systemd timer and service are in `deploy/systemd/`; nothing in the repository installs them. **Closed on 2026-10-25:** the timer's first calendar-triggered run was read in the journal (section 14.9) and accepted by the owner; it exited 0 and found no expired record, so it deleted nothing (owner-reported). Failure reporting is not configured and is not part of this closure. |
| **365-day physical deletion** of conversation history | **CODE RELEASED IN GIT (R4C-D2, `489ff30`); TIMER INSTALLED AND ACTIVE ON THE VPS (2026-10-24); FIRST CALENDAR-TRIGGERED RUN (2026-10-25) VERIFIED AND ACCEPTED. Pre-live gate 2: required before any real user data is stored; not waived.** The same command deletes messages and the turn requests that hold a copy of an answer 365 x 24 hours after their own creation, keeps the recent messages of an old conversation and removes conversations left empty. It is logical SQL deletion, not forensic erasure, and a deleted row survives in every older backup (section 14.7): the backup retention below bounds how real the promise is. **Closed on 2026-10-25** on the same condition as the row above: the first calendar-triggered run was read in the journal and accepted. That run found no expired record, so the deletion of real expired rows on this database has not been observed; it rests on the tests (section 13). The backup row below stays open. |
| **Backup retention and off-host** | Backup, verify and restore **verified**; off-host target, encryption, retention and schedule **undecided and not installed.** BLOCKER before real data. R4C-D2 decides none of it (section 14.7). |
| **Restore interlock** | **Verified** with unit tests on Linux and on a real Compose stack: a restore is refused while the web service runs (the lock is held by the uvicorn process itself), refused while a writer that ignores the lock is mid-transaction, a backup works beside the running service, a failed restore puts the old database back. **Cooperative**, with the limits stated in section 2 (a process that ignores the lock and starts after the final check; a damaged file cannot be probed; anything outside Docker). |
| **Scratch space** | **Verified:** an 84 MB database (above the 64 MiB `/tmp` that failed in the first version) is backed up, verified and restored; too little space is refused before any write. Declared maximum database: 1 GiB (`MAX_DATABASE_BYTES`); a real 1 GiB database was **not** exercised (it needs about 2.2 GiB of free disk to restore). |
| **Upgrade gate** | **Verified** with real Docker: with the database one revision behind, `upgrade.sh` stopped at the verification step and the schema stayed unchanged; a normal upgrade ran through and the new version became healthy; an upgrade whose new web service failed its health check after the migration stopped only the web service, confirmed it, and printed a recovery whose commands, run as printed, brought the old version back healthy from the backup (details in section 13). Failure injection with a fake `docker` covers every step and every Docker answer after the start (container lookup fails or is empty, `inspect` fails, no health status, `unhealthy`, timeout), a stop that fails or cannot be confirmed, signals, an incomplete backup pair, a checksum file that is valid but for another file (and every other deviation from the one canonical line), and a command nothing checks. Not run on the VPS. |
| **Schema validation** | **Verified:** compares tables, columns (type, NOT NULL, primary key), foreign keys, indexes, triggers and views with what this image's migrations produce at the backup's revision, for every revision in the history. **Deliberately not compared** (a choice, not a limit of SQLite; section 10): column defaults, CHECK constraints, and the text of triggers and views. |
| **Authentication rate limiting** | Traefik labels, **verified** against Traefik 3.6.25 (burst, 429, route isolation, spoofed `X-Forwarded-For`). The application has none (by design). **REQUIRES LIVE** confirmation on the VPS Traefik. |
| **Sensitive query parameters in logs** | Application: access log disabled (`--no-access-log`) *and* the redaction filter (`web/logsafety.py`) which was **verified** with the access log switched on; canary values were absent from container logs and Docker's log files. Proxy: Traefik's behaviour for each sink and format is **measured** on 3.6.25 (section 4.1: `RequestPath=drop` and `queryparameters.defaultmode=drop` work, `redact` does not, a static file overrides flags and variables); the shared Traefik itself has **not been inspected** with this procedure: **REQUIRES LIVE**. The invitation secret travels in a URL fragment and is in no server log. |
| **Chat-API ingress limits** | A per-address request limit is included. **Not included:** a global cap on concurrent provider calls and a request-body size limit (candidate Traefik `inFlightReq` / `buffering` middlewares, to be decided and tested). |
| **OpenAI cost cap** | **Not configured (OWNER).** Only an enforced **hard** spend limit on a dedicated project stops spending; alerts do not (section 4.2). |
| **Real Telegram OIDC** | Never exercised. **REQUIRES LIVE** (section 8.3). |
| **Real smartphone/browser behaviour** | Never exercised. **REQUIRES LIVE** (section 8.4-8.5). |
| **Real OpenAI response quality** | Never exercised. **REQUIRES LIVE** (section 8.6). |
| **Controlled first-user provisioning** | Procedure documented (section 7), **not performed.** |
| Email sign-in, remembered devices, public registration | Not activated / not implemented / not present. |

## 13. What was verified, and what was not

**Verified locally** (Windows, Docker Desktop 29.8.2, Compose 5.6.0, Traefik 3.6.25 on a throwaway
loopback-only instance, a throwaway network named `n8n_n8n_network`): the image builds without
warnings and contains no `.env`, database, tests, docs or bot module; non-root, read-only root,
no published port, resource limits and log rotation applied; a new named volume inherits uid 10001;
the start guard refuses an empty, older, newer, unconfigured and relative-path database; migration
0001 -> 0006 as uid 10001; the page, all static assets and `/healthz` are served; `/docs` and
`/openapi.json` are 404; graceful shutdown; persistence across `down`/`up`; enrollment alongside the
running application with the secret absent from Docker's log files (with a positive control);
backup byte-identical to its reported SHA-256, taken while the application ran; verify catching
truncation, page corruption, a non-database and a foreign database; restore including the stale
`-wal` case and an empty-volume recovery; the Traefik routers, priorities, headers and limits,
including the app's own headers surviving; canary `code`/`state` values absent from every log.

**Added by the remediation, on a real Compose stack, a Linux container for the scripts and Linux
pytest runs:** the three audit failures were first reproduced on the first version and then shown
refused or handled: a restore with another container mid-transaction (refused; the writer's later
commit lands in the live database); an 84 MB database backed up, verified and restored through the 64 MiB
`/tmp` limit that broke it; an upgrade whose backup cannot be verified (the migration did not run). Also: the
lock held by the uvicorn process in the container, restore and migration refused beside it, an enrollment
command allowed; anonymous `/staging` volumes deleted by `run --rm`; real full-staging refusals;
`backup.sh` and `upgrade.sh` against the live stack; the Traefik table of section 4.1 (sinks, precedence,
suppression modes, header modes), including `RequestPath=drop` in the common log format.

**Added by the second focused remediation (2026-10-23).** The two audit findings were reproduced against the
previous scripts first (the exit-9 `docker inspect` after `up` left the new service running with no recovery
printed; a checksum name taken by another file after the pre-checks left a published `.db` behind), then shown
fixed with the same inputs. On real Docker (Docker Desktop 29.8.2; the scripts ran in a throwaway Alpine
container with GNU coreutils 9.11, Docker CLI 29.9.2 and Compose 5.6.1, a throwaway `n8n_n8n_network`, a clean
copy of the checkout with its own throwaway git history, a dummy API key): a first install, `upgrade.sh
--dry-run`, a successful upgrade (stop, backup, verification, migration, schema check, start, `healthy`) whose
pair passed `sha256sum -c`; a failing upgrade (an out-of-range `TEMPERATURE` made the new web service
unhealthy) that reported the operation, stopped the web service only (the unrelated container on the host was
untouched), confirmed it, exited 1 and printed the recovery, whose five commands were then run exactly as
printed (checksum, stop, `check-idle`, `restore --replace`, `up`), after the bad setting was reverted, and left the old
version healthy; four
concurrent `backup.sh` runs for one name (exactly one published; one valid pair in the directory) and a
pre-existing checksum file (refused, untouched). All of it was removed afterwards. The scripts were also checked
with ShellCheck (only intentional warnings remain) and mutation-tested: removing the EXIT trap, the stop, the
stop confirmation, the unhealthy verdict, the pair check, the checksum-first order, or the checksum clean-up
each fails a named test.

**Added by the third focused remediation (2026-10-23): the backup gate.** Three findings of the latest independent
review, each reproduced on the previous scripts first and then shown fixed with the same inputs, with the real
scripts and real files (a fake `docker` and `git`, the real `ln`, signals delivered by a wrapped `ln` exactly between
the commit link and the note of it): a `SIGTERM`, `SIGINT` or `SIGHUP` after the `.db` link left the `.db` published
without its checksum file (now: the complete pair is kept and reported as not reported); the clean-up removed names
instead of files it made (now: identity by device and inode; a look-alike checksum file of another publisher is left
alone); `upgrade.sh` accepted `claimed.db` whose checksum file held a valid checksum for `other.db` and went on to
the migration (now: the one-line contract above, byte for byte; every variation in the test table is refused before the migration); and the
printed recovery omitted `check-schema`, the confirmation that the service stopped, and the health look (now: the
runbook's order). Run on Windows (Git Bash) and on Linux (WSL Ubuntu, Bash 5.2, GNU coreutils); **not** re-run on real
Docker, and not on the VPS. `SIGKILL` and a power cut still run nothing: the pair is valid, temporary files may
remain.

**Added by R4C-D2 (2026-10-23), the scheduled cleanup; code and units, no host changed.** On Docker Desktop 29.8.2 / Compose
5.6.0 with a throwaway `n8n_n8n_network`, the throwaway volume `house-helper-data`, a placeholder environment file and synthetic
rows only: the image built from the working tree (uid 10001); the empty volume migrated to `0006`; **the unit's exact command**
(`docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops exec --shared -- python -m house_helper.admin
purge-expired`, run from another working directory) did a dry run that changed nothing, then deleted the four expired rows beside
the real web service (healthy, holding the shared lock) with the report on the client's standard output although the service has
no log driver, then reported `unchanged`; a migration (`exec --exclusive`) was refused with `maintenance_lock_busy` beside the
web service; a text planted in a message older than 365 days was in the database file before and absent after. Everything was
removed by name afterwards and the Docker host was left as found. `systemd-analyze verify` (systemd 255, a throwaway root with
stand-ins for Docker and `purge.env`) accepted both units, and `systemd-analyze calendar` the schedule. The opt-in PostgreSQL tests
passed on `postgres:16-alpine` (18 tests; a first run found that a REPEATABLE READ database default made a second concurrent run
fail with a serialization error, which is why the cleanup now sets its own isolation level; the independent audit then
reproduced a foreign-key failure when a run skipped a request that another run held and went on to delete the message the
request links, which is why a parent is now judged by the child rows that exist, section 14.3). **Not done:** the units were never
installed or run by systemd, so `Persistent=`, `TimeoutStartSec` and the journal were not exercised; the web service had no open
database connection during the container run (its health check touches no database), so live writers beside the cleanup were
exercised in-process only; no real data, no VPS, no real credential; a `docker compose` client killed mid-run was not tested.
The independent audit of this work ended in a PASS and the owner accepted it (both reported by the owner); the code was
released as `489ff30` and its push-triggered CI was green on Python 3.10 and 3.12. CI is not a run on any host.

**Documented, not tested:** the first-install and upgrade sequences end to end on the VPS image
delivery by building on the server, off-host backup, the backup schedule (and the cleanup timer of section 14: its
units exist, were checked as text and never ran on any host), rollback to an older image of a
different schema (the upgrade test used an unchanged schema), the DoH commands, an OTLP sink (the flags
exist in 3.6.25; no collector was run), behaviour on PostgreSQL (SQLite only is deployed), a 1 GiB database.

**Requires the live environment:** DNS, Let's Encrypt issuance, the VPS Traefik's configuration,
Telegram, BotFather, real phones and browsers, OpenAI, the other applications on the host.

Known limits of the kit: single container (an upgrade means about a minute of downtime); the
shared flat network (section 2); dependencies are version-pinned but not hash-pinned; the image also
carries the bot-only libraries `aiogram`/`aiohttp` (about 23 MB) because `requirements.txt` is one
list, and a split into web and bot requirement files would remove them; the maintenance lock is
cooperative (section 2).

## 14. Scheduled cleanup and retention (R4C-D2)

**Status: code RELEASED in Git as `489ff30` (independent audit passed, owner accepted, push-triggered CI green).
The timer is installed and active on the VPS (owner-reported, 2026-10-24). Its first calendar-triggered execution, on
2026-10-25, was verified and accepted by the owner (owner-reported; the evidence is in section 14.9).** The statements
about what was tested (section 13) are about the work before the deployment: the command was run on disposable
databases with synthetic rows. That one run on the real database found no expired record and deleted nothing, so the
deletion of real expired rows has not been observed there; it rests on those tests. Failure reporting (14.4) and the
encrypted off-host backup (section 15) are separate and still pending. Every step marked **[OWNER]** changes
something outside the repository and needs the owner's explicit approval first. The code is
`house_helper/admin/retention.py`; the units are in `deploy/systemd/`. It adds no migration and changes no table.

### 14.1 What is deleted, and what never is

One command deletes two kinds of rows, for real (an SQL `DELETE`; hiding expired rows from reads, which the
application already does, is not deletion). **A sign-in artifact goes when its own `expires_at` has passed**, whether it
was used, withdrawn or never touched: a used or revoked row stays until its own deadline (so no revocation or
replay evidence disappears early and no new retention period exists), and after it is gone every reader sees
"unknown", which each already treats exactly like "expired". **History is judged row by row**, 365 x 24 hours after
the row's own creation.

| Data (table) | Deadline | Deleted when | Kept, and why |
|---|---|---|---|
| Login sessions (`sessions`) | 14 days after sign-in | `expires_at` <= now | live sessions; a session revoked but not yet expired stays until its own deadline (it never authenticates meanwhile) |
| OIDC login states (`oidc_states`) | 10 minutes | `expires_at` <= now | pending logins; a consumed state stays until its deadline, so a replay is refused as "used", afterwards as "unknown" |
| Emailed sign-in links (`email_login_challenges`) | 10 minutes | `expires_at` <= now | live and used-but-unexpired links; the 60-second request cooldown looks only at the last 60 seconds, well inside the 10 minutes |
| Telegram invitations (`identity_invitations`) | 2 hours | `expires_at` <= now **and** no login state is still bound to it | active, used-but-unexpired and withdrawn-but-unexpired ones. The identity an invitation linked (`allowlist_entries`, `identity_links`) is not an artifact and is never touched |
| Turn requests (`turn_requests`; `result_payload` is a copy of an answer) | 365 days after creation | `expires_at` <= now **or** `created_at` <= now - 365 x 24 h, and not in flight under a live lease. A request still in flight whose lease is over (its worker is gone) is first set to `cancelled`, then deleted | a request whose lease is still live, and its messages, until the lease ends |
| Messages (`messages`) | 365 days after creation | the same rule, **and** no request that links the message still exists (the database refuses otherwise) | recent messages of an old conversation; an expired message a request still links waits for the next run (reported as *held back*) |
| Conversations (`conversations`) | none of their own | no message and no request left **and** no activity for 365 days (`updated_at` and `created_at` both at or before the cutoff) | any conversation with a message; a conversation created minutes ago and still empty |

**Never touched:** `users`, `allowlist_entries`, `identity_links`, `alembic_version`, and every table the command does
not name. The favourites and long-term preferences of the product requirements (KHD-05) are **not in the schema
yet**; when they arrive they are independent tables owned by a user, and this command, which names its tables one by
one, will not know them. A test with such tables shows them untouched when the history they came from is deleted. The
converse is guarded too: a test fails when a table appears in the schema that the cleanup does not name and the test does
not list as kept, so that a future table holding conversation text cannot escape the 365 days unnoticed.
Nothing else in this database holds conversation text: a conversation's `title` is never set, the answer copy in
`turn_requests` is deleted with its request, and there are no summaries. Outside this database: the Telegram bot's
history is in that process's memory and is not in the image; whatever OpenAI keeps of the messages it answered is
governed by the account's settings there, not by this command.

**The boundary.** The cutoff is `now - 365 x 24 hours`, computed once per run from one UTC clock. A row created at
exactly the cutoff is deleted (at that instant it has already expired for every read, which show a message only while
its `expires_at` is after now); one created a microsecond later is kept. The second clause of the rule (`created_at`
<= cutoff) is the policy ceiling spelled from the creation time: it removes a row whose stored deadline is wrong
(a restored or hand-edited database; the CHECK constraints refuse such a row on a migrated one).

### 14.2 The command

```bash
hhops exec --shared -- python -m house_helper.admin purge-expired --dry-run   # counts only: one read-only transaction
hhops exec --shared -- python -m house_helper.admin purge-expired             # deletes
```

(`hhops` is defined in section 3. `--shared` is the lock mode of section 14.3, and it is the only mode that works
beside the web service.) **Normal execution deletes; `--dry-run` is the explicit way to look.** An earlier proposal
for a dry-run default was dropped because a scheduled run that only counts would delete nothing. The command line has
no option for the time, the age or a table, on purpose: a mistyped date would delete a year of history. A dry run
is a **preview from one snapshot**: it counts what the stages would delete from the database as it is while the one
read transaction runs, taking it that every stage deletes everything it may. A real run at the same instant deletes the
same rows only if the database does not change in between and the run meets nothing to skip: a login or a request that
changes state, another cleanup that deletes first, or (on PostgreSQL, which is not the deployment) a row that another
transaction holds, which the run leaves for the next one and so deletes less than the preview counted. On a database
nobody else touches the two are equal (tests compare them over random databases and on the worlds of the tests).

The report is one status line and one line per category, counts and two timestamps only (never an id, a name, an
address, a token, a digest or a message):

```text
ok [purged]: deleted 14 row(s): conversation history created at or before 2025-10-23T03:30:00Z (365 x 24 hours before 2026-10-23T03:30:00Z) and sign-in artifacts past their own expiry.
  sessions: 3
  oidc_states: 1
  email_login_challenges: 0
  identity_invitations: 1
  turn_requests: 2
  messages: 6
  conversations: 1
  (included in turn_requests) in flight and abandoned, cancelled first: 0
  (not deleted yet) expired messages still linked by a request that cannot be deleted yet: 0
```

`ok [unchanged]` when nothing had expired, `ok [dry_run]` for a dry run. **Exit status:** 0 ok; 1 an unexpected failure
(`error [purge_failed]`, the stage and the class name of the exception, nothing else); 2 usage; 3 refused
(`refused [database_busy]`: the database stayed locked by another writer for 30 seconds; or `maintenance_lock_busy` from
the operator tool: a restore or migration holds the directory); 4 the database cannot be used (not configured, missing,
not at the schema head: `schema_not_head`). A run that stops half way (1, 3 or 130) prints how many rows its committed
batches had already deleted. It is safe to repeat in every case.

### 14.3 The lock, and how it behaves beside the application

The command does not take a lock of its own: `exec --shared` takes the **shared** maintenance lock of section 2, the one
the web service, a backup and the enrollment commands hold.

| While the cleanup runs | Result |
|---|---|
| the web service runs, or starts | works: both hold the lock shared (tested with a stand-in for the service) |
| a backup, an enrollment command | works: also shared |
| a migration (`exec --exclusive`), `restore`, `check-idle` | **refused** (`maintenance_lock_busy`, exit 3) until the cleanup ends |
| the cleanup starts while a migration or restore holds the directory | **refused** the same way; nothing is deleted |
| a second cleanup at the same time | safe: see below; the second one finds less to do (on SQLite it waits its turn for each batch) |

It is cooperative like the rest (section 2): it protects nothing against a process that never asks for the lock.

What makes it safe beside live writers is the shape of the work, not the lock. Every batch is **one statement** (`DELETE
... WHERE id IN (SELECT id ... LIMIT 500)`), in **its own short transaction**, and that statement is the transaction's
first: SQLite takes its write lock there and then and waits its turn (up to 30 seconds), so there is no read-then-write
snapshot to go stale, and the lock is held for 500 deletions at a time, not for the whole run. A pause of 50 ms between
full batches lets the web service's writers in. Because a batch decides and deletes in one statement, two runs at the
same time never delete a row twice and never see each other half done (a test runs two at once, and a run beside an
application that is writing, and finds nothing lost or duplicated). The order is the one the foreign keys demand (the
database refuses to delete a message a request still links, so requests go first), and a parent is deleted only when no
child row of it **exists** at that moment: an eligible child that an earlier stage did not delete still holds its parent
back, and the parent waits for the next run. On SQLite, where a statement waits for the write lock and skips nothing,
the stages leave nothing behind and this changes no result. On PostgreSQL (not the deployment; opt-in tests) a batch
skips rows that another transaction holds (`FOR UPDATE SKIP LOCKED`) instead of waiting for them, so a second cleanup
runs without error and without deleting under a row it skipped; what it skipped the next run deletes, and every count
in the report is a row that the run itself deleted. A run that is killed (SIGKILL, power
cut, `docker stop`) loses at most the batch in progress, which SQLite rolls back when it next opens the database;
everything committed before stays.

**The host clock is the only input that decides what is deleted.** A clock set far into the future deletes history early
and irreversibly (except from backups). The host must keep time (NTP); nothing in the cleanup can tell a wrong clock from
a right one.

### 14.4 Scheduling with systemd (opt-in) **[OWNER]**

Two units and an environment template, `deploy/systemd/house-helper-purge.service`, `house-helper-purge.timer` and
`house-helper-purge.env.example`. **Nothing installs, enables or starts them**, `docker compose up` does not run the
cleanup, and the web service does not schedule anything: no scheduler runs inside the application. The service runs
`docker compose ... --profile ops run --rm -T house-helper-ops exec --shared -- python -m house_helper.admin
purge-expired`: the existing operator one-shot, unchanged, with its restrictions (no network, no environment file, no log
driver, read-only root filesystem, no capability, 1 CPU, 256 MB). The report goes to the journal.

| | |
|---|---|
| **Default schedule** | every day at 03:30 UTC (04:30 or 05:30 in the Netherlands), plus up to 5 minutes of random delay |
| **Missed runs** | `Persistent=true`: if the host was off or the timer was not running at that time, one run happens when the timer next starts (never one per missed day) |
| **Overlap** | a oneshot service cannot start while it is running (a start request joins the running job); `TimeoutStartSec=1h` ends a hung run, and the batch in progress rolls back. A manual run beside a scheduled one is safe (section 14.3) |
| **Failure** | the unit fails, visibly (`systemctl --failed`, the journal). **No alert is configured**; deciding where a failure is reported is the owner's |
| **Account** | root, like the Docker daemon it talks to (Docker access is root-equivalent). Another account: a drop-in with `User=` |

Install (once; the three values in `purge.env` are the checkout, the same environment file the web service uses, and
the image tag that is **running**):

```bash
sudo install -d -m 0755 /etc/house-helper
sudo install -m 0644 deploy/systemd/house-helper-purge.env.example /etc/house-helper/purge.env
sudoedit /etc/house-helper/purge.env
sudo install -m 0644 deploy/systemd/house-helper-purge.service deploy/systemd/house-helper-purge.timer /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/house-helper-purge.service /etc/systemd/system/house-helper-purge.timer
```

First run by hand, watched, **before** the timer is enabled (a dry run first, section 14.2, shows what it will delete):

```bash
sudo systemctl start house-helper-purge.service          # returns when the run has ended
systemctl status house-helper-purge.service --no-pager   # Result: success; "status=0/SUCCESS"
journalctl -u house-helper-purge.service -n 30 --no-pager
sudo systemctl enable --now house-helper-purge.timer
systemctl list-timers house-helper-purge.timer           # NEXT is the time of the next run
```

Status, and disabling (the timer only; nothing else changes, and a run in progress finishes):

```bash
systemctl list-timers house-helper-purge.timer
systemctl status house-helper-purge.timer house-helper-purge.service --no-pager
journalctl -u house-helper-purge.service --since '7 days ago' --no-pager
sudo systemctl disable --now house-helper-purge.timer
sudo rm /etc/systemd/system/house-helper-purge.service /etc/systemd/system/house-helper-purge.timer
sudo systemctl daemon-reload
```

**Changing the cadence** (the schedule is the owner's to choose; the cleanup is idempotent at any cadence). Do not edit the
installed file; add a drop-in, whose empty `OnCalendar=` line clears the default first:

```bash
sudo systemctl edit house-helper-purge.timer      # in the editor: [Timer] / OnCalendar= / OnCalendar=*-*-* 04:15:00 UTC
systemd-analyze calendar '*-*-* 04:15:00 UTC'     # when that spelling next fires
sudo systemctl restart house-helper-purge.timer
```

**After every upgrade and every rollback** (sections 9 and 11), set `HOUSE_HELPER_IMAGE_TAG` in `/etc/house-helper/purge.env`
to the tag that is now running, in the same step. Forgetting it fails closed: an image whose schema is not the database's
is refused (`schema_not_head`, exit 4) and nothing is deleted; an image older than R4C-D2 has no such command and the run
fails with a usage error (exit 2).

**Before an upgrade or a restore, stop the timer, and start it again afterwards.** A migration and a restore need the
directory to themselves (section 2), so they are refused while a cleanup is running, and the cleanup is refused while they
run. The cleanup lasts seconds, but if it happens to hold the lock when `deploy/upgrade.sh` reaches its migration (step 3),
the migration is refused and the script reports `the migration FAILED ... do not retry it`, although nothing was migrated:
the refusal (`maintenance_lock_busy`) is in the output just above that line. Do not restore on that message alone; read the
refusal, and run the upgrade again. To rule the clash out:

```bash
sudo systemctl stop house-helper-purge.timer
systemctl is-active house-helper-purge.service     # must print: inactive
# ... the upgrade (section 9) or the restore (section 10) ...
sudo systemctl start house-helper-purge.timer
```

R4C-D2 does not change `upgrade.sh`; making the script tell a refused migration from a failed one is left to a later
decision of the owner.

### 14.5 Troubleshooting

| What you see | Cause | What to do |
|---|---|---|
| `Failed to load environment files` | `/etc/house-helper/purge.env` is missing | install it (14.4) |
| `Dependency failed ... docker.service` | Docker is not running | start Docker; the timer's next run (or `systemctl start`) retries |
| Compose: `env file ... not found` | `HOUSE_HELPER_ENV_FILE` in `purge.env` names a file that does not exist (Compose resolves the whole project, including the web service's env file, before it runs any service) | fix the path |
| `refused [maintenance_lock_busy]`, exit 3 | a migration or a restore holds the directory, or ran a moment ago | nothing is wrong: the next run (the timer has `Persistent=true`) catches up; run it by hand once the work is finished |
| `refused [database_busy]`, exit 3 | another writer held SQLite's write lock for over 30 seconds | run again; if it repeats, find the long writer. Nothing partial is harmful |
| `error [schema_not_head]`, exit 4 | the image tag in `purge.env` is not the running version, or a migration is pending | set the tag (14.4); `hhops check-schema` says which side is ahead |
| `invalid choice: 'purge-expired'` (usage, exit 2) | the tag names an image built before R4C-D2 | set the tag to a current image |
| `error [purge_failed]`, exit 1 | a failure in one stage (the message names the stage and the class of the exception, nothing else) | run `--dry-run`, check the disk (`df`) and the journal of Docker; run again |
| the container of a killed run remains | the Compose client died with it | the procedure of section 2 (`docker ps -a --filter label=com.docker.compose.oneoff=True`, then `docker rm -v <id>`); never `docker volume prune` |
| the timer never fires | not enabled, or the calendar spelling is wrong | `systemctl list-timers --all`, `systemd-analyze calendar '...'` |

### 14.6 Recovery after a failed or interrupted run

Rows deleted by batches that committed are gone and stay gone; the batch in progress is rolled back (by SQLite, when it next
opens the database); the message of a failed run says how many rows were already deleted. The recovery is **to run the same
command again**: it finds what is left and finishes. If the database itself is in doubt (a disk error), take a backup
(`backup.sh` checks integrity and the schema before it publishes anything, section 10); the restore procedure is unchanged.
A deleted row cannot be brought back by the application, only by restoring a backup, and **a restored backup that predates a
cleanup holds the rows that were deleted since**: run the cleanup once by hand after any restore (expired rows are hidden from
every read meanwhile, but not yet deleted).

### 14.7 Timing, and what deletion means

**Timing is a tolerance, not a guarantee.** A row is deleted by the first *successful* run after the moment it expired. With
the default schedule that is within about a day and five minutes of the deadline when every run succeeds, and longer by however
long a failing run goes unfixed. Nothing claims that a row disappears at the instant it turns 365 days old. Reads hide it from
that instant.

**Logical deletion is not forensic erasure.** The rows are gone from every table and index of the live database. What SQLite
does with the space is its own:

- the freed pages go to the free list and are reused by later writes; the file does not shrink;
- the write-ahead log (`house_helper.db-wal`) holds earlier page images until SQLite checkpoints them and later overwrites them,
  so deleted text can remain in it for a while;
- the cleanup switches on `secure_delete` for its own connection, so that the bytes of the pages **it** frees are overwritten
  with zeros in the database file. A test shows the text gone from the file after a checkpoint, and shows that a plain `DELETE`
  with the setting off leaves it. This is a mitigation: it does not reach the log, anything the application's own connections
  deleted earlier, older backups, snapshots of the volume or the disk beneath the file. (Observed, not guaranteed by this
  repository: in the image as built on 2026-10-23, Debian's SQLite 3.46.1, `PRAGMA secure_delete` already answers 1, so the
  application's own deletions overwrite too; a Windows build of Python answers 0. The cleanup sets it explicitly so that it
  does not depend on the build.);
- the cleanup **does not `VACUUM`, checkpoint or replace the database**. `VACUUM` rewrites the whole file, blocks every writer
  while it runs and needs free disk space of the order of the database's size; none of these was shown to be compatible with the running
  service and the backup and restore design, so none is done automatically. A future decision, with its own test.

**Backups.** A deleted row survives in every older backup. The backup retention, the off-host target and encryption are
**undecided (section 10, section 12); R4C-D2 invents no policy for them**, and until the owner decides, the 365-day promise
holds for the live database only. This is a blocker before real data, as section 12 says.

### 14.8 Checking it by hand

```bash
hhops exec --shared -- python -m house_helper.admin purge-expired --dry-run    # what would go; nothing changes
hhops exec --shared -- python -m house_helper.admin purge-expired              # run it
hhops exec --shared -- python -m house_helper.admin purge-expired              # again: "ok [unchanged]", every count 0
hhops exec --shared -- python -m house_helper.admin enrollment-status          # unchanged by the cleanup, except for the invitation line
```

The last command shows the one visible effect on another command: `enrollment-status` reports the latest invitation of each
user, and once an invitation's own two hours are over and a cleanup has run, its row is gone, so the line reads `invitation:
none` instead of `used`, `revoked` or `expired`. The `telegram: linked` part comes from the allowlist and does not change.

### 14.9 Before this counts as running: checklist **[OWNER]**

- [x] The independent audit of R4C-D2 is accepted and the code is released in Git (`489ff30`).
- [ ] An image built from a commit that contains `purge-expired` is the running version, and `purge.env` names its tag.
      (The running image, `house-helper:6370bf78980e`, was built from a later commit that contains the command; what
      `purge.env` names was not reported.)
- [x] The database is at the schema head (D2 adds no migration: `0006_oidc_state_invitation`). Reported 2026-10-24.
- [ ] A `--dry-run` was read and the numbers made sense, first on a disposable database, then on the real one.
- [ ] The host's clock is synchronised (14.3).
- [x] The timer: installed and enabled as in 14.4, its first calendar-triggered run (2026-10-25) read in the journal and
      accepted, and `list-timers` showing the next one. Reported 2026-10-25. (The first run by hand that 14.4 suggests
      before enabling was not reported; the gates in section 12 ask for the first automatic run.)
- [ ] Where a failed run is reported is decided (nothing is configured: 14.4).
- [ ] Sections 9 and 11 are followed with the extra steps: the timer stopped before an upgrade or a restore and started
      after (14.4), the tag in `purge.env` changed (14.4), and a restore rehearsal includes "run the cleanup afterwards"
      (14.6).
- [ ] The backup retention, off-host copy and encryption are decided (14.7); without them the deletion promise is for the live
      database only.
- [ ] What the users are told about retention matches 14.7: deletion within about a day of the 365 days, older backups, and
      OpenAI's own settings.

Reported state, 2026-10-25: the timer is installed and active and its first calendar-triggered run was verified and
accepted by the owner. The timer started the service at 03:34:11 UTC and it completed at 03:34:14 UTC with exit 0 and
SUCCESS; it found no expired record and deleted nothing; the SQLite integrity check and the foreign-key check passed; the
schema is `0006_oidc_state_invitation`, the production image is `house-helper:6370bf78980e`, `/healthz` returns 200, no
systemd unit is failed, and the next activation is 2026-10-26 03:32:34 UTC. Nothing was run on the host to write this
down. The timer item above is ticked for that and for nothing more. The other unchecked items were not reported as done
and stay open. Two of them are separate pending work and not part of this acceptance: where a failed run is reported
(nothing is configured, so a failed run raises no alert) and the backup retention, off-host copy and encryption (section 15).

## 15. Encrypted off-host backup (`age`, `rclone`, Yandex Disk)

**Status: the repository side only.** `deploy/offsite_backup.py`, the `sanitize` command of `dbtool`, three systemd
templates and their tests exist in the working tree. **Nothing is installed, scheduled or deployed; no key pair exists; the
tool has never run against a real Yandex Disk account or a real `age`, and its `rclone` commands have run only offline
against a real, pinned stable `rclone` (15.12.6)** (the tests use fakes for those three and the real `dbtool` on synthetic SQLite; section 15.9 lists what is still to be done and by whom). Every step marked
**[OWNER]** changes something outside the repository.

Two additions of Task A1 (2026-10-25), both repository code and neither installed, configured or activated: a **temporary
full-disk access mode**, for a stable `rclone` whose Yandex backend has no `app_folder` option and an OAuth token the owner has
authorised to reach the whole Disk (15.12; owner decision, KHD-33; **off by default**), and `deploy/offsite_rehearsal.py`, the
restore rehearsal for the owner's own machine (15.13). No OAuth credential and no private key exists in the repository. The tool's
rclone commands have now also been run **offline against a real, pinned stable rclone (1.75.2)**, which corrected one assumption
(15.12.6); they have still never run against a Yandex Disk account.

### 15.1 What a run does

`python3 deploy/offsite_backup.py run ...` (the unit starts exactly this) does six things, in this order, and stops at the
first that fails:

1. **Backup** with `deploy/backup.sh` (unchanged), into a private 0700 directory under `/run/house-helper-offsite`: SQLite's
   online backup, checked and verified by a disposable container, published as a `.db` / `.db.sha256` pair. The tool re-checks
   the pair byte for byte (the exact one-line rule of section 10; not `sha256sum -c`).
2. **Rewrite** the snapshot with `dbtool sanitize`, in a disposable container with no network, a read-only root, no capability
   and no volume of the data (the same isolation as the verification inside `backup.sh`). SQLite does not erase a deleted
   row: it stays in the freed page or in the slack of its page, and the online backup copies those pages as they are. The
   rewrite is `VACUUM INTO` on the **snapshot** with `secure_delete` on for that connection: a new file from the live rows only.
   The live database is never opened by it, vacuumed or reconfigured. The result must pass the checks of a backup (integrity,
   foreign keys, this application's schema, at the head), hold exactly the tables and row counts of its input, and have no free
   page; its SHA-256 on the host must equal the one the tool reported.
3. **Verify** the rewritten file in a second disposable container (`dbtool verify --require-head`).
4. **Pack** one tar of three files: `MANIFEST.json` (format name, UTC time, image tag, the snapshot's size and SHA-256: no host,
   no path, no count, no content), `snapshot.db.sha256` (the one-line format of section 10) and `snapshot.db`. The tar is read
   back completely before it is encrypted.
5. **Encrypt** with `age -R <recipients file>`: public keys only. A recipients file that contains a private key
   (`AGE-SECRET-KEY-...`) is refused before anything runs. The private key is never needed on the VPS.
6. **Upload, read back, rename, commit.** `rclone copyto` uploads to `house-helper-<UTC time>-<8 hex>.tar.age.partial` in the one
   configured folder (the folder was listed first and a name that exists stops the run; `--immutable --ignore-existing` is the
   second line: an existing object is never overwritten, and **`--ignore-existing` is the flag that does it**: the real stable
   rclone 1.75.2 overwrote a different existing object under `--immutable` alone, 15.12.6); the folder listing must show the same size **and an MD5** that equals
   the local file's (Yandex Disk lists MD5 and nothing else; a listing without an MD5 stops the run here, with nothing renamed);
   the object is downloaded again and its **SHA-256** must equal the local file's; only then is it renamed to `...tar.age`
   (`rclone moveto --ignore-existing`: a rename never replaces; a destination that appeared since the folder was listed leaves
   the partial where it was, fails the run and is itself left untouched) and the listing checked once more. **That name is not yet a recovery point** (a name is not proof: a stop, a
   failure or an unreadable answer after the rename leaves a final-looking object nobody has confirmed). The **commit** is a
   second, small object, `...tar.age.commit` (the commit record: the object's name, size, SHA-256, SHA-1 and MD5; no content, no
   key, no path), uploaded and read back byte for byte. **A recovery point is an object together with a valid record whose size
   and MD5 are the size and MD5 the listing reports for it. Nothing else is one**: not a name of the right shape, and not an
   object of the right size whose listing gives no MD5 or another one.
7. **The final check.** The folder is listed once more and classified exactly as above, whatever the run has just done to it. If
   that cannot be done (the listing or a record cannot be read), or it does not show the point this run committed as a committed
   one (its record is gone or damaged, the object changed or vanished, the listing gives no MD5), the run ends with **exit 7**,
   deletes nothing and does **not** call the point stored. Only after a passed final check does retention run.

Then, only after the final check, **retention** (15.3). A run never starts it on a failure.

**The closing listing.** After retention (or straight after the final check, when retention had nothing to do) the run lists the
folder **once more, as the very last thing it does with the remote**, and decides its exit status from that listing for leftover
`.partial` objects: every `...tar.age.partial` of the tool's shape in it is logged as `LEFTOVER PARTIAL <name>` (with its size and
MD5, and whether the first listing of the run had already shown it) and makes the exit status **6**, **even though the new point was
committed and everything else went well**. This is what finds an object that appeared *after* the first listing, while the run was
uploading, renaming or applying retention, which no earlier listing shows (the final check and the listings of the retention only
classify the objects with a final name). If the closing listing cannot be taken (rclone fails, the answer is not JSON, any
unexpected error) the run ends with **6** as well, never 0, says so, and still names the partials the first listing had shown. It
deletes nothing. It is an observation at one moment, not a guarantee: an object that appears after it is seen by the next run. A
run that ended with exit 7 takes no closing listing (its status was decided by the final check), and one that failed earlier ends
with its own failure status; both are already not a success.

What each later run can and cannot say about a point: it re-checks the **size and the MD5** that the listing reports against
the record (every run, for every point, so that a point changed after its commit is noticed). The **SHA-256** in the record was
verified once, by the read-back at upload; no later run reads the object again, so none claims to have re-verified it. The MD5
binds an object to its record against corruption and against a changed or replaced object; it is not a defence against someone
who can rewrite both the object and its record, and anyone with the token can delete everything anyway (15.9). The real test of a
point is the restore rehearsal of 15.6.

An object with a final name but **no valid, matching record** is **unresolved**: it is not counted, not relied on by retention, and
**never deleted by the tool** (it may be a good copy, and only a person can decide). It takes **no retention slot**: the plan is
made from the committed points alone, so a damaged or unverifiable newest point never pushes a valid older one out. And **while
any unresolved object is in the folder, retention drops no committed point at all**, however old: a final-looking object the
tool does not understand may be the copy that is needed, so the tool does not shrink the history around it. New points are still
made, encrypted and verified; the run says `retention holds back N committed point(s)` and ends with exit 6 for as long as the
object exists, and the folder grows by one point per run meanwhile. Once an operator has dealt with the object (15.11) the next
run prunes as the plan says. Every run reports it (exit 6, a line `UNRESOLVED <name>: <why>`) and so does `list`, until an
operator has looked at it (15.11). A record whose object is gone (the half of a deletion that was interrupted) is an orphan:
retention removes it, within its budget, whether or not an unresolved object exists. A leftover `.partial` object is not
handled like either: it is never deleted by the tool and never holds retention back (15.2, 15.11).

**Before step 1, and refused with status 3 if any of it is not as required** (no command of the backup has run yet): the
recipients file holds only public keys; the rclone config section of the remote (`NAME` in `--remote NAME:FOLDER`) is
`type = yandex`, has `app_folder = true`, and has a `client_id`, a `client_secret` and a `token` of its own (rclone's
built-in application is shared and its access is wider); this rclone **knows the Yandex `app_folder` option** (read from
`rclone config providers`, not guessed from a version number); and the remote **renames server-side** (`rclone backend
features` of the dedicated folder, never of the remote's root: `Move`), because step 6's commit is a rename. The tool never falls
back to another mode: an rclone or a config that does not meet this is not run, and the message says what is missing. No value
of the config file is ever printed. **That is the default mode (`--access-mode app-folder`).** The one exception, explicitly
requested and temporary, is `--access-mode full-disk-temporary` (15.12): the config must then NOT say `app_folder = true`, the
option is not required of the rclone, the remote must be exactly `NAME:house-helper-backup`, and the config file must sit where
only root can change it. `run` and `list` make exactly the same offline authorization, in one function, before anything else and
before any rclone command.

### 15.2 What is promised, and what is not

- **A failed run deletes nothing on the remote, and no run ever deletes a `.partial`.** The only remote objects a run
  deletes are, after a commit, the points retention drops (the object, then its commit record) and orphaned records.
  **An unresolved object is never deleted by the tool**, and while one exists retention drops no committed point (15.1; only the
  orphaned records are still removed). All deletes go through one function that accepts only a name of the tool's own shape
  (`house-helper-<time>-<8 hex>.tar.age[.commit]`; it refuses a `.partial` too) inside the one folder, one object at a time
  (`rclone deletefile --yandex-hard-delete NAME:FOLDER/<exact name>`). The tool never runs `delete`, `purge`, `sync`, `move` or
  `cleanup`, so it can neither remove anything else in the folder nor empty the account's Trash. Files in the folder that are
  not of that shape are reported and left alone.
- **Why a `.partial` is never deleted, not even the one of a run's own failed upload.** The name `...tar.age.partial` proves
  nothing about whose object it is: it may be the fragment of a hard kill, the half of an upload that stopped, an object another
  writer put there, or the upload this very run made and which was replaced afterwards. `rclone deletefile` deletes by name and
  has no "only if it still has these bytes", so a check made a moment before cannot rule out that the delete removes something
  else. The tool therefore leaves every `.partial` untouched, **logs each one it sees** (`LEFTOVER PARTIAL <name>`), makes
  every run that commits a point end with **exit 6** while one exists (`list` shows them too), and leaves the removal to a
  person who has looked (15.11). **The cost, stated plainly:** a run that failed after its upload had returned (the read-back,
  the rename or a listing failed, or a signal came), and a hard kill during or after the upload, leave one `.partial`, and
  every later run reports it until the operator has dealt with it. It is never counted as a point and never holds retention
  back. A `.partial` that was already there when a run started is just as untouched; the run's own upload has a name of its own
  (a name that is already taken stops the run before anything is uploaded). For the same reason the object under the run's
  **final name** is not deleted when the listing right after the rename shows that it is not the file that was read back (the
  rename may have damaged it, or an object that replaced the partial in between may have been renamed instead, and nothing tells
  the two apart): it gets no commit record, so it is `UNRESOLVED` (15.1) and is dealt with by hand (15.11).
- **A delete is believed only when the folder shows it.** After every group of deletes the tool **lists the folder again** and
  requires each object to be absent. An exit status of 0 from `rclone deletefile` on an object that is still listed is not a
  prune (the run ends with exit 6 and says what remains); an error status on an object that is no longer listed is not a
  failure. A commit record is deleted only after its object has been proven gone, so a point whose object could not be removed
  stays whole and is retried by the next run.
- **Trash.** Yandex Disk puts a deleted file in its Trash unless a hard delete is requested; the tool requests it for every
  delete. Whether the provider honours it is checked in the rehearsal (15.9), not assumed.
- **Plaintext on the host.** The unsanitized pair, the rewritten snapshot and the tar exist, in that order, only in the private
  directory, each removed as soon as the next stage has what it needs, the whole directory removed on every exit path including
  `SIGTERM`, `SIGINT` and `SIGHUP`, and the leftovers of a killed run (`SIGKILL`, power) swept at the next start. **This is
  unlink, not erasure.** The unit puts the directory on tmpfs (`RuntimeDirectory=`), which keeps it off the disk apart from
  swap; the disposable containers use an anonymous Docker volume for scratch space that Docker deletes with the container. Do
  not read any of it as forensic secure deletion. **A directory that cannot be removed is never a success:** after a committed
  backup the run ends with exit 6 (the point is stored; the warning names the directory), after a failure its own failure
  status stands, and the **next run refuses to start (exit 3, nothing done)** while any leftover directory of the tool cannot be
  removed, so plaintext is not piled up next to plaintext (15.11 says what to do). Directories of the tool owned by another
  user are not touched, and stop the run in the same way.
- **A normal stop and a hard kill are different, and only the first runs the clean-up.** A failure, `SIGTERM`, `SIGINT` and
  `SIGHUP` (what `systemctl stop` and a timeout send first) run the clean-up described above: plaintext removed, and the
  log names the run's own `.partial` if one is left (it is **not** deleted, 15.2). `SIGKILL` (what systemd sends if the clean-up
  does not finish within `TimeoutStopSec`, the kernel's
  out-of-memory killer, `kill -9`) and a power cut run **nothing**. What they leave, and what happens to it: (a) the private
  directory with whatever plaintext stage the run was in, until the **next run's start** removes it (or the unit's stop, which
  removes `/run/house-helper-offsite` itself, or a reboot, which empties tmpfs); (b) at most one `...tar.age.partial` on the
  remote, which **no reader can mistake for a point** (a point is an object with a valid commit record, and a `.partial` has
  neither the name nor a record), which **no run removes** (every later run logs it as `LEFTOVER PARTIAL` and ends with exit 6
  until an operator has removed it, 15.11); or, if the kill came **between the rename and the commit
  record**, one final-named object **without a record**, which is **unresolved**: no run counts it, relies on it or deletes it,
  and every run reports it (exit 6) until an operator has looked at it (15.11); (c) nothing else: no lock stays (the kernel
  drops it), no older point is touched, nothing is half-deleted (a point being dropped by retention loses its object first and
  its record second, so an interruption leaves at worst an orphaned record, which the next run removes). This is tested with
  a real kill of a child process at each stage, the rename-to-record gap included.
- **Nothing private in the log.** Lines carry step names, object names, sizes, the checksum of the **encrypted** object and
  counts. The output of `age` and `rclone` is **not** forwarded (rclone's can name paths and accounts): the log says which
  command failed and its exit status; run the same command by hand to read its own message. The progress lines of `backup.sh`
  and `dbtool` (written to be log-safe: table names and row counts, never content) are forwarded.
- **One run at a time** (a lock file in the work directory, and systemd's own oneshot rule).
- **What it does not do.** It does not alert (15.7), does not test that the private key works (15.6 does), does not re-read
  points after they were committed (each run only re-checks the size and the MD5 the listing reports against the record; the
  SHA-256 was verified once, at upload), and does not touch the local backups of `backup.sh` or `upgrade.sh` (those are not
  rotated by anything).
- **Deleted conversations are bounded by the retention, not eliminated.** The live purge (section 14) removes expired rows
  from the database and the rewrite keeps them out of every new point, but a point made **before** a row was deleted still
  holds it until retention drops that point: **up to about 36 days** with a daily schedule (15.3). That is the real tail of the
  365-day promise for the off-host copy, and a number to tell the users the same way section 14.7 describes the rest.

### 15.3 Retention: 7 daily and 4 weekly

Of the recovery points in the folder (committed ones only: an object with a valid commit record whose size and MD5 equal the
listing's; unresolved objects, which take no retention slot, and everything not of the tool's shape are neither counted nor
touched), **keep**

- the newest point of each of the **7 most recent UTC days** that have one (daily);
- the newest point of each of the **4 most recent ISO weeks** whose newest point is not already kept as a daily one (weekly);
- always the point this run just committed.

That is at most 11 points. It counts points, not wall-clock time; the newest point is the newest of its day and so is always
kept; two runs on one day keep one; and a schedule that skips days keeps a longer span. With a daily run the oldest point kept
is at most about 36 days old (a Monday: the Sunday five weeks before it).

**The strict rule: no pruning while an unresolved object exists.** If the folder holds any `UNRESOLVED` object (15.1), the plan
above is **not applied**: **no committed point is dropped**, whatever its age, and the log says `retention holds back N committed
point(s)`. The run still makes and verifies its new point, and still removes orphaned commit records, and it ends with
exit 6 (15.7) so that the condition is never only a log line (a leftover `.partial` is never removed by it, 15.2). The unresolved object is not deleted or
trusted. The hold lasts until an operator quarantines or deletes the object (15.11); the points that piled up meanwhile are
then dropped by the next run, and if that is more than `--max-deletions` allows (below) pass a larger limit **once**.

Guards: retention runs only after a committed point that the final check (15.1, step 7) has confirmed; if the folder, listed
again, does not show the new point as committed, nothing is deleted and the run ends with exit 7; **`--max-deletions` (default 5)
is one budget for the whole run**, used by the points retention drops (an object and its record count once) and orphaned
records; a `.partial` is never deleted and so never draws on it. A run whose retention would exceed it **deletes nothing, exits
6 and says so**: the steady state
deletes one or two a day, so a larger number means the folder is not as expected, or retention is being applied for the first
time to a long history. After checking `list` (below) pass a larger limit **once**.

**The clock is checked, because the names carry it.** Retention orders points by the time in their names, which is the host's
clock when they were made. It deletes **nothing at all** in a run, and ends with exit 6 saying why, if (a) a committed point
is dated **more than an hour later than the host clock** (the clock was wrong when it was made, or is wrong now); (b) this
run's point is dated **more than 7 days after the previous one** (the host did not run that long, or the clock jumped forward;
the next run is again one day after this one and proceeds); or (c) the clock moved forward by **more than 3 hours during the
run**, or back far enough that this run's own point is dated in the future. A future-dated point **takes no daily or weekly
place from a real point and is not deleted**: it is reported by every run and by `list` as `SUSPECT` until an operator has
dealt with it (15.11), so it is neither silently kept for ever nor allowed to push real points out. A clock that is wrong by
the same amount for every run from its first wrong run is caught only at that first run (rule b); once the clock is right
again the points it dated are future-dated points (rule a). There is deliberately no option that overrides the guard.

```bash
python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper
```

`list` is read-only (one `rclone lsjson`, and one `rclone cat` of each commit record): it prints every committed point as `keep`
or `drop` (`hold` instead of `drop` while an unresolved object holds retention back: a run would delete none), then separately the `UNRESOLVED` objects with the reason, the `SUSPECT` future-dated points, the orphaned records
and how many other objects it would leave alone. It also proves that the token, the remote and the folder work. It exits 0
whatever it finds: read it.

### 15.4 Prerequisites **[OWNER]**

1. **The key pair, made on the owner's own machine, never on the VPS and never inside a checkout of this repository.** Choose a
   directory that is not under the repository, not on the VPS, not in n8n and not in a synchronised project folder, give it to
   yourself alone, and write the key there by an **absolute** path:

   ```bash
   umask 077 && mkdir -p "$HOME/keys/house-helper" && age-keygen -o "$HOME/keys/house-helper/age-key.txt"
   ```

   `age-keygen` prints the **public** key (`age1...`) and writes the private one only to that file. **Never run it with a bare file
   name from inside the checkout:** `age-keygen -o age-key.txt` there would put the private key into the working tree, one
   `git add .` from the history. (`.gitignore` refuses the usual key names and `tests/test_repo_secrets.py` fails the build if a
   file that looks like a key is tracked or not ignored; both are a second line of defence, not the procedure.) Keep the
   private file where a loss of the VPS, the laptop or one cloud account does not lose it (a password manager **and** an offline
   copy); without it every off-host point is unreadable. Put the **public** key line (`age1...`) in
   `/etc/house-helper/age-recipients.txt`. A second recipient (a spare key kept elsewhere) is supported: one key per line, at most
   eight. The production tool refuses a recipients file that contains a private key, and the restore rehearsal (15.13) refuses an
   identity file that sits inside the checkout, is readable by other users, or is used on the production host.
2. **A Yandex OAuth application of the owner's own, with only the permission "access to the application folder"**
   (`cloud_api:disk.app_folder`), so that the token cannot read or delete anything else on the disk. **Do not use rclone's
   built-in client**: it is shared and its scope is wider. A Yandex Mail account is required (rclone's documentation: without one,
   a token is issued but every action answers `403 DiskUnsupportedUserAccountTypeError`). (A token with permissions beyond the
   application folder is not what this item asks for; the owner's limited, temporary decision to use one is 15.12.)
3. **A stable `rclone` that has the Yandex `app_folder` option: stable 1.75.2 does not; the first stable release planned
   to is 1.76.0 (owner's information; the option itself was merged into rclone's development branch on 2026-09-08, pull request
   9850).** The Yandex backend addresses `disk:/` by default, and a token restricted to the application folder is refused
   there with `403`; `app_folder = true` makes it address `app:/`. What this repository can check, and does, **at every
   start**: the capability, not the version number (`rclone config providers` must list `app_folder` among the Yandex
   options, and the tool refuses otherwise: checked against the real 1.75.2, which lacks it); the config (above, 15.1); and
   that the remote can rename server-side. **Minimum compatible: a stable rclone release whose Yandex backend lists
   `app_folder`; 1.76.0 is expected to be the first, to be confirmed from its release notes and by `list` before it is
   relied on.** What it cannot check is the **scope of the token**: that is the owner's OAuth application, and the proof is
   the rehearsal of 15.5 (with `app_folder = false` set by hand for one command, the same token must be refused with `403`;
   then set it back). **Never install a development or beta build on the VPS to get the option early**, and never use
   rclone's built-in application or a token with full disk access to get past a refusal: an rclone that cannot be made to
   work with the application-folder scope means this design waits, or **is decided again by the owner**. **The owner has decided
   it again, for a limited time and only under the conditions of 15.12 (KHD-33):** the explicit `--access-mode full-disk-temporary`.
   Outside that mode this item stands as written, and rclone's built-in application is refused in both modes.
4. **`age` and `rclone` installed on the VPS** (`apt-cache policy age` shows whether Ubuntu 24.04's archive has it), and
   Python 3 (Ubuntu 24.04 has 3.12; the tool needs 3.10 or later and no packages).
5. **A running image that knows `sanitize`**: built from a commit that contains this change and deployed through the normal
   upgrade (section 9). An older image answers `invalid choice: 'sanitize'` (exit 2) and the run fails before anything is
   uploaded. The tag in `/etc/house-helper/purge.env` must be the **running** one.
6. **The folder name.** The configuration takes `NAME:FOLDER`, a folder of its own inside the application folder (for example
   `yandex-hh:house-helper`). A bare `NAME:` is refused so that a mistake cannot aim retention at the root. (In the temporary
   full-disk mode the folder is not a choice: it is exactly `NAME:house-helper-backup`, 15.12.2.)
7. **Room.** The work directory is tmpfs under `/run`, which is limited to a fraction of memory (`df -h /run`). A run holds
   about twice the database at any moment (three times is checked up front and the run fails cleanly if it is not free). That
   is far below any limit for two users; look again if the database approaches the size of that tmpfs.

### 15.5 Installing it (opt-in) **[OWNER]**

The rclone config file (made on a machine with a browser by `rclone config`, then copied) holds one section. These are the
settings the tool requires; the values of `client_id`, `client_secret` and `token` are the owner's and are never put in the
repository:

```ini
[yandex-hh]
type = yandex
client_id = <the id of the owner's own Yandex application>
client_secret = <its secret>
token = <written by rclone config after the browser consent; the consent screen must say "application folder" only>
app_folder = true
```

```bash
sudo install -m 0644 deploy/systemd/house-helper-offsite.env.example /etc/house-helper/offsite.env
sudoedit /etc/house-helper/offsite.env                          # the three values; the tag and checkout stay in purge.env
sudo install -m 0644 /dev/null /etc/house-helper/age-recipients.txt && sudoedit /etc/house-helper/age-recipients.txt   # the PUBLIC key
sudo install -m 0600 -o root /path/to/rclone.conf /etc/house-helper/rclone.conf        # the access token: root only
sudo install -m 0644 deploy/systemd/house-helper-offsite-backup.service deploy/systemd/house-helper-offsite-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/house-helper-offsite-backup.service /etc/systemd/system/house-helper-offsite-backup.timer
```

First, by hand and watched, **before the timer is enabled**. `list` proves that the token, the remote and the folder work
(nothing is uploaded; an empty folder is a normal answer, a missing one is made by the first `run`):

```bash
cd ~/apps/house_helper                                          # the checkout
sudo python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper
sudo systemctl start house-helper-offsite-backup.service          # returns when the run has ended
systemctl status house-helper-offsite-backup.service --no-pager   # Result: success; "status=0/SUCCESS"
journalctl -u house-helper-offsite-backup.service -n 40 --no-pager
```

**Prove the scope of the token** once, on a copy of the config (not the installed file): set `app_folder = false` in the copy and
run `rclone --config the-copy lsd NAME:`; it must be **refused with 403**. If it lists anything, the token is not restricted
to the application folder: stop, delete that token at Yandex, and register the application again with only the
application-folder permission. (This is the one thing the tool cannot check for itself, and it is why it also refuses
an rclone config without the owner's own client id and secret.)

Then the restore rehearsal (15.6) with the private key, on a machine that is not the VPS. **Only after it worked:**

```bash
sudo systemctl enable --now house-helper-offsite-backup.timer
systemctl list-timers house-helper-offsite-backup.timer           # NEXT is the time of the next run
```

Disabling (the timer only; a run in progress finishes): `sudo systemctl disable --now house-helper-offsite-backup.timer`.
The default is 04:15 UTC, after the cleanup (03:30 UTC), so that a point is made from the database as the cleanup left it;
change it with a drop-in as the comment in the timer file says.

### 15.6 Restoring from an off-host point **[OWNER]**

On the machine that holds the private key (not the VPS). **Take a point that `list` shows as `keep` or `drop` (a committed
point), not an `UNRESOLVED` or `SUSPECT` one**: only a committed point has had its bytes read back and its record written. Of
two committed points take the newest unless you are going back on purpose.

```bash
# fetch house-helper-<time>-<id>.tar.age from the application folder (the Yandex web interface, or rclone copyto)
age -d -i "$HOME/keys/house-helper/age-key.txt" -o recovery.tar house-helper-20261025T041507Z-0a1b2c3d.tar.age
python3 deploy/offsite_backup.py unpack recovery.tar --output-dir ./recovered
```

(Do this in a scratch directory **outside the checkout**: `recovery.tar` and `recovered/` are plaintext. `.gitignore` refuses those
names, but the place is yours to choose. 15.13 does the fetch, the comparison with the commit record, the decryption and the checks
of the archive and the database in one command, and removes everything afterwards; this manual form is for restoring.)

`unpack` refuses a tar that is not exactly what the tool writes (three named regular files, a manifest of the known shape, the
checksum file exactly the one line for `snapshot.db`, the snapshot equal to its manifest), extracts only those three files by
fixed names, never replaces a file, and removes what it extracted if any check fails. It prints the image tag that wrote the
point. `./recovered/snapshot.db` and `snapshot.db.sha256` are a backup pair as section 10 defines it: copy **both** to the VPS
and restore with that section's procedure (the exact checksum check, stop the service, `check-idle`, `restore` with the image
of the manifest's tag, `check-schema`, start). **Run the cleanup once after any restore** (section 14.6): a point predating a
cleanup holds rows that were deleted since. A restore rehearsal onto a scratch volume, with the date recorded, comes before
the timer is relied on.

If no committed point is left (everything else is lost), an unresolved object may still be the best copy there is. Treat it
the same way: `age -d` authenticates every chunk of the file, so a decryption that succeeds and an `unpack` that reports
"intact" show that the content is whole; what they cannot show is that the tool's own checks ran, so say so when you restore from it.

### 15.7 Statuses and troubleshooting

Exit status: **0** stored, verified, final check passed, retention done, the closing listing taken and showing no leftover `.partial`, nothing needing review. **1** failed: no new point, and
nothing of another run touched (the log says which step); an unconfirmed object of this run may remain **unresolved** (15.11).
**2** usage. **3** refused before anything was done (configuration, a missing tool, or a leftover work directory that cannot be
removed). **6** the new point **is stored and verified** (its SHA-256 was read back before the commit, and the final check of
the folder matched its size and MD5 to its commit record), but something needs attention: housekeeping did not complete (a
listing or a delete failed or could not be confirmed, retention would have exceeded `--max-deletions`), a leftover **`.partial`**
object is in the folder (never deleted by the tool, 15.11; it may have appeared while the run was in progress: the **closing
listing** that ends every run finds it), the closing listing itself could not be taken, the work directory of this run could not be removed, an **unresolved** object or a **suspect**
(future-dated) point is in the folder (while an unresolved one exists retention also drops no committed point), or the clock is
implausible so retention deleted nothing. Extra objects remain, which is the safe side, and **the run fails again in every run in which the cause persists**: it is never only a log line. **7** the point
is **not confirmed**: this run wrote the object and its commit record, but the final check (the folder listed and classified
again afterwards) could not be completed or does not show the point as committed (its record is gone or damaged, the object
changed or vanished, the listing gives no MD5). It is never described as stored or verified, nothing was deleted, and the
operator runs `list` before anything else (15.11). **128+N** a signal (the clean-up ran). A failed unit shows in `systemctl
--failed` and the journal; **no alert is configured** (15.9).

| What you see | Cause | What to do |
|---|---|---|
| `Failed to load environment files` | `purge.env` or `offsite.env` is missing | install them (15.5) |
| `refused: ... is not installed` | `age`, `rclone`, `docker` or `bash` is not on the unit's `PATH` | install it |
| `refused: this rclone has no app_folder option` | the rclone is older than the first release with the Yandex application-folder mode (1.75.2 and earlier) | install a stable release that lists it (15.4 item 3); nothing was done |
| `refused: the remote [NAME] does not set app_folder = true` / `... has no client_id and client_secret of its own` / `... is not a Yandex Disk remote` | the rclone config is not an application-folder remote of the owner's own OAuth application | fix the config (15.5); never work around by widening access (the explicit temporary exception is 15.12) |
| `refused: ... sets app_folder, which contradicts --access-mode full-disk-temporary` | the config asks for the narrow mode and the unit for the wide one | decide which; 15.12.2 |
| `refused: in the full-disk-temporary mode --remote must be NAME:house-helper-backup exactly` | the one fixed folder of that mode is the only one accepted | 15.12.2 |
| `refused: in the full-disk mode ... --rclone-config ... (owned by root / writable by group or others / symbolic link / single link / no access at all)` | the OAuth configuration is not where only root can change it | 15.12.4: fix the owner, the mode or the directory; nothing was done |
| `usage: argument --access-mode: invalid choice` (exit 2) | the value in `offsite.env` is empty, misspelled or not one of the two | 15.12.2; nothing was done |
| `FAILED: the rename did not take place: an object named ... that this run did not make appeared in the folder` | something else created the final name between the listing and the rename; `--ignore-existing` left it alone | 15.11: look at the folder, the object is not touched; this run's own `.partial` stays too (the log names it) |
| `refused: the remote cannot rename server-side` | the commit would not be atomic | do not run; decide again (15.4 item 3) |
| `refused: --recipients-file ... PRIVATE key` | a private key is in the recipients file | remove it, **treat that key as exposed if it was ever on the VPS** |
| `FAILED: deploy/backup.sh failed` | read the `\|`-prefixed lines above it (`backup.sh`'s own messages) | as section 10 |
| `FAILED: dbtool sanitize failed (exit 2)` | the tag names an image older than this feature | deploy a current image, set the tag |
| `FAILED: dbtool sanitize failed (exit 4)` | the snapshot is not sound / not at the head / not this schema, or `sanitize_mismatch` / `sanitize_incomplete` (the rewrite did not do its job; nothing was released) | read the `\|` line; do **not** bypass |
| `FAILED: rclone ... failed (exit N)` | the remote, the token or the network | run `rclone --config /etc/house-helper/rclone.conf lsjson NAME:FOLDER` by hand and read its message |
| `FAILED: the object read back from the remote is not the file that was uploaded` | the provider returned other bytes | nothing was committed; run again; if it repeats, stop and tell the owner |
| `FAILED: another off-host backup is running` | the lock is held | wait for it; nothing was done |
| `exit 6 ... IS stored and verified` | the final check passed (the point is committed and its size and MD5 match its record); something after it needs attention, which the same line names | `list`; fix the cause; run again (or once with a larger `--max-deletions`) |
| `exit 7 FAILED: the final check of the folder did not confirm the recovery point ...` and `NOT reported as stored` | the record or the object was written but the folder, listed again afterwards, does not show a committed point (the line says why: record missing / damaged, object changed or gone, no MD5 listed, or the listing or a record could not be read); nothing was deleted | 15.11: `list` first. If it shows the point as committed, the next run counts it; if `UNRESOLVED`, treat it as such. Do not rely on this run's point until `list` says so |
| `FAILED: the remote's listing gives no MD5 for the object` (exit 1) | the remote did not list an MD5 for the uploaded object, so it can never be tied to a record; nothing was renamed | run `rclone --config /etc/house-helper/rclone.conf lsjson --hash NAME:FOLDER` by hand: every object must show an MD5. If it does not, stop and tell the owner (15.9) |
| `FAILED: ... nothing can tie the renamed object to a commit record, so none was written` (exit 1) | the listing right after the rename gave no MD5 | the object was read back before the rename but has **no record**: `UNRESOLVED` (15.11) |
| `WARNING: <name>, listed right after the rename, is not the file this run read back ... it is NOT deleted` (exit 1) | the listing after the rename shows another size or checksum than the file that was read back: the rename damaged it, or an object that replaced the partial in between was renamed instead; nothing tells the two apart | the object has **no record**, so it is `UNRESOLVED` and not deleted (15.2): look at it (15.11) |
| `WARNING: could not remove the work directory` (run ends 6, or with its own failure status) | the clean-up could not remove plaintext | remove it by hand **before the next run, which refuses to start while it exists** (15.11; `/run/house-helper-offsite` is tmpfs: a reboot clears it) |
| `refused: N leftover work directories ... cannot be removed` (exit 3) | a leftover of an earlier run holds plaintext and cannot be removed by this user | 15.11; nothing was done |
| `WARNING: <name>.partial was uploaded by this run, which ended before the rename. It is LEFT in the folder untouched` (the run failed, exit 1 or 128+N) | the upload had returned, then the read-back, the rename or a listing failed, or a signal came; the tool never deletes a `.partial` (15.2) | 15.11: look at it, then remove it by hand; until then every run reports it |
| `LEFTOVER PARTIAL <name>: left untouched` (exit 6 after a committed run; `list` shows `LEFTOVER PARTIAL <name> (N bytes)`) | a `.partial` is in the folder: the fragment of a hard kill, of a stopped upload or of a failed run, or somebody else's object. The line from the closing listing ends `NOT in the listing taken before this run's upload: it appeared while the run was in progress` if it came in during the run | 15.11: look at it (who writes to the folder, if it appeared during the run?), then quarantine or remove it by hand; the exit 6 stops when it is gone |
| `the closing listing of the folder could not be taken` (exit 6, with `IS stored and verified`) | the last listing of the run failed or was unreadable, so whether a `.partial` is in the folder now is not known | run `list`; the point is stored, but this run is not called healthy and sends no success ping (15.10); the next run looks again |
| `FAILED: rclone lsjson failed ... while confirming the rename` | the rename was issued but its result could not be read | the object was read back before it was renamed but has **no commit record**: it is `UNRESOLVED` (15.11); the next run reports it |
| `FAILED: ... its commit record is not confirmed, so it is NOT a recovery point` | the record could not be written or read back | the object is left untouched and is `UNRESOLVED` or, if the record did arrive whole, a committed point from the next run on; `list` shows which (15.11) |
| `UNRESOLVED <name>: <why>` (exit 6) | a final-named object without a valid, matching commit record | 15.11: look, then quarantine or delete by hand; the tool never does |
| `retention holds back N committed point(s) it would drop` (exit 6, with `UNRESOLVED`) | an unresolved object is in the folder, and no committed point is dropped while one exists (15.3) | 15.11: deal with the unresolved object; the next run prunes as planned (a larger `--max-deletions` once, if many are due) |
| `... is dated ..., later than the host clock` / `... days after the previous one` / `the host clock moved forward by N hours` (exit 6, "retention deleted nothing") | the clock cannot be trusted to decide what to delete | 15.11: check `timedatectl`; nothing was deleted |
| `... not confirmed deleted and remain in the folder` (exit 6) | the backend accepted a delete and the object is still listed, or the listing that should confirm it failed | `list`; the next run retries; if it persists run the delete by hand and read rclone's own message |

### 15.8 Beside upgrades and restores

The backup holds the **shared** maintenance lock, like the cleanup (section 14.3), so it runs beside the web service and the
cleanup, and a migration or a restore (exclusive) is refused while it runs. **Before an upgrade or a restore, stop both timers,
and start them again afterwards** (14.4 has the commands; the same applies to `house-helper-offsite-backup.timer`). After every
upgrade and every rollback, change `HOUSE_HELPER_IMAGE_TAG` in `/etc/house-helper/purge.env`: both units read it. Forgetting it
fails closed (a tag whose schema is not the database's is refused, exit 4) and the backups silently stop being made, which is
why 15.9 asks for an alert.

### 15.9 What is not done: checklist **[OWNER]**

- [ ] The key pair exists, the private key is stored in two places off the VPS, and **the restore of 15.6 was rehearsed from a
      real uploaded point**, with the date recorded.
- [ ] A Yandex OAuth application with only the application-folder permission exists, and a **stable** `rclone` whose release
      notes and `rclone config providers` show the Yandex `app_folder` option works with it (15.4 step 3; 1.75.2 does not,
      1.76.0 is expected to). **Unverified here: no such release was available, and nothing has run against a live account.**
      The scope proof of 15.5 (403 with `app_folder = false`) was done.
- [ ] `list` ran against the real remote; one real `run` was read in the journal; the object in the Yandex web interface is the
      size the log says; the provider's Trash holds nothing of it (the hard delete is honoured).
- [ ] The commit record was seen working on the real remote: `rclone cat` of a `.commit` object returns its bytes, the object
      and its record appear in `list` as a committed point, and the **unresolved path** was rehearsed once on purpose (put a
      copy of a point under a new final-looking name; `list` must show it `UNRESOLVED`; quarantine it as in 15.11). **Unverified
      here: nothing has run against a live account or a real `rclone cat`; the tests use a fake rclone that implements only what
      the tool asks of it.**
- [ ] The real listing carries an **MD5 for every object**, including a `.partial` straight after its upload and an object
      straight after its rename: `rclone lsjson --hash NAME:FOLDER` by hand. The tool refuses a point whose listing gives none
      (exit 1 at the upload, or exit 7 at the final check), so a Yandex listing that omits it (for a moment, or for good) would
      stop every backup. **Unverified here: the tests list an MD5 because the fake does; nothing has run against a live account.**
- [ ] The server-side rename was seen working on the real remote (rclone's Yandex backend implements `Move` as a server-side
      move, per its source; the tool refuses to run if rclone reports otherwise): the object appears under its final name
      only at the end, and a `.partial` of a run stopped by hand (`systemctl kill -s KILL`, then `list`) is not shown as a point,
      is shown as `LEFTOVER PARTIAL`, is **not** removed by the next run (which ends with exit 6) and is removed by hand as in 15.11.
- [ ] The running image contains `sanitize` (a real run of `dbtool sanitize` against a copy of the real database was read:
      `rows per table` equal before and after, no free pages).
- [ ] The timer is enabled and `list-timers` shows the next run.
- [ ] A failure notification exists (15.10). Nothing is configured: a silent backup failure, or a backup that is simply no
      longer made, is the likely way this fails.
- [ ] The retention (7 daily + 4 weekly, about 36 days of tail) is accepted as the bound on the 365-day promise for the
      off-host copy, or changed, and what users are told matches (section 14.7).
- [ ] The owner accepts what the loss of the application-folder token means: whoever holds it can read the encrypted points
      and delete them (so keep the rclone config root-only), but cannot read their content, and it does not reveal the
      private key. **In the temporary full-disk mode the loss is worse:** the token reaches the whole Disk (15.12.4).
- [ ] If the temporary full-disk mode is used: every gate of 15.12.10 is closed.

### 15.10 Failure notification: the minimum **[OWNER]**

A failed unit shows only in `systemctl --failed` and the journal, which nobody reads daily, and **a backup that stops being
made (timer disabled, host down, the unit failing before it starts) produces no failure at all**. The smallest mechanism that
covers both is a **dead-man's switch**: the job pings a URL when it succeeds, and an external service alerts the owner when the
ping has not come for longer than a day plus a grace period. A failing run (any status except 0, including 6 and 7) sends no ping, so
one mechanism covers failure and silence. Nothing in the repository sends it; the owner adds it as a **drop-in** (not an edit of the unit), with a monitor of
their choice (for example a free Healthchecks.io check with a period of one day and a grace of a few hours, which also accepts
a `/fail` ping), after deciding that a third party may learn that the ping happened (the request carries no data, only the
check's URL, which is a capability: anyone who knows it can send a ping):

```ini
# sudo systemctl edit house-helper-offsite-backup.service
[Service]
ExecStartPost=/usr/bin/curl -fsS -m 20 --retry 3 -o /dev/null https://hc-ping.com/REPLACE-WITH-THE-CHECKS-UUID
```

`ExecStartPost=` runs only when the job exited 0. For an immediate alert as well, `OnFailure=` can point (in the same drop-in) at
a one-shot unit of the owner's that pings the check's `/fail` URL. Alternatives (a message to a Telegram bot with its own token,
not the application's; mail) need a credential or a mail path that this host does not have today and are not recommended as a
first step. Whatever is chosen, **test it once by making a run fail** (for example `--max-deletions 0` against a folder that
needs a delete gives status 6) and by waiting out the grace period with the timer stopped.


### 15.11 Operator review: unresolved objects, a suspect clock, plaintext that will not go **[OWNER]**

The tool decides nothing here by itself: each case below is reported by every run (exit 6; exit 7 for a run whose own point was
not confirmed; exit 3 for the last case) until a person has dealt with it, and the tool deletes none of these objects. **Look
first, change nothing first.**

```bash
cd ~/apps/house_helper
sudo python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper
```

**Quarantining one object safely.** Both quarantine steps below (an `UNRESOLVED` object and its record; a leftover `.partial`) and the
`SUSPECT` case use this procedure, once per object, and nothing else. Run all seven steps **in one shell session** (they define
functions and variables that the later steps use). It exists because of what the pinned `rclone` 1.75.2 does
(15.12.6): a plain `rclone moveto A B` **replaces an existing `B` and exits 0**, so a destination named the same way every time
(`<name>.quarantined`) can silently destroy an object that was quarantined earlier. `--ignore-existing` makes `moveto` skip instead
(exit 0; with `--error-on-no-transfer` **exit 9**, which the procedure adds so that a skip is loud). Nothing here shows that the skip is
atomic against a writer that creates the destination between the check and the move: that is why the procedure first stops the
backup, checks again immediately before the move, and accepts **only the listing taken afterwards, never rclone's exit status**, as
proof. Nothing in it deletes anything. It was run against the real `rclone` 1.75.2 on its `local` backend, including a destination
that already exists (`tests/test_deploy_offsite_quarantine.py`, opt-in with the real binary); **not** against Yandex Disk.

1. **Stop the schedule and confirm that no backup is running.** A run owns a `.partial` of its own and lists the folder several
   times; a rename during it can take that run's object or confuse its listings. Stop the **timer** (do not disable it: step 7 starts
   it again, and a reboot in between starts it by itself). A run that is under way is **waited for, not killed** (a killed run leaves a
   `.partial`): repeat the last line until it says `IDLE`.

   ```bash
   sudo systemctl stop house-helper-offsite-backup.timer
   idle() { [ "$(systemctl is-active house-helper-offsite-backup.timer)" = inactive ] \
         && [ "$(systemctl is-active house-helper-offsite-backup.service)" = inactive ] \
         && ! pgrep -f 'deploy/offsite_backup.py' > /dev/null; }
   idle && echo "IDLE: nothing is scheduled or running" || echo "NOT IDLE: do not continue"
   ```

2. **Select the exact source object and inspect it.** Copy its name out of `list` (or the `lsjson` below) into a variable: one
   exact name, never a wildcard, a pattern or a directory. `show` prints `name size md5` for the exact names it is given if they are in
   the folder, by comparing strings, and fails if the listing fails. The guard `exact` accepts only the shape of the tool's own
   objects: `...tar.age`, `...tar.age.partial` or `...tar.age.commit`. `BEFORE` must be **one line**, with a size and an MD5 (not `-`),
   and must be the object you meant: if it is not, stop. For a `.partial`, "whose is it" (step 3 of its procedure) is decided before this.

   ```bash
   CONF=/etc/house-helper/rclone.conf
   REMOTE='NAME:house-helper'          # NAME:house-helper-backup in the temporary full-disk mode (15.12)
   exact() { [[ "$1" =~ ^house-helper-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.tar\.age(\.partial|\.commit)?$ ]]; }
   show() {
     local listing
     listing=$(sudo rclone lsjson --config "$CONF" --files-only --hash "$REMOTE") || return 1
     printf '%s' "$listing" | python3 -c 'import json, sys; names = sys.argv[1:]; [print(i["Name"], i["Size"], {k.lower(): v for k, v in (i.get("Hashes") or {}).items()}.get("md5", "-")) for i in json.load(sys.stdin) if i["Name"] in names]' "$@"
   }

   SRC='house-helper-<time>-<id>.tar.age.partial'    # the EXACT name from the listing, between single quotes
   exact "$SRC" && echo "name ok" || echo "NOT an exact object name of the tool's shape: do not continue"
   BEFORE=$(show "$SRC"); echo "$BEFORE"
   ```

3. **Generate a unique destination and check that it does not exist.** The destination is the source's name plus `.quarantined-`, the
   UTC time and 8 random hex digits, so it is a name this procedure has never used (and not one of the tool's shapes, so the tool leaves it
   alone). It must print `free`; `TAKEN` also covers a listing that failed.

   ```bash
   DST="$SRC.quarantined-$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
   echo "$DST"
   FREE=$(show "$DST") && [ -z "$FREE" ] && echo "free: no object of that name" || echo "TAKEN, or the listing failed: do not continue"
   ```

4. **Move, once, without ever replacing.** Every check is repeated immediately before the move, and the command refuses to replace
   an existing destination. **Do not run this step a second time**, whatever it printed. `rclone exit: 0` means rclone believes it
   moved (or skipped); `9` means it **skipped** because an object of that name exists (on 1.75.2 nothing was moved or replaced); any
   other number is a failure. None of these is proof: go to step 5.

   ```bash
   if idle && exact "$SRC" && [ "$(printf '%s\n' "$BEFORE" | grep -c .)" = 1 ] && FREE=$(show "$DST") && [ -z "$FREE" ]; then
     sudo rclone moveto --config "$CONF" --ignore-existing --error-on-no-transfer "$REMOTE/$SRC" "$REMOTE/$DST"
     echo "rclone exit: $?"
   else
     echo "NOT RUN: the schedule is not idle, or the name, the single listing line or the free destination did not check out"
   fi
   ```

5. **List both names again and compare.** Only this proves the move: **exactly one line**, the destination, with the size and the MD5
   that step 2 recorded; the source is no longer there. A skipped move is **not** a success: it leaves both objects.

   ```bash
   AFTER=$(show "$SRC" "$DST"); echo "$AFTER"
   [ "$AFTER" = "$DST ${BEFORE#* }" ] && echo "MOVED: the source is gone and the destination has the source's size and MD5" \
     || echo "AMBIGUOUS: go to step 6; do not repeat the move"
   ```

6. **An ambiguous result is for the operator, not for a retry.** Do not run step 4 again, do not run it with another destination
   "to see", do not delete either object, and do not rename anything back. Write down `BEFORE`, the exit status of step 4, `AFTER` and
   `rclone lsjson --hash` of the folder, and read the table; the owner decides.

   | What you see | What it means | What to do |
   |---|---|---|
   | step 4 printed `NOT RUN` | a check failed, nothing was sent to rclone | fix the cause the line names (a run under way: wait for it; a name that is not exact; a taken destination: go back to step 3 for a new one) |
   | exit 9 (or 0) and **both** names are listed | the move was **skipped**: an object of the destination's name appeared after step 3 or step 3's listing was incomplete; nothing was replaced (1.75.2) | leave both; find out who created that object before anything else (15.9: someone else is writing to the folder); only then start again from step 3 |
   | the source is still listed, the destination is not | nothing moved: rclone failed (its message is above), or the listing lags | list once more after a minute; if it is unchanged, read rclone's message and fix its cause before starting again from step 3 |
   | neither name is listed | the object was removed by someone else, or the listing is incomplete | list once more; if still neither, look at the Yandex web interface (and its trash) and tell the owner; delete nothing |
   | the destination is listed with another size or MD5 | the destination is not the object you moved: it was replaced or changed | keep it as it is, treat the account as suspect (15.9), tell the owner |
   | `show` printed an error | the listing could not be read | list again; nothing else |

7. **Start the schedule again** only when **every** object of this review is done: an object **and** its `.commit` record (do the record
   as a second round of steps 2 to 6 with `SRC` set to the record's name). A run that meets a record without its object deletes that
   record as an orphan. Do not leave the timer stopped for days to think: a missing backup costs more than an exit 6. If the state is
   ambiguous, write it down, start the timer and accept the exit 6 that follows; the tool deletes none of these objects (only an
   orphaned record). Then look.

   ```bash
   sudo systemctl start house-helper-offsite-backup.timer
   systemctl list-timers house-helper-offsite-backup.timer --no-pager
   cd ~/apps/house_helper && sudo python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper
   ```

   There is deliberately no procedure to move a quarantined object back: it would make an `UNRESOLVED` object again, or give a
   `.partial` its exit 6 again, and a recovery point is made only by a run. If the owner wants it, the safe form is the command of step 4
   with the two names swapped by hand and a destination that `show` lists nothing for.

**An `UNRESOLVED` object** (a final-looking `...tar.age` without a valid commit record that matches it). The reason says which:
`it has no commit record` (a run was killed or failed between the rename and the record, the record was not confirmed, or the
listing right after the rename showed that the object is not the file that was read back, which the tool does not delete (15.2),
or the object is not the tool's); `its commit record is not one this tool
writes for it, or is cut short`; `it is not the object its commit record describes` (the object, or its record, changed after
the commit: treat it as damaged); `the remote's listing gives no MD5 for it, so it cannot be tied to its commit record` (the
size may well be right: with no MD5 listed nothing distinguishes the object from a changed one of the same size, so it is not
accepted; check `rclone lsjson --hash` by hand, and if the listing is whole again the next run counts it); `it is empty`.

**While it is there the tool prunes no committed point** (15.3): the folder grows by one point per run, and every run ends
with exit 6. Steps 2 and 3 below end that; the next run then drops the points the plan calls for (pass `--max-deletions` larger
**once** if the hold lasted long enough for more than the default 5 to be due).

1. Decide whether you want it. A fresh committed point is made every day, so an unresolved object is usually not worth keeping.
   If you do want to judge it, fetch it and run the checks of 15.6 on the key machine: a successful `age -d` and an `unpack` that
   reports "intact" mean the content is whole.
2. **Quarantine it (reversible):** rename it out of the tool's shape, together with its record if it has one, with the procedure
   **Quarantining one object safely** above: steps 1 to 6 for the object (`SRC` = `house-helper-<time>-<id>.tar.age`), then steps 2 to 6
   again for its record (`SRC` = `house-helper-<time>-<id>.tar.age.commit`), then step 7. The destination is a fresh, unique name each
   time (never a fixed `.quarantined`), the move cannot replace an existing object, and an ambiguous result stops for review. The
   tool then counts the renamed objects among "other objects" and leaves them alone, and the exit 6 for it stops. Do not use
   `--yandex-hard-delete` here.
3. **Or delete it,** once you have decided (this is the only deletion of such an object, and it is yours):
   `sudo rclone deletefile --config /etc/house-helper/rclone.conf --yandex-hard-delete NAME:house-helper/<exact name>`.
4. Run `list` again, or wait for the next run. Renaming a quarantined object back makes it unresolved again; there is no command
   that promotes it to a point, because a point is made only by a run that has read its bytes back.

**A leftover `.partial`** (`LEFTOVER PARTIAL <name>: left untouched` in the log, `LEFTOVER PARTIAL <name> (N bytes)` in `list`,
and exit 6 after a committed run). It is an object named `house-helper-<time>-<id>.tar.age.partial`: the fragment of a hard kill or
of an upload that stopped, the upload of a run that failed after it had returned (that run's log says `WARNING: ... was uploaded
by this run, which ended before the rename. It is LEFT in the folder untouched`), or somebody else's object. The tool cannot tell
which, and `rclone deletefile` deletes by name with no "only if it still has these bytes", so the tool **never deletes a
`.partial`** (15.2). It is not a recovery point and nothing reads it; it holds retention back from nothing. It only costs space and
turns every committing run's exit status into 6 until it is gone. Controlled manual cleanup, one object at a time:

1. `list` (above). Note the name and the size. In the temporary full-disk mode the folder is `NAME:house-helper-backup` in every
   command below.
2. Stop the backup timer and confirm that no run is under way (step 1 of **Quarantining one object safely**, above: it must print
   `IDLE`). A run that is uploading owns a `.partial` of its own, and removing or renaming it fails that run. Do not touch one while a
   run or a manual command is active.
3. Decide whether it is yours. If the time in its name is the time of a run the journal shows as failed or killed
   (`journalctl -u house-helper-offsite-backup.service`), it is that run's fragment. If you cannot place it, treat it as somebody
   else's object and ask before touching it.
4. **Quarantine it (reversible):** rename it out of the tool's shape with steps 2 to 6 of **Quarantining one object safely**, above
   (`SRC` = `house-helper-<time>-<id>.tar.age.partial`; the destination is that name plus a fresh `.quarantined-<UTC time>-<8 hex>`).
   The tool then counts it among "other objects" and leaves it alone, and the exit 6 for it stops. An ambiguous result is not retried
   (step 6 of the procedure).
5. **Or delete it,** once you have decided (this is the only deletion of such an object, and it is yours; timer stopped and `IDLE`
   as in step 2, and the exact name from the listing, as in step 2 of the procedure):
   `sudo rclone deletefile --config /etc/house-helper/rclone.conf --yandex-hard-delete NAME:house-helper/<exact name of the .partial>`.
6. Start the timer again (step 7 of the procedure), run `list` again, or wait for the next run, and expect exit 0 again. Delete the
   quarantined copy (`...partial.quarantined-<UTC time>-<8 hex>`) the same way when you are sure nobody needs it.

**A run that ended with exit 7** (`the final check of the folder did not confirm the recovery point ...`, `NOT reported as
stored`). This run wrote its object and its commit record and read the record back, but the folder, listed again afterwards,
does not show a committed point: the line names the reason (the record was removed or damaged in between, the object changed or
vanished, the listing gave no MD5, or the listing or a record could not be read). Nothing was deleted. Do not count this run's
point as a backup yet.

1. Run `list`. If the point is listed as a recovery point (a transient read problem), the next run counts it and carries on.
2. If it is `UNRESOLVED`, handle it as above. A point whose record is missing or damaged has no way back to being a point: the
   next daily run makes a new one. Until then the previous night's committed point is the newest there is.
3. If the object itself changed or vanished between the commit and the final listing, treat the account as suspect: someone or
   something else is writing to the folder (15.9, the token).

**A `SUSPECT` point, or "retention deleted nothing, because the clock cannot be trusted".** Nothing was deleted and nothing will
be until the cause is gone.

1. On the VPS: `timedatectl` (is `System clock synchronized: yes`? is the date right?). Fix the clock first.
2. If the **clock now is wrong** (set back, or a jump forward): once it is right, the points it dated are in the past again (or
   the next run is one day after this one) and the next run proceeds by itself. Nothing to rename.
3. If a point was **made while the clock was wrong** (its name is dated in the future by more than an hour, and the clock is right
   now): its content is real, its date is not. Quarantine it with **Quarantining one object safely** above (object **and** its
   `.commit`, then step 7) or delete both once you have decided; the first run after that deletes again. Old points are never touched on account of it.
4. "More than 7 days after the previous one" after a long outage of the host or the timer needs no action: the next run is
   one day after this one.

**A leftover work directory that cannot be removed** (`refused: ... leftover work director...`, exit 3, or the warning at the end of
a run). It may hold plaintext (a database copy, the tar). On the VPS: `ls -la /run/house-helper-offsite`; remove the directories
named in the message (`sudo rm -r /run/house-helper-offsite/hh-offsite-<random>`), or reboot (tmpfs is emptied); if `rm` fails,
find out why (`lsattr`, a busy mount, another user's directory) before the next run. This is unlink, not erasure (15.2).

### 15.12 Temporary full-disk access mode (an exception) **[OWNER]**

**Status: repository code only (Task A1, 2026-10-25; KHD-33 in `docs/DECISIONS.md`). Nothing here is installed or configured, no OAuth
credential exists in the repository, and no production system was changed.** This is an *exception*, not the design. The design is
15.1–15.11: a token that can reach the Yandex application folder only. The exception exists because the stable `rclone` available
today (1.75.2) has no Yandex `app_folder` option (checked on the real binary, 15.12.6) while the owner has explicitly authorised an
OAuth token that reaches the whole Disk, to be used **temporarily**, until a stable `rclone` with the option exists and the
design can be used as written (15.12.9 is the way back).

**What the exception costs.** In 15.1–15.11 the *token* limits the damage of a mistake or a leak to one folder. In this mode the
token limits nothing: whoever holds it can read, change and delete **everything on the Disk**, and the only things that keep this
tool inside its own folder are its own code and tests (15.12.3). The loss of the token is therefore the loss of the Disk (revoke it
at once, 15.12.4), which is why the credential handling below is strict and why the mode is off unless it is asked for.

#### 15.12.1 What changes, and what does not

| | default mode (`app-folder`) | `full-disk-temporary` |
|---|---|---|
| selected by | nothing (the default), or `--access-mode app-folder` | `--access-mode full-disk-temporary`, and nothing else |
| the config says | `app_folder = true` | `app_folder` absent, empty or `false`; `true` is a contradiction and is refused |
| the rclone must know `app_folder` | yes | no (it is the reason for the mode); a note says when it does |
| the folder | any `NAME:FOLDER` of 1–4 plain components, never the root | exactly `NAME:house-helper-backup` |
| own OAuth client id and secret, a token | required | required (rclone's built-in application is refused in both modes) |
| where the config file may be | a private (`0600`) regular file | additionally: only root can change it or any directory above it (15.12.4) |

Everything else is the same code in both modes and is **not** changed by the mode: age encryption for public keys only, the
verified backup, the sanitize rewrite, the upload, the read-back, the commit record, the final check, retention (7 daily + 4
weekly), the deletes (one object at a time, by exact name, confirmed by a fresh listing), the exit statuses and the log.

#### 15.12.2 The authorization contract (exact)

The mode is on only when **all** of this is true; any other state is refused (exit 3) or is a usage error (exit 2) **before any
command runs, rclone included**, and `run` and `list` give the same answer to every case (one function, `authorize_remote`, called
first by both):

1. `--access-mode full-disk-temporary`, spelled exactly like that. An empty value (an unset `HOUSE_HELPER_OFFSITE_ACCESS_MODE`
   expands to nothing), `full-disk`, `FULL-DISK-TEMPORARY`, `full_disk_temporary`, `true`, a value with a space or a suffix: exit
   2, nothing runs. The tool reads **no environment variable** to choose the mode: a stray `HOUSE_HELPER_OFFSITE_ACCESS_MODE` or
   `RCLONE_YANDEX_APP_FOLDER` cannot switch it on (the unit passes the first one on the command line, nothing else reads it).
2. `--remote NAME:house-helper-backup`, exactly: not `NAME:` (the root), not `NAME:house-helper`, not a sub-folder, not a
   different case, not a trailing slash, not `..`, not a connection string. Everything the validator accepts as a folder in the
   default mode and is not this one is refused here.
3. The config section `[NAME]`: `type = yandex`, the owner's own `client_id` and `client_secret`, a `token`, and **no**
   `app_folder = true` (any value but empty or `false` is a contradiction).
4. The config file where only root can change it (15.12.4).
5. The remote renames server-side (the commit is a rename), as always.

With the flag absent, a config like (3) is refused with a message that names the flag; with the flag present, a config that says
`app_folder = true` is refused with a message that says it contradicts it. In the unit this is one line of `offsite.env`:

```ini
HOUSE_HELPER_OFFSITE_ACCESS_MODE=app-folder
```

and the template ships exactly that. To use the exception the owner edits that line **and** the remote line (and, outside the
repository, the rclone config); nothing is guessed from the config or from a failure.

#### 15.12.3 The safe folder, and what the tool does and does not do with the Disk

* **One folder.** Every rclone command names `NAME:house-helper-backup` or one object directly inside it, and nothing else: `mkdir`,
  `lsjson --files-only --hash`, `copyto`, `moveto`, `cat` (a commit record) and `deletefile` are the only commands; `backend
  features` is asked of the folder as well (it answered without a network request, 15.12.6). **No command names the root of the
  Disk.** Upload, listing, read-back, record, verification, retention deletion and the rehearsal's download all go through the same
  `Remote` object, which accepts only names of the shape `house-helper-<UTC time>-<8 hex>.tar.age[.partial|.commit]` (and
  never deletes a `.partial`, 15.2).
* **Never** `delete`, `purge`, `sync`, `cleanup`, `rmdirs`, a recursive listing, a copy of a directory, or anything on the
  account's Trash. A test makes the fake remote fail on every other command and on every operand outside the folder.
* **Nothing outside the folder is read as a point or touched.** Tests plant a perfectly valid point in the *other* mode's folder, in
  sibling folders (`house-helper-backup2`, `house-helper-backup-old`), at the root of the Disk and next to personal files; none is
  counted, none is deleted, none changes by a byte. Files inside the folder that are not of the tool's shape are left alone too.
* **The Disk is not otherwise listed or read.** `list` lists the one folder. No command reads a file's content except this tool's
  own objects.
* **No plaintext reaches the Disk.** The only things uploaded are the age-encrypted recovery point and its small commit record
  (names, sizes, checksums of the *encrypted* object). The tool is given public keys only; the private identity is never on the
  VPS or in n8n (15.4, 15.13).
* **Non-destructive upload.** A name that already exists stops the run before the upload; `copyto` and `moveto` carry
  `--ignore-existing` (15.12.6 says why `--immutable` is not trusted alone); the object is read back and its SHA-256 compared; the
  rename is checked by a listing; the record is read back; the final check lists and classifies the folder again, and a closing
  listing, taken last of all, looks for any `.partial` that came in meanwhile (15.1). An interrupted
  upload leaves a `.partial` that no reader can mistake for a point and that no run deletes (it is reported, and removed by
  hand, 15.11); a mismatched, unlisted or
  unverifiable object is `UNRESOLVED` or exit 1/7, never "stored". (Unchanged from 15.1–15.3, now also run against a real rclone.)
* **Retention** is 15.3, unchanged: it deletes only committed points beyond 7 daily + 4 weekly and orphaned records, only by exact
  name inside the folder, only after the final check, never while an unresolved object exists, within `--max-deletions`.

#### 15.12.4 Credentials: the OAuth application, the token, the file **[OWNER]**

* **The application.** The owner's own Yandex OAuth application, with the Disk permissions the owner has authorised for this
  exception and no others (read and write on the Disk; nothing that this tool does not need: `list` and one `run` show what is
  needed). **Not rclone's built-in application** (shared; refused by the tool). Record who owns the application and where its
  secret is kept (a password manager), and the date after which it must be replaced or deleted (15.12.9).
* **The token** is made on a machine with a browser by `rclone config` (a remote of type `yandex` with `client_id`,
  `client_secret` and **no** `app_folder`), then copied to the VPS once, by the owner. It is never put in the repository, in n8n, in
  a chat, in a ticket, in a log or in an environment file; `.gitignore` refuses `rclone.conf` and `*.rclone.conf` and
  `tests/test_repo_secrets.py` fails the build if a file that looks like a token is tracked or not ignored.
* **The file** is `/etc/house-helper/rclone.conf`, mode `0600`, owner `root:root`, and the tool refuses to use it in this mode
  unless **every one** of these holds (checked from `/` down to the file, on one open descriptor):
  - the path is absolute, with no `.` or `..`, and no component of it, directory or file, is a symbolic link;
  - `/`, `/etc`, `/etc/house-helper` (every directory above the file) is a real directory **owned by root** and **not writable by
    group or others** (even with the sticky bit: `/tmp` and its kin are refused);
  - the file is a regular file with a **single** hard link, owned by root, with no permission for group or others, at most 1 MiB;
  - it is the very file that was opened: the path is looked at again after reading and must still name it.

  ```bash
  sudo install -d -m 0755 -o root -g root /etc/house-helper
  sudo install -m 0600 -o root -g root /path/to/rclone.conf /etc/house-helper/rclone.conf
  namei -l /etc/house-helper/rclone.conf       # every line: root root, nothing writable by group/other, no "l" (link)
  ```
* **Why those rules, and the race they close.** Between the tool's check and rclone's use of the file a second process could
  swap the file, a directory above it or a link, unless nobody but root can. That is what the ownership and write rules mean:
  the path is *root-controlled*, so only root could do it, and root can read the token anyway. The tool does not try to hand
  rclone a descriptor: rclone re-opens the path, and saves by replacing it.
* **Token refresh keeps working, and stays private.** rclone refreshes the OAuth token on its own and persists the new one by
  **writing a new file beside the old one and renaming it over it** (verified on the real rclone 1.75.2: the old file's inode and a
  hard link to it kept the old content; the new file kept the old file's mode and, run by root, its owner; no temporary file was
  left). So the directory must stay writable by root (it is), the unit must not mount it read-only (it does not; if the unit is
  ever hardened with `ProtectSystem=`, add `ReadWritePaths=/etc/house-helper`: a test pins this), and the unit's `UMask=0077`
  keeps the new file private. The refreshed file is checked again at the next start. rclone **follows a symbolic link** when it
  saves (the new content went to the link's target): another reason the tool refuses one. rclone does not tighten a loose mode
  (`0644` stayed `0644`), so it is this tool's check, not rclone, that refuses a file others can read.
* **If the token may be exposed** (a copy left on a laptop, a backup of `/etc` that left the host, a message): revoke the
  application's access at Yandex ID **first**, then replace the file. Everything the token could reach is exposed until it is
  revoked, which is the whole Disk, not a folder.
* **Logs and alerts.** The tool prints no token, no client secret, no path of the config file, no rclone message (only which
  command failed and its exit status), no database content and no key. A test makes every failing rclone and age answer with a
  message full of all of them and checks the output, for both modes.

#### 15.12.5 Activating it (opt-in) **[OWNER]**

Not done by this change. In this order, each step by the owner, none of them automatic:

1. Everything in 15.4 except the application-folder permission and the `app_folder` rclone: the age key pair (outside the
   checkout, 15.4 item 1), age and rclone on the VPS, a running image that knows `sanitize`. The rclone is the **pinned stable**
   release the owner chooses (1.75.2 was the one tested); never a beta or development build.
2. Edit `/etc/house-helper/offsite.env` (from `deploy/systemd/house-helper-offsite.env.example`): `HOUSE_HELPER_OFFSITE_REMOTE=NAME:house-helper-backup` and `HOUSE_HELPER_OFFSITE_ACCESS_MODE=full-disk-temporary`.
3. Install the config file as in 15.12.4 (the section has **no** `app_folder`).
4. By hand, watched, before the timer:

   ```bash
   sudo python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper-backup --access-mode full-disk-temporary
   ```

   It prints the NOTICE line first; an empty folder is a normal answer, a missing one is made by the first `run`. Then
   `sudo systemctl start house-helper-offsite-backup.service`, `systemctl status`, `journalctl -u house-helper-offsite-backup.service -n 40 --no-pager`.
5. In the Yandex web interface check that the only new thing on the Disk is the folder `house-helper-backup` with the encrypted
   point and its `.commit`, of the size the log states.
6. The restore rehearsal of 15.13 from that real point, on your own machine. **Only after it passed** enable the timer (15.5).

#### 15.12.6 What was verified against a real rclone, and what was not

Run on 2026-10-25 against `rclone/rclone:1.75.2` (image digest `sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5`,
binary SHA-256 `b7587df2b9d5840c4e1e3ca75904d8c0e8f8150ab51c76651e7f855d8e80befc`), offline (a network-less container for the probes of
requests; a closed proxy port for the tests). The tests are `tests/test_deploy_offsite_real_rclone.py`, opt-in:
`HOUSE_HELPER_REAL_RCLONE=/path/to/rclone HOUSE_HELPER_REAL_RCLONE_VERSION=v1.75.2 pytest tests/test_deploy_offsite_real_rclone.py`.

* `rclone config providers`: the Yandex backend has `client_id`, `client_secret`, `token`, `hard_delete` (which `--yandex-hard-delete`
  sets) and **no `app_folder`**; there is no `1.76.x` image yet. The default mode refuses this rclone; the full-disk mode accepts it.
* `rclone backend features NAME:house-helper-backup` (and `NAME:`) answered `"Move": true` **without any network request**, for a
  folder that does not exist. The tool asks about the folder and never the root.
* With a fake token and no network, the requests rclone tried were `lsjson` → `path=disk:/house-helper-backup/`, `mkdir` →
  `path=/house-helper-backup`, `copyto` → `path=disk:/house-helper-backup/<object>`: all inside the folder.
* Every command line the tool builds is accepted by rclone and does what the fake assumed, on rclone's `local` backend: upload, a
  listing with hashes, read-back, rename, commit record by `cat`, classification as a committed point, confirmed delete.
* **A wrong assumption it corrected:** `copyto --immutable` and `moveto --immutable` **replaced** an existing different object and
  exited 0 on 1.75.2; `--ignore-existing` skipped it. Before this the tool relied on `--immutable` alone. It now passes
  `--ignore-existing` to both, a skipped upload is caught by the size/MD5 comparison, and a skipped rename fails the run and leaves
  the foreign object alone (tests with the fake and with the real binary). The listing before the upload and the unique name were
  always the first line; this is the second.
* **The manual quarantine of 15.11, against the same binary** (`tests/test_deploy_offsite_quarantine.py`): a plain `rclone moveto A B`
  onto an **existing** `B` replaced it and exited 0, so the earlier fixed `...quarantined` destination could destroy an earlier
  quarantined object; `moveto --ignore-existing` left both objects as they were and exited 0; with `--error-on-no-transfer` it
  exited **9**; a missing source exited 1. The procedure of 15.11 uses a fresh destination, `--ignore-existing --error-on-no-transfer`
  and a listing afterwards, and the opt-in test runs the RUNBOOK's own shell blocks against the real binary, including a destination
  that appears between the check and the move. It does **not** show that `--ignore-existing` is atomic against such a writer, nor
  anything about Yandex Disk's own `Move`; the procedure therefore stops the backup first and trusts only the listing afterwards.
* rclone's saving of a changed config: 15.12.4.

**Not verified: anything about Yandex Disk itself.** No account, no network: the listing and MD5 of a Yandex object, the 403 and
quota behaviour, the trash and `--yandex-hard-delete`, the server-side move of the real backend, the OAuth consent screen and the
scopes actually granted, and the real `age`. The first `list` and `run` of 15.12.5 are where those are seen, and 15.9 lists them.

#### 15.12.7 Retention in this mode

Exactly 15.3 (7 daily + 4 weekly committed points, `--max-deletions`, the strict no-pruning-while-unresolved policy, the clock
guard), applied to the one folder. Nothing outside it is a point, so nothing outside it is ever a candidate for deletion.

#### 15.12.8 Recovery

15.6 and 15.13, unchanged: a recovery point is decrypted on the owner's own machine with the private key; the rehearsal downloads
from the same folder with the same mode flag. If the Disk is lost with the token, the points are lost: this is a copy, not the only
copy (15.9).

#### 15.12.9 Migrating to application-folder permissions (the way back)

When a **stable** rclone whose `rclone config providers` lists the Yandex `app_folder` option exists (the tool says
`This rclone HAS the app_folder option` in the NOTICE line of every run and `list`), end the exception:

1. Install that stable rclone on the VPS (not a beta). Register a **new** Yandex OAuth application with **only** the
   application-folder permission, make its token on a machine with a browser with `app_folder = true`, and install the config as
   in 15.5 (15.12.4's root-controlled path rules are not required in that mode but are good practice for the file).
2. `HOUSE_HELPER_OFFSITE_ACCESS_MODE=app-folder` and `HOUSE_HELPER_OFFSITE_REMOTE=NAME:house-helper`, with the new config; run
   `list` and one `run` by hand, and the rehearsal (15.13, without the mode flag).
3. The old points in `house-helper-backup` are not moved automatically. Keep them until the new folder holds 7 daily + 4 weekly
   points (about 5 weeks), or copy them by hand (each object **and** its `.commit`), then delete the old folder by hand.
4. **Revoke the full-disk OAuth application's access at Yandex ID** and delete its secret; delete the old `rclone.conf` from
   every machine it was copied to. Only then is the exception over: say so in `docs/STATUS.md`.

#### 15.12.10 Interfaces for monitoring, and the gates that remain **[OWNER]**

*Preserved for the monitoring work (the other agent's deploy/monitoring/, not part of this change). The monitoring must read
nothing but these:*

* **The unit** `house-helper-offsite-backup.service` (unchanged name; `ExecStart` gained `--access-mode ${HOUSE_HELPER_OFFSITE_ACCESS_MODE}`),
  `SyslogIdentifier=house-helper-offsite`, `Type=oneshot`; `ExecStopPost=` and `OnFailure=` drop-ins (15.10) see the usual
  `SERVICE_RESULT`, `EXIT_CODE`, `EXIT_STATUS`.
* **The exit status, unchanged and no new code:** 0 stored and clean; 1 failed; 2 usage (including a bad `--access-mode`); 3 refused
  before anything was done (including every authorization failure of this mode); 6 stored and verified but needs attention; 7 not
  confirmed; 128+N a signal. **Only 0 is success; 6 is never a success heartbeat** (15.7). The new refusals are 3 (`refused`), the new
  usage error is 2.
* **The journal:** names, sizes, checksums of the encrypted object, counts and the fixed texts of 15.7. In this mode every run and
  every `list` starts with one `NOTICE: TEMPORARY access mode full-disk-temporary ...` line: it is information, not a failure, and an
  alert may use its absence from a run as "the mode is not on". There is no token, secret, path of the config or rclone message.
* **A health check** that needs no secret of its own is `list` (read only, exit 0 whatever it finds: read the text).
* The monitoring must not read `rclone.conf`, the age key, the recipients file's private counterpart or any recovery point.

*Gates that stay open (none is closed by this change): the owner's explicit approval to activate; the real Yandex OAuth
application and token (15.12.4); a pinned stable rclone on the VPS and `age`; the first real `list` and `run` and the checks of
15.12.6's "not verified"; the restore rehearsal of 15.13 from a real point; the failure notification of 15.10 and the monitoring,
live and tested by a failing run; the acceptance of the retention tail (15.9); a date for the end of the exception (15.12.9).*

### 15.13 Restore rehearsal: `deploy/offsite_rehearsal.py` **[OWNER]**

The proof that a point can be restored is a restore. This program does the part of it that can run anywhere, on the point that
really left the VPS, and it **never** touches production. It is a separate program from the backup on purpose: it is the only place
the **private** age identity is used, and the production script and the units know nothing of identities (a test pins that).

**Where:** the owner's own machine (any OS with Python 3.10+, `age` and `rclone`). Never the VPS, never n8n, never inside a checkout
of this repository: the program refuses to run when `/etc/house-helper` exists (the production host), when the identity file is
inside the checkout, when it is readable by other users (POSIX), when it is a link, or when the scratch directory is inside the
checkout. It needs a copy of `rclone.conf` for the same remote (make one on that machine with `rclone config`, or copy the VPS one
over a channel you trust, **and delete it afterwards**; in the full-disk mode that file reaches the whole Disk).

```bash
python3 deploy/offsite_rehearsal.py --rclone-config "$HOME/keys/house-helper/rclone.conf" --remote NAME:house-helper --identity-file "$HOME/keys/house-helper/age-key.txt" --work-dir "$HOME/scratch/rehearsal" --expect-rows users=2 --expect-rows messages=1
python3 deploy/offsite_rehearsal.py --rclone-config "$HOME/keys/house-helper/rclone.conf" --remote NAME:house-helper-backup --access-mode full-disk-temporary --identity-file "$HOME/keys/house-helper/age-key.txt" --work-dir "$HOME/scratch/rehearsal" --expect-rows users=2 --point house-helper-20261025T041507Z-0a1b2c3d.tar.age
```

**What it does,** and stops at the first that fails: (1) the same offline authorization as the backup (mode, folder, config), then the
identity, host and scratch checks, all before any command runs; (2) lists the folder and chooses a **committed** point (the newest,
or `--point`): an `UNRESOLVED` object or one that is not in the folder is refused; (3) downloads that one object into a fresh `0700`
directory under `--work-dir` and requires its size, SHA-256, SHA-1 and MD5 to equal its commit record; (4) decrypts with `age -d -i
IDENTITY` (the identity is passed as a path and never printed); (5) unpacks with the backup tool's own reader (exactly three files, the
manifest's shape, the snapshot equal to its size and SHA-256) and checks that the manifest's time is the time in the point's name; (6)
opens the snapshot **read only** and checks SQLite's integrity, foreign keys, no free page, exactly one plain Alembic revision, and every
`--expect-rows TABLE=N` (the table exists and has at least N rows; at least one is required, so "expected contents" is something you
state); (7) removes the scratch directory on every exit path, signals included. A directory that cannot be removed is a failure.

**What it never does:** write to the remote (it lists, reads one commit record and downloads one object; the commands that create,
move or delete are not in the file), write anywhere but the new directory under `--work-dir`, open an existing file for writing,
use Docker, `backup.sh` or `restore`, or print a key, a token, a path of the config, a row or an rclone/age message.

| Exit | Meaning |
|---|---|
| 0 | retrieved, matched its record, decrypted, unpacked and verified; the scratch directory is gone. **The only status that says the point can be restored.** |
| 1 | failed before a verdict: rclone or the disk failed, a signal, or the scratch directory could not be removed (a failure of `age` is **not** here: it is exit 4) |
| 2 | usage |
| 3 | refused before anything was done: configuration, identity, host, scratch place, no committed point, a `--point` that is not one |
| 4 | **not restorable**: the bytes differ from the record, `age` fails to decrypt it (any non-zero exit of `age -d`: not a recipient of this point, or the object is damaged), the archive or the database is not sound, or an expectation is not met |

**What it does not replace.** It does not check the schema against the application's migrations or restore into the application. For
that, after a pass, do 15.6 and section 10 with the image of the manifest's tag onto a **scratch volume** (never the live one), and
run the cleanup afterwards (14.6). Record in 15.9: the date, the point's name, its encrypted SHA-256 (the verdict prints it), the
mode, the machine, and the result. Plaintext existed on that machine for the length of the run: it is unlink, not erasure (15.2).

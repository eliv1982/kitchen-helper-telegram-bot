# House Helper web application: deployment runbook (R4C-D1)

**Status of this document: the kit was released as repository-only (R4C-D1) and has since been used for the
technical deployment of 2026-10-24** (commit `6370bf7`, image `house-helper:6370bf78980e`, database at
`0006_oidc_state_invitation`; sign-in and OpenAI disabled, no real users; see `docs/STATUS.md`, "Current deployment
state"). Section 14 (scheduled cleanup, R4C-D2) was added after the kit's release: its code is RELEASED in Git as
`489ff30` (independent audit passed, owner accepted, CI green). Its systemd timer is installed and active on the VPS;
its first automatic execution, scheduled for 2026-10-25, has not been verified. The encrypted off-host backup and a
restore rehearsal are not yet completed.
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

### Pre-live deployment gates (built by R4C-D2, code released in Git; timer installed, first run not yet verified)

Two product requirements are **gates, not options**, and nothing in this document (a short beta, two users,
an owner-only trial, a time limit on the trial) substitutes for either of them. R4C-D2 built them (section 14;
code `489ff30`, audited and accepted), but a gate stays open until the work is **scheduled on the host and seen to run**:

1. **Scheduled cleanup of authentication artifacts** must be implemented, tested and running **before real use**,
   that is before any real person signs in.
2. **365-day physical deletion of conversation history** must be implemented, tested and running **before any
   real user data is stored** (the rule in `docs/STATUS.md`). Hiding expired rows from reads is not deletion.

Until both are running (the checklist is section 14.9) the deployment may be rehearsed with disposable data only:
no real users are enrolled and no real conversation is stored. Building them changed the application (the command
`purge-expired`, `house_helper/admin/retention.py`), which is outside the kit R4C-D1 released; it was audited on its
own and is not part of what D1's audits covered.

| Gate | Status |
|---|---|
| **Scheduled cleanup** of expired sessions, OIDC states, invitations, email challenges | **CODE RELEASED IN GIT (R4C-D2, `489ff30`); TIMER INSTALLED AND ACTIVE ON THE VPS (2026-10-24), FIRST RUN NOT YET VERIFIED. Pre-live gate 1: required before real use.** `python -m house_helper.admin purge-expired` deletes each of them past its own expiry, in short batches, beside the web service (`hhops exec --shared`); `--dry-run` only counts. The opt-in systemd timer and service are in `deploy/systemd/`; nothing in the repository installs them. **Open until** the timer's first automatic run (scheduled for 2026-10-25) has been seen in the journal (section 14.9); the running image already carries the command. |
| **365-day physical deletion** of conversation history | **CODE RELEASED IN GIT (R4C-D2, `489ff30`); TIMER INSTALLED AND ACTIVE ON THE VPS (2026-10-24), FIRST RUN NOT YET VERIFIED. Pre-live gate 2: required before any real user data is stored; not waived.** The same command deletes messages and the turn requests that hold a copy of an answer 365 x 24 hours after their own creation, keeps the recent messages of an old conversation and removes conversations left empty. It is logical SQL deletion, not forensic erasure, and a deleted row survives in every older backup (section 14.7): the backup retention below bounds how real the promise is. **Open until** the same condition as the row above: the first automatic run seen in the journal. |
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
The timer is installed and active on the VPS (owner-reported, 2026-10-24); its first automatic execution, scheduled for
2026-10-25, has not been verified.** The statements about what was tested (section 13) are about the work before the
deployment: the command was run on disposable databases with synthetic rows. No result of a run on the real database is
claimed here. Every step marked **[OWNER]** changes
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
- [ ] The timer: installed and enabled as in 14.4, the first run started by hand and its journal line read, and `list-timers`
      showing the next one.
- [ ] Where a failed run is reported is decided (nothing is configured: 14.4).
- [ ] Sections 9 and 11 are followed with the extra steps: the timer stopped before an upgrade or a restore and started
      after (14.4), the tag in `purge.env` changed (14.4), and a restore rehearsal includes "run the cleanup afterwards"
      (14.6).
- [ ] The backup retention, off-host copy and encryption are decided (14.7); without them the deletion promise is for the live
      database only.
- [ ] What the users are told about retention matches 14.7: deletion within about a day of the 365 days, older backups, and
      OpenAI's own settings.

Reported state, 2026-10-24: the timer is installed and active and its first automatic run, scheduled for 2026-10-25, has not
been verified. The unchecked items above were not reported as done and stay open; the timer item stays unchecked until the
first run's journal line has been read.

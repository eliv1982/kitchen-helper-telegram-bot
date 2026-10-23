# syntax=docker/dockerfile:1
#
# The House Helper web application (house_helper/web): FastAPI under uvicorn, SQLite on a
# volume. The Telegram bot (bot.py and its root modules) is a separate process and is not in
# this image. See deploy/RUNBOOK.md for how it is built, run and operated.
#
# Python 3.12 on Debian 13, pinned by digest so that a build is the same build tomorrow.
# To take a newer base: `docker pull python:3.12-slim-trixie`, read the new digest with
# `docker image inspect python:3.12-slim-trixie --format '{{index .RepoDigests 0}}'`, rebuild,
# and run the checks of the runbook again.
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2


FROM ${PYTHON_IMAGE} AS build

# The dependencies exactly as the repository pins them (requirements.txt: the same set CI
# tests), in a virtual environment that is copied whole into the runtime image, so no pip
# cache, build tool or wheel download remains there.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --requirement /tmp/requirements.txt


FROM ${PYTHON_IMAGE} AS runtime

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="house-helper-web" \
      org.opencontainers.image.description="House Helper (Kitchen Helper) web application" \
      org.opencontainers.image.revision="${VCS_REF}"

# PYTHONPATH: the operator tools under /opt/house-helper/ops are run as scripts, and a
# script's own directory, not the working directory, is what Python puts on its path.
# No bytecode is written at run time (the root filesystem is read-only); it is compiled once
# below instead.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOUSE_HELPER_STAGING_DIR=/staging

# A fixed numeric user, so that the ownership of a volume does not depend on names. /data is
# the only directory the application writes; it is created owned by that user, and a new
# named volume mounted there inherits that ownership. /staging is the same for the operator
# tool's scratch copies of the database (backup, verify, restore): a disk-backed volume the
# compose file mounts for the ops service only, because a database can be larger than /tmp.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app \
 && install --directory --owner app --group app --mode 0700 /data /staging

COPY --from=build /opt/venv /opt/venv

# Application, tools and configuration are owned by root and not writable by the
# application's user. Nothing else of the repository is copied (.dockerignore is an
# allowlist): not the bot, the tests, the docs, a .env file or a database.
WORKDIR /app
COPY house_helper ./house_helper
COPY alembic.ini ./alembic.ini
COPY deploy/ops /opt/house-helper/ops
COPY deploy/logging.json /opt/house-helper/logging.json
RUN python -m compileall -q /app/house_helper

# No VOLUME instruction: it would leave an anonymous volume behind every `docker run` that
# does not mount /data (the disposable backup verification). The compose file mounts it.
USER 10001:10001
EXPOSE 8000

# Liveness only, by design (house_helper/web/health.py touches no database): a database
# outage must not make the orchestrator restart the process. Docker marks the container
# unhealthy, and Traefik stops routing to a container that is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

STOPSIGNAL SIGTERM

# One worker, deliberately: the chat turn runner and the database work queue are
# in-process, and SQLite has one writer at a time. Migrations are NOT run here.
#
# `dbtool.py serve -- <program>` is the start. It takes a shared lock on the data directory
# (deploy/ops/dbtool.py, "The maintenance lock"), checks the schema read only, so that a
# container started before the migration, or with an older database than its code, stops
# with a clear message instead of serving errors, and then REPLACES ITSELF with the program
# after the `--` (os.execvp: same process, new program). That process, uvicorn, holds the lock
# for as long as it runs, and the kernel drops it when it ends, so a restore (which needs the
# lock exclusively) is refused while the web service is up. The operator applies migrations
# as a separate, deliberate step (deploy/RUNBOOK.md).
#
#   --no-access-log          uvicorn's access log writes the request line, query string
#                            included; the OIDC callback carries ?code=...&state=....
#                            The application also filters those (web/logsafety.py), but
#                            the safest line is the one never written.
#   --timeout-keep-alive 120 longer than Traefik's 90 s idle limit towards a backend, so
#                            the proxy never reuses a connection uvicorn has just closed
#                            (an intermittent 502).
#   --timeout-graceful-shutdown 30
#                            then the application's own shutdown (the chat runner and the
#                            database each wait up to 10-20 s); the compose file's
#                            stop_grace_period must exceed the sum.
#   --lifespan on            a failure to start is a failure, not a server without a database.
CMD ["python", "/opt/house-helper/ops/dbtool.py", "serve", "--", \
     "uvicorn", "house_helper.web.asgi:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--no-server-header", \
     "--log-config", "/opt/house-helper/logging.json", \
     "--lifespan", "on", \
     "--timeout-keep-alive", "120", "--timeout-graceful-shutdown", "30"]

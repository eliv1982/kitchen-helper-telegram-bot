# Legacy SQLite maintenance units (historical rollback assets, reference only)

These are the exact systemd units and environment templates of the pre-cutover SQLite
deployment (R4C-D2). They are **preserved, not installed**: nothing in the repository
installs, enables or starts them, and the PostgreSQL procedure
(`deploy/postgres/scheduled_maintenance.md`) never copies this directory to `/etc`.

They are kept as **historical rollback assets — reference text only**. They are **not a
rollback procedure**, and reactivating them is prohibited as a routine or automatic step:
while PostgreSQL is authoritative, a legacy retention run would mutate preserved SQLite
data, upload stale SQLite recovery points into the shared offsite destination and displace
PostgreSQL recovery points through the shared 7 daily + 4 weekly retention. The PostgreSQL
units live one directory up and keep the same unit names, which is exactly why a file
replacement is *not* a rollback: nothing would stop the SQLite jobs from running.

The default recovery response is to stop and disable the automated maintenance and keep
PostgreSQL authoritative (`deploy/postgres/scheduled_maintenance.md`, section 6). Returning
to the SQLite schedule requires a separate, written, Owner-approved data-rollback procedure
that satisfies the frozen rollback contract (RBK-01, RBK-06) — including the accepted loss of
PostgreSQL data after the accepted recovery point, drained connections, the preservation of
every existing recovery asset, and separate protection (or explicit approval) of the shared
offsite destination.

The SQLite data path and its Compose file (`deploy/compose.yaml`), `deploy/backup.sh`
and the SQLite env templates are unchanged and remain in place as well. Do not edit these
copies: they are the historical text, and their `Documentation=` lines point at the
pre-cutover RUNBOOK sections.

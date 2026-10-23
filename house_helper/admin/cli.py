"""The command line: `python -m house_helper.admin <command> ...`.

Commands: provision-email (the first user and their email address); enroll-user,
reissue-invitation, revoke-invitation and enrollment-status (the initial users, who link
their own Telegram account with a one-time invitation; see enrollment.py); purge-expired
(deletes expired sign-in artifacts and conversation history older than 365 days; see
retention.py).

Output is one line per result, `<status> [<code>]: <message>`: `ok` goes to stdout,
`refused` and `error` to stderr, and the exit status says which kind it was:

  0  ok           created, unchanged, reapproved, enrolled, reissued, revoked, purged, a report
  1  error        an unexpected failure (the class name is printed, nothing else)
  2  usage        the command line, or a value on it, cannot be used
  3  refused      the database is in a state in which the command declines to act
                  (purge-expired: it stayed locked by another writer)
  4  error        the database cannot be used: not configured, missing, not at the head

What is never printed: the database URL (not even in an error), a driver's error text
(it can quote hosts, users, SQL parameters), a traceback, or a value the operator typed
that has not passed validation. argparse itself quotes the offending text in most of its
messages (an option someone mistook for `--database-url postgresql://user:pw@...`), so
its error messages are replaced by a fixed one.

The one secret there is, an invitation's, is printed exactly once, on its own line of
stdout (`invitation code: ...`), by enroll-user and reissue-invitation when they have
committed it. It is never printed to stderr, never in a refusal or an error, and what the
database keeps is only its digest, which is never printed either. Do not run these two
commands where their output is logged.

Arguments are validated first, so a bad command line never reaches the database.
"""

import argparse
import enum
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

import sqlalchemy as sa

from house_helper.admin import enrollment, retention
from house_helper.admin.enrollment import EnrollmentRefusal
from house_helper.admin.provisioning import (
    InvalidInput,
    Outcome,
    ProvisionResult,
    Refusal,
    prepare_request,
    provision_email,
)
from house_helper.admin.target import TargetError, open_target
from house_helper.persistence.base import utcnow
from house_helper.persistence.database import create_session_factory
from house_helper.persistence.invitations import INVITATION_LIFETIME, IssuedInvitation


class ExitCode(enum.IntEnum):
    OK = 0
    INTERNAL = 1
    USAGE = 2
    REFUSED = 3
    DATABASE = 4


INTERRUPTED = 130  # the shell convention for Ctrl-C

_EPILOG = """\
The database is chosen by the HOUSE_HELPER_DATABASE_URL environment variable only
(never by an argument: it holds a password). It must already exist and be migrated to
the head; this command never creates a database and never migrates one. Never run it
while a migration is running (alembic or anything else that changes the schema): its
checks of the schema cannot stop one. Stop the applications, migrate, wait for the
migration to finish, then provision.

exit status: 0 ok; 1 unexpected error; 2 usage; 3 refused (the database is in a state
in which the command declines to act); 4 the database cannot be used."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse quotes the offending text: see the module docstring
        if not message.startswith("the following arguments are required"):
            message = "the command line is not valid"
        self.print_usage(sys.stderr)
        _say(sys.stderr, "usage", "arguments", f"{message} (see --help)")
        raise SystemExit(ExitCode.USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m house_helper.admin",
        description="Operator-only administrative commands.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    provision = commands.add_parser(
        "provision-email",
        help="create the first user and approve their email sign-in address",
        description=(
            "Create the first (and only) user together with an approved email sign-in "
            "address, in one transaction. Repeating the command with the same address is a "
            "no-op. It never creates a second user, never adds an address to an existing "
            "user, never provisions a Telegram identity and never enables a disabled user."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    provision.add_argument(
        "--email",
        required=True,
        help="the address to approve. Its domain is lower-cased; the part before '@' is kept "
        "exactly as typed (Elena@example.com and elena@example.com are different addresses)",
    )
    provision.add_argument(
        "--display-name",
        help="the new user's display name (used only when the user is created)",
    )
    provision.add_argument(
        "--note",
        help="a note stored on the allowlist entry (who asked, why). Not applied when nothing "
        "changes; replaces the stored note when a revoked address is reapproved",
    )
    provision.add_argument(
        "--reapprove-revoked",
        action="store_true",
        help="allow lifting the revocation of an address that was revoked (also needs "
        "--verified-mailbox). Revoked sessions and sign-in links stay revoked",
    )
    provision.add_argument(
        "--verified-mailbox",
        action="store_true",
        help="confirm that you checked that this exact spelling of the address, including "
        "the capitals before the '@', is the mailbox that should sign in (needed with "
        "--reapprove-revoked)",
    )

    def enrollment_command(name: str, help: str, description: str) -> argparse.ArgumentParser:
        return commands.add_parser(
            name,
            help=help,
            description=description,
            epilog=_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )

    hours = int(INVITATION_LIFETIME.total_seconds() // 3600)
    enroll = enrollment_command(
        "enroll-user",
        "create a new user and issue them a one-time Telegram invitation",
        "Create a NEW user and their first Telegram invitation, in one transaction. The user "
        "can sign in nowhere until the invitation has been claimed with a verified Telegram "
        f"login. The invitation is valid for {hours} hours, once. At most "
        f"{enrollment.ENROLLMENT_USER_CAP} users can be created this way (a temporary policy "
        "of this command, not a database rule). Never creates an allowlist entry, never "
        "links an identity and never matches anybody by name, email address or Telegram id.",
    )
    enroll.add_argument(
        "--display-name",
        required=True,
        help="the new user's display name: how you tell the users apart. A user with the "
        "same name (any capitals) is refused, so that a repeated command is not a second user",
    )
    reissue = enrollment_command(
        "reissue-invitation",
        "give an existing user without a Telegram identity a new invitation",
        f"Issue a new {hours}-hour, single-use Telegram invitation to an EXISTING user who has "
        "no Telegram identity yet, and revoke the earlier outstanding ones in the same "
        "transaction. Also the first invitation for a user that has none (the user "
        "provision-email created, for example). Refuses an unknown or disabled user and a user "
        "who already has a Telegram identity, revoked or not.",
    )
    revoke = enrollment_command(
        "revoke-invitation",
        "withdraw a user's active invitation",
        "Withdraw the user's active invitation, so that it can never be used. Repeating "
        "it changes nothing. A used or expired invitation is not touched.",
    )
    for command in (reissue, revoke):
        command.add_argument(
            "--user-id",
            required=True,
            help="the user, as enrollment-status prints the id (8-4-4-4-12 hexadecimal digits)",
        )
    enrollment_command(
        "enrollment-status",
        "show the users and the state of their invitations (read only)",
        "A read-only report of every user: enabled or disabled, whether they have a Telegram "
        "identity (never its value), and the state of their current invitation: the active one "
        "if there is one, otherwise the one that ended last. It shows no "
        "Telegram id, no secret and no digest.",
    )
    purge = enrollment_command(
        "purge-expired",
        "delete expired sign-in artifacts and conversation history older than 365 days",
        "Delete, for real, what has expired: sessions, sign-in states, emailed sign-in links and "
        "invitations past their own expiry (used or withdrawn ones too, once they have expired), "
        "and conversation history (messages, the turn requests that hold a copy of an answer, "
        "and conversations left empty) older than 365 x 24 hours. Recent messages of an old "
        "conversation are kept. Nothing else is touched: not users, not sign-in identities, not "
        "anything this command does not name. Safe to repeat and safe to run beside the "
        "application (work is done in short batches; it waits for the database's write lock "
        "and stops with a refusal if it never gets it). The command line has no way to choose "
        "the time: the cutoff is always now minus 365 days. The report is counts only.",
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be deleted, in one read-only transaction; change nothing",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> int:
    """Run the command and return its exit status. `environ` is os.environ and `clock` the
    real UTC clock unless a test gives others."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:  # --help (0) or a usage error (2)
        return int(stop.code or 0)
    environ = os.environ if environ is None else environ
    clock = utcnow if clock is None else clock
    try:
        if args.command == "provision-email":
            return _provision_email(args, environ)
        if args.command == "purge-expired":
            return _purge_expired(args, environ, clock)
        return _ENROLLMENT_COMMANDS[args.command](args, environ, clock)
    except KeyboardInterrupt:
        _say(sys.stderr, "error", "interrupted", "interrupted; an unfinished change was rolled back.")
        return INTERRUPTED
    except Exception as exc:  # the text of an exception can carry SQL parameters: class only
        _say(
            sys.stderr,
            "error",
            "internal",
            f"unexpected {type(exc).__name__}. The change was rolled back or not confirmed; "
            "run the same command again to see the state (it is safe to repeat).",
        )
        return ExitCode.INTERNAL


def _provision_email(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    if args.reapprove_revoked != args.verified_mailbox:
        _say(
            sys.stderr,
            "usage",
            "arguments",
            "--reapprove-revoked and --verified-mailbox are given together or not at all.",
        )
        return ExitCode.USAGE
    try:
        request = prepare_request(
            args.email,
            args.display_name,
            args.note,
            reapprove_revoked=args.reapprove_revoked,
            verified_mailbox=args.verified_mailbox,
        )
    except InvalidInput as invalid:
        _say(sys.stderr, "usage", "invalid_value", str(invalid) + ".")
        return ExitCode.USAGE

    try:
        engine = open_target(environ)
    except TargetError as error:
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    try:
        result = provision_email(create_session_factory(engine), request)
    except Refusal as refusal:
        _say(sys.stderr, "refused", refusal.reason.value, refusal.message)
        return ExitCode.REFUSED
    except TargetError as error:  # the schema changed after the first look
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    finally:
        engine.dispose()

    _say(sys.stdout, "ok", result.outcome.value, _success_message(result))
    return ExitCode.OK


def _success_message(result: ProvisionResult) -> str:
    if result.outcome is Outcome.CREATED:
        message = (
            f"created the initial user {result.user_id} and approved {result.email} for email "
            "sign-in. No Telegram identity was provisioned."
        )
    elif result.outcome is Outcome.REAPPROVED:
        message = (
            f"lifted the revocation of {result.email} for user {result.user_id}. Sessions and "
            "sign-in links revoked with it stay revoked: the person requests a new link."
        )
    else:
        message = (
            f"{result.email} is already approved for user {result.user_id}. Nothing was changed."
        )
    if result.ignored:
        message += f" Without effect in this run: {', '.join(result.ignored)}."
    return message


# --- enrollment -------------------------------------------------------------------


def _run_enrollment(
    environ: Mapping[str, str],
    operation: Callable,
    report: Callable,
) -> int:
    """Open the database, run `operation(session_factory)`, and print `report(result)` only
    once it has committed. The same mapping of failures to exit statuses as provision-email."""
    try:
        engine = open_target(environ)
    except TargetError as error:
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    try:
        result = operation(create_session_factory(engine))
    except EnrollmentRefusal as refusal:
        _say(sys.stderr, "refused", refusal.reason.value, refusal.message)
        return ExitCode.REFUSED
    except TargetError as error:  # the schema changed after the first look
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    finally:
        engine.dispose()
    report(result)
    return ExitCode.OK


def _usage_error(invalid: InvalidInput) -> int:
    _say(sys.stderr, "usage", "invalid_value", str(invalid) + ".")
    return ExitCode.USAGE


def _enroll_user(args: argparse.Namespace, environ: Mapping[str, str], clock: Callable) -> int:
    try:
        display_name = enrollment.prepare_display_name(args.display_name)
    except InvalidInput as invalid:
        return _usage_error(invalid)
    return _run_enrollment(
        environ,
        lambda factory: enrollment.enroll_user(factory, display_name, now=clock),
        lambda issued: _print_issued(
            issued, "enrolled", f"created user {issued.user_id} and issued a Telegram invitation"
        ),
    )


def _reissue_invitation(args: argparse.Namespace, environ: Mapping[str, str], clock: Callable) -> int:
    try:
        user_id = enrollment.prepare_user_id(args.user_id)
    except InvalidInput as invalid:
        return _usage_error(invalid)
    return _run_enrollment(
        environ,
        lambda factory: enrollment.reissue_invitation(factory, user_id, now=clock),
        lambda issued: _print_issued(
            issued,
            "reissued",
            f"issued a new Telegram invitation for user {issued.user_id}",
            superseded=issued.superseded,
        ),
    )


def _revoke_invitation(args: argparse.Namespace, environ: Mapping[str, str], clock: Callable) -> int:
    try:
        user_id = enrollment.prepare_user_id(args.user_id)
    except InvalidInput as invalid:
        return _usage_error(invalid)

    def report(revoked: int) -> None:
        if revoked:
            _say(sys.stdout, "ok", "revoked", f"revoked the outstanding invitation of user {user_id}.")
        else:
            _say(
                sys.stdout,
                "ok",
                "unchanged",
                f"user {user_id} has no active invitation (one that has expired is left as it "
                "is). Nothing was changed.",
            )

    return _run_enrollment(
        environ, lambda factory: enrollment.revoke_invitation(factory, user_id, now=clock), report
    )


def _enrollment_status(args: argparse.Namespace, environ: Mapping[str, str], clock: Callable) -> int:
    return _run_enrollment(
        environ, lambda factory: enrollment.enrollment_status(factory, now=clock()), _print_status
    )


_ENROLLMENT_COMMANDS = {
    "enroll-user": _enroll_user,
    "reissue-invitation": _reissue_invitation,
    "revoke-invitation": _revoke_invitation,
    "enrollment-status": _enrollment_status,
}


def _print_issued(
    issued: IssuedInvitation, code: str, summary: str, superseded: Optional[int] = None
) -> None:
    """The committed invitation: a status line, then the one line that carries the secret."""
    hours = int(INVITATION_LIFETIME.total_seconds() // 3600)
    message = (
        f"{summary}, valid until {_timestamp(issued.expires_at)} ({hours} hours, single use)."
    )
    if superseded is not None:
        message += (
            f" {superseded} earlier outstanding invitation(s) revoked."
            if superseded
            else " No earlier invitation was outstanding."
        )
    message += (
        " The invitation code on the next line is a secret: it is shown only now and is not "
        "stored. Whoever holds it before it ends can link THEIR OWN Telegram account to this "
        "user, so give it only to the person it is for."
    )
    _say(sys.stdout, "ok", code, message)
    print(f"invitation code: {issued.code}", file=sys.stdout)


def _print_status(status: enrollment.EnrollmentStatus) -> None:
    _say(
        sys.stdout,
        "ok",
        "status",
        f"{len(status.users)} user(s). The enrollment limit is {status.cap} users (a temporary "
        "policy of this command, not a database rule). No Telegram id, secret or digest is shown.",
    )
    for user in status.users:
        invitation = "none"
        if user.invitation is not None:
            verb = {"active": "until", "expired": "ended", "used": "used", "revoked": "revoked"}[
                user.invitation.state.value
            ]
            invitation = f"{user.invitation.state.value} ({verb} {_timestamp(user.invitation.at)})"
        print(
            f"user {user.user_id} name {_shown(user.display_name)}: "
            f"{'disabled' if user.disabled else 'enabled'}; created {_timestamp(user.created_at)}; "
            f"telegram: {user.telegram.value}; invitation: {invitation}",
            file=sys.stdout,
        )


# --- purge-expired ----------------------------------------------------------------


def _purge_expired(args: argparse.Namespace, environ: Mapping[str, str], clock: Callable) -> int:
    """Open the database, delete (or count) what has expired, print the counts. The same
    mapping of failures to exit statuses as the other commands; a run that stopped half way
    says how much its committed batches had deleted, because that is gone for good."""
    try:
        engine = open_target(environ)
    except TargetError as error:
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    try:
        report = retention.purge_expired(engine, now=clock(), dry_run=args.dry_run)
    except retention.PurgeAborted as aborted:
        return _purge_stopped(aborted)
    except TargetError as error:  # the schema is not the head (changed since the first look)
        _say(sys.stderr, "error", error.code, error.message)
        return ExitCode.DATABASE
    finally:
        engine.dispose()
    _print_purge(report)
    return ExitCode.OK


def _purge_progress(aborted: retention.PurgeAborted) -> str:
    deleted = sum(aborted.done.values())
    return (
        f"{deleted} row(s) had already been deleted by batches that committed "
        f"({_counts_inline(aborted.done)}); the batch in progress was rolled back"
    )


def _purge_stopped(aborted: retention.PurgeAborted) -> int:
    cause = aborted.__cause__
    again = "Run the same command again: it is safe to repeat and finishes the job."
    if isinstance(cause, KeyboardInterrupt):
        _say(
            sys.stderr,
            "error",
            "interrupted",
            f"interrupted in stage {aborted.stage}: {_purge_progress(aborted)}. {again}",
        )
        return INTERRUPTED
    # Only the driver's own message says "busy": SQLAlchemy's text also quotes the statement,
    # which on PostgreSQL contains "SKIP LOCKED".
    if isinstance(cause, sa.exc.OperationalError) and any(
        word in str(cause.orig).lower() for word in ("locked", "busy")
    ):
        _say(
            sys.stderr,
            "refused",
            "database_busy",
            f"the database stayed locked by another writer for longer than the command waits "
            f"(stage {aborted.stage}): {_purge_progress(aborted)}. {again}",
        )
        return ExitCode.REFUSED
    # The text of the exception can carry SQL and parameters: the class only.
    _say(
        sys.stderr,
        "error",
        "purge_failed",
        f"unexpected {type(cause).__name__} in stage {aborted.stage}: {_purge_progress(aborted)}. {again}",
    )
    return ExitCode.INTERNAL


def _counts_inline(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def _print_purge(report: retention.PurgeReport) -> None:
    cutoff = _timestamp(report.cutoff)
    scope = (
        f"conversation history created at or before {cutoff} (365 x 24 hours before "
        f"{_timestamp(report.now)}) and sign-in artifacts past their own expiry"
    )
    if report.dry_run:
        _say(
            sys.stdout,
            "ok",
            "dry_run",
            f"would delete {report.total} row(s): {scope}. Nothing was changed.",
        )
    elif report.total:
        _say(sys.stdout, "ok", "purged", f"deleted {report.total} row(s): {scope}.")
    else:
        _say(sys.stdout, "ok", "unchanged", f"nothing has expired ({scope}); nothing was deleted.")
    for name, count in report.deleted.items():
        print(f"  {name}: {count}", file=sys.stdout)
    print(
        f"  (included in turn_requests) in flight and abandoned, cancelled first: "
        f"{report.cancelled_in_flight}",
        file=sys.stdout,
    )
    print(
        f"  (not deleted yet) expired messages still linked by a request that cannot be "
        f"deleted yet: {report.held_back_messages}",
        file=sys.stdout,
    )


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shown(name: Optional[str]) -> str:
    """A display name as one quoted token that cannot break the line or be mistaken for
    something else, and that the output stream can carry (a console on a legacy code page
    gets backslash escapes instead of an error that would end the report)."""
    if name is None:
        return "(none)"
    text = "".join(c if c.isprintable() else "?" for c in name)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text = text.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        text = text.encode("ascii", "backslashreplace").decode("ascii")
    return f'"{text}"'


def _say(stream, status: str, code: str, message: str) -> None:
    print(f"{status} [{code}]: {message}", file=stream)

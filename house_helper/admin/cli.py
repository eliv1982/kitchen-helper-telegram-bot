"""The command line: `python -m house_helper.admin provision-email ...`.

Output is one line per result, `<status> [<code>]: <message>`: `ok` goes to stdout,
`refused` and `error` to stderr, and the exit status says which kind it was:

  0  ok           created, unchanged or reapproved
  1  error        an unexpected failure (the class name is printed, nothing else)
  2  usage        the command line, or a value on it, cannot be used
  3  refused      the database is in a state in which the command declines to act
  4  error        the database cannot be used: not configured, missing, not at the head

What is never printed: the database URL (not even in an error), a driver's error text
(it can quote hosts, users, SQL parameters), a traceback, or a value the operator typed
that has not passed validation. argparse itself quotes the offending text in most of its
messages (an option someone mistook for `--database-url postgresql://user:pw@...`), so
its error messages are replaced by a fixed one.

Arguments are validated first, so a bad command line never reaches the database.
"""

import argparse
import enum
import os
import sys
from typing import Mapping, Optional, Sequence

from house_helper.admin.provisioning import (
    InvalidInput,
    Outcome,
    ProvisionResult,
    Refusal,
    prepare_request,
    provision_email,
)
from house_helper.admin.target import TargetError, open_target
from house_helper.persistence.database import create_session_factory


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
    return parser


def main(argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None) -> int:
    """Run the command and return its exit status. `environ` is os.environ unless a test
    gives another."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:  # --help (0) or a usage error (2)
        return int(stop.code or 0)
    environ = os.environ if environ is None else environ
    try:
        return _provision_email(args, environ)
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


def _say(stream, status: str, code: str, message: str) -> None:
    print(f"{status} [{code}]: {message}", file=stream)

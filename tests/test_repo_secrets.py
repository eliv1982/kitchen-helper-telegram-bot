"""A small, dependency-free check that no secret of the off-host backup is in the working tree (RUNBOOK 15.4, 15.12).

The things that must never be in the repository: the age PRIVATE key, an rclone configuration (it holds an OAuth token that
reaches a whole disk), a refresh token, a decrypted or an encrypted recovery point. Three lines of defence, none of them a
substitute for the others: the RUNBOOK makes the key outside the checkout (tests/test_deploy_offsite_docs.py), `.gitignore`
refuses the usual names (below), and this test fails the build if a tracked or not-ignored file looks like one.

The patterns are assembled from pieces so that this file does not match itself, and the test that the scanner works builds
its synthetic "secret" in a temporary directory at run time: no secret-shaped value exists in the repository.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy_support import REPO_ROOT

BECH32 = "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L"  # the alphabet of an age secret key (upper case)
PATTERNS = {
    "an age private key": re.compile("AGE-SECRET-" + "KEY-1[" + BECH32 + "]{58}"),
    "a PEM private key": re.compile("-----BEGIN [A-Z ]*PRIVATE " + "KEY-----"),
    "a refresh token in an rclone-style token": re.compile('"refresh_' + 'token"\\s*:\\s*"[^"]{20,}"'),
    "a Yandex OAuth token": re.compile("\\by[0-9]_[A-Za-z0-9_-]{40,}"),
}
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(rclone[^/]*\.conf|(?:[^/]*-)?age-key[^/]*|age-identity[^/]*|[^/]*\.age-identity|recovery[^/]*\.tar|snapshot\.db[^/]*|"
    r"[^/]*\.tar\.age(?:\.partial|\.commit)?)$"   # "age-key" only as a word of its own: "package-keys.json" is not a key
)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
TEXT_LIMIT = 4 << 20


def candidate_files(root: Path):
    """Tracked files and files that are neither tracked nor ignored (what a `git add .` would take); every file of the
    tree when this is not a git checkout."""
    git = shutil.which("git")
    if git and (root / ".git").exists():
        done = subprocess.run(
            [git, "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=120,
        )
        if done.returncode == 0:
            for raw in done.stdout.split(b"\0"):
                if raw:
                    path = root / raw.decode("utf-8", "surrogateescape")
                    if path.is_file():
                        yield path
            return
    for current, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in SKIP_DIRS]
        for name in files:
            yield Path(current) / name


def scan(root: Path) -> list:
    """(relative path, what) for every file that is named like a secret or holds something shaped like one. Reports no value."""
    found = []
    for path in candidate_files(root):
        relative = path.relative_to(root).as_posix()
        if FORBIDDEN_NAMES.search(relative):
            found.append((relative, "a file named like a secret or a recovery artifact"))
            continue
        try:
            if path.stat().st_size > TEXT_LIMIT:
                continue
            text = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        for what, pattern in PATTERNS.items():
            if pattern.search(text):
                found.append((relative, what))
    return found


def test_no_tracked_or_unignored_file_is_a_secret_or_looks_like_one():
    assert scan(REPO_ROOT) == []


def test_the_scanner_finds_what_it_is_meant_to_find(tmp_path):
    (tmp_path / "notes.txt").write_text("harmless\n")
    (tmp_path / "leaked.txt").write_text("# oops\nAGE-SECRET-" + "KEY-1" + "Q" * 58 + "\n")
    (tmp_path / "pem.txt").write_text("-----BEGIN OPENSSH PRIVATE " + "KEY-----\n")
    (tmp_path / "conf.txt").write_text('token = {"access_token":"a","refresh_' + 'token":"' + "r" * 40 + '"}\n')
    (tmp_path / "yandex.txt").write_text("y0_" + "A" * 60 + "\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "rclone.conf").write_text("[x]\n")
    (tmp_path / "house-helper-age-key.txt").write_text("empty\n")
    (tmp_path / "recovery.tar").write_bytes(b"\0" * 10)
    (tmp_path / "house-helper-20261025T041507Z-0a1b2c3d.tar.age").write_bytes(b"\0")
    reported = dict(scan(tmp_path))
    assert reported == {
        "leaked.txt": "an age private key",
        "pem.txt": "a PEM private key",
        "conf.txt": "a refresh token in an rclone-style token",
        "yandex.txt": "a Yandex OAuth token",
        "sub/rclone.conf": "a file named like a secret or a recovery artifact",
        "house-helper-age-key.txt": "a file named like a secret or a recovery artifact",
        "recovery.tar": "a file named like a secret or a recovery artifact",
        "house-helper-20261025T041507Z-0a1b2c3d.tar.age": "a file named like a secret or a recovery artifact",
    }


def test_the_scanner_does_not_print_or_return_a_value(tmp_path):
    secret = "AGE-SECRET-" + "KEY-1" + "Q" * 58
    (tmp_path / "leaked.txt").write_text(secret)
    assert all(secret not in repr(item) for item in scan(tmp_path))


def test_the_names_and_shapes_in_the_tests_and_the_documents_of_this_repository_are_not_secrets():
    # The words the RUNBOOK and the tests use for a key (with an ellipsis, or too short) are not a key.
    assert not PATTERNS["an age private key"].search("AGE-SECRET-" + "KEY-... or AGE-SECRET-" + "KEY-1QQQ")
    assert not PATTERNS["a Yandex OAuth token"].search("def test_the_conversation_stays_open_y0_x")


# --- .gitignore: a second line of defence for the names a person would use --------------------------------------------------------

IGNORED = [
    "house-helper-age-key.txt", "age-key.txt", "age-key-backup.txt", "me-age-key.txt", "deploy/age-key.txt", "age-identity.txt", "my.age-identity",
    "rclone.conf", "deploy/rclone.conf", "yandex.rclone.conf", "rclone-yandex.conf",
    "recovery.tar", "recovery-point.tar", "recovered/MANIFEST.json",
    "house-helper-20261025T041507Z-0a1b2c3d.tar.age", "house-helper-20261025T041507Z-0a1b2c3d.tar.age.partial",
    "house-helper-20261025T041507Z-0a1b2c3d.tar.age.commit", "hh-offsite-abc123/pair/notes.txt", "hh-rehearsal-abc123/restored/MANIFEST.json",
]
NOT_IGNORED = ["deploy/offsite_backup.py", "deploy/offsite_rehearsal.py", "deploy/systemd/house-helper-offsite.env.example", "package-keys.json",
               "tests/test_deploy_offsite.py", "deploy/RUNBOOK.md", "docs/STATUS.md", "storage-key-notes.md"]


@pytest.fixture(scope="module")
def git():
    found = shutil.which("git")
    if not found or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    return found


def ignored(git, name: str) -> bool:
    return subprocess.run([git, "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", name], capture_output=True, timeout=60).returncode == 0


@pytest.mark.parametrize("name", IGNORED)
def test_gitignore_refuses_the_names_of_keys_credentials_and_recovery_artifacts(git, name):
    assert ignored(git, name), name


@pytest.mark.parametrize("name", NOT_IGNORED)
def test_gitignore_does_not_swallow_the_files_of_the_feature(git, name):
    assert not ignored(git, name), name

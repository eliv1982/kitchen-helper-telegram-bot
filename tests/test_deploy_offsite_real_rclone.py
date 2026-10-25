"""The off-host backup against a REAL, pinned, stable rclone binary (opt-in; skipped unless HOUSE_HELPER_REAL_RCLONE is set).

Everything else in tests/test_deploy_offsite*.py talks to a fake rclone, which proves what the tool asks and nothing about
what rclone answers. These tests run the tool's own code (Runner, Remote, the capability and configuration checks) against
the real binary, OFFLINE:

  * the capability check reads the real `rclone config providers`: for stable 1.75.2 the Yandex backend has NO app_folder
    option, so the default mode refuses and the temporary full-disk mode is accepted, and a version that does have it is
    reported as such;
  * `rclone backend features NAME:FOLDER` of a Yandex remote answers {"Move": true} without a network request, for a folder
    that does not exist;
  * every rclone command line the tool builds (mkdir, copyto --immutable, lsjson --files-only --hash, moveto, cat, deletefile
    --yandex-hard-delete) is accepted by rclone's own parser and does what the fake assumes, on rclone's `local` backend in a
    temporary directory: upload, listing with hashes, read-back, rename, commit record, classification, confirmed delete;
  * rclone saves a changed configuration (what a refreshed OAuth token is) as a new file beside the old one, keeping its mode
    and owner, and that file passes the tool's own check of the full-disk mode's configuration.

What they cannot show: anything about Yandex Disk itself (its listings, its MD5-only checksums, its 403s, its trash). No
network is used; HTTPS_PROXY points at a closed port as a second line, so that a request could not leave the machine anyway.

    HOUSE_HELPER_REAL_RCLONE=/path/to/rclone [HOUSE_HELPER_REAL_RCLONE_VERSION=v1.75.2] \\
    [HOUSE_HELPER_REAL_RCLONE_HAS_APP_FOLDER=0] pytest tests/test_deploy_offsite_real_rclone.py
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.deploy_support import posix_only
from tests.offsite_support import RCLONE_CONF_FULL_DISK, offsite

BINARY = os.environ.get("HOUSE_HELPER_REAL_RCLONE", "")
EXPECTED_VERSION = os.environ.get("HOUSE_HELPER_REAL_RCLONE_VERSION", "")
HAS_APP_FOLDER = os.environ.get("HOUSE_HELPER_REAL_RCLONE_HAS_APP_FOLDER", "0") == "1"

pytestmark = pytest.mark.skipif(not BINARY, reason="set HOUSE_HELPER_REAL_RCLONE to a pinned stable rclone binary to run these")


@pytest.fixture(autouse=True)
def no_way_out(monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runner():
    return offsite.Runner()


def yandex_config(tmp_path: Path, **extra) -> Path:
    text = RCLONE_CONF_FULL_DISK.replace('token = {"access_token":"test"}','token = {"access_token":"PROBE","token_type":"bearer","refresh_token":"PROBE","expiry":"2099-01-01T00:00:00Z"}')
    path = tmp_path / "yandex.conf"
    path.write_text(text + "".join(f"{k} = {v}\n" for k, v in extra.items()))
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_the_binary_is_the_release_the_run_was_asked_to_check(runner):
    done = runner.run([BINARY, "version"], timeout=60, capture=True)
    assert done.returncode == 0
    first = done.stdout.decode().splitlines()[0]
    assert first.startswith("rclone v")
    if EXPECTED_VERSION:
        assert first == f"rclone {EXPECTED_VERSION}", first
    print("real rclone:", first)


def test_the_real_providers_listing_decides_the_capability_the_way_the_fake_assumes(runner, tmp_path):
    config = yandex_config(tmp_path)
    if HAS_APP_FOLDER:
        assert offsite.check_rclone_capability(runner, BINARY, config, offsite.ACCESS_APP_FOLDER) is True
    else:
        with pytest.raises(offsite.Refused, match="no app_folder option"):
            offsite.check_rclone_capability(runner, BINARY, config, offsite.ACCESS_APP_FOLDER)
    # the full-disk mode needs the Yandex backend and not the option; it reports whether the option exists
    assert offsite.check_rclone_capability(runner, BINARY, config, offsite.ACCESS_FULL_DISK) is HAS_APP_FOLDER


def test_the_yandex_options_the_tool_relies_on_exist_in_the_real_rclone(runner, tmp_path):
    done = runner.run([BINARY, "config", "providers", "--config", str(yandex_config(tmp_path))], timeout=60, capture=True)
    options = {o["Name"] for p in json.loads(done.stdout) if p["Name"] == "yandex" for o in p["Options"]}
    assert {"client_id", "client_secret", "token", "hard_delete"} <= options        # --yandex-hard-delete is the flag of "hard_delete"
    assert ("app_folder" in options) is HAS_APP_FOLDER


@pytest.mark.parametrize("folder", ["house-helper-backup", "house-helper"])
def test_backend_features_of_a_yandex_remote_answers_move_for_a_folder_that_does_not_exist_without_a_request(runner, tmp_path, folder):
    config = yandex_config(tmp_path)
    mode = offsite.ACCESS_FULL_DISK if folder == offsite.FULL_DISK_PREFIX else offsite.ACCESS_APP_FOLDER
    remote = offsite.Remote(f"fake:{folder}", config, runner, BINARY, mode)
    remote.require_server_side_move()          # raises unless the real answer parses to Features.Move == true


def test_the_real_rclone_accepts_every_command_the_tool_builds_and_the_commit_protocol_works_on_its_local_backend(runner, tmp_path, monkeypatch):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)                                  # the local backend's "fake:folder" is ./folder
    config = tmp_path / "local.conf"
    config.write_text("[fake]\ntype = local\n")
    remote = offsite.Remote(f"fake:{offsite.FULL_DISK_PREFIX}", config, runner, BINARY, offsite.ACCESS_FULL_DISK)

    remote.require_server_side_move()
    remote.ensure_folder()
    assert remote.listing() == []

    sealed = tmp_path / "sealed.bin"
    sealed.write_bytes(offsite.AGE_HEADER + os.urandom(5000))
    local = offsite.digests_of(sealed)
    name = "house-helper-20261025T041507Z-0a1b2c3d.tar.age"
    partial = name + ".partial"

    remote.upload(sealed, partial)
    listed = [e for e in remote.listing() if e.name == partial]
    assert len(listed) == 1 and listed[0].size == local.size
    assert offsite.check_entry(listed[0], local)                # real rclone lists an MD5 (and more) the tool can compare
    other = tmp_path / "other.bin"
    other.write_bytes(b"different bytes")
    with pytest.raises(offsite.Failed, match=r"rclone skipped the upload \(exit 9"):
        remote.upload(other, partial)                           # --ignore-existing skips it, --error-on-no-transfer makes that exit 9 ...
    survivor = [e for e in remote.listing() if e.name == partial][0]
    assert survivor.size == local.size                          # ... the existing object is untouched
    with pytest.raises(offsite.Failed, match="different size"):
        offsite.check_entry(survivor, offsite.digests_of(other))   # ... and what is there is still not this upload's bytes

    read_back = tmp_path / "readback.bin"
    remote.download(partial, read_back)
    assert hashlib.sha256(read_back.read_bytes()).hexdigest() == local.sha256

    # the rename never replaces an existing object either: the source stays, the destination is untouched
    remote.upload(other, name)
    remote.move(partial, name)
    after = {e.name: e.size for e in remote.listing()}
    assert after == {partial: local.size, name: len(b"different bytes")}
    remote.delete_confirmed([name])

    remote.move(partial, name)
    assert {e.name for e in remote.listing()} == {name}
    record = offsite.commit_record(name, local)
    record_path = tmp_path / "record.json"
    record_path.write_bytes(record)
    remote.upload(record_path, name + offsite.COMMIT_SUFFIX)
    assert remote.read_record(name) == record

    folder = offsite.classify_folder(remote.listing(), remote.read_record)
    assert [p.name for p in folder.committed] == [name] and folder.unresolved == {} and folder.stray_records == []

    result = remote.delete_confirmed([name, name + offsite.COMMIT_SUFFIX])
    assert sorted(result.gone) == sorted([name, name + offsite.COMMIT_SUFFIX]) and result.left == []
    assert remote.listing() == []
    # nothing was ever written outside the one folder
    assert sorted(p.name for p in workdir.iterdir()) == [offsite.FULL_DISK_PREFIX]


def test_why_the_tool_passes_ignore_existing_and_not_only_immutable(runner, tmp_path):
    """Found with this test on stable 1.75.2: `copyto --immutable` and `moveto --immutable` REPLACE an existing different object
    and exit 0; only --ignore-existing refuses to. If this rclone's --immutable does refuse, the tool's extra flag is merely redundant."""
    config = tmp_path / "local.conf"
    config.write_text("[fake]\ntype = local\n")
    (tmp_path / "remote").mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"new bytes")
    existing = tmp_path / "remote" / "object"
    existing.write_bytes(b"x" * 5000)
    os.utime(existing, (1577836800, 1577836800))
    done = runner.run([BINARY, "copyto", "--immutable", "--retries", "1", "--config", str(config), str(source), f"fake:{tmp_path / 'remote'}/object"],
                      timeout=60, capture=True)
    if done.returncode != 0 or existing.stat().st_size == 5000:
        pytest.skip("this rclone's --immutable refuses to replace: the tool's --ignore-existing is redundant here")
    assert done.returncode == 0 and existing.read_bytes() == b"new bytes"       # replaced, silently
    existing.write_bytes(b"x" * 5000)
    done = runner.run([BINARY, "copyto", "--immutable", "--ignore-existing", "--retries", "1", "--config", str(config), str(source),
                       f"fake:{tmp_path / 'remote'}/object"], timeout=60, capture=True)
    assert done.returncode == 0 and existing.read_bytes() == b"x" * 5000        # skipped


def test_a_skipped_copy_is_exit_9_under_error_on_no_transfer_and_a_real_one_is_exit_0(runner, tmp_path):
    """The foreign `.partial` race of the final audit: without --error-on-no-transfer the skip is exit 0 (a quiet success);
    with it, exit 9. The destination is never written either way, and it is the exit status alone that tells this run its
    upload did not happen (the run then fails and says the object is not proven to be its own; in any case no `.partial` is ever
    deleted by the tool)."""
    config = tmp_path / "local.conf"
    config.write_text("[fake]\ntype = local\n")
    (tmp_path / "remote").mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"this run's bytes")
    destination = f"fake:{tmp_path / 'remote'}/object.partial"
    flags = ["--immutable", "--ignore-existing", "--retries", "1", "--config", str(config)]

    def copy(*extra):
        return runner.run([BINARY, "copyto", *flags, *extra, str(source), destination], timeout=60, capture=True).returncode

    foreign = tmp_path / "remote" / "object.partial"
    foreign.write_bytes(b"foreign bytes")
    assert copy() == 0 and foreign.read_bytes() == b"foreign bytes"                                       # skipped, and says success
    assert copy("--error-on-no-transfer") == 9 and foreign.read_bytes() == b"foreign bytes"               # skipped, and says so
    foreign.unlink()
    assert copy("--error-on-no-transfer") == 0 and foreign.read_bytes() == b"this run's bytes"            # a transfer: exit 0


def test_the_real_rclone_lists_a_missing_folder_as_an_error_not_as_an_empty_one(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "local.conf"
    config.write_text("[fake]\ntype = local\n")
    remote = offsite.Remote(f"fake:{offsite.FULL_DISK_PREFIX}", config, runner, BINARY, offsite.ACCESS_FULL_DISK)
    with pytest.raises(offsite.Failed):
        remote.listing()                                          # as the fake does (exit 3): `list` of a folder never made says so


@posix_only
def test_a_configuration_that_rclone_saves_is_a_new_private_file_beside_the_old_one_and_passes_the_trusted_read(runner, tmp_path):
    directory = tmp_path / "etc" / "house-helper"
    directory.mkdir(parents=True)
    (tmp_path / "etc").chmod(0o755)
    directory.chmod(0o755)
    path = directory / "rclone.conf"
    path.write_text("[fake]\ntype = local\ndescription = before\n")
    path.chmod(0o600)
    trust = offsite.ConfigTrust(os.getuid(), tmp_path)
    assert "before" in offsite.read_trusted_config(path, trust)

    link = directory / "hard-link-probe"
    os.link(path, link)                                         # shows that the save REPLACES the file instead of rewriting it
    inode = path.stat().st_ino
    umask = os.umask(0o077)                                     # the unit's UMask=0077
    try:
        done = runner.run([BINARY, "config", "update", "fake", "description=after", "--non-interactive", "--config", str(path)], timeout=60, capture=True)
    finally:
        os.umask(umask)
    assert done.returncode == 0
    link.unlink()
    assert path.stat().st_ino != inode                          # a new file, renamed over the old one
    assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_uid == os.getuid()
    assert sorted(p.name for p in directory.iterdir()) == ["rclone.conf"]       # no stray temporary file
    assert "after" in offsite.read_trusted_config(path, trust)  # and the next run's check accepts it


@posix_only
def test_rclone_keeps_the_mode_it_finds_so_the_tools_check_not_rclone_is_what_refuses_a_loose_file(runner, tmp_path):
    path = tmp_path / "rclone.conf"
    path.write_text("[fake]\ntype = local\n")
    path.chmod(0o644)
    done = runner.run([BINARY, "config", "update", "fake", "description=x", "--non-interactive", "--config", str(path)], timeout=60, capture=True)
    assert done.returncode == 0 and path.stat().st_mode & 0o777 == 0o644            # rclone does not tighten it
    with pytest.raises(offsite.Refused, match="no access at all"):
        offsite.read_trusted_config(path, offsite.ConfigTrust(os.getuid(), tmp_path))

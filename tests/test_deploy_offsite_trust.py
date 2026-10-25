"""Where the full-disk mode's OAuth configuration may be (deploy/offsite_backup.py, read_trusted_config; RUNBOOK 15.12).

An OAuth token that reaches the whole disk must be read from a path nobody but root can change, so that no one can swap
the file, a directory above it or a link between the check and rclone's use of it. The tests build that path under a
temporary directory and for the user running them (ConfigTrust(uid, root)); production is ConfigTrust(0, "/") and is not
an option of the command line. Each rule is broken in turn. POSIX only: the tool runs on the Linux host.
"""

import os
import re
import stat
import types

import pytest

from tests.deploy_support import posix_only
from tests.offsite_support import FULL_DISK_REMOTE, RCLONE_CONF_FULL_DISK, Rig, offsite
from tests.test_deploy_offsite_fulldisk import MODE, attempt, conf, write_conf
from tests.test_deploy_offsite_units import SERVICE, parse_unit, values

pytestmark = posix_only

TEXT = RCLONE_CONF_FULL_DISK


@pytest.fixture
def layout(tmp_path):
    """tmp/etc/house-helper/rclone.conf: directories 0755, the file 0600, all the current user's; trust root: tmp."""
    directory = tmp_path / "etc" / "house-helper"
    directory.mkdir(parents=True)
    for part in (tmp_path / "etc", directory):
        part.chmod(0o755)
    path = directory / "rclone.conf"
    path.write_text(TEXT)
    path.chmod(0o600)
    return types.SimpleNamespace(root=tmp_path, directory=directory, path=path, trust=offsite.ConfigTrust(os.getuid(), tmp_path))


def refused(layout, words, path=None, trust=None):
    with pytest.raises(offsite.Refused) as raised:
        offsite.read_trusted_config(path or layout.path, trust or layout.trust)
    assert re.search(words, str(raised.value)), str(raised.value)
    assert str(layout.root) not in str(raised.value)          # no path in the message, only the last component at most
    return str(raised.value)


# --- the good path, and the refresh --------------------------------------------------------------------------------


def test_a_private_root_style_path_is_read(layout):
    assert offsite.read_trusted_config(layout.path, layout.trust) == TEXT


def test_a_read_only_file_is_fine(layout):
    layout.path.chmod(0o400)
    assert offsite.read_trusted_config(layout.path, layout.trust) == TEXT


def test_a_refreshed_token_that_rclone_wrote_beside_the_file_and_renamed_over_it_passes_the_next_check(layout):
    # rclone saves a refreshed token as a NEW file in the same directory, renamed over the old one: the new file is the
    # owner's, mode 0600 under the unit's UMask=0077, and the directory (writable by its owner) allowed it.
    before = os.stat(layout.path).st_ino
    new = layout.directory / "rclone.conf.refresh"
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(TEXT.replace("test", "refreshed"))
    os.replace(new, layout.path)
    assert os.stat(layout.path).st_ino != before
    assert "refreshed" in offsite.read_trusted_config(layout.path, layout.trust)
    assert sorted(p.name for p in layout.directory.iterdir()) == ["rclone.conf"]


def test_the_service_does_not_sandbox_the_directory_rclone_must_write_the_refreshed_token_into():
    unit = parse_unit(SERVICE)
    strict = values(unit, "Service", "ProtectSystem")
    writable = " ".join(values(unit, "Service", "ReadWritePaths"))
    assert not strict or "/etc/house-helper" in writable        # hardening the unit later must keep the token refreshable
    assert not values(unit, "Service", "User")                  # runs as root: the owner the rule demands, the one that may write there
    assert values(unit, "Service", "UMask") == ["0077"]         # so that the refreshed file is 0600


# --- ownership --------------------------------------------------------------------------------------------------


def test_a_directory_owned_by_someone_else_is_refused(layout):
    other = offsite.ConfigTrust(os.getuid() + 1, layout.root)  # nobody the directories belong to
    assert "owned by root" in refused(layout, "owned by root", trust=other)


def test_a_file_owned_by_someone_else_is_refused(layout, monkeypatch):
    real = os.fstat

    def foreign_owner(descriptor):
        found = real(descriptor)
        return types.SimpleNamespace(
            st_mode=found.st_mode, st_nlink=found.st_nlink, st_size=found.st_size, st_dev=found.st_dev, st_ino=found.st_ino,
            st_uid=found.st_uid + 1,
        )

    monkeypatch.setattr(os, "fstat", foreign_owner)
    refused(layout, "must be owned by root")


# --- permissions of the file ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660, 0o604, 0o606, 0o666, 0o470, 0o750, 0o610, 0o601, 0o602, 0o604], ids=lambda m: oct(m))
def test_a_file_that_gives_group_or_others_anything_is_refused(layout, mode):
    layout.path.chmod(mode)
    refused(layout, "no access at all")


# --- permissions and kind of the directories ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o1777, 0o2775, 0o1775], ids=lambda m: oct(m))
@pytest.mark.parametrize("which", ["parent", "grandparent", "trust root"])
def test_a_directory_on_the_path_that_group_or_others_can_write_is_refused(layout, mode, which):
    target = {"parent": layout.directory, "grandparent": layout.root / "etc", "trust root": layout.root}[which]
    target.chmod(mode)
    try:
        message = refused(layout, "writable by group or others")
        assert target.name in message or which == "trust root"
    finally:
        target.chmod(0o755)


def test_a_directory_with_the_sticky_bit_is_not_trusted_either(layout):
    # /tmp-style: owned by root, writable by everyone, sticky. Others cannot replace the file, but can create files
    # and links beside it; the rule is deliberately "no group or other write at all".
    layout.directory.chmod(0o1777)
    try:
        refused(layout, "writable by group or others")
    finally:
        layout.directory.chmod(0o755)


def test_a_symbolic_link_above_the_file_is_refused(layout):
    link = layout.root / "alias"
    link.symlink_to(layout.root / "etc", target_is_directory=True)
    refused(layout, "symbolic link", path=link / "house-helper" / "rclone.conf")


def test_a_symbolic_link_as_the_file_is_refused_and_not_followed(layout):
    elsewhere = layout.root / "elsewhere.conf"
    elsewhere.write_text(TEXT)
    elsewhere.chmod(0o600)
    layout.path.unlink()
    layout.path.symlink_to(elsewhere)
    refused(layout, "cannot be opened")


def test_a_named_pipe_at_the_path_is_refused_without_waiting_for_a_writer(layout):
    layout.path.unlink()
    os.mkfifo(layout.path, 0o600)
    refused(layout, "regular file")


def test_a_directory_at_the_path_is_refused(layout):
    layout.path.unlink()
    layout.path.mkdir()
    refused(layout, "regular file")


def test_a_file_with_a_second_hard_link_is_refused(layout):
    os.link(layout.path, layout.root / "etc" / "second-name")
    refused(layout, "single link")


def test_a_file_larger_than_any_rclone_configuration_is_refused(layout):
    layout.path.write_text("[fake]\n" + "x = " + "y" * (offsite.CONFIG_LIMIT + 10) + "\n")
    layout.path.chmod(0o600)
    refused(layout, "single link, of at most 1 MiB")


def test_a_file_that_is_not_utf8_is_refused(layout):
    layout.path.write_bytes(b"[fake]\ntype = yandex\n\xff\xfe\n")
    layout.path.chmod(0o600)
    refused(layout, "cannot be read as an rclone configuration")


def test_a_file_that_is_replaced_while_it_is_being_checked_is_refused(layout, monkeypatch):
    real = os.lstat

    def swapped(path, *args, **kwargs):
        found = real(path, *args, **kwargs)
        if str(path) == str(layout.path):  # the look at the path AFTER the descriptor was read: another file now
            return types.SimpleNamespace(st_mode=found.st_mode, st_dev=found.st_dev, st_ino=found.st_ino + 1)
        return found

    monkeypatch.setattr(os, "lstat", swapped)
    refused(layout, "changed while it was being checked")


# --- the shape of the path -----------------------------------------------------------------------------------------------


def test_a_relative_path_is_refused(layout, monkeypatch):
    monkeypatch.chdir(layout.directory)
    refused(layout, "absolute path", path=offsite.Path("rclone.conf"))


def test_dot_dot_components_are_refused(layout):
    # (pathlib drops "." components by itself; ".." it keeps, and ".." is what climbs)
    refused(layout, "without . or .. components", path=layout.directory / ".." / "house-helper" / "rclone.conf")
    refused(layout, "without . or .. components", path=layout.root / "etc" / "house-helper" / ".." / ".." / "etc" / "house-helper" / "rclone.conf")


def test_a_path_outside_the_trusted_root_is_refused(layout, tmp_path_factory):
    stranger = tmp_path_factory.mktemp("elsewhere") / "rclone.conf"
    stranger.write_text(TEXT)
    stranger.chmod(0o600)
    refused(layout, "not under the directory", path=stranger)


def test_a_missing_file_is_refused_not_an_error(layout):
    layout.path.unlink()
    refused(layout, "cannot be opened")


# --- the rule is part of the authorization of BOTH commands, in the full-disk mode only ----------------------------------------


@pytest.fixture
def full(tmp_path):
    rig = Rig(tmp_path, full_disk=True)
    safe = tmp_path / "etc"
    safe.mkdir()
    safe.chmod(0o755)
    rig.config = safe / "rclone.conf"
    write_conf(rig, TEXT)
    return rig


@pytest.mark.parametrize("command", ["run", "list"])
def test_both_commands_refuse_an_unsafe_location_before_any_command_runs(full, command):
    full.config.parent.chmod(0o777)
    try:
        assert attempt(full, command, mode=MODE, remote=FULL_DISK_REMOTE) == offsite.EXIT_REFUSED, full.text
        assert "writable by group or others" in full.text
        assert full.fake.calls == [] and full.fake.remote_files() == {}
    finally:
        full.config.parent.chmod(0o755)


@pytest.mark.parametrize("command", ["run", "list"])
def test_both_commands_accept_the_safe_location(full, command):
    full.fake.folder().mkdir(parents=True)             # `list` of a folder that was never made is rclone's "not found"; `run` makes it
    assert attempt(full, command, mode=MODE, remote=FULL_DISK_REMOTE) == 0, full.text


def test_the_default_trust_is_root_and_the_filesystem_root_and_no_option_can_change_it():
    assert offsite.DEFAULT_TRUST == offsite.ConfigTrust(uid=0, root=offsite.Path("/"))
    parser = offsite._build_parser()
    options = {a for action in parser._actions for a in action.option_strings}
    for sub in parser._subparsers._group_actions[0].choices.values():
        options |= {a for action in sub._actions for a in action.option_strings}
    assert not {o for o in options if "trust" in o or "owner" in o or "uid" in o}


def test_with_the_production_trust_a_configuration_in_an_ordinary_temporary_directory_is_refused(tmp_path):
    rig = Rig(tmp_path, full_disk=True)
    code = offsite.main(rig.args(), runner=rig.fake, clock=lambda: rig.now, token=lambda: rig.token, log=rig.log.append)  # no config_trust: root and /
    assert code == offsite.EXIT_REFUSED, rig.text
    assert re.search(r"owned by root|writable by group or others", rig.text)
    assert rig.fake.calls == []


def test_the_application_folder_mode_does_not_apply_the_rule_it_is_the_narrow_mode(tmp_path):
    rig = Rig(tmp_path)
    tmp_path.chmod(0o777)                         # a config directory anyone can write: the existing mode never cared
    try:
        assert rig.run() == 0, rig.text
    finally:
        tmp_path.chmod(0o755)


def test_the_rehearsal_on_the_owners_own_machine_skips_only_this_rule(tmp_path):
    rig = Rig(tmp_path, full_disk=True)
    offsite.authorize_remote(rig.config, FULL_DISK_REMOTE, MODE, trust=None)          # accepted: not root's, not on the VPS
    with pytest.raises(offsite.Refused):
        offsite.authorize_remote(rig.config, FULL_DISK_REMOTE, MODE, trust=offsite.DEFAULT_TRUST)
    write_conf(rig, conf(app_folder="true"))
    with pytest.raises(offsite.Refused, match="contradicts"):                           # everything else still applies
        offsite.authorize_remote(rig.config, FULL_DISK_REMOTE, MODE, trust=None)

"""B: one canonical Compose-relative path resolver for every host command.

The Codex failure was a mixed relative/absolute credential path: the same approved
directory spelled one way by an explicit option and another way by the environment could
be reported as a conflict, or resolved against the caller's working directory under an
ordinary `sudo`. These tests pin the one rule:

* a relative value (option, environment or Compose default) is resolved against the
  compose file's directory, never the current working directory;
* every spelling is canonicalised, so symlinks and relative/absolute forms of one
  directory are one identity;
* a genuine disagreement is still refused;
* `maintenance.py` uses the same resolver, not its own default or `Path(...)` join.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from deploy.postgres import enroll
from deploy.postgres import maintenance

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_TEXT = (REPO_ROOT / "deploy" / "compose.postgres.yaml").read_text(
    encoding="utf-8"
)


def _compose(tmp_path: Path) -> Path:
    directory = tmp_path / "deploy"
    directory.mkdir()
    compose = directory / "compose.postgres.yaml"
    compose.write_text(COMPOSE_TEXT, encoding="utf-8")
    return compose


def _canonical(path: Path) -> str:
    return enroll._canonical_path(path)


def _approved(compose: Path) -> Path:
    return compose.parent / "postgres" / "credentials"


def test_a_relative_explicit_option_and_an_absolute_environment_agree(
    tmp_path, monkeypatch
):
    compose = _compose(tmp_path)
    approved = _approved(compose)
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(approved))
    # The option is spelled relative to the compose file, exactly as Compose would.
    spec = enroll.load_spec(compose, Path("postgres/credentials"))
    assert spec.credentials_directory == _canonical(approved)
    assert spec.bootstrap_credential_source == _canonical(
        approved / "postgres_superuser.password"
    )


def test_an_absolute_explicit_option_and_a_relative_environment_agree(
    tmp_path, monkeypatch
):
    compose = _compose(tmp_path)
    approved = _approved(compose)
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, "postgres/credentials")
    spec = enroll.load_spec(compose, approved)
    assert spec.credentials_directory == _canonical(approved)
    # The resolver used by `check-credentials` is the same contract.
    assert enroll.resolve_credentials_directory(compose) == Path(_canonical(approved))


def test_a_symlinked_spelling_is_the_same_directory(tmp_path, monkeypatch):
    compose = _compose(tmp_path)
    real = tmp_path / "real-credentials"
    real.mkdir()
    link = tmp_path / "linked-credentials"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create directory symlinks")
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(real))
    spec = enroll.load_spec(compose, link)
    assert spec.credentials_directory == _canonical(real)


def test_a_genuine_disagreement_is_still_refused(tmp_path, monkeypatch):
    compose = _compose(tmp_path)
    monkeypatch.setenv(
        enroll.CREDENTIALS_DIRECTORY_ENV, str(tmp_path / "from-the-environment")
    )
    with pytest.raises(enroll.DockerError) as refused:
        enroll.load_spec(compose, tmp_path / "from-the-option")
    assert refused.value.code == "credentials_conflict"


def _install_target_fakes(monkeypatch):
    # `maintenance.py` imports its own top-level `enroll` module (it puts deploy/postgres
    # on sys.path); patching the package module would not reach it.
    monkeypatch.setattr(maintenance.enroll, "validate_image", lambda *a, **k: None)
    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: {}),
    )
    monkeypatch.setattr(
        maintenance.enroll,
        "verify_mode_b",
        lambda *a, **k: types.SimpleNamespace(
            verdict=types.SimpleNamespace(ok=True, mismatches=(), completed={})
        ),
    )


def test_maintenance_resolves_the_same_contract(tmp_path, monkeypatch):
    compose = _compose(tmp_path)
    approved = _approved(compose)
    _install_target_fakes(monkeypatch)
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(approved))
    # `maintenance.py` has no repository default any more: the relative explicit value is
    # resolved against the compose file, and the environment agrees by identity.
    target = maintenance.verify_target(
        compose,
        tmp_path / "journal",
        credentials_dir=Path("postgres/credentials"),
    )
    assert target.credentials_dir == Path(_canonical(approved))
    assert target.spec.credentials_directory == _canonical(approved)


def test_maintenance_reads_the_environment_when_no_option_is_given(
    tmp_path, monkeypatch
):
    compose = _compose(tmp_path)
    approved = _approved(compose)
    _install_target_fakes(monkeypatch)
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, "postgres/credentials")
    target = maintenance.verify_target(compose, tmp_path / "journal")
    assert target.credentials_dir == Path(_canonical(approved))


def test_maintenance_has_no_hard_coded_credential_default():
    # The parser default must be None, so the shared precedence contract (not a repository
    # path that an ordinary `sudo` cannot compare with the registered environment) decides.
    parser = maintenance._parser()
    args = parser.parse_args(["backup", "--output", str(Path("out.tar"))])
    assert args.credentials_dir is None

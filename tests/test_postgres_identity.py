"""The independent test-cluster identity contract, checked OFFLINE (HH-DB-01 R3-01, R4-02, R4-04).

No Docker daemon, no PostgreSQL and no connection: `Docker` is given a fake `execute`
that answers like the CLI, and `establish` is given the same stand-in. What this proves
is the contract itself - which container is selected in which identity mode, which facts
are required, how the control file's identifier is parsed, and that every gap is a
refusal with fixed text - not how a real daemon behaves. The live behavior belongs to the
first authorized run.
"""

import json
import re
from pathlib import Path

import pytest

from tests import postgres_identity as identity

ROOT = Path(__file__).resolve().parent.parent
REMOTE = "203.0.113.10"


class Target:
    """The two fields `establish` reads from the harness's Target."""

    port = 55432
    address = "127.0.0.1"


CONTAINER = "a" * 64
RUN_ID = "1234567890"
RUN_ATTEMPT = "2"
CI_VOLUME = f"{identity.CI_VOLUME_PREFIX}-{RUN_ID}-{RUN_ATTEMPT}"
CI_ENVIRON = {
    identity.GITHUB_ACTIONS_ENV: "true",
    identity.CONTAINER_ID_ENV: CONTAINER,
    identity.RUN_ID_ENV: RUN_ID,
    identity.RUN_ATTEMPT_ENV: RUN_ATTEMPT,
}


def facts(**overrides):
    values = {
        "Id": CONTAINER,
        "State": {"Running": True, "Status": "running"},
        "Config": {
            "Image": identity.PINNED_IMAGE,
            "Labels": {
                "com.docker.compose.project": identity.COMPOSE_PROJECT,
                "com.docker.compose.service": identity.COMPOSE_SERVICE,
                "com.docker.compose.oneoff": "False",
            },
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": identity.DATA_VOLUME,
                "Destination": identity.DATA_PATH,
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Ports": {identity.DATABASE_PORT: [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
        },
    }
    for key, value in overrides.items():
        values[key] = value
    return values


def _truncated_listing(out):
    """What `docker ps --format "{{.ID}}"` prints without `--no-trunc` (R5.1): the first
    12 characters of every id."""
    return "\n".join(line.strip()[:12] for line in out.splitlines() if line.strip())


class Cli:
    """A docker CLI that answers from a dict of canned commands (first matching argv).

    It models the one real-CLI behavior the attachment check depends on (R5.1): `docker
    ps` truncates ids to 12 characters unless `--no-trunc` is given. A double that always
    answered full ids would hide exactly the defect this contract guards against."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        for matcher, answer in self.answers.items():
            if matcher in argv:
                if matcher == "ps" and "--no-trunc" not in argv:
                    code, out, err = answer
                    answer = (code, _truncated_listing(out), err)
                return answer
        raise AssertionError(f"unexpected docker command: {argv[:2]}")


def docker_with(**overrides):
    perform = overrides.pop("facts", facts())
    identifier = overrides.pop("identifier", "7000000000000000001")
    containers = overrides.pop("containers", [CONTAINER])
    volume_facts = overrides.pop("volume_facts", None)
    attached = overrides.pop("attached", None)
    mount = next(
        (
            entry
            for entry in perform.get("Mounts") or ()
            if isinstance(entry, dict) and entry.get("Destination") == identity.DATA_PATH
        ),
        None,
    )
    volume_name = mount.get("Name") if isinstance(mount, dict) else ""
    if volume_facts is None:
        labels = (perform.get("Config") or {}).get("Labels") or {}
        project = labels.get(identity.COMPOSE_PROJECT_LABEL) if isinstance(labels, dict) else None
        # R5-04: Compose labels the volumes IT creates; a container without Compose
        # provenance (the CI service container) implies an unlabelled volume.
        volume_labels = (
            {identity.COMPOSE_PROJECT_LABEL: project, identity.COMPOSE_VOLUME_LABEL: volume_name}
            if project
            else {}
        )
        volume_facts = {
            "Name": volume_name,
            "Driver": "local",
            "Scope": "local",
            "Labels": volume_labels,
            "Mountpoint": f"/var/lib/docker/volumes/{volume_name}/_data",
        }
    if attached is None:
        attached = [perform.get("Id") or CONTAINER]
    answers = {
        # Ordered: `docker volume inspect` also contains the word "inspect", and
        # `docker compose ... ps` also contains "ps".
        "volume": (0, json.dumps(volume_facts), ""),
        "compose": (0, "\n".join(containers) + ("\n" if containers else ""), ""),
        "inspect": (0, json.dumps(perform), ""),
        "ps": (0, "\n".join(attached) + ("\n" if attached else ""), ""),
        "exec": (0, f"Database system identifier:           {identifier}\n", ""),
    }
    answers.update(overrides)
    cli = Cli(answers)
    return identity.Docker(execute=cli), cli


def compose_docker(**overrides):
    return docker_with(**overrides)[0]


# --- the documented container and the contract's constants -----------------------------------


def test_the_contract_constants_are_the_documented_compose_file():
    text = (ROOT / "compose.test-postgres.yaml").read_text(encoding="utf-8")

    assert f"name: {identity.COMPOSE_PROJECT}" in text
    assert f"    image: {identity.PINNED_IMAGE}" in text
    assert f"{identity.DATA_VOLUME}:/var/lib/postgresql/data" in text
    assert "127.0.0.1:55432:5432" in text
    assert identity.COMPOSE_FILE.name == "compose.test-postgres.yaml"


def test_the_ci_mode_is_named_and_has_a_run_scoped_volume():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert identity.CONTAINER_ID_ENV in text
    assert "job.services.postgres.id" in text
    # It must be available to every step that opens a database: preflight, alembic,
    # the postgres-marked suites and the generic (PostgreSQL-backed) suite.
    assert text.count(f"{identity.CONTAINER_ID_ENV}: ${{{{ job.services.postgres.id }}}}") >= 4
    # R4-02: the service container's data volume is provisioned explicitly and is unique
    # to this run and attempt; that is the CI disposability evidence.
    assert identity.CI_VOLUME_PREFIX in text
    assert "github.run_id" in text and "github.run_attempt" in text
    assert f"{identity.CI_VOLUME_PREFIX}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}" in text


# --- the Docker surface -----------------------------------------------------------------------


def test_inspect_parses_the_json_document():
    docker = compose_docker()

    assert docker.inspect(CONTAINER)["Config"]["Image"] == identity.PINNED_IMAGE


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param((1, "", "no such container"), id="command-failed"),
        pytest.param((0, "not json", ""), id="not-json"),
        pytest.param((0, "[]", ""), id="not-an-object"),
    ],
)
def test_an_unreadable_inspection_is_an_unavailable_identity(answer):
    docker = identity.Docker(execute=Cli({"inspect": answer}))

    with pytest.raises(identity.IdentityError) as refused:
        docker.inspect(CONTAINER)

    assert refused.value.code == "cluster_identity_unavailable"
    assert "not_a_real" not in str(refused.value)  # no value from the command


def test_the_control_file_identifier_is_read_from_pg_controldata():
    docker, cli = docker_with(identifier="7000000000000000042")

    assert docker.system_identifier(CONTAINER) == "7000000000000000042"
    # R4-04: the locale is fixed, so the line's spelling is deterministic.
    command = next(call for call in cli.calls if "exec" in call)
    assert "LC_ALL=C" in command and "LANG=C" in command
    assert command[-1] == identity.DATA_PATH


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param((1, "", "container not running"), id="exec-failed"),
        pytest.param((1, "Database system identifier:           42\n", ""), id="failed-exit-with-identifier"),
        pytest.param((0, "pg_controldata: error\n", ""), id="no-identifier"),
        pytest.param((0, "Database system identifier: unknown\n", ""), id="not-a-number"),
        pytest.param((0, "Database system identifier:           0\n", ""), id="zero"),
        pytest.param(
            (
                0,
                "Database system identifier:           42\n"
                "Database system identifier:           43\n",
                "",
            ),
            id="two-identifiers",
        ),
        pytest.param(
            (0, "WARNING: could not read file\nDatabase system identifier:           42\n", ""),
            id="warning-output",
        ),
        pytest.param(
            (0, "Database system identifier:           42\n", "ERROR: invalid control file\n"),
            id="error-on-stderr",
        ),
        # R5-06: identifier evidence is evaluated across BOTH streams; a conflicting
        # identifier on stderr is contradictory, not discarded.
        pytest.param(
            (
                0,
                "Database system identifier:           42\n",
                "Database system identifier:           43\n",
            ),
            id="conflicting-identifier-on-stderr",
        ),
        pytest.param(
            (0, "Database system identifier:           42\n", "Database system identifier:           42\n"),
            id="duplicate-identifier-on-both-streams",
        ),
    ],
)
def test_an_unreadable_or_untrustworthy_control_file_is_refused(answer):
    docker = identity.Docker(execute=Cli({"exec": answer}))

    with pytest.raises(identity.IdentityError) as refused:
        docker.system_identifier(CONTAINER)

    assert refused.value.code == "cluster_identity_unavailable"


def test_compose_lists_the_containers_of_the_documented_project():
    docker = compose_docker(containers=[CONTAINER, "b" * 64])

    assert docker.compose_container_ids() == [CONTAINER, "b" * 64]


def test_a_compose_command_that_fails_is_an_unavailable_identity():
    docker = identity.Docker(execute=Cli({"compose": (1, "", "no such project")}))

    with pytest.raises(identity.IdentityError) as refused:
        docker.compose_container_ids()

    assert refused.value.code == "cluster_identity_unavailable"


def test_a_compose_listing_that_is_not_a_container_id_is_refused():
    docker = identity.Docker(execute=Cli({"compose": (0, "not-an-id\n", "")}))

    with pytest.raises(identity.IdentityError) as refused:
        docker.compose_container_ids()

    assert refused.value.code == "cluster_identity_invalid"


# --- establish: what it accepts and what it refuses ---------------------------------------------


def test_the_documented_container_establishes_the_identity():
    cluster = identity.establish(Target(), environ={}, docker=compose_docker())

    assert cluster == identity.ClusterIdentity(
        system_identifier="7000000000000000001",
        container_id=CONTAINER,
        image=identity.PINNED_IMAGE,
        volume=identity.DATA_VOLUME,
        mode="compose",
        port=55432,
    )


def test_an_environment_variable_alone_is_never_identity_proof():
    # The variable names a container that cannot be inspected ...
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(
            Target(),
            environ={identity.CONTAINER_ID_ENV: CONTAINER},
            docker=identity.Docker(execute=Cli({"inspect": (1, "", "no such container")})),
        )
    # R4-02: alone, it does not even select the CI mode - that is a conflict.
    assert refused.value.code == "cluster_identity_conflict"

    # ... and with the CI evidence it still only selects: the facts must agree.
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(
            Target(),
            environ=CI_ENVIRON,
            docker=docker_with(facts=facts(**{"State": {"Running": False, "Status": "exited"}}))[0],
        )
    assert refused.value.code == "cluster_identity_unavailable"


@pytest.mark.parametrize(
    "override, code",
    [
        pytest.param({"containers": []}, "cluster_identity_missing", id="no-container"),
        pytest.param({"containers": [CONTAINER, "b" * 64]}, "cluster_identity_ambiguous", id="two-containers"),
        pytest.param(
            {"facts": facts(**{"NetworkSettings": {"Ports": {identity.DATABASE_PORT: []}}})},
            "cluster_identity_ambiguous",
            id="no-port-binding",
        ),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "NetworkSettings": {
                            "Ports": {
                                identity.DATABASE_PORT: [
                                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                                    {"HostIp": "0.0.0.0", "HostPort": "55433"},
                                ]
                            }
                        }
                    }
                )
            },
            "cluster_identity_ambiguous",
            id="two-port-bindings",
        ),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "NetworkSettings": {
                            "Ports": {identity.DATABASE_PORT: [{"HostIp": REMOTE, "HostPort": "55432"}]}
                        }
                    }
                )
            },
            "cluster_identity_mismatch",
            id="remote-publication",
        ),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "NetworkSettings": {
                            "Ports": {
                                identity.DATABASE_PORT: [
                                    {"HostIp": "127.0.0.1", "HostPort": "55432"}
                                ],
                                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}],
                            }
                        }
                    }
                )
            },
            "cluster_identity_mismatch",
            id="an-extra-published-port",
        ),
        pytest.param({"facts": facts(**{"Mounts": []})}, "cluster_identity_mismatch", id="no-volume"),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "Mounts": [
                            {
                                "Type": "volume",
                                "Name": identity.DATA_VOLUME,
                                "Destination": identity.DATA_PATH,
                                "RW": False,
                            }
                        ]
                    }
                )
            },
            "cluster_identity_mismatch",
            id="read-only-volume",
        ),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "Mounts": [
                            {"Type": "volume", "Name": identity.DATA_VOLUME, "Destination": identity.DATA_PATH}
                        ]
                    }
                )
            },
            "cluster_identity_mismatch",
            id="writability-not-stated",
        ),
        pytest.param(
            {"facts": facts(**{"Mounts": [{"Type": "bind", "Source": "/srv/pg", "Destination": identity.DATA_PATH, "RW": True}]})},
            "cluster_identity_mismatch",
            id="bind-mount",
        ),
        # R5-06: a well-formed binding does not license discarding a malformed sibling.
        pytest.param(
            {
                "facts": facts(
                    **{
                        "NetworkSettings": {
                            "Ports": {
                                identity.DATABASE_PORT: [
                                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                                    "not-a-binding",
                                ]
                            }
                        }
                    }
                )
            },
            "cluster_identity_invalid",
            id="malformed-port-binding-beside-a-valid-one",
        ),
        pytest.param(
            {
                "facts": facts(
                    **{
                        "NetworkSettings": {
                            "Ports": {
                                identity.DATABASE_PORT: [
                                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                                    {"HostIp": "127.0.0.1"},
                                ]
                            }
                        }
                    }
                )
            },
            "cluster_identity_invalid",
            id="incomplete-port-binding-beside-a-valid-one",
        ),
        pytest.param({"facts": {}}, "cluster_identity_unavailable", id="empty-description"),
        pytest.param({"facts": {"State": "running"}}, "cluster_identity_unavailable", id="unreadable-state"),
        pytest.param(
            {"facts": facts(**{"State": {"Running": True, "Status": "exited"}})},
            "cluster_identity_unavailable",
            id="running-state-contradicts-status",
        ),
        pytest.param(
            {"facts": facts(**{"State": {"Running": False, "Status": "running"}})},
            "cluster_identity_unavailable",
            id="stopped-state-contradicts-status",
        ),
        pytest.param(
            {"facts": facts(**{"Config": {"Image": identity.PINNED_IMAGE, "Labels": {}}})},
            "cluster_identity_mismatch",
            id="no-compose-labels",
        ),
        pytest.param(
            {"facts": facts(**{"Id": "b" * 64})},
            "cluster_identity_mismatch",
            id="inspected-another-container",
        ),
        pytest.param({"facts": facts(**{"Id": None})}, "cluster_identity_unavailable", id="no-container-id"),
        pytest.param(
            {"facts": facts(**{"Id": "not-a-container-id"})},
            # R5-04: the volume-attachment listing is parsed strictly too; a malformed id
            # is refused there before the container/selection comparison.
            "cluster_identity_invalid",
            id="malformed-container-id",
        ),
        pytest.param(
            {"facts": facts(**{"Config": {"Image": "postgres:16", "Labels": facts()["Config"]["Labels"]}})},
            "cluster_identity_mismatch",
            id="tag-instead-of-digest",
        ),
        # R4-04: a container inspection has no RepoDigests field; it is never evidence.
        pytest.param(
            {
                "facts": facts(
                    **{
                        "Config": {"Image": "postgres:16", "Labels": facts()["Config"]["Labels"]},
                        "RepoDigests": [identity.PINNED_IMAGE],
                    }
                )
            },
            "cluster_identity_mismatch",
            id="repo-digests-do-not-substitute",
        ),
    ],
)
def test_establish_refuses_every_incomplete_or_ambiguous_identity(override, code):
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ={}, docker=compose_docker(**override))

    assert refused.value.code == code


# --- R4-02: the two identity modes are explicit and mutually exclusive --------------------------


def test_ci_uses_the_runner_scoped_service_container_and_its_run_scoped_volume():
    cluster = identity.establish(
        Target(),
        environ=CI_ENVIRON,
        docker=compose_docker(
            facts=facts(
                **{
                    "Config": {"Image": identity.PINNED_IMAGE, "Labels": {}},
                    "Mounts": [
                        {"Type": "volume", "Name": CI_VOLUME, "Destination": identity.DATA_PATH, "RW": True}
                    ],
                }
            ),
            # the service container is not a Compose container; no Compose listing is used
            containers=[],
        ),
    )

    assert cluster.mode == "ci-service" and cluster.container_id == CONTAINER
    assert cluster.volume == CI_VOLUME


def test_a_container_id_alone_never_selects_the_ci_contract():
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(
            Target(), environ={identity.CONTAINER_ID_ENV: CONTAINER}, docker=compose_docker()
        )

    assert refused.value.code == "cluster_identity_conflict"


def test_ci_without_a_container_id_is_refused():
    environ = {identity.GITHUB_ACTIONS_ENV: "true", identity.RUN_ID_ENV: RUN_ID,
               identity.RUN_ATTEMPT_ENV: RUN_ATTEMPT}

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=environ, docker=compose_docker())

    assert refused.value.code == "cluster_identity_missing"


@pytest.mark.parametrize("missing", [identity.RUN_ID_ENV, identity.RUN_ATTEMPT_ENV])
def test_ci_without_its_run_evidence_is_incomplete(missing):
    environ = dict(CI_ENVIRON)
    environ.pop(missing)

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=environ, docker=compose_docker())

    assert refused.value.code == "cluster_identity_incomplete"


@pytest.mark.parametrize("value", ["", "not-a-container-id", "XYZ"])
def test_ci_must_name_a_real_container_id(value):
    environ = dict(CI_ENVIRON, **{identity.CONTAINER_ID_ENV: value})

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=environ, docker=compose_docker())

    assert refused.value.code in ("cluster_identity_missing", "cluster_identity_invalid")


def test_ci_refuses_a_container_of_the_local_compose_project():
    # The local Compose facts, including the project label, cannot be the CI contract.
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=CI_ENVIRON, docker=compose_docker())

    assert refused.value.code == "cluster_identity_conflict"


def test_ci_refuses_a_volume_that_is_not_this_runs():
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(
            Target(),
            environ=CI_ENVIRON,
            docker=compose_docker(
                facts=facts(**{"Config": {"Image": identity.PINNED_IMAGE, "Labels": {}}})
            ),
        )

    assert refused.value.code == "cluster_identity_mismatch"


def test_the_ci_volume_name_encodes_the_run_and_attempt():
    assert identity.ci_volume(CI_ENVIRON) == CI_VOLUME
    for broken in ({}, {identity.RUN_ID_ENV: "x", identity.RUN_ATTEMPT_ENV: "1"}):
        with pytest.raises(identity.IdentityError) as refused:
            identity.ci_volume(broken)
        assert refused.value.code == "cluster_identity_incomplete"


def test_an_unexpected_github_actions_value_is_refused():
    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(
            Target(), environ={identity.GITHUB_ACTIONS_ENV: "maybe"}, docker=compose_docker()
        )

    assert refused.value.code == "cluster_identity_invalid"


# --- revalidation -------------------------------------------------------------------------------


def test_a_hand_built_identity_is_equal_but_not_issued():
    """R4-01: the facts can be copied, the seal cannot. `PrivilegedBoundary` accepts only
    an identity this module verified."""
    genuine = identity.establish(Target(), environ={}, docker=compose_docker())
    forged = identity.ClusterIdentity(
        system_identifier=genuine.system_identifier,
        container_id=genuine.container_id,
        image=genuine.image,
        volume=genuine.volume,
        mode=genuine.mode,
        port=genuine.port,
    )

    assert forged == genuine  # equality is about the facts
    assert identity.issued(genuine) is True
    assert identity.issued(forged) is False


def test_revalidation_of_a_changed_cluster_refuses():
    cluster = identity.establish(Target(), environ={}, docker=compose_docker())

    with pytest.raises(identity.IdentityError) as refused:
        cluster.revalidate(compose_docker(identifier="7000000000000000099"), Target())

    assert refused.value.code == "cluster_identity_changed"


def test_revalidation_of_a_changed_volume_or_container_refuses():
    cluster = identity.establish(Target(), environ={}, docker=compose_docker())
    substituted = facts(**{"Mounts": [{"Type": "volume", "Name": "another-volume", "Destination": identity.DATA_PATH, "RW": True}]})

    with pytest.raises(identity.IdentityError) as refused:
        cluster.revalidate(compose_docker(facts=substituted), Target())

    assert refused.value.code == "cluster_identity_mismatch"


def test_revalidation_of_the_same_cluster_returns_the_same_identity():
    docker = compose_docker()
    cluster = identity.establish(Target(), environ={}, docker=docker)

    assert cluster.revalidate(docker, Target()) == cluster


def test_the_refusal_texts_carry_no_secret_or_endpoint():
    for docker in (
        compose_docker(facts=facts(**{"NetworkSettings": {"Ports": {}}})),
        compose_docker(facts=facts(**{"Config": {"Image": "elsewhere@" + "x" * 10}})),
    ):
        with pytest.raises(identity.IdentityError) as refused:
            identity.establish(Target(), environ={}, docker=docker)

        message = str(refused.value)
        assert not re.search(r"postgresql|password|@", message), message


# --- R5-01: an identity is only valid while its established facts are unchanged -----------------


def test_an_unissued_identity_cannot_be_promoted_by_revalidation():
    """Failure A: a caller-built identity (caller-chosen mode and a foreign volume) used to
    become issued through `revalidate()` without the initial eligibility checks."""
    forged = identity.ClusterIdentity(
        system_identifier="7000000000000000001",
        container_id=CONTAINER,
        image=identity.PINNED_IMAGE,
        volume="a-foreign-volume",
        mode="ci-service",
        port=55432,
    )
    docker, cli = docker_with()

    with pytest.raises(identity.IdentityError) as refused:
        forged.revalidate(docker, Target(), environ={})

    assert refused.value.code == "cluster_identity_missing"
    assert identity.issued(forged) is False
    # Nothing was even inspected: revalidation is not a route into issuance.
    assert cli.calls == []


def test_revalidation_reruns_the_initial_eligibility_checks():
    """R5-01 item 2: the located mode/container/volume must still be the established ones.
    Here the CI contract is asked to revalidate a Compose identity (or vice versa)."""
    cluster = identity.establish(Target(), environ={}, docker=compose_docker())

    with pytest.raises(identity.IdentityError) as refused:
        cluster.revalidate(compose_docker(), Target(), environ=CI_ENVIRON)

    assert refused.value.code == "cluster_identity_changed"
    assert identity.issued(cluster) is True  # the original identity is untouched


def test_a_replace_copy_with_an_altered_fact_is_not_issued():
    """Failure B: `dataclasses.replace()` copies the proof token. Authorization is bound to
    the FACTS recorded at issuance, so the altered copy is refused."""
    import dataclasses

    genuine = identity.establish(Target(), environ={}, docker=compose_docker())
    altered = dataclasses.replace(genuine, system_identifier="7999999999999999999")

    assert identity.issued(genuine) is True
    assert identity.issued(altered) is False  # the copied proof does not carry altered facts


def test_a_replace_copy_without_an_altered_fact_is_still_the_same_identity():
    import dataclasses

    genuine = identity.establish(Target(), environ={}, docker=compose_docker())

    assert identity.issued(dataclasses.replace(genuine)) is True


# --- R5-04: the volume's own provenance, not just its name -------------------------------------


def test_a_volume_without_compose_provenance_is_a_foreign_volume():
    docker = compose_docker(
        volume_facts={
            "Name": identity.DATA_VOLUME,
            "Driver": "local",
            "Scope": "local",
            "Labels": {},  # a manually created volume with the expected name
        }
    )

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ={}, docker=docker)

    assert refused.value.code == "cluster_identity_mismatch"


def test_a_differently_named_volume_record_is_refused():
    docker = compose_docker(
        volume_facts={
            "Name": "another-volume",
            "Driver": "local",
            "Scope": "local",
            "Labels": {
                identity.COMPOSE_PROJECT_LABEL: identity.COMPOSE_PROJECT,
                identity.COMPOSE_VOLUME_LABEL: "another-volume",
            },
        }
    )

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ={}, docker=docker)

    assert refused.value.code == "cluster_identity_mismatch"


def test_a_volume_attached_to_another_container_is_refused():
    docker = compose_docker(attached=[CONTAINER, "b" * 64])

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ={}, docker=docker)

    assert refused.value.code == "cluster_identity_mismatch"


def test_a_compose_volume_is_not_evidence_in_ci_mode():
    docker = compose_docker(
        facts=facts(**{"Config": {"Image": identity.PINNED_IMAGE, "Labels": {}}}),
        volume_facts={
            "Name": identity.DATA_VOLUME,
            "Driver": "local",
            "Scope": "local",
            "Labels": {
                identity.COMPOSE_PROJECT_LABEL: identity.COMPOSE_PROJECT,
                identity.COMPOSE_VOLUME_LABEL: identity.DATA_VOLUME,
            },
        },
    )

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=CI_ENVIRON, docker=docker)

    assert refused.value.code in (
        "cluster_identity_conflict",
        "cluster_identity_mismatch",
    )


def test_another_compose_project_container_is_refused_in_ci_mode():
    """R5-04 item 5: not only THIS repository's Compose project - any Compose-managed
    container, including another project's or a one-off, is refused in CI mode."""
    labels = {
        identity.COMPOSE_PROJECT_LABEL: "another-project",
        identity.COMPOSE_SERVICE_LABEL: "postgres",
        identity.COMPOSE_ONEOFF_LABEL: "False",
    }
    run_volume = identity.ci_volume(CI_ENVIRON)
    docker = compose_docker(
        facts=facts(
            **{
                "Config": {"Image": identity.PINNED_IMAGE, "Labels": labels},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": run_volume,
                        "Destination": identity.DATA_PATH,
                        "RW": True,
                    }
                ],
            }
        ),
        volume_facts={
            "Name": run_volume,
            "Driver": "local",
            "Scope": "local",
            "Labels": {},
        },
    )

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=CI_ENVIRON, docker=docker)

    assert refused.value.code == "cluster_identity_conflict"


def test_runner_scoped_evidence_is_conflicting_in_local_mode():
    for name in (identity.RUN_ID_ENV, identity.RUN_ATTEMPT_ENV):
        with pytest.raises(identity.IdentityError) as refused:
            identity.establish(Target(), environ={name: "12345"}, docker=compose_docker())
        assert refused.value.code == "cluster_identity_conflict"


# --- R5.1: the volume attachment listing names FULL container ids ------------------------------


def ci_facts():
    """The CI service container (R4-02): no Compose provenance, the run-scoped volume."""
    return facts(
        **{
            "Config": {"Image": identity.PINNED_IMAGE, "Labels": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": CI_VOLUME,
                    "Destination": identity.DATA_PATH,
                    "RW": True,
                }
            ],
        }
    )


MODE_ENVIRONS = {"compose": {}, "ci-service": CI_ENVIRON}


def mode_docker(mode, **overrides):
    """The canned facts and attachment listing of one identity mode (R4-02)."""
    if mode == "ci-service":
        overrides.setdefault("facts", ci_facts())
    return compose_docker(**overrides)


@pytest.mark.parametrize("mode", ["compose", "ci-service"])
def test_a_full_attachment_id_is_accepted(mode):
    cluster = identity.establish(
        Target(), environ=MODE_ENVIRONS[mode], docker=mode_docker(mode)
    )

    assert cluster.mode == mode
    assert identity.issued(cluster) is True


@pytest.mark.parametrize("mode", ["compose", "ci-service"])
def test_a_truncated_attachment_id_is_refused(mode):
    """R5.1: 12 characters is what `docker ps` prints without `--no-trunc`. It is not a
    container identity: the strict parser refuses it outright, never matching its prefix."""
    docker = mode_docker(mode, ps=(0, CONTAINER[:12] + "\n", ""))

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=MODE_ENVIRONS[mode], docker=docker)

    assert refused.value.code == "cluster_identity_invalid"


@pytest.mark.parametrize("mode", ["compose", "ci-service"])
def test_a_different_full_attachment_id_is_refused(mode):
    docker = mode_docker(mode, attached=["b" * 64])

    with pytest.raises(identity.IdentityError) as refused:
        identity.establish(Target(), environ=MODE_ENVIRONS[mode], docker=docker)

    assert refused.value.code == "cluster_identity_mismatch"


def test_the_attachment_listing_requests_non_truncated_ids():
    """R5.1: the command must ask Docker for full ids. The modeled CLI truncates the
    listing unless `--no-trunc` is present, so this also proves full-id comparison can
    succeed against real Docker behavior."""
    docker, cli = docker_with()

    assert docker.volume_containers(identity.DATA_VOLUME) == [CONTAINER]

    command = next(call for call in cli.calls if "ps" in call)
    assert command[:2] == ["docker", "ps"]
    assert "--no-trunc" in command
    assert "--format" in command


def test_the_attachment_listing_parser_refuses_a_truncated_id():
    docker = identity.Docker(execute=Cli({"ps": (0, CONTAINER[:12] + "\n", "")}))

    with pytest.raises(identity.IdentityError) as refused:
        docker.volume_containers(identity.DATA_VOLUME)

    assert refused.value.code == "cluster_identity_invalid"

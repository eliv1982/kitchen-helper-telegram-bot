"""WP-2D: the frozen recovery-point contract (offline tests).

These tests do not need Docker or a database. They pin the flags, the manifest shape,
the scoped sequence extraction parser, the sequence invariant and the archive member
order. The real PostgreSQL rehearsal is the separate live evidence; this suite is what
CI can always run.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from house_helper.persistence import pg_artifacts
from house_helper.persistence import pg_backup as backup
from house_helper.persistence import pg_contract


def _manifest(**overrides):
    tables = {
        name: {"rows": 0, "digest": backup.sha256_bytes(b"")}
        for name in pg_contract.expected_tables()
    }
    tables["messages"] = {"rows": 3, "digest": backup.sha256_bytes(b"row")}
    sequence = {
        "name": "messages_id_seq",
        "last_value": 5,
        "is_called": True,
        "next_allocatable": 6,
        "max_messages_id": 3,
        "source": "archive:SEQUENCE SET",
        "unowned": False,
        "owner": "hh_owner",
    }
    value = {
        "format": backup.FORMAT_V2,
        "database": "postgresql",
        "server_version_num": 160015,
        "system_identifier": "7709166311476633635",
        "pg_dump_version": "16.15",
        "dump_format": "custom",
        "compression": "zstd",
        "alembic_revision": "0014_user_measure_overrides",
        "encoding": "UTF8",
        "datcollate": "C",
        "datctype": "C",
        "roles_expected": list(pg_contract.PROJECT_ROLE_NAMES),
        "acl_contract_sha256": backup.acl_contract_digest(),
        "tables": tables,
        "sequence": sequence,
        "snapshot": {
            "bytes": 1234,
            "sha256": backup.sha256_bytes(b"dump"),
        },
        "dump_settings": dict(backup._EXPECTED_DUMP_SETTINGS),
    }
    value.update(overrides)
    return value


def test_the_dump_flags_are_the_frozen_set():
    assert backup.DUMP_FLAGS == (
        "--format=custom",
        "--compress=zstd",
        "--schema=public",
        "--encoding=UTF8",
        "--no-owner",
        "--no-privileges",
        "--no-tablespaces",
        "--no-subscriptions",
        "--no-publications",
        "--no-large-objects",
    )


def test_a_valid_manifest_has_no_failures():
    assert backup.manifest_failures(_manifest()) == ()


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"format": "house-helper-recovery-point/1"}, "manifest:format"),
        ({"database": "sqlite"}, "manifest:database"),
        ({"server_version_num": 150000}, "manifest:server_version_num"),
        ({"system_identifier": "not-a-number"}, "manifest:system_identifier"),
        ({"pg_dump_version": "15.4"}, "manifest:pg_dump_version"),
        ({"dump_format": "plain"}, "manifest:dump_format"),
        ({"compression": "gzip"}, "manifest:compression"),
        ({"alembic_revision": ""}, "manifest:alembic_revision"),
        ({"encoding": "LATIN1"}, "manifest:encoding"),
        ({"roles_expected": ["hh_owner"]}, "manifest:roles_expected"),
        ({"acl_contract_sha256": "0" * 64}, "manifest:acl_contract_sha256"),
        ({"dump_settings": {}}, "manifest:dump_settings"),
        ({"snapshot": {"bytes": 0, "sha256": "0" * 64}}, "manifest:snapshot_bytes"),
        ({"snapshot": {"bytes": 1, "sha256": "x"}}, "manifest:snapshot_sha256"),
    ],
)
def test_manifest_refuses_each_frozen_violation(overrides, expected):
    failures = backup.manifest_failures(_manifest(**overrides))
    assert expected in failures


def test_a_duplicate_manifest_field_is_not_silently_accepted():
    value = _manifest()
    value["extra"] = True
    assert "manifest:keys" in backup.manifest_failures(value)


def test_the_table_set_is_exactly_the_image_models():
    value = _manifest()
    value["tables"].pop(next(iter(value["tables"])))
    assert "manifest:table_set" in backup.manifest_failures(value)


@pytest.mark.parametrize(
    "last_value, is_called, maximum, expected",
    [
        (1, False, None, ()),          # never-used sequence
        (1, True, 1, ()),              # next 2 > max 1
        (5, False, 4, ()),             # setval(N, false), next N
        (5, True, 5, ()),
        (4, True, 4, ()),              # next 5 > 4
        (3, True, 3, ()),              # next 4 > 3
        (3, False, 3, ("sequence:behind_rows",)),
        (1, False, 1, ("sequence:behind_rows",)),
    ],
)
def test_the_next_allocatable_value_must_be_ahead_of_the_rows(
    last_value, is_called, maximum, expected
):
    sequence = {
        "name": "messages_id_seq",
        "last_value": last_value,
        "is_called": is_called,
        "next_allocatable": last_value + (1 if is_called else 0),
        "max_messages_id": maximum,
        "source": "archive:SEQUENCE SET",
        "unowned": False,
        "owner": "hh_owner",
    }
    assert backup.sequence_failures(sequence, max_messages_id=maximum) == expected


def test_sequence_parameters_outside_the_bounds_are_refused():
    sequence = {
        "name": "messages_id_seq",
        "last_value": 10,
        "is_called": True,
        "next_allocatable": 11,
        "max_messages_id": 3,
        "source": "archive:SEQUENCE SET",
        "unowned": False,
        "owner": "hh_owner",
    }
    assert "sequence:above_max" in backup.sequence_failures(
        sequence, max_messages_id=3, seqmin=1, seqmax=9
    )
    assert "sequence:below_min" in backup.sequence_failures(
        {**sequence, "last_value": 0, "next_allocatable": 1},
        max_messages_id=3,
        seqmin=1,
        seqmax=100,
    )
    exhausted = {**sequence, "last_value": 9, "next_allocatable": 10}
    assert "sequence:exhausted" in backup.sequence_failures(
        exhausted, max_messages_id=3, seqmin=1, seqmax=9
    )


def test_the_restored_raw_pair_is_compared_exactly():
    archive = _manifest()["sequence"]
    assert backup.compare_sequence(archive, (5, True)) == ()
    assert backup.compare_sequence(archive, (5, False)) == ("sequence:is_called_mismatch",)
    assert backup.compare_sequence(archive, (4, True)) == ("sequence:last_value_mismatch",)


# --- the scoped SEQUENCE SET extraction ------------------------------------------------------


PINNED_EXTRACTION = """\
--
-- PostgreSQL database dump
--

\\restrict ncgud1TBx6WRwfm0cUOBSerrI3iV2aNZuLCD12XOrUoa12xOxS5Y91cyMWRO6QT

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: messages_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.messages_id_seq', 1, false);


--
-- PostgreSQL database dump complete
--

\\unrestrict ncgud1TBx6WRwfm0cUOBSerrI3iV2aNZuLCD12XOrUoa12xOxS5Y91cyMWRO6QT

"""


def test_the_pinned_sequence_extraction_is_accepted():
    parsed = backup.parse_sequence_extraction(PINNED_EXTRACTION)
    assert (parsed.last_value, parsed.is_called) == (1, False)


def test_the_pinned_extraction_with_a_different_value_is_parsed_as_data():
    parsed = backup.parse_sequence_extraction(
        PINNED_EXTRACTION.replace(
            "setval('public.messages_id_seq', 1, false)",
            "setval('public.messages_id_seq', 41, true)",
        )
    )
    assert (parsed.last_value, parsed.is_called) == (41, True)


def _replace(pattern, replacement, text=PINNED_EXTRACTION):
    assert pattern in text
    return text.replace(pattern, replacement)


@pytest.mark.parametrize(
    "text",
    [
        _replace("SELECT pg_catalog.setval", "SELECT 1;\nSELECT pg_catalog.setval"),
        _replace("SET statement_timeout = 0;", "SET statement_timeout = 10;"),
        _replace("SET row_security = off;", "SET row_security = on;"),
        _replace(
            "SET row_security = off;",
            "SET row_security = off;\nSET row_security = off;",
        ),
        _replace("setval('public.messages_id_seq', 1, false);",
                 "setval('public.messages_id_seq', 1, false);\n"
                 "SELECT pg_catalog.setval('public.messages_id_seq', 2, true);"),
        _replace("setval('public.messages_id_seq'", "setval('public.other_seq'"),
        _replace("setval('public.messages_id_seq', 1, false)", "setval('public.messages_id_seq', 999999999999999999999, false)"),
        _replace("\\unrestrict ", "\\unrestrict other-key"),
        _replace("\\restrict ", "\\restrict-key "),
        _replace("-- PostgreSQL database dump complete", "-- an unapproved comment"),
        _replace("SET row_security = off;", "\\copy (SELECT 1) TO '/tmp/x'"),
        _replace("SELECT pg_catalog.setval('public.messages_id_seq', 1, false);", ""),
        PINNED_EXTRACTION.replace("(true|false)", "").replace("false", "FALSE"),
    ],
)
def test_the_parser_rejects_everything_outside_the_frozen_envelope(text):
    with pytest.raises(backup.BackupError):
        backup.parse_sequence_extraction(text)


def test_an_unbalanced_restrict_wrapper_is_refused():
    text = PINNED_EXTRACTION.replace(
        "\\unrestrict ncgud1TBx6WRwfm0cUOBSerrI3iV2aNZuLCD12XOrUoa12xOxS5Y91cyMWRO6QT\n", ""
    )
    with pytest.raises(backup.BackupError):
        backup.parse_sequence_extraction(text)


# --- TOC selection ---------------------------------------------------------------------------


REAL_TOC = """\
;
; Archive created at 2026-11-17 18:29:52 UTC
;     dbname: house_helper
;     TOC Entries: 147
;     Compression: zstd
;
5; 2615 2200 SCHEMA - public hh_owner
222; 1259 16502 SEQUENCE public messages_id_seq hh_owner
3808; 0 0 SEQUENCE OWNED BY public messages_id_seq hh_owner
3809; 0 0 SEQUENCE SET public messages_id_seq hh_owner
"""


def test_the_toc_selects_exactly_one_sequence_set_entry():
    entries = backup.parse_toc(REAL_TOC)
    selected = backup.select_sequence_set(entries)
    assert selected.object_class == "SEQUENCE SET"
    assert selected.list_line() == (
        "3809; 0 0 SEQUENCE SET public messages_id_seq hh_owner"
    )


def test_zero_or_two_sequence_set_entries_are_refused():
    entries = backup.parse_toc(REAL_TOC)
    without = tuple(entry for entry in entries if entry.object_class != "SEQUENCE SET")
    with pytest.raises(backup.BackupError) as refused:
        backup.select_sequence_set(without)
    assert refused.value.code == "sequence_set_selection"
    duplicate = entries + (
        backup.TocEntry(3810, 0, 0, "SEQUENCE SET", "public", "messages_id_seq", "hh_owner"),
    )
    with pytest.raises(backup.BackupError) as refused:
        backup.select_sequence_set(duplicate)
    assert refused.value.code == "sequence_set_selection"


def test_a_sequence_set_entry_for_another_sequence_is_not_selected():
    entries = backup.parse_toc(
        REAL_TOC.replace("messages_id_seq", "other_id_seq")
    )
    with pytest.raises(backup.BackupError):
        backup.select_sequence_set(entries)


def test_a_toc_line_that_does_not_match_the_pinned_grammar_is_refused():
    with pytest.raises(backup.BackupError):
        backup.parse_toc("not a toc line\n")


# --- archive round trip ----------------------------------------------------------------------


def test_the_archive_members_are_exactly_the_frozen_three_in_order(tmp_path):
    dump = b"custom-format-dump-bytes"
    manifest = _manifest(snapshot={"bytes": len(dump), "sha256": backup.sha256_bytes(dump)})
    path = tmp_path / "point.tar"
    backup.write_archive(path, manifest=manifest, dump=dump)
    with tarfile.open(path) as archive:
        assert archive.getnames() == ["MANIFEST.json", "snapshot.dump.sha256", "snapshot.dump"]
        assert archive.extractfile("snapshot.dump").read() == dump
    point = backup.read_archive(path)
    assert point.manifest == manifest


def test_a_truncated_dump_is_refused(tmp_path):
    dump = b"custom-format-dump-bytes"
    manifest = _manifest(snapshot={"bytes": len(dump), "sha256": backup.sha256_bytes(dump)})
    path = tmp_path / "point.tar"
    backup.write_archive(path, manifest=manifest, dump=dump)
    # Rewrite the dump member with one changed byte, keeping the archive structurally valid.
    rewritten = tmp_path / "bad.tar"
    with tarfile.open(path) as source, tarfile.open(rewritten, "w") as target:
        for name in source.getnames():
            data = source.extractfile(name).read()
            if name == "snapshot.dump":
                data = data[:-1] + b"X"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
    with pytest.raises(backup.BackupError) as refused:
        backup.read_archive(rewritten)
    assert refused.value.code in ("archive_checksum",)


def test_a_v1_recovery_point_is_refused_not_converted(tmp_path):
    path = tmp_path / "v1.tar"
    manifest = {"format": backup.FORMAT_V1, "snapshot": {}}
    with tarfile.open(path, "w") as archive:
        for name, value in (
            (backup.MANIFEST_NAME, json.dumps(manifest).encode()),
            (backup.CHECKSUM_NAME, b"x"),
            (backup.DUMP_NAME, b"x"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    with pytest.raises(backup.BackupError) as refused:
        backup.read_archive(path)
    assert refused.value.code == "archive_v1"


def test_an_archive_with_the_wrong_member_order_is_refused(tmp_path):
    dump = b"dump"
    manifest = _manifest(snapshot={"bytes": len(dump), "sha256": backup.sha256_bytes(dump)})
    path = tmp_path / "wrong-order.tar"
    with tarfile.open(path, "w") as archive:
        for name, value in (
            (backup.DUMP_NAME, dump),
            (backup.MANIFEST_NAME, backup.canonical_json(manifest) + b"\n"),
            (backup.CHECKSUM_NAME, backup.checksum_line(backup.sha256_bytes(dump))),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    with pytest.raises(backup.BackupError) as refused:
        backup.read_archive(path)
    assert refused.value.code == "archive_members"


# --- digests and catalog facts ---------------------------------------------------------------


def test_every_digest_query_names_only_an_image_table():
    for name in pg_contract.expected_tables():
        sql = backup.digest_sql(name)
        assert f'"' + name + '"' in sql
        assert "row_to_json" in sql
    with pytest.raises(backup.BackupError):
        backup.digest_sql("users; DROP TABLE users")


def test_the_empty_table_digest_is_the_sha256_of_the_empty_string():
    # The SQL uses sha256(coalesce(string_agg(...), '')), so an empty table is the
    # deterministic empty-input digest, never a NULL that the manifest cannot carry.
    assert backup.sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_the_facts_parser_requires_every_fact_and_the_exact_table_set():
    facts = "\n".join(
        [
            "server_version_num|160015",
            "database|UTF8|C|C",
            "revision|0014_user_measure_overrides",
            "max_messages_id|",
            "sequence_catalog|public|messages_id_seq|hh_owner|public.messages_id_seq|1|bigint|1|1|9223372036854775807|1|1|f",
            *(
                f"table|{name}|0|" + backup.sha256_bytes(b"")
                for name in sorted(pg_contract.expected_tables())
            ),
        ]
    )
    parsed = backup.parse_facts(facts)
    assert parsed["revision"] == "0014_user_measure_overrides"
    assert parsed["max_messages_id"] is None
    assert sorted(parsed["tables"]) == sorted(pg_contract.expected_tables())
    with pytest.raises(backup.BackupError):
        backup.parse_facts(facts.replace("sequence_catalog|public|", "sequence_catalog|other|"))


# --- the object contract ---------------------------------------------------------------------


def test_the_checked_in_object_contract_is_the_image_revision():
    fixture = backup.load_object_contract_fixture(
        Path(__file__).parent / "fixtures" / "pg_object_contract.json"
    )
    assert fixture["revision"] == "0014_user_measure_overrides"
    assert fixture["format"] == backup.OBJECT_CONTRACT_FORMAT
    names = {row[2] for row in fixture["relations"] if row[0] == "r"}
    assert names == set(pg_contract.expected_tables())
    relations = {
        row[2] for row in fixture["relations"] if row[0] in ("r", "S", "i")
    }
    assert relations == set(pg_contract.expected_relation_names())
    assert {row[2] for row in fixture["relations"] if row[0] == "S"} == {
        "messages_id_seq"
    }


def test_a_different_constraint_rendering_is_normalized_to_the_same_contract():
    original = (
        "CHECK (((provider)::text = ANY ((ARRAY['telegram'::character varying, "
        "'email'::character varying])::text[])))"
    )
    restored = (
        "CHECK (((provider)::text = ANY (ARRAY[('telegram'::character varying)::text, "
        "('email'::character varying)::text])))"
    )
    assert backup._normalize_definition(original) == backup._normalize_definition(restored)


# --- F1: the permitted deparse equivalences are narrow, never a broad text strip ------------


@pytest.mark.parametrize(
    "left, right",
    [
        # 1. String-literal contents, including a `public.` prefix and whitespace, are data.
        (
            "CHECK ((provider) <> 'public.telegram')",
            "CHECK ((provider) <> 'telegram')",
        ),
        ("CHECK ((provider) <> ' email')", "CHECK ((provider) <> 'email')"),
        # 2. A default that is a different string is a different default.
        ("'public.text'::text", "'text'::text"),
        # 3. A cast on a non-literal is semantically significant: a uuid is not its text.
        ("md5((id)::text)", "md5(id)"),
        ("lower(name::text)", "lower(name)"),
        ("(uuid_col)::text", "uuid_col"),
        # 4. Grouping and precedence are preserved.
        ("((a OR b) AND c)", "(a OR (b AND c))"),
        ("((a + b) * c)", "(a + (b * c))"),
        # 5. A typmod cast changes the value (`varchar(3)` truncates) and is not folded.
        ("'abcdef'::character varying(3)", "'abcdef'"),
        # 6. A non-text cast is not folded even on a literal.
        ("'1'::uuid", "'1'::text"),
    ],
)
def test_only_value_preserving_deparse_variations_are_normalized(left, right):
    assert backup._normalize_definition(left) != backup._normalize_definition(right)


def test_whitespace_between_tokens_is_not_significant():
    assert backup._normalize_definition("md5 (id)") == backup._normalize_definition(
        "md5(id)"
    )
    assert backup._normalize_definition(
        "CHECK ( length(subject) >= 1 )"
    ) == backup._normalize_definition("CHECK(length(subject)>=1)")


def test_a_uuid_index_expression_is_not_folded_into_a_text_index():
    original = "CREATE UNIQUE INDEX ix ON public.users USING btree (md5((id)::text))"
    tampered = "CREATE UNIQUE INDEX ix ON public.users USING btree (md5(id))"
    assert backup._normalize_definition(original) != backup._normalize_definition(tampered)


def _real_fixture():
    return backup.load_object_contract_fixture(
        Path(__file__).parent / "fixtures" / "pg_object_contract.json"
    )


# The reproduced Codex collision: a text-family array cast is *not* value-preserving
# unless the constructor holds only string literals. These two expressions mean
# different things and must stay distinct through the object contract.
F1_ARRAY_CAST = "CHECK (((ARRAY[id])::text[] < (ARRAY[(id + 1)])::text[]))"
F1_ARRAY_REFERENCE = "CHECK ((ARRAY[id] < ARRAY[(id + 1)]))"


def test_an_array_cast_is_not_folded_off_a_non_literal_constructor():
    assert backup._normalize_definition(
        F1_ARRAY_CAST
    ) != backup._normalize_definition(F1_ARRAY_REFERENCE)


@pytest.mark.parametrize(
    "left, right",
    [
        # A cast on a constructor of a column reference is not a string literal.
        ("CHECK (x = ANY ((ARRAY[id])::text[]))", "CHECK (x = ANY (ARRAY[id]))"),
        # A numeric or compound element is not a string literal either.
        ("CHECK (x = ANY ((ARRAY[1])::text[]))", "CHECK (x = ANY (ARRAY[1]))"),
        ("CHECK (x = ANY ((ARRAY[id + 1])::text[]))", "CHECK (x = ANY (ARRAY[id + 1]))"),
        ("CHECK (x = ANY ((ARRAY['a' || 'b'])::text[]))", "CHECK (x = ANY (ARRAY['a' || 'b']))"),
        # A nested constructor is not a list of string literals.
        ("CHECK (x = ANY ((ARRAY[ARRAY['a']])::text[]))", "CHECK (x = ANY (ARRAY[ARRAY['a']]))"),
        # A mixed list is not a list of string literals.
        ("CHECK (x = ANY ((ARRAY['a', id])::text[]))", "CHECK (x = ANY (ARRAY['a', id]))"),
    ],
)
def test_a_non_literal_array_constructor_keeps_its_cast(left, right):
    assert backup._normalize_definition(left) != backup._normalize_definition(right)


def test_the_reproduced_array_collision_fails_the_object_contract_not_just_a_tokenizer():
    fixture = _real_fixture()
    original = json.loads(json.dumps(fixture))
    tampered = json.loads(json.dumps(fixture))
    original["constraints"][0][4] = F1_ARRAY_CAST
    tampered["constraints"][0][4] = F1_ARRAY_REFERENCE
    assert "object_contract:constraints" in backup.object_contract_failures(
        tampered, original
    )


def test_a_check_that_changes_a_literal_collides_with_nothing():
    fixture = _real_fixture()
    changed = json.loads(json.dumps(fixture))
    entry = next(
        row
        for row in changed["constraints"]
        if row[1] == "ck_allowlist_entries_provider_valid"
    )
    entry[4] = entry[4].replace("'telegram'", "'public.telegram'")
    assert "object_contract:constraints" in backup.object_contract_failures(
        changed, fixture
    )


def test_a_default_that_changes_a_literal_collides_with_nothing():
    fixture = _real_fixture()
    changed = json.loads(json.dumps(fixture))
    column = next(
        row
        for row in changed["columns"]
        if row[0] == "favorite_recipes" and row[2] == "kind"
    )
    assert column[5] == "'text'"
    column[5] = "'public.text'"
    assert "object_contract:columns" in backup.object_contract_failures(changed, fixture)


def test_an_index_expression_cast_to_text_collides_with_nothing():
    fixture = _real_fixture()
    changed = json.loads(json.dumps(fixture))
    entry = next(
        row for row in changed["indexes"] if row[1] == "ix_allowlist_entries_user_id"
    )
    entry[5] = entry[5].replace("btree(user_id)", "btree((user_id)::text)")
    assert "object_contract:indexes" in backup.object_contract_failures(changed, fixture)


def test_an_object_contract_change_is_reported():
    fixture = backup.load_object_contract_fixture(
        Path(__file__).parent / "fixtures" / "pg_object_contract.json"
    )
    changed = json.loads(json.dumps(fixture))
    changed["constraints"][0][4] = "CHECK (false)"
    assert "object_contract:constraints" in backup.object_contract_failures(
        changed, fixture
    )


def _toc_fixture():
    return {
        "format": backup.OBJECT_CONTRACT_FORMAT,
        "revision": "0014_user_measure_overrides",
        "relations": [
            ["r", "public", "users", "hh_owner"],
            ["S", "public", "messages_id_seq", "hh_owner"],
            ["i", "public", "pk_users", "hh_owner"],
            ["i", "public", "ix_users_name", "hh_owner"],
        ],
        "columns": [],
        "constraints": [
            ["users", "pk_users", "p", True, "PRIMARY KEY (id)"],
            ["users", "fk_users_parent", "f", True, "FOREIGN KEY (parent) REFERENCES users(id)"],
        ],
        "indexes": [],
        "sequence": [],
    }


def test_the_toc_names_the_object_contract():
    toc = "\n".join(
        [
            "1; 1259 1 TABLE public users hh_owner",
            "2; 1259 2 SEQUENCE public messages_id_seq hh_owner",
            "3; 1259 3 INDEX public ix_users_name hh_owner",
            "4; 2606 4 CONSTRAINT public users pk_users hh_owner",
            "5; 2606 5 FK CONSTRAINT public users fk_users_parent hh_owner",
        ]
    )
    entries = backup.parse_toc(toc)
    assert backup.toc_contract_failures(entries, _toc_fixture()) == ()
    # A missing table, a missing foreign key, or an extra index fails the level-1 check.
    assert "toc_contract:tables" in backup.toc_contract_failures(
        tuple(e for e in entries if e.object_class != "TABLE"), _toc_fixture()
    )
    assert "toc_contract:constraints" in backup.toc_contract_failures(
        tuple(e for e in entries if e.object_class != "FK CONSTRAINT"), _toc_fixture()
    )
    extra = entries + (
        backup.TocEntry(6, 1259, 6, "INDEX", "public", "ix_unexpected", "hh_owner"),
    )
    assert "toc_contract:indexes" in backup.toc_contract_failures(extra, _toc_fixture())

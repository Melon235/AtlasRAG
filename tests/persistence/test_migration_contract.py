"""Service-free checks of the actual SQL emitted by the append-only migration."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
REVISION = "20260928_0001"
MIGRATION = ROOT / "migrations/versions/20260928_0001_stage2_persistence.py"


def _offline(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "ATLASRAG_POSTGRES_DSN": (
                "postgresql://offline:p%25ss@127.0.0.1:1/never_connect"
            ),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.fixture(scope="module")
def sql() -> str:
    result = _offline("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr + result.stdout
    return re.sub(r"\s+", " ", result.stdout).strip()


def test_offline_cli_accepts_plain_postgresql_url_without_database() -> None:
    result = _offline("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr + result.stdout


def _table(sql: str, name: str) -> str:
    match = re.search(rf"CREATE TABLE {name} \((.*?)\);", sql)
    assert match, f"missing table {name}"
    return match[1]


def test_one_linear_append_only_revision() -> None:
    assert (ROOT / "alembic.ini").is_file(), "migration entrypoint is missing"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    revisions = list(scripts.walk_revisions())
    assert scripts.get_heads() == [REVISION]
    assert len(revisions) == 1
    assert revisions[0].down_revision is None


def test_only_canonical_business_tables_and_alembic_version(sql: str) -> None:
    assert set(re.findall(r"CREATE TABLE (\w+)", sql)) == {
        "documents",
        "document_elements",
        "chunks",
        "runtime_metadata",
        "alembic_version",
    }
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{REVISION}')" in sql
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE)\b", sql, re.IGNORECASE)


@pytest.mark.parametrize(
    "column",
    ["document_id", "file_name", "relative_source_path", "observed_content_hash"],
)
def test_required_document_text_columns(sql: str, column: str) -> None:
    assert re.search(
        rf"\b{column} TEXT (?:NOT NULL|PRIMARY KEY)", _table(sql, "documents")
    )


def test_document_identity_and_enums(sql: str) -> None:
    table = _table(sql, "documents")
    assert "CONSTRAINT pk_documents PRIMARY KEY (document_id)" in table
    assert (
        "CONSTRAINT uq_documents_relative_source_path UNIQUE (relative_source_path)"
        in table
    )
    expected = {
        "source_type": {"PDF", "DOCX", "XLSX", "MD", "TXT"},
        "ingestion_status": {
            "RECEIVED",
            "PARSING",
            "PARSED",
            "CHUNKING",
            "CHUNKED",
            "INDEXING",
            "READY",
            "FAILED",
        },
        "failed_stage": {
            "PARSE",
            "CHUNK",
            "REPRESENTATION",
            "EMBEDDING",
            "INDEX_WRITE",
            "INDEX_VERIFY",
        },
    }
    for column, values in expected.items():
        match = re.search(rf"{column} IN \(([^)]+)\)", table)
        assert match
        assert set(re.findall(r"'([^']+)'", match[1])) == values
    assert "(ingestion_status = 'FAILED') = (failed_stage IS NOT NULL)" in table


@pytest.mark.parametrize("prefix", ["current", "building"])
def test_revision_triples_are_nullable_and_all_or_none(sql: str, prefix: str) -> None:
    table = _table(sql, "documents")
    columns = [
        f"{prefix}_{suffix}"
        for suffix in ("revision_id", "content_hash", "pipeline_fingerprint")
    ]
    for column in columns:
        assert re.search(rf"\b{column} TEXT,", table)
    assert " AND ".join(f"{column} IS NULL" for column in columns) in table
    assert " AND ".join(f"{column} IS NOT NULL" for column in columns) in table
    assert f"CONSTRAINT ck_documents_{prefix}_revision_complete CHECK" in table


@pytest.mark.parametrize(
    "table,column,kind,nullable",
    [
        ("documents", "parse_metadata", "object", False),
        ("document_elements", "section_path", "array", False),
        ("document_elements", "source_anchor", "object", True),
        ("document_elements", "structured_content", "object", False),
        ("document_elements", "metadata", "object", False),
        ("chunks", "section_path", "array", False),
        ("chunks", "source_anchor", "object", True),
        ("chunks", "metadata", "object", False),
        ("chunks", "strategy_metadata", "object", False),
    ],
)
def test_json_container_checks(
    sql: str, table: str, column: str, kind: str, nullable: bool
) -> None:
    ddl = _table(sql, table)
    assert f"{column} JSONB" in ddl
    assert f"jsonb_typeof({column}) = '{kind}'" in ddl
    assert bool(re.search(rf"\b{column} JSONB NOT NULL", ddl)) is not nullable
    if not nullable:
        default = "[]" if kind == "array" else "{}"
        assert f"{column} JSONB NOT NULL DEFAULT '{default}'::jsonb" in ddl


@pytest.mark.parametrize(
    "table", ["documents", "document_elements", "chunks", "runtime_metadata"]
)
def test_aware_required_timestamps(sql: str, table: str) -> None:
    for column in ["updated_at"] if table == "runtime_metadata" else ["created_at"]:
        assert f"{column} TIMESTAMPTZ NOT NULL" in _table(sql, table)
    if table == "documents":
        assert "updated_at TIMESTAMPTZ NOT NULL" in _table(sql, table)


def test_elements_identity_order_and_revision(sql: str) -> None:
    table = _table(sql, "document_elements")
    for column in [
        "element_id",
        "document_id",
        "revision_id",
        "element_type",
        "content",
    ]:
        assert f"{column} TEXT NOT NULL" in table
    assert "CONSTRAINT pk_document_elements PRIMARY KEY (element_id)" in table
    assert "FOREIGN KEY (document_id) REFERENCES documents (document_id)" in table
    assert "order_index INTEGER NOT NULL" in table
    assert "CHECK (order_index >= 0)" in table
    assert "CHECK (element_type ~ '[^[:space:]]')" in table


def test_chunks_lineage_constraints(sql: str) -> None:
    table = _table(sql, "chunks")
    for column in ["chunk_id", "document_id", "revision_id", "chunk_type", "content"]:
        assert f"{column} TEXT NOT NULL" in table
    assert "CONSTRAINT pk_chunks PRIMARY KEY (chunk_id)" in table
    assert "FOREIGN KEY (document_id) REFERENCES documents (document_id)" in table
    assert "UNIQUE (chunk_id, document_id, revision_id) NOT DEFERRABLE" in table
    assert (
        "FOREIGN KEY (parent_id, document_id, revision_id) REFERENCES chunks "
        "(chunk_id, document_id, revision_id) DEFERRABLE INITIALLY DEFERRED"
    ) in table
    assert "chunk_type IN ('TEXT_PARENT', 'TEXT_CHILD', 'TABLE')" in table
    assert "chunk_type <> 'TEXT_CHILD' OR parent_id IS NOT NULL" in table
    assert "chunk_type <> 'TEXT_PARENT' OR parent_id IS NULL" in table
    assert "parent_id IS NULL OR parent_id <> chunk_id" in table
    assert "sheet_name IS NULL OR sheet_name ~ '[^[:space:]]'" in table


def test_parent_type_trigger_checks_final_state_and_parent_updates(sql: str) -> None:
    assert "CREATE FUNCTION atlasrag_check_chunk_parent_type() RETURNS trigger" in sql
    assert (
        "CREATE CONSTRAINT TRIGGER ct_chunks_parent_type AFTER INSERT OR UPDATE ON chunks "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION atlasrag_check_chunk_parent_type()"
    ) in sql
    # Query stored final rows, not the potentially stale NEW parent/type values.
    assert "child.chunk_id = NEW.chunk_id OR child.parent_id = NEW.chunk_id" in sql
    assert "child.chunk_type = 'TEXT_CHILD'" in sql
    assert "parent.document_id = child.document_id" in sql
    assert "parent.revision_id = child.revision_id" in sql
    assert "parent.chunk_type = 'TEXT_PARENT'" in sql
    assert "FOR SHARE" in sql
    assert "ERRCODE = '23514'" in sql


def test_only_required_indexes(sql: str) -> None:
    indexes = re.findall(r"CREATE INDEX (\w+) ON (\w+) \(([^)]+)\)", sql)
    assert set(indexes) == {
        ("ix_documents_current_revision_id", "documents", "current_revision_id"),
        ("ix_documents_building_revision_id", "documents", "building_revision_id"),
        (
            "ix_document_elements_document_revision",
            "document_elements",
            "document_id, revision_id",
        ),
        ("ix_chunks_document_revision", "chunks", "document_id, revision_id"),
        ("ix_chunks_parent_id", "chunks", "parent_id"),
        ("ix_chunks_chunk_type", "chunks", "chunk_type"),
    }


def test_runtime_singleton_seed(sql: str) -> None:
    table = _table(sql, "runtime_metadata")
    assert "metadata_key TEXT NOT NULL DEFAULT 'runtime'" in table
    assert "PRIMARY KEY (metadata_key)" in table
    assert "CHECK (metadata_key = 'runtime')" in table
    assert "query_cache_invalidation_required BOOLEAN NOT NULL DEFAULT false" in table
    assert sql.count("INSERT INTO runtime_metadata") == 1
    assert "VALUES ('runtime', false, CURRENT_TIMESTAMP)" in sql


def test_downgrade_explicitly_refuses_destructive_changes() -> None:
    result = _offline("downgrade", f"{REVISION}:base", "--sql")
    assert result.returncode != 0
    assert "destructive downgrade is unsupported" in result.stderr.lower()
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE)\b", result.stdout)


def test_environment_has_no_application_imports_or_autogeneration() -> None:
    path = ROOT / "migrations/env.py"
    assert path.is_file(), "migration environment is missing"
    source = path.read_text()
    assert "target_metadata = None" in source
    tree = ast.parse(source)
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not any(name.startswith("atlasrag") for name in imports)
    assert "ATLASRAG_POSTGRES_DSN" in source
    assert "create_all" not in source
    assert "drop_all" not in source


def test_offline_generation_does_not_construct_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    import sqlalchemy
    from alembic import command

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline migration tried to construct a database engine")

    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    monkeypatch.setattr(sqlalchemy, "engine_from_config", forbidden)
    monkeypatch.setenv(
        "ATLASRAG_POSTGRES_DSN", "postgresql://offline@127.0.0.1:1/offline"
    )
    config = Config(str(ROOT / "alembic.ini"), output_buffer=StringIO())
    assert (ROOT / "alembic.ini").is_file(), "migration entrypoint is missing"
    command.upgrade(config, "head", sql=True)

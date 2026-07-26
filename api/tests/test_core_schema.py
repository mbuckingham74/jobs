"""Migration-focused tests for the Phase 1 core schema revision.

These tests make regressions in the revision identifiers and the required
upgrade/downgrade definitions visible without touching a database. They load
the single domain revision module directly from ``api/migrations/versions``
so the test does not depend on the Alembic runtime or on a live database.
Catalog-level enforcement of the constraints is verified separately against a
disposable PostgreSQL 16 + pgvector instance by
``api/tests/core_schema_enforcement.sql`` (see the task validation commands).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Callable
from pathlib import Path

import pytest

REVISION_ID = "0001_core_schema"
REVISION_SLUG = "core_schema"

PHASE_1_TABLES = [
    "company",
    "source_endpoint",
    "pipeline_run",
    "source_fetch",
    "posting",
    "posting_version",
    "resume_version",
    "candidate_profile_version",
    "company_research",
    "score",
    "daily_digest",
    "digest_item",
]

# The migration environment is not a Python package; ``migrations`` has no
# ``__init__.py``. Resolve the single domain revision by path and load it as an
# isolated module so the test does not require Alembic or a database session.
_API_DIR = Path(__file__).resolve().parent.parent
_VERSIONS_DIR = _API_DIR / "migrations" / "versions"


def _load_revision_module() -> tuple[object, Path]:
    py_files = sorted(p for p in _VERSIONS_DIR.glob("*.py") if p.name != "__init__.py")
    assert len(py_files) == 1, (
        "expected exactly one domain revision under migrations/versions, found "
        f"{[p.name for p in py_files]}"
    )
    path = py_files[0]
    spec = importlib.util.spec_from_file_location("jobs_core_schema_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


@pytest.fixture(scope="module")
def revision_module() -> object:
    module, _ = _load_revision_module()
    return module


@pytest.fixture(scope="module")
def revision_path() -> Path:
    _, path = _load_revision_module()
    return path


@pytest.fixture(scope="module")
def revision_source(revision_path: Path) -> str:
    return revision_path.read_text()


def _created_tables(source: str) -> list[str]:
    return re.findall(r'op\.create_table\(\s*"([a-z_]+)"', source)


def _dropped_tables(source: str) -> list[str]:
    return re.findall(r'op\.drop_table\(\s*"([a-z_]+)"', source)


def test_exactly_one_revision_file_exists(revision_path: Path) -> None:
    # _load_revision_module already asserts uniqueness, so reaching here is the
    # pass condition. Verify the file is not a placeholder.
    assert revision_path.suffix == ".py"


def test_revision_identifier_is_stable(revision_module: object) -> None:
    assert revision_module.revision == REVISION_ID  # type: ignore[attr-defined]


def test_revision_is_first_domain_revision(revision_module: object) -> None:
    assert revision_module.down_revision is None  # type: ignore[attr-defined]
    assert revision_module.branch_labels is None  # type: ignore[attr-defined]
    assert revision_module.depends_on is None  # type: ignore[attr-defined]


def test_file_name_follows_template(revision_path: Path) -> None:
    # alembic.ini configures: %Y%m%d_%H%M_<rev>_<slug>.py. The revision id and
    # slug may both contain underscores, so the parts cannot be split
    # unambiguously; assert the fixed prefix and the trailing slug instead.
    stem = revision_path.stem
    prefix, rest = stem.split("_", 1)
    assert prefix.isdigit() and len(prefix) == 8, stem  # YYYYMMDD
    hour_minute, tail = rest.split("_", 1)
    assert len(hour_minute) == 4 and hour_minute.isdigit(), stem  # HHMM
    assert tail.endswith(REVISION_ID) or tail.endswith(REVISION_SLUG), stem
    assert REVISION_ID in tail and REVISION_SLUG in tail, stem


def test_upgrade_and_downgrade_are_defined(revision_module: object) -> None:
    assert isinstance(revision_module.upgrade, Callable)  # type: ignore[attr-defined]
    assert isinstance(revision_module.downgrade, Callable)  # type: ignore[attr-defined]


def test_revision_uses_pgvector_vector_type(revision_module: object) -> None:
    # The posting_version.embedding column is declared vector(1024) via the
    # pgvector SQLAlchemy type. Importing the module confirms the dependency is
    # available and that the type is referenced.
    from pgvector.sqlalchemy import Vector

    assert hasattr(revision_module, "Vector")
    assert revision_module.Vector is Vector  # type: ignore[attr-defined]


def test_extension_lifecycle_and_hnsw_operator_class(revision_source: str) -> None:
    assert "create extension if not exists vector" in revision_source
    assert "drop extension if exists vector" in revision_source
    assert "vector_cosine_ops" in revision_source
    assert 'postgresql_using="hnsw"' in revision_source


def test_deep_score_constraints_are_named_and_fail_closed(revision_source: str) -> None:
    assert "deep_score_version_inputs" in revision_source
    assert "deep_score_location_status" in revision_source
    # The location-status check must wrap its JSON membership test in
    # coalesce(..., false) so a missing or JSON-null status fails closed.
    assert "coalesce(" in revision_source


def test_upgrade_creates_exactly_the_phase_1_tables(revision_source: str) -> None:
    assert _created_tables(revision_source) == PHASE_1_TABLES


def test_downgrade_drops_every_phase_1_table(revision_source: str) -> None:
    dropped = _dropped_tables(revision_source)
    assert dropped == list(reversed(PHASE_1_TABLES))


def test_no_out_of_scope_tables_are_created(revision_source: str) -> None:
    out_of_scope = ["discovery_event", "application", "application_event"]
    created = set(_created_tables(revision_source))
    assert created.isdisjoint(out_of_scope)
    dropped = set(_dropped_tables(revision_source))
    assert dropped.isdisjoint(out_of_scope)


# Every foreign key in the Phase 1 schema must be declared with an explicit,
# stable ``name=`` so later migrations and schema tests can refer to it. The
# task forbids inline ``sa.ForeignKey`` columns precisely because SQLAlchemy
# would synthesise a PostgreSQL-style name at create time but leave the
# migration source silent about it. These counts cover all 20 reference
# columns across the 12 tables, including the resume_version self-reference.
EXPECTED_NAMED_FOREIGN_KEYS = 20


def _collect_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _attr_chain(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_every_foreign_key_has_an_explicit_stable_name(revision_path: Path) -> None:
    tree = ast.parse(revision_path.read_text())
    fkc_calls = [c for c in _collect_calls(tree) if _attr_chain(c) == "sa.ForeignKeyConstraint"]
    assert fkc_calls, "no sa.ForeignKeyConstraint calls found; FKs must be named"

    unnamed: list[str] = []
    for call in fkc_calls:
        keywords = {kw.arg for kw in call.keywords}
        if "name" not in keywords:
            unnamed.append(ast.unparse(call))
    assert (
        not unnamed
    ), f"every ForeignKeyConstraint must pass an explicit name=; unnamed: {unnamed}"
    assert len(fkc_calls) == EXPECTED_NAMED_FOREIGN_KEYS, (
        f"expected {EXPECTED_NAMED_FOREIGN_KEYS} named foreign keys, found " f"{len(fkc_calls)}"
    )


def test_no_inline_unnamed_sa_foreign_key_columns(revision_path: Path) -> None:
    # An inline ``sa.ForeignKey`` inside a ``sa.Column`` would let SQLAlchemy
    # synthesise the constraint name at create time and leave the migration
    # source silent about it. The task requires every FK to be declared as a
    # named ``sa.ForeignKeyConstraint`` instead.
    tree = ast.parse(revision_path.read_text())
    inline_fk_calls = [
        ast.unparse(c) for c in _collect_calls(tree) if _attr_chain(c) == "sa.ForeignKey"
    ]
    assert not inline_fk_calls, (
        f"inline sa.ForeignKey columns are not allowed; use a named "
        f"sa.ForeignKeyConstraint instead. Found: {inline_fk_calls}"
    )

"""Migration-focused tests for the resume_source_state revision.

Parallel in structure to ``api/tests/test_core_schema.py``. Loads the new
revision module directly, asserts its identifiers, the exact one-new-table
upgrade/downgrade behavior (leaving ``0001_core_schema``'s tables untouched),
the named constraints and indexes, and that no inline unnamed ``sa.ForeignKey``
column is used.

It additionally validates that the Alembic graph remains a single linear chain
through the later ``0004_hard_filter_evaluation`` revision.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Callable
from pathlib import Path

import pytest

REVISION_ID = "0002_resume_source_state"
REVISION_SLUG = "resume_source_state"
DOWN_REVISION = "0001_core_schema"
EXPECTED_FILENAME = "20260725_2201_0002_resume_source_state.py"

EXPECTED_NEW_TABLE = "resume_source_state"
EXPECTED_NAMED_CONSTRAINTS = {
    "resume_source_state_pkey",
    "resume_source_state_variant_check",
    "resume_source_state_source_kind_check",
    "resume_source_state_variant_source_kind_source_url_key",
    "resume_source_state_current_resume_version_id_fkey",
}
EXPECTED_NAMED_INDEXES = {
    # Named PK and unique constraints supply backing indexes with these names.
    "resume_source_state_pkey",
    "resume_source_state_variant_source_kind_source_url_key",
}

PHASE_1_TABLES_0001 = [
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

_API_DIR = Path(__file__).resolve().parent.parent
_VERSIONS_DIR = _API_DIR / "migrations" / "versions"

PERMITTED_REVISIONS = {
    "0001_core_schema",
    "0002_resume_source_state",
    "0003_posting_current_version",
    "0004_hard_filter_evaluation",
}


def _load_module(path: Path, label: str) -> object:
    spec = importlib.util.spec_from_file_location(label, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_by_id(revision_id: str) -> object:
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path, f"jobs_revision_{revision_id}")
        if getattr(module, "revision", None) == revision_id:
            return module
    raise AssertionError(f"no migration module with revision {revision_id!r} found")


def _load_path_by_id(revision_id: str) -> Path:
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path, f"jobs_revision_path_{revision_id}")
        if getattr(module, "revision", None) == revision_id:
            return path
    raise AssertionError(f"no migration module with revision {revision_id!r} found")


def _all_revision_modules() -> list[tuple[object, Path]]:
    out: list[tuple[object, Path]] = []
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path, f"jobs_revision_all_{path.stem}")
        if getattr(module, "revision", None) is None:
            continue
        out.append((module, path))
    return out


@pytest.fixture(scope="module")
def revision_module() -> object:
    return _load_by_id(REVISION_ID)


@pytest.fixture(scope="module")
def revision_path() -> Path:
    return _load_path_by_id(REVISION_ID)


@pytest.fixture(scope="module")
def revision_source(revision_path: Path) -> str:
    return revision_path.read_text()


def test_revision_identifier_is_stable(revision_module: object) -> None:
    assert revision_module.revision == REVISION_ID  # type: ignore[attr-defined]


def test_down_revision_points_at_core_schema(revision_module: object) -> None:
    assert revision_module.down_revision == DOWN_REVISION  # type: ignore[attr-defined]
    assert revision_module.branch_labels is None  # type: ignore[attr-defined]
    assert revision_module.depends_on is None  # type: ignore[attr-defined]


def test_filename_follows_template_and_is_stable(revision_path: Path) -> None:
    stem = revision_path.stem
    prefix, rest = stem.split("_", 1)
    assert prefix.isdigit() and len(prefix) == 8, stem
    hour_minute, tail = rest.split("_", 1)
    assert len(hour_minute) == 4 and hour_minute.isdigit(), stem
    assert tail.endswith(REVISION_ID) or tail.endswith(REVISION_SLUG), stem
    assert REVISION_ID in tail and REVISION_SLUG in tail, stem
    assert revision_path.name == EXPECTED_FILENAME, revision_path.name


def test_upgrade_creates_exactly_resume_source_state(revision_source: str) -> None:
    created = re.findall(r'op\.create_table\(\s*"([a-z_]+)"', revision_source)
    assert created == [EXPECTED_NEW_TABLE], created


def test_downgrade_drops_exactly_resume_source_state(revision_source: str) -> None:
    dropped = re.findall(r'op\.drop_table\(\s*"([a-z_]+)"', revision_source)
    assert dropped == [EXPECTED_NEW_TABLE], dropped


def test_upgrade_and_downgrade_are_defined(revision_module: object) -> None:
    assert isinstance(revision_module.upgrade, Callable)  # type: ignore[attr-defined]
    assert isinstance(revision_module.downgrade, Callable)  # type: ignore[attr-defined]


def test_no_out_of_scope_tables_are_created(revision_source: str) -> None:
    # The new table is structural only; no 0001 table is re-created or touched.
    assert EXPECTED_NEW_TABLE not in PHASE_1_TABLES_0001
    created = set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', revision_source))
    out_of_scope = [
        "discovery_event",
        "application",
        "application_event",
        *PHASE_1_TABLES_0001,
    ]
    assert created.isdisjoint(out_of_scope), created


def test_no_out_of_scope_tables_are_dropped(revision_source: str) -> None:
    dropped = set(re.findall(r'op\.drop_table\(\s*"([a-z_]+)"', revision_source))
    out_of_scope = [
        "discovery_event",
        "application",
        "application_event",
        *PHASE_1_TABLES_0001,
    ]
    assert dropped.isdisjoint(out_of_scope), dropped


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


def _collect_named_kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def test_every_constraint_and_index_has_an_explicit_stable_name(
    revision_path: Path,
) -> None:
    tree = ast.parse(revision_path.read_text())
    calls = _collect_calls(tree)

    pk_calls = [c for c in calls if _attr_chain(c) == "sa.PrimaryKeyConstraint"]
    check_calls = [c for c in calls if _attr_chain(c) == "sa.CheckConstraint"]
    unique_calls = [c for c in calls if _attr_chain(c) == "sa.UniqueConstraint"]
    fk_calls = [c for c in calls if _attr_chain(c) == "sa.ForeignKeyConstraint"]
    create_index_calls = [c for c in calls if _attr_chain(c) == "op.create_index"]

    for label, group in (
        ("PrimaryKeyConstraint", pk_calls),
        ("CheckConstraint", check_calls),
        ("UniqueConstraint", unique_calls),
        ("ForeignKeyConstraint", fk_calls),
    ):
        for call in group:
            kws = _collect_named_kwargs(call)
            assert "name" in kws, (
                f"every {label} must pass an explicit name=; unnamed: " f"{ast.unparse(call)}"
            )

    found_names = set()
    for call in [*pk_calls, *check_calls, *unique_calls, *fk_calls]:
        name_expr = _collect_named_kwargs(call).get("name")
        assert isinstance(
            name_expr, ast.Constant
        ), f"constraint name must be a literal: {ast.unparse(call)}"
        found_names.add(str(name_expr.value))

    assert EXPECTED_NAMED_CONSTRAINTS.issubset(
        found_names
    ), f"missing named constraints: {EXPECTED_NAMED_CONSTRAINTS - found_names}"
    # Task 003 forbids a second or speculative index on resume_source_state.
    assert not create_index_calls, (
        f"no explicit op.create_index expected on resume_source_state; "
        f"named PK and unique constraints supply the backing indexes; found: "
        f"{[ast.unparse(c) for c in create_index_calls]}"
    )


def test_no_inline_unnamed_sa_foreign_key_columns(revision_path: Path) -> None:
    tree = ast.parse(revision_path.read_text())
    inline_fk_calls = [
        ast.unparse(c) for c in _collect_calls(tree) if _attr_chain(c) == "sa.ForeignKey"
    ]
    assert not inline_fk_calls, (
        f"inline sa.ForeignKey columns are not allowed; use a named "
        f"sa.ForeignKeyConstraint instead. Found: {inline_fk_calls}"
    )


def test_foreign_key_uses_on_delete_restrict(revision_path: Path) -> None:
    tree = ast.parse(revision_path.read_text())
    fk_calls = [c for c in _collect_calls(tree) if _attr_chain(c) == "sa.ForeignKeyConstraint"]
    assert len(fk_calls) == 1, "exactly one ForeignKeyConstraint expected"
    kws = _collect_named_kwargs(fk_calls[0])
    ondelete = kws.get("ondelete")
    assert (
        isinstance(ondelete, ast.Constant) and ondelete.value == "RESTRICT"
    ), f"foreign key must use ondelete='RESTRICT'; got {ast.unparse(fk_calls[0])}"
    name = kws.get("name")
    assert isinstance(name, ast.Constant) and str(name.value) == (
        "resume_source_state_current_resume_version_id_fkey"
    )


def test_identity_column_is_by_default(revision_path: Path) -> None:
    # The spec demands ``bigint generated by default as identity`` for the
    # ``id`` column; this is emitted by ``sa.Identity(always=False)`` rather
    # than an inline ``sa.BigInteger`` primary-key serial.
    tree = ast.parse(revision_path.read_text())
    identity_calls = [c for c in _collect_calls(tree) if _attr_chain(c) == "sa.Identity"]
    assert len(identity_calls) == 1, "exactly one sa.Identity expected (id column)"
    kws = _collect_named_kwargs(identity_calls[0])
    always = kws.get("always")
    assert (
        isinstance(always, ast.Constant) and always.value is False
    ), f"id column identity must be always=False; got {ast.unparse(identity_calls[0])}"


# ------------------------------------------------------------------
# Multi-revision chain invariants.
# ------------------------------------------------------------------


def test_exactly_four_revisions_are_permitted() -> None:
    modules = _all_revision_modules()
    revisions = {m.revision for m, _ in modules}
    assert (
        revisions == PERMITTED_REVISIONS
    ), f"expected exactly {PERMITTED_REVISIONS}; found {revisions}"
    assert len(modules) == 4, f"expected exactly four revisions; found {len(modules)}"


def test_single_linear_chain_with_one_head() -> None:
    modules = _all_revision_modules()
    by_id: dict[str, object] = {m.revision: m for m, _ in modules}

    roots = [rid for rid, m in by_id.items() if m.down_revision is None]
    heads = [rid for rid in by_id if rid not in {m.down_revision for m in by_id.values()}]

    assert roots == ["0001_core_schema"], roots
    assert heads == ["0004_hard_filter_evaluation"], heads
    assert by_id[REVISION_ID].down_revision == DOWN_REVISION  # type: ignore[attr-defined]
    assert by_id["0003_posting_current_version"].down_revision == REVISION_ID  # type: ignore[attr-defined]
    assert by_id["0004_hard_filter_evaluation"].down_revision == "0003_posting_current_version"  # type: ignore[attr-defined]


def test_0001_core_schema_chain_invariants_preserved() -> None:
    # The original first revision still passes its own chain invariants.
    module = _load_by_id("0001_core_schema")
    assert module.revision == "0001_core_schema"  # type: ignore[attr-defined]
    assert module.down_revision is None  # type: ignore[attr-defined]
    assert module.branch_labels is None  # type: ignore[attr-defined]
    assert module.depends_on is None  # type: ignore[attr-defined]


def test_0001_core_schema_migration_file_is_unchanged() -> None:
    # The 0001 migration file must preserve its historical filename; renaming
    # it would break automated upgrade scripts and the validation commands.
    path = _load_path_by_id("0001_core_schema")
    assert path.name == "20260725_2200_0001_core_schema.py", path.name

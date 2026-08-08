"""Static invariants for the Task 008 Alembic revision.

Loads the migration source directly (never the live schema) and verifies the
revision identifiers, the single created table, every named constraint, and —
via AST inspection of the source — the exact SQL expression of every check
constraint and the exact column order of the sole lookup index. Expected
values are defined independently in this module; nothing is imported from the
migration module.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REVISION = "0004_hard_filter_evaluation"
EXPECTED_FILENAME = "20260801_1200_0004_hard_filter_evaluation.py"
ROOT = Path(__file__).resolve().parent.parent

LOOKUP_INDEX_NAME = "hard_filter_evaluation_current_lookup_idx"

# Independent contract for every check constraint in upgrade(): the SQL
# expression must be byte-for-byte the string below for its stable name.
EXPECTED_CHECK_EXPRESSIONS: dict[str, str] = {
    "hard_filter_evaluation_positive_ids_check": (
        "posting_id > 0 and posting_version_id > 0 and id > 0"
    ),
    "hard_filter_evaluation_policy_version_check": (
        "policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'"
    ),
    "hard_filter_evaluation_policy_manifest_hash_check": (
        "policy_manifest_hash ~ '^[0-9a-f]{64}$'"
    ),
    "hard_filter_evaluation_mutable_state_hash_check": "mutable_state_hash ~ '^[0-9a-f]{64}$'",
    "hard_filter_evaluation_input_hash_check": "input_hash ~ '^[0-9a-f]{64}$'",
    "hard_filter_evaluation_result_hash_check": "result_hash ~ '^[0-9a-f]{64}$'",
    "hard_filter_evaluation_policy_manifest_object_check": (
        "jsonb_typeof(policy_manifest) = 'object'"
    ),
    "hard_filter_evaluation_input_payload_object_check": ("jsonb_typeof(input_payload) = 'object'"),
    "hard_filter_evaluation_output_payload_object_check": (
        "jsonb_typeof(output_payload) = 'object'"
    ),
    "hard_filter_evaluation_rejection_reasons_array_check": (
        "jsonb_typeof(rejection_reasons) = 'array'"
    ),
    "hard_filter_evaluation_unknowns_array_check": "jsonb_typeof(unknowns) = 'array'",
    "hard_filter_evaluation_rule_outcomes_array_check": ("jsonb_typeof(rule_outcomes) = 'array'"),
    "hard_filter_evaluation_rule_outcomes_count_check": ("jsonb_array_length(rule_outcomes) = 9"),
    "hard_filter_evaluation_staleness_source_check": (
        "staleness_source in ('source_published_at', 'first_seen_at')"
    ),
    "hard_filter_evaluation_eligible_reasons_check": (
        "eligible = (jsonb_array_length(rejection_reasons) = 0)"
    ),
}


def _migration_path() -> Path:
    return ROOT / "migrations" / "versions" / EXPECTED_FILENAME


def _module_and_source() -> tuple[object, str]:
    path = _migration_path()
    spec = importlib.util.spec_from_file_location("hard_filter_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path.read_text()


def _migration_tree() -> ast.Module:
    return ast.parse(_migration_path().read_text())


def _attr_chain(node: ast.expr) -> str:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _named_kwarg(call: ast.Call, key: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == key:
            return keyword.value
    return None


def _sa_calls(tree: ast.AST, chain: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _attr_chain(node.func) == chain
    ]


def _check_expression(call: ast.Call) -> str:
    literal = call.args[0] if call.args else _named_kwarg(call, "sqltext")
    assert isinstance(literal, ast.Constant) and isinstance(
        literal.value, str
    ), f"check expression must be a string literal: {ast.unparse(call)}"
    return literal.value


def _column_label(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and _attr_chain(node.func) == "sa.text":
        args = node.args
        assert (
            len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)
        ), f"sa.text() index column must take one string literal; {ast.unparse(node)}"
        return f'sa.text("{args[0].value}")'
    raise AssertionError(f"unexpected index column expression: {ast.unparse(node)}")


def test_revision_chain_and_filename() -> None:
    module, _ = _module_and_source()
    assert module.revision == REVISION
    assert module.down_revision == "0003_posting_current_version"


def test_only_evaluation_table_and_required_index_are_created() -> None:
    _, source = _module_and_source()
    assert 'op.create_table(\n        "hard_filter_evaluation"' in source
    assert source.count("op.create_table(") == 1
    assert source.count("op.create_index(") == 1
    assert '"hard_filter_evaluation_current_lookup_idx"' in source
    assert '"hard_filter_evaluation_input_hash_key"' in source
    assert '"score"' not in source
    assert '"digest_item"' not in source


def test_stable_constraint_names_and_reversible_downgrade() -> None:
    _, source = _module_and_source()
    required = {
        "hard_filter_evaluation_pkey",
        "hard_filter_evaluation_posting_version_owner_fkey",
        "hard_filter_evaluation_input_hash_key",
        "hard_filter_evaluation_positive_ids_check",
        "hard_filter_evaluation_policy_version_check",
        "hard_filter_evaluation_policy_manifest_hash_check",
        "hard_filter_evaluation_mutable_state_hash_check",
        "hard_filter_evaluation_input_hash_check",
        "hard_filter_evaluation_result_hash_check",
        "hard_filter_evaluation_policy_manifest_object_check",
        "hard_filter_evaluation_input_payload_object_check",
        "hard_filter_evaluation_output_payload_object_check",
        "hard_filter_evaluation_rejection_reasons_array_check",
        "hard_filter_evaluation_unknowns_array_check",
        "hard_filter_evaluation_rule_outcomes_array_check",
        "hard_filter_evaluation_rule_outcomes_count_check",
        "hard_filter_evaluation_staleness_source_check",
        "hard_filter_evaluation_eligible_reasons_check",
    }
    assert all(name in source for name in required)
    assert 'op.drop_table("hard_filter_evaluation")' in source
    assert (
        'op.drop_index("hard_filter_evaluation_current_lookup_idx", '
        'table_name="hard_filter_evaluation")' in source
    )


def test_id_column_is_bigint_identity() -> None:
    _, source = _module_and_source()
    assert "sa.Identity(always=False)" in source
    assert '"hard_filter_evaluation_pkey"' in source
    assert "primary_key=True" not in source


def test_composite_ownership_foreign_key_uses_restrict() -> None:
    _, source = _module_and_source()
    assert '"hard_filter_evaluation_posting_version_owner_fkey"' in source
    assert '["posting_id", "posting_version_id"]' in source
    assert '["posting_version.posting_id", "posting_version.id"]' in source
    assert 'ondelete="RESTRICT"' in source


def test_every_check_constraint_has_exact_name_and_expression() -> None:
    calls = _sa_calls(_migration_tree(), "sa.CheckConstraint")
    assert len(calls) == len(EXPECTED_CHECK_EXPRESSIONS), (
        f"expected exactly {len(EXPECTED_CHECK_EXPRESSIONS)} check constraints, "
        f"found {len(calls)}"
    )
    found: dict[str, str] = {}
    for call in calls:
        name = _named_kwarg(call, "name")
        assert isinstance(name, ast.Constant) and isinstance(name.value, str), (
            f"every check constraint must pass an explicit literal name=; " f"{ast.unparse(call)}"
        )
        assert name.value not in found, f"duplicate check constraint name {name.value!r}"
        found[name.value] = _check_expression(call)
    missing = sorted(set(EXPECTED_CHECK_EXPRESSIONS) - set(found))
    extra = sorted(set(found) - set(EXPECTED_CHECK_EXPRESSIONS))
    changed = sorted(
        name
        for name in set(found) & set(EXPECTED_CHECK_EXPRESSIONS)
        if found[name] != EXPECTED_CHECK_EXPRESSIONS[name]
    )
    assert not missing and not extra and not changed, (
        "check constraints differ from the independent expectation; "
        f"missing={missing} extra={extra} changed={changed}"
    )


def test_single_lookup_index_has_exact_structure() -> None:
    calls = _sa_calls(_migration_tree(), "op.create_index")
    assert len(calls) == 1, f"exactly one explicit index expected; found {len(calls)}"
    call = calls[0]
    assert len(call.args) == 3, f"index must pass name, table, and columns; {ast.unparse(call)}"
    name, table = call.args[0], call.args[1]
    assert isinstance(name, ast.Constant) and name.value == LOOKUP_INDEX_NAME
    assert isinstance(table, ast.Constant) and table.value == "hard_filter_evaluation"
    columns = call.args[2]
    assert isinstance(
        columns, ast.List
    ), f"index columns must be a literal list; {ast.unparse(call)}"
    assert [_column_label(item) for item in columns.elts] == [
        "posting_id",
        "posting_version_id",
        "policy_version",
        'sa.text("evaluation_as_of DESC")',
    ]
    unique = _named_kwarg(call, "unique")
    assert unique is None or (
        isinstance(unique, ast.Constant) and unique.value is False
    ), "the lookup index must stay non-unique"

"""Deterministic checks for revision 0003_posting_current_version."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REVISION_ID = "0003_posting_current_version"
DOWN_REVISION = "0002_resume_source_state"
EXPECTED_FILENAME = "20260727_1200_0003_posting_current_version.py"

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _revision_path() -> Path:
    paths = sorted(_VERSIONS_DIR.glob("*_0003_posting_current_version.py"))
    assert len(paths) == 1
    return paths[0]


def _revision_module() -> object:
    path = _revision_path()
    spec = importlib.util.spec_from_file_location("jobs_revision_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "op":
            names.append(node.func.attr)
    return names


def test_revision_identity_and_filename_are_stable() -> None:
    module = _revision_module()
    assert module.revision == REVISION_ID  # type: ignore[attr-defined]
    assert module.down_revision == DOWN_REVISION  # type: ignore[attr-defined]
    assert module.branch_labels is None  # type: ignore[attr-defined]
    assert module.depends_on is None  # type: ignore[attr-defined]
    assert _revision_path().name == EXPECTED_FILENAME


def test_upgrade_and_downgrade_have_exact_structural_operations() -> None:
    source = _revision_path().read_text()
    calls = _call_names(source)
    assert calls.count("add_column") == 1
    assert calls.count("create_index") == 1
    assert calls.count("create_unique_constraint") == 1
    assert calls.count("create_foreign_key") == 1
    assert calls.count("execute") == 1
    assert calls.count("drop_constraint") == 2
    assert calls.count("drop_index") == 1
    assert calls.count("drop_column") == 1


def test_stable_names_deferred_ownership_and_backfill_order_are_present() -> None:
    source = _revision_path().read_text()
    assert "posting_current_version_id_idx" in source
    assert "posting_version_posting_id_id_key" in source
    assert "posting_current_version_owner_fkey" in source
    assert 'ondelete="NO ACTION"' in source
    assert "deferrable=True" in source
    assert 'initially="DEFERRED"' in source
    assert "ORDER BY pv.observed_at DESC, pv.id DESC" in source

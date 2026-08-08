"""Single-posting hard-filter evaluation orchestration.

The service owns caller validation, UTC evaluation-time normalization, policy
loading, the initial deterministic snapshot, pure evaluator invocation,
canonical output construction, and the repository persistence boundary. All
transactional work — locking, immutable evaluation persistence, the required
filter-stage score projection, replay, race recovery, and persisted-state
validation — lives in :mod:`app.filters.repository`, which is the single
authoritative persistence implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.filters.evaluator import ActionFact, PureFilterInput, evaluate_pure
from app.filters.policy import (
    POLICY_MANIFEST_HASH,
    POLICY_VERSION,
    SCHEMA_VERSION,
    manifest_copy,
    manifest_is_intact,
)
from app.filters.repository import (
    EvaluationRecord,
    company,
    digest_item,
    persist_or_replay,
    posting,
    posting_version,
    score,
)
from app.filters.results import (
    CurrentVersionError,
    FilterDatabaseError,
    FilterError,
    FilterErrorCode,
    FilterEvaluatorDefectError,
    FilterInputError,
    FilterPersistedInputError,
    HardFilterResult,
    PostingClosedError,
    PostingNotFoundError,
    UnsupportedFilterPolicyError,
)

_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTION_STATES = {"recommended", "restored", "expired", "skipped", "applied"}


def _canonical_json(value: object) -> tuple[dict[str, Any] | list[Any], str]:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    parsed = json.loads(serialized)
    return parsed, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _hash_json(value: object) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalize_as_of(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT) from exc
    if offset != UTC.utcoffset(value):
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    return value.astimezone(UTC)


def _validate_policy(policy_version: str) -> None:
    if not isinstance(policy_version, str) or not _POLICY_VERSION_RE.fullmatch(policy_version):
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if policy_version != POLICY_VERSION or not manifest_is_intact():
        raise UnsupportedFilterPolicyError(FilterErrorCode.POLICY_UNSUPPORTED)


def _validate_positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    return value


def _action_rows(conn, posting_id: int) -> list[dict[str, Any]]:
    statement = (
        select(
            digest_item.c.id.label("digest_item_id"),
            digest_item.c.posting_id,
            digest_item.c.score_id,
            digest_item.c.state,
            score.c.posting_version_id.label("score_posting_version_id"),
            posting_version.c.posting_id.label("version_posting_id"),
        )
        .select_from(
            digest_item.join(score, digest_item.c.score_id == score.c.id).outerjoin(
                posting_version, score.c.posting_version_id == posting_version.c.id
            )
        )
        .where(digest_item.c.posting_id == posting_id)
        .order_by(digest_item.c.id)
    )
    result = conn.execute(statement)
    try:
        return [dict(row) for row in result.mappings().all()]
    finally:
        result.close()


def _validate_action_rows(rows: list[dict[str, Any]], posting_id: int) -> tuple[ActionFact, ...]:
    facts: list[ActionFact] = []
    for row in rows:
        if (
            type(row["digest_item_id"]) is not int
            or row["digest_item_id"] <= 0
            or type(row["posting_id"]) is not int
            or row["posting_id"] != posting_id
            or type(row["score_id"]) is not int
            or row["score_id"] <= 0
            or type(row["score_posting_version_id"]) is not int
            or row["score_posting_version_id"] <= 0
            or type(row["version_posting_id"]) is not int
            or row["version_posting_id"] != posting_id
            or row["state"] not in _ACTION_STATES
        ):
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
        facts.append(
            ActionFact(
                digest_item_id=row["digest_item_id"],
                posting_id=posting_id,
                score_posting_version_id=row["score_posting_version_id"],
                state=row["state"],
                score_id=row["score_id"],
            )
        )
    return tuple(facts)


def _posting_snapshot(conn, posting_id: int) -> dict[str, Any]:
    result = conn.execute(select(posting).where(posting.c.id == posting_id))
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    if row is None:
        raise PostingNotFoundError(FilterErrorCode.POSTING_NOT_FOUND)
    data = dict(row)
    current_id = data["current_version_id"]
    if isinstance(current_id, bool) or not isinstance(current_id, int) or current_id <= 0:
        raise CurrentVersionError(FilterErrorCode.CURRENT_VERSION_INVALID)
    version_result = conn.execute(
        select(posting_version).where(
            and_(posting_version.c.id == current_id, posting_version.c.posting_id == posting_id)
        )
    )
    try:
        version = version_result.mappings().one_or_none()
    finally:
        version_result.close()
    if version is None:
        raise CurrentVersionError(FilterErrorCode.CURRENT_VERSION_INVALID)
    company_result = conn.execute(select(company).where(company.c.id == data["company_id"]))
    try:
        company_row = company_result.mappings().one_or_none()
    finally:
        company_result.close()
    if company_row is None or type(company_row["blocked"]) is not bool:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if data["closed_at"] is not None:
        raise PostingClosedError(FilterErrorCode.POSTING_CLOSED)
    for timestamp in (data["source_published_at"], data["first_seen_at"]):
        if timestamp is not None and (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if data["first_seen_at"] is None:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if not isinstance(version["title"], str) or not isinstance(version["locations"], list):
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if not version["title"]:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if version["description_md"] is not None and not isinstance(version["description_md"], str):
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if not isinstance(version["content_hash"], str) or not _HASH_RE.fullmatch(
        version["content_hash"]
    ):
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    for location in version["locations"]:
        if not isinstance(location, dict):
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
        if not isinstance(location.get("label"), str) or not location["label"]:
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
        for key in ("label", "country_code", "region", "city", "workplace_type"):
            if key in location and location[key] is not None and not isinstance(location[key], str):
                raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    return {"posting": data, "version": dict(version), "company": dict(company_row)}


def _mutable_payload(snapshot: dict[str, Any], facts: tuple[ActionFact, ...]) -> dict[str, Any]:
    p = snapshot["posting"]
    c = snapshot["company"]
    return {
        "action_state": [
            {
                "digest_item_id": fact.digest_item_id,
                "posting_id": fact.posting_id,
                "score_posting_version_id": fact.score_posting_version_id,
                "state": fact.state,
            }
            for fact in facts
        ],
        "closed_at": _timestamp(p["closed_at"]),
        "company_blocked": c["blocked"],
        "current_version_id": p["current_version_id"],
        "first_seen_at": _timestamp(p["first_seen_at"]),
        "source_published_at": _timestamp(p["source_published_at"]),
    }


def _input_payload(
    snapshot: dict[str, Any], facts: tuple[ActionFact, ...], *, policy_version: str, as_of: datetime
) -> tuple[dict[str, Any], str, str]:
    mutable, mutable_hash = _canonical_json(_mutable_payload(snapshot, facts))
    v = snapshot["version"]
    payload = {
        "evaluation_as_of": _timestamp(as_of),
        "mutable_state": mutable,
        "mutable_state_hash": mutable_hash,
        "policy_manifest": manifest_copy(),
        "policy_manifest_hash": POLICY_MANIFEST_HASH,
        "policy_version": policy_version,
        "posting_id": snapshot["posting"]["id"],
        "posting_version_content_hash": v["content_hash"],
        "posting_version_id": v["id"],
        "schema_version": SCHEMA_VERSION,
    }
    canonical, input_hash = _canonical_json(payload)
    assert isinstance(canonical, dict)
    return canonical, input_hash, mutable_hash


def _output_payload(pure) -> tuple[dict[str, Any], str]:
    payload = {
        "eligible": pure.eligible,
        "rejection_reasons": [reason.value for reason in pure.rejection_reasons],
        "rule_outcomes": [
            {
                "evidence": outcome.evidence.value,
                "rule": outcome.rule.value,
                "status": outcome.status.value,
            }
            for outcome in pure.rule_outcomes
        ],
        "schema_version": SCHEMA_VERSION,
        "staleness_source": pure.staleness_source.value,
        "unknowns": [unknown.value for unknown in pure.unknowns],
    }
    canonical, result_hash = _canonical_json(payload)
    assert isinstance(canonical, dict)
    return canonical, result_hash


def _load_snapshot(
    engine: Engine, posting_id: int
) -> tuple[dict[str, Any], tuple[ActionFact, ...]]:
    """Read the initial deterministic snapshot without locks. The repository
    re-validates every canonical identity input under its lock sequence before
    any persistence and raises the closed stale error on any change."""
    with engine.connect() as conn:
        actions = _validate_action_rows(_action_rows(conn, posting_id), posting_id)
        snapshot = _posting_snapshot(conn, posting_id)
    return snapshot, actions


def evaluate_hard_filters(
    *, engine: Engine, posting_id: int, policy_version: str, evaluation_as_of: datetime
) -> HardFilterResult:
    """Evaluate and atomically persist one posting's deterministic filters."""

    posting_id = _validate_positive_id(posting_id)
    _validate_policy(policy_version)
    as_of = _normalize_as_of(evaluation_as_of)
    try:
        snapshot, actions = _load_snapshot(engine, posting_id)
        v = snapshot["version"]
        pure_input = PureFilterInput(
            posting_id=posting_id,
            posting_version_id=v["id"],
            content_hash=v["content_hash"],
            title=v["title"],
            description_md=v["description_md"],
            locations=tuple(deepcopy(v["locations"])),
            source_published_at=snapshot["posting"]["source_published_at"],
            first_seen_at=snapshot["posting"]["first_seen_at"],
            current_version_id=snapshot["posting"]["current_version_id"],
            closed_at=snapshot["posting"]["closed_at"],
            company_blocked=snapshot["company"]["blocked"],
            action_state=actions,
        )
        try:
            pure = evaluate_pure(pure_input, evaluation_as_of=as_of)
        except ValueError as exc:
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID) from exc
        input_payload, input_hash, mutable_hash = _input_payload(
            snapshot, actions, policy_version=policy_version, as_of=as_of
        )
        output_payload, result_hash = _output_payload(pure)
        record = EvaluationRecord(
            posting_id=posting_id,
            posting_version_id=v["id"],
            policy_version=policy_version,
            policy_manifest_hash=POLICY_MANIFEST_HASH,
            mutable_state_hash=mutable_hash,
            result_hash=result_hash,
            input_hash=input_hash,
            policy_manifest=manifest_copy(),
            input_payload=input_payload,
            output_payload=output_payload,
            evaluation_as_of=as_of,
            eligible=pure.eligible,
            rejection_reasons=pure.rejection_reasons,
            unknowns=pure.unknowns,
            staleness_source=pure.staleness_source,
            rule_outcomes=pure.rule_outcomes,
            observed_run_id=v["observed_in_run_id"],
        )
        return persist_or_replay(engine=engine, record=record)
    except FilterError:
        raise
    except SQLAlchemyError as exc:
        raise FilterDatabaseError(FilterErrorCode.DATABASE_ERROR) from exc
    except Exception as exc:
        raise FilterEvaluatorDefectError(FilterErrorCode.EVALUATOR_DEFECT) from exc

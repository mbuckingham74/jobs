"""Declared SQLAlchemy Core tables and the Task 008 hard-filter repository.

The lower half of this module declares the exact column projections the
repository reads and writes. The upper layer implements Task 008's immutable
persistence contract for ``hard_filter_evaluation`` plus its required
filter-stage ``score`` projection:

- one transaction with the exact decision-11 lock order;
- insertion of exactly one evaluation from a complete validated
  ``EvaluationRecord`` supplied by the service layer, and — for a completed
  rejected evaluation — exactly one filter-stage ``score`` row in the same
  transaction (neither commits unless both succeed);
- full replay validation of an existing row for the same ``input_hash`` and
  of its required score projection (exactly one for a rejected evaluation,
  none for an eligible one), raising ``FilterEvaluationConflictError`` on any
  disagreement, never repairing;
- revalidation of the authoritative ``posting_version.observed_in_run_id``
  provenance before any persistence;
- the authorized unique-insert race recovery (fresh transaction, complete lock
  sequence, winner validation, ``replayed=True``) that fires only for the
  ``hard_filter_evaluation_input_hash_key`` constraint.

The repository evaluates no rules, reads no clock, makes no model, network,
filesystem, or environment call, and never updates or deletes an evaluation
row.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    MetaData,
    Numeric,
    Table,
    Text,
    and_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.filters.evaluator import (
    HardFilterEvidence,
    HardFilterReason,
    HardFilterRule,
    HardFilterRuleOutcome,
    HardFilterUnknown,
    RuleStatus,
    StalenessSource,
)
from app.filters.policy import (
    POLICY_MANIFEST,
    POLICY_MANIFEST_HASH,
    POLICY_VERSION,
    SCHEMA_VERSION,
    manifest_copy,
)
from app.filters.results import (
    CurrentVersionError,
    FilterDatabaseError,
    FilterErrorCode,
    FilterEvaluationConflictError,
    FilterInputError,
    FilterPersistedInputError,
    HardFilterResult,
    PostingClosedError,
    PostingNotFoundError,
    StaleFilterInputError,
    UnsupportedFilterPolicyError,
)

metadata = MetaData()

company = Table(
    "company",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("blocked", Boolean),
    extend_existing=True,
)
posting = Table(
    "posting",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("company_id", BigInteger),
    Column("source_published_at", TIMESTAMP(timezone=True)),
    Column("first_seen_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("current_version_id", BigInteger),
    extend_existing=True,
)
posting_version = Table(
    "posting_version",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("posting_id", BigInteger),
    Column("observed_in_run_id", BigInteger),
    Column("content_hash", Text),
    Column("title", Text),
    Column("locations", JSONB),
    Column("description_md", Text),
    extend_existing=True,
)
pipeline_run = Table(
    "pipeline_run", metadata, Column("id", BigInteger, primary_key=True), extend_existing=True
)
score = Table(
    "score",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("run_id", BigInteger),
    Column("posting_version_id", BigInteger),
    Column("resume_version_id", BigInteger),
    Column("candidate_profile_version_id", BigInteger),
    Column("company_research_id", BigInteger),
    Column("stage", Text),
    Column("model_id", Text),
    Column("prompt_version", Text),
    Column("ruleset_version", Text),
    Column("rubric_version", Text),
    Column("input_hash", Text),
    Column("overall", Numeric),
    Column("domain_fit", Numeric),
    Column("seniority_fit", Numeric),
    Column("remote_fit", Numeric),
    Column("company_fit", Numeric),
    Column("axis_notes", JSONB),
    Column("verdict", Text),
    Column("location_eligibility", JSONB),
    Column("rationale", Text),
    Column("gaps", JSONB),
    Column("hooks", JSONB),
    Column("red_flags", JSONB),
    Column("resume_variant", Text),
    Column("reject_reasons", JSONB),
    Column("scored_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)
digest_item = Table(
    "digest_item",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("posting_id", BigInteger),
    Column("score_id", BigInteger),
    Column("state", Text),
    extend_existing=True,
)
hard_filter_evaluation = Table(
    "hard_filter_evaluation",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("posting_id", BigInteger),
    Column("posting_version_id", BigInteger),
    Column("policy_version", Text),
    Column("policy_manifest_hash", Text),
    Column("mutable_state_hash", Text),
    Column("result_hash", Text),
    Column("input_hash", Text),
    Column("policy_manifest", JSONB),
    Column("input_payload", JSONB),
    Column("output_payload", JSONB),
    Column("evaluation_as_of", TIMESTAMP(timezone=True)),
    Column("eligible", Boolean),
    Column("rejection_reasons", JSONB),
    Column("unknowns", JSONB),
    Column("staleness_source", Text),
    Column("rule_outcomes", JSONB),
    Column("created_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)

# --------------------------------------------------------------------------
# Typed persistence record and closed vocabulary validation.
# --------------------------------------------------------------------------

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTION_STATES = frozenset({"recommended", "restored", "expired", "skipped", "applied"})

_INPUT_KEYS = frozenset(
    {
        "evaluation_as_of",
        "mutable_state",
        "mutable_state_hash",
        "policy_manifest",
        "policy_manifest_hash",
        "policy_version",
        "posting_id",
        "posting_version_content_hash",
        "posting_version_id",
        "schema_version",
    }
)
_MUTABLE_KEYS = frozenset(
    {
        "action_state",
        "closed_at",
        "company_blocked",
        "current_version_id",
        "first_seen_at",
        "source_published_at",
    }
)
_ACTION_ENTRY_KEYS = frozenset(
    {"digest_item_id", "posting_id", "score_posting_version_id", "state"}
)
_OUTPUT_KEYS = frozenset(
    {
        "eligible",
        "rejection_reasons",
        "rule_outcomes",
        "schema_version",
        "staleness_source",
        "unknowns",
    }
)
_OUTCOME_KEYS = frozenset({"evidence", "rule", "status"})

_ALLOWED_TRIPLES = frozenset(
    (rule, status, evidence)
    for rule, statuses in POLICY_MANIFEST["allowed_outcomes"].items()
    for status, evidence in statuses.items()
    for evidence in (evidence if isinstance(evidence, list) else [evidence])
)


@dataclass(frozen=True)
class EvaluationRecord:
    """Complete validated persistence record produced by the service layer.

    Every field is authoritative: the repository persists exactly these values
    and validates any existing row for the same ``input_hash`` against them.
    ``observed_run_id`` is the authoritative
    ``posting_version.observed_in_run_id`` revalidated under lock and used for
    the filter-stage score provenance only; it never affects ``input_hash``.
    """

    posting_id: int
    posting_version_id: int
    policy_version: str
    policy_manifest_hash: str
    mutable_state_hash: str
    result_hash: str
    input_hash: str
    policy_manifest: dict[str, Any]
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    evaluation_as_of: datetime
    eligible: bool
    rejection_reasons: tuple[HardFilterReason, ...]
    unknowns: tuple[HardFilterUnknown, ...]
    staleness_source: StalenessSource
    rule_outcomes: tuple[HardFilterRuleOutcome, ...]
    observed_run_id: int


def canonical_json(value: object) -> tuple[dict[str, Any] | list[Any], str]:
    """Canonical UTF-8 JSON and lowercase SHA-256 (Task 008 canonical form)."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    parsed = json.loads(serialized)
    return parsed, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _conflict() -> FilterEvaluationConflictError:
    return FilterEvaluationConflictError(FilterErrorCode.EVALUATION_CONFLICT)


def _validate_record(record: EvaluationRecord) -> None:
    if type(record.posting_id) is not int or record.posting_id <= 0:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if type(record.posting_version_id) is not int or record.posting_version_id <= 0:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if type(record.observed_run_id) is not int or record.observed_run_id <= 0:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if not isinstance(record.evaluation_as_of, datetime) or record.evaluation_as_of.tzinfo is None:
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if not isinstance(record.input_hash, str) or not _HASH_RE.fullmatch(record.input_hash):
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)
    if not isinstance(record.eligible, bool) or record.eligible != (not record.rejection_reasons):
        raise FilterInputError(FilterErrorCode.INVALID_INPUT)


# Columns unrelated to the filter-stage projection. A filter-stage score row
# must keep every one of these NULL; replay validates that.
_UNRELATED_SCORE_COLUMNS = (
    "resume_version_id",
    "candidate_profile_version_id",
    "company_research_id",
    "model_id",
    "prompt_version",
    "rubric_version",
    "overall",
    "domain_fit",
    "seniority_fit",
    "remote_fit",
    "company_fit",
    "axis_notes",
    "verdict",
    "location_eligibility",
    "rationale",
    "gaps",
    "hooks",
    "red_flags",
    "resume_variant",
)


def _validated_reasons(values: list[Any]) -> tuple[HardFilterReason, ...]:
    try:
        reasons = tuple(HardFilterReason(value) for value in values)
    except ValueError:
        raise _conflict() from None
    ordered = tuple(reason for reason in HardFilterReason if reason in reasons)
    if reasons != ordered:
        raise _conflict()
    return reasons


def _validated_unknowns(values: list[Any]) -> tuple[HardFilterUnknown, ...]:
    try:
        unknowns = tuple(HardFilterUnknown(value) for value in values)
    except ValueError:
        raise _conflict() from None
    ordered = tuple(unknown for unknown in HardFilterUnknown if unknown in unknowns)
    if unknowns != ordered:
        raise _conflict()
    return unknowns


def _validated_outcomes(values: list[Any]) -> tuple[HardFilterRuleOutcome, ...]:
    if len(values) != 9:
        raise _conflict()
    for item in values:
        if not isinstance(item, dict) or set(item) != _OUTCOME_KEYS:
            raise _conflict()
        if not all(isinstance(item[key], str) for key in _OUTCOME_KEYS):
            raise _conflict()
    if [item["rule"] for item in values] != [rule.value for rule in HardFilterRule]:
        raise _conflict()
    try:
        outcomes = tuple(
            HardFilterRuleOutcome(
                rule=HardFilterRule(item["rule"]),
                status=RuleStatus(item["status"]),
                evidence=HardFilterEvidence(item["evidence"]),
            )
            for item in values
        )
    except ValueError:
        raise _conflict() from None
    for outcome in outcomes:
        if (
            outcome.rule.value,
            outcome.status.value,
            outcome.evidence.value,
        ) not in _ALLOWED_TRIPLES:
            raise _conflict()
    return outcomes


def _validate_input_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != _INPUT_KEYS:
        raise _conflict()
    for key in ("posting_id", "posting_version_id"):
        if type(payload[key]) is not int or payload[key] <= 0:
            raise _conflict()
    if not isinstance(payload["evaluation_as_of"], str):
        raise _conflict()
    if payload["schema_version"] != SCHEMA_VERSION:
        raise _conflict()
    if not isinstance(payload["policy_version"], str):
        raise _conflict()
    for key in ("mutable_state_hash", "policy_manifest_hash", "posting_version_content_hash"):
        if not isinstance(payload[key], str) or not _HASH_RE.fullmatch(payload[key]):
            raise _conflict()
    if not isinstance(payload["policy_manifest"], dict):
        raise _conflict()
    mutable = payload["mutable_state"]
    if not isinstance(mutable, dict) or set(mutable) != _MUTABLE_KEYS:
        raise _conflict()
    actions = mutable["action_state"]
    if not isinstance(actions, list):
        raise _conflict()
    for entry in actions:
        if not isinstance(entry, dict) or set(entry) != _ACTION_ENTRY_KEYS:
            raise _conflict()
        for key in ("digest_item_id", "posting_id", "score_posting_version_id"):
            if type(entry[key]) is not int or entry[key] <= 0:
                raise _conflict()
        if not isinstance(entry["state"], str) or entry["state"] not in _ACTION_STATES:
            raise _conflict()
    item_ids = [entry["digest_item_id"] for entry in actions]
    if item_ids != sorted(item_ids) or len(set(item_ids)) != len(item_ids):
        raise _conflict()
    if type(mutable["current_version_id"]) is not int or mutable["current_version_id"] <= 0:
        raise _conflict()
    if not isinstance(mutable["company_blocked"], bool):
        raise _conflict()
    if not isinstance(mutable["first_seen_at"], str):
        raise _conflict()
    for key in ("closed_at", "source_published_at"):
        if mutable[key] is not None and not isinstance(mutable[key], str):
            raise _conflict()


def _validate_output_payload(output: Any, row: dict[str, Any]) -> None:
    if not isinstance(output, dict) or set(output) != _OUTPUT_KEYS:
        raise _conflict()
    if type(output["eligible"]) is not bool:
        raise _conflict()
    if output["schema_version"] != SCHEMA_VERSION:
        raise _conflict()
    if not isinstance(output["staleness_source"], str):
        raise _conflict()
    for key in ("rejection_reasons", "unknowns", "rule_outcomes"):
        if not isinstance(output[key], list):
            raise _conflict()
    if output["rejection_reasons"] != row["rejection_reasons"]:
        raise _conflict()
    if output["unknowns"] != row["unknowns"]:
        raise _conflict()
    if output["staleness_source"] != row["staleness_source"]:
        raise _conflict()
    if output["rule_outcomes"] != row["rule_outcomes"]:
        raise _conflict()
    if output["eligible"] != row["eligible"]:
        raise _conflict()


# --------------------------------------------------------------------------
# Decision-11 lock sequence, authoritative loading, and stale recheck.
# --------------------------------------------------------------------------


def _action_facts(conn: Connection, posting_id: int) -> list[dict[str, Any]]:
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


def _validated_action_facts(conn: Connection, posting_id: int) -> list[dict[str, Any]]:
    rows = _action_facts(conn, posting_id)
    facts: list[dict[str, Any]] = []
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
            {
                "digest_item_id": row["digest_item_id"],
                "posting_id": row["posting_id"],
                "score_id": row["score_id"],
                "score_posting_version_id": row["score_posting_version_id"],
                "state": row["state"],
            }
        )
    return facts


def _lock_linked_scores(conn: Connection, facts: list[dict[str, Any]]) -> None:
    score_ids = sorted({fact["score_id"] for fact in facts})
    if not score_ids:
        return
    result = conn.execute(
        select(score.c.id)
        .where(score.c.id.in_(score_ids))
        .order_by(score.c.id)
        .with_for_update(read=True)
    )
    try:
        locked = [value for (value,) in result.all()]
    finally:
        result.close()
    if locked != score_ids:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)


def _lock_posting(conn: Connection, posting_id: int) -> dict[str, Any]:
    result = conn.execute(select(posting).where(posting.c.id == posting_id).with_for_update())
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    if row is None:
        raise PostingNotFoundError(FilterErrorCode.POSTING_NOT_FOUND)
    data = dict(row)
    if type(data["id"]) is not int or type(data["company_id"]) is not int:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    current_id = data["current_version_id"]
    if isinstance(current_id, bool) or not isinstance(current_id, int) or current_id <= 0:
        raise CurrentVersionError(FilterErrorCode.CURRENT_VERSION_INVALID)
    if data["closed_at"] is not None:
        raise PostingClosedError(FilterErrorCode.POSTING_CLOSED)
    for key in ("source_published_at", "first_seen_at"):
        value = data[key]
        if value is not None and (
            not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        ):
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    if data["first_seen_at"] is None:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    return data


def _lock_version(conn: Connection, posting_row: dict[str, Any]) -> dict[str, Any]:
    result = conn.execute(
        select(posting_version)
        .where(
            and_(
                posting_version.c.id == posting_row["current_version_id"],
                posting_version.c.posting_id == posting_row["id"],
            )
        )
        .with_for_update(read=True)
    )
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    if row is None:
        raise CurrentVersionError(FilterErrorCode.CURRENT_VERSION_INVALID)
    version = dict(row)
    if (
        type(version["id"]) is not int
        or type(version["posting_id"]) is not int
        or version["posting_id"] != posting_row["id"]
        or not isinstance(version["content_hash"], str)
        or not _HASH_RE.fullmatch(version["content_hash"])
    ):
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    return version


def _lock_company(conn: Connection, posting_row: dict[str, Any]) -> dict[str, Any]:
    result = conn.execute(
        select(company).where(company.c.id == posting_row["company_id"]).with_for_update(read=True)
    )
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    if row is None or type(row["blocked"]) is not bool:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    return dict(row)


def _validate_run_provenance(conn: Connection, record: EvaluationRecord) -> None:
    """The authoritative version's ``observed_in_run_id`` must name an
    existing run. The run id is score-projection provenance only and never
    affects ``input_hash``."""
    result = conn.execute(
        select(pipeline_run.c.id).where(pipeline_run.c.id == record.observed_run_id)
    )
    try:
        if result.scalar_one_or_none() is None:
            raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    finally:
        result.close()


def _recheck(
    record: EvaluationRecord,
    posting_row: dict[str, Any],
    version_row: dict[str, Any],
    company_row: dict[str, Any],
    facts: list[dict[str, Any]],
) -> None:
    if (
        record.policy_version != POLICY_VERSION
        or record.policy_manifest_hash != POLICY_MANIFEST_HASH
        or record.policy_manifest != manifest_copy()
    ):
        raise UnsupportedFilterPolicyError(FilterErrorCode.POLICY_UNSUPPORTED)
    payload = record.input_payload
    mutable = payload["mutable_state"]
    rebuilt_actions = [
        {
            "digest_item_id": fact["digest_item_id"],
            "posting_id": fact["posting_id"],
            "score_posting_version_id": fact["score_posting_version_id"],
            "state": fact["state"],
        }
        for fact in facts
    ]
    if version_row["observed_in_run_id"] != record.observed_run_id:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if posting_row["current_version_id"] != record.posting_version_id:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if version_row["content_hash"] != payload["posting_version_content_hash"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["posting_id"] != record.posting_id:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["posting_version_id"] != record.posting_version_id:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["policy_version"] != record.policy_version:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["policy_manifest"] != record.policy_manifest:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["policy_manifest_hash"] != record.policy_manifest_hash:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["schema_version"] != SCHEMA_VERSION:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if payload["evaluation_as_of"] != _ts(record.evaluation_as_of):
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if rebuilt_actions != mutable["action_state"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if _ts(posting_row["closed_at"]) != mutable["closed_at"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if company_row["blocked"] != mutable["company_blocked"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if posting_row["current_version_id"] != mutable["current_version_id"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if _ts(posting_row["first_seen_at"]) != mutable["first_seen_at"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if _ts(posting_row["source_published_at"]) != mutable["source_published_at"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    _, mutable_hash = canonical_json(mutable)
    if mutable_hash != record.mutable_state_hash:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    if mutable_hash != payload["mutable_state_hash"]:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)


def _lock_and_recheck(
    conn: Connection, record: EvaluationRecord
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    # Decision 11 order, preserved literally:
    # 1. table lock fences digest inserts/updates/deletes
    conn.execute(text("LOCK TABLE digest_item IN SHARE MODE"))
    # 2. re-read the complete joined action fact set
    initial_actions = _validated_action_facts(conn, record.posting_id)
    # 3. lock every linked score row FOR SHARE in ascending score.id
    _lock_linked_scores(conn, initial_actions)
    # 4. linked score posting-version ownership validated inside the fact read
    # 5. lock the target posting row FOR UPDATE
    posting_row = _lock_posting(conn, record.posting_id)
    # 6. lock its selected posting_version row FOR SHARE
    version_row = _lock_version(conn, posting_row)
    _validate_run_provenance(conn, record)
    # 7. lock the linked company row FOR SHARE
    company_row = _lock_company(conn, posting_row)
    # 8. recheck every canonical mutable and immutable identity input
    locked_actions = _validated_action_facts(conn, record.posting_id)
    if locked_actions != initial_actions:
        raise StaleFilterInputError(FilterErrorCode.INPUT_STALE)
    _recheck(record, posting_row, version_row, company_row, locked_actions)
    return posting_row, version_row, company_row, locked_actions


# --------------------------------------------------------------------------
# Immutable insert, replay validation, and the authorized race recovery.
# --------------------------------------------------------------------------


def _load_by_input_hash(conn: Connection, input_hash: str) -> dict[str, Any] | None:
    result = conn.execute(
        select(hard_filter_evaluation).where(hard_filter_evaluation.c.input_hash == input_hash)
    )
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    return dict(row) if row is not None else None


def _insert_evaluation(conn: Connection, record: EvaluationRecord) -> int:
    inserted = conn.execute(
        hard_filter_evaluation.insert()
        .values(
            posting_id=record.posting_id,
            posting_version_id=record.posting_version_id,
            policy_version=record.policy_version,
            policy_manifest_hash=record.policy_manifest_hash,
            mutable_state_hash=record.mutable_state_hash,
            result_hash=record.result_hash,
            input_hash=record.input_hash,
            policy_manifest=record.policy_manifest,
            input_payload=record.input_payload,
            output_payload=record.output_payload,
            evaluation_as_of=record.evaluation_as_of,
            eligible=record.eligible,
            rejection_reasons=[reason.value for reason in record.rejection_reasons],
            unknowns=[unknown.value for unknown in record.unknowns],
            staleness_source=record.staleness_source.value,
            rule_outcomes=[
                {
                    "evidence": outcome.evidence.value,
                    "rule": outcome.rule.value,
                    "status": outcome.status.value,
                }
                for outcome in record.rule_outcomes
            ],
        )
        .returning(hard_filter_evaluation.c.id)
    )
    try:
        return inserted.scalar_one()
    finally:
        inserted.close()


def _insert_score(conn: Connection, record: EvaluationRecord) -> None:
    """Insert the sole filter-stage score projection for a rejected evaluation.

    Only the required projection fields are populated; every unrelated
    nullable model, prompt, résumé, research, numeric, verdict, rationale,
    gap, hook, flag, and triage column stays NULL. This runs in the same
    transaction as the evaluation insert, so neither row commits unless both
    succeed.
    """
    inserted = conn.execute(
        score.insert().values(
            run_id=record.observed_run_id,
            posting_version_id=record.posting_version_id,
            stage="filter",
            ruleset_version=record.policy_version,
            input_hash=record.input_hash,
            reject_reasons=[reason.value for reason in record.rejection_reasons],
            scored_at=record.evaluation_as_of,
        )
    )
    inserted.close()


def _validate_score_projection(conn: Connection, record: EvaluationRecord) -> None:
    """Replay validation of the required filter-stage score projection.

    A rejected evaluation requires exactly the one matching ``S`` with every
    projection field correct and all unrelated columns NULL. An eligible
    evaluation requires that no ``S`` exists at its key. Any mismatch raises
    the closed conflict error and is never repaired.
    """
    result = conn.execute(
        select(score)
        .where(
            and_(
                score.c.posting_version_id == record.posting_version_id,
                score.c.stage == "filter",
                score.c.input_hash == record.input_hash,
            )
        )
        .order_by(score.c.id)
    )
    try:
        rows = [dict(row) for row in result.mappings().all()]
    finally:
        result.close()
    if record.eligible:
        if rows:
            raise _conflict()
        return
    if len(rows) != 1:
        raise _conflict()
    row = rows[0]
    expected_reasons = [reason.value for reason in record.rejection_reasons]
    if (
        row["run_id"] != record.observed_run_id
        or row["posting_version_id"] != record.posting_version_id
        or row["ruleset_version"] != record.policy_version
        or row["input_hash"] != record.input_hash
        or row["reject_reasons"] != expected_reasons
        or row["scored_at"] != record.evaluation_as_of
        or any(row[column] is not None for column in _UNRELATED_SCORE_COLUMNS)
    ):
        raise _conflict()


def _ensure_no_score(conn: Connection, record: EvaluationRecord) -> None:
    """A new eligible identity must have no filter-stage score at its key."""
    result = conn.execute(
        select(score.c.id).where(
            and_(
                score.c.posting_version_id == record.posting_version_id,
                score.c.stage == "filter",
                score.c.input_hash == record.input_hash,
            )
        )
    )
    try:
        if result.scalar_one_or_none() is not None:
            raise _conflict()
    finally:
        result.close()


def _result_from_record(
    record: EvaluationRecord, evaluation_id: int, *, replayed: bool
) -> HardFilterResult:
    return HardFilterResult(
        evaluation_id=evaluation_id,
        posting_id=record.posting_id,
        posting_version_id=record.posting_version_id,
        policy_version=record.policy_version,
        policy_manifest_hash=record.policy_manifest_hash,
        mutable_state_hash=record.mutable_state_hash,
        input_hash=record.input_hash,
        result_hash=record.result_hash,
        evaluation_as_of=record.evaluation_as_of,
        eligible=record.eligible,
        rejection_reasons=record.rejection_reasons,
        unknowns=record.unknowns,
        staleness_source=record.staleness_source,
        rule_outcomes=record.rule_outcomes,
        replayed=replayed,
    )


def _validate_persisted(row: dict[str, Any], record: EvaluationRecord) -> HardFilterResult:
    """Full Task 008 replay validation of one persisted evaluation row."""
    if type(row["id"]) is not int or row["id"] <= 0:
        raise _conflict()
    if (
        row["posting_id"] != record.posting_id
        or row["posting_version_id"] != record.posting_version_id
    ):
        raise _conflict()
    if row["policy_version"] != record.policy_version:
        raise _conflict()
    if row["policy_manifest_hash"] != record.policy_manifest_hash:
        raise _conflict()
    if row["mutable_state_hash"] != record.mutable_state_hash:
        raise _conflict()
    if row["result_hash"] != record.result_hash:
        raise _conflict()
    if row["input_hash"] != record.input_hash:
        raise _conflict()
    if row["evaluation_as_of"] != record.evaluation_as_of:
        raise _conflict()
    if row["eligible"] != record.eligible:
        raise _conflict()
    for column in ("policy_manifest", "input_payload", "output_payload"):
        if not isinstance(row[column], dict):
            raise _conflict()
    for column in ("rejection_reasons", "unknowns", "rule_outcomes"):
        if not isinstance(row[column], list):
            raise _conflict()
    if row["policy_manifest"] != record.policy_manifest:
        raise _conflict()
    if row["input_payload"] != record.input_payload:
        raise _conflict()
    if row["output_payload"] != record.output_payload:
        raise _conflict()
    if row["policy_manifest"] != manifest_copy():
        raise _conflict()
    _validate_input_payload(row["input_payload"])
    _validate_output_payload(row["output_payload"], row)
    if canonical_json(row["input_payload"])[1] != row["input_hash"]:
        raise _conflict()
    if canonical_json(row["output_payload"])[1] != row["result_hash"]:
        raise _conflict()
    if canonical_json(row["policy_manifest"])[1] != row["policy_manifest_hash"]:
        raise _conflict()
    if canonical_json(row["input_payload"]["mutable_state"])[1] != row["mutable_state_hash"]:
        raise _conflict()
    reasons = _validated_reasons(row["rejection_reasons"])
    unknowns = _validated_unknowns(row["unknowns"])
    staleness = (
        StalenessSource(row["staleness_source"])
        if isinstance(row["staleness_source"], str)
        else None
    )
    if staleness is None:
        raise _conflict()
    outcomes = _validated_outcomes(row["rule_outcomes"])
    if row["eligible"] != (not reasons):
        raise _conflict()
    if reasons != record.rejection_reasons:
        raise _conflict()
    if unknowns != record.unknowns:
        raise _conflict()
    if staleness != record.staleness_source:
        raise _conflict()
    if outcomes != record.rule_outcomes:
        raise _conflict()
    return HardFilterResult(
        evaluation_id=row["id"],
        posting_id=row["posting_id"],
        posting_version_id=row["posting_version_id"],
        policy_version=row["policy_version"],
        policy_manifest_hash=row["policy_manifest_hash"],
        mutable_state_hash=row["mutable_state_hash"],
        input_hash=row["input_hash"],
        result_hash=row["result_hash"],
        evaluation_as_of=row["evaluation_as_of"],
        eligible=row["eligible"],
        rejection_reasons=reasons,
        unknowns=unknowns,
        staleness_source=staleness,
        rule_outcomes=outcomes,
        replayed=True,
    )


def _is_input_hash_race(error: IntegrityError) -> bool:
    return (
        getattr(error.orig, "sqlstate", None) == "23505"
        and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        == "hard_filter_evaluation_input_hash_key"
    )


def _persist_once(engine: Engine, record: EvaluationRecord) -> HardFilterResult:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            _lock_and_recheck(conn, record)
            existing = _load_by_input_hash(conn, record.input_hash)
            if existing is not None:
                # Decision 11 step 9: validate/replay the existing immutable
                # row and its required score projection as one unit.
                result = _validate_persisted(existing, record)
                _validate_score_projection(conn, record)
                transaction.commit()
                return result
            # A new identity. An eligible evaluation must have no score at
            # its key; a rejected evaluation inserts H and S atomically.
            if record.eligible:
                _ensure_no_score(conn, record)
            evaluation_id = _insert_evaluation(conn, record)
            if not record.eligible:
                _insert_score(conn, record)
            transaction.commit()
            return _result_from_record(record, evaluation_id, replayed=False)
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise


def _recover_race(engine: Engine, record: EvaluationRecord) -> HardFilterResult:
    """Authorized unique-race recovery: fresh transaction, complete lock
    sequence, winner validation, replayed=True."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            _lock_and_recheck(conn, record)
            winner = _load_by_input_hash(conn, record.input_hash)
            if winner is None:
                raise _conflict()
            result = _validate_persisted(winner, record)
            _validate_score_projection(conn, record)
            transaction.commit()
            return result
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise


def persist_or_replay(*, engine: Engine, record: EvaluationRecord) -> HardFilterResult:
    """Persist exactly one immutable evaluation, or replay the existing row.

    ``input_hash`` is the Task 008 idempotency identity. The only authorized
    recovery is the unique-insert race on
    ``hard_filter_evaluation_input_hash_key``; every other integrity failure
    stays distinguishable and raises the closed database error.
    """
    _validate_record(record)
    try:
        return _persist_once(engine, record)
    except IntegrityError as exc:
        if _is_input_hash_race(exc):
            return _recover_race(engine, record)
        raise FilterDatabaseError(FilterErrorCode.DATABASE_ERROR) from exc
    except SQLAlchemyError as exc:
        raise FilterDatabaseError(FilterErrorCode.DATABASE_ERROR) from exc

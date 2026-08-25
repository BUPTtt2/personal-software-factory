from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TurnEvidence:
    started_at: datetime
    stopped: bool
    thread_runtime: str
    changes_detected: bool
    verification_statuses: tuple[str, ...]
    awaiting_user: bool
    assistant_claim_summary: str


@dataclass(frozen=True)
class DerivedStatus:
    status: str
    reasons: tuple[str, ...]


def derive_turn_status(
    evidence: TurnEvidence,
    now: datetime,
    stale_after: timedelta,
) -> DerivedStatus:
    if evidence.awaiting_user:
        return DerivedStatus("awaiting_user", ("explicit_user_decision_required",))
    if "failed" in evidence.verification_statuses:
        return DerivedStatus("verification_failed", ("observed_verification_failure",))
    if evidence.changes_detected and "passed" in evidence.verification_statuses:
        return DerivedStatus("verification_passed", ("changes_and_passing_verification_observed",))
    if evidence.changes_detected:
        return DerivedStatus("evidence_incomplete", ("changes_without_passing_verification",))
    if evidence.stopped:
        return DerivedStatus("stopped", ("turn_stop_observed",))
    if now - evidence.started_at >= stale_after:
        if evidence.thread_runtime in {"idle", "notLoaded", "systemError"}:
            return DerivedStatus("interrupted", ("stale_unfinished_turn_inactive",))
        if evidence.thread_runtime == "unknown":
            return DerivedStatus("unknown", ("stale_turn_runtime_unknown",))
    return DerivedStatus("working", ("recent_unfinished_turn",))

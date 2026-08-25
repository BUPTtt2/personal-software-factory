from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observer.action_policy import CURRENT_ACTION_STATUS_SQL, is_current_action_status
from observer.config import ProjectConfig, load_projects
from observer.redact import detect_sensitive_output


_PUBLIC_TEXT_FALLBACK = "受保护的历史摘要"
_PUBLIC_BODY_MARKERS = re.compile(
    r"```|(?:^|\s)(?:def|class|function|const|let|var|import|from|export)\s+"
    r"|(?:system|user|assistant)\s+prompt\b|tool\s+(?:output|result)\b|\benv(?:ironment)?\s*=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    summary: str


@dataclass(frozen=True)
class AgentActivity:
    status: str = "not_run"
    started_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    evidence_event_at: str | None = None


@dataclass(frozen=True)
class WorkSummary:
    stage: str
    active_threads: int
    interrupted_threads: int
    branch: str | None
    head: str | None
    changed_paths: tuple[str, ...]
    verification_status: str
    verification_freshness: str
    verification_observed_at: str | None


@dataclass(frozen=True)
class CurrentActionSummary:
    action_id: str
    intent_version: int
    title: str
    why_now: str
    completion_evidence: str
    actor: str
    status: str
    created_at: str


@dataclass(frozen=True)
class OpenDecisionSummary:
    decision_id: str
    intent_version: int
    question: str
    reason: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    display_name: str
    source: str
    judgement: str
    reason: str
    next_action: str
    can_run_agent: bool
    evidence: tuple[EvidenceItem, ...]
    last_event_at: str | None
    active_run_id: str | None
    work_summary: WorkSummary = WorkSummary(
        "尚无活动", 0, 0, None, None, (), "unknown", "unknown", None,
    )
    activity_status: str = "none"
    agent_activity: AgentActivity = AgentActivity()
    intent_status: str = "missing"
    outcome_summary: str | None = None
    intent_version: int = 0
    current_action: CurrentActionSummary | None = None
    can_confirm_achievement: bool = False
    open_decision: OpenDecisionSummary | None = None
    evidence_cursor: str = ""

    def to_public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("evidence_cursor", None)
        return value


@dataclass(frozen=True)
class _ProjectFacts:
    event_count: int = 0
    last_event_at: str | None = None
    working_threads: int = 0
    interrupted_threads: int = 0
    unknown_threads: int = 0
    stopped_threads: int = 0
    evidence_incomplete_turns: int = 0
    verification_failed_turns: int = 0
    passing_verifications: int = 0
    failing_verifications: int = 0
    verification_status: str = "unknown"
    verification_freshness: str = "unknown"
    verification_observed_at: str | None = None
    changes_detected: bool = False
    git_state: str = "unavailable"
    git_branch: str | None = None
    git_head: str | None = None
    changed_paths: tuple[str, ...] = ()
    active_run_id: str | None = None
    agent_judgement: str | None = None
    agent_reason: str | None = None
    agent_next_action: str | None = None
    latest_run_status: str = "not_run"
    latest_run_started_at: str | None = None
    latest_run_completed_at: str | None = None
    latest_run_error_code: str | None = None
    latest_run_evidence_event_at: str | None = None
    intent_status: str = "missing"
    outcome_summary: str | None = None
    intent_version: int = 0
    current_action: CurrentActionSummary | None = None
    can_confirm_achievement: bool = False
    open_decision: OpenDecisionSummary | None = None
    evidence_cursor: str = ""


def build_overview(
    root: Path,
    now: datetime | None = None,
    *,
    registry_path: Path | None = None,
    db_path: Path | None = None,
) -> tuple[ProjectSnapshot, ...]:
    observed_at = now or datetime.now(timezone.utc)
    registry = registry_path or root / "config/projects.yaml"
    database = db_path or root / "data/factory.sqlite"
    projects = tuple(project for project in load_projects(registry) if project.enabled)
    facts = {project.project_id: _ProjectFacts() for project in projects}
    if database.exists():
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for project in projects:
                    facts[project.project_id] = _read_project_facts(
                        connection, tables, project.project_id, observed_at,
                    )
        except sqlite3.Error:
            facts = {
                project.project_id: replace(facts[project.project_id], intent_status="unknown")
                for project in projects
            }
    return tuple(_snapshot(project, facts[project.project_id], observed_at) for project in projects)


def current_evidence_cursor(connection: sqlite3.Connection, project_id: str) -> str:
    """Read the project evidence watermark inside the caller's transaction."""
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    event_count = 0
    last_event_at = None
    if "events" in tables:
        row = connection.execute(
            "SELECT count(*) AS event_count,max(occurred_at) AS last_event_at "
            "FROM events WHERE project_id=?",
            (project_id,),
        ).fetchone()
        event_count = int(row["event_count"])
        last_event_at = row["last_event_at"]
    return _evidence_cursor(connection, tables, project_id, last_event_at, event_count)


def _read_project_facts(
    connection: sqlite3.Connection,
    tables: set[str],
    project_id: str,
    observed_at: datetime,
) -> _ProjectFacts:
    event_count = 0
    last_event_at = None
    if "events" in tables:
        row = connection.execute(
            "SELECT count(*) AS event_count,max(occurred_at) AS last_event_at "
            "FROM events WHERE project_id=?",
            (project_id,),
        ).fetchone()
        event_count = int(row["event_count"])
        last_event_at = row["last_event_at"]

    thread_counts: dict[str, int] = {}
    latest_thread_id = None
    if "threads" in tables:
        for row in connection.execute(
            "SELECT status,last_seen_at FROM threads WHERE project_id=?",
            (project_id,),
        ):
            status = row["status"] or "unknown"
            if status == "working" and _is_stale(row["last_seen_at"], observed_at):
                status = "unknown"
            thread_counts[status] = thread_counts.get(status, 0) + 1
        latest_thread = connection.execute(
            "SELECT thread_id,status FROM threads WHERE project_id=? "
            "ORDER BY last_seen_at DESC,thread_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if latest_thread:
            latest_thread_id = latest_thread["thread_id"]

    turn_counts: dict[str, int] = {}
    latest_turn_id = None
    if "turns" in tables and latest_thread_id:
        latest_turn = connection.execute(
            "SELECT turn_id,status FROM turns WHERE thread_id=? "
            "ORDER BY COALESCE(started_at,'') DESC,turn_id DESC LIMIT 1",
            (latest_thread_id,),
        ).fetchone()
        if latest_turn:
            latest_turn_id = latest_turn["turn_id"]
            turn_counts[latest_turn["status"]] = 1

    verification_counts: dict[str, int] = {}
    verification_status = "unknown"
    verification_freshness = "unknown"
    verification_observed_at = None
    if "verification_records" in tables and "events" in tables:
        latest_verification = connection.execute(
            "SELECT verification_records.status,verification_records.observed_at "
            "FROM verification_records JOIN events USING(event_id) "
            "WHERE events.project_id=? "
            "ORDER BY (events.event_type='verification_receipt') DESC,"
            "verification_records.observed_at DESC,verification_records.verification_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if latest_verification:
            verification_status = latest_verification["status"]
            verification_observed_at = latest_verification["observed_at"]
            latest_change = connection.execute(
                "SELECT max(occurred_at) FROM events WHERE project_id=? AND event_type='code_change_observed'",
                (project_id,),
            ).fetchone()[0]
            verification_freshness = (
                "stale" if latest_change and latest_change > verification_observed_at else "current"
            )
            if verification_freshness == "current":
                verification_counts[verification_status] = 1

    changes_detected = False
    git_state = "unavailable"
    git_branch = None
    git_head = None
    changed_paths: tuple[str, ...] = ()
    if "git_snapshots" in tables and "events" in tables:
        latest_snapshot = connection.execute(
            "SELECT git_snapshots.changed_paths,git_snapshots.branch,git_snapshots.head_sha "
            "FROM git_snapshots "
            "JOIN events ON events.event_id=git_snapshots.event_id "
            "WHERE events.project_id=? "
            "ORDER BY git_snapshots.captured_at DESC,git_snapshots.snapshot_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if latest_snapshot:
            try:
                changed_paths = tuple(json.loads(latest_snapshot["changed_paths"]))
                changes_detected = bool(changed_paths)
                git_state = "dirty" if changes_detected else "clean"
            except (TypeError, json.JSONDecodeError):
                changes_detected = False
                changed_paths = ()
            git_branch = latest_snapshot["branch"]
            git_head = latest_snapshot["head_sha"]

    active_run_id = None
    agent_judgement = agent_reason = agent_next_action = None
    latest_run_status = "not_run"
    latest_run_started_at = latest_run_completed_at = latest_run_error_code = None
    latest_run_evidence_event_at = None
    evidence_cursor = _evidence_cursor(connection, tables, project_id, last_event_at, event_count)
    if "agent_runs" in tables:
        latest_run = connection.execute(
            "SELECT status,started_at,completed_at,error_code,evidence_event_at FROM agent_runs "
            "WHERE project_id=? ORDER BY created_at DESC,run_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if latest_run:
            latest_run_status = latest_run["status"]
            latest_run_started_at = latest_run["started_at"]
            latest_run_completed_at = latest_run["completed_at"]
            latest_run_error_code = latest_run["error_code"]
            latest_run_evidence_event_at = latest_run["evidence_event_at"]
        active = connection.execute(
            "SELECT run_id FROM agent_runs WHERE project_id=? AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        active_run_id = active["run_id"] if active else None
        completed = connection.execute(
            "SELECT judgement,reason,next_action FROM agent_runs "
            "WHERE project_id=? AND status='completed' AND evidence_event_at=? "
            "ORDER BY completed_at DESC LIMIT 1",
            (project_id, evidence_cursor),
        ).fetchone()
        if completed:
            agent_judgement = _bounded_copy(completed["judgement"], 100)
            agent_reason = _bounded_copy(completed["reason"], 300)
            agent_next_action = _bounded_copy(completed["next_action"], 180)

    intent_status = "missing"
    outcome_summary = None
    intent_version = 0
    current_action = None
    can_confirm_achievement = False
    open_decision = None
    if "project_intents" in tables:
        intent = connection.execute(
            "SELECT version,outcome,provenance FROM project_intents "
            "WHERE project_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if intent:
            intent_version = int(intent["version"])
            outcome_summary = _safe_work_text(intent["outcome"], intent["provenance"], 240)
            intent_status = "active"
            if "action_resolutions" in tables and "action_items" in tables:
                achieved = connection.execute(
                    "SELECT 1 FROM action_items JOIN action_resolutions USING(action_id) "
                    "WHERE action_items.project_id=? AND action_items.intent_version=? "
                    "AND action_items.status='verified' "
                    "AND action_items.completion_evidence='user_confirms_acceptance' "
                    "AND action_resolutions.resolution_kind='user_confirmation' "
                    "AND action_resolutions.decision_id IS NOT NULL LIMIT 1",
                    (project_id, intent_version),
                ).fetchone()
                if achieved:
                    intent_status = "achieved"
            if "action_items" in tables and intent_status == "active":
                action = connection.execute(
                    "SELECT action_id,intent_version,title,why_now,completion_evidence,actor,status,"
                    "created_at,source,provenance FROM action_items WHERE project_id=? AND intent_version=? "
                    f"AND status IN {CURRENT_ACTION_STATUS_SQL} "
                    "ORDER BY updated_at DESC,action_id DESC LIMIT 1",
                    (project_id, intent_version),
                ).fetchone()
                if action:
                    current_action = CurrentActionSummary(
                        action_id=_safe_work_text(action["action_id"], action["provenance"], 128),
                        intent_version=int(action["intent_version"]),
                        title=_safe_work_text(action["title"], action["provenance"], 120),
                        why_now=_safe_work_text(action["why_now"], action["provenance"], 240),
                        completion_evidence=_safe_work_text(
                            action["completion_evidence"], action["provenance"], 64,
                        ),
                        actor=_safe_work_text(action["actor"], action["provenance"], 32),
                        status=_safe_work_text(action["status"], action["provenance"], 32),
                        created_at=_safe_work_text(action["created_at"], action["provenance"], 64),
                    )
                    can_confirm_achievement = is_current_action_status(action["status"]) and (
                        action["actor"] == "user"
                        and action["source"] in {"policy", "user"}
                        and action["provenance"] in {"policy", "local_user"}
                        and action["completion_evidence"] == "user_confirms_acceptance"
                    )
            if "decision_requests" in tables and intent_status == "active":
                decision = connection.execute(
                    "SELECT decision_id,intent_version,question,reason,options,provenance "
                    "FROM decision_requests WHERE project_id=? AND intent_version=? AND status='open' "
                    "ORDER BY created_at DESC,decision_id DESC LIMIT 1",
                    (project_id, intent_version),
                ).fetchone()
                if decision:
                    try:
                        options = tuple(json.loads(decision["options"]))
                    except (TypeError, json.JSONDecodeError):
                        options = ()
                    open_decision = OpenDecisionSummary(
                        decision_id=_safe_work_text(decision["decision_id"], decision["provenance"], 128),
                        intent_version=int(decision["intent_version"]),
                        question=_safe_work_text(decision["question"], decision["provenance"], 240),
                        reason=_safe_work_text(decision["reason"], decision["provenance"], 240),
                        options=tuple(
                            _safe_work_text(item, decision["provenance"], 240)
                            for item in options if isinstance(item, str)
                        )[:3],
                    )

    return _ProjectFacts(
        event_count=event_count,
        last_event_at=last_event_at,
        working_threads=thread_counts.get("working", 0),
        interrupted_threads=thread_counts.get("interrupted", 0),
        unknown_threads=thread_counts.get("unknown", 0),
        stopped_threads=thread_counts.get("stopped", 0) + thread_counts.get("ended", 0),
        evidence_incomplete_turns=turn_counts.get("evidence_incomplete", 0),
        verification_failed_turns=turn_counts.get("verification_failed", 0),
        passing_verifications=verification_counts.get("passed", 0),
        failing_verifications=verification_counts.get("failed", 0),
        verification_status=verification_status,
        verification_freshness=verification_freshness,
        verification_observed_at=verification_observed_at,
        changes_detected=changes_detected,
        git_state=git_state,
        git_branch=git_branch,
        git_head=git_head,
        changed_paths=changed_paths,
        active_run_id=active_run_id,
        agent_judgement=agent_judgement,
        agent_reason=agent_reason,
        agent_next_action=agent_next_action,
        latest_run_status=latest_run_status,
        latest_run_started_at=latest_run_started_at,
        latest_run_completed_at=latest_run_completed_at,
        latest_run_error_code=latest_run_error_code,
        latest_run_evidence_event_at=latest_run_evidence_event_at,
        intent_status=intent_status,
        outcome_summary=outcome_summary,
        intent_version=intent_version,
        current_action=current_action,
        can_confirm_achievement=can_confirm_achievement,
        open_decision=open_decision,
        evidence_cursor=evidence_cursor,
    )


def _snapshot(project: ProjectConfig, facts: _ProjectFacts, now: datetime) -> ProjectSnapshot:
    source = "agent" if facts.agent_judgement else "policy"
    judgement, reason, next_action, can_run_agent = _policy_judgement(facts)
    if facts.agent_judgement:
        judgement = facts.agent_judgement
        reason = facts.agent_reason or reason
        next_action = facts.agent_next_action or next_action
    if facts.active_run_id:
        can_run_agent = False

    observed = (
        f"已采集 {facts.event_count} 个事件，最近事件时间为 {facts.last_event_at}。"
        if facts.event_count and facts.last_event_at
        else "当前没有采集到项目事件。"
    )
    if facts.verification_freshness == "stale":
        label = "通过" if facts.verification_status == "passed" else "失败"
        verified = f"最近一次{label}回执已过期，代码在此后发生了变化。"
    elif facts.failing_verifications:
        verified = f"当前失败，真实退出码证据记录于 {facts.verification_observed_at}。"
    elif facts.passing_verifications:
        verified = f"当前通过，真实退出码证据记录于 {facts.verification_observed_at}。"
    else:
        verified = "没有可证明通过或失败的退出码证据。"
    if facts.changes_detected:
        verified += " 最近一次 Git 快照显示有未收口变更。"
    git_summary = {
        "dirty": "最近一次 Git 快照显示有未收口变更。",
        "clean": "最近一次 Git 快照显示工作区干净。",
        "unavailable": "当前没有 Git 快照，无法确认工作区状态。",
    }[facts.git_state]
    return ProjectSnapshot(
        project_id=project.project_id,
        display_name=project.display_name,
        source=source,
        judgement=judgement,
        reason=reason,
        next_action=next_action,
        can_run_agent=can_run_agent,
        evidence=(
            EvidenceItem("Observer", observed),
            EvidenceItem("Git", git_summary),
            EvidenceItem("Verifier", verified),
        ),
        last_event_at=facts.last_event_at,
        active_run_id=facts.active_run_id,
        work_summary=WorkSummary(
            stage=_stage(facts),
            active_threads=facts.working_threads,
            interrupted_threads=facts.interrupted_threads + facts.unknown_threads,
            branch=facts.git_branch,
            head=facts.git_head[:8] if facts.git_head else None,
            changed_paths=facts.changed_paths[:3],
            verification_status=facts.verification_status,
            verification_freshness=facts.verification_freshness,
            verification_observed_at=facts.verification_observed_at,
        ),
        activity_status=_activity_status(facts.last_event_at, now),
        agent_activity=AgentActivity(
            status=facts.latest_run_status,
            started_at=facts.latest_run_started_at,
            completed_at=facts.latest_run_completed_at,
            error_code=facts.latest_run_error_code,
            evidence_event_at=facts.latest_run_evidence_event_at,
        ),
        intent_status=facts.intent_status,
        outcome_summary=facts.outcome_summary,
        intent_version=facts.intent_version,
        current_action=facts.current_action,
        can_confirm_achievement=facts.can_confirm_achievement,
        open_decision=facts.open_decision,
        evidence_cursor=facts.evidence_cursor,
    )


def _activity_status(last_event_at: str | None, now: datetime) -> str:
    if not last_event_at:
        return "none"
    try:
        observed = datetime.fromisoformat(last_event_at)
    except ValueError:
        return "stale"
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return "recent" if now.astimezone(timezone.utc) - observed.astimezone(timezone.utc) <= timedelta(minutes=30) else "stale"


def _is_stale(value: str | None, now: datetime) -> bool:
    if not value:
        return True
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return True
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) - observed.astimezone(timezone.utc) > timedelta(minutes=30)


def _policy_judgement(facts: _ProjectFacts) -> tuple[str, str, str, bool]:
    if facts.failing_verifications or (facts.verification_failed_turns and not facts.passing_verifications):
        return (
            "验证已经失败，先处理失败证据。",
            "失败证据优先于完成声明，项目不能继续向后推进。",
            "让 Agent 分析失败",
            True,
        )
    if facts.working_threads:
        return (
            "Codex 正在工作，先让当前任务完成。",
            "Observer 仍能看到进行中的线程，此时重复启动主管只会制造上下文冲突。",
            "等待当前任务",
            False,
        )
    if facts.evidence_incomplete_turns:
        return (
            "现在不该继续加功能，先补齐验证闭环。",
            "已经发生代码变化，但缺少可证明结果成立的通过证据。",
            "让 Agent 检查验证缺口",
            True,
        )
    if facts.verification_freshness == "stale":
        return (
            "验证证据已经过期，先重新证明当前代码。",
            "最近一次可信回执之后又发生了代码变化，旧结果不能证明当前现场。",
            "重新运行验证",
            True,
        )
    if facts.changes_detected and not facts.passing_verifications:
        return (
            "现在不该继续加功能，先补齐验证闭环。",
            "最近一次 Git 快照有未收口变更，但没有当前通过证据。",
            "让 Agent 检查验证缺口",
            True,
        )
    if facts.interrupted_threads:
        return (
            "有任务中断，恢复前先重新核验现场。",
            "中断后的 Git、依赖和验证状态可能已经变化，不能继承旧结论。",
            "让 Agent 准备恢复",
            True,
        )
    if facts.unknown_threads:
        return (
            "当前状态无法确定，需要一次只读检查。",
            "Observer 保留了事件，但线程运行状态没有可靠收口。",
            "让 Agent 检查状态",
            True,
        )
    if facts.event_count or facts.stopped_threads:
        return (
            "这一轮已经收口，等待新的项目目标。",
            "当前没有进行中任务，也没有需要立即处理的失败或中断。",
            "让 Agent 复核现状",
            True,
        )
    return (
        "目前没有新事件，不需要打扰你。",
        "Observer 没有发现新的执行、提交或验证活动。",
        "让 Agent 初次检查",
        True,
    )


def _stage(facts: _ProjectFacts) -> str:
    if facts.failing_verifications:
        return "验证失败"
    if facts.working_threads:
        return "工作中"
    if (
        facts.evidence_incomplete_turns
        or facts.changes_detected and not facts.passing_verifications
        or facts.verification_freshness == "stale"
    ):
        return "等待验证"
    if facts.interrupted_threads or facts.unknown_threads:
        return "需要恢复核验"
    return "已收口" if facts.event_count or facts.stopped_threads else "尚无活动"


def _bounded_copy(value: str | None, max_chars: int) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    suffix = "…"
    return value[:max_chars - len(suffix)].rstrip() + suffix


def _safe_work_text(value: str, provenance: str, max_chars: int) -> str:
    if (
        provenance not in {"local_user", "policy"}
        or not isinstance(value, str)
        or not value.strip()
        or len(value) > max_chars
        or "\n" in value
        or "\r" in value
        or detect_sensitive_output(value)
        or _PUBLIC_BODY_MARKERS.search(value)
    ):
        return _PUBLIC_TEXT_FALLBACK
    return value


def _evidence_cursor(
    connection: sqlite3.Connection,
    tables: set[str],
    project_id: str,
    last_event_at: str | None,
    event_count: int,
) -> str:
    state: list[tuple[object, ...]] = []
    if "threads" in tables:
        state.extend(tuple(row) for row in connection.execute(
            "SELECT thread_id,status,last_seen_at FROM threads WHERE project_id=? ORDER BY thread_id",
            (project_id,),
        ))
    if "turns" in tables and "threads" in tables:
        state.extend(tuple(row) for row in connection.execute(
            "SELECT turns.thread_id,turns.turn_id,turns.status,turns.stopped_at FROM turns "
            "JOIN threads ON threads.thread_id=turns.thread_id WHERE threads.project_id=? "
            "ORDER BY turns.thread_id,turns.turn_id",
            (project_id,),
        ))
    payload = json.dumps(
        [last_event_at or "", event_count, state], ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

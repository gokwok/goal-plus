from __future__ import annotations

from datetime import datetime
import json
import time
from pathlib import Path
from typing import Any

from goal_plus.agent_hosts import get_agent_host_adapter
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_observability import collect_codex_transcript_observability
from goal_plus.models import (
    AgentSessionRecord,
    CandidateRecord,
    EvidenceAnnotationTask,
    FrozenSpec,
    GoalPlusLinkedSearch,
    GoalPlusRecord,
    IterationRecord,
    RunRecord,
    SearchPlan,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.runtime import (
    RESULTS_TSV_RELATIVE_PATH,
    load_json,
    utc_timestamp,
    utc_timestamp_from_epoch,
)
from goal_plus.statistics import (
    aggregate_run_statistics,
    aggregate_usage,
    build_run_statistics,
)


def _path_mtime(path: str | None) -> float | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    return candidate.stat().st_mtime


def _path_info(path: str | None) -> dict[str, Any]:
    if not path:
        return {"path": None, "exists": False, "last_updated_at": None, "size_bytes": None}
    candidate = Path(path)
    if not candidate.exists():
        return {"path": path, "exists": False, "last_updated_at": None, "size_bytes": None}
    stat = candidate.stat()
    return {
        "path": path,
        "exists": True,
        "last_updated_at": utc_timestamp_from_epoch(stat.st_mtime),
        "size_bytes": stat.st_size,
    }


def _parse_utc_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _evidence_annotation_payload(run_dir: Path) -> dict[str, Any]:
    tasks: list[EvidenceAnnotationTask] = []
    for path in sorted(
        run_dir.glob("candidates/*/evidence-annotations/iteration-*.json")
    ):
        try:
            tasks.append(EvidenceAnnotationTask.model_validate(load_json(path)))
        except (OSError, ValueError):
            continue
    states: dict[str, int] = {}
    for task in tasks:
        states[task.state] = states.get(task.state, 0) + 1

    attempts: list[dict[str, Any]] = []
    for path in sorted((run_dir / "evidence-annotator" / "attempts").glob("*.json")):
        try:
            monitor = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(monitor, dict):
            continue
        attempts.append(
            {
                key: monitor.get(key)
                for key in (
                    "candidate_id",
                    "iteration",
                    "attempt",
                    "host",
                    "model",
                    "reasoning_effort",
                    "timeout_seconds",
                    "state",
                    "started_at",
                    "updated_at",
                    "elapsed_seconds",
                    "pid",
                    "process_returncode",
                    "stdout_bytes",
                    "stderr_bytes",
                    "json_lines",
                    "non_json_lines",
                    "event_type_counts",
                    "assistant_event_type_counts",
                    "last_events",
                    "detail",
                )
                if monitor.get(key) is not None
            }
            | {"monitor_path": str(path)}
        )
    attempts.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("candidate_id") or ""),
            int(item.get("iteration") or 0),
            int(item.get("attempt") or 0),
        )
    )
    active_states = {"starting", "running", "process_exited"}
    active = [item for item in attempts if item.get("state") in active_states]
    recent = attempts[-8:]
    visible = active + [item for item in recent if item not in active]
    return {
        "tasks": len(tasks),
        "attempts": sum(task.attempts for task in tasks),
        "views_published": sum(task.view is not None for task in tasks),
        "states": dict(sorted(states.items())),
        "active_attempts": active,
        "recent_attempts": visible,
        "monitor_files": len(attempts),
    }


def _goal_metric_contexts(
    root_dir: Path,
    record: GoalPlusRecord | None,
) -> dict[int, dict[str, Any]]:
    """Recover revision-scoped baseline/target data from the append-only event log."""
    if record is None:
        return {}
    contexts: dict[int, dict[str, Any]] = {}
    current_revision = 1
    events_path = root_dir / "goal-plus" / record.goal_plus_id / "events.jsonl"
    try:
        stream = events_path.open("r", encoding="utf-8")
    except OSError:
        stream = None
    if stream is not None:
        with stream:
            for line in stream:
                try:
                    event = _load_json_line(line)
                except ValueError:
                    continue
                payload = event.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                event_type = event.get("event_type")
                if event_type in {"created", "goal_updated"}:
                    revision = payload.get("goal_revision")
                    if isinstance(revision, int):
                        current_revision = revision
                elif event_type == "spec_draft_saved":
                    contexts[current_revision] = payload
    if record.spec_draft is not None:
        contexts[record.goal_revision] = record.spec_draft.model_dump(mode="json")
    return contexts


def _load_json_line(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload


def _metric_context(
    frozen: FrozenSpec,
    task: GoalPlusLinkedSearch | None,
    goal_metric_contexts: dict[int, dict[str, Any]],
) -> tuple[float | None, float | None]:
    draft = goal_metric_contexts.get(task.goal_revision) if task is not None else None
    baseline = None
    target = None
    if isinstance(draft, dict):
        baseline_payload = draft.get("baseline")
        metric_payload = draft.get("metric")
        correctness = draft.get("correctness_gate")
        if isinstance(baseline_payload, dict):
            baseline = _number(baseline_payload.get(frozen.spec.metric_name))
        if isinstance(metric_payload, dict):
            target = _number(metric_payload.get("target"))
        if target is None and isinstance(correctness, dict):
            target = _number(correctness.get("score_threshold"))
    if target is None:
        target = _number(frozen.spec.constraints.get("success_threshold"))
    return baseline, target


def _latest_mtime(paths: list[str | None]) -> float | None:
    values = [_path_mtime(path) for path in paths]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _latest_run_id(root_dir: Path) -> str | None:
    run_paths = sorted(
        root_dir.glob("runs/*/run.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not run_paths:
        return None
    return RunRecord.model_validate(load_json(run_paths[0])).run_id


def _run_dir(root_dir: Path, run_id: str) -> Path:
    path = root_dir / "runs" / run_id
    if not path.exists():
        raise FileNotFoundError(f"search run not found: {run_id}")
    return path


def _load_goal_record(root_dir: Path, goal_plus_id: str) -> GoalPlusRecord:
    path = root_dir / "goal-plus" / goal_plus_id / "goal.json"
    if not path.exists():
        raise FileNotFoundError(f"goal-plus record not found: {goal_plus_id}")
    return FileGoalPlusRuntime(root_dir).status(goal_plus_id)


def _load_candidates(run_dir: Path) -> list[CandidateRecord]:
    candidate_dir = run_dir / "candidates"
    if not candidate_dir.exists():
        return []
    return [
        CandidateRecord.model_validate(load_json(path))
        for path in sorted(candidate_dir.glob("*/candidate.json"))
    ]


def _load_agent_sessions(run_dir: Path) -> list[AgentSessionRecord]:
    session_dir = run_dir / "agent_sessions"
    if not session_dir.exists():
        return []
    return [
        AgentSessionRecord.model_validate(load_json(path))
        for path in sorted(session_dir.glob("agent_*.json"))
    ]


def _time_advisory_evidence(
    root: Path,
    session: AgentSessionRecord,
) -> dict[str, Any] | None:
    metadata = session.host_handle.metadata
    pi_advisory = metadata.get("time_advisory")
    if isinstance(pi_advisory, dict):
        return pi_advisory
    if session.host != "codex":
        return None
    path = (
        root
        / "host-logs"
        / "codex-time-advisory"
        / "sent"
        / f"{session.agent_session_id}.json"
    )
    try:
        payload = load_json(path)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_plans(run_dir: Path) -> list[SearchPlan]:
    return [
        SearchPlan.model_validate(load_json(path))
        for path in sorted((run_dir / "plans").glob("plan_*.json"))
    ]


_STRATEGY_STATE_KEYS = (
    "generation",
    "generation_index",
    "population",
    "population_size",
    "tree_depth",
    "max_tree_depth",
    "frontier_node",
    "sampling_mode",
    "parent_candidate_id",
    "archive_candidate_ids",
    "inspiration_candidate_ids",
    "selected_worker_agent_type",
    "seed",
    "external_ref",
)


def _strategy_payload(
    frozen: FrozenSpec,
    latest_plan: SearchPlan | None,
) -> dict[str, Any]:
    strategy = frozen.spec.strategy
    latest_plan_payload: dict[str, Any] | None = None
    if latest_plan is not None:
        trace = latest_plan.strategy_trace
        latest_plan_payload = {
            "plan_id": latest_plan.plan_id,
            "status": latest_plan.status,
            "requested_k": latest_plan.requested_k,
            "planned_k": latest_plan.planned_k,
            "started_candidate_ids": latest_plan.started_candidate_ids,
            "selection_rule": trace.get("selection_rule"),
            "state": {
                key: trace[key]
                for key in _STRATEGY_STATE_KEYS
                if key in trace
            },
        }
    return {
        "name": strategy.name,
        "orchestration_mode": strategy.orchestration_mode,
        "worker_host": strategy.worker_host,
        "worker_agent_type": strategy.worker_agent_type,
        "latest_plan": latest_plan_payload,
    }


def _best_iteration(
    candidate: CandidateRecord,
    metric_direction: str,
) -> IterationRecord | None:
    scored = [
        iteration
        for iteration in candidate.iterations
        if iteration.process_passed is True
        and iteration.score is not None
        and not iteration.touched_denied_files
        and not iteration.changed_outside_allowed
    ]
    if not scored:
        return None
    reverse = metric_direction == "maximize"
    return sorted(scored, key=lambda iteration: iteration.score, reverse=reverse)[0]


def _goal_payload(record: GoalPlusRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    linked = record.linked_search.model_dump(mode="json") if record.linked_search else None
    next_action = record.next_action.model_dump(mode="json") if record.next_action else None
    latest_final_check = (
        record.final_checks[-1].model_dump(mode="json") if record.final_checks else None
    )
    return {
        "goal_plus_id": record.goal_plus_id,
        "status": record.status,
        "phase": record.phase,
        "raw_goal": record.raw_goal,
        "goal_revision": record.goal_revision,
        "goal_revisions_total": len(record.goal_revisions),
        "final_check_policy": record.policy.get("final_check", {"mode": "disabled"}),
        "latest_final_check": latest_final_check,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "search_tasks_total": len(record.search_tasks),
        "current_search_run_id": linked.get("run_id") if linked else None,
        "linked_search": linked,
        "next_action": next_action,
        "hook_counters": record.hook_counters,
    }


def _observability_cost(observability: dict[str, Any]) -> float:
    usage = observability.get("usage")
    value = usage.get("cost_usd") if isinstance(usage, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


def _agent_observability(
    session: AgentSessionRecord,
    cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = (session.run_id, session.agent_session_id)
    if cache is not None and key in cache:
        return cache[key]
    try:
        payload = get_agent_host_adapter(session.host).collect_observability(session)
    except Exception as exc:
        payload = {
            "schema_version": 2,
            "agent_session_id": session.agent_session_id,
            "run_id": session.run_id,
            "candidate_id": session.candidate_id,
            "host": session.host,
            "source": "collection_failed",
            "identity": {
                "native_session_id": None,
                "external_id": session.host_handle.external_id,
                "task_name": session.host_handle.task_name,
                "nickname": session.host_handle.nickname,
            },
            "execution": {
                "duration_seconds": None,
                "terminal_state": "unknown",
                "timed_out": bool(session.host_handle.metadata.get("timed_out")),
                "runner_failed": bool(session.host_handle.metadata.get("runner_failed")),
            },
            "usage": {"cost_usd": None},
            "context": {
                "tokens": None,
                "context_window": None,
                "percent": None,
                "source": "unknown",
            },
            "artifacts": {
                "event_log": session.host_handle.metadata.get("event_log"),
                "text_log": session.host_handle.metadata.get("text_log"),
                "session_file": session.host_handle.metadata.get("session_file"),
            },
            "handoff": {"present": False, "source_path": None, "error": None},
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    if cache is not None:
        cache[key] = payload
    return payload


def _context_payload(metrics: dict[str, Any]) -> dict[str, Any]:
    session_stats = metrics.get("session_stats")
    context = session_stats.get("contextUsage") if isinstance(session_stats, dict) else None
    if isinstance(context, dict):
        return {
            "tokens": context.get("tokens"),
            "context_window": context.get("contextWindow"),
            "percent": context.get("percent"),
            "source": "pi_session_stats",
        }
    tokens = session_stats.get("tokens") if isinstance(session_stats, dict) else None
    if isinstance(tokens, dict):
        return {
            "tokens": tokens.get("total"),
            "context_window": None,
            "percent": None,
            "source": "pi_session_tokens_total",
        }
    return {"tokens": None, "context_window": None, "percent": None, "source": "unknown"}


def _session_liveness(
    candidate: CandidateRecord | None,
    *,
    latest_output_mtime: float | None,
    now: float,
    stale_after_seconds: int,
    timed_out: bool,
    runner_failed: bool,
    terminal_state: str = "unknown",
) -> str:
    if candidate and candidate.status == "failed":
        return "failed"
    if runner_failed:
        return "failed"
    if candidate and candidate.status == "evaluated":
        return "evaluated"
    if timed_out:
        return "timed_out"
    if terminal_state in {"completed", "interrupted"}:
        return terminal_state
    if latest_output_mtime is not None and now - latest_output_mtime > stale_after_seconds:
        return "stale"
    return "running_or_waiting"


def _search_task_payload(
    root_dir: Path,
    task: GoalPlusLinkedSearch,
    *,
    current_run_id: str | None,
    goal_metric_contexts: dict[int, dict[str, Any]],
    now_epoch: float,
    observability_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = task.model_dump(mode="json")
    run_id = task.run_id
    payload.update(
        {
            "is_current": run_id is not None and run_id == current_run_id,
            "run_exists": False,
            "state": None,
            "run_frozen_spec_id": None,
            "frozen_spec_exists": False,
            "strategy": None,
            "planning_rounds_total": 0,
            "started_rounds_total": 0,
            "candidates_total": 0,
            "candidates_evaluated": 0,
            "worker_sessions_total": 0,
            "verifier_runs_total": 0,
            "evidence_annotations": None,
            "estimated_cost_total": 0.0,
            "statistics": None,
        }
    )
    if run_id is None:
        return payload
    run_dir = root_dir / "runs" / run_id
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return payload

    run = RunRecord.model_validate(load_json(run_path))
    frozen_path = root_dir / "specs" / run.frozen_spec_id / "frozen_spec.json"
    frozen = FrozenSpec.model_validate(load_json(frozen_path)) if frozen_path.exists() else None
    plans = _load_plans(run_dir)
    candidates = _load_candidates(run_dir)
    sessions = _load_agent_sessions(run_dir)
    observations = {
        session.agent_session_id: _agent_observability(session, observability_cache)
        for session in sessions
    }
    estimated_cost_total = sum(
        _observability_cost(observation) for observation in observations.values()
    )
    baseline_score = None
    target_score = None
    statistics = None
    if frozen is not None:
        baseline_score, target_score = _metric_context(
            frozen,
            task,
            goal_metric_contexts,
        )
        statistics = build_run_statistics(
            run,
            frozen,
            candidates,
            sessions,
            observations,
            baseline_score=baseline_score,
            target_score=target_score,
            now_epoch=now_epoch,
        )
    payload.update(
        {
            "run_exists": True,
            "state": run.state,
            "run_frozen_spec_id": run.frozen_spec_id,
            "frozen_spec_exists": frozen is not None,
            "strategy": (
                {
                    "name": frozen.spec.strategy.name,
                    "worker_host": frozen.spec.strategy.worker_host,
                }
                if frozen is not None
                else None
            ),
            "planning_rounds_total": len(plans),
            "started_rounds_total": sum(1 for plan in plans if plan.status == "started"),
            "candidates_total": len(candidates),
            "candidates_evaluated": sum(
                1 for candidate in candidates if candidate.status == "evaluated"
            ),
            "worker_sessions_total": len(sessions),
            "verifier_runs_total": sum(len(candidate.iterations) for candidate in candidates),
            "evidence_annotations": _evidence_annotation_payload(run_dir),
            "estimated_cost_total": estimated_cost_total,
            "statistics": statistics,
        }
    )
    return payload


def _search_task_aggregate(search_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "search_tasks_total": len(search_tasks),
        "planning_rounds_total": sum(task["planning_rounds_total"] for task in search_tasks),
        "started_rounds_total": sum(task["started_rounds_total"] for task in search_tasks),
        "candidates_total": sum(task["candidates_total"] for task in search_tasks),
        "candidates_evaluated": sum(task["candidates_evaluated"] for task in search_tasks),
        "worker_sessions_total": sum(task["worker_sessions_total"] for task in search_tasks),
        "verifier_runs_total": sum(task["verifier_runs_total"] for task in search_tasks),
        "estimated_cost_total": sum(task["estimated_cost_total"] for task in search_tasks),
        "statistics": aggregate_run_statistics(
            [task.get("statistics") for task in search_tasks]
        ),
    }


def goal_plus_monitor_snapshot(
    root_dir: Path | str = DEFAULT_RUNTIME_ROOT,
    *,
    goal_plus_id: str | None = None,
    run_id: str | None = None,
    stale_after_seconds: int = 600,
) -> dict[str, Any]:
    """Read-only monitoring snapshot for Goal Plus/Search runs.

    This function intentionally does not wait for workers, inspect live
    processes, or mutate runtime state. It summarizes durable `.gp` and
    host-handle evidence so a monitoring agent can poll cheaply.
    """

    root = Path(root_dir).resolve()
    now = time.time()
    warnings: list[dict[str, Any]] = []
    goal_record: GoalPlusRecord | None = None
    if goal_plus_id:
        goal_record = _load_goal_record(root, goal_plus_id)
        linked = goal_record.linked_search
        if run_id is None and linked and linked.run_id:
            run_id = linked.run_id

    if run_id is None and goal_plus_id is None:
        run_id = _latest_run_id(root)
        if run_id:
            warnings.append({"kind": "inferred_latest_run", "run_id": run_id})
    elif run_id is None and goal_record is not None:
        warnings.append(
            {
                "kind": "goal_without_linked_search",
                "goal_plus_id": goal_record.goal_plus_id,
            }
        )

    current_search_run_id = (
        goal_record.linked_search.run_id
        if goal_record is not None and goal_record.linked_search is not None
        else None
    )
    task_links = list(goal_record.search_tasks) if goal_record is not None else []
    if goal_record is None and run_id is not None:
        run_record = RunRecord.model_validate(load_json(_run_dir(root, run_id) / "run.json"))
        task_links = [
            GoalPlusLinkedSearch(
                frozen_spec_id=run_record.frozen_spec_id,
                run_id=run_record.run_id,
                linked_at=run_record.created_at,
            )
        ]
        current_search_run_id = run_id

    linked_run_ids = {task.run_id for task in task_links if task.run_id is not None}
    if goal_record is not None and run_id is not None and run_id not in linked_run_ids:
        warnings.append(
            {
                "kind": "run_not_linked_to_goal",
                "goal_plus_id": goal_record.goal_plus_id,
                "run_id": run_id,
            }
        )

    observability_cache: dict[tuple[str, str], dict[str, Any]] = {}
    goal_metric_contexts = _goal_metric_contexts(root, goal_record)
    search_tasks = [
        _search_task_payload(
            root,
            task,
            current_run_id=current_search_run_id,
            goal_metric_contexts=goal_metric_contexts,
            now_epoch=now,
            observability_cache=observability_cache,
        )
        for task in task_links
    ]
    search_task_aggregate = _search_task_aggregate(search_tasks)
    terminal_run_states = {"promoted", "aborted", "failed"}
    for task in search_tasks:
        task_run_id = task.get("run_id")
        if not task["run_exists"]:
            warnings.append(
                {
                    "kind": "linked_search_run_missing",
                    "goal_plus_id": goal_record.goal_plus_id if goal_record else None,
                    "run_id": task_run_id,
                }
            )
            continue
        if task.get("frozen_spec_id") != task.get("run_frozen_spec_id"):
            warnings.append(
                {
                    "kind": "linked_search_spec_mismatch",
                    "run_id": task_run_id,
                    "linked_frozen_spec_id": task.get("frozen_spec_id"),
                    "run_frozen_spec_id": task.get("run_frozen_spec_id"),
                }
            )
        if not task["frozen_spec_exists"]:
            warnings.append(
                {
                    "kind": "linked_search_frozen_spec_missing",
                    "run_id": task_run_id,
                    "frozen_spec_id": task.get("run_frozen_spec_id"),
                }
            )
        if not task["is_current"] and task["state"] not in terminal_run_states:
            warnings.append(
                {
                    "kind": "superseded_search_task_not_terminal",
                    "run_id": task_run_id,
                    "state": task["state"],
                }
            )
    current_task = next((task for task in search_tasks if task["is_current"]), None)
    if (
        goal_record is not None
        and goal_record.status == "complete"
        and current_task is not None
        and current_task["state"] != "promoted"
    ):
        warnings.append(
            {
                "kind": "completed_goal_current_search_not_promoted",
                "goal_plus_id": goal_record.goal_plus_id,
                "run_id": current_task.get("run_id"),
                "state": current_task.get("state"),
            }
        )

    run_payload: dict[str, Any] | None = None
    strategy_payload: dict[str, Any] | None = None
    candidates_payload: dict[str, dict[str, Any]] = {}
    subagents: list[dict[str, Any]] = []
    selected_run_statistics: dict[str, Any] | None = None
    main_agent = {
        "elapsed_seconds": None,
        "run_age_seconds": None,
        "observed_duration_seconds": None,
        "estimated_cost_total": 0.0,
        "subagent_count": 0,
        "worker_dispatch_count": 0,
        "verifier_count": 0,
        "context_tokens_max": None,
        "context_percent_max": None,
    }

    selected_run_exists = run_id is not None and (root / "runs" / run_id / "run.json").exists()
    if run_id is not None and selected_run_exists:
        run_path = _run_dir(root, run_id)
        run = RunRecord.model_validate(load_json(run_path / "run.json"))
        frozen = FrozenSpec.model_validate(
            load_json(root / "specs" / run.frozen_spec_id / "frozen_spec.json")
        )
        candidates = _load_candidates(run_path)
        sessions = _load_agent_sessions(run_path)
        plans = _load_plans(run_path)
        plans_count = len(plans)
        started_rounds_total = sum(1 for plan in plans if plan.status == "started")
        latest_plan = plans[-1] if plans else None
        strategy_payload = _strategy_payload(frozen, latest_plan)
        by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
        sessions_by_candidate: dict[str, list[AgentSessionRecord]] = {}
        for session in sessions:
            sessions_by_candidate.setdefault(session.candidate_id, []).append(session)

        created_at_epoch = _parse_utc_timestamp(run.created_at)
        if created_at_epoch is not None:
            main_agent["run_age_seconds"] = max(0.0, now - created_at_epoch)

        observations = {
            session.agent_session_id: _agent_observability(
                session, observability_cache
            )
            for session in sessions
        }
        selected_task_link = next(
            (task for task in task_links if task.run_id == run.run_id),
            None,
        )
        baseline_score, target_score = _metric_context(
            frozen,
            selected_task_link,
            goal_metric_contexts,
        )
        selected_run_statistics = build_run_statistics(
            run,
            frozen,
            candidates,
            sessions,
            observations,
            baseline_score=baseline_score,
            target_score=target_score,
            now_epoch=now,
        )
        observed_duration = selected_run_statistics["run"][
            "observed_duration_seconds"
        ]
        main_agent["elapsed_seconds"] = observed_duration
        main_agent["observed_duration_seconds"] = observed_duration

        run_payload = {
            "run_id": run.run_id,
            "state": run.state,
            "frozen_spec_id": run.frozen_spec_id,
            "created_at": run.created_at,
            "source_path": run.source_path,
            "plans_count": plans_count,
            "planning_rounds_total": plans_count,
            "started_rounds_total": started_rounds_total,
            "candidates_total": len(candidates),
            "candidates_evaluated": sum(1 for candidate in candidates if candidate.status == "evaluated"),
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
            "selected_candidate_id": run.selected_candidate_id,
            "selected_score": run.selected_score,
            "selected_iteration": run.selected_iteration,
            "baseline_artifact_ref": (
                run.baseline_artifact_ref.model_dump(mode="json")
                if run.baseline_artifact_ref is not None
                else None
            ),
            "selected_artifact_ref": (
                run.selected_artifact_ref.model_dump(mode="json")
                if run.selected_artifact_ref is not None
                else None
            ),
            "selected_git_head": run.selected_git_head,
            "publication": (
                run.publication.model_dump(mode="json")
                if run.publication is not None
                else None
            ),
            "fs_requests": {
                "total": len(run.fs_requests),
                "state_counts": {
                    state: sum(1 for item in run.fs_requests if item.state == state)
                    for state in sorted({item.state for item in run.fs_requests})
                },
            },
            "fs_cleanup": run.fs_cleanup,
            "budget_used": run.budget_used,
            "evidence_annotations": _evidence_annotation_payload(run_path),
        }

        for candidate in candidates:
            candidate_sessions = sessions_by_candidate.get(candidate.candidate_id, [])
            last_iteration = candidate.iterations[-1] if candidate.iterations else None
            best_iteration = _best_iteration(candidate, frozen.spec.metric_direction)
            results_path = (
                candidate.task.workspace / RESULTS_TSV_RELATIVE_PATH
                if candidate.task.workspace is not None
                else None
            )
            results_tsv = _path_info(
                str(results_path) if results_path is not None else None
            )
            results_tsv["row_count"] = len(candidate.results_ledger)
            results_tsv["storage"] = (
                "workspace_file" if results_path is not None else "durable_record"
            )
            shared_status_counts: dict[str, int] = {}
            toolization_outcome_counts: dict[str, int] = {}
            toolization_signal_counts: dict[str, int] = {}
            toolization_exclusion_counts: dict[str, int] = {}
            toolization_advisory_counts: dict[str, int] = {}
            for item in candidate.iterations:
                status = item.shared_tool_publish_status
                shared_status_counts[status] = shared_status_counts.get(status, 0) + 1
                decision = item.toolization_decision
                if decision is not None:
                    toolization_outcome_counts[decision.outcome] = (
                        toolization_outcome_counts.get(decision.outcome, 0) + 1
                    )
                    for signal in decision.signals:
                        toolization_signal_counts[signal] = (
                            toolization_signal_counts.get(signal, 0) + 1
                        )
                    if decision.exclusion is not None:
                        toolization_exclusion_counts[decision.exclusion] = (
                            toolization_exclusion_counts.get(decision.exclusion, 0) + 1
                        )
                for advisory in item.toolization_advisories:
                    toolization_advisory_counts[advisory] = (
                        toolization_advisory_counts.get(advisory, 0) + 1
                    )
            candidates_payload[candidate.candidate_id] = {
                "candidate_id": candidate.candidate_id,
                "status": candidate.status,
                "plan_id": candidate.task.plan_id,
                "parent_id": candidate.task.parent_id,
                "parent_candidate_ids": candidate.task.parent_candidate_ids,
                "base_candidate_id": candidate.task.base_candidate_id,
                "agent_session_count": len(candidate_sessions),
                "process_dispatch_count": sum(
                    int(session.host_handle.metadata.get("dispatch_count") or 1)
                    for session in candidate_sessions
                ),
                "verifier_count": len(candidate.iterations),
                "last_score": last_iteration.score if last_iteration else None,
                "last_disposition": (
                    last_iteration.disposition if last_iteration else None
                ),
                "last_verifier_at": last_iteration.created_at if last_iteration else None,
                "last_git_head": last_iteration.git_head if last_iteration else None,
                "last_artifact_ref": (
                    last_iteration.attempt_ref.model_dump(mode="json")
                    if last_iteration and last_iteration.attempt_ref is not None
                    else None
                ),
                "best_iteration": best_iteration.iteration if best_iteration else None,
                "best_iteration_score": best_iteration.score if best_iteration else None,
                "best_iteration_at": best_iteration.created_at if best_iteration else None,
                "best_iteration_git_head": best_iteration.git_head if best_iteration else None,
                "best_iteration_artifact_ref": (
                    best_iteration.attempt_ref.model_dump(mode="json")
                    if best_iteration and best_iteration.attempt_ref is not None
                    else None
                ),
                "settled_artifact_ref": (
                    candidate.settled_artifact_ref.model_dump(mode="json")
                    if candidate.settled_artifact_ref is not None
                    else None
                ),
                "workspace_git_head_after_settlement": (
                    last_iteration.workspace_git_head_after_settlement
                    if last_iteration
                    else None
                ),
                "changed_files": candidate.detected_changed_files,
                "touched_denied_files": candidate.touched_denied_files,
                "changed_outside_allowed": candidate.changed_outside_allowed,
                "shared_tools_published_total": sum(
                    len(item.shared_tools) for item in candidate.iterations
                ),
                "shared_tool_staged_entries_last": (
                    list(last_iteration.shared_tool_staged_entries)
                    if last_iteration
                    else []
                ),
                "shared_tool_staged_file_count_last": (
                    last_iteration.shared_tool_staged_file_count
                    if last_iteration
                    else 0
                ),
                "shared_tool_publish_status_last": (
                    last_iteration.shared_tool_publish_status
                    if last_iteration
                    else None
                ),
                "shared_tool_publish_status_counts": shared_status_counts,
                "toolization_decision_last": (
                    last_iteration.toolization_decision.model_dump(mode="json")
                    if last_iteration and last_iteration.toolization_decision
                    else None
                ),
                "toolization_advisories_last": (
                    list(last_iteration.toolization_advisories)
                    if last_iteration
                    else []
                ),
                "toolization_outcome_counts": toolization_outcome_counts,
                "toolization_signal_counts": toolization_signal_counts,
                "toolization_exclusion_counts": toolization_exclusion_counts,
                "toolization_advisory_counts": toolization_advisory_counts,
                "results_tsv": results_tsv,
            }
            if not candidate_sessions and candidate.status == "created":
                warnings.append(
                    {
                        "kind": "candidate_without_agent_session",
                        "candidate_id": candidate.candidate_id,
                    }
                )

        for session in sessions:
            candidate = by_candidate.get(session.candidate_id)
            metadata = session.host_handle.metadata
            time_advisory = _time_advisory_evidence(root, session)
            observability = observations[session.agent_session_id]
            execution = (
                observability.get("execution")
                if isinstance(observability.get("execution"), dict)
                else {}
            )
            artifacts = (
                observability.get("artifacts")
                if isinstance(observability.get("artifacts"), dict)
                else {}
            )
            metrics = metadata.get("pi_metrics") if isinstance(metadata.get("pi_metrics"), dict) else {}
            usage_delta = metrics.get("usage_delta") if isinstance(metrics, dict) else None
            usage_total = metrics.get("usage_total") if isinstance(metrics, dict) else None
            context = (
                observability.get("context")
                if isinstance(observability.get("context"), dict)
                else _context_payload(metrics if isinstance(metrics, dict) else {})
            )
            event_log = artifacts.get("event_log") if isinstance(artifacts.get("event_log"), str) else None
            text_log = artifacts.get("text_log") if isinstance(artifacts.get("text_log"), str) else None
            session_file = artifacts.get("session_file") if isinstance(artifacts.get("session_file"), str) else None
            latest_output_mtime = _latest_mtime([event_log, text_log, session_file])
            timed_out = bool(execution.get("timed_out"))
            runner_failed = bool(execution.get("runner_failed"))
            terminal_state = str(execution.get("terminal_state") or "unknown")
            session_iterations = [
                iteration
                for iteration in (candidate.iterations if candidate else [])
                if iteration.agent_session_id == session.agent_session_id
            ]
            raw_dispatches = metadata.get("dispatches")
            dispatches = raw_dispatches if isinstance(raw_dispatches, list) else []
            dispatch_count = int(metadata.get("dispatch_count") or len(dispatches) or 1)
            liveness = _session_liveness(
                candidate,
                latest_output_mtime=latest_output_mtime,
                now=now,
                stale_after_seconds=stale_after_seconds,
                timed_out=timed_out,
                runner_failed=runner_failed,
                terminal_state=terminal_state,
            )
            main_agent["estimated_cost_total"] = float(
                main_agent["estimated_cost_total"]
            ) + _observability_cost(observability)
            if isinstance(context.get("tokens"), int | float):
                current = main_agent["context_tokens_max"]
                main_agent["context_tokens_max"] = max(current or 0, context["tokens"])  # type: ignore[arg-type]
            if isinstance(context.get("percent"), int | float):
                current_percent = main_agent["context_percent_max"]
                main_agent["context_percent_max"] = max(current_percent or 0.0, context["percent"])  # type: ignore[arg-type]
            if timed_out:
                warnings.append(
                    {
                        "kind": "subagent_timed_out",
                        "agent_session_id": session.agent_session_id,
                        "candidate_id": session.candidate_id,
                    }
                )
            if runner_failed:
                warnings.append(
                    {
                        "kind": "subagent_runner_failed",
                        "agent_session_id": session.agent_session_id,
                        "candidate_id": session.candidate_id,
                        "failure_stage": metadata.get("failure_stage"),
                        "error_type": metadata.get("error_type"),
                        "error": metadata.get("error"),
                    }
                )
            if liveness == "stale":
                warnings.append(
                    {
                        "kind": "subagent_stale",
                        "agent_session_id": session.agent_session_id,
                        "candidate_id": session.candidate_id,
                    }
                )
            subagents.append(
                {
                    "agent_session_id": session.agent_session_id,
                    "candidate_id": session.candidate_id,
                    "host": session.host,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "attempt_count": dispatch_count,
                    "dispatch_count": dispatch_count,
                    "dispatches": dispatches,
                    "verifier_count": len(session_iterations),
                    "session_verifier_count": len(session_iterations),
                    "candidate_verifier_count": len(candidate.iterations) if candidate else 0,
                    "last_score": candidate.iterations[-1].score if candidate and candidate.iterations else None,
                    "last_verifier_at": (
                        candidate.iterations[-1].created_at if candidate and candidate.iterations else None
                    ),
                    "duration_seconds": execution.get("duration_seconds"),
                    "usage_delta": usage_delta if isinstance(usage_delta, dict) else None,
                    "usage_total": usage_total if isinstance(usage_total, dict) else None,
                    "context": context,
                    "event_log": _path_info(event_log),
                    "text_log": _path_info(text_log),
                    "session_file": _path_info(session_file),
                    "timed_out": timed_out,
                    "runner_failed": runner_failed,
                    "progress_handoff": (
                        metadata.get("progress_handoff")
                        if isinstance(metadata.get("progress_handoff"), dict)
                        else None
                    ),
                    "failure_stage": metadata.get("failure_stage"),
                    "error_type": metadata.get("error_type"),
                    "error": metadata.get("error"),
                    "soft_closeout_seconds": metadata.get("soft_closeout_seconds"),
                    "soft_closeout_sent": bool(metadata.get("soft_closeout_sent")),
                    "time_advisory_sent": time_advisory is not None,
                    "time_advisory": time_advisory,
                    "raw_logging": bool(metadata.get("raw_logging")),
                    "liveness": liveness,
                    "observability": observability,
                }
            )

        main_agent["subagent_count"] = len(sessions)
        main_agent["worker_dispatch_count"] = sum(
            int(session.host_handle.metadata.get("dispatch_count") or 1)
            for session in sessions
        )
        main_agent["verifier_count"] = sum(len(candidate.iterations) for candidate in candidates)

    orchestrator_observability: dict[str, Any] | None = None
    if (
        goal_record is not None
        and goal_record.active_session is not None
        and goal_record.active_session.host == "codex"
        and goal_record.active_session.transcript_path
    ):
        transcript_path = Path(goal_record.active_session.transcript_path).expanduser()
        if transcript_path.is_file():
            orchestrator_observability = collect_codex_transcript_observability(
                transcript_path,
                since=goal_record.created_at,
            )
        else:
            warnings.append(
                {
                    "kind": "orchestrator_transcript_missing",
                    "path": str(transcript_path),
                }
            )

    usage_sources: list[dict[str, Any] | None] = []
    if selected_run_statistics is not None:
        usage_sources.append(selected_run_statistics.get("usage"))
    if orchestrator_observability is not None:
        usage_sources.append(orchestrator_observability.get("usage"))
    total_usage = aggregate_usage(usage_sources, scope="goal_plus_total")
    unavailable_metrics = [
        "orchestrator_cost_usd",
        "hardware_utilization",
        "semantic_candidate_coverage",
        "redundant_attempt_rate",
        "temporal_collision_rate",
        "research_rollup_quality",
        "normalized_score",
        "orchestrator_usage_breakdown",
        "promotion_attempt_history",
    ]
    if orchestrator_observability is None:
        unavailable_metrics.append("orchestrator_token_usage")

    return {
        "ok": True,
        "snapshot_at": utc_timestamp(),
        "root_dir": str(root),
        "goal_plus": _goal_payload(goal_record),
        "selected_run_id": run_id,
        "search_tasks": search_tasks,
        "search_task_aggregate": search_task_aggregate,
        "run": run_payload,
        "strategy": strategy_payload,
        "statistics": {
            "schema_version": 1,
            "selected_run": selected_run_statistics,
            "orchestrator": orchestrator_observability,
            "total_usage": total_usage,
            "unavailable_metrics": unavailable_metrics,
        },
        "main_agent": main_agent,
        "candidates": candidates_payload,
        "subagents": subagents,
        "warnings": warnings,
    }

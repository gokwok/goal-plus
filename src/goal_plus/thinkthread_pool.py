from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Literal
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from goal_plus.agent_pool import WorkerPoolEvent
from goal_plus.models import FsSnapshotArtifactRef
from goal_plus.runtime import (
    FileSearchRuntime,
    exclusive_file_lock,
    load_json,
    utc_timestamp,
    write_json,
)
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
)
from goal_plus.tools import SearchTools


PROTOCOL = "goal-plus.pi-thinkthread.v2"
POOL_SCHEMA_VERSION = 2
ACTIVE_STATES = {"starting", "running", "needs_recovery"}
TERMINAL_STATES = {"completed", "failed", "interrupted", "timed_out"}
WORKER_TOOLS = {
    "search_get_agent_context",
    "search_get_global_evidence",
    "search_get_evidence_detail",
    "search_stage_shared_tool",
    "search_copy_shared_tool",
    "search_run_verifier",
    "search_list_iterations",
}
RESPONSE_CHUNK_BYTES = 32 * 1024
MESSAGE_RECEIVE_LIMIT = 32
CHILD_DEADLINE_GRACE_SECONDS = 5.0
BRANCH_MUTATION_STOP_TIMEOUT_SECONDS = 15.0


class _WorkerRpcNeedsRecovery(RuntimeError):
    pass


def _wait_for_execution_absent(
    sdk: AgentPosixSdkClient,
    child_id: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Return the authoritative absent Child projection after a bounded wait.

    ThinkThread reports an elapsed wait as a typed ``WaitTimeout`` rejection;
    older test doubles and earlier builds returned a successful response with
    the Child still running. Both mean the same thing to the controller: the
    current signal did not yet make execution absent, so the caller may
    escalate or persist recovery instead of treating the timeout as a fatal
    control-transport error.
    """

    try:
        waited = sdk.invoke(
            "thinkthread.wait",
            {
                "id": child_id,
                "timeoutMs": int(timeout_seconds * 1000),
            },
            timeout_seconds=timeout_seconds + 5.0,
        )
    except AgentPosixBridgeError as exc:
        if exc.code != "WaitTimeout":
            raise
        observed = sdk.invoke("thinkthread.get", {"id": child_id})
        return (
            observed
            if observed.get("executionState") == "absent"
            else None
        )
    child = waited.get("child")
    return (
        child
        if isinstance(child, dict) and child.get("executionState") == "absent"
        else None
    )


def _pool_root(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser().resolve() / "host-pools" / "pi"


def _safe(value: str, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _pool_dir(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_root(root_dir) / _safe(pool_id, "pool_id")


def _pool_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.json"


def _lock_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.lock"


def _controller_lock_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "controller.lock"


class _TryControllerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None
        self.lock_dir: Path | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            self.handle = self.path.open("a", encoding="utf-8")
            try:
                fcntl.flock(
                    self.handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                self.handle.close()
                self.handle = None
                return False
            self.acquired = True
            return True
        self.lock_dir = self.path.with_suffix(".dir")
        try:
            self.lock_dir.mkdir()
        except FileExistsError:  # pragma: no cover - non-POSIX fallback
            return False
        self.acquired = True
        return True

    def __exit__(self, *_exc: Any) -> None:
        if not self.acquired:
            return
        if self.handle is not None:
            assert fcntl is not None
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        elif self.lock_dir is not None:  # pragma: no cover - non-POSIX fallback
            self.lock_dir.rmdir()


def _job_path(root_dir: Path | str, pool_id: str, job_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "jobs" / _safe(job_id, "job_id") / "job.json"


def _load_pool(root_dir: Path | str, pool_id: str) -> dict[str, Any]:
    path = _pool_path(root_dir, pool_id)
    if not path.exists():
        raise FileNotFoundError(f"unknown Pi worker pool: {pool_id}")
    pool = load_json(path)
    if pool.get("host") != "pi-thinkthread":
        raise ValueError(f"pool {pool_id} is not a pi-thinkthread pool")
    return pool


def _load_job(root_dir: Path | str, pool_id: str, job_id: str) -> dict[str, Any]:
    return load_json(_job_path(root_dir, pool_id, job_id))


def _write_pool(root_dir: Path | str, pool: dict[str, Any]) -> None:
    pool["updated_at"] = utc_timestamp()
    write_json(_pool_path(root_dir, str(pool["pool_id"])), pool)


def _write_job(
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> None:
    job["updated_at"] = utc_timestamp()
    write_json(_job_path(root_dir, pool_id, str(job["job_id"])), job)


def _jobs(root_dir: Path | str, pool: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _load_job(root_dir, str(pool["pool_id"]), str(job_id))
        for job_id in pool.get("jobs", [])
    ]


def _capabilities(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item["id"])
        for item in value
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _selected_session(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate_id: str,
) -> Any:
    sessions = [
        session
        for session in runtime._load_agent_sessions(run_id)
        if session.candidate_id == candidate_id and session.host == "pi-thinkthread"
    ]
    if not sessions:
        raise RuntimeError(
            f"candidate {candidate_id} has no pi-thinkthread agent session"
        )
    return sessions[-1]


def _budget_from_launch(
    launch: dict[str, Any],
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    control = launch.get("budget_control")
    control = control if isinstance(control, dict) else {}
    lease = control.get("autoresearch_lease")
    lease = lease if isinstance(lease, dict) else {}
    budget: dict[str, Any] = {
        "max_runtime_seconds": control.get("max_runtime_seconds"),
        "max_turns": control.get("max_turns_hint"),
        "on_exceed": control.get("on_exceed", "interrupt"),
        "min_runtime_seconds": lease.get("min_runtime_seconds"),
        "min_verifier_runs": lease.get("min_verifier_runs"),
    }
    budget = {key: value for key, value in budget.items() if value is not None}
    budget.update(override or {})
    max_runtime = budget.get("max_runtime_seconds")
    if not isinstance(max_runtime, int) or isinstance(max_runtime, bool) or max_runtime <= 0:
        raise ValueError(
            "pi-thinkthread pool workers require worker_budget.max_runtime_seconds"
        )
    for key in ("min_runtime_seconds", "min_verifier_runs"):
        value = budget.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ValueError(f"pi-thinkthread {key} must be a positive integer")
    minimum_runtime = budget.get("min_runtime_seconds")
    if isinstance(minimum_runtime, int) and minimum_runtime >= max_runtime:
        raise ValueError(
            "pi-thinkthread min_runtime_seconds must be less than max_runtime_seconds"
        )
    return budget


def _timestamp_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _lease_started_epoch(job: dict[str, Any]) -> float:
    value = _timestamp_epoch(job.get("lease_started_unix"))
    if value is not None:
        return value
    value = _timestamp_epoch(job.get("started_at"))
    if value is not None:
        return value
    return time.time()


def _lease_deadline_epoch(job: dict[str, Any]) -> float:
    budget = job.get("worker_budget")
    if not isinstance(budget, dict):
        raise RuntimeError("pi-thinkthread pool job omitted worker_budget")
    return _lease_started_epoch(job) + float(budget["max_runtime_seconds"])


def _lease_satisfied(job: dict[str, Any], *, now: float | None = None) -> bool:
    budget = job.get("worker_budget")
    if not isinstance(budget, dict):
        return False
    current = time.time() if now is None else now
    elapsed = max(0.0, current - _lease_started_epoch(job))
    verifier_delta = max(
        0,
        int(job.get("verifier_runs", 0))
        - int(job.get("lease_start_verifier_runs", 0)),
    )
    return (
        elapsed >= float(budget.get("min_runtime_seconds") or 0)
        and verifier_delta >= int(budget.get("min_verifier_runs") or 0)
    )


def _validate_run(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate_ids: list[str],
    max_parallel: int | None,
) -> tuple[Any, Any, int, str]:
    run = runtime._load_run(run_id)
    runtime._assert_run_not_invalidated(run, "open Pi ThinkThread pool")
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    if frozen.spec.strategy.worker_host != "pi-thinkthread":
        raise ValueError("pi-thinkthread pool requires worker_host='pi-thinkthread'")
    baseline = run.baseline_artifact_ref
    if not isinstance(baseline, FsSnapshotArtifactRef):
        raise RuntimeError("pi-thinkthread run has no exact baseline snapshot")
    limit = int(max_parallel or frozen.spec.budget.max_parallel)
    if limit <= 0 or limit > frozen.spec.budget.max_parallel:
        raise ValueError("max_parallel exceeds the frozen Search limit")
    if len(candidate_ids) > limit:
        raise ValueError("initial candidate count exceeds max_parallel")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate_ids must be unique")
    for candidate_id in candidate_ids:
        record = runtime._load_candidate_record(run_id, candidate_id)
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot dispatch candidate {candidate_id} in status {record.status}"
            )
        # The host-owned pool is the public launch surface.  Persist the
        # Search provenance session here before the Child spawn, matching the
        # legacy Pi pool contract and making a retried pool-open idempotently
        # recover a turn interrupted between start_batch and pool_open.
        if not any(
            session.candidate_id == candidate_id
            and session.host == "pi-thinkthread"
            for session in runtime._load_agent_sessions(run_id)
        ):
            runtime.start_agent_session(run_id, candidate_id)
        _selected_session(runtime, run_id, candidate_id)
    return run, frozen, limit, baseline.snapshot_id


def _assert_root(client: AgentPosixSdkClient) -> dict[str, Any]:
    client.preflight()
    view = client.self_view()
    if view.get("parentThinkthreadId") is not None:
        raise RuntimeError("pi-thinkthread pool controller must run in Root")
    missing = {
        "thinkthread.child",
        "thinkthread.message",
        "thinkthread.fs",
    } - _capabilities(view.get("capabilities"))
    if missing:
        raise RuntimeError(
            "pi-thinkthread Root lacks capabilities: " + ", ".join(sorted(missing))
        )
    fs = client.invoke("fs.stat")
    if fs.get("kind") != "direct":
        raise RuntimeError("pi-thinkthread Root filesystem must be direct")
    storage = fs.get("storage")
    if isinstance(storage, dict):
        snapshot_count = int(storage.get("snapshotCount", 0)) + int(
            storage.get("pendingSnapshotCreations", 0)
        )
        if snapshot_count >= int(storage.get("snapshotLimit", 1)):
            raise RuntimeError("ThinkThread snapshot quota is exhausted")
        if int(storage.get("requestCount", 0)) >= int(
            storage.get("requestLimit", 1)
        ):
            raise RuntimeError("ThinkThread durable request quota is exhausted")
        if int(storage.get("activeRequestCount", 0)) >= int(
            storage.get("activeRequestLimit", 1)
        ):
            raise RuntimeError("ThinkThread active filesystem operation quota is exhausted")
        if int(storage.get("snapshotLogicalBytes", 0)) >= int(
            storage.get("snapshotLogicalByteLimit", 1)
        ):
            raise RuntimeError("ThinkThread snapshot logical byte quota is exhausted")
    return view


def _spawn_params(
    launch: dict[str, Any],
    baseline_snapshot_id: str,
    registration_nonce: str,
    dispatch_nonce: str,
) -> dict[str, Any]:
    message = str(launch.get("message") or "")
    message += (
        "\n\nThinkThread registration: "
        f"protocol={PROTOCOL}; registration_nonce={registration_nonce}; "
        f"dispatch_nonce={dispatch_nonce}."
    )
    params: dict[str, Any] = {
        "profile": str(launch.get("profile") or "self"),
        "fs": "private",
        "fsSnapshotId": baseline_snapshot_id,
        "capabilities": ["thinkthread.message"],
        "initialMessage": message,
    }
    model = launch.get("model")
    if isinstance(model, dict):
        params["model"] = model
    return params


def _bind_spawn(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate_id: str,
    session: Any,
    result: dict[str, Any],
    job: dict[str, Any],
) -> None:
    child_id = result.get("thinkthreadId")
    attachment = result.get("fs")
    branch_id = attachment.get("fsBranchId") if isinstance(attachment, dict) else None
    if not isinstance(child_id, str) or not child_id.startswith("tt-"):
        raise RuntimeError("thinkthread.spawn omitted thinkthreadId")
    if (
        not isinstance(attachment, dict)
        or attachment.get("kind") != "private"
        or not isinstance(branch_id, str)
        or not branch_id.startswith("fsbranch-")
    ):
        raise RuntimeError("thinkthread.spawn omitted private fsBranchId")
    if _capabilities(result.get("capabilities")) != {"thinkthread.message"}:
        raise RuntimeError("ThinkThread Child grant is not Message-only")
    expected_model = session.launch.get("model")
    if isinstance(expected_model, dict) and result.get("model") != expected_model:
        raise RuntimeError("ThinkThread Child model does not match selected model")

    with runtime._run_transaction(run_id):
        record = runtime._load_candidate_record(run_id, candidate_id)
        record.task.fs_branch_id = branch_id
        record.task.strategy_metadata["thinkthread_id"] = child_id
        record.task.strategy_metadata["fs_branch_id"] = branch_id
        runtime._write_candidate_record(run_id, record)
    runtime.bind_agent_handle(
        session.agent_session_id,
        {
            "host": "pi-thinkthread",
            "external_id": child_id,
            "metadata": {
                "continuation": "retained_child_session",
                "fs_branch_id": branch_id,
                "initial_message_id": result.get("initialMessageId"),
                "model": result.get("model"),
                "capabilities": sorted(_capabilities(result.get("capabilities"))),
            },
        },
    )
    job.update(
        {
            "thinkthread_id": child_id,
            "fs_branch_id": branch_id,
            "initial_message_id": result.get("initialMessageId"),
            "active_message_id": result.get("initialMessageId"),
            "model": result.get("model"),
            "status": "running",
            "started_at": utc_timestamp(),
        }
    )
    initial_nonce = job.get("initial_dispatch_nonce") or job.get("dispatch_nonce")
    initial_dispatch = next(
        (
            item
            for item in job.get("dispatch_records", [])
            if isinstance(item, dict)
            and item.get("dispatch_nonce") == initial_nonce
        ),
        None,
    )
    if isinstance(initial_dispatch, dict):
        initial_dispatch["state"] = "sent"
        initial_dispatch["message_id"] = result.get("initialMessageId")
        initial_dispatch["sent_at"] = job["started_at"]


def open_pool(
    *,
    root_dir: Path | str,
    run_id: str,
    candidate_ids: list[str] | None = None,
    worker_budgets: dict[str, dict[str, Any]] | None = None,
    final_verify: bool = True,
    max_parallel: int | None = None,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    runtime = FileSearchRuntime(root_dir)
    initial_ids = list(candidate_ids or [])
    run, frozen, selected_parallel, baseline = _validate_run(
        runtime,
        run_id,
        initial_ids,
        max_parallel,
    )
    unknown_budget_ids = sorted(set(worker_budgets or {}) - set(initial_ids))
    if unknown_budget_ids:
        raise ValueError(
            "worker_budgets contains unknown candidate ids: "
            + ", ".join(unknown_budget_ids)
        )
    sdk = client or AgentPosixSdkClient()
    root_view = _assert_root(sdk)
    pool_id = f"pool_{uuid.uuid4().hex[:12]}"
    now = utc_timestamp()
    pool = {
        "schema_version": POOL_SCHEMA_VERSION,
        "pool_id": pool_id,
        "host": "pi-thinkthread",
        "run_id": run_id,
        "root_thinkthread_id": root_view.get("thinkthreadId"),
        "baseline_snapshot_id": baseline,
        "max_parallel": selected_parallel,
        "state": "open",
        "created_at": now,
        "updated_at": now,
        "jobs": [],
    }
    with exclusive_file_lock(_lock_path(root_dir, pool_id)):
        _write_pool(root_dir, pool)

    for candidate_id in initial_ids:
        session = _selected_session(runtime, run_id, candidate_id)
        budget = _budget_from_launch(
            session.launch,
            (worker_budgets or {}).get(candidate_id),
        )
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        registration_nonce = str(uuid.uuid4())
        dispatch_nonce = str(uuid.uuid4())
        lease_started_unix = time.time()
        job = {
            "job_id": job_id,
            "pool_id": pool_id,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": session.agent_session_id,
            "status": "starting",
            "dispatch_index": 1,
            "registration_nonce": registration_nonce,
            "initial_dispatch_nonce": dispatch_nonce,
            "dispatch_nonce": dispatch_nonce,
            "dispatch_records": [
                {
                    "dispatch_nonce": dispatch_nonce,
                    "stage": "initial_spawn",
                    "state": "prepared",
                    "created_at": utc_timestamp(),
                }
            ],
            "thinkthread_id": None,
            "fs_branch_id": None,
            "message_cursor": None,
            "settled_requests": {},
            "verifier_runs": 0,
            "lease_start_verifier_runs": 0,
            "lease_started_unix": lease_started_unix,
            "worker_budget": budget,
            "final_verify": bool(final_verify),
            "created_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
            "started_at": None,
            "finished_at": None,
            "delivered_at": None,
            "error": None,
        }
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            _write_job(root_dir, pool_id, job)
            pool = _load_pool(root_dir, pool_id)
            pool["jobs"].append(job_id)
            _write_pool(root_dir, pool)
        params = _spawn_params(
            session.launch,
            baseline,
            registration_nonce,
            dispatch_nonce,
        )
        job["spawn_intent"] = {
            "state": "platform_mutation_started",
            "params_sha256": hashlib.sha256(
                json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "observed_children_before": [
                child.get("thinkthreadId")
                for child in sdk.invoke("thinkthread.list").get("children", [])
                if isinstance(child, dict)
            ],
        }
        _write_job(root_dir, pool_id, job)
        try:
            result = sdk.invoke("thinkthread.spawn", params, timeout_seconds=120)
            _bind_spawn(runtime, run_id, candidate_id, session, result, job)
            job["spawn_intent"]["state"] = "bound"
        except AgentPosixBridgeError as exc:
            job["status"] = "needs_recovery" if exc.completion_unknown else "failed"
            job["error"] = {
                "stage": "spawn",
                "message": str(exc),
                "error_code": exc.code,
                "completion_unknown": exc.completion_unknown,
            }
            job["spawn_intent"]["state"] = (
                "outcome_unknown" if exc.completion_unknown else "failed"
            )
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = {
                "stage": "spawn_binding",
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
        _write_job(root_dir, pool_id, job)
    return snapshot_pool(root_dir=root_dir, pool_id=pool_id)


def _registration_from_message(
    message: dict[str, Any],
    registration_nonce: str,
) -> bool:
    text = message.get("text")
    if not isinstance(text, str) or message.get("truncated") is True:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("protocol") == PROTOCOL
        and payload.get("type") == "registration"
        and payload.get("registration_nonce") == registration_nonce
    )


def _recover_spawn_binding(
    runtime: FileSearchRuntime,
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
) -> bool:
    intent = job.get("spawn_intent")
    if (
        job.get("status") != "needs_recovery"
        or not isinstance(intent, dict)
        or intent.get("state") != "outcome_unknown"
    ):
        return False
    observed_before = {
        child_id
        for child_id in intent.get("observed_children_before", [])
        if isinstance(child_id, str)
    }
    already_bound = {
        item.get("thinkthread_id")
        for item in _jobs(root_dir, pool)
        if item.get("job_id") != job.get("job_id")
        and isinstance(item.get("thinkthread_id"), str)
    }
    listed = sdk.invoke("thinkthread.list").get("children")
    if not isinstance(listed, list):
        raise AgentPosixBridgeError("thinkthread.list omitted children")
    expected_model = _selected_session(
        runtime,
        str(job["run_id"]),
        str(job["candidate_id"]),
    ).launch.get("model")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for child in listed:
        if not isinstance(child, dict):
            continue
        child_id = child.get("thinkthreadId")
        attachment = child.get("fs")
        if (
            not isinstance(child_id, str)
            or child_id in observed_before
            or child_id in already_bound
            or _capabilities(child.get("capabilities")) != {"thinkthread.message"}
            or not isinstance(attachment, dict)
            or attachment.get("kind") != "private"
            or (isinstance(expected_model, dict) and child.get("model") != expected_model)
        ):
            continue
        batch = sdk.invoke(
            "message.receive",
            {
                "senderThinkthreadId": child_id,
                "limit": MESSAGE_RECEIVE_LIMIT,
            },
        )
        messages = batch.get("messages")
        if not isinstance(messages, list):
            raise AgentPosixBridgeError("message.receive omitted messages")
        if any(
            isinstance(message, dict)
            and _registration_from_message(message, str(job["registration_nonce"]))
            for message in messages
        ):
            matches.append((child, batch))
    if len(matches) != 1:
        intent["recovery_candidates"] = [
            child.get("thinkthreadId") for child, _batch in matches
        ]
        intent["last_recovery_at"] = utc_timestamp()
        _write_job(root_dir, str(pool["pool_id"]), job)
        return False

    child, batch = matches[0]
    session = _selected_session(
        runtime,
        str(job["run_id"]),
        str(job["candidate_id"]),
    )
    _bind_spawn(
        runtime,
        str(job["run_id"]),
        str(job["candidate_id"]),
        session,
        child,
        job,
    )
    job["registered_at"] = utc_timestamp()
    intent["state"] = "bound_after_recovery"
    intent["recovered_at"] = utc_timestamp()
    for message in batch.get("messages", []):
        if not isinstance(message, dict) or _registration_from_message(
            message,
            str(job["registration_nonce"]),
        ):
            continue
        _process_message(runtime, sdk, root_dir, job, message)
    next_cursor = batch.get("nextCursor")
    if not isinstance(next_cursor, str):
        raise AgentPosixBridgeError("message.receive omitted nextCursor")
    job["message_cursor"] = next_cursor
    _write_job(root_dir, str(pool["pool_id"]), job)
    return True


def _request_hash(request: dict[str, Any]) -> str:
    payload = {
        "agent_session_id": request.get("agent_session_id"),
        "tool": request.get("tool"),
        "params": request.get("params"),
    }
    content_json = request.get("content_json")
    if not isinstance(content_json, str):
        raise ValueError("worker RPC content_json is required")
    try:
        decoded = json.loads(content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("worker RPC content_json is invalid") from exc
    if decoded != payload:
        raise ValueError("worker RPC content_json does not match request fields")
    return hashlib.sha256(content_json.encode("utf-8")).hexdigest()


def _validate_request(job: dict[str, Any], request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if request.get("protocol") != PROTOCOL or request.get("type") != "request":
        raise ValueError("unsupported worker RPC envelope")
    request_id = request.get("request_id")
    tool = request.get("tool")
    params = request.get("params")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("worker RPC request_id is required")
    if tool not in WORKER_TOOLS:
        raise PermissionError(f"worker RPC tool is not allowed: {tool}")
    if not isinstance(params, dict):
        raise ValueError("worker RPC params must be an object")
    if request.get("agent_session_id") != job["agent_session_id"]:
        raise PermissionError("worker RPC agent_session_id binding mismatch")
    expected_hash = _request_hash(request)
    if request.get("content_sha256") != expected_hash:
        raise ValueError("worker RPC content_sha256 mismatch")
    return request_id, str(tool), params


def _dispatch(
    runtime: FileSearchRuntime,
    job: dict[str, Any],
    tool: str,
    params: dict[str, Any],
    *,
    request_id: str,
) -> Any:
    tools = SearchTools(runtime)
    session_id = str(job["agent_session_id"])
    run_id = str(job["run_id"])
    candidate_id = str(job["candidate_id"])
    if "agent_session_id" in params and params["agent_session_id"] != session_id:
        raise PermissionError("worker RPC session parameter mismatch")
    if "run_id" in params and params["run_id"] != run_id:
        raise PermissionError("worker RPC run parameter mismatch")
    if tool != "search_get_evidence_detail" and "candidate_id" in params and params["candidate_id"] != candidate_id:
        raise PermissionError("worker RPC candidate parameter mismatch")
    if tool == "search_get_agent_context":
        return tools.search_get_agent_context(session_id)
    if tool == "search_get_global_evidence":
        return tools.search_get_global_evidence(session_id)
    if tool == "search_get_evidence_detail":
        return tools.search_get_evidence_detail(
            session_id,
            str(params["candidate_id"]),
            int(params["iteration"]),
        )
    if tool == "search_stage_shared_tool":
        return runtime.stage_shared_tool(
            session_id,
            name=str(params["name"]),
            summary=str(params["summary"]),
            entrypoint=str(params["entrypoint"]),
            candidate_relative_source_paths=list(
                params["candidate_relative_source_paths"]
            ),
            idempotency_key=request_id,
        )
    if tool == "search_copy_shared_tool":
        result = runtime.copy_shared_tool(
            session_id,
            str(params["tool_id"]),
            str(params["snapshot_hash"]),
            idempotency_key=request_id,
        )
        if result.get("state") == "copy_required_at_turn_boundary":
            job.setdefault("copy_requirements", []).append(
                {
                    "state": "copy_required",
                    "receipt_id": result["receipt_id"],
                    "requested_at": utc_timestamp(),
                }
            )
            _require_turn_boundary_wake(job, "shared_tool_copy")
        return result
    if tool == "search_run_verifier":
        result = tools.search_run_verifier(
            run_id=run_id,
            candidate_id=candidate_id,
            scope="process",
            agent_session_id=session_id,
            hypothesis=params.get("hypothesis"),
            toolization_decision=params.get("toolization_decision"),
            idempotency_key=request_id,
        )
        job["verifier_runs"] = int(job.get("verifier_runs", 0)) + 1
        latest_record = runtime._load_candidate_record(run_id, candidate_id)
        latest_iteration = (
            latest_record.iterations[-1] if latest_record.iterations else None
        )
        if (
            latest_iteration is not None
            and latest_iteration.metrics.get("thinkthread_continuation_required")
            is True
        ):
            job["checkpoint_continuation"] = {
                "state": "required",
                "iteration": latest_iteration.iteration,
                "snapshot_id": (
                    latest_iteration.attempt_ref.snapshot_id
                    if isinstance(
                        latest_iteration.attempt_ref, FsSnapshotArtifactRef
                    )
                    else None
                ),
                "required_at": utc_timestamp(),
            }
            _require_turn_boundary_wake(job, "checkpoint_resume")
        if result.get("disposition") in {"discard", "failure"}:
            target = result.get("workspace_artifact_after_settlement")
            target_id = (
                target.get("snapshot_id") if isinstance(target, dict) else None
            )
            if not isinstance(target_id, str):
                raise RuntimeError("discard settlement omitted restore snapshot")
            job["restore_required"] = {
                "state": "restore_required",
                "target_snapshot_id": target_id,
                "requested_at": utc_timestamp(),
            }
            _require_turn_boundary_wake(job, "verifier_restore")
        return result
    if tool == "search_list_iterations":
        return tools.search_list_iterations(run_id, candidate_id)
    raise AssertionError(tool)


def _require_turn_boundary_wake(job: dict[str, Any], reason: str) -> None:
    intent = job.get("turn_boundary_wake_required")
    if not isinstance(intent, dict):
        intent = {
            "state": "required",
            "reasons": [],
            "required_at": utc_timestamp(),
        }
        job["turn_boundary_wake_required"] = intent
    reasons = intent.setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)
    if intent.get("state") not in {"waking", "outcome_unknown"}:
        intent["state"] = "required"


def _complete_turn_boundary_wake(job: dict[str, Any]) -> None:
    intent = job.get("turn_boundary_wake_required")
    if not isinstance(intent, dict):
        return
    intent["state"] = "woken"
    intent["woken_at"] = utc_timestamp()
    checkpoint = job.get("checkpoint_continuation")
    if (
        "checkpoint_resume" in intent.get("reasons", [])
        and isinstance(checkpoint, dict)
    ):
        checkpoint["state"] = "resumed"
        checkpoint["resumed_at"] = intent["woken_at"]
    job.setdefault("turn_boundary_wakes", []).append(dict(intent))
    job.pop("turn_boundary_wake_required", None)


def _response_chunks(request_id: str, response: dict[str, Any]) -> list[str]:
    data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    response_hash = hashlib.sha256(data).hexdigest()
    pieces = [data[index : index + RESPONSE_CHUNK_BYTES] for index in range(0, len(data), RESPONSE_CHUNK_BYTES)] or [b""]
    messages = []
    for index, piece in enumerate(pieces):
        messages.append(
            json.dumps(
                {
                    "protocol": PROTOCOL,
                    "type": "response_chunk",
                    "request_id": request_id,
                    "chunk_index": index,
                    "chunk_count": len(pieces),
                    "data_base64": base64.b64encode(piece).decode("ascii"),
                    "chunk_sha256": hashlib.sha256(piece).hexdigest(),
                    "response_sha256": response_hash,
                },
                separators=(",", ":"),
            )
        )
    return messages


def _response_hash(response: dict[str, Any]) -> str:
    data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _send_pending_response(
    sdk: AgentPosixSdkClient,
    job: dict[str, Any],
    request_id: str,
) -> None:
    pending = job.setdefault("pending_responses", {})
    record = pending.get(request_id)
    settled = job.setdefault("settled_requests", {}).get(request_id)
    if not isinstance(record, dict) or not isinstance(settled, dict):
        return
    response = settled.get("response")
    if not isinstance(response, dict):
        return
    for chunk in _response_chunks(request_id, response):
        sdk.invoke(
            "message.send",
            {
                "recipientThinkthreadId": job["thinkthread_id"],
                "text": chunk,
                "wake": False,
                "replyToMessageId": record.get("reply_to_message_id"),
            },
        )
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["last_attempt_unix"] = time.time()
    record["last_attempt_at"] = utc_timestamp()


def _retry_pending_responses(
    sdk: AgentPosixSdkClient,
    job: dict[str, Any],
) -> None:
    pending = job.get("pending_responses")
    if not isinstance(pending, dict):
        return
    now = time.time()
    for request_id, record in list(pending.items()):
        if not isinstance(record, dict):
            continue
        last_attempt = _timestamp_epoch(record.get("last_attempt_unix")) or 0.0
        if now - last_attempt < 0.5:
            continue
        try:
            _send_pending_response(sdk, job, str(request_id))
        except AgentPosixBridgeError as exc:
            record["last_error"] = {
                "message": str(exc),
                "error_code": exc.code,
                "completion_unknown": exc.completion_unknown,
            }
            record["last_attempt_unix"] = now
            record["last_attempt_at"] = utc_timestamp()


def _process_message(
    runtime: FileSearchRuntime,
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    job: dict[str, Any],
    message: dict[str, Any],
) -> None:
    if message.get("senderThinkthreadId") != job.get("thinkthread_id"):
        raise PermissionError("worker Message sender does not match bound Child")
    text = message.get("text")
    if not isinstance(text, str):
        return
    if message.get("truncated") is True:
        if text.lstrip().startswith('{"protocol":"goal-plus.pi-thinkthread.'):
            raise ValueError("worker RPC Message was truncated")
        return
    try:
        request = json.loads(text)
    except json.JSONDecodeError:
        # ThinkThread also retains the Child's ordinary assistant completion
        # on this direct Message stream.  It is observability, not an RPC
        # envelope, and must not fail an otherwise valid pool job.
        return
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        return
    if isinstance(request, dict) and request.get("type") == "registration":
        if (
            request.get("registration_nonce") != job.get("registration_nonce")
        ):
            raise PermissionError("Child registration nonce mismatch")
        job["registered_at"] = utc_timestamp()
        return
    if isinstance(request, dict) and request.get("type") == "dispatch_ack":
        nonce = request.get("dispatch_nonce")
        records = job.setdefault("dispatch_records", [])
        matching = next(
            (
                item
                for item in records
                if isinstance(item, dict)
                and item.get("dispatch_nonce") == nonce
            ),
            None,
        )
        if matching is None and nonce in {
            job.get("initial_dispatch_nonce"),
            job.get("dispatch_nonce"),
        }:
            matching = {
                "dispatch_nonce": nonce,
                "stage": "legacy",
                "state": "sent",
            }
            records.append(matching)
        if request.get("protocol") != PROTOCOL or matching is None:
            raise PermissionError("Child dispatch acknowledgement mismatch")
        job["dispatch_ack_at"] = utc_timestamp()
        job["dispatch_ack_nonce"] = nonce
        matching["state"] = "acknowledged"
        matching["acknowledged_at"] = job["dispatch_ack_at"]
        wake_intent = job.get("wake_intent")
        if isinstance(wake_intent, dict) and wake_intent.get("dispatch_nonce") == nonce:
            wake_intent["state"] = "acknowledged"
            wake_intent["acknowledged_at"] = utc_timestamp()
            if job.get("status") == "needs_recovery":
                job["status"] = "running"
                job["error"] = None
        return
    if isinstance(request, dict) and request.get("type") == "response_ack":
        if request.get("protocol") != PROTOCOL:
            raise ValueError("unsupported worker response acknowledgement")
        request_id = request.get("request_id")
        pending = job.setdefault("pending_responses", {})
        record = pending.get(request_id)
        if not isinstance(request_id, str) or not isinstance(record, dict):
            raise ValueError("unknown worker response acknowledgement")
        if request.get("response_sha256") != record.get("response_sha256"):
            raise ValueError("worker response acknowledgement hash mismatch")
        settled_record = job.setdefault("settled_requests", {}).get(request_id)
        if isinstance(settled_record, dict):
            settled_record["response_ack_at"] = utc_timestamp()
        pending.pop(request_id, None)
        return
    if not isinstance(job.get("registered_at"), str):
        raise PermissionError("worker RPC arrived before Child registration")
    request_id, tool, params = _validate_request(job, request)
    settled = job.setdefault("settled_requests", {})
    request_hash = _request_hash(request)
    prior = settled.get(request_id)
    if isinstance(prior, dict):
        if prior.get("request_hash") != request_hash:
            raise ValueError("settled worker RPC request_id was reused with new content")
        response = prior["response"]
    else:
        intents = job.setdefault("request_intents", {})
        intent = intents.get(request_id)
        if isinstance(intent, dict):
            if intent.get("request_hash") != request_hash:
                raise ValueError(
                    "in-flight worker RPC request_id was reused with new content"
                )
        else:
            intent = {
                "request_hash": request_hash,
                "tool": tool,
                "state": "platform_mutation_started",
                "started_at": utc_timestamp(),
            }
            intents[request_id] = intent
        # Persist the business idempotency key before any runtime mutation.
        _write_job(root_dir, str(job["pool_id"]), job)
        try:
            result = _dispatch(
                runtime,
                job,
                tool,
                params,
                request_id=request_id,
            )
            response = {"ok": True, "result": result}
        except Exception as exc:
            if (
                isinstance(exc, RuntimeError)
                and "ThinkThreadRequestNeedsRecovery" in str(exc)
            ):
                intent["state"] = "needs_recovery"
                intent["error"] = str(exc)
                intent["updated_at"] = utc_timestamp()
                job["status"] = "needs_recovery"
                job["error"] = {
                    "stage": "worker_rpc",
                    "request_id": request_id,
                    "message": str(exc),
                }
                _write_job(root_dir, str(job["pool_id"]), job)
                raise _WorkerRpcNeedsRecovery(str(exc)) from exc
            response = {
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        intent["state"] = "settled"
        intent["settled_at"] = utc_timestamp()
        settled[request_id] = {
            "request_hash": request_hash,
            "tool": tool,
            "response": response,
            "settled_at": utc_timestamp(),
        }
    job.setdefault("pending_responses", {})[request_id] = {
        "response_sha256": _response_hash(response),
        "reply_to_message_id": message.get("messageId"),
        "attempts": 0,
        "queued_at": utc_timestamp(),
    }
    # Durable settlement and response intent must precede Message transmission.
    _write_job(root_dir, str(job["pool_id"]), job)
    try:
        _send_pending_response(sdk, job, request_id)
    except AgentPosixBridgeError as exc:
        pending = job["pending_responses"][request_id]
        pending["last_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
        pending["last_attempt_unix"] = time.time()
        pending["last_attempt_at"] = utc_timestamp()
    _write_job(root_dir, str(job["pool_id"]), job)


def _receive_job_messages(
    runtime: FileSearchRuntime,
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    job: dict[str, Any],
) -> bool:
    child_id = job.get("thinkthread_id")
    if not isinstance(child_id, str):
        return False
    _retry_pending_responses(sdk, job)
    params: dict[str, Any] = {
        "senderThinkthreadId": child_id,
        "limit": MESSAGE_RECEIVE_LIMIT,
    }
    if isinstance(job.get("message_cursor"), str):
        params["after"] = job["message_cursor"]
    batch = sdk.invoke("message.receive", params)
    messages = batch.get("messages")
    if not isinstance(messages, list):
        raise AgentPosixBridgeError("message.receive omitted messages")
    handled = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        try:
            _process_message(runtime, sdk, root_dir, job, message)
            handled = True
        except (ValueError, PermissionError, json.JSONDecodeError) as exc:
            job.setdefault("message_errors", []).append(
                {
                    "message_id": message.get("messageId"),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "recorded_at": utc_timestamp(),
                }
            )
            job["status"] = "failed"
            job["finished_at"] = utc_timestamp()
            job["error"] = {
                "stage": "message_validation",
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
            handled = True
            break
    next_cursor = batch.get("nextCursor")
    if not isinstance(next_cursor, str):
        raise AgentPosixBridgeError("message.receive omitted nextCursor")
    job["message_cursor"] = next_cursor
    _write_job(root_dir, str(job["pool_id"]), job)
    return handled


def _restore_job_branch(
    runtime: FileSearchRuntime,
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> bool:
    restore = job.get("restore_required")
    if not isinstance(restore, dict) or restore.get("state") == "restored":
        return True
    branch_id = job.get("fs_branch_id")
    target_snapshot_id = restore.get("target_snapshot_id")
    if not isinstance(branch_id, str) or not isinstance(target_snapshot_id, str):
        raise RuntimeError("restore intent omitted branch or target snapshot")
    restore["state"] = "restoring"
    _write_job(root_dir, pool_id, job)
    branch = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
    if branch.get("baseSnapshotId") != target_snapshot_id:
        if not _ensure_branch_mutation_execution_absent(
            sdk,
            root_dir,
            pool_id,
            job,
        ):
            restore["state"] = "needs_recovery"
            _write_job(root_dir, pool_id, job)
            return False
        generation = branch.get("controlGeneration")
        if not isinstance(generation, int):
            raise RuntimeError("fs.branch.stat omitted controlGeneration")
        try:
            sdk.invoke(
                "fs.branch.reset",
                {
                    "branchId": branch_id,
                    "toSnapshotId": target_snapshot_id,
                    "ifGeneration": generation,
                },
                timeout_seconds=60.0,
            )
        except AgentPosixBridgeError as exc:
            if not exc.completion_unknown:
                raise
            observed = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
            if observed.get("baseSnapshotId") != target_snapshot_id:
                restore["state"] = "needs_recovery"
                restore["error"] = str(exc)
                _write_job(root_dir, pool_id, job)
                return False
    runtime.complete_pi_thinkthread_restore(
        run_id=str(job["run_id"]),
        candidate_id=str(job["candidate_id"]),
        branch_id=branch_id,
        target_snapshot_id=target_snapshot_id,
    )
    restore["state"] = "restored"
    restore["restored_at"] = utc_timestamp()
    _write_job(root_dir, pool_id, job)
    return True


def _ensure_branch_mutation_execution_absent(
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> bool:
    """Stop only the current retained runtime before mutating its branch.

    A completed Pi turn can leave the retained Agent process resident with
    ``agentState=ready`` and ``executionState=running``. ThinkThread correctly
    rejects branch reset while that process is present. TERM ends the current
    execution without destroying the Child or its native Session; a later
    Message wake starts the same Session on the restored branch.
    """

    child_id = job.get("thinkthread_id")
    if not isinstance(child_id, str):
        raise RuntimeError("branch mutation omitted retained ThinkThread Child")
    active_message_id = job.get("active_message_id")
    intent = job.get("branch_mutation_execution_stop")
    if not isinstance(intent, dict) or intent.get("message_id") != active_message_id:
        intent = {
            "state": "prepared",
            "message_id": active_message_id,
            "prepared_at": utc_timestamp(),
        }
        job["branch_mutation_execution_stop"] = intent
        _write_job(root_dir, pool_id, job)

    observed = sdk.invoke("thinkthread.get", {"id": child_id})
    if observed.get("executionState") == "absent":
        intent["state"] = "execution_absent"
        intent["confirmed_at"] = utc_timestamp()
        _write_job(root_dir, pool_id, job)
        return True

    intent["state"] = "term_started"
    intent["term_started_at"] = utc_timestamp()
    _write_job(root_dir, pool_id, job)
    try:
        sdk.invoke("thinkthread.signal", {"id": child_id, "signal": "TERM"})
    except AgentPosixBridgeError as exc:
        # A lost signal response is reconciled by the authoritative wait below;
        # an explicit rejection remains a recovery condition unless the same
        # wait proves the execution already disappeared.
        intent["signal_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
    intent["state"] = "waiting_for_execution_absent"
    intent["wait_started_at"] = utc_timestamp()
    _write_job(root_dir, pool_id, job)
    child = _wait_for_execution_absent(
        sdk,
        child_id,
        BRANCH_MUTATION_STOP_TIMEOUT_SECONDS,
    )
    if child is not None:
        intent["state"] = "execution_absent"
        intent["confirmed_at"] = utc_timestamp()
        intent["completion"] = child.get("completion")
        intent.pop("error", None)
        _write_job(root_dir, pool_id, job)
        return True

    intent["state"] = "needs_recovery"
    intent["error"] = "retained Child execution remained present after TERM/wait"
    intent["last_observation"] = sdk.invoke(
        "thinkthread.get", {"id": child_id}
    )
    intent["updated_at"] = utc_timestamp()
    _write_job(root_dir, pool_id, job)
    return False


def _apply_job_tool_copies(
    runtime: FileSearchRuntime,
    sdk: AgentPosixSdkClient,
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> bool:
    requirements = job.get("copy_requirements")
    if not isinstance(requirements, list):
        return True
    for requirement in requirements:
        if not isinstance(requirement, dict) or requirement.get("state") == "applied":
            continue
        branch_id = job.get("fs_branch_id")
        receipt_id = requirement.get("receipt_id")
        if not isinstance(branch_id, str) or not isinstance(receipt_id, str):
            raise RuntimeError("tool copy intent omitted branch or receipt")
        if not _ensure_branch_mutation_execution_absent(
            sdk,
            root_dir,
            pool_id,
            job,
        ):
            requirement["state"] = "needs_recovery"
            requirement["error"] = (
                "retained Child execution remained present before shared tool copy"
            )
            _write_job(root_dir, pool_id, job)
            return False
        intent_id = requirement.get("snapshot_intent_id")
        if not isinstance(intent_id, str):
            intent_id = f"snapshot_{uuid.uuid4().hex}"
            requirement["snapshot_intent_id"] = intent_id
        requirement["state"] = "branch_snapshot_started"
        _write_job(root_dir, pool_id, job)
        try:
            snapshot_request_id, source_snapshot_id = (
                runtime.capture_pi_thinkthread_branch_snapshot(
                    run_id=str(job["run_id"]),
                    candidate_id=str(job["candidate_id"]),
                    branch_id=branch_id,
                    purpose=f"shared tool copy {receipt_id}",
                    client=sdk,
                    intent_id=intent_id,
                )
            )
        except AgentPosixBridgeError as exc:
            requirement["state"] = (
                "needs_recovery" if exc.completion_unknown else "failed"
            )
            requirement["error"] = str(exc)
            _write_job(root_dir, pool_id, job)
            raise
        except RuntimeError as exc:
            requirement["state"] = "needs_recovery"
            requirement["error"] = str(exc)
            _write_job(root_dir, pool_id, job)
            return False
        requirement.update(
            {
                "state": "branch_snapshot_created",
                "source_snapshot_id": source_snapshot_id,
                "snapshot_request_id": snapshot_request_id,
            }
        )
        _write_job(root_dir, pool_id, job)
        runtime._close_fs_requests_after_evidence(
            str(job["run_id"]), [snapshot_request_id], sdk
        )
        patch_request_id, target_snapshot_id = runtime.patch_pi_thinkthread_tool_copy(
            run_id=str(job["run_id"]),
            candidate_id=str(job["candidate_id"]),
            receipt_id=receipt_id,
            source_snapshot_id=source_snapshot_id,
            client=sdk,
        )
        branch = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
        generation = branch.get("controlGeneration")
        if not isinstance(generation, int):
            raise RuntimeError("fs.branch.stat omitted controlGeneration")
        try:
            sdk.invoke(
                "fs.branch.reset",
                {
                    "branchId": branch_id,
                    "toSnapshotId": target_snapshot_id,
                    "ifGeneration": generation,
                },
                timeout_seconds=60.0,
            )
        except AgentPosixBridgeError as exc:
            if not exc.completion_unknown:
                raise
            observed = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
            if observed.get("baseSnapshotId") != target_snapshot_id:
                requirement["state"] = "needs_recovery"
                requirement["error"] = str(exc)
                _write_job(root_dir, pool_id, job)
                return False
        runtime.complete_pi_thinkthread_tool_copy(
            run_id=str(job["run_id"]),
            candidate_id=str(job["candidate_id"]),
            receipt_id=receipt_id,
            target_snapshot_id=target_snapshot_id,
            request_id=patch_request_id,
            client=sdk,
        )
        requirement.update(
            {
                "state": "applied",
                "source_snapshot_id": source_snapshot_id,
                "target_snapshot_id": target_snapshot_id,
                "request_id": patch_request_id,
                "snapshot_request_id": snapshot_request_id,
                "applied_at": utc_timestamp(),
            }
        )
        _write_job(root_dir, pool_id, job)
    return True


def _event(pool: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    kind: Literal["candidate_ready", "failed", "interrupted", "timed_out"]
    kind = {
        "completed": "candidate_ready",
        "interrupted": "interrupted",
        "timed_out": "timed_out",
    }.get(str(job["status"]), "failed")
    return WorkerPoolEvent(
        event_id=f"event_{job['job_id']}",
        host="pi-thinkthread",
        pool_id=str(pool["pool_id"]),
        kind=kind,
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        job_id=str(job["job_id"]),
        terminal=True,
        agent_session_id=str(job["agent_session_id"]),
        result={
            "handle": {
                "host": "pi-thinkthread",
                "external_id": job.get("thinkthread_id"),
                "metadata": {
                    "fs_branch_id": job.get("fs_branch_id"),
                    "completion": job.get("completion"),
                    "verifier_runs": job.get("verifier_runs", 0),
                },
            },
            "error": job.get("error"),
        },
    ).as_dict()

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

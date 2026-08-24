from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Literal
import uuid

from goal_plus.agent_pool import WorkerPoolEvent
from goal_plus.pi_driver import run_pi_search_candidate
from goal_plus.runtime import (
    FileSearchRuntime,
    exclusive_file_lock,
    load_json,
    utc_timestamp,
    write_json,
)


PoolCloseMode = Literal["drain", "interrupt"]
ACTIVE_JOB_STATES = {"starting", "running"}
TERMINAL_JOB_STATES = {"completed", "failed", "interrupted", "timed_out"}
POOL_SCHEMA_VERSION = 1
NO_PROGRESS_BACKOFF_BASE_SECONDS = 5.0
NO_PROGRESS_BACKOFF_MAX_SECONDS = 60.0


class _PoolWorkerInterrupted(BaseException):
    """Unwind the wrapper so the Pi RPC runner can clean up its child process."""


def _no_progress_backoff_seconds(consecutive_dispatches: int) -> float:
    if consecutive_dispatches <= 0:
        return 0.0
    exponent = min(4, consecutive_dispatches - 1)
    return min(
        NO_PROGRESS_BACKOFF_MAX_SECONDS,
        NO_PROGRESS_BACKOFF_BASE_SECONDS * (2**exponent),
    )


def _pool_root(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser().resolve() / "host-pools" / "pi"


def _safe_identifier(value: str, *, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(ch not in allowed for ch in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _pool_dir(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_root(root_dir) / _safe_identifier(pool_id, label="pool_id")


def _pool_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.json"


def _pool_lock_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.lock"


def _job_dir(root_dir: Path | str, pool_id: str, job_id: str) -> Path:
    return (
        _pool_dir(root_dir, pool_id)
        / "jobs"
        / _safe_identifier(job_id, label="job_id")
    )


def _job_path(root_dir: Path | str, pool_id: str, job_id: str) -> Path:
    return _job_dir(root_dir, pool_id, job_id) / "job.json"


def _load_pool(root_dir: Path | str, pool_id: str) -> dict[str, Any]:
    path = _pool_path(root_dir, pool_id)
    if not path.exists():
        raise FileNotFoundError(f"unknown Pi worker pool: {pool_id}")
    return load_json(path)


def _load_job(root_dir: Path | str, pool_id: str, job_id: str) -> dict[str, Any]:
    path = _job_path(root_dir, pool_id, job_id)
    if not path.exists():
        raise FileNotFoundError(f"unknown Pi pool job: {job_id}")
    return load_json(path)


def _write_pool(root_dir: Path | str, pool: dict[str, Any]) -> None:
    pool["updated_at"] = utc_timestamp()
    write_json(_pool_path(root_dir, str(pool["pool_id"])), pool)


def _write_job(root_dir: Path | str, pool_id: str, job: dict[str, Any]) -> None:
    job["updated_at"] = utc_timestamp()
    write_json(_job_path(root_dir, pool_id, str(job["job_id"])), job)


def _is_process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _validate_pool_run(
    root_dir: Path | str,
    run_id: str,
    max_parallel: int | None,
) -> int:
    runtime = FileSearchRuntime(root_dir)
    run = runtime._load_run(run_id)
    runtime._assert_run_not_invalidated(run, "open or submit Pi pool work")
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    if frozen.spec.strategy.worker_host != "pi-rpc":
        raise ValueError(
            "Pi worker pools require SearchSpec strategy.worker_host='pi-rpc'; "
            f"got {frozen.spec.strategy.worker_host!r}"
        )
    frozen_limit = int(frozen.spec.budget.max_parallel)
    selected = frozen_limit if max_parallel is None else int(max_parallel)
    if selected <= 0:
        raise ValueError("max_parallel must be > 0")
    if selected > frozen_limit:
        raise ValueError(
            f"max_parallel {selected} exceeds frozen Search limit {frozen_limit}"
        )
    return selected


def _validate_candidate(root_dir: Path | str, run_id: str, candidate_id: str) -> None:
    runtime = FileSearchRuntime(root_dir)
    record = runtime._load_candidate_record(run_id, candidate_id)
    if record.status not in {"created", "evaluated"}:
        raise RuntimeError(
            f"cannot dispatch candidate {candidate_id} in status {record.status}"
        )


def _resolve_worker_budget(
    root_dir: Path | str,
    run_id: str,
    candidate_id: str,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime = FileSearchRuntime(root_dir)
    run = runtime._load_run(run_id)
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    record = runtime._load_candidate_record(run_id, candidate_id)
    base = runtime._candidate_worker_budget(frozen, record) or {}
    budget = runtime._normalize_worker_budget_override(
        worker_host="pi-rpc",
        worker_budget={**base, **(override or {})},
    )
    if budget is None or budget.get("max_runtime_seconds") is None:
        raise ValueError("Pi pool workers require worker_budget.max_runtime_seconds")
    return budget


def _lease_max_runtime_seconds(
    root_dir: Path | str,
    run_id: str,
    worker_budget: dict[str, Any],
) -> float:
    configured = float(worker_budget["max_runtime_seconds"])
    outer_deadline = FileSearchRuntime._outer_deadline_epoch(
        os.environ.get("GOAL_PLUS_OUTER_DEADLINE_AT")
    )
    if outer_deadline is None:
        return configured
    runtime = FileSearchRuntime(root_dir)
    run = runtime._load_run(run_id)
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    reserve = _closeout_reserve_seconds(frozen.spec.strategy.config)
    return min(configured, max(0.0, outer_deadline - time.time() - reserve))


def _closeout_reserve_seconds(config: dict[str, Any]) -> float:
    return float(
        config.get("closeout_reserve_seconds")
        or config.get("reserve_closeout_seconds")
        or 0
    )


def _assert_active_pool_close_allowed(
    root_dir: Path | str,
    pool: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> None:
    active_count = sum(job["status"] in ACTIVE_JOB_STATES for job in jobs)
    if active_count == 0:
        return
    outer_deadline = FileSearchRuntime._outer_deadline_epoch(
        os.environ.get("GOAL_PLUS_OUTER_DEADLINE_AT")
    )
    if outer_deadline is None:
        return
    runtime = FileSearchRuntime(root_dir)
    run = runtime._load_run(str(pool["run_id"]))
    if run.invalidated_at:
        return
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    reserve = _closeout_reserve_seconds(frozen.spec.strategy.config)
    remaining = outer_deadline - time.time()
    if remaining > reserve:
        raise RuntimeError(
            f"cannot close Pi pool {pool['pool_id']} with {active_count} active job(s) "
            f"outside the closeout reserve ({remaining:.0f}s remaining, "
            f"{reserve:.0f}s reserved); effective leases are owned by the pool "
            "supervisor, so continue pi_search_pool_wait_any"
        )


def _lease_min_runtime_seconds(
    worker_budget: dict[str, Any],
    max_runtime_seconds: float,
) -> int:
    configured = int(worker_budget.get("min_runtime_seconds") or 0)
    if not configured:
        return 0
    closeout = min(45, max(5, int(max_runtime_seconds) // 5))
    return min(configured, max(0, math.floor(max_runtime_seconds) - closeout))


def _lease_verifier_runs(result: dict[str, Any]) -> int:
    bound_session = result.get("bound_session")
    if not isinstance(bound_session, dict):
        return 0
    counters = bound_session.get("counters")
    if not isinstance(counters, dict):
        return 0
    value = counters.get("verifier_runs")
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _session_refresh_reason(result: dict[str, Any]) -> str | None:
    handle = result.get("handle")
    if not isinstance(handle, dict):
        return None
    metadata = handle.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("refresh_required") is not True:
        return None
    reason = metadata.get("refresh_reason")
    return reason if isinstance(reason, str) and reason else "worker_requested"


def _pool_is_open(root_dir: Path | str, pool_id: str) -> bool:
    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        return _load_pool(root_dir, pool_id)["state"] == "open"


def _resume_agent_session_id(
    root_dir: Path | str,
    *,
    run_id: str,
    candidate_id: str,
    jobs: list[dict[str, Any]],
    pool_id: str,
) -> str:
    for job in reversed(jobs):
        if job.get("candidate_id") != candidate_id:
            continue
        result_path = _job_dir(root_dir, pool_id, str(job["job_id"])) / "result.json"
        if not result_path.exists():
            continue
        result = load_json(result_path)
        agent_session_id = result.get("agent_session_id")
        if isinstance(agent_session_id, str) and agent_session_id:
            return agent_session_id

    runtime = FileSearchRuntime(root_dir)
    sessions = [
        session
        for session in runtime._load_agent_sessions(run_id)
        if session.candidate_id == candidate_id and session.host == "pi-rpc"
    ]
    if sessions:
        return sessions[-1].agent_session_id
    raise RuntimeError(
        f"candidate {candidate_id} has no Pi native session to continue"
    )


def _pending_session_refresh_reason(
    root_dir: Path | str,
    *,
    candidate_id: str,
    jobs: list[dict[str, Any]],
    pool_id: str,
) -> str | None:
    for job in reversed(jobs):
        if job.get("candidate_id") != candidate_id:
            continue
        result_path = _job_dir(root_dir, pool_id, str(job["job_id"])) / "result.json"
        if not result_path.exists():
            continue
        result = load_json(result_path)
        lease = result.get("lease")
        if isinstance(lease, dict):
            reason = lease.get("pending_session_refresh_reason")
            if isinstance(reason, str) and reason:
                return reason
        return _session_refresh_reason(result)
    return None


def _launch_pool_job(
    *,
    root_dir: Path | str,
    pool_id: str,
    job_id: str,
) -> int:
    job_dir = _job_dir(root_dir, pool_id, job_id)
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    source_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(source_root)
        if not existing_pythonpath
        else os.pathsep.join((str(source_root), existing_pythonpath))
    )
    command = [
        sys.executable,
        "-m",
        "goal_plus.pi_pool",
        "worker",
        "--root",
        str(Path(root_dir).expanduser().resolve()),
        "--pool-id",
        pool_id,
        "--job-id",
        job_id,
    ]
    with stdout_path.open("a", encoding="utf-8") as stdout_handle, stderr_path.open(
        "a", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    return int(process.pid)


def open_pi_search_pool(
    *,
    root_dir: Path | str,
    run_id: str,
    candidate_ids: list[str] | None = None,
    worker_budgets: dict[str, dict[str, Any]] | None = None,
    final_verify: bool = True,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    runtime = FileSearchRuntime(root_dir)
    run = runtime._load_run(run_id)
    frozen = runtime._load_frozen_spec(run.frozen_spec_id)
    if frozen.spec.strategy.worker_host == "pi-thinkthread":
        from goal_plus.thinkthread_pool import open_pool

        return open_pool(
            root_dir=root_dir,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            final_verify=final_verify,
            max_parallel=max_parallel,
        )
    selected_parallel = _validate_pool_run(root_dir, run_id, max_parallel)
    initial_ids = list(candidate_ids or [])
    if len(initial_ids) != len(set(initial_ids)):
        raise ValueError("candidate_ids must be unique")
    if len(initial_ids) > selected_parallel:
        raise ValueError(
            f"initial candidate count {len(initial_ids)} exceeds max_parallel {selected_parallel}"
        )
    unknown_budget_ids = sorted(set(worker_budgets or {}) - set(initial_ids))
    if unknown_budget_ids:
        raise ValueError(
            "worker_budgets contains unknown candidate ids: "
            + ", ".join(unknown_budget_ids)
        )
    for candidate_id in initial_ids:
        _validate_candidate(root_dir, run_id, candidate_id)

    pool_id = f"pool_{uuid.uuid4().hex[:12]}"
    now = utc_timestamp()
    pool = {
        "schema_version": POOL_SCHEMA_VERSION,
        "pool_id": pool_id,
        "host": "pi-rpc",
        "run_id": run_id,
        "max_parallel": selected_parallel,
        "state": "open",
        "created_at": now,
        "updated_at": now,
        "jobs": [],
    }
    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        _write_pool(root_dir, pool)

    submitted = []
    try:
        for candidate_id in initial_ids:
            submitted.append(
                _submit_pi_search_pool(
                    root_dir=root_dir,
                    pool_id=pool_id,
                    candidate_id=candidate_id,
                    worker_budget=(worker_budgets or {}).get(candidate_id),
                    final_verify=final_verify,
                )
            )
    except Exception:
        try:
            close_pi_search_pool(
                root_dir=root_dir,
                pool_id=pool_id,
                mode="interrupt",
                timeout_seconds=5,
                _allow_early_close=True,
            )
        except Exception:
            pass
        raise
    snapshot = snapshot_pi_search_pool(root_dir=root_dir, pool_id=pool_id)
    snapshot["submitted"] = submitted
    return snapshot


def _submit_pi_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    redispatch: bool = False,
    worker_budget: dict[str, Any] | None = None,
    final_verify: bool = True,
    _launcher: Callable[..., int] | None = None,
) -> dict[str, Any]:
    launcher = _launcher or _launch_pool_job
    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        pool = _load_pool(root_dir, pool_id)
        if pool["state"] != "open":
            raise RuntimeError(f"cannot submit to Pi pool in state {pool['state']}")
        _validate_candidate(root_dir, str(pool["run_id"]), candidate_id)
        jobs = [_load_job(root_dir, pool_id, job_id) for job_id in pool["jobs"]]
        active = [job for job in jobs if job["status"] in ACTIVE_JOB_STATES]
        if len(active) >= int(pool["max_parallel"]):
            raise RuntimeError(
                f"Pi pool {pool_id} is full ({len(active)}/{pool['max_parallel']})"
            )
        if any(job["candidate_id"] == candidate_id for job in active):
            raise RuntimeError(f"candidate {candidate_id} already has an active pool job")
        if not redispatch and any(job["candidate_id"] == candidate_id for job in jobs):
            raise RuntimeError(
                f"candidate {candidate_id} was already submitted; use pool_continue for continuation"
            )
        resume_agent_session_id = (
            _resume_agent_session_id(
                root_dir,
                run_id=str(pool["run_id"]),
                candidate_id=candidate_id,
                jobs=jobs,
                pool_id=pool_id,
            )
            if redispatch
            else None
        )
        refresh_reason = (
            _pending_session_refresh_reason(
                root_dir,
                candidate_id=candidate_id,
                jobs=jobs,
                pool_id=pool_id,
            )
            if redispatch
            else None
        )
        effective_worker_budget = _resolve_worker_budget(
            root_dir,
            str(pool["run_id"]),
            candidate_id,
            worker_budget,
        )

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = utc_timestamp()
        job = {
            "job_id": job_id,
            "pool_id": pool_id,
            "run_id": pool["run_id"],
            "candidate_id": candidate_id,
            "redispatch": bool(redispatch),
            "refresh_session": refresh_reason is not None,
            "continuation": (
                "new_session_refresh"
                if refresh_reason is not None
                else "native_session"
                if redispatch
                else "new_session"
            ),
            "status": "starting",
            "pid": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "delivered_at": None,
            "error": None,
        }
        request = {
            "root_dir": str(Path(root_dir).expanduser().resolve()),
            "run_id": pool["run_id"],
            "candidate_id": candidate_id,
            "redispatch": bool(redispatch),
            "refresh_session": refresh_reason is not None,
            "refresh_reason": refresh_reason,
            "resume_agent_session_id": resume_agent_session_id,
            "worker_budget": effective_worker_budget,
            "final_verify": bool(final_verify),
        }
        job_dir = _job_dir(root_dir, pool_id, job_id)
        write_json(job_dir / "request.json", request)
        _write_job(root_dir, pool_id, job)
        pool["jobs"].append(job_id)
        _write_pool(root_dir, pool)
        try:
            pid = int(
                launcher(
                    root_dir=root_dir,
                    pool_id=pool_id,
                    job_id=job_id,
                )
            )
        except Exception as exc:
            job.update(
                {
                    "status": "failed",
                    "finished_at": utc_timestamp(),
                    "error": {
                        "stage": "launch",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
            )
            _write_job(root_dir, pool_id, job)
            raise
        job.update({"status": "running", "pid": pid, "started_at": utc_timestamp()})
        _write_job(root_dir, pool_id, job)

    return {
        "pool_id": pool_id,
        "job_id": job_id,
        "candidate_id": candidate_id,
        "redispatch": bool(redispatch),
        "refresh_session": refresh_reason is not None,
        "continuation": (
            "new_session_refresh"
            if refresh_reason is not None
            else "native_session"
            if redispatch
            else "new_session"
        ),
        "status": "running",
        "pid": pid,
    }


def continue_pi_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    worker_budget: dict[str, Any] | None = None,
    final_verify: bool = True,
) -> dict[str, Any]:
    pool = _load_pool(root_dir, pool_id)
    if pool.get("host") == "pi-thinkthread":
        from goal_plus.thinkthread_pool import continue_pool

        return continue_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            candidate_id=candidate_id,
            worker_budget=worker_budget,
            final_verify=final_verify,
        )
    return _submit_pi_search_pool(
        root_dir=root_dir,
        pool_id=pool_id,
        candidate_id=candidate_id,
        redispatch=True,
        worker_budget=worker_budget,
        final_verify=final_verify,
    )


def _reconcile_jobs_locked(
    root_dir: Path | str,
    pool: dict[str, Any],
) -> list[dict[str, Any]]:
    pool_id = str(pool["pool_id"])
    jobs = []
    for job_id in pool["jobs"]:
        job = _load_job(root_dir, pool_id, job_id)
        if job["status"] in ACTIVE_JOB_STATES and not _is_process_alive(job.get("pid")):
            job.update(
                {
                    "status": "failed",
                    "finished_at": utc_timestamp(),
                    "error": {
                        "stage": "supervisor",
                        "error_type": "WorkerProcessExited",
                        "message": "Pi pool worker exited without a terminal result",
                    },
                }
            )
            _write_job(root_dir, pool_id, job)
        jobs.append(job)
    return jobs


def _job_result(
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    result_path = _job_dir(root_dir, pool_id, str(job["job_id"])) / "result.json"
    if result_path.exists():
        return load_json(result_path)
    if job.get("error") is not None:
        return {
            "ok": False,
            "failure": job["error"],
            "error": job["error"].get("message"),
        }
    return None


def _snapshot_payload(
    root_dir: Path | str,
    pool: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    active_count = sum(job["status"] in ACTIVE_JOB_STATES for job in jobs)
    terminal_count = sum(job["status"] in TERMINAL_JOB_STATES for job in jobs)
    pool_id = str(pool["pool_id"])
    return {
        "pool_id": pool_id,
        "host": pool["host"],
        "run_id": pool["run_id"],
        "state": pool["state"],
        "max_parallel": pool["max_parallel"],
        "active_count": active_count,
        "free_slots": max(0, int(pool["max_parallel"]) - active_count),
        "terminal_count": terminal_count,
        "undelivered_count": sum(
            job["status"] in TERMINAL_JOB_STATES and not job.get("delivered_at")
            for job in jobs
        ),
        "jobs": [
            {
                **job,
                "result": (
                    _job_result(root_dir, pool_id, job)
                    if job["status"] in TERMINAL_JOB_STATES
                    else None
                ),
            }
            for job in jobs
        ],
        "created_at": pool["created_at"],
        "updated_at": pool["updated_at"],
    }


def snapshot_pi_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if pool_id is None:
        pools = []
        root = _pool_root(root_dir)
        if root.exists():
            for path in sorted(root.glob("pool_*/pool.json")):
                candidate_pool_id = path.parent.name
                snapshot = snapshot_pi_search_pool(
                    root_dir=root_dir,
                    pool_id=candidate_pool_id,
                )
                if run_id is None or snapshot["run_id"] == run_id:
                    pools.append(snapshot)
        return {"run_id": run_id, "pools": pools}
    raw_pool = _load_pool(root_dir, pool_id)
    if raw_pool.get("host") == "pi-thinkthread":
        from goal_plus.thinkthread_pool import snapshot_pool

        snapshot = snapshot_pool(root_dir=root_dir, pool_id=pool_id)
        if run_id is not None and snapshot["run_id"] != run_id:
            raise ValueError(
                f"Pi pool {pool_id} belongs to run {snapshot['run_id']}, not {run_id}"
            )
        return snapshot
    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        pool = _load_pool(root_dir, pool_id)
        if run_id is not None and pool["run_id"] != run_id:
            raise ValueError(
                f"Pi pool {pool_id} belongs to run {pool['run_id']}, not {run_id}"
            )
        jobs = _reconcile_jobs_locked(root_dir, pool)
        return _snapshot_payload(root_dir, pool, jobs)


def _event_from_job(
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
) -> WorkerPoolEvent:
    status = str(job["status"])
    result = _job_result(root_dir, str(pool["pool_id"]), job)
    request_path = (
        _job_dir(root_dir, str(pool["pool_id"]), str(job["job_id"]))
        / "request.json"
    )
    request = load_json(request_path) if request_path.exists() else {}
    worker_budget = request.get("worker_budget") or {}
    lease = result.get("lease") if isinstance(result, dict) else None
    lease_required = any(
        worker_budget.get(field) is not None
        for field in ("min_runtime_seconds", "min_verifier_runs")
    )
    lease_unsatisfied = (lease_required or lease is not None) and not (
        isinstance(lease, dict) and lease.get("satisfied") is True
    )
    kind: Literal["candidate_ready", "failed", "interrupted", "timed_out"]
    if status == "completed" and not lease_unsatisfied:
        kind = "candidate_ready"
    elif status == "interrupted":
        kind = "interrupted"
    elif status in {"completed", "timed_out"}:
        kind = "timed_out"
    else:
        kind = "failed"
    return WorkerPoolEvent(
        event_id=f"event_{job['job_id']}",
        host="pi-rpc",
        pool_id=str(pool["pool_id"]),
        kind=kind,
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        job_id=str(job["job_id"]),
        agent_session_id=(
            str(result["agent_session_id"])
            if isinstance(result, dict) and result.get("agent_session_id")
            else None
        ),
        result=result,
    )


def wait_any_pi_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    timeout_seconds: float = 30,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    pool = _load_pool(root_dir, pool_id)
    if pool.get("host") == "pi-thinkthread":
        from goal_plus.thinkthread_pool import wait_any

        result = wait_any(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
        )
        snapshot = result["snapshot"]
        event = result.get("event")
        return {
            "pool_id": pool_id,
            "events": [event] if event is not None else [],
            "timed_out": event is None and snapshot["active_count"] > 0,
            "active_count": snapshot["active_count"],
            "free_slots": max(
                0, int(snapshot["max_parallel"]) - int(snapshot["active_count"])
            ),
            "state": snapshot["state"],
        }
    deadline = time.monotonic() + timeout_seconds
    while True:
        with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            jobs = _reconcile_jobs_locked(root_dir, pool)
            ready = [
                job
                for job in jobs
                if job["status"] in TERMINAL_JOB_STATES and not job.get("delivered_at")
            ]
            if ready:
                events = []
                delivered_at = utc_timestamp()
                for job in ready:
                    events.append(_event_from_job(root_dir, pool, job).as_dict())
                    job["delivered_at"] = delivered_at
                    _write_job(root_dir, pool_id, job)
                snapshot = _snapshot_payload(root_dir, pool, jobs)
                return {
                    "pool_id": pool_id,
                    "events": events,
                    "timed_out": False,
                    "active_count": snapshot["active_count"],
                    "free_slots": snapshot["free_slots"],
                    "state": snapshot["state"],
                }
            active_count = sum(job["status"] in ACTIVE_JOB_STATES for job in jobs)
            if active_count == 0:
                return {
                    "pool_id": pool_id,
                    "events": [],
                    "timed_out": False,
                    "active_count": 0,
                    "free_slots": int(pool["max_parallel"]),
                    "state": pool["state"],
                }
        if time.monotonic() >= deadline:
            snapshot = snapshot_pi_search_pool(root_dir=root_dir, pool_id=pool_id)
            return {
                "pool_id": pool_id,
                "events": [],
                "timed_out": True,
                "active_count": snapshot["active_count"],
                "free_slots": snapshot["free_slots"],
                "state": snapshot["state"],
            }
        time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def _signal_process(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def close_pi_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    mode: PoolCloseMode = "drain",
    timeout_seconds: float = 30,
    _allow_early_close: bool = False,
) -> dict[str, Any]:
    if mode not in {"drain", "interrupt"}:
        raise ValueError("mode must be 'drain' or 'interrupt'")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    pool = _load_pool(root_dir, pool_id)
    if pool.get("host") == "pi-thinkthread":
        from goal_plus.thinkthread_pool import close_pool

        return close_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            mode=mode,
            timeout_seconds=timeout_seconds,
        )
    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        pool = _load_pool(root_dir, pool_id)
        if pool["state"] == "closed":
            jobs = _reconcile_jobs_locked(root_dir, pool)
            return _snapshot_payload(root_dir, pool, jobs)
        jobs = _reconcile_jobs_locked(root_dir, pool)
        if not _allow_early_close:
            _assert_active_pool_close_allowed(root_dir, pool, jobs)
        pool["state"] = "draining" if mode == "drain" else "interrupting"
        _write_pool(root_dir, pool)
        if mode == "interrupt":
            for job in jobs:
                if job["status"] in ACTIVE_JOB_STATES and job.get("pid"):
                    _signal_process(int(job["pid"]), signal.SIGTERM)

    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = snapshot_pi_search_pool(root_dir=root_dir, pool_id=pool_id)
        if snapshot["active_count"] == 0:
            break
        if time.monotonic() >= deadline:
            if mode == "interrupt":
                for job in snapshot["jobs"]:
                    if job["status"] in ACTIVE_JOB_STATES and job.get("pid"):
                        _signal_process(int(job["pid"]), signal.SIGKILL)
                time.sleep(0.2)
                snapshot = snapshot_pi_search_pool(
                    root_dir=root_dir,
                    pool_id=pool_id,
                )
                if snapshot["active_count"] == 0:
                    break
            snapshot["close_timed_out"] = True
            return snapshot
        time.sleep(0.2)

    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
        pool = _load_pool(root_dir, pool_id)
        pool["state"] = "closed"
        _write_pool(root_dir, pool)
        jobs = _reconcile_jobs_locked(root_dir, pool)
        return _snapshot_payload(root_dir, pool, jobs)


def _worker_signal_handler(_signum: int, _frame: Any) -> None:
    raise _PoolWorkerInterrupted()


def run_pool_worker(
    *,
    root_dir: Path | str,
    pool_id: str,
    job_id: str,
) -> int:
    request_path = _job_dir(root_dir, pool_id, job_id) / "request.json"
    request = load_json(request_path)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _worker_signal_handler)
    signal.signal(signal.SIGINT, _worker_signal_handler)
    try:
        try:
            worker_budget = dict(request["worker_budget"])
            configured_min_runtime_seconds = int(
                worker_budget.get("min_runtime_seconds") or 0
            )
            max_runtime_seconds = _lease_max_runtime_seconds(
                root_dir,
                str(request["run_id"]),
                worker_budget,
            )
            min_runtime_seconds = _lease_min_runtime_seconds(
                worker_budget,
                max_runtime_seconds,
            )
            min_verifier_runs = int(
                worker_budget.get("min_verifier_runs")
                or (1 if min_runtime_seconds else 0)
            )
            lease_started = time.monotonic()
            verifier_runs = 0
            dispatch_count = 0
            no_progress_dispatches = 0
            agent_session_id = request.get("resume_agent_session_id")
            agent_session_ids: list[str] = []
            verifier_run_baselines: dict[str, int] = {}
            observed_verifier_runs: dict[str, int] = {}
            refresh_next_reason = (
                str(request["refresh_reason"])
                if request.get("refresh_session") and request.get("refresh_reason")
                else None
            )
            session_refreshes: list[dict[str, Any]] = []
            if agent_session_id is not None:
                prior_session = FileSearchRuntime(root_dir)._load_agent_session_by_id(
                    str(agent_session_id),
                    run_id=str(request["run_id"]),
                )
                agent_session_id = str(agent_session_id)
                agent_session_ids.append(agent_session_id)
                verifier_run_baselines[agent_session_id] = int(
                    prior_session.counters.get("verifier_runs", 0)
                )
            release_reason = "no_minimum_lease"

            while True:
                elapsed = max(0.0, time.monotonic() - lease_started)
                remaining = max_runtime_seconds - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        "Pi candidate lease had no time remaining before dispatch"
                    )
                dispatch_budget = dict(worker_budget)
                dispatch_budget["max_runtime_seconds"] = max(1, math.floor(remaining))
                remaining_minimum = max(0.0, min_runtime_seconds - elapsed)
                if 0 < remaining_minimum < dispatch_budget["max_runtime_seconds"]:
                    dispatch_budget["min_runtime_seconds"] = math.ceil(
                        remaining_minimum
                    )
                else:
                    dispatch_budget.pop("min_runtime_seconds", None)
                remaining_verifiers = max(0, min_verifier_runs - verifier_runs)
                if remaining_verifiers:
                    dispatch_budget["min_verifier_runs"] = remaining_verifiers
                else:
                    dispatch_budget.pop("min_verifier_runs", None)

                refresh_dispatch = refresh_next_reason is not None
                prior_agent_session_id = agent_session_id
                dispatch_request = {
                    **request,
                    "redispatch": (
                        False
                        if refresh_dispatch
                        else bool(request.get("redispatch"))
                        if dispatch_count == 0
                        else True
                    ),
                    "refresh_session": refresh_dispatch,
                    "resume_agent_session_id": (
                        None if refresh_dispatch else agent_session_id
                    ),
                    "worker_budget": dispatch_budget,
                }
                previous_verifier_runs = verifier_runs
                result = run_pi_search_candidate(**dispatch_request)
                dispatch_count += 1
                if result.get("ok") is False:
                    failure = result.get("failure") or {
                        "stage": "pool_worker",
                        "error_type": "CandidateDriverFailure",
                        "message": str(result.get("error") or "Pi candidate driver failed"),
                    }
                    with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
                        write_json(
                            _job_dir(root_dir, pool_id, job_id) / "result.json",
                            result,
                        )
                        job = _load_job(root_dir, pool_id, job_id)
                        job.update(
                            {
                                "status": "failed",
                                "finished_at": utc_timestamp(),
                                "error": failure,
                            }
                        )
                        _write_job(root_dir, pool_id, job)
                    return 1

                returned_session_id = result.get("agent_session_id")
                if not isinstance(returned_session_id, str) or not returned_session_id:
                    raise RuntimeError("Pi candidate driver returned no agent_session_id")
                if (
                    prior_agent_session_id is not None
                    and returned_session_id != prior_agent_session_id
                    and not refresh_dispatch
                ):
                    raise RuntimeError(
                        "Pi candidate continuation changed the native agent session"
                    )
                if (
                    refresh_dispatch
                    and prior_agent_session_id is not None
                    and returned_session_id == prior_agent_session_id
                ):
                    raise RuntimeError(
                        "Pi candidate refresh reused the prior native agent session"
                    )
                if refresh_dispatch:
                    session_refreshes.append(
                        {
                            "dispatch": dispatch_count,
                            "reason": refresh_next_reason,
                            "from_agent_session_id": prior_agent_session_id,
                            "to_agent_session_id": returned_session_id,
                        }
                    )
                agent_session_id = returned_session_id
                if agent_session_id not in agent_session_ids:
                    agent_session_ids.append(agent_session_id)
                verifier_run_baselines.setdefault(agent_session_id, 0)
                observed_verifier_runs[agent_session_id] = max(
                    observed_verifier_runs.get(agent_session_id, 0),
                    max(
                        0,
                        _lease_verifier_runs(result)
                        - verifier_run_baselines[agent_session_id],
                    ),
                )
                verifier_runs = sum(observed_verifier_runs.values())
                refresh_next_reason = _session_refresh_reason(result)
                if verifier_runs > previous_verifier_runs:
                    no_progress_dispatches = 0
                else:
                    no_progress_dispatches += 1
                elapsed = max(0.0, time.monotonic() - lease_started)
                runtime_complete = elapsed >= min_runtime_seconds
                verifier_complete = verifier_runs >= min_verifier_runs
                lease_satisfied = runtime_complete and verifier_complete
                if elapsed >= max_runtime_seconds:
                    release_reason = "max_runtime_reached"
                    break
                if lease_satisfied:
                    release_reason = (
                        "minimum_satisfied"
                        if min_runtime_seconds or min_verifier_runs
                        else "no_minimum_lease"
                    )
                    break
                if not _pool_is_open(root_dir, pool_id):
                    release_reason = "pool_closing"
                    break
                backoff_seconds = (
                    0.0
                    if refresh_next_reason is not None
                    else min(
                        _no_progress_backoff_seconds(no_progress_dispatches),
                        max(0.0, max_runtime_seconds - elapsed),
                    )
                )
                if backoff_seconds:
                    time.sleep(backoff_seconds)
                    elapsed = max(0.0, time.monotonic() - lease_started)
                    runtime_complete = elapsed >= min_runtime_seconds
                    verifier_complete = verifier_runs >= min_verifier_runs
                    lease_satisfied = runtime_complete and verifier_complete
                    if elapsed >= max_runtime_seconds:
                        release_reason = "max_runtime_reached"
                        break
                    if lease_satisfied:
                        release_reason = "minimum_satisfied"
                        break

            result = {
                **result,
                "lease": {
                    "satisfied": lease_satisfied,
                    "release_reason": release_reason,
                    "elapsed_seconds": elapsed,
                    "min_runtime_seconds": min_runtime_seconds,
                    "configured_min_runtime_seconds": (
                        configured_min_runtime_seconds
                    ),
                    "max_runtime_seconds": max_runtime_seconds,
                    "verifier_runs": verifier_runs,
                    "min_verifier_runs": min_verifier_runs,
                    "dispatch_count": dispatch_count,
                    "agent_session_id": agent_session_id,
                    "agent_session_ids": agent_session_ids,
                    "session_refreshes": session_refreshes,
                    "pending_session_refresh_reason": refresh_next_reason,
                },
            }
        except _PoolWorkerInterrupted:
            with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
                job = _load_job(root_dir, pool_id, job_id)
                job.update(
                    {
                        "status": "interrupted",
                        "finished_at": utc_timestamp(),
                        "error": {
                            "stage": "supervisor",
                            "error_type": "WorkerInterrupted",
                            "message": (
                                "Pi pool worker was interrupted by the supervisor"
                            ),
                        },
                    }
                )
                _write_job(root_dir, pool_id, job)
            return 130
        except BaseException as exc:
            with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
                job = _load_job(root_dir, pool_id, job_id)
                job.update(
                    {
                        "status": "failed",
                        "finished_at": utc_timestamp(),
                        "error": {
                            "stage": "pool_worker",
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
                _write_job(root_dir, pool_id, job)
            return 1

        with exclusive_file_lock(_pool_lock_path(root_dir, pool_id)):
            write_json(_job_dir(root_dir, pool_id, job_id) / "result.json", result)
            job = _load_job(root_dir, pool_id, job_id)
            terminal_status = (
                "completed"
                if lease_satisfied
                else "timed_out"
                if release_reason == "max_runtime_reached"
                else "interrupted"
            )
            job.update(
                {
                    "status": terminal_status,
                    "finished_at": utc_timestamp(),
                    "error": (
                        None
                        if terminal_status == "completed"
                        else {
                            "stage": "lease",
                            "error_type": "MinimumLeaseUnsatisfied",
                            "message": (
                                "Pi candidate stopped before its cumulative minimum "
                                "lease was satisfied"
                            ),
                        }
                    ),
                }
            )
            _write_job(root_dir, pool_id, job)
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable Pi worker-pool supervisor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="Run one detached pool worker")
    worker.add_argument("--root", required=True)
    worker.add_argument("--pool-id", required=True)
    worker.add_argument("--job-id", required=True)
    parsed = parser.parse_args(argv)
    if parsed.command == "worker":
        return run_pool_worker(
            root_dir=parsed.root,
            pool_id=parsed.pool_id,
            job_id=parsed.job_id,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

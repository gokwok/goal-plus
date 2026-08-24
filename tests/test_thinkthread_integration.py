from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import pytest

from goal_plus import thinkthread_pool
from goal_plus.models import FsSnapshotArtifactRef, RunState, SearchSpec
from goal_plus.runtime import FileSearchRuntime
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from goal_plus.thinkthread_pool import (
    _TryControllerLock,
    _controller_lock_path,
    _load_job,
    _request_hash,
    _restore_job_branch,
    _response_chunks,
    _send_retained_wake,
    _write_job,
    close_pool,
    continue_pool,
    open_pool,
    wait_any,
)
from tests._runtime_helpers import make_project


class FakeRootAgentPosixClient:
    def __init__(self) -> None:
        self.operations: list[tuple[str, dict]] = []
        self.spawn_params: list[dict] = []
        self.children: list[dict] = []
        self.snapshots: set[str] = {"fsnap-baseline"}
        self.fs_request_status: dict[str, dict] = {}
        self.snapshot_create_completion_unknown_once = False
        self.snapshot_create_terminal_failure_once = False
        self.branch_snapshot_completion_unknown_once = False
        self.branch_snapshot_terminal_failure_once = False
        self.snapshot_remove_completion_unknown_once = False
        self.storage_override: dict[str, int] = {}
        self.replace_completion_unknown_once = False
        self.replace_error_code: str | None = None

    def preflight(self):
        return {"controlProtocolVersion": 2}

    def self_view(self):
        return {
            "thinkthreadId": "tt-root",
            "parentThinkthreadId": None,
            "capabilities": [
                {"id": "thinkthread.child", "version": 1},
                {"id": "thinkthread.message", "version": 1},
                {"id": "thinkthread.fs", "version": 1},
            ],
            "profiles": [],
        }

    def invoke(self, operation: str, params=None, **kwargs):
        normalized = dict(params or {})
        self.operations.append((operation, normalized))
        if operation == "fs.stat":
            result = {
                "kind": "direct",
                "thinkthreadId": "tt-root",
                "state": "attached",
                "storage": {
                    "snapshotCount": len(self.snapshots),
                    "snapshotLimit": 1024,
                    "pendingSnapshotCreations": 0,
                    "snapshotLogicalBytes": len(self.snapshots) * 10,
                    "snapshotLogicalByteLimit": 1024 * 1024,
                    "mutableBranchByteLimit": 1024 * 1024,
                    "requestCount": len(self.fs_request_status),
                    "requestLimit": 1024,
                    "activeRequestCount": 0,
                    "activeRequestLimit": 32,
                },
            }
            result["storage"].update(self.storage_override)
            return result
        if operation == "fs.snapshot.create":
            request_id = normalized["requestId"]
            if self.snapshot_create_terminal_failure_once:
                self.snapshot_create_terminal_failure_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                error = {
                    "code": "FsSnapshotCaptureFailed",
                    "message": "snapshot capture failed",
                }
                self.fs_request_status[request_id] = {
                    "requestId": request_id,
                    "method": "fs.snapshot.create",
                    "state": "failed",
                    "acceptedAtUnixMs": 1,
                    "finishedAtUnixMs": 2,
                    "result": None,
                    "error": error,
                }
                raise AgentPosixBridgeError(
                    "snapshot capture failed",
                    error={"response": {"error": error}},
                )
            result = {
                "snapshotId": "fsnap-baseline",
                "ownerThinkthreadId": "tt-root",
                "createdAtUnixMs": 1,
                "logicalBytes": 10,
            }
            self.fs_request_status[request_id] = {
                "requestId": request_id,
                "method": "fs.snapshot.create",
                "state": "succeeded",
                "acceptedAtUnixMs": 1,
                "finishedAtUnixMs": 2,
                "result": result,
                "error": None,
            }
            if self.snapshot_create_completion_unknown_once:
                self.snapshot_create_completion_unknown_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "root snapshot response was lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
        if operation == "fs.request.close":
            self.fs_request_status.pop(normalized["requestId"], None)
            return {}
        if operation == "fs.request.status":
            status = self.fs_request_status.get(normalized["requestId"])
            if status is None:
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "request missing",
                    error={
                        "response": {
                            "error": {
                                "code": "RequestNotFound",
                                "message": "request missing",
                            }
                        }
                    },
                )
            return dict(status)
        if operation == "thinkthread.list":
            return {"children": list(self.children)}
        if operation == "thinkthread.spawn":
            self.spawn_params.append(normalized)
            index = len(self.spawn_params)
            result = {
                "thinkthreadId": f"tt-child-{index}",
                "capabilities": [{"id": "thinkthread.message", "version": 1}],
                "fs": {
                    "kind": "private",
                    "fsBranchId": f"fsbranch-child-{index}",
                },
                "initialMessageId": f"msg-child-{index}",
            }
            self.children.append(
                {
                    **result,
                    "parentThinkthreadId": "tt-root",
                    "agentState": "running",
                    "executionState": "running",
                    "pendingWake": False,
                }
            )
            return result
        raise AssertionError(operation)


class FakeVerifierAgentPosixClient(FakeRootAgentPosixClient):
    def __init__(self, project: Path) -> None:
        super().__init__()
        self.project = project
        self.run_params: list[dict] = []
        self.root_snapshot_id = "fsnap-baseline"
        self.run_result_override: dict | None = None

    def snapshot_diff_all(self, base_snapshot_id, target_snapshot_id, **kwargs):
        return self.invoke(
            "fs.snapshot.diff",
            {
                "baseSnapshotId": base_snapshot_id,
                "targetSnapshotId": target_snapshot_id,
                "limit": 256,
            },
        )["changes"]

    def snapshot_read_file(self, snapshot_id, path, **kwargs):
        return self._file(snapshot_id, path)

    def invoke(self, operation: str, params=None, **kwargs):
        normalized = dict(params or {})
        if operation in {
            "fs.branch.snapshot",
            "thinkthread.get",
            "fs.snapshot.diff",
            "fs.snapshot.stat",
            "fs.snapshot.pread",
            "fs.run",
            "fs.replace",
            "fs.verify",
            "fs.request.close",
            "fs.request.status",
            "fs.snapshot.remove",
        }:
            self.operations.append((operation, normalized))
        else:
            return super().invoke(operation, params, **kwargs)
        if operation == "fs.branch.snapshot":
            request_id = normalized["requestId"]
            if self.branch_snapshot_terminal_failure_once:
                self.branch_snapshot_terminal_failure_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                error = {
                    "code": "FsSnapshotCaptureFailed",
                    "message": "branch snapshot capture failed",
                }
                self.fs_request_status[request_id] = {
                    "requestId": request_id,
                    "method": "fs.branch.snapshot",
                    "state": "failed",
                    "acceptedAtUnixMs": 1,
                    "finishedAtUnixMs": 2,
                    "result": None,
                    "error": error,
                }
                raise AgentPosixBridgeError(
                    "branch snapshot capture failed",
                    error={"response": {"error": error}},
                )
            result = {
                "snapshotId": "fsnap-attempt",
                "ownerThinkthreadId": "tt-root",
                "createdFromBranchId": normalized["branchId"],
                "createdAtUnixMs": 2,
                "logicalBytes": 20,
            }
            self.snapshots.add(result["snapshotId"])
            self.fs_request_status[request_id] = {
                "requestId": request_id,
                "method": "fs.branch.snapshot",
                "state": "succeeded",
                "acceptedAtUnixMs": 1,
                "finishedAtUnixMs": 2,
                "result": result,
                "error": None,
            }
            if self.branch_snapshot_completion_unknown_once:
                self.branch_snapshot_completion_unknown_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "branch snapshot response was lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
        if operation == "thinkthread.get":
            return {
                "thinkthreadId": normalized["id"],
                "executionState": "running",
                "agentState": "busy",
            }
        if operation == "fs.snapshot.diff":
            return {
                "changes": [
                    {
                        "path": {
                            "utf8": "initial_program.py",
                            "bytesBase64": "aW5pdGlhbF9wcm9ncmFtLnB5",
                        },
                        "kind": "modified",
                    }
                ],
                "hasMore": False,
                "nextCursor": None,
            }
        if operation == "fs.snapshot.stat":
            path = normalized["path"]
            data = self._file(normalized["snapshotId"], path)
            return {
                "path": {"utf8": path, "bytesBase64": ""},
                "kind": "file",
                "mode": 0o644,
                "len": len(data),
            }
        if operation == "fs.snapshot.pread":
            data = self._file(normalized["snapshotId"], normalized["path"])
            offset = int(normalized.get("offset", 0))
            length = int(normalized.get("length", 65536))
            chunk = data[offset : offset + length]
            return {
                "dataBase64": base64.b64encode(chunk).decode("ascii"),
                "bytesRead": len(chunk),
                "eof": offset + len(chunk) >= len(data),
            }
        if operation == "fs.run":
            self.run_params.append(normalized)
            if self.run_result_override is not None:
                return dict(self.run_result_override)
            payload = json.dumps({"combined_score": 1.5}) + "\n"
            midpoint = len(payload) // 2
            return {
                "exit": {"kind": "code", "code": 0},
                "outputChunks": [
                    {
                        "sequence": 2,
                        "stream": "stdout",
                        "dataBase64": base64.b64encode(
                            payload[midpoint:].encode()
                        ).decode("ascii"),
                    },
                    {
                        "sequence": 1,
                        "stream": "stderr",
                        "dataBase64": base64.b64encode(b"diagnostic\n").decode(
                            "ascii"
                        ),
                    },
                    {
                        "sequence": 0,
                        "stream": "stdout",
                        "dataBase64": base64.b64encode(
                            payload[:midpoint].encode()
                        ).decode("ascii"),
                    },
                ],
                "outputTruncated": False,
                "retainedOutputBytes": len(payload) + 11,
                "observedOutputBytes": len(payload) + 11,
                "runKey": "run-key-1",
                "metrics": {},
            }
        if operation == "fs.replace":
            request_id = normalized["requestId"]
            prior = self.fs_request_status.get(request_id)
            if isinstance(prior, dict) and prior.get("state") == "failed":
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "publication request already failed",
                    error={"response": {"error": dict(prior["error"])}},
                )
            if self.replace_error_code is not None:
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                error = {
                    "code": self.replace_error_code,
                    "message": "publication base does not match",
                }
                self.fs_request_status[request_id] = {
                    "requestId": request_id,
                    "method": "fs.replace",
                    "state": "failed",
                    "acceptedAtUnixMs": 1,
                    "finishedAtUnixMs": 2,
                    "result": None,
                    "error": error,
                }
                raise AgentPosixBridgeError(
                    "publication base does not match",
                    error={"response": {"error": error}},
                )
            if self.root_snapshot_id != normalized["baseSnapshotId"]:
                raise AssertionError("unexpected publication base")
            self.root_snapshot_id = normalized["targetSnapshotId"]
            result = {
                "status": "replaced",
                "baseSnapshotId": normalized["baseSnapshotId"],
                "targetSnapshotId": normalized["targetSnapshotId"],
            }
            self.fs_request_status[request_id] = {
                "requestId": request_id,
                "method": "fs.replace",
                "state": "succeeded",
                "acceptedAtUnixMs": 1,
                "finishedAtUnixMs": 2,
                "result": result,
                "error": None,
            }
            if self.replace_completion_unknown_once:
                self.replace_completion_unknown_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "publication response was lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
        if operation == "fs.verify":
            assert normalized["dependencies"] == [
                {"path": ".", "scope": "tree_content"}
            ]
            return {
                "status": (
                    "matched"
                    if normalized["snapshotId"] == self.root_snapshot_id
                    else "stale"
                ),
                "durationMs": 1,
                "comparedEntries": 3,
                "comparedBytes": 20,
            }
        if operation == "fs.request.close":
            self.fs_request_status.pop(normalized["requestId"], None)
            return {}
        if operation == "fs.request.status":
            status = self.fs_request_status.get(normalized["requestId"])
            if status is None:
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "request missing",
                    error={
                        "response": {
                            "error": {
                                "code": "RequestNotFound",
                                "message": "request missing",
                            }
                        }
                    },
                )
            return dict(status)
        if operation == "fs.snapshot.remove":
            snapshot_id = normalized["snapshotId"]
            request_id = normalized["requestId"]
            if snapshot_id not in self.snapshots:
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "snapshot missing",
                    error={
                        "response": {
                            "error": {
                                "code": "FsSnapshotNotFound",
                                "message": "snapshot missing",
                            }
                        }
                    },
                )
            self.snapshots.remove(snapshot_id)
            self.fs_request_status[request_id] = {
                "requestId": request_id,
                "method": "fs.snapshot.remove",
                "state": "succeeded",
                "acceptedAtUnixMs": 1,
                "finishedAtUnixMs": 2,
                "result": {},
                "error": None,
            }
            if self.snapshot_remove_completion_unknown_once:
                self.snapshot_remove_completion_unknown_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "snapshot remove response was lost",
                    error={"delivery": "completion_unknown"},
                )
            return {}
        raise AssertionError(operation)

    def _file(self, snapshot_id: str, path: str) -> bytes:
        if path == "evaluator.py":
            return (self.project / path).read_bytes()
        if path == "initial_program.py":
            return (
                b"VALUE = 1\n"
                if snapshot_id == "fsnap-attempt"
                else b"VALUE = 0\n"
            )
        if path == ".tmp/tool-drafts/probe.py":
            return b"def probe(value):\n    return value + 1\n"
        raise AssertionError((snapshot_id, path))


class StatefulPoolAgentPosixClient(FakeVerifierAgentPosixClient):
    def __init__(self, project: Path) -> None:
        super().__init__(project)
        self.messages: dict[str, list[dict]] = {}
        self.sent_messages: list[dict] = []
        self.branch_states: dict[str, dict] = {}
        self.snapshot_index = 0
        self.message_index = 0
        self.run_scores: list[float] = []
        self.drop_execution_on_snapshot = False
        self.spawn_completion_unknown_once = False
        self.wake_completion_unknown_once = False
        self.wait_timeouts_before_absent = 0
        self.wait_rejections_before_absent = 0
        self.reset_blocked_once = False
        self.branch_snapshot_terminal_failure_once = False

    def set_child_absent(self, child_id: str, *, failed: bool = False) -> None:
        child = self._child(child_id)
        child["executionState"] = "absent"
        child["agentState"] = "failed" if failed else "ready"
        child["completion"] = {"kind": "completed", "summary": "turn ended"}

    def enqueue(self, child_id: str, payload: dict, *, sender: str | None = None) -> str:
        self.message_index += 1
        message_id = f"msg-worker-{self.message_index}"
        self.messages.setdefault(child_id, []).append(
            {
                "messageId": message_id,
                "senderThinkthreadId": sender or child_id,
                "text": json.dumps(payload, separators=(",", ":")),
                "truncated": False,
            }
        )
        return message_id

    def _child(self, child_id: str) -> dict:
        return next(
            child
            for child in self.children
            if child.get("thinkthreadId") == child_id
        )

    def invoke(self, operation: str, params=None, **kwargs):
        normalized = dict(params or {})
        if operation == "thinkthread.spawn":
            result = super().invoke(operation, normalized, **kwargs)
            branch_id = result["fs"]["fsBranchId"]
            self.messages[result["thinkthreadId"]] = []
            self.branch_states[branch_id] = {
                "branchId": branch_id,
                "thinkthreadId": result["thinkthreadId"],
                "baseSnapshotId": normalized["fsSnapshotId"],
                "controlGeneration": 0,
                "state": "attached",
                "storage": {},
            }
            if self.spawn_completion_unknown_once:
                self.spawn_completion_unknown_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "spawn response was lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
        if operation == "thinkthread.get":
            self.operations.append((operation, normalized))
            child_id = normalized["id"]
            try:
                return dict(self._child(child_id))
            except StopIteration as exc:
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "child missing",
                    error={
                        "response": {
                            "error": {
                                "code": "ThinkThreadNotFound",
                                "message": "child missing",
                            }
                        }
                    },
                ) from exc
        if operation == "message.receive":
            self.operations.append((operation, normalized))
            child_id = normalized["senderThinkthreadId"]
            after = int(normalized.get("after") or 0)
            queued = self.messages.get(child_id, [])
            retained = queued[after : after + int(normalized.get("limit", 64))]
            return {
                "messages": [dict(message) for message in retained],
                "nextCursor": str(after + len(retained)),
            }
        if operation == "message.send":
            self.operations.append((operation, normalized))
            self.sent_messages.append(normalized)
            self.message_index += 1
            if normalized.get("wake") is True:
                child = self._child(normalized["recipientThinkthreadId"])
                child["executionState"] = "running"
                child["agentState"] = "busy"
                if self.wake_completion_unknown_once:
                    self.wake_completion_unknown_once = False
                    from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                    raise AgentPosixBridgeError(
                        "wake response was lost",
                        error={"delivery": "completion_unknown"},
                    )
            return {"messageId": f"msg-root-{self.message_index}"}
        if operation == "fs.branch.snapshot":
            self.operations.append((operation, normalized))
            if self.branch_snapshot_terminal_failure_once:
                self.branch_snapshot_terminal_failure_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                request_id = normalized["requestId"]
                error = {
                    "code": "FsSnapshotCaptureFailed",
                    "message": "branch snapshot capture failed",
                }
                self.fs_request_status[request_id] = {
                    "requestId": request_id,
                    "method": "fs.branch.snapshot",
                    "state": "failed",
                    "acceptedAtUnixMs": 1,
                    "finishedAtUnixMs": 2,
                    "result": None,
                    "error": error,
                }
                raise AgentPosixBridgeError(
                    "branch snapshot capture failed",
                    error={"response": {"error": error}},
                )
            self.snapshot_index += 1
            result = {
                "snapshotId": f"fsnap-attempt-{self.snapshot_index}",
                "ownerThinkthreadId": "tt-root",
                "createdFromBranchId": normalized["branchId"],
                "createdAtUnixMs": 2 + self.snapshot_index,
                "logicalBytes": 20,
            }
            self.snapshots.add(result["snapshotId"])
            if self.drop_execution_on_snapshot:
                branch = self.branch_states[normalized["branchId"]]
                self.set_child_absent(branch["thinkthreadId"])
                self.drop_execution_on_snapshot = False
            return result
        if operation == "fs.run" and self.run_scores:
            self.operations.append((operation, normalized))
            self.run_params.append(normalized)
            payload = json.dumps(
                {"combined_score": self.run_scores.pop(0)}
            ) + "\n"
            return {
                "exit": {"kind": "code", "code": 0},
                "outputChunks": [
                    {
                        "sequence": 0,
                        "stream": "stdout",
                        "dataBase64": base64.b64encode(payload.encode()).decode(
                            "ascii"
                        ),
                    }
                ],
                "outputTruncated": False,
                "retainedOutputBytes": len(payload),
                "observedOutputBytes": len(payload),
                "runKey": f"run-key-{len(self.run_params)}",
                "metrics": {},
            }
        if operation == "fs.branch.stat":
            self.operations.append((operation, normalized))
            return dict(self.branch_states[normalized["branchId"]])
        if operation == "fs.branch.reset":
            self.operations.append((operation, normalized))
            if self.reset_blocked_once:
                self.reset_blocked_once = False
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "branch reset blocked",
                    error={
                        "response": {
                            "error": {
                                "code": "FsResetBlocked",
                                "message": "branch reset blocked",
                            }
                        }
                    },
                )
            branch = self.branch_states[normalized["branchId"]]
            assert branch["controlGeneration"] == normalized["ifGeneration"]
            branch["baseSnapshotId"] = normalized["toSnapshotId"]
            branch["controlGeneration"] += 1
            return dict(branch)
        if operation == "workflow.fs.snapshot.patchBytes":
            self.operations.append((operation, normalized))
            self.snapshot_index += 1
            snapshot_id = f"fsnap-patched-{self.snapshot_index}"
            self.snapshots.add(snapshot_id)
            return {
                "requestId": normalized["requestId"],
                "snapshot": {
                    "snapshotId": snapshot_id,
                    "ownerThinkthreadId": "tt-root",
                    "createdAtUnixMs": 20 + self.snapshot_index,
                    "logicalBytes": 40,
                },
            }
        if operation == "fs.branch.remove":
            self.operations.append((operation, normalized))
            branch = self.branch_states[normalized["branchId"]]
            assert branch["controlGeneration"] == normalized["ifGeneration"]
            branch["state"] = "removed"
            branch["controlGeneration"] += 1
            return dict(branch)
        if operation == "thinkthread.signal":
            self.operations.append((operation, normalized))
            return {}
        if operation == "thinkthread.wait":
            self.operations.append((operation, normalized))
            if self.wait_rejections_before_absent > 0:
                self.wait_rejections_before_absent -= 1
                from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

                raise AgentPosixBridgeError(
                    "wait timed out",
                    error={
                        "response": {
                            "error": {
                                "code": "WaitTimeout",
                                "message": "wait timed out",
                            }
                        }
                    },
                )
            if self.wait_timeouts_before_absent > 0:
                self.wait_timeouts_before_absent -= 1
                return {
                    "child": dict(self._child(normalized["id"])),
                    "timedOut": True,
                }
            self.set_child_absent(normalized["id"])
            return {"child": dict(self._child(normalized["id"]))}
        if operation == "thinkthread.destroy":
            self.operations.append((operation, normalized))
            self.children = [
                child
                for child in self.children
                if child.get("thinkthreadId") != normalized["id"]
            ]
            return {}
        return super().invoke(operation, normalized, **kwargs)

    def _file(self, snapshot_id: str, path: str) -> bytes:
        if path == "initial_program.py" and snapshot_id.startswith(
            ("fsnap-attempt-", "fsnap-patched-")
        ):
            return b"VALUE = 1\n"
        return super()._file(snapshot_id, path)


def thinkthread_spec(project: Path) -> SearchSpec:
    return SearchSpec.model_validate(
        {
            "objective": "test runtime",
            "metric_name": "combined_score",
            "metric_direction": "maximize",
            "source_path": str(project),
            "edit_surface": {
                "allow": ["initial_program.py"],
                "deny": ["evaluator.py", "config.yaml"],
            },
            "budget": {"max_parallel": 2},
            "process_verifiers": [
                {
                    "name": "score",
                    "role": "ranking_signal",
                    "command": ["python", "evaluator.py"],
                    "timeout_seconds": 30,
                }
            ],
            "strategy": {
                "name": "random",
                "worker_host": "pi-thinkthread",
                "worker_budget": {
                    "max_runtime_seconds": 300,
                    "on_exceed": "interrupt",
                },
            },
            "shared_dir": {"enabled": True},
        }
    )


def worker_request(
    *,
    request_id: str,
    agent_session_id: str,
    tool: str,
    params: dict,
) -> dict:
    content = {
        "agent_session_id": agent_session_id,
        "tool": tool,
        "params": params,
    }
    content_json = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "protocol": "goal-plus.pi-thinkthread.v2",
        "type": "request",
        "request_id": request_id,
        **content,
        "content_json": content_json,
        "content_sha256": hashlib.sha256(
            content_json.encode("utf-8")
        ).hexdigest(),
    }


def test_worker_rpc_content_hash_accepts_unicode_and_json_number_spelling() -> None:
    request = worker_request(
        request_id="request-unicode",
        agent_session_id="session-unicode",
        tool="search_run_verifier",
        params={"hypothesis": "优化性能", "future_numeric_value": 1.0e-7},
    )

    assert _request_hash(request) == request["content_sha256"]


def test_second_pool_controller_does_not_process_the_same_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = StatefulPoolAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.start_agent_session(run_id, task.candidate_id)
    opened = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )

    with _TryControllerLock(
        _controller_lock_path(runtime.root_dir, opened["pool_id"])
    ) as acquired:
        assert acquired is True
        result = wait_any(
            root_dir=runtime.root_dir,
            pool_id=opened["pool_id"],
            timeout_seconds=0,
            client=client,
        )
        with pytest.raises(RuntimeError, match="controller is busy"):
            continue_pool(
                root_dir=runtime.root_dir,
                pool_id=opened["pool_id"],
                candidate_id=task.candidate_id,
                client=client,
            )
        with pytest.raises(RuntimeError, match="controller is busy"):
            close_pool(
                root_dir=runtime.root_dir,
                pool_id=opened["pool_id"],
                mode="interrupt",
                client=client,
            )

    assert result["event"] is None
    assert result["controller_busy"] is True
    assert not any(operation == "message.receive" for operation, _ in client.operations)


@pytest.mark.pi
def test_run_baseline_and_candidates_use_one_exact_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])

    run_id = runtime.create_run(frozen.frozen_spec_id)
    run = runtime._load_run(run_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    assert run.baseline_artifact_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-baseline"
    )
    assert run.fs_source_relative_path == "."
    baseline_request = run.fs_requests[0]
    assert baseline_request.operation == "root_snapshot"
    assert baseline_request.state == "closed"
    assert client.operations == [
        ("fs.stat", {}),
        (
            "fs.snapshot.create",
            {"requestId": baseline_request.request_id},
        ),
        ("fs.request.close", {"requestId": baseline_request.request_id}),
    ]
    assert len(tasks) == 2
    assert {task.fs_base_snapshot_id for task in tasks} == {"fsnap-baseline"}
    assert all(task.workspace is None for task in tasks)
    assert all(task.workspace_backend is None for task in tasks)
    assert not (project / ".gp-test" / "runs" / run_id / "workspace").exists()


@pytest.mark.pi
def test_root_snapshot_completion_unknown_recovers_by_persisted_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    client.snapshot_create_completion_unknown_once = True
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])

    run_id = runtime.create_run(frozen.frozen_spec_id)
    run = runtime._load_run(run_id)
    request = run.fs_requests[0]
    intent = run.fs_snapshot_intents[0]

    assert request.operation == "root_snapshot"
    assert request.state == "closed"
    assert request.result == {
        "snapshotId": "fsnap-baseline",
        "ownerThinkthreadId": "tt-root",
        "createdAtUnixMs": 1,
        "logicalBytes": 10,
    }
    assert intent.request_id == request.request_id
    assert intent.snapshot_id == "fsnap-baseline"
    assert [
        params["requestId"]
        for operation, params in client.operations
        if operation == "fs.snapshot.create"
    ] == [request.request_id]
    assert (
        "fs.request.status",
        {"requestId": request.request_id},
    ) in client.operations


@pytest.mark.pi
def test_root_snapshot_request_recovers_after_goal_plus_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with runtime._run_transaction(run_id):
        run = runtime._load_run(run_id)
        request = run.fs_requests[0]
        request.state = "prepared"
        request.result = None
        request.closed_at = None
        intent = run.fs_snapshot_intents[0]
        intent.state = "platform_mutation_started"
        intent.snapshot_id = None
        run.baseline_artifact_ref = None
        run.state = RunState.NEEDS_RECOVERY
        run.budget_used["fs_recovery_previous_state"] = RunState.RUNNING.value
        run.budget_used["needs_recovery_reason"] = (
            f"fs.snapshot.create request {request.request_id} simulated process crash"
        )
        runtime._write_run(run)
    result = {
        "snapshotId": "fsnap-baseline",
        "ownerThinkthreadId": "tt-root",
        "createdAtUnixMs": 1,
        "logicalBytes": 10,
    }
    client.fs_request_status[request.request_id] = {
        "requestId": request.request_id,
        "method": "fs.snapshot.create",
        "state": "succeeded",
        "acceptedAtUnixMs": 1,
        "finishedAtUnixMs": 2,
        "result": result,
        "error": None,
    }

    recovered = runtime.recover_pi_thinkthread_snapshot_requests(run_id)

    run = runtime._load_run(run_id)
    assert recovered["failed"] == []
    assert recovered["resolved"] == [
        {
            "request_id": request.request_id,
            "snapshot_id": "fsnap-baseline",
        }
    ]
    assert run.state == RunState.RUNNING
    assert run.baseline_artifact_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-baseline"
    )
    assert run.fs_requests[0].state == "closed"
    assert run.fs_snapshot_intents[0].snapshot_id == "fsnap-baseline"


@pytest.mark.pi
def test_snapshot_recovery_does_not_clear_an_unrelated_recovery_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    with runtime._run_transaction(run_id):
        run = runtime._load_run(run_id)
        run.state = RunState.NEEDS_RECOVERY
        run.budget_used["fs_recovery_previous_state"] = RunState.RUNNING.value
        run.budget_used["needs_recovery_reason"] = (
            "publication request req-unrelated requires reconciliation"
        )
        runtime._write_run(run)

    recovered = runtime.recover_pi_thinkthread_snapshot_requests(run_id)

    run = runtime._load_run(run_id)
    assert recovered["failed"] == []
    assert run.state == RunState.NEEDS_RECOVERY
    assert "publication request" in run.budget_used["needs_recovery_reason"]


@pytest.mark.pi
def test_terminal_root_snapshot_failure_is_failed_not_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    client.snapshot_create_terminal_failure_once = True
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])

    with pytest.raises(Exception, match="snapshot capture failed|request failed"):
        runtime.create_run(frozen.frozen_spec_id)

    run_dirs = list((runtime.root_dir / "runs").glob("run_*"))
    assert len(run_dirs) == 1
    run = runtime._load_run(run_dirs[0].name)
    assert run.state == RunState.FAILED
    assert run.fs_snapshot_intents[0].state == "failed"
    assert run.fs_requests[0].state == "failed"


@pytest.mark.pi
def test_terminal_branch_snapshot_failure_is_failed_not_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    task = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=1).plan_id,
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    opened = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    client.branch_snapshot_terminal_failure_once = True

    with pytest.raises(Exception, match="branch snapshot capture failed|request failed"):
        runtime.run_verifier(
            run_id,
            task.candidate_id,
            agent_session_id=session.agent_session_id,
            hypothesis="capture terminal failure",
        )

    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.fs_snapshot_intents[-1].state == "failed"
    request = runtime._load_run(run_id).fs_requests[-1]
    assert request.operation == "branch_snapshot"
    assert request.state == "failed"


@pytest.mark.pi
def test_terminal_shared_copy_snapshot_failure_marks_requirement_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TerminalCaptureFailureRuntime:
        def capture_pi_thinkthread_branch_snapshot(self, **_kwargs):
            raise AgentPosixBridgeError(
                "shared copy snapshot capture failed",
                error={
                    "response": {
                        "error": {
                            "code": "FsSnapshotCaptureFailed",
                            "message": "shared copy snapshot capture failed",
                        }
                    }
                },
            )

    job = {
        "job_id": "job-shared-copy",
        "run_id": "run-shared-copy",
        "candidate_id": "c001",
        "fs_branch_id": "fsbr-shared-copy",
        "copy_requirements": [
            {"state": "copy_required", "receipt_id": "copy-receipt"}
        ],
    }
    monkeypatch.setattr(
        thinkthread_pool,
        "_ensure_branch_mutation_execution_absent",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        thinkthread_pool,
        "_write_job",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(AgentPosixBridgeError, match="snapshot capture failed"):
        thinkthread_pool._apply_job_tool_copies(
            TerminalCaptureFailureRuntime(),
            object(),
            tmp_path,
            "pool-shared-copy",
            job,
        )

    requirement = job["copy_requirements"][0]
    assert requirement["state"] == "failed"
    assert "snapshot capture failed" in requirement["error"]


@pytest.mark.pi
def test_pool_spawns_private_message_only_children_from_same_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    assert runtime._load_agent_sessions(run_id) == []

    snapshot = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id for task in tasks],
        client=client,
    )
    sessions = sorted(
        runtime._load_agent_sessions(run_id),
        key=lambda item: item.candidate_id,
    )

    assert snapshot["host"] == "pi-thinkthread"
    assert snapshot["active_count"] == 2
    assert snapshot["free_slots"] == 0
    assert snapshot["terminal_count"] == 0
    assert snapshot["undelivered_terminal_count"] == 0
    assert len(client.spawn_params) == 2
    assert {item["fsSnapshotId"] for item in client.spawn_params} == {
        "fsnap-baseline"
    }
    assert all(item["fs"] == "private" for item in client.spawn_params)
    assert all(
        item["capabilities"] == ["thinkthread.message"]
        for item in client.spawn_params
    )
    for task, session in zip(tasks, sessions, strict=True):
        record = runtime._load_candidate_record(run_id, task.candidate_id)
        bound = runtime._load_agent_session_by_id(session.agent_session_id)
        assert record.task.fs_branch_id is not None
        assert bound.host_handle.external_id is not None


@pytest.mark.pi
def test_pool_settles_completed_wake_while_retained_runtime_stays_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = StatefulPoolAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.start_agent_session(run_id, task.candidate_id)
    pool = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        final_verify=False,
        client=client,
    )
    job = _load_job(
        runtime.root_dir,
        pool["pool_id"],
        pool["jobs"][0]["job_id"],
    )
    child = client._child(job["thinkthread_id"])
    child.update(
        {
            "agentState": "ready",
            "executionState": "running",
            "pendingWake": False,
            "lastWakeOutcome": {
                "messageId": job["active_message_id"],
                "outcome": "completed",
                "finishedAtUnixMs": 1,
            },
        }
    )

    completed = wait_any(
        root_dir=runtime.root_dir,
        pool_id=pool["pool_id"],
        timeout_seconds=1,
        client=client,
    )

    assert completed["event"]["kind"] == "candidate_ready"
    settled = _load_job(
        runtime.root_dir,
        pool["pool_id"],
        pool["jobs"][0]["job_id"],
    )
    assert settled["wake_outcome"]["messageId"] == job["active_message_id"]
    assert client._child(job["thinkthread_id"])["executionState"] == "running"


@pytest.mark.pi
def test_turn_boundary_final_verifier_stops_resident_runtime_before_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = StatefulPoolAgentPosixClient(project)
    client.run_scores = [2.0]
    monkeypatch.setattr(
        FileSearchRuntime,
        "_agent_posix_client",
        lambda _runtime: client,
    )
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.start_agent_session(run_id, task.candidate_id)
    pool = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    job = _load_job(
        runtime.root_dir,
        pool["pool_id"],
        pool["jobs"][0]["job_id"],
    )
    child = client._child(job["thinkthread_id"])
    child.update(
        {
            "agentState": "ready",
            "executionState": "running",
            "pendingWake": False,
            "lastWakeOutcome": {
                "messageId": job["active_message_id"],
                "outcome": "completed",
                "finishedAtUnixMs": 1,
            },
        }
    )

    completed = wait_any(
        root_dir=runtime.root_dir,
        pool_id=pool["pool_id"],
        timeout_seconds=1,
        client=client,
    )

    assert completed["event"]["kind"] == "candidate_ready"
    job = _load_job(
        runtime.root_dir,
        pool["pool_id"],
        pool["jobs"][0]["job_id"],
    )
    assert job["final_verifier"]["process_passed"] is True
    assert job["final_verifier_boundary"]["state"] == "settled"
    ordered = [
        operation
        for operation, _params in client.operations
        if operation
        in {"thinkthread.signal", "thinkthread.wait", "fs.branch.snapshot"}
    ]
    assert ordered[-3:] == [
        "thinkthread.signal",
        "thinkthread.wait",
        "fs.branch.snapshot",
    ]


@pytest.mark.pi
def test_successor_start_batch_ignores_legacy_git_ledger_for_fs_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    first_run_id = runtime.create_run(frozen.frozen_spec_id)
    first_plan = runtime.plan_next(first_run_id, requested_k=1)
    first_task = runtime.start_batch(first_run_id, first_plan.plan_id)[0]
    assert first_task.workspace is None

    runtime.invalidate_run(
        first_run_id,
        reason="verifier_infrastructure_failure",
        summary="worker dispatch was interrupted before session creation",
        evidence=[{"stage": "pool_open"}],
    )
    successor_id = runtime.create_run(
        frozen.frozen_spec_id,
        source_run_id=first_run_id,
    )
    successor_plan = runtime.plan_next(successor_id, requested_k=1)
    successor = runtime.start_batch(successor_id, successor_plan.plan_id)[0]

    assert successor.workspace is None
    record = runtime._load_candidate_record(successor_id, successor.candidate_id)
    assert record.results_ledger == []


@pytest.mark.pi
def test_pool_rejects_exhausted_durable_request_quota_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeRootAgentPosixClient()
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.start_agent_session(run_id, task.candidate_id)
    client.storage_override.update({"requestCount": 1024, "requestLimit": 1024})

    with pytest.raises(RuntimeError, match="durable request quota is exhausted"):
        open_pool(
            root_dir=runtime.root_dir,
            run_id=run_id,
            candidate_ids=[task.candidate_id],
            client=client,
        )

    assert client.spawn_params == []


@pytest.mark.pi
def test_spawn_completion_unknown_reconciles_by_registration_nonce_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = StatefulPoolAgentPosixClient(project)
    client.spawn_completion_unknown_once = True
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    runtime.start_agent_session(run_id, task.candidate_id)

    pool = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    job_id = pool["jobs"][0]["job_id"]
    job = _load_job(runtime.root_dir, pool["pool_id"], job_id)
    assert job["status"] == "needs_recovery"
    # Reproduce a hard controller exit after platform admission but before the
    # exception/result was persisted.
    job["status"] = "starting"
    job["spawn_intent"]["state"] = "platform_mutation_started"
    _write_job(runtime.root_dir, pool["pool_id"], job)
    child_id = client.children[0]["thinkthreadId"]
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.pi-thinkthread.v2",
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )

    reconciled = wait_any(
        root_dir=runtime.root_dir,
        pool_id=pool["pool_id"],
        timeout_seconds=0,
        client=client,
    )

    assert reconciled["event"] is None
    job = _load_job(runtime.root_dir, pool["pool_id"], job_id)
    assert job["status"] == "running"
    assert job["thinkthread_id"] == child_id
    assert job["spawn_intent"]["state"] == "bound_after_recovery"
    assert len(client.spawn_params) == 1


@pytest.mark.pi
def test_exact_snapshot_verifier_uses_durable_fs_run_and_closes_after_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    monkeypatch.setenv("PATH", "/task-venv/bin:/usr/bin:/bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/task-venv")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="test exact snapshot",
    )

    assert report.process_passed is True
    assert report.aggregate_score == 1.5
    assert report.disposition == "keep"
    assert report.best_artifact_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-attempt"
    )
    assert len(client.run_params) == 1
    invocation = client.run_params[0]
    assert invocation["snapshotId"] == "fsnap-attempt"
    assert invocation["writes"] == "discard"
    assert invocation["invocation"]["argv"][1:3] == [
        "-m",
        "goal_plus.revision_verifier",
    ]
    assert invocation["invocation"]["environment"] == {
        "PATH": "/task-venv/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": "/task-venv",
    }
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.iterations[0].attempt_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-attempt"
    )
    assert record.iterations[0].changed_files == ["initial_program.py"]
    assert record.settled_artifact_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-attempt"
    )
    run = runtime._load_run(run_id)
    assert [request.operation for request in run.fs_requests] == [
        "root_snapshot",
        "branch_snapshot",
        "run",
    ]
    assert all(request.state == "closed" for request in run.fs_requests)
    assert all(request.result is not None for request in run.fs_requests)
    assert record.iterations[0].verifier_request_ids == [
        run.fs_requests[1].request_id,
        run.fs_requests[2].request_id,
    ]


@pytest.mark.pi
def test_branch_snapshot_completion_unknown_recovers_without_duplicate_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    client.branch_snapshot_completion_unknown_once = True
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="recover exact snapshot capture",
    )

    assert report.process_passed is True
    run = runtime._load_run(run_id)
    request = next(
        item for item in run.fs_requests if item.operation == "branch_snapshot"
    )
    intent = runtime._load_candidate_record(
        run_id, task.candidate_id
    ).fs_snapshot_intents[0]
    assert request.state == "closed"
    assert intent.request_id == request.request_id
    assert intent.snapshot_id == "fsnap-attempt"
    assert [
        params["requestId"]
        for operation, params in client.operations
        if operation == "fs.branch.snapshot"
    ] == [request.request_id]
    assert (
        "fs.request.status",
        {"requestId": request.request_id},
    ) in client.operations


@pytest.mark.pi
def test_exact_snapshot_resource_lock_covers_execution_and_result_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    lock_active = False

    @contextmanager
    def fake_lock(_resource):
        nonlocal lock_active
        assert lock_active is False
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    original_parse = runtime._parse_metrics

    def parse_while_locked(stdout: str):
        assert lock_active is True
        return original_parse(stdout)

    monkeypatch.setattr("goal_plus.runtime.verifier_resource_lock", fake_lock)
    monkeypatch.setattr(runtime, "_parse_metrics", parse_while_locked)

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="prove the verifier lock includes parsing",
    )

    assert report.process_passed is True
    assert lock_active is False


@pytest.mark.pi
@pytest.mark.parametrize(
    ("exit_status", "truncated", "failure_class"),
    [
        ({"kind": "code", "code": 7}, False, "VerifierCommandFailed"),
        ({"kind": "signal", "signal": 9}, False, "VerifierSignal"),
        ({"kind": "timeout"}, False, "Timeout"),
        ({"kind": "cancelled"}, False, "VerifierCancelled"),
        ({"kind": "killed"}, False, "VerifierKilled"),
        ({"kind": "code", "code": 0}, True, "VerifierInfrastructureFailure"),
    ],
)
def test_exact_snapshot_verifier_normalizes_all_terminal_exits_and_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_status: dict,
    truncated: bool,
    failure_class: str,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    payload = json.dumps({"combined_score": 1.5}) + "\n"
    client.run_result_override = {
        "exit": exit_status,
        "outputChunks": [
            {
                "sequence": 0,
                "stream": "stdout",
                "dataBase64": base64.b64encode(payload.encode()).decode("ascii"),
            }
        ],
        "outputTruncated": truncated,
        "retainedOutputBytes": len(payload),
        "observedOutputBytes": len(payload),
        "runKey": "run-key-terminal",
        "metrics": {},
    }
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="normalize an exact verifier terminal exit",
    )

    assert report.process_passed is False
    assert report.verifier_results[0].failure_class == failure_class
    if truncated:
        assert report.verifier_results[0].metrics["infrastructure_failure"] is True
    request = runtime._load_run(run_id).fs_requests[-1]
    assert request.operation == "run"
    assert request.state == "closed"


@pytest.mark.pi
def test_exact_snapshot_launch_failure_is_durable_infrastructure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    original_invoke = client.invoke

    def fail_run_once(operation: str, params=None, **kwargs):
        normalized = dict(params or {})
        if operation == "fs.run":
            request_id = normalized["requestId"]
            error = {
                "code": "FsRunLaunchFailed",
                "message": "exact Fs process could not be launched",
                "retryable": True,
            }
            client.fs_request_status[request_id] = {
                "requestId": request_id,
                "method": "fs.run",
                "state": "failed",
                "acceptedAtUnixMs": 1,
                "finishedAtUnixMs": 2,
                "result": None,
                "error": error,
            }
            from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError

            raise AgentPosixBridgeError(
                "fs.run was rejected: FsRunLaunchFailed",
                error={"response": {"error": error}},
            )
        return original_invoke(operation, params, **kwargs)

    monkeypatch.setattr(client, "invoke", fail_run_once)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="record a transient exact execution launch failure",
    )

    assert report.process_passed is False
    result = report.verifier_results[0]
    assert result.failure_class == "VerifierInfrastructureFailure"
    assert result.metrics["error_code"] == "FsRunLaunchFailed"
    assert result.metrics["retryable"] is True
    request = runtime._load_run(run_id).fs_requests[-1]
    assert request.state == "closed"
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.iterations[-1].failure_class == "VerifierInfrastructureFailure"


@pytest.mark.pi
def test_selection_and_strict_publication_bind_the_exact_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="publish exact snapshot",
    )

    selection = runtime.select(run_id)
    manifest_path = runtime.promote(run_id, task.candidate_id)

    assert selection["selected_artifact_ref"] == {
        "kind": "fs_snapshot",
        "snapshot_id": "fsnap-attempt",
    }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["base_artifact_ref"]["snapshot_id"] == "fsnap-baseline"
    assert manifest["target_artifact_ref"]["snapshot_id"] == "fsnap-attempt"
    assert client.root_snapshot_id == "fsnap-attempt"
    run = runtime._load_run(run_id)
    assert run.state == "promoted"
    assert run.publication is not None
    assert run.publication.state == "committed"
    assert run.fs_requests[-1].operation == "replace"
    assert run.fs_requests[-1].state == "closed"

    report_path = runtime.report(run_id)
    report = report_path.read_text(encoding="utf-8")
    assert '"kind":"fs_snapshot","snapshot_id":"fsnap-attempt"' in report
    assert "durable ledger (1 rows)" in report
    assert report_path.with_suffix(".html").is_file()


@pytest.mark.pi
def test_publication_response_loss_replays_terminal_request_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="publish despite a lost response",
    )
    runtime.select(run_id)
    client.replace_completion_unknown_once = True

    manifest = runtime.promote(run_id, task.candidate_id)

    assert manifest.is_file()
    assert client.root_snapshot_id == "fsnap-attempt"
    assert sum(
        operation == "fs.replace" for operation, _params in client.operations
    ) == 1
    assert any(
        operation == "fs.request.status"
        for operation, _params in client.operations
    )
    run = runtime._load_run(run_id)
    assert run.state == "promoted"
    assert run.publication is not None
    assert run.publication.state == "committed"
    assert run.fs_requests[-1].state == "closed"


@pytest.mark.pi
def test_publication_recovery_reconciles_selected_root_before_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    spec_payload = thinkthread_spec(project).model_dump(mode="json")
    spec_payload["promotion_verifiers"] = list(spec_payload["process_verifiers"])
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_payload),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    task = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=1).plan_id,
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="publish after exact promotion evidence",
    )
    runtime.select(run_id)
    original_commit = runtime._commit_pi_thinkthread_publication
    monkeypatch.setattr(
        runtime,
        "_commit_pi_thinkthread_publication",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated crash before publication manifest")
        ),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        runtime.promote(run_id, task.candidate_id)

    assert client.root_snapshot_id == "fsnap-attempt"
    verifier_runs_before_recovery = len(client.run_params)
    monkeypatch.setattr(runtime, "_commit_pi_thinkthread_publication", original_commit)
    original_invoke = client.invoke

    def reject_redundant_verifier(operation: str, params=None, **kwargs):
        if operation == "fs.run":
            raise AssertionError("publication recovery reran promotion verifier")
        return original_invoke(operation, params, **kwargs)

    monkeypatch.setattr(client, "invoke", reject_redundant_verifier)

    manifest_path = runtime.promote(run_id, task.candidate_id)

    assert manifest_path.is_file()
    assert len(client.run_params) == verifier_runs_before_recovery
    assert runtime._load_run(run_id).publication.state == "committed"


@pytest.mark.pi
def test_publication_conflict_never_overwrites_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="detect a strict publication conflict",
    )
    runtime.select(run_id)
    client.root_snapshot_id = "fsnap-unrelated-root"
    client.replace_error_code = "FsBaseMismatch"

    with pytest.raises(RuntimeError, match="WorkspacePublicationConflict"):
        runtime.promote(run_id, task.candidate_id)

    assert client.root_snapshot_id == "fsnap-unrelated-root"
    run = runtime._load_run(run_id)
    assert run.state == "ready_to_promote"
    assert run.publication is not None
    assert run.publication.state == "outcome_unknown"
    assert run.publication.manifest == {
        "status": "conflict",
        "request_id": run.publication.request_id,
        "baseline_matches": False,
        "selected_matches": False,
        "recorded_at": run.publication.manifest["recorded_at"],
    }

    failed_request_id = run.publication.request_id
    client.root_snapshot_id = "fsnap-baseline"
    client.replace_error_code = None

    manifest_path = runtime.promote(run_id, task.candidate_id)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "committed"
    assert manifest["request_id"] != failed_request_id
    run = runtime._load_run(run_id)
    attempts = {
        item.request_id: item
        for item in run.fs_requests
        if item.operation == "replace"
    }
    assert attempts[failed_request_id].state == "closed"
    assert attempts[manifest["request_id"]].state == "closed"
    assert client.root_snapshot_id == "fsnap-attempt"


@pytest.mark.pi
def test_terminal_report_reclaims_owned_snapshots_after_pool_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = StatefulPoolAgentPosixClient(project)
    client.run_scores = [1.5]
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    pool = open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="publish and reclaim exact snapshots",
    )
    runtime.select(run_id)
    runtime.promote(run_id, task.candidate_id)

    deferred_report = runtime.report(run_id)
    deferred_run = runtime._load_run(run_id)
    assert any(
        item.get("kind") == "snapshot_cleanup"
        and item.get("state") == "deferred_pool_open"
        for item in deferred_run.fs_cleanup
    )
    assert client.snapshots == {"fsnap-baseline", "fsnap-attempt-1"}

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=pool["pool_id"],
        mode="interrupt",
        client=client,
    )
    client.snapshot_remove_completion_unknown_once = True
    report_path = runtime.report(run_id)

    assert report_path == deferred_report
    assert client.snapshots == set()
    remove_calls = [
        params
        for operation, params in client.operations
        if operation == "fs.snapshot.remove"
    ]
    assert [item["snapshotId"] for item in remove_calls] == [
        "fsnap-attempt-1",
        "fsnap-baseline",
    ]
    run = runtime._load_run(run_id)
    cleanup = next(
        item
        for item in reversed(run.fs_cleanup)
        if item.get("kind") == "snapshot_cleanup"
    )
    assert cleanup["state"] == "complete"
    assert cleanup["pending"] == []
    assert cleanup["storage"]["snapshotCount"] == 0
    remove_requests = [
        item for item in run.fs_requests if item.operation == "snapshot_remove"
    ]
    assert len(remove_requests) == 2
    assert all(item.state == "closed" for item in remove_requests)
    assert any(
        operation == "fs.request.status"
        for operation, _params in client.operations
    )
    assert all(
        intent.state == "cleaned"
        for intent in run.fs_snapshot_intents
        if intent.snapshot_id is not None
    )
    candidate = runtime._load_candidate_record(run_id, task.candidate_id)
    assert all(
        intent.state == "cleaned"
        for intent in candidate.fs_snapshot_intents
        if intent.snapshot_id is not None
    )
    assert '"snapshotCount":0' in report_path.read_text(encoding="utf-8")


@pytest.mark.pi
def test_external_evidence_binds_exact_fs_snapshot_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "external"
    directory.mkdir()
    monkeypatch.setenv("GOAL_PLUS_EXTERNAL_EVIDENCE_DIR", str(directory))
    payload = {
        "source": "official-judge",
        "artifact": {
            "source": "goal_plus_best",
            "run_id": "run_1",
            "candidate_id": "c001",
            "iteration": 1,
            "artifact_ref": {
                "kind": "fs_snapshot",
                "snapshot_id": "fsnap-attempt-1",
            },
        },
        "evaluation": {"status": "completed", "score": 97},
    }
    (directory / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    evidence = [
        {
            "candidate_id": "c001",
            "iteration": 1,
            "artifact_ref": {
                "kind": "fs_snapshot",
                "snapshot_id": "fsnap-attempt-1",
            },
        }
    ]

    FileSearchRuntime.attach_external_evaluations("run_1", evidence)

    assert evidence[0]["external_evaluations"] == [
        {"status": "completed", "score": 97, "source": "official-judge"}
    ]


@pytest.mark.pi
def test_shared_tool_is_published_from_the_exact_attempt_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    runtime = FileSearchRuntime(project / ".gp-test")
    client = FakeVerifierAgentPosixClient(project)
    monkeypatch.setattr(runtime, "_agent_posix_client", lambda: client)
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    open_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[task.candidate_id],
        client=client,
    )
    staged = runtime.stage_shared_tool(
        session.agent_session_id,
        name="probe",
        summary="Reusable deterministic probe",
        entrypoint="probe.py:probe",
        candidate_relative_source_paths=[".tmp/tool-drafts/probe.py"],
    )

    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="publish exact shared tool",
        toolization_decision={
            "outcome": "staged",
            "signals": ["domain_probe"],
            "rationale": "Peers can reuse the deterministic probe.",
            "tool_names": ["probe"],
        },
    )

    assert staged["staging_path"].startswith("snapshot://next/")
    assert report.shared_tool_publish_status == "published"
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.pending_fs_tool_stages == []
    assert len(record.iterations[0].shared_tools) == 1
    tool = record.iterations[0].shared_tools[0]
    assert tool.source_artifact_ref == FsSnapshotArtifactRef(
        snapshot_id="fsnap-attempt"
    )
    assert (tool.read_only_path / "probe.py").read_text(encoding="utf-8").startswith(
        "def probe"
    )


@pytest.mark.pi

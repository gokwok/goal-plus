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

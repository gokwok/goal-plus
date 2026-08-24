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


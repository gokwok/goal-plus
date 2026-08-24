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



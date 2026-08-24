from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid


AGENT_POSIX_CONTRACT_FINGERPRINT = (
    "fcc80b665cd990f9d1e3681a9d384cb99994f2b739cd4fbddc97bdda01391131"
)
AGENT_POSIX_CONTROL_PROTOCOL_VERSION = 2


class AgentPosixBridgeError(RuntimeError):
    def __init__(self, message: str, *, error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error = error or {}

    @property
    def delivery(self) -> str | None:
        value = self.error.get("delivery")
        return value if isinstance(value, str) else None

    @property
    def completion_unknown(self) -> bool:
        if self.delivery == "completion_unknown":
            return True
        cause = self.error.get("cause")
        return (
            isinstance(cause, dict)
            and cause.get("delivery") == "completion_unknown"
        )

    @property
    def rejection(self) -> dict[str, Any] | None:
        value = self.error.get("response") or self.error.get("rejection")
        return value if isinstance(value, dict) else None

    @property
    def code(self) -> str | None:
        rejection = self.rejection
        nested = rejection.get("error") if rejection else None
        if isinstance(nested, dict) and isinstance(nested.get("code"), str):
            return str(nested["code"])
        return None

    @property
    def retryable(self) -> bool | None:
        rejection = self.rejection
        nested = rejection.get("error") if rejection else None
        if isinstance(nested, dict) and isinstance(nested.get("retryable"), bool):
            return bool(nested["retryable"])
        return None


class AgentPosixSdkMismatch(AgentPosixBridgeError):
    pass


def new_request_id() -> str:
    return f"req-{uuid.uuid4()}"


class AgentPosixSdkClient:
    """Thin process bridge to ThinkThread's official TypeScript SDK.

    The bridge owns no protocol DTOs and never accesses the Control socket
    directly. Each call executes exactly one validated SDK operation in the
    caller's Root or Child execution domain.
    """

    def __init__(
        self,
        *,
        bridge_path: Path | str | None = None,
        node: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        configured_bridge = bridge_path or os.environ.get(
            "GOAL_PLUS_AGENT_POSIX_BRIDGE"
        )
        self.bridge_path = (
            Path(configured_bridge).expanduser().resolve()
            if configured_bridge
            else Path(__file__).resolve().parent
            / "assets"
            / "thinkthread-agent-posix-bridge.mjs"
        )
        self.node = node or os.environ.get("GOAL_PLUS_NODE", "node")
        self.environment = environment

    def _env(self) -> dict[str, str]:
        if self.environment is None:
            return os.environ.copy()
        return dict(self.environment)

    def invoke(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if not self.bridge_path.is_file():
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge is missing: {self.bridge_path}"
            )
        request = json.dumps(
            {"operation": operation, "params": params or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                [self.node, str(self.bridge_path)],
                input=request,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge requires Node.js: {self.node}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge timed out during {operation}",
                error={
                    "category": "transport",
                    "delivery": "completion_unknown",
                    "operation": operation,
                },
            ) from exc
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            diagnostic = (completed.stderr or completed.stdout).strip()
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge returned invalid JSON during {operation}: "
                f"{diagnostic or f'exit {completed.returncode}'}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge returned an invalid envelope during {operation}"
            )
        if payload["ok"] is not True:
            error = payload.get("error")
            normalized = error if isinstance(error, dict) else {}
            message = normalized.get("message")
            raise AgentPosixBridgeError(
                str(message or f"Agent POSIX operation {operation} failed"),
                error=normalized,
            )
        if completed.returncode != 0:
            raise AgentPosixBridgeError(
                f"Agent POSIX SDK bridge exited {completed.returncode} after success"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AgentPosixBridgeError(
                f"Agent POSIX operation {operation} returned a non-object result"
            )
        return result

    def preflight(self) -> dict[str, Any]:
        result = self.invoke("bridge.meta")
        fingerprint = result.get("contractFingerprint")
        protocol = result.get("controlProtocolVersion")
        if (
            fingerprint != AGENT_POSIX_CONTRACT_FINGERPRINT
            or protocol != AGENT_POSIX_CONTROL_PROTOCOL_VERSION
        ):
            raise AgentPosixSdkMismatch(
                "unsupported ThinkThread Agent POSIX SDK contract: "
                f"fingerprint={fingerprint!r}, protocol={protocol!r}"
            )
        return result

    def self_view(self) -> dict[str, Any]:
        return self.invoke("self")

    def snapshot_diff_all(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        *,
        page_limit: int = 256,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        changes: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "baseSnapshotId": base_snapshot_id,
                "targetSnapshotId": target_snapshot_id,
                "limit": page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = self.invoke("fs.snapshot.diff", params)
            raw_changes = page.get("changes")
            if not isinstance(raw_changes, list):
                raise AgentPosixBridgeError("fs.snapshot.diff omitted changes")
            changes.extend(item for item in raw_changes if isinstance(item, dict))
            if not page.get("hasMore"):
                return changes
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AgentPosixBridgeError(
                    "fs.snapshot.diff hasMore=true without nextCursor"
                )
            cursor = next_cursor

    def snapshot_read_file(
        self,
        snapshot_id: str,
        path: str,
        *,
        max_bytes: int = 1024 * 1024,
        chunk_bytes: int = 64 * 1024,
    ) -> bytes:
        offset = 0
        output = bytearray()
        while True:
            result = self.invoke(
                "fs.snapshot.pread",
                {
                    "snapshotId": snapshot_id,
                    "path": path,
                    "offset": offset,
                    "length": min(chunk_bytes, max_bytes + 1 - len(output)),
                },
            )
            encoded = result.get("dataBase64")
            if not isinstance(encoded, str):
                raise AgentPosixBridgeError("fs.snapshot.pread omitted dataBase64")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned invalid base64"
                ) from exc
            output.extend(chunk)
            if len(output) > max_bytes:
                raise ValueError(f"snapshot file exceeds {max_bytes} bytes: {path}")
            if result.get("eof") is True:
                return bytes(output)
            bytes_read = result.get("bytesRead")
            if not isinstance(bytes_read, int) or bytes_read <= 0:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned no bytes before EOF"
                )
            offset += bytes_read

    def snapshot_readdir_all(
        self,
        snapshot_id: str,
        path: str,
        *,
        page_limit: int = 256,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        entries: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "snapshotId": snapshot_id,
                "path": path,
                "limit": page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = self.invoke("fs.snapshot.readdir", params)
            raw_entries = page.get("entries")
            if not isinstance(raw_entries, list):
                raise AgentPosixBridgeError("fs.snapshot.readdir omitted entries")
            entries.extend(
                item for item in raw_entries if isinstance(item, dict)
            )
            if not page.get("hasMore"):
                return entries
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AgentPosixBridgeError(
                    "fs.snapshot.readdir hasMore=true without nextCursor"
                )
            cursor = next_cursor

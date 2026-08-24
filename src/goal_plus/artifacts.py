from __future__ import annotations

import difflib
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from typing import Any, Protocol

from goal_plus.models import ArtifactRef, FsSnapshotArtifactRef, GitCommitArtifactRef
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
)
from goal_plus.workspaces import IGNORED_NAMES, IGNORED_SUFFIXES


def candidate_artifact_path(path: str) -> bool:
    parts = Path(path).parts
    if path == "results.tsv":
        return False
    if any(part in IGNORED_NAMES - {".git"} for part in parts):
        return False
    return not any(path.endswith(suffix) for suffix in IGNORED_SUFFIXES)


class ArtifactReader(Protocol):
    def changed_files(self, base: ArtifactRef, target: ArtifactRef) -> list[str]: ...

    def diff(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
        *,
        paths: list[str] | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> str: ...

    def read_file(
        self,
        artifact: ArtifactRef,
        path: str,
        *,
        max_bytes: int = 1024 * 1024,
    ) -> bytes: ...


def _git_ref(reference: ArtifactRef) -> str:
    if not isinstance(reference, GitCommitArtifactRef):
        raise TypeError("GitArtifactReader requires git_commit ArtifactRef")
    return reference.commit


def _snapshot_ref(reference: ArtifactRef) -> str:
    if not isinstance(reference, FsSnapshotArtifactRef):
        raise TypeError("FsSnapshotArtifactReader requires fs_snapshot ArtifactRef")
    return reference.snapshot_id


class GitArtifactReader:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()

    def _run(self, arguments: list[str]) -> str:
        return subprocess.run(
            ["git", "-C", str(self.workspace), *arguments],
            capture_output=True,
            check=True,
            text=True,
        ).stdout

    def _run_bounded(
        self,
        arguments: list[str],
        *,
        max_bytes: int,
    ) -> tuple[bytes, bool]:
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        command = ["git", "-C", str(self.workspace), *arguments]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = bytearray()
        stderr = bytearray()
        truncated = False

        def drain(stream: Any, capture: bytearray, limit: int) -> None:
            nonlocal truncated
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    remaining = max(0, limit - len(capture))
                    if remaining:
                        capture.extend(chunk[:remaining])
                    if len(chunk) > remaining and capture is stdout:
                        truncated = True
            except (OSError, ValueError):
                return

        readers = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, max_bytes + 1),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, 64 * 1024),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        returncode = process.wait()
        for reader in readers:
            reader.join()
        if returncode:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                output=bytes(stdout),
                stderr=bytes(stderr),
            )
        if len(stdout) > max_bytes:
            del stdout[max_bytes:]
            truncated = True
        return bytes(stdout), truncated

    def changed_files(self, base: ArtifactRef, target: ArtifactRef) -> list[str]:
        output = self._run(
            ["diff", "--name-only", _git_ref(base), _git_ref(target), "--"]
        )
        return [
            line
            for line in output.splitlines()
            if line and candidate_artifact_path(line)
        ]

    def diff(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
        *,
        paths: list[str] | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> str:
        if paths == []:
            return ""
        arguments = [
            "diff",
            "--full-index",
            "--no-ext-diff",
            "--function-context",
            "--unified=10",
            _git_ref(base),
            _git_ref(target),
            "--",
        ]
        if paths is not None:
            arguments.extend(paths)
        output, truncated = self._run_bounded(arguments, max_bytes=max_bytes)
        text = output.decode("utf-8", errors="replace")
        return text + ("\n[diff truncated]\n" if truncated else "")

    def read_file(
        self,
        artifact: ArtifactRef,
        path: str,
        *,
        max_bytes: int = 1024 * 1024,
    ) -> bytes:
        output, truncated = self._run_bounded(
            ["show", f"{_git_ref(artifact)}:{path}"],
            max_bytes=max_bytes,
        )
        if truncated:
            raise ValueError(f"artifact file exceeds {max_bytes} bytes: {path}")
        return output


def fs_path_text(value: Any) -> str:
    if not isinstance(value, dict):
        raise AgentPosixBridgeError("Agent POSIX fs path is not an object")
    utf8 = value.get("utf8")
    if not isinstance(utf8, str):
        raise AgentPosixBridgeError(
            "Goal Plus requires UTF-8 fs paths for edit-surface validation"
        )
    return utf8


class FsSnapshotArtifactReader:
    def __init__(self, client: AgentPosixSdkClient) -> None:
        self.client = client

    def _changes_by_path(
        self,
        base: ArtifactRef,
        target: ArtifactRef,
    ) -> dict[str, dict[str, Any]]:
        changes = self.client.snapshot_diff_all(
            _snapshot_ref(base),
            _snapshot_ref(target),
        )
        result: dict[str, dict[str, Any]] = {}
        for raw in changes:
            path = fs_path_text(raw.get("path"))
            if path in result:
                raise AgentPosixBridgeError(
                    f"fs.snapshot.diff returned duplicate path {path!r}"
                )
            result[path] = raw
        return result

    def changed_files(self, base: ArtifactRef, target: ArtifactRef) -> list[str]:
        base_id = _snapshot_ref(base)
        target_id = _snapshot_ref(target)
        changed: list[str] = []
        for path, change in self._changes_by_path(base, target).items():
            if not candidate_artifact_path(path):
                continue
            snapshot_id = base_id if change.get("kind") == "deleted" else target_id
            entry = self.client.invoke(
                "fs.snapshot.stat",
                {"snapshotId": snapshot_id, "path": path},
            )
            if entry.get("kind") != "directory":
                changed.append(path)
        return sorted(changed)

    def _pread_prefix(
        self,
        snapshot_id: str,
        path: str,
        *,
        length: int,
    ) -> bytes:
        output = bytearray()
        while len(output) < length:
            result = self.client.invoke(
                "fs.snapshot.pread",
                {
                    "snapshotId": snapshot_id,
                    "path": path,
                    "offset": len(output),
                    "length": min(64 * 1024, length - len(output)),
                },
            )
            encoded = result.get("dataBase64")
            if not isinstance(encoded, str):
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread omitted dataBase64"
                )
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned invalid base64"
                ) from exc
            if not chunk:
                break
            output.extend(chunk)
        return bytes(output)

    def _file_sha256(
        self,
        snapshot_id: str,
        path: str,
        *,
        expected_length: int,
    ) -> str:
        digest = hashlib.sha256()
        offset = 0
        while offset < expected_length:
            result = self.client.invoke(
                "fs.snapshot.pread",
                {
                    "snapshotId": snapshot_id,
                    "path": path,
                    "offset": offset,
                    "length": min(64 * 1024, expected_length - offset),
                },
            )
            encoded = result.get("dataBase64")
            if not isinstance(encoded, str):
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread omitted dataBase64"
                )
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned invalid base64"
                ) from exc
            if not chunk:
                raise AgentPosixBridgeError(
                    "fs.snapshot.pread returned no bytes before expected EOF"
                )
            digest.update(chunk)
            offset += len(chunk)
        return digest.hexdigest()

    def _project_entry(
        self,
        snapshot_id: str,
        path: str,
        *,
        absent: bool,
        max_bytes: int,
    ) -> bytes:
        if absent:
            return b""
        entry = self.client.invoke(
            "fs.snapshot.stat",
            {"snapshotId": snapshot_id, "path": path},
        )
        kind = entry.get("kind")
        if kind == "file":
            length = entry.get("len")
            if not isinstance(length, int) or length < 0:
                raise AgentPosixBridgeError("fs.snapshot.stat omitted file len")
            if length <= max_bytes:
                return self.client.snapshot_read_file(
                    snapshot_id,
                    path,
                    max_bytes=max_bytes,
                )
            prefix = self._pread_prefix(
                snapshot_id,
                path,
                length=max_bytes,
            )
            return prefix + b"\n[content truncated]\n"
        if kind == "symlink":
            target = fs_path_text(entry.get("symlinkTarget"))
            return (target + "\n").encode("utf-8")
        metadata = json.dumps(
            {
                "kind": kind,
                "mode": entry.get("mode"),
                "len": entry.get("len"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return (metadata + "\n").encode("utf-8")

    def _entry_descriptor(

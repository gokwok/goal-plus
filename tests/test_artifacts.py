from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import subprocess

from goal_plus.artifacts import FsSnapshotArtifactReader, GitArtifactReader
from goal_plus.models import FsSnapshotArtifactRef, GitCommitArtifactRef


def test_git_artifact_reader_projects_exact_commit_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "program.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "program.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    target.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "program.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "attempt",
        ],
        cwd=tmp_path,
        check=True,
    )
    attempt = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    reader = GitArtifactReader(tmp_path)

    assert reader.changed_files(
        GitCommitArtifactRef(commit=base),
        GitCommitArtifactRef(commit=attempt),
    ) == ["program.py"]
    assert "VALUE = 2" in reader.diff(
        GitCommitArtifactRef(commit=base),
        GitCommitArtifactRef(commit=attempt),
    )
    assert reader.read_file(
        GitCommitArtifactRef(commit=attempt), "program.py"
    ) == b"VALUE = 2\n"


class FakeSnapshotClient:
    contents = {
        ("fsnap-base", "program.py"): b"VALUE = 1\n",
        ("fsnap-attempt", "program.py"): b"VALUE = 2\n",
    }

    def snapshot_diff_all(self, base: str, target: str):
        assert (base, target) == ("fsnap-base", "fsnap-attempt")
        return [
            {
                "path": {"utf8": "program.py", "bytesBase64": "cHJvZ3JhbS5weQ=="},
                "kind": "modified",
            }
        ]

    def invoke(self, operation: str, params: dict):
        assert operation == "fs.snapshot.stat"
        data = self.contents[(params["snapshotId"], params["path"])]
        return {
            "path": {"utf8": params["path"], "bytesBase64": ""},
            "kind": "file",
            "mode": 0o644,
            "len": len(data),
        }

    def snapshot_read_file(self, snapshot: str, path: str, *, max_bytes: int):
        data = self.contents[(snapshot, path)]
        assert len(data) <= max_bytes
        return data


def test_fs_snapshot_artifact_reader_projects_exact_snapshot_diff() -> None:
    reader = FsSnapshotArtifactReader(FakeSnapshotClient())  # type: ignore[arg-type]
    base = FsSnapshotArtifactRef(snapshot_id="fsnap-base")
    attempt = FsSnapshotArtifactRef(snapshot_id="fsnap-attempt")

    assert reader.changed_files(base, attempt) == ["program.py"]
    projection = reader.diff(base, attempt)
    assert "-VALUE = 1" in projection
    assert "+VALUE = 2" in projection
    assert reader.read_file(attempt, "program.py") == b"VALUE = 2\n"
    assert len(reader.canonical_digest(base, attempt)) == 64


class StructuralSnapshotClient(FakeSnapshotClient):
    def snapshot_diff_all(self, base: str, target: str):
        assert (base, target) == ("fsnap-base", "fsnap-attempt")
        return [
            {
                "path": {"utf8": "newpkg", "bytesBase64": ""},
                "kind": "added",
            },
            {
                "path": {"utf8": "newpkg/kernel.py", "bytesBase64": ""},
                "kind": "added",
            },
        ]

    def invoke(self, operation: str, params: dict):
        assert operation == "fs.snapshot.stat"
        path = params["path"]
        return {
            "path": {"utf8": path, "bytesBase64": ""},
            "kind": "directory" if path == "newpkg" else "file",
            "mode": 0o755 if path == "newpkg" else 0o644,
            "len": None if path == "newpkg" else 1,
        }


def test_fs_snapshot_changed_files_excludes_directory_inventory_entries() -> None:
    reader = FsSnapshotArtifactReader(StructuralSnapshotClient())  # type: ignore[arg-type]

    assert reader.changed_files(
        FsSnapshotArtifactRef(snapshot_id="fsnap-base"),
        FsSnapshotArtifactRef(snapshot_id="fsnap-attempt"),
    ) == ["newpkg/kernel.py"]


class LargeSnapshotClient:
    length = 64 * 1024 * 1024 + 1

    def snapshot_diff_all(self, base: str, target: str):
        return [
            {
                "path": {"utf8": "large.bin", "bytesBase64": ""},
                "kind": "added",
            }
        ]

    def invoke(self, operation: str, params: dict):
        if operation == "fs.snapshot.stat":
            return {
                "path": {"utf8": params["path"], "bytesBase64": ""},
                "kind": "file",
                "mode": 0o644,
                "len": self.length,
            }
        assert operation == "fs.snapshot.pread"
        remaining = self.length - int(params["offset"])
        chunk = b"x" * min(int(params["length"]), remaining)
        return {
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "bytesRead": len(chunk),
            "eof": len(chunk) == remaining,
        }

    def snapshot_read_file(self, *args, **kwargs):
        raise AssertionError("large files must be projected and hashed incrementally")


def test_fs_snapshot_large_file_diff_is_bounded_and_digest_is_streamed() -> None:
    client = LargeSnapshotClient()
    reader = FsSnapshotArtifactReader(client)  # type: ignore[arg-type]
    base = FsSnapshotArtifactRef(snapshot_id="fsnap-base")
    attempt = FsSnapshotArtifactRef(snapshot_id="fsnap-attempt")

    projection = reader.diff(base, attempt, max_bytes=1024 * 1024)
    assert len(projection.encode("utf-8")) <= 1024 * 1024 + 32
    assert "[diff truncated]" in projection
    expected_file_hash = hashlib.sha256(b"x" * client.length).hexdigest()
    expected_manifest = (
        '[{"kind":"file","len":67108865,"mode":420,"path":"large.bin",'
        f'"sha256":"{expected_file_hash}"}}]'
    )
    assert reader.canonical_digest(base, attempt) == hashlib.sha256(
        expected_manifest.encode("utf-8")
    ).hexdigest()


class TailModifiedSnapshotClient:
    def __init__(self, *, target_suffix: bytes) -> None:
        self.contents = {
            "fsnap-base": b"a" * 1024 + b"X",
            "fsnap-attempt": b"a" * 1024 + target_suffix,
        }

    def snapshot_diff_all(self, base: str, target: str):
        return [
            {
                "path": {"utf8": "tail.bin", "bytesBase64": ""},
                "kind": "modified",
            }
        ]

    def invoke(self, operation: str, params: dict):
        data = self.contents[params["snapshotId"]]
        if operation == "fs.snapshot.stat":
            return {
                "path": {"utf8": params["path"], "bytesBase64": ""},
                "kind": "file",
                "mode": 0o644,
                "len": len(data),
            }
        assert operation == "fs.snapshot.pread"
        offset = int(params["offset"])
        chunk = data[offset : offset + int(params["length"])]
        return {
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "bytesRead": len(chunk),
            "eof": offset + len(chunk) >= len(data),
        }

    def snapshot_read_file(self, *args, **kwargs):
        raise AssertionError("truncated projections must use fs.snapshot.pread")


def test_fs_snapshot_modified_outside_projection_keeps_change_marker() -> None:
    base = FsSnapshotArtifactRef(snapshot_id="fsnap-base")
    attempt = FsSnapshotArtifactRef(snapshot_id="fsnap-attempt")

    same_length = FsSnapshotArtifactReader(
        TailModifiedSnapshotClient(target_suffix=b"Y")  # type: ignore[arg-type]
    ).diff(base, attempt, max_bytes=512)
    assert "bounded artifact change" in same_length
    assert '"change_kind":"modified"' in same_length
    assert '"len":1025' in same_length
    assert "tail.bin" in same_length

    different_length = FsSnapshotArtifactReader(
        TailModifiedSnapshotClient(target_suffix=b"YZ")  # type: ignore[arg-type]
    ).diff(base, attempt, max_bytes=512)
    assert "bounded artifact change" in different_length
    assert '"len":1025' in different_length
    assert '"len":1026' in different_length

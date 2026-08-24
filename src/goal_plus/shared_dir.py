"""Run-scoped, verifier-settled shared tool snapshots.

Candidates stage optional tools under ``.tmp/share-out``.  Only an attributed,
passing process verifier may consume that directory.  Consumption first renames
the whole staging directory into runtime-owned pending storage, so a successful
settlement cannot publish the same staging contents again on the next iteration.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from goal_plus.models import ArtifactRef, SharedToolRecord


SHARE_OUT_RELATIVE_PATH = ".tmp/share-out"
TOOL_DRAFTS_RELATIVE_PATH = ".tmp/tool-drafts"
TOOL_INBOX_RELATIVE_PATH = ".tmp/shared-tools"
SHARED_INDEX_SCHEMA_VERSION = 2
TOOL_VIEW_MAX_CONTENT_BYTES = 256 * 1024
TOOL_VIEW_MAX_FILE_BYTES = 64 * 1024


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:80] or "tool"


def _is_link_or_reparse_point(path: Path) -> bool:
    """Reject links that could make the runtime traverse outside staging."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        reparse_flag and file_attributes & reparse_flag
    )


def _manifest_metadata(tool: Path) -> tuple[str, str | None, str | None]:
    manifest_path = tool / "manifest.json" if tool.is_dir() else None
    payload: dict[str, Any] = {}
    if manifest_path is not None and manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                payload = value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    def text_field(name: str, limit: int) -> str | None:
        value = payload.get(name)
        if not isinstance(value, str):
            return None
        normalized = " ".join(value.split()).strip()
        return normalized[:limit] or None

    return (
        text_field("name", 120) or tool.name,
        text_field("summary", 500),
        text_field("entrypoint", 300),
    )


class _StagedToolResult(TypedDict):
    name: str
    staged_name: str
    staging_path: str
    entrypoint: str
    source_paths: list[str]
    files: list[str]
    file_count: int
    size_bytes: int
    path_count: int


class _StagingInspection(TypedDict):
    entries: list[str]
    file_count: int
    size_bytes: int
    path_count: int
    errors: list[str]


@dataclass(frozen=True)
class _ToolLimits:
    max_tools: int
    max_files: int
    max_bytes: int
    max_path_entries: int
    max_depth: int


@dataclass
class _ToolUsage:
    file_count: int = 0
    size_bytes: int = 0
    path_count: int = 0

    def add(self, *, file_count: int, size_bytes: int, path_count: int) -> None:
        self.file_count += file_count
        self.size_bytes += size_bytes
        self.path_count += path_count


@dataclass(frozen=True)
class _ToolManifest:
    name: str
    summary: str
    entrypoint: str

    def to_bytes(self) -> bytes:
        payload = {
            "name": self.name,
            "summary": self.summary,
            "entrypoint": self.entrypoint,
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )


@dataclass(frozen=True)
class _DraftPlan:
    files: dict[Path, tuple[Path, int]]
    directories: set[Path]
    manifest: _ToolManifest
    manifest_bytes: bytes

    @property
    def file_count(self) -> int:
        return len(self.files) + 1

    @property
    def size_bytes(self) -> int:
        return sum(size for _source, size in self.files.values()) + len(
            self.manifest_bytes
        )

    @property
    def path_count(self) -> int:
        return 1 + len(self.directories) + self.file_count


@dataclass
class SharedDirSettlement:
    tools: list[SharedToolRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    staged_entries: list[str] = field(default_factory=list)
    staged_file_count: int = 0
    staged_bytes: int = 0
    consumed_entries: list[str] = field(default_factory=list)
    deduplicated_entries: list[str] = field(default_factory=list)


@dataclass
class _SettlementBatch:
    result: SharedDirSettlement
    latest_by_source: dict[str, dict[str, Any]]
    physical_by_hash: dict[str, Path]
    new_records: list[SharedToolRecord] = field(default_factory=list)
    created_snapshots: list[Path] = field(default_factory=list)
    processed_entries: list[Path] = field(default_factory=list)
    usage: _ToolUsage = field(default_factory=_ToolUsage)


@dataclass(frozen=True)
class _SnapshotContent:
    ordered_files: list[tuple[str, Path]]
    file_sizes: dict[str, int]
    captured: dict[str, bytearray]

    @property
    def captured_bytes(self) -> int:
        return sum(len(value) for value in self.captured.values())


class SharedDirManager:
    """Snapshot candidate exports into a runtime-owned immutable view.

    File modes are best-effort protection against accidental edits.  They are
    not a security boundary when a worker runs as the same OS user as the
    runtime; host sandboxing or ACL separation must provide that boundary.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.shared_dir = run_dir / "shared"
        self.tools_dir = self.shared_dir / "tools"
        self.index_path = self.shared_dir / "index.json"
        # Pending candidate input is deliberately outside the advertised
        # shared directory, so peers cannot mistake unverified input for a
        # published snapshot.
        self.pending_dir = self.run_dir / ".shared-tool-consume"
        self.publish_temp_dir = self.run_dir / ".shared-tool-publish"
        self.index_temp_dir = self.run_dir / ".shared-tool-index"

    def ensure_layout(self) -> Path:
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_index([])
        return self.shared_dir

    def stage_tool(
        self,
        *,
        workspace: Path,
        share_out_dir: Path,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        max_tools: int,
        max_files: int,
        max_bytes: int,
        max_path_entries: int,
        max_depth: int,
    ) -> _StagedToolResult:
        """Copy explicit candidate-local drafts into bounded verifier staging."""
        limits = _ToolLimits(
            max_tools=max_tools,
            max_files=max_files,
            max_bytes=max_bytes,
            max_path_entries=max_path_entries,
            max_depth=max_depth,
        )
        workspace, draft_root, resolved_draft_root = self._prepare_draft_roots(
            workspace, share_out_dir
        )
        manifest = self._validate_manifest(name, summary, entrypoint)
        selected = self._select_draft_sources(
            candidate_relative_source_paths,
            draft_root=draft_root,
            resolved_draft_root=resolved_draft_root,
            max_files=limits.max_files,
        )
        plan = self._build_draft_plan(
            selected,
            resolved_draft_root=resolved_draft_root,
            manifest=manifest,
            limits=limits,
        )
        self._validate_draft_plan(plan, limits)

        existing = self.inspect_staging(
            share_out_dir,
            max_tools=limits.max_tools,
            max_files=limits.max_files,
            max_bytes=limits.max_bytes,
            max_path_entries=limits.max_path_entries,
            max_depth=limits.max_depth,
        )
        if existing["errors"]:
            raise ValueError(
                "existing share-out is invalid: " + "; ".join(existing["errors"])
            )
        if len(existing["entries"]) >= limits.max_tools:
            raise ValueError(
                f"iteration share-out exceeds {limits.max_tools} top-level tools"
            )

        safe_name = _safe_name(manifest.name)
        destination = share_out_dir / safe_name
        if destination.exists():
            raise ValueError(f"staged tool already exists: {safe_name}")
        files, size_bytes, path_count = self._materialize_draft_plan(
            plan,
            workspace=workspace,
            destination=destination,
            limits=limits,
            existing=existing,
        )

        return {
            "name": manifest.name,
            "staged_name": safe_name,
            "staging_path": str(destination),
            "entrypoint": manifest.entrypoint,
            "source_paths": [
                (Path(TOOL_DRAFTS_RELATIVE_PATH) / relative).as_posix()
                for relative, _source in selected
            ],
            "files": files,
            "file_count": len(files),
            "size_bytes": size_bytes,
            "path_count": path_count,
        }

    @staticmethod
    def _prepare_draft_roots(
        workspace: Path,
        share_out_dir: Path,
    ) -> tuple[Path, Path, Path]:
        workspace = workspace.resolve(strict=True)
        expected_share_out = workspace / SHARE_OUT_RELATIVE_PATH
        if share_out_dir != expected_share_out:
            raise ValueError("share-out path does not match the candidate workspace")
        if _is_link_or_reparse_point(expected_share_out.parent):
            raise ValueError("candidate .tmp directory must be a real directory")
        if _is_link_or_reparse_point(share_out_dir):
            raise ValueError(f"{SHARE_OUT_RELATIVE_PATH} must be a real directory")
        share_out_dir.mkdir(parents=True, exist_ok=True)

        draft_root = workspace / TOOL_DRAFTS_RELATIVE_PATH
        if _is_link_or_reparse_point(draft_root):
            raise ValueError(f"{TOOL_DRAFTS_RELATIVE_PATH} must be a real directory")
        if not draft_root.is_dir():
            raise ValueError(f"{TOOL_DRAFTS_RELATIVE_PATH} does not exist")
        resolved_draft_root = draft_root.resolve(strict=True)
        if not resolved_draft_root.is_relative_to(workspace):
            raise ValueError("tool draft directory escaped the candidate workspace")
        return workspace, draft_root, resolved_draft_root

    @staticmethod
    def _validate_manifest(name: str, summary: str, entrypoint: str) -> _ToolManifest:
        manifest = _ToolManifest(
            name=" ".join(name.split()).strip(),
            summary=" ".join(summary.split()).strip(),
            entrypoint=entrypoint.strip(),
        )
        if not manifest.name or len(manifest.name) > 120:
            raise ValueError("tool name must contain 1-120 characters")
        if not manifest.summary or len(manifest.summary) > 500:
            raise ValueError("tool summary must contain 1-500 characters")
        if not manifest.entrypoint or len(manifest.entrypoint) > 300:
            raise ValueError("tool entrypoint must contain 1-300 characters")
        return manifest

    @staticmethod
    def _select_draft_sources(
        source_paths: list[str],
        *,
        draft_root: Path,
        resolved_draft_root: Path,
        max_files: int,
    ) -> list[tuple[Path, Path]]:
        if not source_paths:
            raise ValueError("candidate_relative_source_paths must not be empty")
        if len(source_paths) > max_files:
            raise ValueError(f"tool source list exceeds {max_files} entries")

        draft_prefix = Path(TOOL_DRAFTS_RELATIVE_PATH)
        selected: list[tuple[Path, Path]] = []
        for raw_path in source_paths:
            relative = Path(raw_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("tool source paths must be candidate-relative without '..'")
            try:
                draft_relative = relative.relative_to(draft_prefix)
            except ValueError as exc:
                raise ValueError(
                    f"tool sources must be under {TOOL_DRAFTS_RELATIVE_PATH}"
                ) from exc
            if draft_relative == Path("."):
                raise ValueError("select explicit entries below the tool draft directory")

            source = draft_root / draft_relative
            current = draft_root
            for part in draft_relative.parts:
                current /= part
                if _is_link_or_reparse_point(current):
                    raise ValueError(
                        "tool sources cannot contain symbolic links or reparse points"
                    )
            try:
                resolved_source = source.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"tool source does not exist: {raw_path}") from exc
            if not resolved_source.is_relative_to(resolved_draft_root):
                raise ValueError("tool source escaped the tool draft directory")
            if not resolved_source.is_file() and not resolved_source.is_dir():
                raise ValueError("tool sources must be regular files or directories")
            selected.append((draft_relative, source))

        selected.sort(key=lambda item: item[0].as_posix())
        for index, (relative, _source) in enumerate(selected):
            if any(
                relative == other or other.is_relative_to(relative)
                for other, _other_source in selected[index + 1 :]
            ):
                raise ValueError("tool source paths must be unique and non-overlapping")
        return selected

    @staticmethod
    def _build_draft_plan(
        selected: list[tuple[Path, Path]],
        *,
        resolved_draft_root: Path,
        manifest: _ToolManifest,
        limits: _ToolLimits,
    ) -> _DraftPlan:
        files: dict[Path, tuple[Path, int]] = {}
        directories: set[Path] = set()
        manifest_bytes = manifest.to_bytes()
        file_count = 1
        size_bytes = len(manifest_bytes)
        path_count = 2

        if file_count > limits.max_files:
            raise ValueError(f"tool exceeds {limits.max_files} files")
        if size_bytes > limits.max_bytes:
            raise ValueError(f"tool exceeds {limits.max_bytes} bytes")
        if path_count > limits.max_path_entries:
            raise ValueError(
                f"tool exceeds {limits.max_path_entries} filesystem entries"
            )

        def add_directory(relative: Path) -> None:
            nonlocal path_count
            if relative in directories:
                return
            if len(relative.parts) > limits.max_depth:
                raise ValueError(
                    f"tool nesting exceeds maximum depth {limits.max_depth}"
                )
            if path_count + 1 > limits.max_path_entries:
                raise ValueError(
                    f"tool exceeds {limits.max_path_entries} filesystem entries"
                )
            directories.add(relative)
            path_count += 1

        def add_parents(relative: Path) -> None:
            parent = relative.parent
            while parent != Path("."):
                add_directory(parent)
                parent = parent.parent

        def visit(source: Path, relative: Path) -> None:
            nonlocal file_count, size_bytes, path_count
            if _is_link_or_reparse_point(source):
                raise ValueError(
                    "tool sources cannot contain symbolic links or reparse points"
                )
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(resolved_draft_root):
                raise ValueError("tool source escaped the tool draft directory")
            if source.is_file():
                add_parents(relative)
                if relative == Path("manifest.json"):
                    raise ValueError("manifest.json is generated by the staging helper")
                if relative in files:
                    raise ValueError("tool source paths resolve to duplicate destinations")
                if len(relative.parts) > limits.max_depth:
                    raise ValueError(
                        f"tool nesting exceeds maximum depth {limits.max_depth}"
                    )
                if file_count + 1 > limits.max_files:
                    raise ValueError(f"tool exceeds {limits.max_files} files")
                source_size = source.stat().st_size
                if size_bytes + source_size > limits.max_bytes:
                    raise ValueError(f"tool exceeds {limits.max_bytes} bytes")
                if path_count + 1 > limits.max_path_entries:
                    raise ValueError(
                        f"tool exceeds {limits.max_path_entries} filesystem entries"
                    )
                files[relative] = (source, source_size)
                file_count += 1
                size_bytes += source_size
                path_count += 1
                return
            if not source.is_dir():
                raise ValueError("tool sources must be regular files or directories")
            add_parents(relative)
            add_directory(relative)
            with os.scandir(source) as entries:
                for child in entries:
                    visit(Path(child.path), relative / child.name)

        for relative, source in selected:
            visit(source, relative)
        if not files:
            raise ValueError("tool sources contain no regular files")
        return _DraftPlan(
            files=files,
            directories=directories,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
        )

    @staticmethod
    def _validate_draft_plan(plan: _DraftPlan, limits: _ToolLimits) -> None:
        if plan.file_count > limits.max_files:
            raise ValueError(f"tool exceeds {limits.max_files} files")
        if plan.size_bytes > limits.max_bytes:
            raise ValueError(f"tool exceeds {limits.max_bytes} bytes")
        if plan.path_count > limits.max_path_entries:
            raise ValueError(
                f"tool exceeds {limits.max_path_entries} filesystem entries"
            )
        if any(
            len(relative.parts) > limits.max_depth
            for relative in [*plan.directories, *plan.files]
        ):
            raise ValueError(f"tool nesting exceeds maximum depth {limits.max_depth}")

        entrypoint = plan.manifest.entrypoint
        entrypoint_file = entrypoint.split(":", 1)[0].strip()
        if (
            "\\" in entrypoint
            or entrypoint.startswith("/")
            or re.match(r"^[A-Za-z]:", entrypoint)
        ):
            raise ValueError("tool entrypoint must use a candidate-relative POSIX path")
        entrypoint_path = Path(entrypoint_file)
        if (
            entrypoint_path.is_absolute()
            or ".." in entrypoint_path.parts
            or entrypoint_path not in plan.files
        ):
            raise ValueError("tool entrypoint must name one staged source file")

    def _materialize_draft_plan(
        self,
        plan: _DraftPlan,
        *,
        workspace: Path,
        destination: Path,
        limits: _ToolLimits,
        existing: _StagingInspection,
    ) -> tuple[list[str], int, int]:
        temporary_root = workspace / ".tmp" / f".shared-tool-stage-{uuid.uuid4().hex}"
        temporary_tool = temporary_root / destination.name
        try:
            temporary_tool.mkdir(parents=True, exist_ok=False)
            (temporary_tool / "manifest.json").write_bytes(plan.manifest_bytes)
            for relative, (source, expected_size) in sorted(
                plan.files.items(), key=lambda item: item[0].as_posix()
            ):
                target = temporary_tool / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copied = self._copy_file_bounded(
                    source,
                    target,
                    max_bytes=limits.max_bytes,
                )
                if copied != expected_size:
                    raise ValueError("tool source changed while it was being copied")
            files, size_bytes, path_count = self._tool_files(
                temporary_tool,
                max_files=limits.max_files,
                max_bytes=limits.max_bytes,
                max_path_entries=limits.max_path_entries,
                max_depth=limits.max_depth,
                files_already=existing["file_count"],
                bytes_already=existing["size_bytes"],
                paths_already=existing["path_count"],
            )
            relative_files = [
                path.relative_to(temporary_tool).as_posix() for path in files
            ]
            os.replace(temporary_tool, destination)
            return relative_files, size_bytes, path_count
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)

    def inspect_staging(
        self,
        share_out_dir: Path,
        *,
        max_tools: int = 16,
        max_files: int = 64,
        max_bytes: int = 2 * 1024 * 1024,
        max_path_entries: int = 512,
        max_depth: int = 8,
        deep: bool = True,
    ) -> _StagingInspection:
        """Inspect staging with hard traversal bounds and no publication.

        ``deep=False`` performs only a capped top-level inventory.  Runtime
        settlement uses that cheap form until verifier validity and worker
        attribution are known.
        """
        entries, paths, errors = self._top_level_entries(
            share_out_dir,
            max_tools=max_tools,
        )
        result: _StagingInspection = {
            "entries": entries,
            "file_count": 0,
            "size_bytes": 0,
            "path_count": 0,
            "errors": errors,
        }
        if errors or not deep:
            return result

        usage = _ToolUsage()
        for entry in paths:
            try:
                files, entry_size, entry_paths = self._tool_files(
                    entry,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_path_entries=max_path_entries,
                    max_depth=max_depth,
                    files_already=usage.file_count,
                    bytes_already=usage.size_bytes,
                    paths_already=usage.path_count,
                )
            except (OSError, ValueError) as exc:
                result["errors"].append(f"{entry.name}: {exc}")
                continue
            usage.add(
                file_count=len(files),
                size_bytes=entry_size,
                path_count=entry_paths,
            )
        result["file_count"] = usage.file_count
        result["size_bytes"] = usage.size_bytes
        result["path_count"] = usage.path_count
        return result

    def settle_iteration(
        self,
        *,
        candidate_id: str,
        iteration: int,
        source_commit: str | None,
        source_artifact_ref: ArtifactRef | None = None,
        share_out_dir: Path,
        max_tools: int,
        max_files: int,
        max_bytes: int,
        max_path_entries: int,
        max_depth: int,
        settlement_id: str | None = None,
    ) -> SharedDirSettlement:
        """Atomically claim staging, publish deltas, and consume accepted input."""
        limits = _ToolLimits(
            max_tools=max_tools,
            max_files=max_files,
            max_bytes=max_bytes,
            max_path_entries=max_path_entries,
            max_depth=max_depth,
        )
        self.ensure_layout()
        if settlement_id is not None:
            recovered = self._load_settlement(
                settlement_id,
                candidate_id=candidate_id,
                iteration=iteration,
            )
            if recovered is not None:
                return recovered
        inventory = self.inspect_staging(
            share_out_dir,
            max_tools=limits.max_tools,
            max_files=limits.max_files,
            max_bytes=limits.max_bytes,
            max_path_entries=limits.max_path_entries,
            max_depth=limits.max_depth,
            deep=False,
        )
        result = SharedDirSettlement(
            staged_entries=list(inventory["entries"]),
            errors=list(inventory["errors"]),
        )
        if result.errors or not result.staged_entries:
            return result

        claim_dir = self._claim_staging(
            share_out_dir,
            candidate_id=candidate_id,
            iteration=iteration,
        )
        existing = self._load_index()
        batch = _SettlementBatch(
            result=result,
            latest_by_source=self._latest_by_source(existing, candidate_id),
            physical_by_hash=self._physical_by_hash(existing),
        )

        try:
            for entry in sorted(claim_dir.iterdir(), key=lambda item: item.name):
                try:
                    self._settle_claimed_entry(
                        batch,
                        entry=entry,
                        claim_dir=claim_dir,
                        candidate_id=candidate_id,
                        iteration=iteration,
                        source_commit=source_commit,
                        source_artifact_ref=source_artifact_ref,
                        limits=limits,
                    )
                except (OSError, ValueError) as exc:
                    result.errors.append(f"{entry.name}: {exc}")
                    self._restore_entry(entry, share_out_dir, result.errors)

            result.tools = batch.new_records
            result.staged_file_count = batch.usage.file_count
            result.staged_bytes = batch.usage.size_bytes
            if batch.new_records or settlement_id is not None:
                self._append_index(
                    batch.new_records,
                    settlement_id=settlement_id,
                    settlement=result,
                    candidate_id=candidate_id,
                    iteration=iteration,
                )
        except Exception:
            # No index publication means none of this batch is durably settled.
            for entry in batch.processed_entries:
                if entry.exists():
                    self._restore_entry(entry, share_out_dir, result.errors)
            for snapshot in reversed(batch.created_snapshots):
                self._remove_unindexed_snapshot(snapshot)
            raise
        finally:
            self._cleanup_claim(claim_dir, batch.processed_entries, result.errors)
        return result

    def _settle_claimed_entry(
        self,
        batch: _SettlementBatch,
        *,
        entry: Path,
        claim_dir: Path,
        candidate_id: str,
        iteration: int,
        source_commit: str | None,
        source_artifact_ref: ArtifactRef | None,
        limits: _ToolLimits,
    ) -> None:
        files, size_bytes, path_entries = self._tool_files(
            entry,
            max_files=limits.max_files,
            max_bytes=limits.max_bytes,
            max_path_entries=limits.max_path_entries,
            max_depth=limits.max_depth,
            files_already=batch.usage.file_count,
            bytes_already=batch.usage.size_bytes,
            paths_already=batch.usage.path_count,
        )
        snapshot_hash, relative_files = self._tool_digest(
            entry,
            files,
            expected_size=size_bytes,
        )
        source_relative_path = entry.relative_to(claim_dir).as_posix()
        previous = batch.latest_by_source.get(source_relative_path)
        if previous and previous.get("snapshot_hash") == snapshot_hash:
            batch.result.deduplicated_entries.append(entry.name)
        else:
            record, created_snapshot = self._publish_tool(
                candidate_id=candidate_id,
                iteration=iteration,
                source_commit=source_commit,
                source_artifact_ref=source_artifact_ref,
                tool=entry,
                source_relative_path=source_relative_path,
                files=files,
                relative_files=relative_files,
                size_bytes=size_bytes,
                snapshot_hash=snapshot_hash,
                physical_by_hash=batch.physical_by_hash,
            )
            batch.new_records.append(record)
            if created_snapshot:
                batch.created_snapshots.append(record.read_only_path)
            batch.latest_by_source[source_relative_path] = record.model_dump(mode="json")
            batch.physical_by_hash[snapshot_hash] = record.read_only_path

        batch.usage.add(
            file_count=len(files),
            size_bytes=size_bytes,
            path_count=path_entries,
        )
        batch.processed_entries.append(entry)
        batch.result.consumed_entries.append(entry.name)

    @staticmethod
    def _cleanup_claim(
        claim_dir: Path,
        processed_entries: list[Path],
        errors: list[str],
    ) -> None:
        if not claim_dir.exists():
            return
        try:
            # Delete only consumed entries. Rejected entries that could not be
            # restored remain in pending storage for manual recovery.
            for entry in processed_entries:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                elif entry.exists() or entry.is_symlink():
                    entry.unlink()
            claim_dir.rmdir()
        except OSError as exc:
            errors.append(f"consumed staging cleanup failed at {claim_dir}: {exc}")

    def tool_view_input(
        self,
        tool: SharedToolRecord,
        *,
        max_content_bytes: int,
    ) -> tuple[dict[str, Any], int]:
        """Build bounded, hash-checked, untrusted input for the annotator."""
        files = self._resolve_snapshot_files(tool)
        try:
            content = self._read_snapshot_content(
                tool,
                files,
                max_content_bytes=max_content_bytes,
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"shared tool snapshot integrity mismatch for {tool.tool_id}: {exc}"
            ) from exc

        manifest, excerpts = self._build_snapshot_excerpts(content)
        return (
            {
                "tool_id": tool.tool_id,
                "snapshot_hash": tool.snapshot_hash,
                "source_commit": tool.source_commit,
                "name": tool.name,
                "summary": tool.summary,
                "entrypoint": tool.entrypoint,
                "files": list(tool.files),
                "size_bytes": tool.size_bytes,
                "manifest": manifest,
                "snapshot_excerpts": excerpts,
                "evidence_warning": (
                    "The candidate iteration passed its process verifier; the tool "
                    "was not independently verified."
                ),
            },
            content.captured_bytes,
        )

    def _resolve_snapshot_files(
        self,
        tool: SharedToolRecord,
    ) -> list[tuple[str, Path]]:
        root = tool.read_only_path
        if not self._safe_snapshot_path(root):
            raise ValueError(f"unsafe shared tool snapshot path for {tool.tool_id}")

        root = root.resolve()
        files: list[tuple[str, Path]] = []
        for value in tool.files:
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe shared tool file path {value!r}")
            path = (root / relative).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(root):
                raise ValueError(f"shared tool file escaped its snapshot: {value!r}")
            if path.relative_to(root).as_posix() != value:
                raise ValueError(f"non-canonical shared tool file path {value!r}")
            files.append((value, path))
        return files

    @staticmethod
    def _read_snapshot_content(
        tool: SharedToolRecord,
        files: list[tuple[str, Path]],
        *,
        max_content_bytes: int,
    ) -> _SnapshotContent:
        entrypoint_file = (tool.entrypoint or "").partition(":")[0]
        ordered = sorted(
            files,
            key=lambda item: (
                item[0] != "manifest.json",
                item[0] != entrypoint_file,
                item[0],
            ),
        )
        remaining = max(0, max_content_bytes)
        capture_limits: dict[str, int] = {}
        for relative, path in ordered:
            limit = min(path.stat().st_size, remaining, TOOL_VIEW_MAX_FILE_BYTES)
            capture_limits[relative] = limit
            remaining -= limit

        digest = hashlib.sha256()
        bytes_read = 0
        file_sizes: dict[str, int] = {}
        captured = {relative: bytearray() for relative, _path in files}
        for relative, path in files:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            file_size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    file_size += len(chunk)
                    bytes_read += len(chunk)
                    if bytes_read > tool.size_bytes:
                        raise ValueError("tool changed while it was being read")
                    digest.update(chunk)
                    excerpt = captured[relative]
                    excerpt.extend(
                        chunk[: max(0, capture_limits[relative] - len(excerpt))]
                    )
            file_sizes[relative] = file_size
        if bytes_read != tool.size_bytes or digest.hexdigest() != tool.snapshot_hash:
            raise ValueError("tool changed while it was being read")
        return _SnapshotContent(
            ordered_files=ordered,
            file_sizes=file_sizes,
            captured=captured,
        )

    @staticmethod
    def _build_snapshot_excerpts(
        content: _SnapshotContent,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        excerpts: list[dict[str, Any]] = []
        manifest: dict[str, Any] | None = None
        for relative, _path in content.ordered_files:
            size = content.file_sizes[relative]
            raw = bytes(content.captured[relative])
            if not raw:
                excerpts.append(
                    {"path": relative, "size_bytes": size, "content_omitted": True}
                )
                continue
            truncated = len(raw) < size
            try:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
                text = decoder.decode(raw, final=not truncated)
            except UnicodeDecodeError:
                excerpts.append(
                    {"path": relative, "size_bytes": size, "binary": True}
                )
                continue
            if relative == "manifest.json" and not truncated:
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        manifest = payload
                        continue
                except json.JSONDecodeError:
                    pass
            excerpts.append(
                {
                    "path": relative,
                    "size_bytes": size,
                    "text": text,
                    "truncated": truncated,
                }
            )
        return manifest, excerpts

    def materialize_tool(
        self,
        tool: SharedToolRecord,
        destination: Path,
    ) -> Path:
        """Copy one hash-verified snapshot into a candidate-local inbox."""
        self.tool_view_input(tool, max_content_bytes=0)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"tool copy destination already exists: {destination}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".copy-{uuid.uuid4().hex}"
        temporary.mkdir(parents=False, exist_ok=False)
        copied_files: list[Path] = []
        copied_bytes = 0
        try:
            root = tool.read_only_path.resolve(strict=True)
            for value in tool.files:
                relative = Path(value)
                source = (root / relative).resolve(strict=True)
                if not source.is_file() or not source.is_relative_to(root):
                    raise ValueError(f"shared tool file escaped its snapshot: {value!r}")
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copied_bytes += self._copy_file_bounded(
                    source,
                    target,
                    max_bytes=tool.size_bytes - copied_bytes,
                )
                copied_files.append(target)
            copied_hash, _ = self._tool_digest(
                temporary,
                copied_files,
                expected_size=tool.size_bytes,
            )
            if copied_hash != tool.snapshot_hash:
                raise ValueError("tool changed while it was being copied")
            os.replace(temporary, destination)
            return destination
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _top_level_entries(
        share_out_dir: Path,
        *,
        max_tools: int,
    ) -> tuple[list[str], list[Path], list[str]]:
        try:
            if _is_link_or_reparse_point(share_out_dir):
                return [], [], [
                    f"{SHARE_OUT_RELATIVE_PATH} must be a real directory"
                ]
            if not share_out_dir.exists():
                return [], [], []
            if not share_out_dir.is_dir():
                return [], [], [
                    f"{SHARE_OUT_RELATIVE_PATH} must be a real directory"
                ]
            paths: list[Path] = []
            with os.scandir(share_out_dir) as entries:
                for entry in entries:
                    if len(paths) >= max_tools:
                        names = sorted(path.name for path in paths)
                        return names, paths, [
                            f"iteration share-out exceeds {max_tools} top-level tools"
                        ]
                    paths.append(Path(entry.path))
        except OSError as exc:
            return [], [], [f"{SHARE_OUT_RELATIVE_PATH} inspection failed: {exc}"]
        paths.sort(key=lambda item: item.name)
        return [path.name for path in paths], paths, []

    @staticmethod
    def _tool_files(
        tool: Path,
        *,
        max_files: int,
        max_bytes: int,
        max_path_entries: int,
        max_depth: int,
        files_already: int = 0,
        bytes_already: int = 0,
        paths_already: int = 0,
    ) -> tuple[list[Path], int, int]:
        if _is_link_or_reparse_point(tool.parent):
            raise ValueError("staging root must be a real directory")
        if _is_link_or_reparse_point(tool):
            raise ValueError("symbolic links and reparse points are not supported")
        staging_root = tool.parent.resolve(strict=True)
        tool_root = tool.resolve(strict=True)
        if not tool_root.is_relative_to(staging_root):
            raise ValueError("tool escaped its staging root")

        files: list[Path] = []
        total_bytes = 0
        path_entries = 0

        def account_file(path: Path) -> None:
            nonlocal total_bytes
            size = path.stat().st_size
            if files_already + len(files) + 1 > max_files:
                raise ValueError(f"iteration share-out exceeds {max_files} files")
            if bytes_already + total_bytes + size > max_bytes:
                raise ValueError(f"iteration share-out exceeds {max_bytes} bytes")
            files.append(path)
            total_bytes += size

        def visit(path: Path, depth: int) -> None:
            nonlocal path_entries
            path_entries += 1
            if paths_already + path_entries > max_path_entries:
                raise ValueError(
                    "iteration share-out exceeds "
                    f"{max_path_entries} filesystem entries"
                )
            if depth > max_depth:
                raise ValueError(
                    f"tool nesting exceeds maximum depth {max_depth}"
                )
            if _is_link_or_reparse_point(path):
                raise ValueError(
                    "symbolic links and reparse points are not supported"
                )
            resolved = path.resolve(strict=True)
            if resolved != tool_root and not resolved.is_relative_to(tool_root):
                raise ValueError("tool entry escaped its staging root")
            if path.is_file():
                account_file(path)
                return
            if not path.is_dir():
                raise ValueError("tool entries must be regular files or directories")
            with os.scandir(path) as children:
                for child in children:
                    visit(Path(child.path), depth + 1)

        visit(tool, 0)
        if not files:
            raise ValueError("tool is empty")
        files.sort(key=lambda path: path.relative_to(tool).as_posix())
        return files, total_bytes, path_entries

    @staticmethod
    def _tool_digest(
        tool: Path,
        files: list[Path],
        *,
        expected_size: int,
    ) -> tuple[str, list[str]]:
        digest = hashlib.sha256()
        tool_is_file = tool.is_file()
        relative_files: list[str] = []
        bytes_read = 0
        for path in files:
            relative = Path(tool.name) if tool_is_file else path.relative_to(tool)
            value = relative.as_posix()
            relative_files.append(value)
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    bytes_read += len(chunk)
                    if bytes_read > expected_size:
                        raise ValueError("tool changed while it was being hashed")
                    digest.update(chunk)
        if bytes_read != expected_size:
            raise ValueError("tool changed while it was being hashed")
        return digest.hexdigest(), relative_files

    def _publish_tool(
        self,
        *,
        candidate_id: str,
        iteration: int,
        source_commit: str | None,
        source_artifact_ref: ArtifactRef | None,
        tool: Path,
        source_relative_path: str,
        files: list[Path],
        relative_files: list[str],
        size_bytes: int,
        snapshot_hash: str,
        physical_by_hash: dict[str, Path],
    ) -> tuple[SharedToolRecord, bool]:
        destination = physical_by_hash.get(snapshot_hash)
        created_snapshot = False
        if destination is None or not self._safe_snapshot_path(destination):
            destination = (
                self.tools_dir / "sha256" / snapshot_hash[:2] / snapshot_hash
            )
            if destination.exists():
                destination = destination.with_name(
                    f"{snapshot_hash}-{uuid.uuid4().hex[:8]}"
                )
            self.publish_temp_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.publish_temp_dir / f"snapshot-{uuid.uuid4().hex}"
            temporary.mkdir(parents=True, exist_ok=False)
            try:
                copied_bytes = 0
                copied_files: list[Path] = []
                for source in files:
                    relative = (
                        Path(tool.name)
                        if tool.is_file()
                        else source.relative_to(tool)
                    )
                    target = temporary / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copied_bytes += self._copy_file_bounded(
                        source,
                        target,
                        max_bytes=size_bytes - copied_bytes,
                    )
                    copied_files.append(target)
                if copied_bytes != size_bytes:
                    raise ValueError("tool changed while it was being copied")
                copied_hash, _ = self._tool_digest(
                    temporary,
                    copied_files,
                    expected_size=size_bytes,
                )
                if copied_hash != snapshot_hash:
                    raise ValueError("tool changed while it was being copied")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, destination)
                self._make_read_only(destination)
                created_snapshot = True
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
                raise

        name, summary, entrypoint = _manifest_metadata(tool)
        identity = hashlib.sha256(
            f"{candidate_id}\0{iteration}\0{source_relative_path}\0{snapshot_hash}".encode(
                "utf-8"
            )
        ).hexdigest()
        return SharedToolRecord(
            tool_id=f"{candidate_id}-i{iteration:04d}-{identity[:16]}",
            candidate_id=candidate_id,
            iteration=iteration,
            source_commit=source_commit,
            source_artifact_ref=source_artifact_ref,
            snapshot_hash=snapshot_hash,
            name=name,
            summary=summary,
            entrypoint=entrypoint,
            source_relative_path=source_relative_path,
            read_only_path=destination.resolve(),
            files=relative_files,
            size_bytes=size_bytes,
            created_at=_utc_timestamp(),
        ), created_snapshot

    @staticmethod
    def _copy_file_bounded(source: Path, target: Path, *, max_bytes: int) -> int:
        """Copy one source file without letting a concurrent append exceed bounds."""
        if _is_link_or_reparse_point(source):
            raise ValueError("tool sources cannot contain symbolic links or reparse points")
        copied = 0
        with source.open("rb") as reader, target.open("xb") as writer:
            if _is_link_or_reparse_point(source):
                raise ValueError(
                    "tool sources cannot contain symbolic links or reparse points"
                )
            opened = os.fstat(reader.fileno())
            current = source.lstat()
            if (
                opened.st_dev != current.st_dev
                or opened.st_ino != current.st_ino
                or not stat.S_ISREG(current.st_mode)
            ):
                raise ValueError("tool source changed before it was copied")
            while True:
                chunk = reader.read(min(1024 * 1024, max_bytes - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise ValueError("tool changed while it was being copied")
                writer.write(chunk)
        return copied

    def _claim_staging(
        self,
        share_out_dir: Path,
        *,
        candidate_id: str,
        iteration: int,
    ) -> Path:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        claim = self.pending_dir / (
            f"{_safe_name(candidate_id)}-i{iteration:04d}-{uuid.uuid4().hex}"
        )
        os.replace(share_out_dir, claim)
        try:
            if _is_link_or_reparse_point(claim):
                raise ValueError(
                    f"{SHARE_OUT_RELATIVE_PATH} must be a real directory"
                )
            share_out_dir.mkdir(parents=True, exist_ok=False)
        except Exception:
            os.replace(claim, share_out_dir)
            raise
        return claim

    @staticmethod
    def _restore_entry(entry: Path, share_out_dir: Path, errors: list[str]) -> None:
        target = share_out_dir / entry.name
        if target.exists():
            errors.append(
                f"{entry.name}: could not restore rejected staging because "
                "a new entry already exists"
            )
            return
        try:
            os.replace(entry, target)
        except OSError as exc:
            errors.append(f"{entry.name}: could not restore rejected staging: {exc}")

    def _safe_snapshot_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
            return resolved.is_dir() and resolved.is_relative_to(
                self.tools_dir.resolve()
            )
        except (OSError, RuntimeError):
            return False

    def _remove_unindexed_snapshot(self, path: Path) -> None:
        """Best-effort rollback for a snapshot not committed to ``index.json``."""
        if not self._safe_snapshot_path(path):
            return
        try:
            for item in sorted(path.rglob("*"), reverse=True):
                try:
                    item.chmod(0o666 if item.is_file() else 0o777)
                except OSError:
                    pass
            path.chmod(0o777)
            shutil.rmtree(path)
        except OSError:
            # A failed rollback leaves an unreachable runtime-owned orphan;
            # it is never referenced by Global Evidence or the shared index.
            pass

    @staticmethod
    def _latest_by_source(
        tools: list[dict[str, Any]],
        candidate_id: str,
    ) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if tool.get("candidate_id") != candidate_id:
                continue
            source = tool.get("source_relative_path")
            if not isinstance(source, str):
                continue
            previous = latest.get(source)
            iteration = tool.get("iteration")
            if previous is None or (
                isinstance(iteration, int)
                and iteration > int(previous.get("iteration", 0))
            ):
                latest[source] = tool
        return latest

    def _physical_by_hash(self, tools: list[dict[str, Any]]) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for tool in tools:
            snapshot_hash = tool.get("snapshot_hash")
            raw_path = tool.get("read_only_path")
            if not isinstance(snapshot_hash, str) or not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if self._safe_snapshot_path(path):
                result.setdefault(snapshot_hash, path)
        return result

    @staticmethod
    def _make_read_only(root: Path) -> None:
        """Apply advisory read-only modes on both POSIX and Windows."""
        try:
            for path in sorted(root.rglob("*"), reverse=True):
                try:
                    path.chmod(0o444 if path.is_file() else 0o555)
                except OSError:
                    pass
            root.chmod(0o555)
        except OSError:
            pass

    @staticmethod
    def _settlement_payload(result: SharedDirSettlement) -> dict[str, Any]:
        return {
            "tools": [item.model_dump(mode="json") for item in result.tools],
            "errors": list(result.errors),
            "staged_entries": list(result.staged_entries),
            "staged_file_count": result.staged_file_count,
            "staged_bytes": result.staged_bytes,
            "consumed_entries": list(result.consumed_entries),
            "deduplicated_entries": list(result.deduplicated_entries),
        }

    @staticmethod
    def _settlement_from_payload(payload: dict[str, Any]) -> SharedDirSettlement:
        return SharedDirSettlement(
            tools=[
                SharedToolRecord.model_validate(item)
                for item in payload.get("tools", [])
                if isinstance(item, dict)
            ],
            errors=[str(item) for item in payload.get("errors", [])],
            staged_entries=[
                str(item) for item in payload.get("staged_entries", [])
            ],
            staged_file_count=int(payload.get("staged_file_count", 0)),
            staged_bytes=int(payload.get("staged_bytes", 0)),
            consumed_entries=[
                str(item) for item in payload.get("consumed_entries", [])
            ],
            deduplicated_entries=[
                str(item) for item in payload.get("deduplicated_entries", [])
            ],
        )

    def _load_index_payload(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"tools": [], "settlements": {}}
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
            raise ValueError("shared tool index has an invalid shape")
        if not isinstance(payload.get("settlements", {}), dict):
            raise ValueError("shared tool settlement index has an invalid shape")
        return payload

    def _load_settlement(
        self,
        settlement_id: str,
        *,
        candidate_id: str,
        iteration: int,
    ) -> SharedDirSettlement | None:
        receipt = self._load_index_payload().get("settlements", {}).get(settlement_id)
        if receipt is None:
            return None
        if (
            not isinstance(receipt, dict)
            or receipt.get("candidate_id") != candidate_id
            or receipt.get("iteration") != iteration
            or not isinstance(receipt.get("result"), dict)
        ):
            raise ValueError("shared tool settlement id is bound to another iteration")
        return self._settlement_from_payload(receipt["result"])

    def _append_index(
        self,
        tools: list[SharedToolRecord],
        *,
        settlement_id: str | None = None,
        settlement: SharedDirSettlement | None = None,
        candidate_id: str | None = None,
        iteration: int | None = None,
    ) -> None:
        payload = self._load_index_payload()
        existing = [item for item in payload["tools"] if isinstance(item, dict)]
        by_id = {item.get("tool_id"): item for item in existing}
        for tool in tools:
            tool_payload = tool.model_dump(mode="json")
            by_id[tool_payload["tool_id"]] = tool_payload
        settlements = dict(payload.get("settlements") or {})
        if settlement_id is not None:
            if settlement is None or candidate_id is None or iteration is None:
                raise ValueError("shared tool settlement receipt is incomplete")
            settlements[settlement_id] = {
                "candidate_id": candidate_id,
                "iteration": iteration,
                "result": self._settlement_payload(settlement),
                "recorded_at": _utc_timestamp(),
            }
        self._write_index(list(by_id.values()), settlements=settlements)

    def _load_index(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self._load_index_payload()["tools"]
            if isinstance(item, dict)
        ]

    def _write_index(
        self,
        tools: list[dict[str, Any]],
        *,
        settlements: dict[str, Any] | None = None,
    ) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SHARED_INDEX_SCHEMA_VERSION,
            "tools": tools,
            "settlements": settlements or {},
        }
        self.index_temp_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.index_temp_dir / (
            f"index-{os.getpid()}-{uuid.uuid4().hex}.json"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.index_path)

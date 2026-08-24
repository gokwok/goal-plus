from __future__ import annotations

from contextlib import contextmanager
import base64
import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib
import uuid
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

from goal_plus.agent_hosts import (
    UnsupportedHostCapability,
    get_agent_host_adapter,
    portable_strategy_mode,
)
from goal_plus.models import (
    AgentHostHandle,
    AgentSessionRecord,
    BestArtifactRecord,
    CandidateRecord,
    CandidateProposal,
    CandidateTask,
    CandidateWorkOrder,
    EvidenceAnnotationTask,
    FeedbackPolicy,
    EvidenceViewRecord,
    FsSnapshotArtifactRef,
    FsSnapshotCreationIntent,
    FsRequestRecord,
    FrozenSpec,
    GlobalEvidenceReadRecord,
    GlobalEvidenceViewReference,
    GitCommitArtifactRef,
    IterationDisposition,
    PromotionEvidence,
    RunRecord,
    RunState,
    RunSummary,
    IterationRecord,
    ModelSpec,
    PublicationIntent,
    SelectedModel,
    ResultLedgerEntry,
    ResolvedCodexProvider,
    ResolvedEvidenceAnnotatorProfile,
    ScoreReport,
    SearchPlan,
    SearchSpec,
    SharedToolRecord,
    StrategySpec,
    ToolizationDecision,
    ToolAdoptionRecord,
    ToolCopyReceipt,
    VerifierCommand,
    VerifierInvalidationReason,
    VerifierResult,
    VerifierRole,
    WorkerBudget,
    WorkerLaunchOptions,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT
from goal_plus.shared_dir import (
    SHARE_OUT_RELATIVE_PATH,
    TOOL_DRAFTS_RELATIVE_PATH,
    TOOL_INBOX_RELATIVE_PATH,
    TOOL_VIEW_MAX_CONTENT_BYTES,
    SharedDirManager,
    SharedDirSettlement,
)
from goal_plus.workspaces import (
    IGNORED_NAMES,
    IGNORED_SUFFIXES,
    copy_source_tree,
    initialize_workspace_git_baseline,
    list_files,
    list_source_files,
    materialize_candidate_workspace,
)
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    new_request_id,
)
from goal_plus.artifacts import (
    FsSnapshotArtifactReader,
    GitArtifactReader,
    fs_path_text,
)


VERIFIER_PHASE_ENV = "GOAL_PLUS_VERIFIER_PHASE"
VERIFIER_DIAGNOSTICS_ENV = "GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR"
VERIFIER_RESOURCE_ENV = "GOAL_PLUS_VERIFIER_RESOURCE"
VERIFIER_RESOURCE_LOCK_DIR_ENV = "GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR"
VERIFIER_OUTPUT_LIMIT_BYTES = 64 * 1024
VERIFIER_LOG_LIMIT_BYTES = VERIFIER_OUTPUT_LIMIT_BYTES * 2 + 8192
VERIFIER_TERM_GRACE_SECONDS = 0.5
MAX_EVIDENCE_ANNOTATION_DIFF_BYTES = 1024 * 1024
MAX_EVIDENCE_COMPARISON_PEERS = 8
MAX_EVIDENCE_PEER_DIFF_BYTES = 64 * 1024
EVIDENCE_ANNOTATOR_MODEL_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL"
EVIDENCE_ANNOTATOR_REASONING_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT"
EVIDENCE_ANNOTATOR_BASE_URL_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL"
EVIDENCE_ANNOTATOR_PROVIDER_ID_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID"
EVIDENCE_ANNOTATOR_PROVIDER_NAME_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME"
EVIDENCE_ANNOTATOR_API_KEY_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV"
EVIDENCE_ANNOTATOR_WIRE_API_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API"
OUTER_DEADLINE_ENV = "GOAL_PLUS_OUTER_DEADLINE_AT"
GLOBAL_EVIDENCE_MODE_ENV = "GOAL_PLUS_GLOBAL_EVIDENCE_MODE"
GLOBAL_EVIDENCE_MODES = frozenset({"manual", "auto", "independent"})
SUPPLEMENTAL_EVALUATION_ENABLED_ENV = (
    "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"
)
SUPPLEMENTAL_EVALUATION_REQUIRED_ENV = (
    "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED"
)
_UNSET = object()


def _boolean_environment_value(
    name: str,
    *,
    default: bool,
    environment: dict[str, str] | None = None,
) -> bool:
    source = os.environ if environment is None else environment
    raw = source.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, found {raw!r}")


def supplemental_evaluation_enabled(
    environment: dict[str, str] | None = None,
) -> bool:
    return _boolean_environment_value(
        SUPPLEMENTAL_EVALUATION_ENABLED_ENV,
        default=False,
        environment=environment,
    )


def supplemental_evaluation_required(
    environment: dict[str, str] | None = None,
) -> bool:
    return _boolean_environment_value(
        SUPPLEMENTAL_EVALUATION_REQUIRED_ENV,
        default=False,
        environment=environment,
    )
EXTERNAL_EVIDENCE_DIR_ENV = "GOAL_PLUS_EXTERNAL_EVIDENCE_DIR"
MAX_EXTERNAL_EVIDENCE_BYTES = 256 * 1024


@dataclass(frozen=True)
class _CandidateArtifactState:
    changed_files: list[str]
    touched_denied_files: bool
    changed_outside_allowed: bool
    artifact_hash: str
    git_head: str | None
    git_status: list[str]
    git_artifact_clean: bool


@dataclass(frozen=True)
class _FsAttemptState:
    base_ref: FsSnapshotArtifactRef
    attempt_ref: FsSnapshotArtifactRef
    changed_files: list[str]
    actual_diff: str
    cumulative_diff: str
    touched_denied_files: bool
    changed_outside_allowed: bool
    artifact_hash: str
    continuation_required: bool
    snapshot_request_id: str | None = None


class _BoundedOutput:
    def __init__(self, limit: int = VERIFIER_OUTPUT_LIMIT_BYTES) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) >= self.limit:
            self.data[:] = chunk[-self.limit :]
            self.truncated = True
            return
        overflow = len(self.data) + len(chunk) - self.limit
        if overflow > 0:
            del self.data[:overflow]
            self.truncated = True
        self.data.extend(chunk)

    def text(self) -> str:
        value = self.data.decode("utf-8", errors="replace")
        if self.truncated:
            return "[... output truncated ...]\n" + value
        return value


def _bounded_log(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= VERIFIER_LOG_LIMIT_BYTES:
        return value
    marker = b"[... log truncated ...]\n"
    tail = encoded[-(VERIFIER_LOG_LIMIT_BYTES - len(marker)) :]
    return (marker + tail).decode("utf-8", errors="replace")


def _bounded_projection(value: str | None, max_bytes: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = b"\n[diff projection truncated]\n"
    retained = encoded[: max(0, max_bytes - len(marker))]
    return (retained + marker).decode("utf-8", errors="replace")


def _verifier_output_tail_detail(stdout: str, stderr: str) -> str:
    details = []
    stdout_tail = stdout.strip()[-2000:]
    stderr_tail = stderr.strip()[-2000:]
    if stdout_tail:
        details.append(f"Stdout tail: {stdout_tail}")
    if stderr_tail:
        details.append(f"Stderr tail: {stderr_tail}")
    return " " + " ".join(details) if details else ""
RESULTS_TSV_RELATIVE_PATH = "results.tsv"
LEGACY_RESULTS_TSV_RELATIVE_PATH = ".tmp/results.tsv"
MODEL_HANDOFF_RELATIVE_PATH = ".tmp/handoff.json"
MAX_MODEL_HANDOFF_BYTES = 64 * 1024
MAX_VERIFIER_FEEDBACK_CHARS = 4_000
WORKER_ITERATION_RUN_STATES = frozenset(
    {
        RunState.RUNNING,
        RunState.WAITING_FOR_WORKERS,
        RunState.SELECTING,
        RunState.SELECTION_BLOCKED,
    }
)
EVIDENCE_ANNOTATION_RUN_STATES = WORKER_ITERATION_RUN_STATES | frozenset(
    {
        RunState.READY_TO_PROMOTE,
        RunState.PROMOTED,
    }
)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_timestamp_from_epoch(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def parse_utc_timestamp(timestamp: str) -> float:
    return float(calendar.timegm(time.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
    tmp_path.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


@contextmanager
def exclusive_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    lock_dir = lock_path.with_suffix(lock_path.suffix + ".dir")
    while True:  # pragma: no cover - fallback for non-POSIX hosts
        try:
            lock_dir.mkdir(parents=True)
            break
        except FileExistsError:
            time.sleep(0.05)
    try:
        yield
    finally:
        lock_dir.rmdir()


@contextmanager
def verifier_resource_lock(resource: str | None):
    if resource is None:
        yield
        return
    lock_root = Path(
        os.environ.get(
            VERIFIER_RESOURCE_LOCK_DIR_ENV,
            str(Path(tempfile.gettempdir()) / "goal-plus-verifier-locks"),
        )
    ).resolve()
    lock_name = f"{sha256_text(resource)}.lock"
    with exclusive_file_lock(lock_root / lock_name):
        yield


def path_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    for pattern in patterns:
        pat = pattern.replace(os.sep, "/")
        if normalized == pat or fnmatch(normalized, pat):
            return True
        if pat.endswith("/") and normalized.startswith(pat):
            return True
        if normalized.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def safe_verifier_name(value: str) -> str:
    readable = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in value
    ).strip("._-")
    return f"{readable or 'verifier'}-{sha256_text(value)[:8]}"


def relative_artifact_path(source_root: Path, artifact_path: Path) -> str:
    artifact = artifact_path.resolve()
    try:
        return artifact.relative_to(source_root.resolve()).as_posix()
    except ValueError:
        return artifact.name


def _normalize_verifier_cwds_for_candidate_workspace(spec: SearchSpec) -> SearchSpec:
    source_root = Path(spec.source_path).resolve()

    def normalize_command(command: VerifierCommand) -> VerifierCommand:
        cwd_path = Path(command.cwd)
        if cwd_path.resolve() == source_root:
            return command.model_copy(update={"cwd": "."})
        return command

    return spec.model_copy(
        deep=True,
        update={
            "process_verifiers": [
                normalize_command(command) for command in spec.process_verifiers
            ],
            "promotion_verifiers": [
                normalize_command(command) for command in spec.promotion_verifiers
            ],
        },
    )


class FileSearchRuntime:
    def __init__(
        self,
        root_dir: Path | str = DEFAULT_RUNTIME_ROOT,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.specs_dir = self.root_dir / "specs"
        self.runs_dir = self.root_dir / "runs"
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def list_available_models(
        self,
        host: Literal["codex", "pi-rpc", "pi-thinkthread"],
        query: str | None = None,
    ) -> dict[str, Any]:
        adapter = get_agent_host_adapter(host)
        return {
            "host": host,
            "adapter_version": adapter.adapter_version,
            "models": adapter.list_available_models(query),
        }

    @staticmethod
    def _agent_posix_client() -> AgentPosixSdkClient:
        return AgentPosixSdkClient()

    @staticmethod
    def _match_available_model(
        requested: str,
        available: list[dict[str, Any]],
    ) -> dict[str, Any]:
        needle = requested.strip().casefold()
        exact_refs = [
            model
            for model in available
            if str(model.get("model") or "").casefold() == needle
        ]
        if len(exact_refs) == 1:
            return exact_refs[0]
        exact_ids = [
            model
            for model in available
            if str(model.get("model_id") or "").casefold() == needle
        ]
        if len(exact_ids) == 1:
            return exact_ids[0]
        candidates = [
            model
            for model in available
            if needle
            in " ".join(
                str(model.get(key) or "")
                for key in ("model", "model_id", "display_name")
            ).casefold()
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and not exact_refs and not exact_ids:
            raise ValueError(f"requested model is not available: {requested}")
        matches = exact_refs or exact_ids or candidates
        raise ValueError(
            f"requested model is ambiguous: {requested}; matches: "
            + ", ".join(str(model.get("model")) for model in matches)
        )

    def _normalize_strategy_models(self, spec: SearchSpec) -> SearchSpec:
        requested = list(spec.strategy.models)
        if not requested:
            return spec
        explicit_counts = [model.count is not None for model in requested]
        if any(explicit_counts) and not all(explicit_counts):
            raise ValueError(
                "strategy.models must either specify count for every model or for none"
            )
        max_parallel = spec.budget.max_parallel
        if len(requested) > max_parallel:
            raise ValueError(
                "strategy.models cannot contain more entries than budget.max_parallel"
            )
        if all(explicit_counts):
            counts: list[int | None] = [int(model.count or 0) for model in requested]
            if sum(int(count or 0) for count in counts) != max_parallel:
                raise ValueError(
                    "explicit strategy.models counts must sum to budget.max_parallel"
                )
        else:
            counts = [None] * len(requested)

        discovery = self.list_available_models(spec.strategy.worker_host)
        available = discovery["models"]
        normalized_models: list[ModelSpec] = []
        for requested_model, count in zip(requested, counts, strict=True):
            match = self._match_available_model(requested_model.model, available)
            normalized_models.append(
                requested_model.model_copy(
                    update={
                        "model": match["model"],
                        "count": count,
                        "provider": match.get("provider"),
                        "adapter_version": discovery["adapter_version"],
                    }
                )
            )
        strategy = spec.strategy.model_copy(update={"models": normalized_models})
        return spec.model_copy(update={"strategy": strategy})

    def _execute_verifier_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
        start_new_session: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not text or not capture_output:
            raise ValueError("verifier processes require text capture")

        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=start_new_session,
        )
        stdout_capture = _BoundedOutput()
        stderr_capture = _BoundedOutput()

        def drain(stream: Any, capture: _BoundedOutput) -> None:
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    capture.append(chunk)
            except (OSError, ValueError):
                pass

        readers = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout_capture),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr_capture),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_verifier_process_group(process)
            returncode = process.returncode if process.returncode is not None else -signal.SIGKILL

        for reader in readers:
            reader.join(timeout=VERIFIER_TERM_GRACE_SECONDS)
        if any(reader.is_alive() for reader in readers):
            # A verifier that exits while leaving descendants with inherited
            # output pipes would otherwise leak both processes and reader threads.
            self._terminate_verifier_process_group(process)
            for reader in readers:
                reader.join(timeout=VERIFIER_TERM_GRACE_SECONDS)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

        stdout = stdout_capture.text()
        stderr = stderr_capture.text()
        if timed_out:
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=stdout,
                stderr=stderr,
            )
        completed = subprocess.CompletedProcess(
            args=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and returncode:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                output=stdout,
                stderr=stderr,
            )
        return completed

    def _terminate_verifier_process_group(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=VERIFIER_TERM_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return

        process_group = process.pid

        def group_exists() -> bool:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + VERIFIER_TERM_GRACE_SECONDS
        while group_exists() and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.02)
        if group_exists():
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=VERIFIER_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def freeze_spec(self, spec: SearchSpec, verifier_artifacts: list[Path]) -> FrozenSpec:
        supplemental_enabled = supplemental_evaluation_enabled()
        supplemental_required = supplemental_evaluation_required()
        if supplemental_required and not supplemental_enabled:
            raise ValueError(
                f"{SUPPLEMENTAL_EVALUATION_REQUIRED_ENV}=1 requires "
                f"{SUPPLEMENTAL_EVALUATION_ENABLED_ENV}=1"
            )
        spec = _normalize_verifier_cwds_for_candidate_workspace(spec)
        spec = self._apply_global_evidence_mode_from_environment(spec)
        spec = self._normalize_strategy_models(spec)
        self._validate_strategy_config(spec.strategy)
        source_root = Path(spec.source_path).resolve()
        verifier_hashes: dict[str, str] = {}
        artifact_entries: list[tuple[Path, str]] = []

        for artifact in verifier_artifacts:
            artifact_path = Path(artifact).resolve()
            if not artifact_path.exists() or not artifact_path.is_file():
                raise FileNotFoundError(f"verifier artifact not found: {artifact_path}")
            try:
                artifact_path.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(
                    f"Verifier artifact is outside source_path '{source_root}': "
                    f"{artifact_path}. Move it into a source-owned, materialized "
                    "path such as '.goal-plus-verifiers/'."
                ) from exc
            rel_path = relative_artifact_path(source_root, artifact_path)
            ignored_part = next(
                (part for part in Path(rel_path).parts if part in IGNORED_NAMES),
                None,
            )
            if ignored_part is not None:
                if ignored_part in {DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT}:
                    raise ValueError(
                        "Verifier artifact is under the ignored Goal Plus runtime "
                        f"directory '{ignored_part}': {rel_path}. Move it to a "
                        "source-owned path such as "
                        "'.goal-plus-verifiers/score.sh'."
                    )
                raise ValueError(
                    f"Verifier artifact is under ignored workspace path "
                    f"'{ignored_part}': {rel_path}. Move it to a source-owned, "
                    "materialized path."
                )
            if Path(rel_path).suffix in IGNORED_SUFFIXES:
                raise ValueError(
                    f"Verifier artifact uses ignored workspace suffix "
                    f"'{Path(rel_path).suffix}': {rel_path}. Move it to a "
                    "source-owned, materialized path."
                )
            artifact_entries.append((artifact_path, rel_path))

        self._preflight_ranking_verifiers(spec)

        for artifact_path, rel_path in artifact_entries:
            verifier_hashes[rel_path] = sha256_file(artifact_path)

        spec_payload = spec.model_dump(mode="json")
        spec_hash = sha256_text(canonical_json({"spec": spec_payload, "verifiers": verifier_hashes}))
        frozen_spec_id = f"spec_{spec_hash[:12]}"
        spec_dir = self._spec_dir(frozen_spec_id)
        frozen_verifier_paths: dict[str, str] = {}

        for artifact_path, rel_path in artifact_entries:
            frozen_path = spec_dir / "frozen_verifiers" / rel_path
            frozen_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact_path, frozen_path)
            frozen_verifier_paths[rel_path] = str(frozen_path)

        frozen = FrozenSpec(
            frozen_spec_id=frozen_spec_id,
            spec_hash=spec_hash,
            spec=spec,
            verifier_hashes=verifier_hashes,
            frozen_verifier_paths=frozen_verifier_paths,
            created_at=utc_timestamp(),
        )
        write_json(spec_dir / "frozen_spec.json", frozen.model_dump(mode="json"))
        return frozen

    def _preflight_ranking_verifiers(self, spec: SearchSpec) -> None:
        source_root = Path(spec.source_path).resolve()
        source_workspace = source_root if source_root.is_dir() else source_root.parent
        ranking_verifiers = [
            command
            for command in [*spec.process_verifiers, *spec.promotion_verifiers]
            if command.role == VerifierRole.RANKING_SIGNAL
        ]

        with tempfile.TemporaryDirectory(
            prefix="goal-plus-verifier-preflight-"
        ) as preflight_root:
            workspace = Path(preflight_root) / "workspace"
            copy_source_tree(source_workspace, workspace)
            initialize_workspace_git_baseline(workspace)

            for command in ranking_verifiers:
                cwd = (workspace / command.cwd).resolve()
                if not cwd.is_dir():
                    raise ValueError(
                        f"Ranking verifier '{command.name}' has a missing working "
                        f"directory: {cwd}"
                    )
                if command.command[0] == "goal-plus-internal":
                    raise ValueError(
                        f"Ranking verifier '{command.name}' cannot use a "
                        "goal-plus-internal command; use a process verifier that "
                        "prints the numeric metric as JSON."
                    )

                workspace_before = self._hash_verifier_workspace(workspace)
                try:
                    with verifier_resource_lock(command.resource_lock):
                        with tempfile.TemporaryDirectory(
                            prefix="goal-plus-verifier-command-"
                        ) as verifier_tmp:
                            verifier_tmp_path = Path(verifier_tmp)
                            diagnostics_dir = verifier_tmp_path / "diagnostics"
                            diagnostics_dir.mkdir()
                            completed = self._execute_verifier_process(
                                command.command,
                                cwd=cwd,
                                env=self._verifier_environment(
                                    cwd,
                                    verifier_tmp_path,
                                    phase="freeze_preflight",
                                    diagnostics_dir=diagnostics_dir,
                                    resource=command.resource_lock,
                                ),
                                text=True,
                                capture_output=True,
                                timeout=command.timeout_seconds,
                                check=False,
                                start_new_session=(
                                    spec.strategy.worker_host != "pi-thinkthread"
                                ),
                            )
                except subprocess.TimeoutExpired as exc:
                    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                    detail = _verifier_output_tail_detail(stdout, stderr)
                    raise ValueError(
                        f"Ranking verifier '{command.name}' timed out during freeze "
                        f"preflight after {command.timeout_seconds} seconds.{detail}"
                    ) from exc
                except OSError as exc:
                    raise ValueError(
                        f"Ranking verifier '{command.name}' could not start during "
                        f"freeze preflight: {exc}"
                    ) from exc

                side_effects = self._hash_changes(
                    workspace_before,
                    self._hash_verifier_workspace(workspace),
                )
                if side_effects:
                    raise ValueError(
                        f"VerifierWorkspaceSideEffect: ranking verifier "
                        f"'{command.name}' changed the disposable preflight "
                        f"workspace: {side_effects}. Verifiers must keep the "
                        "candidate workspace read-only. Put compiler products and "
                        "temporary outputs in the per-invocation directory exposed "
                        "through GOAL_PLUS_VERIFIER_TMPDIR/TMPDIR, or use Python "
                        "tempfile.TemporaryDirectory(). Never use one fixed /tmp "
                        "path because candidates may verify concurrently."
                    )

                if completed.returncode != 0:
                    detail = _verifier_output_tail_detail(
                        completed.stdout,
                        completed.stderr,
                    )
                    raise ValueError(
                        f"Ranking verifier '{command.name}' failed during freeze "
                        f"preflight with exit code {completed.returncode}.{detail}"
                    )

                metrics = self._parse_metrics(completed.stdout)
                if self._has_verifier_error(metrics):
                    error_detail = str(metrics["error"])[-2000:]
                    raise ValueError(
                        f"Ranking verifier '{command.name}' reported an error "
                        f"during freeze preflight: {error_detail}"
                    )
                score = self._score_from_metrics(spec.metric_name, metrics)
                if score is None:
                    example = canonical_json({spec.metric_name: 123.0})
                    raise ValueError(
                        f"Ranking verifier '{command.name}' exited successfully but "
                        "emitted no finite numeric metric. The final non-empty stdout "
                        f"line must be a JSON object such as: {example}. "
                        "VerifierCommand.expected_outputs lists artifact paths only; "
                        "it does not parse stdout."
                    )

    def _verifier_environment(
        self,
        cwd: Path,
        temp_dir: Path,
        *,
        phase: Literal["freeze_preflight", "candidate", "promotion"],
        diagnostics_dir: Path | None = None,
        resource: str | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(cwd) + os.pathsep + env.get("PYTHONPATH", "")
        for name in ("TMPDIR", "TMP", "TEMP", "GOAL_PLUS_VERIFIER_TMPDIR"):
            env[name] = str(temp_dir)
        env[VERIFIER_PHASE_ENV] = phase
        if diagnostics_dir is not None:
            env[VERIFIER_DIAGNOSTICS_ENV] = str(diagnostics_dir)
        else:
            env.pop(VERIFIER_DIAGNOSTICS_ENV, None)
        if resource is not None:
            env[VERIFIER_RESOURCE_ENV] = resource
        else:
            env.pop(VERIFIER_RESOURCE_ENV, None)
        return env

    def _hash_changes(
        self,
        before: dict[str, str],
        after: dict[str, str],
    ) -> list[str]:
        return [
            path
            for path in sorted(set(before) | set(after))
            if before.get(path) != after.get(path)
        ]

    def _hash_verifier_workspace(self, root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        ignored_names = IGNORED_NAMES - {".tmp"}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root)
            if any(part in ignored_names for part in rel_path.parts):
                continue
            if path.suffix in IGNORED_SUFFIXES:
                continue
            hashes[rel_path.as_posix()] = sha256_file(path)
        return hashes

    @staticmethod
    def _expand_selected_models(
        models: list[ModelSpec], max_parallel: int
    ) -> list[SelectedModel]:
        if not models:
            return []
        explicit_counts = [model.count is not None for model in models]
        if any(explicit_counts) and not all(explicit_counts):
            raise ValueError(
                "frozen strategy.models must either specify count for every model or for none"
            )
        expanded = (
            [model for model in models for _ in range(int(model.count or 0))]
            if all(explicit_counts)
            else [models[index % len(models)] for index in range(max_parallel)]
        )
        selected: list[SelectedModel] = []
        for model in expanded:
            selected.append(
                SelectedModel(
                    slot=len(selected) + 1,
                    model=model.model,
                    provider=model.provider,
                    adapter_version=model.adapter_version,
                    reasoning_effort=model.reasoning_effort,
                    service_tier=model.service_tier,
                    context_policy=model.context_policy,
                )
            )
        return selected

    @staticmethod
    def _selected_model_provenance(model: SelectedModel) -> dict[str, Any]:
        launch = {
            "model": model.model,
            "reasoning_effort": model.reasoning_effort,
            "service_tier": model.service_tier,
        }
        return {
            "selected_model": model.model,
            "provider": model.provider,
            "exact_model_ref": model.model,
            "adapter_version": model.adapter_version,
            "context_policy": model.context_policy,
            "worker_launch": {
                key: value for key, value in launch.items() if value is not None
            },
        }

    @staticmethod
    def _selected_model_for_slot(
        plan: SearchPlan, slot: int
    ) -> SelectedModel | None:
        if not plan.selected_models:
            return None
        return plan.selected_models[slot - 1]

    @staticmethod
    def _capability_ids(view: dict[str, Any]) -> set[str]:
        raw = view.get("capabilities")
        if not isinstance(raw, list):
            return set()
        return {
            str(item["id"])
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    @staticmethod
    def _fs_source_relative_path(source_path: str) -> str:
        source = Path(source_path).resolve()
        execution_root = Path.cwd().resolve()
        try:
            relative = source.relative_to(execution_root)
        except ValueError as exc:
            raise ValueError(
                "pi-thinkthread source_path must be within the Root execution "
                f"workspace {execution_root}: {source}"
            ) from exc
        return relative.as_posix() if relative.parts else "."

    def _create_pi_thinkthread_baseline(self, run: RunRecord) -> RunRecord:
        now = utc_timestamp()
        request_id = new_request_id()
        intent = FsSnapshotCreationIntent(
            intent_id=f"snapshot-intent-{uuid.uuid4()}",
            operation="root_snapshot",
            request_id=request_id,
            state="prepared",
            purpose="initial_baseline",
            created_at=now,
            updated_at=now,
        )
        run.fs_source_relative_path = self._fs_source_relative_path(run.source_path)
        run.fs_snapshot_intents.append(intent)
        run.fs_requests.append(
            FsRequestRecord(
                request_id=request_id,
                operation="root_snapshot",
                context={"purpose": "initial_baseline"},
                created_at=now,
                updated_at=now,
            )
        )
        self._write_run(run)

        client = self._agent_posix_client()
        client.preflight()
        self_view = client.self_view()
        if self_view.get("parentThinkthreadId") is not None:
            raise RuntimeError(
                "pi-thinkthread Search run baseline must be created by a Root Agent"
            )
        missing = {
            "thinkthread.child",
            "thinkthread.message",
            "thinkthread.fs",
        } - self._capability_ids(self_view)
        if missing:
            raise RuntimeError(
                "pi-thinkthread Root lacks required capabilities: "
                + ", ".join(sorted(missing))
            )
        fs_view = client.invoke("fs.stat")
        if fs_view.get("kind") != "direct":
            raise RuntimeError(
                "pi-thinkthread Root Profile must use rootFsMode=direct"
            )

        intent.state = "platform_mutation_started"
        intent.updated_at = utc_timestamp()
        self._write_run(run)
        try:
            snapshot = self._invoke_durable_fs_operation(
                client=client,
                run_id=run.run_id,
                request_id=request_id,
                method="fs.snapshot.create",
                params={"requestId": request_id},
                timeout_seconds=120,
            )
        except AgentPosixBridgeError as exc:
            with self._run_transaction(run.run_id):
                latest = self._load_run(run.run_id)
                current_intent = next(
                    item
                    for item in latest.fs_snapshot_intents
                    if item.intent_id == intent.intent_id
                )
                current_intent.state = "failed"
                current_intent.updated_at = utc_timestamp()
                latest.state = RunState.FAILED
                latest.budget_used["baseline_snapshot_error"] = {
                    "request_id": request_id,
                    "error_code": exc.code,
                    "message": str(exc),
                }
                self._write_run(latest)
            raise
        except RuntimeError:
            with self._run_transaction(run.run_id):
                latest = self._load_run(run.run_id)
                current_intent = next(
                    item
                    for item in latest.fs_snapshot_intents
                    if item.intent_id == intent.intent_id
                )
                current_intent.state = "needs_recovery"
                current_intent.updated_at = utc_timestamp()
                self._write_run(latest)
            raise
        snapshot_id = snapshot.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            with self._run_transaction(run.run_id):
                latest = self._load_run(run.run_id)
                current_intent = next(
                    item
                    for item in latest.fs_snapshot_intents
                    if item.intent_id == intent.intent_id
                )
                current_intent.state = "needs_recovery"
                current_intent.updated_at = utc_timestamp()
                latest.state = RunState.NEEDS_RECOVERY
                latest.budget_used["needs_recovery_reason"] = (
                    f"fs.snapshot.create request {request_id} returned no snapshotId"
                )
                self._write_run(latest)
            raise RuntimeError(
                f"pi-thinkthread run {run.run_id} baseline snapshot omitted snapshotId"
            )
        with self._run_transaction(run.run_id):
            latest = self._load_run(run.run_id)
            current_intent = next(
                item
                for item in latest.fs_snapshot_intents
                if item.intent_id == intent.intent_id
            )
            current_intent.state = "created"
            current_intent.snapshot_id = snapshot_id
            current_intent.updated_at = utc_timestamp()
            latest.baseline_artifact_ref = FsSnapshotArtifactRef(
                snapshot_id=snapshot_id
            )
            latest.budget_used.pop("needs_recovery_reason", None)
            self._write_run(latest)
        self._close_fs_requests_after_evidence(run.run_id, [request_id], client)
        return self._load_run(run.run_id)

    def create_run(
        self,
        frozen_spec_id: str,
        source_run_id: str | None = None,
    ) -> str:
        frozen = self._load_frozen_spec(frozen_spec_id)
        selected_models = self._expand_selected_models(
            frozen.spec.strategy.models,
            frozen.spec.budget.max_parallel,
        )
        if selected_models and len(selected_models) != frozen.spec.budget.max_parallel:
            raise ValueError(
                "frozen strategy.models must resolve to budget.max_parallel selected models"
            )
        inherited_research = (
            self._build_inherited_research(
                source_run_id,
            )
            if source_run_id
            else {}
        )
        run_id = f"run_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{uuid.uuid4().hex[:8]}"
        run = RunRecord(
            run_id=run_id,
            state=RunState.RUNNING,
            frozen_spec_id=frozen.frozen_spec_id,
            source_path=str(Path(frozen.spec.source_path).resolve()),
            created_at=utc_timestamp(),
            source_run_id=source_run_id,
            inherited_research=inherited_research,
            selected_models=selected_models,
        )
        self._write_run(run)
        (self._run_dir(run_id) / "candidates").mkdir(parents=True, exist_ok=True)
        if frozen.spec.strategy.worker_host != "pi-thinkthread":
            (self._run_dir(run_id) / "workspace").mkdir(parents=True, exist_ok=True)
        (self._run_dir(run_id) / "plans").mkdir(parents=True, exist_ok=True)
        (self._run_dir(run_id) / "agent_sessions").mkdir(parents=True, exist_ok=True)
        if frozen.spec.strategy.worker_host == "pi-thinkthread":
            run = self._create_pi_thinkthread_baseline(run)
        if source_run_id:
            with self._run_transaction(source_run_id):
                source_run = self._load_run(source_run_id)
                if source_run.invalidated_at:
                    source_run.replacement_run_id = run_id
                    self._write_run(source_run)
        return run_id

    def invalidate_run(
        self,
        run_id: str,
        *,
        reason: VerifierInvalidationReason,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> RunRecord:
        """Atomically fence a verifier-invalid run before host workers stop."""

        if not summary.strip():
            raise ValueError("summary must be non-empty")
        if not evidence:
            raise ValueError("evidence must contain at least one concrete item")

        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            if run.state == RunState.PROMOTED:
                raise RuntimeError("cannot invalidate an already promoted run")
            if run.invalidated_at:
                if run.invalidation_reason != reason:
                    raise RuntimeError(
                        "run is already invalidated for a different verifier reason"
                    )
                return run
            updated = RunRecord.model_validate(
                {
                    **run.model_dump(mode="json"),
                    "state": RunState.ABORTED,
                    "invalidated_at": utc_timestamp(),
                    "invalidation_reason": reason,
                    "invalidation_summary": summary.strip(),
                    "invalidation_evidence": evidence,
                }
            )
            self._write_run(updated)
            return updated

    def _assert_run_not_invalidated(self, run: RunRecord, operation: str) -> None:
        if run.invalidated_at:
            raise RuntimeError(
                f"cannot {operation}: run {run.run_id} was invalidated because "
                f"{run.invalidation_reason}"
            )

    def _assert_worker_iteration_allowed(
        self,
        run: RunRecord,
        operation: str,
    ) -> None:
        self._assert_run_not_invalidated(run, operation)
        if run.state not in WORKER_ITERATION_RUN_STATES:
            raise RuntimeError(
                f"cannot {operation}: run {run.run_id} is in state {run.state}"
            )

    def status(self, run_id: str) -> RunSummary:
        run = self._load_run(run_id)
        records = self._load_candidate_records(run_id)
        evaluated = sum(1 for record in records if record.status == "evaluated")
        return RunSummary(
            run_id=run.run_id,
            state=run.state,
            frozen_spec_id=run.frozen_spec_id,
            candidates_total=len(records),
            candidates_evaluated=evaluated,
            best_candidate_id=run.best_candidate_id,
            best_score=run.best_score,
            budget_used=run.budget_used,
            source_run_id=run.source_run_id,
            invalidated_at=run.invalidated_at,
            invalidation_reason=run.invalidation_reason,
            replacement_run_id=run.replacement_run_id,
        )

    def list_history(self, run_id: str, top_n: int = 5, sort_by: str = "score") -> dict[str, Any]:
        if top_n <= 0:
            raise ValueError("top_n must be > 0")
        if sort_by not in {"score", "created"}:
            raise ValueError("sort_by must be 'score' or 'created'")

        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        records = self._load_candidate_records(run_id)

        def score_value(record: CandidateRecord) -> float | None:
            return self._record_ranking_score(record, frozen.spec)

        def created_index(record: CandidateRecord) -> int:
            try:
                return int(record.candidate_id.removeprefix("c"))
            except ValueError:
                return 0

        if sort_by == "score":
            reverse = frozen.spec.metric_direction == "maximize"

            def score_key(record: CandidateRecord) -> tuple[int, float, int]:
                score = score_value(record)
                if score is None:
                    return (1, 0.0, created_index(record))
                sortable_score = score if reverse else -score
                return (0, -sortable_score, created_index(record))

            ordered = sorted(records, key=score_key)
        else:
            ordered = sorted(records, key=created_index)

        selected = ordered[:top_n]
        candidates = [
            self._history_candidate_payload(record, frozen.spec) for record in selected
        ]
        research_rollup = self._run_research_rollup(
            records,
            frozen.spec,
            visible_candidate_ids=[record.candidate_id for record in selected],
        )

        return {
            "run_id": run.run_id,
            "state": run.state,
            "frozen_spec_id": run.frozen_spec_id,
            "source_run_id": run.source_run_id,
            "inherited_research": run.inherited_research,
            "invalidated_at": run.invalidated_at,
            "invalidation_reason": run.invalidation_reason,
            "invalidation_summary": run.invalidation_summary,
            "invalidation_evidence": run.invalidation_evidence,
            "replacement_run_id": run.replacement_run_id,
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "strategy": frozen.spec.strategy.model_dump(mode="json"),
            "worker_policy": self._normalize_worker_policy(frozen.spec.strategy),
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
            "total_candidates": len(records),
            "returned_candidates": len(candidates),
            "top_n": top_n,
            "sort_by": sort_by,
            "candidates": candidates,
            "feature_ledger": research_rollup["feature_ledger"],
            "pitfalls": research_rollup["pitfalls"],
            "verifier_assessments": research_rollup["verifier_assessments"],
            "research_rollup": research_rollup,
        }

    def list_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        record = self._load_candidate_record(run_id, candidate_id)
        return [it.model_dump(mode="json") for it in record.iterations]

    def plan_next(self, run_id: str, requested_k: int = 4) -> SearchPlan:
        with self._run_transaction(run_id):
            return self._plan_next_locked(run_id, requested_k)

    def _plan_next_locked(self, run_id: str, requested_k: int) -> SearchPlan:
        if requested_k <= 0:
            raise ValueError("requested_k must be > 0")

        run = self._load_run(run_id)
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot plan next batch from state {run.state}")

        frozen = self._load_frozen_spec(run.frozen_spec_id)
        spec = frozen.spec
        if self._load_plans(run_id):
            raise RuntimeError(
                "Search permits one initial SearchPlan; resume or "
                "redispatch the existing candidates instead of planning a new batch"
            )
        remaining = max(0, spec.budget.max_parallel - run.candidates_total)
        planned_k = min(requested_k, remaining)
        selected_models = list(run.selected_models)
        if selected_models:
            if requested_k != len(selected_models):
                raise ValueError(
                    "requested_k must equal the fixed selected_models size "
                    f"({len(selected_models)})"
                )
            if planned_k != len(selected_models):
                raise ValueError(
                    "selected_models size exceeds the remaining parallel budget"
                )
        strategy = spec.strategy
        self._validate_host_strategy(strategy)
        mode = self._strategy_mode(strategy)

        if mode in {"agent", "agent_guided", "default"}:
            plan = self._plan_agent_guided(run, frozen, requested_k, planned_k, remaining)
        elif mode in {"random", "random_mode"}:
            plan = self._plan_independent(run, frozen, requested_k, planned_k, remaining)
        else:
            raise ValueError(
                f"unsupported initial strategy: {strategy.name}; "
                "use agent_guided or random"
            )

        plan.worker_policy = self._normalize_worker_policy(plan.strategy, plan.worker_policy)
        plan.selected_models = selected_models
        plan.strategy_trace.setdefault("worker_policy", plan.worker_policy)
        self._write_plan(plan)
        run.budget_used["last_plan_id"] = plan.plan_id
        self._write_run(run)
        return plan

    def start_batch(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None = None,
    ) -> list[CandidateTask]:
        with self._run_transaction(run_id):
            return self._start_batch_locked(run_id, plan_id, proposals)

    def _start_batch_locked(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None,
    ) -> list[CandidateTask]:
        run = self._load_run(run_id)
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot create candidates from state {run.state}")

        frozen = self._load_frozen_spec(run.frozen_spec_id)
        plan = self._load_plan(run_id, plan_id)
        all_records = self._load_candidate_records(run_id)
        plan_records = sorted(
            (record for record in all_records if record.task.plan_id == plan_id),
            key=lambda record: record.candidate_id,
        )
        for record in plan_records:
            if frozen.spec.strategy.worker_host != "pi-thinkthread":
                self._ensure_results_tsv(record, frozen.spec.metric_name)
            self._write_candidate_record(run_id, record)
        if all_records:
            highest_index = max(
                int(record.candidate_id.removeprefix("c")) for record in all_records
            )
            run.candidates_total = max(run.candidates_total, len(all_records))
            run.next_candidate_index = max(run.next_candidate_index, highest_index + 1)

        if plan.status == "started":
            records_by_id = {record.candidate_id: record for record in plan_records}
            try:
                tasks = [
                    records_by_id[candidate_id].task
                    for candidate_id in plan.started_candidate_ids
                ]
            except KeyError as exc:
                raise RuntimeError(
                    f"started plan {plan_id} is missing candidate state for {exc.args[0]}"
                ) from exc
            if tasks:
                run.state = RunState.WAITING_FOR_WORKERS
                self._write_run(run)
            return tasks
        if plan.status != "planned":
            raise RuntimeError(f"plan {plan_id} has already been started")

        remaining = max(
            0,
            frozen.spec.budget.max_parallel - run.candidates_total,
        )
        target_count = min(plan.planned_k, len(plan_records) + remaining)
        if target_count <= 0:
            return []

        if plan.requires_agent_proposals:
            if not proposals:
                raise ValueError("this strategy plan requires candidate proposals")
            self._validate_agent_proposals(plan, proposals)
            candidate_proposals = proposals[:target_count]
        else:
            if proposals:
                raise ValueError("this strategy plan already contains fixed work orders")
            candidate_proposals = [
                self._proposal_from_work_order(work_order) for work_order in plan.work_orders
            ][:target_count]

        if len(plan_records) > len(candidate_proposals):
            raise RuntimeError(
                f"plan {plan_id} has more persisted candidates than candidate proposals"
            )
        for record, proposal in zip(plan_records, candidate_proposals, strict=False):
            if record.task.proposal != proposal:
                raise RuntimeError(
                    f"retry proposals do not match persisted candidate "
                    f"{record.candidate_id}"
                )

        tasks = [record.task for record in plan_records]
        for index, proposal in enumerate(
            candidate_proposals[len(plan_records):], start=len(plan_records) + 1
        ):
            candidate_id = f"c{run.next_candidate_index:03d}"
            task = self._create_candidate_task(
                run=run,
                frozen=frozen,
                candidate_id=candidate_id,
                plan=plan,
                proposal=proposal,
                slot=index,
            )
            record = CandidateRecord(
                candidate_id=candidate_id,
                status="created",
                task=task,
                results_ledger=self._inherited_results_ledger(run, task),
            )
            if frozen.spec.strategy.worker_host != "pi-thinkthread":
                self._ensure_results_tsv(record, frozen.spec.metric_name)
                if record.results_ledger_git_head is not None:
                    record.settled_artifact_ref = GitCommitArtifactRef(
                        commit=record.results_ledger_git_head
                    )
            self._write_candidate_record(run_id, record)
            tasks.append(task)
            run.next_candidate_index += 1
            run.candidates_total += 1
            run.state = RunState.WAITING_FOR_WORKERS
            self._write_run(run)

        if tasks:
            run.state = RunState.WAITING_FOR_WORKERS
            plan.status = "started"
            plan.started_candidate_ids = [task.candidate_id for task in tasks]
            self._write_plan(plan)
            self._write_run(run)

        return tasks

    def start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        *,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Create a context/provenance handle and host-native launch payload.

        Does not start a worker or track lifecycle state. ``worker_budget`` is
        an optional one-dispatch override; it does not mutate the frozen spec or
        the candidate policy.
        """
        return self._create_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=directive,
            worker_budget_override=worker_budget,
            reuse_initial=True,
        )

    def redispatch_candidate(
        self,
        run_id: str,
        candidate_id: str,
        *,
        worker_agent_type: str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Create a new worker launch for an existing candidate workspace.

        This is state-level resume, not same-worker continuation. It allocates
        a new agent_session_id for the same candidate/workspace and may
        temporarily override the worker tier or budget for that launch. It does
        not mutate the candidate task policy or track host lifecycle state.
        """
        if worker_agent_type is not None and not worker_agent_type.strip():
            raise ValueError("worker_agent_type must be non-empty when provided")

        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        candidate_record = self._load_candidate_record(run_id, candidate_id)
        if candidate_record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot redispatch candidate in status {candidate_record.status}"
            )

        selected_worker_agent_type = (
            worker_agent_type
            or self._candidate_worker_agent_type(frozen, candidate_record)
        )
        worker_budget_override = self._resolve_worker_budget_for_dispatch(
            frozen=frozen,
            candidate_record=candidate_record,
            worker_budget_override=worker_budget,
        )
        previous_session_ids = [
            session["agent_session_id"]
            for session in self._agent_session_payloads_for_candidate(run_id, candidate_id)
        ]
        resume_directive = {
            "state_level_resume": True,
            "resume_candidate_id": candidate_id,
            "previous_agent_session_ids": previous_session_ids,
            "resume_instruction": (
                "这是现有候选的新 worker session。首先调用 search_get_agent_context，"
                "将其中本 candidate 的 iterations/results 作为权威恢复上下文，"
                "再读取 search_get_global_evidence。"
            ),
        }
        return self._create_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=resume_directive,
            worker_agent_type_override=selected_worker_agent_type,
            worker_budget_override=worker_budget_override,
        )

    def _create_agent_session(
        self,
        *,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None,
        worker_agent_type_override: str | None = None,
        worker_budget_override: dict[str, Any] | None = None,
        reuse_initial: bool = False,
    ) -> AgentSessionRecord:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            if run.state not in {
                RunState.RUNNING,
                RunState.WAITING_FOR_WORKERS,
                RunState.SELECTING,
                RunState.SELECTION_BLOCKED,
            }:
                raise RuntimeError(f"cannot start agent session from state {run.state}")
            frozen = self._load_frozen_spec(run.frozen_spec_id)

            candidate_record = self._load_candidate_record(run_id, candidate_id)
            workspace = candidate_record.task.workspace

            if worker_budget_override is not None:
                worker_budget_override = self._normalize_worker_budget_override(
                    worker_host=frozen.spec.strategy.worker_host,
                    worker_budget=worker_budget_override,
                )

            normalized_directive = self._normalize_main_directive(directive)
            if reuse_initial:
                session = next(
                    (
                        item
                        for item in self._load_agent_sessions(run_id)
                        if item.candidate_id == candidate_id
                    ),
                    None,
                )
                if session is not None:
                    expected_launch = self._build_launch_payload(
                        frozen=frozen,
                        candidate_id=candidate_id,
                        agent_session_id=session.agent_session_id,
                        directive=normalized_directive,
                        candidate_record=candidate_record,
                        worker_agent_type_override=worker_agent_type_override,
                        worker_budget_override=worker_budget_override,
                    )
                    if (
                        session.directive != normalized_directive
                        or session.launch != expected_launch
                    ):
                        raise RuntimeError(
                            f"candidate {candidate_id} initial agent session already exists "
                            "with different launch options"
                        )
                    return session

            agent_session_id = self._make_agent_session_id(
                run_id, run.next_agent_session_index
            )
            run.next_agent_session_index += 1
            now = utc_timestamp()
            launch = self._build_launch_payload(
                frozen=frozen,
                candidate_id=candidate_id,
                agent_session_id=agent_session_id,
                directive=normalized_directive,
                candidate_record=candidate_record,
                worker_agent_type_override=worker_agent_type_override,
                worker_budget_override=worker_budget_override,
            )
            host = frozen.spec.strategy.worker_host
            if host in {"pi-rpc", "pi-thinkthread"}:
                launch["run_id"] = run_id
            host_handle = AgentHostHandle(host=host)
            if host == "codex":
                host_handle = host_handle.model_copy(
                    update={"task_name": launch.get("task_name")}
                )
            elif host == "pi-rpc":
                host_handle = host_handle.model_copy(
                    update={
                        "external_id": launch.get("session_id", agent_session_id),
                        "metadata": {"continuation": "native_session"},
                    }
                )
            elif host == "pi-thinkthread":
                host_handle = host_handle.model_copy(
                    update={
                        "metadata": {
                            "continuation": "retained_child_session",
                            "fs_base_snapshot_id": candidate_record.task.fs_base_snapshot_id,
                        },
                    }
                )
            session = AgentSessionRecord(
                agent_session_id=agent_session_id,
                run_id=run_id,
                candidate_id=candidate_id,
                selected_model=candidate_record.task.selected_model,
                model_provenance=candidate_record.task.model_provenance,
                host=host,
                host_handle=host_handle,
                created_at=now,
                updated_at=now,
                directive=normalized_directive,
                workspace=workspace,
                launch=launch,
                counters={},
            )
            self._write_run(run)
            self._write_agent_session(session)
            return session

    def bind_agent_handle(
        self,
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> AgentSessionRecord:
        """Bind a runtime session to the host-specific worker handle."""
        session = self._load_agent_session_by_id(agent_session_id)
        host = handle.get("host", session.host)
        if host != session.host:
            raise ValueError(f"agent session host is {session.host}, got handle for {host}")

        metadata = {
            **session.host_handle.metadata,
            **dict(handle.get("metadata") or {}),
        }
        if session.host == "pi-rpc":
            prior_dispatches = session.host_handle.metadata.get("dispatches")
            dispatches = (
                list(prior_dispatches) if isinstance(prior_dispatches, list) else []
            )
            handle_metadata = dict(handle.get("metadata") or {})
            metrics = handle_metadata.get("pi_metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            dispatches.append(
                {
                    "process_pid": handle_metadata.get("process_pid"),
                    "started_at": metrics.get("dispatch_started_at")
                    or metrics.get("started_at"),
                    "ended_at": metrics.get("dispatch_ended_at")
                    or metrics.get("ended_at"),
                    "duration_seconds": metrics.get("dispatch_duration_seconds")
                    or metrics.get("duration_seconds"),
                    "usage": metrics.get("usage_delta"),
                    "baseline_last_entry_id": metrics.get("baseline_last_entry_id"),
                    "final_last_entry_id": metrics.get("final_last_entry_id"),
                    "timed_out": bool(handle_metadata.get("timed_out")),
                    "runner_failed": bool(handle_metadata.get("runner_failed")),
                }
            )
            metadata["dispatches"] = dispatches
            metadata["dispatch_count"] = len(dispatches)
        progress = metadata.get("progress_handoff")
        if isinstance(progress, dict) and not isinstance(progress.get("model_handoff"), dict):
            model_keys = {
                "key_results",
                "what_was_tried",
                "pitfalls",
                "blockers",
                "next_steps",
                "verifier_assessment",
            }
            if model_keys.intersection(progress):
                progress = {
                    "model_handoff": dict(progress),
                    "source": "bound_metadata",
                }

        model_handoff: dict[str, Any] | None = None
        handoff_error: str | None = None
        if session.workspace is not None:
            model_handoff, handoff_error = self._workspace_model_handoff(
                session.workspace
            )
        if model_handoff is not None:
            progress_payload = dict(progress) if isinstance(progress, dict) else {}
            progress_payload.update(
                {
                    "model_handoff": model_handoff,
                    "source_path": MODEL_HANDOFF_RELATIVE_PATH,
                }
            )
            metadata["progress_handoff"] = progress_payload
            metadata.pop("progress_handoff_error", None)
        elif isinstance(progress, dict):
            metadata["progress_handoff"] = progress
        if handoff_error:
            metadata["progress_handoff_error"] = handoff_error
        updated_handle = session.host_handle.model_copy(
            update={
                "host": session.host,
                "external_id": handle.get("external_id", session.host_handle.external_id),
                "task_name": handle.get("task_name", session.host_handle.task_name),
                "nickname": handle.get("nickname", session.host_handle.nickname),
                "metadata": metadata,
            }
        )
        updated = session.model_copy(
            update={
                "host_handle": updated_handle,
                "updated_at": utc_timestamp(),
            }
        )
        self._write_agent_session(updated)
        return updated

    @staticmethod
    def _workspace_model_handoff(workspace: Path) -> tuple[dict[str, Any] | None, str | None]:
        """Read a bounded candidate-authored handoff without failing handle binding."""
        workspace = workspace.resolve()
        handoff_path = workspace / MODEL_HANDOFF_RELATIVE_PATH
        if not handoff_path.exists():
            return None, None
        try:
            resolved = handoff_path.resolve(strict=True)
            if not resolved.is_relative_to(workspace):
                return None, "handoff path resolves outside the candidate workspace"
            if resolved.stat().st_size > MAX_MODEL_HANDOFF_BYTES:
                return None, f"handoff exceeds {MAX_MODEL_HANDOFF_BYTES} bytes"
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"could not read handoff: {type(exc).__name__}: {exc}"
        if not isinstance(payload, dict):
            return None, "handoff must be a JSON object"
        return payload, None

    def continue_agent_session(
        self,
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Return host launch fields that continue a prior worker session.

        This does not create a new candidate workspace. Hosts with native
        continuation reuse the bound worker; state-redispatch hosts return
        their explicit redispatch payload. ``worker_budget`` applies only to
        this continuation dispatch and does not mutate the frozen spec.
        """
        session = self._load_agent_session_by_id(agent_session_id)
        run = self._load_run(session.run_id)
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot continue agent session from state {run.state}")
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        candidate_record = self._load_candidate_record(
            session.run_id,
            session.candidate_id,
        )
        if candidate_record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot continue candidate in status {candidate_record.status}"
            )

        worker_budget_override = self._normalize_worker_budget_override(
            worker_host=session.host,
            worker_budget=worker_budget,
        )
        try:
            launch = self._build_continue_launch_payload(
                frozen=frozen,
                session=session,
                candidate_record=candidate_record,
                worker_budget_override=worker_budget_override,
            )
        except UnsupportedHostCapability as exc:
            raise RuntimeError(str(exc)) from exc
        counters = dict(session.counters)
        counters["resume_dispatches"] = counters.get("resume_dispatches", 0) + 1
        updated = session.model_copy(
            update={
                "updated_at": utc_timestamp(),
                "workspace": candidate_record.task.workspace,
                "launch": launch,
                "counters": counters,
            }
        )
        self._write_agent_session(updated)
        return updated

    def get_agent_observability(self, agent_session_id: str) -> dict[str, Any]:
        """Return normalized, read-only host evidence for one worker session."""
        session = self._load_agent_session_by_id(agent_session_id)
        adapter = get_agent_host_adapter(session.host)
        return adapter.collect_observability(session)

    def get_agent_context(self, agent_session_id: str) -> dict[str, Any]:
        """Subagent first call. Returns the authoritative ids, workspace, and
        candidate context. The subagent must treat prompt-supplied ids as
        labels only and rely on this response as the source of truth.
        """
        session = self._load_agent_session_by_id(agent_session_id)
        run = self._load_run(session.run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        candidate_record = self._load_candidate_record(session.run_id, session.candidate_id)
        previous_sessions: list[dict[str, Any]] = []
        latest_handoff: dict[str, Any] | None = None
        for previous in self._load_agent_sessions(session.run_id):
            if (
                previous.candidate_id != session.candidate_id
                or previous.agent_session_id == session.agent_session_id
            ):
                continue
            metadata = previous.host_handle.metadata
            progress_handoff = metadata.get("progress_handoff")
            if isinstance(progress_handoff, dict):
                latest_handoff = progress_handoff
            assistant_text = metadata.get("assistant_text")
            error = metadata.get("error")
            previous_sessions.append(
                {
                    "agent_session_id": previous.agent_session_id,
                    "timed_out": bool(metadata.get("timed_out")),
                    "runner_failed": bool(metadata.get("runner_failed")),
                    "assistant_summary": (
                        assistant_text[:2000] + ("..." if len(assistant_text) > 2000 else "")
                        if isinstance(assistant_text, str)
                        else None
                    ),
                    "progress_handoff": progress_handoff
                    if isinstance(progress_handoff, dict)
                    else None,
                    "error": (
                        error[:500] + ("..." if len(error) > 500 else "")
                        if isinstance(error, str)
                        else None
                    ),
                }
            )
        is_thinkthread = frozen.spec.strategy.worker_host == "pi-thinkthread"
        results_tsv: Path | None = None
        if not is_thinkthread:
            results_were_initialized = (
                candidate_record.results_ledger_git_head is not None
            )
            results_tsv = self._ensure_results_tsv(
                candidate_record,
                frozen.spec.metric_name,
            )
            if not results_were_initialized:
                self._write_candidate_record(session.run_id, candidate_record)
            workspace_status = self._git_status(candidate_record.task.workspace)
            workspace_resume = {
                "git_head": self._git_head(candidate_record.task.workspace),
                "git_status": workspace_status,
                "dirty": bool(workspace_status),
                "changed_files": self._detect_changed_files(
                    Path(run.source_path), candidate_record.task.workspace
                ),
            }
        else:
            workspace_resume = {
                "fs_branch_id": candidate_record.task.fs_branch_id,
                "baseline_artifact_ref": (
                    run.baseline_artifact_ref.model_dump(mode="json")
                    if run.baseline_artifact_ref is not None
                    else None
                ),
                "settled_artifact_ref": (
                    candidate_record.settled_artifact_ref.model_dump(mode="json")
                    if candidate_record.settled_artifact_ref is not None
                    else None
                ),
                "changed_files": list(candidate_record.detected_changed_files),
            }
        dispatch_count = session.host_handle.metadata.get("dispatch_count")
        dispatch_count = dispatch_count if isinstance(dispatch_count, int) else 0
        continuation_mode = session.launch.get("continuation")
        is_native_session_resume = (
            continuation_mode == "native_session" and dispatch_count > 0
        )
        supplemental_enabled = supplemental_evaluation_enabled()
        return {
            "agent_session_id": session.agent_session_id,
            "run_id": session.run_id,
            "candidate_id": session.candidate_id,
            "supplemental_evaluation_enabled": supplemental_enabled,
            "selected_model": (
                session.selected_model.model if session.selected_model else None
            ),
            "model_provenance": session.model_provenance,
            "host": session.host,
            "host_handle": session.host_handle.model_dump(mode="json"),
            "directive": session.directive,
            "workspace": "." if is_thinkthread else str(session.workspace),
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "run_budget": frozen.spec.budget.model_dump(mode="json"),
            "candidate_task": candidate_record.task.model_dump(mode="json"),
            "results_tsv": None if results_tsv is None else str(results_tsv),
            "results": [
                entry.model_dump(mode="json")
                for entry in candidate_record.results_ledger
            ],
            "resume": {
                "is_redispatch": bool(session.directive.get("state_level_resume")),
                "is_native_session_resume": is_native_session_resume,
                "mode": continuation_mode if is_native_session_resume else None,
                "dispatch_count": dispatch_count,
                "previous_sessions": previous_sessions,
                "latest_handoff": latest_handoff,
                "workspace": workspace_resume,
            },
            "iterations": self.list_iterations(session.run_id, session.candidate_id),
        }

    def get_global_evidence(self, agent_session_id: str) -> list[dict[str, Any]]:
        """Return settled worker evidence and any completed objective views."""
        session = self._load_agent_session_by_id(agent_session_id)
        with self._run_transaction(session.run_id):
            session = self._load_agent_session_by_id(
                agent_session_id,
                run_id=session.run_id,
            )
            run = self._load_run(session.run_id)
            view = self._global_evidence_view(session.run_id)
            self.attach_external_evaluations(session.run_id, view)
            frozen = self._load_frozen_spec(run.frozen_spec_id)
            mode = self._global_evidence_mode(frozen.spec.strategy.config)
            if mode == "independent":
                view = [
                    entry
                    for entry in view
                    if entry["candidate_id"] == session.candidate_id
                ]
            completed_views = [
                GlobalEvidenceViewReference(
                    candidate_id=str(entry["candidate_id"]),
                    iteration=int(entry["iteration"]),
                    artifact_ref=entry.get("artifact_ref"),
                    commit=(
                        str(entry["commit"])
                        if entry.get("commit") is not None
                        else None
                    ),
                    view_created_at=str(entry["view_created_at"]),
                    supplemental_evaluation_present=(
                        bool(entry.get("supplemental_available"))
                    ),
                )
                for entry in view
                if entry["view"] is not None
                and (
                    entry.get("artifact_ref") is not None
                    or entry.get("commit") is not None
                )
                and entry["view_created_at"] is not None
            ]
            read_record = GlobalEvidenceReadRecord(
                read_at=utc_timestamp(),
                evidence_count=len(view),
                completed_view_count=len(completed_views),
                completed_supplemental_evaluation_count=sum(
                    item.supplemental_evaluation_present
                    for item in completed_views
                ),
                completed_views=completed_views,
            )
            self._write_agent_session(
                session.model_copy(
                    update={
                        "updated_at": read_record.read_at,
                        "global_evidence_reads": [
                            *session.global_evidence_reads,
                            read_record,
                        ],
                    }
                )
            )
        self._kick_evidence_annotator(session.run_id)
        return view

    def get_evidence_detail(
        self,
        agent_session_id: str,
        candidate_id: str,
        iteration: int,
    ) -> dict[str, Any]:
        """Return one immutable supplemental evaluation visible to a worker."""
        session = self._load_agent_session_by_id(agent_session_id)
        if not supplemental_evaluation_enabled():
            raise RuntimeError("supplemental evaluation is disabled for this run")
        run = self._load_run(session.run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        if (
            self._global_evidence_mode(frozen.spec.strategy.config) == "independent"
            and candidate_id != session.candidate_id
        ):
            raise PermissionError(
                "independent Global Evidence only exposes the caller's candidate"
            )

        entry = next(
            (
                item
                for item in self._global_evidence_view(session.run_id)
                if item["candidate_id"] == candidate_id
                and item["iteration"] == iteration
            ),
            None,
        )
        if entry is None:
            raise ValueError("settled worker Evidence iteration not found")

        task = self._load_evidence_annotation_task(
            session.run_id, candidate_id, iteration
        )
        view = task.view if task is not None and task.state == "completed" else None
        if (
            task is None
            or view is None
            or task.run_id != session.run_id
            or task.candidate_id != candidate_id
            or task.iteration != iteration
            or task.attempt_commit != entry["commit"]
            or (
                task.attempt_ref.model_dump(mode="json")
                if task.attempt_ref is not None
                else None
            )
            != entry.get("artifact_ref")
        ):
            raise RuntimeError("supplemental Evidence identity does not match iteration")
        if view.supplemental_evaluation is None:
            raise RuntimeError("supplemental evaluation is not available")

        return {
            "candidate_id": candidate_id,
            "iteration": iteration,
            "artifact_ref": entry.get("artifact_ref"),
            "commit": entry["commit"],
            "supplemental_evaluation": view.supplemental_evaluation.model_dump(
                mode="json"
            ),
        }

    @staticmethod
    def _global_evidence_mode(config: dict[str, Any]) -> str:
        if "global_evidence_mode" in config:
            return str(config["global_evidence_mode"])
        if config.get("share_global_evidence") is False:
            return "independent"
        if config.get("inject_global_evidence_after_verifier") is True:
            return "auto"
        return "manual"

    def should_inject_global_evidence_after_verifier(self, run_id: str) -> bool:
        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        mode = self._global_evidence_mode(frozen.spec.strategy.config)
        return mode == "auto"

    def stage_shared_tool(
        self,
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        session = self._load_agent_session_by_id(agent_session_id)
        lock_path = self._candidate_dir(session.run_id, session.candidate_id) / "verifier.lock"
        with exclusive_file_lock(lock_path):
            with self._run_transaction(session.run_id):
                run = self._load_run(session.run_id)
                self._assert_worker_iteration_allowed(run, "stage shared tool")
                frozen = self._load_frozen_spec(run.frozen_spec_id)
                if not frozen.spec.shared_dir.enabled:
                    raise ValueError("tool staging requires shared_dir.enabled=true")
                record = self._load_candidate_record(session.run_id, session.candidate_id)
                if record.status not in {"created", "evaluated"}:
                    raise RuntimeError(
                        f"cannot stage a tool for candidate in status {record.status}"
                    )
                if frozen.spec.strategy.worker_host == "pi-thinkthread":
                    return self._stage_pi_thinkthread_shared_tool(
                        run=run,
                        frozen=frozen,
                        record=record,
                        name=name,
                        summary=summary,
                        entrypoint=entrypoint,
                        candidate_relative_source_paths=(
                            candidate_relative_source_paths
                        ),
                        idempotency_key=idempotency_key,
                    )
                if record.task.share_out_dir is None:
                    raise RuntimeError("shared-dir candidate has no share-out directory")
                limits = frozen.spec.shared_dir
                return SharedDirManager(self._run_dir(session.run_id)).stage_tool(
                    workspace=record.task.workspace,
                    share_out_dir=record.task.share_out_dir,
                    name=name,
                    summary=summary,
                    entrypoint=entrypoint,
                    candidate_relative_source_paths=candidate_relative_source_paths,
                    max_tools=limits.max_tools_per_iteration,
                    max_files=limits.max_files_per_iteration,
                    max_bytes=limits.max_bytes_per_iteration,
                    max_path_entries=limits.max_path_entries_per_iteration,
                    max_depth=limits.max_depth,
                )

    def _stage_pi_thinkthread_shared_tool(
        self,
        *,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = " ".join(name.split()).strip()
        normalized_summary = " ".join(summary.split()).strip()
        normalized_entrypoint = entrypoint.strip()
        if not normalized_name or len(normalized_name) > 120:
            raise ValueError("tool name must contain 1-120 characters")
        if not normalized_summary or len(normalized_summary) > 500:
            raise ValueError("tool summary must contain 1-500 characters")
        if not normalized_entrypoint or len(normalized_entrypoint) > 300:
            raise ValueError("tool entrypoint must contain 1-300 characters")
        limits = frozen.spec.shared_dir
        if not candidate_relative_source_paths:
            raise ValueError("candidate_relative_source_paths must not be empty")
        if len(candidate_relative_source_paths) > limits.max_files_per_iteration:
            raise ValueError("tool source list exceeds shared_dir file limit")
        prefix = PurePosixPath(TOOL_DRAFTS_RELATIVE_PATH)
        normalized_paths: list[str] = []
        for raw_path in candidate_relative_source_paths:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("tool source paths must be relative without '..'")
            try:
                relative = path.relative_to(prefix)
            except ValueError as exc:
                raise ValueError(
                    f"tool sources must be under {TOOL_DRAFTS_RELATIVE_PATH}"
                ) from exc
            if str(relative) == ".":
                raise ValueError("select explicit entries below the tool draft directory")
            normalized_paths.append(path.as_posix())
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("tool source paths must be unique")
        for left_index, left in enumerate(normalized_paths):
            left_path = PurePosixPath(left)
            for right in normalized_paths[left_index + 1 :]:
                right_path = PurePosixPath(right)
                if left_path in right_path.parents or right_path in left_path.parents:
                    raise ValueError("tool source paths must be non-overlapping")
        if idempotency_key is not None:
            existing = next(
                (
                    item
                    for item in record.pending_fs_tool_stages
                    if item.get("rpc_request_id") == idempotency_key
                ),
                None,
            )
            if existing is not None:
                expected = {
                    "name": normalized_name,
                    "summary": normalized_summary,
                    "entrypoint": normalized_entrypoint,
                    "source_paths": normalized_paths,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise RuntimeError(
                        "shared tool idempotency key was reused with new content"
                    )
                return {
                    **existing,
                    "staging_path": f"snapshot://next/{existing['staged_name']}",
                    "file_count": None,
                    "size_bytes": None,
                    "path_count": None,
                }
        if len(record.pending_fs_tool_stages) >= limits.max_tools_per_iteration:
            raise ValueError("pending shared tools exceed max_tools_per_iteration")
        stage_id = f"stage_{uuid.uuid4().hex}"
        staged_name = f"tool-{sha256_text(normalized_name)[:12]}-{stage_id[-8:]}"
        stage = {
            "stage_id": stage_id,
            "staged_name": staged_name,
            "name": normalized_name,
            "summary": normalized_summary,
            "entrypoint": normalized_entrypoint,
            "source_paths": normalized_paths,
            "staged_at": utc_timestamp(),
            **(
                {"rpc_request_id": idempotency_key}
                if idempotency_key is not None
                else {}
            ),
        }
        record.pending_fs_tool_stages.append(stage)
        self._write_candidate_record(run.run_id, record)
        return {
            **stage,
            "staging_path": f"snapshot://next/{staged_name}",
            "file_count": None,
            "size_bytes": None,
            "path_count": None,
        }

    def copy_shared_tool(
        self,
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        session = self._load_agent_session_by_id(agent_session_id)
        lock_path = self._candidate_dir(session.run_id, session.candidate_id) / "verifier.lock"
        with exclusive_file_lock(lock_path):
            with self._run_transaction(session.run_id):
                run = self._load_run(session.run_id)
                self._assert_worker_iteration_allowed(run, "copy shared tool")
                frozen = self._load_frozen_spec(run.frozen_spec_id)
                if not frozen.spec.shared_dir.enabled:
                    raise ValueError("tool copy requires shared_dir.enabled=true")
                record = self._load_candidate_record(session.run_id, session.candidate_id)
                if (
                    len(record.pending_tool_copies)
                    >= frozen.spec.shared_dir.max_tools_per_iteration
                ):
                    raise ValueError("pending tool copies exceed shared_dir max_tools_per_iteration")
                if any(item.tool_id == tool_id for item in record.pending_tool_copies):
                    existing = next(
                        item
                        for item in record.pending_tool_copies
                        if item.tool_id == tool_id
                    )
                    if (
                        idempotency_key is not None
                        and existing.rpc_request_id == idempotency_key
                        and existing.snapshot_hash == snapshot_hash
                    ):
                        return {
                            **existing.model_dump(mode="json"),
                            "state": "copy_required_at_turn_boundary",
                            "logical_inbox": (
                                f"{TOOL_INBOX_RELATIVE_PATH}/{existing.receipt_id}"
                            ),
                        }
                    raise ValueError(
                        "tool already copied for the next verifier iteration: "
                        f"{tool_id}"
                    )
                tool = self._resolve_shared_tool(session.run_id, tool_id, snapshot_hash)
                if frozen.spec.strategy.worker_host == "pi-thinkthread":
                    base_ref = self._fs_snapshot_ref(
                        record.settled_artifact_ref or run.baseline_artifact_ref,
                        field="tool copy candidate base",
                    )
                    receipt = ToolCopyReceipt(
                        receipt_id=f"copy_{uuid.uuid4().hex[:24]}",
                        rpc_request_id=idempotency_key,
                        tool_id=tool.tool_id,
                        snapshot_hash=tool.snapshot_hash,
                        source_artifact_ref=tool.source_artifact_ref,
                        source_commit=tool.source_commit,
                        agent_session_id=agent_session_id,
                        candidate_base_artifact_ref=base_ref,
                        copied_at=utc_timestamp(),
                    )
                    record.pending_tool_copies.append(receipt)
                    self._write_candidate_record(session.run_id, record)
                    return {
                        **receipt.model_dump(mode="json"),
                        "state": "copy_required_at_turn_boundary",
                        "logical_inbox": (
                            f"{TOOL_INBOX_RELATIVE_PATH}/{receipt.receipt_id}"
                        ),
                    }
                if record.results_ledger_git_head is None:
                    raise RuntimeError("tool copy requires a Git-backed candidate")
                receipt_id = f"copy_{uuid.uuid4().hex[:24]}"
                inbox_path = record.task.workspace / TOOL_INBOX_RELATIVE_PATH / receipt_id
                manager = SharedDirManager(self._run_dir(session.run_id))
                try:
                    manager.materialize_tool(tool, inbox_path)
                except Exception:
                    shutil.rmtree(inbox_path, ignore_errors=True)
                    raise
                receipt = ToolCopyReceipt(
                    receipt_id=receipt_id,
                    tool_id=tool.tool_id,
                    snapshot_hash=tool.snapshot_hash,
                    source_artifact_ref=(
                        tool.source_artifact_ref
                        or (
                            GitCommitArtifactRef(commit=tool.source_commit)
                            if tool.source_commit is not None
                            else None
                        )
                    ),
                    source_commit=tool.source_commit,
                    agent_session_id=agent_session_id,
                    candidate_base_artifact_ref=GitCommitArtifactRef(
                        commit=record.results_ledger_git_head
                    ),
                    candidate_base_git_head=record.results_ledger_git_head,
                    inbox_path=inbox_path,
                    copied_at=utc_timestamp(),
                )
                record.pending_tool_copies.append(receipt)
                self._write_candidate_record(session.run_id, record)
        return receipt.model_dump(mode="json")

    def run_verifier(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ScoreReport:
        if scope not in {"process", "promotion"}:
            raise ValueError("verifier scope must be 'process' or 'promotion'")
        lock_path = self._candidate_dir(run_id, candidate_id) / "verifier.lock"
        with exclusive_file_lock(lock_path):
            if idempotency_key is not None:
                record = self._load_candidate_record(run_id, candidate_id)
                matching = [
                    item
                    for item in record.iterations
                    if item.rpc_request_id == idempotency_key
                ]
                if len(matching) > 1:
                    raise RuntimeError(
                        "verifier idempotency key is bound to multiple iterations"
                    )
                if matching:
                    if (
                        matching[0] is not record.iterations[-1]
                        or record.score_report is None
                    ):
                        raise RuntimeError(
                            "verifier idempotency replay cannot recover its exact report"
                        )
                    run = self._load_run(run_id)
                    frozen = self._load_frozen_spec(run.frozen_spec_id)
                    if frozen.spec.strategy.worker_host != "pi-thinkthread":
                        raise RuntimeError(
                            "verifier idempotency keys are reserved for pi-thinkthread"
                        )
                    self._reconcile_pi_thinkthread_iteration_replay(
                        run_id=run_id,
                        candidate_id=candidate_id,
                        iteration=matching[0],
                        report=record.score_report,
                    )
                    if scope == "process" and agent_session_id is not None:
                        self._kick_evidence_annotator(run_id)
                    self._close_fs_requests_after_evidence(
                        run_id,
                        list(matching[0].verifier_request_ids),
                        self._agent_posix_client(),
                    )
                    return record.score_report
            if scope == "process" and agent_session_id is None:
                with self._run_transaction(run_id):
                    record = self._load_candidate_record(run_id, candidate_id)
                    if record.pending_tool_copies:
                        raise RuntimeError(
                            "parent process verifier cannot settle a candidate "
                            "with pending tool copies"
                        )
            report = self._run_verifier(
                run_id,
                candidate_id,
                scope=scope,
                agent_session_id=agent_session_id,
                hypothesis=hypothesis,
                toolization_decision=toolization_decision,
                idempotency_key=idempotency_key,
            )
        if scope == "process" and agent_session_id is not None:
            self._kick_evidence_annotator(run_id)
        return report

    def _reconcile_pi_thinkthread_iteration_replay(
        self,
        *,
        run_id: str,
        candidate_id: str,
        iteration: IterationRecord,
        report: ScoreReport,
    ) -> None:
        """Repair run-derived state after candidate Evidence won a crash race.

        ``candidate.json`` and ``run.json`` are separate atomic files. A worker
        RPC replay can therefore observe the exact durable iteration before
        the corresponding run best/restore intent was written. Rebuild only
        the idempotent derived state bound to that iteration before returning
        its saved report.
        """

        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            frozen = self._load_frozen_spec(run.frozen_spec_id)
            record = self._load_candidate_record(run_id, candidate_id)
            exact = next(
                (
                    item
                    for item in record.iterations
                    if item.rpc_request_id == iteration.rpc_request_id
                    and item.iteration == iteration.iteration
                    and item.attempt_ref == iteration.attempt_ref
                ),
                None,
            )
            if exact is None or exact is not record.iterations[-1]:
                raise RuntimeError(
                    "verifier idempotency replay lost its exact candidate Evidence"
                )

            replay_requests = [
                item
                for item in run.fs_requests
                if item.request_id in exact.verifier_request_ids
            ]
            reason = str(run.budget_used.get("needs_recovery_reason") or "")
            if (
                run.state == RunState.NEEDS_RECOVERY
                and replay_requests
                and all(
                    item.state in {"succeeded", "failed", "cancelled", "closed"}
                    for item in replay_requests
                )
                and any(item.request_id in reason for item in replay_requests)
            ):
                previous = run.budget_used.pop(
                    "fs_recovery_previous_state", RunState.RUNNING.value
                )
                run.budget_used.pop("needs_recovery_reason", None)
                run.state = RunState(str(previous))

            if exact.disposition in {"discard", "failure"}:
                attempt = self._fs_snapshot_ref(
                    exact.attempt_ref,
                    field="replayed restore attempt",
                )
                target = self._fs_snapshot_ref(
                    exact.settled_ref,
                    field="replayed restore target",
                )
                branch_id = record.task.fs_branch_id
                if not isinstance(branch_id, str):
                    raise RuntimeError("replayed restore omitted candidate branch")
                if not any(
                    item.get("kind") == "branch_restore"
                    and item.get("candidate_id") == candidate_id
                    and item.get("branch_id") == branch_id
                    and item.get("attempt_snapshot_id") == attempt.snapshot_id
                    and item.get("target_snapshot_id") == target.snapshot_id
                    for item in run.fs_cleanup
                ):
                    run.fs_cleanup.append(
                        {
                            "kind": "branch_restore",
                            "state": "restore_required",
                            "candidate_id": candidate_id,
                            "branch_id": branch_id,
                            "attempt_snapshot_id": attempt.snapshot_id,
                            "target_snapshot_id": target.snapshot_id,
                            "created_at": utc_timestamp(),
                        }
                    )

            should_write_best = False
            if (
                exact.disposition in {"keep", "retain"}
                and report.aggregate_score is not None
            ):
                if run.best_score is None:
                    should_write_best = True
                else:
                    better = (
                        report.aggregate_score > run.best_score
                        if frozen.spec.metric_direction == "maximize"
                        else report.aggregate_score < run.best_score
                    )
                    if better or run.best_candidate_id == candidate_id:
                        should_write_best = True
                    elif report.aggregate_score == run.best_score:
                        best_path = self._run_dir(run_id) / "best.json"
                        if not best_path.exists():
                            should_write_best = True
                        else:
                            prior = BestArtifactRecord.model_validate(
                                load_json(best_path)
                            )
                            should_write_best = prior.updated_at <= exact.created_at
                if should_write_best:
                    run.best_score = report.aggregate_score
                    run.best_candidate_id = candidate_id
                    self._write_best_fs_artifact(
                        run,
                        frozen.spec,
                        record,
                        exact,
                    )

            run.candidates_evaluated = len(
                [
                    item
                    for item in self._load_candidate_records(run_id)
                    if item.status == "evaluated"
                ]
            )
            self._write_run(run)
            try:
                self._create_evidence_annotation_task(
                    run_id,
                    frozen,
                    candidate_id,
                    exact,
                )
            except Exception:
                pass

        if exact.agent_session_id is not None:
            session = self._load_agent_session_by_id(
                exact.agent_session_id,
                run_id=run_id,
            )
            expected_runs = sum(
                1
                for item in record.iterations
                if item.agent_session_id == exact.agent_session_id
            )
            counters = dict(session.counters)
            counters["verifier_runs"] = max(
                int(counters.get("verifier_runs", 0)),
                expected_runs,
            )
            session.counters = counters
            session.updated_at = utc_timestamp()
            self._write_agent_session(session)

    def finalize_pi_thinkthread_candidate(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        idempotency_key: str | None = None,
    ) -> ScoreReport:
        """Bind a turn-boundary verifier to the retained Child session.

        The pool calls this only after ThinkThread reports that the Child
        execution is absent.  It captures the private branch once more so edits
        made after the worker's last verifier cannot escape exact-snapshot
        settlement.
        """

        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.worker_host != "pi-thinkthread":
            raise RuntimeError(
                "turn-boundary exact verification requires worker_host=pi-thinkthread"
            )
        session = self._load_agent_session_by_id(agent_session_id, run_id=run_id)
        if session.candidate_id != candidate_id or session.host != "pi-thinkthread":
            raise PermissionError(
                "turn-boundary verifier session does not match the ThinkThread candidate"
            )
        child_id = session.host_handle.external_id
        if not child_id:
            raise RuntimeError("turn-boundary verifier has no retained Child id")
        child = self._agent_posix_client().invoke(
            "thinkthread.get",
            {"id": child_id},
        )
        if child.get("executionState") != "absent":
            raise RuntimeError(
                "turn-boundary verifier requires absent Child execution"
            )
        return self.run_verifier(
            run_id,
            candidate_id,
            scope="process",
            agent_session_id=agent_session_id,
            hypothesis="parent turn-boundary exact snapshot verification",
            idempotency_key=idempotency_key,
        )

    def _run_verifier(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ScoreReport:
        """Subagent self-score with ``agent_session_id``; main final verify
        without it. Process calls record ranking iterations; promotion calls
        retain separate acceptance evidence.
        """
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            if scope == "process":
                recovery_replay = bool(
                    idempotency_key is not None
                    and run.state == RunState.NEEDS_RECOVERY
                    and any(
                        item.context.get("rpc_request_id") == idempotency_key
                        for item in run.fs_requests
                    )
                )
                if not recovery_replay:
                    self._assert_worker_iteration_allowed(run, "run verifier")
            else:
                self._assert_run_not_invalidated(run, "run verifier")
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        if not frozen.spec.shared_dir.enabled:
            toolization_outcome = (
                toolization_decision.get("outcome")
                if isinstance(toolization_decision, dict)
                else getattr(toolization_decision, "outcome", None)
            )
            if toolization_outcome == "not_applicable":
                toolization_decision = None
        normalized_toolization_decision = (
            ToolizationDecision.model_validate(toolization_decision)
            if toolization_decision is not None
            else None
        )
        if normalized_toolization_decision is not None and (
            scope != "process" or agent_session_id is None
        ):
            raise ValueError(
                "toolization_decision is only valid for worker process verifier calls"
            )
        if (
            normalized_toolization_decision is not None
            and not frozen.spec.shared_dir.enabled
        ):
            raise ValueError("toolization_decision requires shared_dir.enabled=true")
        record = self._load_candidate_record(run_id, candidate_id)
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot verify candidate in status {record.status}"
            )
        if scope == "promotion":
            if agent_session_id is not None:
                raise PermissionError(
                    "promotion verification is parent-owned and cannot be "
                    "called from a candidate agent session"
                )
            selected_identity_present = (
                isinstance(run.selected_artifact_ref, FsSnapshotArtifactRef)
                if frozen.spec.strategy.worker_host == "pi-thinkthread"
                else bool(run.selected_git_head)
            )
            if (
                run.state != RunState.READY_TO_PROMOTE
                or run.selected_candidate_id != candidate_id
                or not selected_identity_present
            ):
                raise RuntimeError(
                    "promotion verification requires the candidate and immutable "
                    "artifact selected by search_select"
                )

        session: AgentSessionRecord | None = None
        if agent_session_id:
            session = self._load_agent_session_by_id(agent_session_id, run_id=run_id)
            if session.candidate_id != candidate_id:
                raise ValueError(
                    "agent_session_id does not belong to this candidate"
                )
            if scope == "process":
                hypothesis = self._tsv_cell(hypothesis or "")
                if not hypothesis:
                    raise ValueError(
                        "worker process verifier requires a non-empty hypothesis"
                    )

        if scope == "process":
            outer_deadline = self._outer_deadline_epoch(
                os.environ.get(OUTER_DEADLINE_ENV)
            )
            if outer_deadline is not None:
                verifier_seconds = sum(
                    float(command.timeout_seconds)
                    + 2 * VERIFIER_TERM_GRACE_SECONDS
                    for command in frozen.spec.process_verifiers
                )
                closeout_seconds = max(
                    0.0,
                    float(
                        frozen.spec.strategy.config.get(
                            "reserve_closeout_seconds", 0
                        )
                        or 0
                    ),
                )
                remaining_seconds = outer_deadline - time.time()
                required_seconds = verifier_seconds + closeout_seconds
                if remaining_seconds < required_seconds:
                    raise RuntimeError(
                        "VerifierDeadlineInsufficient: process verifier not started; "
                        f"{remaining_seconds:.1f}s remain, but the verifier suite may "
                        f"use {verifier_seconds:.1f}s and closeout reserves "
                        f"{closeout_seconds:.1f}s"
                    )

        if frozen.spec.strategy.worker_host == "pi-thinkthread":
            return self._run_pi_thinkthread_verifier(
                run=run,
                frozen=frozen,
                record=record,
                scope=scope,
                session=session,
                hypothesis=hypothesis,
                toolization_decision=normalized_toolization_decision,
                idempotency_key=idempotency_key,
            )

        results_were_initialized = record.results_ledger_git_head is not None
        self._ensure_results_tsv(record, frozen.spec.metric_name)
        if not results_were_initialized:
            self._write_candidate_record(run_id, record)
        pre_attempt_settled_head = record.results_ledger_git_head

        try:
            state = self._candidate_artifact_state(run, frozen, record)
            self._apply_candidate_artifact_state(record, state)
            if scope == "process":
                attempt_git_head = self._commit_workspace_iteration(
                    record.task.workspace,
                    (
                        f"search verifier iteration "
                        f"{candidate_id}:{len(record.iterations) + 1}"
                    ),
                )
                if attempt_git_head is None or pre_attempt_settled_head is None:
                    raise RuntimeError(
                        "candidate verifier requires a committed attempt and settled head"
                    )
                if self._git_returncode(
                    record.task.workspace,
                    [
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        pre_attempt_settled_head,
                        attempt_git_head,
                    ],
                ) != 0:
                    raise RuntimeError(
                        "candidate Git history diverged from the settled results ledger"
                    )
                attempt_changed_files = self._git_changed_files(
                    record.task.workspace,
                    pre_attempt_settled_head,
                    attempt_git_head,
                )
                state = self._candidate_artifact_state(run, frozen, record)
                self._apply_candidate_artifact_state(record, state)
                if state.git_head != attempt_git_head or not state.git_artifact_clean:
                    raise RuntimeError(
                        "candidate attempt is not fully captured by its Git commit"
                    )

            precheck = self._precheck_candidate(frozen, record)
            if precheck is not None:
                report = precheck
            else:
                commands = (
                    frozen.spec.process_verifiers
                    if scope == "process"
                    else frozen.spec.promotion_verifiers
                )
                if not commands:
                    commands = frozen.spec.process_verifiers
                report = self._run_commands(run, frozen, record, commands, scope)

            if scope == "process":
                return self._settle_process_verifier(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    frozen=frozen,
                    report=report,
                    attempt=state,
                    pre_attempt_settled_head=pre_attempt_settled_head,
                    attempt_changed_files=attempt_changed_files,
                    hypothesis=hypothesis,
                    agent_session_id=agent_session_id,
                    session=session,
                    toolization_decision=normalized_toolization_decision,
                )

            if report.promotion_passed is None:
                report = report.model_copy(
                    update={"promotion_passed": report.process_passed}
                )
            state = self._candidate_artifact_state(run, frozen, record)

            with self._run_transaction(run_id):
                run = self._load_run(run_id)
                self._assert_run_not_invalidated(run, "record verifier result")
                record = self._load_candidate_record(run_id, candidate_id)
                self._apply_candidate_artifact_state(record, state)
                record.promotion_report = report
                selected_ref = (
                    GitCommitArtifactRef(commit=run.selected_git_head)
                    if run.selected_git_head is not None
                    else None
                )
                record.promotion_evidence = PromotionEvidence(
                    candidate_id=candidate_id,
                    selected_artifact_ref=selected_ref,
                    artifact_ref=selected_ref,
                    selected_git_head=run.selected_git_head,
                    git_head=run.selected_git_head,
                    artifact_hash=state.artifact_hash,
                    passed=bool(report.promotion_passed),
                    created_at=utc_timestamp(),
                )
                self._write_candidate_record(run_id, record)
                self._write_run(run)

            return report
        except Exception:
            if scope == "process":
                with self._run_transaction(run_id):
                    run = self._load_run(run_id)
                    if (
                        not run.invalidated_at
                        and run.state in WORKER_ITERATION_RUN_STATES
                    ):
                        run.state = RunState.FAILED
                        self._write_run(run)
            raise

    @staticmethod
    def _fs_snapshot_ref(
        reference: object,
        *,
        field: str,
    ) -> FsSnapshotArtifactRef:
        if not isinstance(reference, FsSnapshotArtifactRef):
            raise RuntimeError(
                f"pi-thinkthread {field} must be an exact FsSnapshot artifact"
            )
        return reference

    @staticmethod
    def _fs_join_path(prefix: str, path: str) -> str:
        normalized_prefix = PurePosixPath(prefix or ".")
        normalized_path = PurePosixPath(path)
        if normalized_path.is_absolute() or ".." in normalized_path.parts:
            raise ValueError(f"invalid fs-relative path: {path!r}")
        joined = (
            normalized_path
            if str(normalized_prefix) == "."
            else normalized_prefix / normalized_path
        )
        return joined.as_posix()

    @staticmethod
    def _fs_source_projected_path(source_prefix: str, path: str) -> str:
        prefix = PurePosixPath(source_prefix or ".")
        candidate = PurePosixPath(path)
        if str(prefix) == ".":
            return candidate.as_posix()
        try:
            return candidate.relative_to(prefix).as_posix()
        except ValueError:
            return f"@workspace/{candidate.as_posix()}"

    def _mark_fs_recovery(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> None:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            if run.state != RunState.NEEDS_RECOVERY:
                run.budget_used.setdefault(
                    "fs_recovery_previous_state",
                    (
                        run.state.value
                        if isinstance(run.state, RunState)
                        else str(run.state)
                    ),
                )
            run.state = RunState.NEEDS_RECOVERY
            run.budget_used["needs_recovery_reason"] = reason
            self._write_run(run)

    def _append_fs_request(
        self,
        run_id: str,
        request: FsRequestRecord,
    ) -> None:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            if any(item.request_id == request.request_id for item in run.fs_requests):
                raise RuntimeError(f"duplicate fs request id: {request.request_id}")
            run.fs_requests.append(request)
            self._write_run(run)

    def _update_fs_request(
        self,
        run_id: str,
        request_id: str,
        *,
        state: str,
        result: dict[str, Any] | None | object = _UNSET,
        error: dict[str, Any] | None | object = _UNSET,
        closed_at: str | None = None,
    ) -> None:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            request = next(
                (item for item in run.fs_requests if item.request_id == request_id),
                None,
            )
            if request is None:
                raise RuntimeError(f"fs request is not persisted: {request_id}")
            request.state = state  # type: ignore[assignment]
            if result is not _UNSET:
                request.result = result  # type: ignore[assignment]
            if error is not _UNSET:
                request.error = error  # type: ignore[assignment]
            request.updated_at = utc_timestamp()
            request.closed_at = closed_at
            self._write_run(run)

    def _recover_fs_request_result(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        request_id: str,
        operation: str,
        deadline: float,
    ) -> dict[str, Any] | None:
        while True:
            try:
                status = client.invoke(
                    "fs.request.status",
                    {"requestId": request_id},
                )
            except AgentPosixBridgeError as exc:
                if exc.code == "RequestNotFound":
                    return None
                self._update_fs_request(
                    run_id,
                    request_id,
                    state="needs_recovery",
                    error={
                        "message": str(exc),
                        "code": exc.code,
                        "delivery": exc.delivery,
                    },
                )
                self._mark_fs_recovery(
                    run_id,
                    reason=(
                        f"cannot inspect outcome of {operation} request {request_id}: "
                        f"{exc}"
                    ),
                )
                raise RuntimeError(
                    f"ThinkThreadRequestNeedsRecovery: {request_id}"
                ) from exc
            if status.get("requestId") != request_id or status.get("method") != operation:
                raise RuntimeError(
                    f"fs.request.status identity mismatch for {request_id}"
                )
            state = status.get("state")
            if state in {"accepted", "running"}:
                self._update_fs_request(
                    run_id,
                    request_id,
                    state="running" if state == "running" else "accepted",
                )
                if time.monotonic() >= deadline:
                    self._update_fs_request(
                        run_id,
                        request_id,
                        state="needs_recovery",
                        error={"message": "request remained non-terminal at deadline"},
                    )
                    self._mark_fs_recovery(
                        run_id,
                        reason=f"{operation} request {request_id} remained {state}",
                    )
                    raise RuntimeError(
                        f"ThinkThreadRequestNeedsRecovery: {request_id} is {state}"
                    )
                time.sleep(0.05)
                continue
            if state == "succeeded" or state == "cancelled":
                result = status.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"terminal fs request {request_id} omitted result"
                    )
                self._update_fs_request(
                    run_id,
                    request_id,
                    state="cancelled" if state == "cancelled" else "succeeded",
                    result=result,
                )
                return result
            if state == "failed":
                error = status.get("error")
                normalized = error if isinstance(error, dict) else {}
                self._update_fs_request(
                    run_id,
                    request_id,
                    state="failed",
                    error=normalized,
                )
                raise AgentPosixBridgeError(
                    f"ThinkThread {operation} request failed: {request_id}",
                    error={"rejection": {"error": normalized}},
                )
            self._update_fs_request(
                run_id,
                request_id,
                state="needs_recovery",
                error={"message": f"request state is {state!r}"},
            )
            self._mark_fs_recovery(
                run_id,
                reason=f"{operation} request {request_id} is {state!r}",
            )
            raise RuntimeError(
                f"ThinkThreadRequestNeedsRecovery: {request_id} is {state!r}"
            )

    def _invoke_durable_fs_operation(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        request_id: str,
        method: str,
        params: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds + 30.0
        try:
            result = client.invoke(
                method,
                params,
                timeout_seconds=timeout_seconds + 30.0,
            )
        except AgentPosixBridgeError as exc:
            if not exc.completion_unknown and exc.code != "RequestInProgress":
                try:
                    recovered = self._recover_fs_request_result(
                        client=client,
                        run_id=run_id,
                        request_id=request_id,
                        operation=method,
                        deadline=deadline,
                    )
                except AgentPosixBridgeError:
                    raise
                if recovered is None:
                    self._update_fs_request(
                        run_id,
                        request_id,
                        state="failed",
                        error={
                            "message": str(exc),
                            "code": exc.code,
                            "delivery": exc.delivery,
                        },
                    )
                    raise
                return recovered
            recovered = self._recover_fs_request_result(
                client=client,
                run_id=run_id,
                request_id=request_id,
                operation=method,
                deadline=deadline,
            )
            if recovered is not None:
                return recovered
            # Inspection proved that admission did not persist the operation.
            # Reusing the same RequestId preserves idempotency.
            try:
                result = client.invoke(
                    method,
                    params,
                    timeout_seconds=timeout_seconds + 30.0,
                )
            except AgentPosixBridgeError as retry_exc:
                recovered = self._recover_fs_request_result(
                    client=client,
                    run_id=run_id,
                    request_id=request_id,
                    operation=method,
                    deadline=deadline,
                )
                if recovered is None:
                    if (
                        retry_exc.completion_unknown
                        or retry_exc.code == "RequestInProgress"
                    ):
                        self._update_fs_request(
                            run_id,
                            request_id,
                            state="needs_recovery",
                            error={
                                "message": str(retry_exc),
                                "code": retry_exc.code,
                                "delivery": retry_exc.delivery,
                            },
                        )
                        self._mark_fs_recovery(
                            run_id,
                            reason=(
                                f"{method} request {request_id} remained "
                                "unobservable after idempotent replay"
                            ),
                        )
                        raise RuntimeError(
                            f"ThinkThreadRequestNeedsRecovery: {request_id}"
                        ) from retry_exc
                    self._update_fs_request(
                        run_id,
                        request_id,
                        state="failed",
                        error={
                            "message": str(retry_exc),
                            "code": retry_exc.code,
                            "delivery": retry_exc.delivery,
                        },
                    )
                    raise
                return recovered
        self._update_fs_request(
            run_id,
            request_id,
            state="succeeded",
            result=result,
        )
        return result

    def _invoke_durable_fs_run(
        self,
        *,
        client: AgentPosixSdkClient,
        run_id: str,
        request_id: str,
        params: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        return self._invoke_durable_fs_operation(
            client=client,
            run_id=run_id,
            request_id=request_id,
            method="fs.run",
            params=params,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _decode_fs_run_output(result: dict[str, Any]) -> tuple[str, str]:
        chunks = result.get("outputChunks")
        if not isinstance(chunks, list):
            raise ValueError("fs.run omitted outputChunks")
        ordered: list[tuple[int, str, bytes]] = []
        seen: set[int] = set()
        for raw in chunks:
            if not isinstance(raw, dict):
                raise ValueError("fs.run output chunk is not an object")
            sequence = raw.get("sequence")
            stream = raw.get("stream")
            encoded = raw.get("dataBase64")
            if (
                not isinstance(sequence, int)
                or sequence < 0
                or sequence in seen
                or stream not in {"stdout", "stderr"}
                or not isinstance(encoded, str)
            ):
                raise ValueError("fs.run returned an invalid output chunk")
            seen.add(sequence)
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("fs.run returned invalid output base64") from exc
            ordered.append((sequence, stream, data))
        ordered.sort(key=lambda item: item[0])
        stdout = b"".join(data for _, stream, data in ordered if stream == "stdout")
        stderr = b"".join(data for _, stream, data in ordered if stream == "stderr")
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _fs_exit_status(result: dict[str, Any]) -> tuple[int | None, str | None]:
        exit_status = result.get("exit")
        if not isinstance(exit_status, dict):
            return None, "VerifierInfrastructureFailure"
        kind = exit_status.get("kind")
        if kind == "code" and isinstance(exit_status.get("code"), int):
            return int(exit_status["code"]), None
        if kind == "signal" and isinstance(exit_status.get("signal"), int):
            return -int(exit_status["signal"]), "VerifierSignal"
        return {
            "timeout": (None, "Timeout"),
            "killed": (None, "VerifierKilled"),
            "cancelled": (None, "VerifierCancelled"),
        }.get(str(kind), (None, "VerifierInfrastructureFailure"))

    def _fs_internal_verifier(
        self,
        *,
        reader: FsSnapshotArtifactReader,
        artifact: FsSnapshotArtifactRef,
        source_prefix: str,
        frozen: FrozenSpec,
        command: VerifierCommand,
    ) -> VerifierResult:
        if len(command.command) < 2 or command.command[1] != "check-frozen-hashes":
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"error": "unknown internal command"},
                failure_class="UnknownInternalCommand",
            )
        failures = self._fs_frozen_hash_failures(
            reader=reader,
            artifact=artifact,
            source_prefix=source_prefix,
            frozen=frozen,
        )
        return VerifierResult(
            name=command.name,
            role=command.role,
            passed=not failures,
            score=1.0 if not failures else 0.0,
            metrics={"hash_failures": failures},
            failure_class=None if not failures else "FrozenVerifierModified",
        )

    def _fs_frozen_hash_failures(
        self,
        *,
        reader: FsSnapshotArtifactReader,
        artifact: FsSnapshotArtifactRef,
        source_prefix: str,
        frozen: FrozenSpec,
    ) -> dict[str, dict[str, str | None]]:
        failures: dict[str, dict[str, str | None]] = {}
        for relative_path, expected in frozen.verifier_hashes.items():
            try:
                data = reader.read_file(
                    artifact,
                    self._fs_join_path(source_prefix, relative_path),
                    max_bytes=64 * 1024 * 1024,
                )
            except (AgentPosixBridgeError, ValueError):
                actual = None
            else:
                actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                failures[relative_path] = {
                    "expected": expected,
                    "actual": actual,
                }
        return failures

    def _fs_run_verifier_command(
        self,
        *,
        client: AgentPosixSdkClient,
        reader: FsSnapshotArtifactReader,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        artifact: FsSnapshotArtifactRef,
        command: VerifierCommand,
        verifier_phase: Literal["candidate", "promotion"],
        idempotency_key: str | None = None,
    ) -> tuple[VerifierResult, str | None]:
        if command.command[0] == "goal-plus-internal":
            return (
                self._fs_internal_verifier(
                    reader=reader,
                    artifact=artifact,
                    source_prefix=run.fs_source_relative_path or ".",
                    frozen=frozen,
                    command=command,
                ),
                None,
            )
        current_run = self._load_run(run.run_id)
        existing = next(
            (
                item
                for item in current_run.fs_requests
                if idempotency_key is not None
                and item.operation == "run"
                and item.context.get("rpc_request_id") == idempotency_key
                and item.context.get("verifier") == command.name
                and item.context.get("phase") == verifier_phase
            ),
            None,
        )
        if existing is None:
            request_id = new_request_id()
            now = utc_timestamp()
            self._append_fs_request(
                run.run_id,
                FsRequestRecord(
                    request_id=request_id,
                    operation="run",
                    context={
                        "candidate_id": record.candidate_id,
                        "snapshot_id": artifact.snapshot_id,
                        "verifier": command.name,
                        "phase": verifier_phase,
                        **(
                            {"rpc_request_id": idempotency_key}
                            if idempotency_key is not None
                            else {}
                        ),
                    },
                    created_at=now,
                    updated_at=now,
                ),
            )
            existing_result: dict[str, Any] | None = None
        else:
            if (
                existing.context.get("candidate_id") != record.candidate_id
                or existing.context.get("snapshot_id") != artifact.snapshot_id
            ):
                raise RuntimeError(
                    "verifier idempotency key changed candidate or snapshot"
                )
            request_id = existing.request_id
            existing_result = (
                existing.result
                if existing.state in {"succeeded", "closed"}
                and isinstance(existing.result, dict)
                else None
            )
        logs_dir = (
            self._candidate_dir(run.run_id, record.candidate_id)
            / "logs"
            / ("process" if verifier_phase == "candidate" else "promotion")
        )
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / (
            f"iteration-{len(record.iterations) + 1:04d}-"
            f"{safe_verifier_name(command.name)}-{uuid.uuid4().hex[:8]}.log"
        )
        source_prefix = run.fs_source_relative_path or "."
        argv = [
            sys.executable,
            "-m",
            "goal_plus.revision_verifier",
            "--source-path",
            source_prefix,
            "--cwd",
            command.cwd,
            "--phase",
            verifier_phase,
            "--",
            *command.command,
        ]
        fs_environment = {"PATH": os.environ.get("PATH", os.defpath)}
        virtual_env = os.environ.get("VIRTUAL_ENV")
        if virtual_env:
            fs_environment["VIRTUAL_ENV"] = virtual_env
        params = {
            "snapshotId": artifact.snapshot_id,
            "invocation": {
                "argv": argv,
                "cwd": ".",
                "environment": fs_environment,
            },
            "writes": "discard",
            "limits": {
                "timeoutMs": command.timeout_seconds * 1000,
                "maxOutputBytes": VERIFIER_OUTPUT_LIMIT_BYTES,
            },
            "requestId": request_id,
        }
        start = time.perf_counter()
        try:
            with verifier_resource_lock(command.resource_lock):
                result = existing_result or self._invoke_durable_fs_run(
                    client=client,
                    run_id=run.run_id,
                    request_id=request_id,
                    params=params,
                    timeout_seconds=command.timeout_seconds,
                )
                stdout, stderr = self._decode_fs_run_output(result)
                elapsed = time.perf_counter() - start
                metrics = self._parse_metrics(stdout)
                returncode, exit_failure = self._fs_exit_status(result)
                metrics.setdefault("returncode", returncode)
                metrics.setdefault("elapsed_seconds", elapsed)
                metrics["fs_execution_metrics"] = result.get("metrics", {})
                metrics["retained_output_bytes"] = result.get("retainedOutputBytes")
                metrics["observed_output_bytes"] = result.get("observedOutputBytes")
                truncated = result.get("outputTruncated") is True
                score = self._score_from_metrics(frozen.spec.metric_name, metrics)
                has_verifier_error = self._has_verifier_error(metrics)
                missing_numeric_metric = (
                    returncode == 0
                    and not has_verifier_error
                    and command.role == VerifierRole.RANKING_SIGNAL
                    and score is None
                )
                if missing_numeric_metric:
                    metrics["expected_metric_name"] = frozen.spec.metric_name
                if truncated:
                    metrics["infrastructure_failure"] = True
                    metrics["candidate_action"] = "stop_and_report"
                passed = bool(
                    returncode == 0
                    and exit_failure is None
                    and not truncated
                    and not has_verifier_error
                    and not missing_numeric_metric
                )
                failure_class = (
                    None
                    if passed
                    else "VerifierInfrastructureFailure"
                    if truncated
                    else "MissingNumericMetric"
                    if missing_numeric_metric
                    else exit_failure
                    or "VerifierCommandFailed"
                )
                if not passed:
                    self._add_visible_verifier_feedback(
                        command,
                        metrics,
                        stdout=stdout,
                        stderr=stderr,
                    )
                log_path.write_text(
                    _bounded_log(
                        "\n".join(
                            [
                                f"$ {' '.join(command.command)}",
                                f"snapshot: {artifact.snapshot_id}",
                                f"request_id: {request_id}",
                                f"returncode: {returncode}",
                                f"exit: {result.get('exit')}",
                                f"output_truncated: {truncated}",
                                "",
                                "## stdout",
                                stdout,
                                "## stderr",
                                stderr,
                            ]
                        )
                    ),
                    encoding="utf-8",
                )
                return (
                    VerifierResult(
                        name=command.name,
                        role=command.role,
                        passed=passed,
                        score=score,
                        metrics=metrics,
                        log_path=log_path,
                        failure_class=failure_class,
                    ),
                    request_id,
                )
        except AgentPosixBridgeError as exc:
            # The durable request record already distinguishes a terminal
            # platform failure from completion-unknown recovery. Surface the
            # former as verifier infrastructure Evidence so the worker can
            # react and the terminal RequestId can be closed after that
            # Evidence is persisted. RuntimeError remains reserved for the
            # explicit needs_recovery path below.
            log_path.write_text(_bounded_log(str(exc)), encoding="utf-8")
            return (
                VerifierResult(
                    name=command.name,
                    role=command.role,
                    passed=False,
                    score=0.0,
                    metrics={
                        "error": str(exc),
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "infrastructure_failure": True,
                        "candidate_action": "stop_and_report",
                    },
                    log_path=log_path,
                    failure_class="VerifierInfrastructureFailure",
                ),
                request_id,
            )
        except RuntimeError:
            raise
        except ValueError as exc:
            log_path.write_text(_bounded_log(str(exc)), encoding="utf-8")
            return (
                VerifierResult(
                    name=command.name,
                    role=command.role,
                    passed=False,
                    score=0.0,
                    metrics={
                        "error": str(exc),
                        "infrastructure_failure": True,
                        "candidate_action": "stop_and_report",
                    },
                    log_path=log_path,
                    failure_class="VerifierInfrastructureFailure",
                ),
                request_id,
            )

    def _fs_score_report(
        self,
        *,
        run: RunRecord,
        record: CandidateRecord,
        frozen: FrozenSpec,
        results: list[VerifierResult],
        scope: Literal["process", "promotion"],
        touched_denied_files: bool,
        changed_outside_allowed: bool,
    ) -> ScoreReport:
        hard_failed = any(
            not result.passed
            and result.role
            in {
                VerifierRole.VALIDITY_GATE,
                VerifierRole.PROCESS_GATE,
                VerifierRole.PROMOTION_GATE,
                VerifierRole.ANTI_CHEAT_GATE,
            }
            for result in results
        )
        process_passed = not hard_failed and all(
            result.passed or result.role == VerifierRole.DIAGNOSTIC_SIGNAL
            for result in results
        )
        score = self._aggregate_score(frozen.spec.metric_name, results)
        if not process_passed:
            score = 0.0
        return ScoreReport(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            parent_id=record.task.parent_id,
            validity_passed=process_passed,
            process_passed=process_passed,
            promotion_passed=process_passed if scope == "promotion" else None,
            aggregate_score=score,
            verifier_results=results,
            touched_denied_files=touched_denied_files,
            changed_outside_allowed=changed_outside_allowed,
            hardcoding_suspected=False,
        )

    def _materialize_fs_tool_path(
        self,
        *,
        client: AgentPosixSdkClient,
        snapshot_id: str,
        snapshot_path: str,
        destination: Path,
        usage: dict[str, int],
        max_files: int,
        max_bytes: int,
        max_path_entries: int,
        max_depth: int,
        depth: int,
    ) -> None:
        if depth > max_depth:
            raise ValueError(f"shared tool exceeds max depth {max_depth}")
        entry = client.invoke(
            "fs.snapshot.stat",
            {"snapshotId": snapshot_id, "path": snapshot_path},
        )
        kind = entry.get("kind")
        usage["paths"] += 1
        if usage["paths"] > max_path_entries:
            raise ValueError("shared tool exceeds path-entry limit")
        if kind == "symlink":
            raise ValueError("shared tool sources cannot contain symbolic links")
        if kind == "file":
            usage["files"] += 1
            if usage["files"] > max_files:
                raise ValueError("shared tool exceeds file limit")
            remaining = max_bytes - usage["bytes"]
            if remaining < 0:
                raise ValueError("shared tool exceeds byte limit")
            data = client.snapshot_read_file(
                snapshot_id,
                snapshot_path,
                max_bytes=remaining,
            )
            usage["bytes"] += len(data)
            if usage["bytes"] > max_bytes:
                raise ValueError("shared tool exceeds byte limit")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            mode = entry.get("mode")
            if isinstance(mode, int):
                destination.chmod(mode & 0o777)
            return
        if kind != "directory":
            raise ValueError(f"unsupported shared tool entry kind: {kind!r}")
        destination.mkdir(parents=True, exist_ok=True)
        for child in client.snapshot_readdir_all(snapshot_id, snapshot_path):
            child_path = fs_path_text(child.get("path"))
            child_posix = PurePosixPath(child_path)
            if child_posix.parent != PurePosixPath(snapshot_path):
                raise ValueError("snapshot readdir returned a non-immediate child")
            self._materialize_fs_tool_path(
                client=client,
                snapshot_id=snapshot_id,
                snapshot_path=child_path,
                destination=destination / child_posix.name,
                usage=usage,
                max_files=max_files,
                max_bytes=max_bytes,
                max_path_entries=max_path_entries,
                max_depth=max_depth,
                depth=depth + 1,
            )

    def _settle_pi_thinkthread_shared_tools(
        self,
        *,
        client: AgentPosixSdkClient,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        attempt_ref: FsSnapshotArtifactRef,
        iteration: int,
        settlement_id: str | None = None,
    ) -> SharedDirSettlement | None:
        stages = list(record.pending_fs_tool_stages)
        if not stages:
            return None
        limits = frozen.spec.shared_dir
        run_dir = self._run_dir(run.run_id)
        with tempfile.TemporaryDirectory(
            prefix=f"fs-share-{record.candidate_id}-",
            dir=run_dir,
        ) as temporary:
            share_out = Path(temporary) / "share-out"
            share_out.mkdir()
            usage = {"files": 0, "bytes": 0, "paths": 0}
            for stage in stages:
                destination = share_out / str(stage["staged_name"])
                destination.mkdir()
                usage["paths"] += 1
                manifest = {
                    "name": stage["name"],
                    "summary": stage["summary"],
                    "entrypoint": stage["entrypoint"],
                }
                manifest_bytes = (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                usage["files"] += 1
                usage["paths"] += 1
                usage["bytes"] += len(manifest_bytes)
                if (
                    usage["files"] > limits.max_files_per_iteration
                    or usage["paths"] > limits.max_path_entries_per_iteration
                    or usage["bytes"] > limits.max_bytes_per_iteration
                ):
                    raise ValueError("staged shared tools exceed configured limits")
                (destination / "manifest.json").write_bytes(manifest_bytes)
                draft_prefix = PurePosixPath(TOOL_DRAFTS_RELATIVE_PATH)
                for relative_source in stage["source_paths"]:
                    source_path = PurePosixPath(relative_source)
                    relative = source_path.relative_to(draft_prefix)
                    snapshot_path = self._fs_join_path(
                        run.fs_source_relative_path or ".",
                        source_path.as_posix(),
                    )
                    self._materialize_fs_tool_path(
                        client=client,
                        snapshot_id=attempt_ref.snapshot_id,
                        snapshot_path=snapshot_path,
                        destination=destination / relative.as_posix(),
                        usage=usage,
                        max_files=limits.max_files_per_iteration,
                        max_bytes=limits.max_bytes_per_iteration,
                        max_path_entries=limits.max_path_entries_per_iteration,
                        max_depth=limits.max_depth,
                        depth=len(relative.parts),
                    )
            return SharedDirManager(run_dir).settle_iteration(
                candidate_id=record.candidate_id,
                iteration=iteration,
                source_commit=None,
                source_artifact_ref=attempt_ref,
                share_out_dir=share_out,
                max_tools=limits.max_tools_per_iteration,
                max_files=limits.max_files_per_iteration,
                max_bytes=limits.max_bytes_per_iteration,
                max_path_entries=limits.max_path_entries_per_iteration,
                max_depth=limits.max_depth,
                settlement_id=settlement_id,
            )

    def capture_pi_thinkthread_branch_snapshot(
        self,
        *,
        run_id: str,
        candidate_id: str,
        branch_id: str,
        purpose: str,
        client: AgentPosixSdkClient,
        intent_id: str | None = None,
    ) -> tuple[str, str]:
        """Capture one Child branch behind a durable caller-owned RequestId.

        The Goal Plus request record and creation intent are persisted before
        invoking ThinkThread. A transport-ambiguous response is reconciled or
        replayed with the same RequestId, so the platform cannot create an
        unidentifiable duplicate snapshot.
        """

        resolved_intent_id = intent_id or f"snapshot_{uuid.uuid4().hex}"
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            record = self._load_candidate_record(run_id, candidate_id)
            intent = next(
                (
                    item
                    for item in record.fs_snapshot_intents
                    if item.intent_id == resolved_intent_id
                ),
                None,
            )
            if intent is None:
                now = utc_timestamp()
                request_id = new_request_id()
                intent = FsSnapshotCreationIntent(
                    intent_id=resolved_intent_id,
                    operation="branch_snapshot",
                    request_id=request_id,
                    branch_id=branch_id,
                    purpose=purpose,
                    created_at=now,
                    updated_at=now,
                )
                record.fs_snapshot_intents.append(intent)
                run.fs_requests.append(
                    FsRequestRecord(
                        request_id=request_id,
                        operation="branch_snapshot",
                        context={
                            "candidate_id": candidate_id,
                            "branch_id": branch_id,
                            "intent_id": resolved_intent_id,
                            "purpose": purpose,
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                if intent.branch_id != branch_id:
                    raise RuntimeError(
                        "branch snapshot intent changed branch during recovery"
                    )
                if intent.snapshot_id is not None and intent.state in {
                    "created",
                    "cleaned",
                }:
                    if intent.request_id is None:
                        raise RuntimeError(
                            "legacy branch snapshot intent omitted request_id"
                        )
                    return intent.request_id, intent.snapshot_id
                if intent.request_id is None:
                    if intent.state != "prepared":
                        raise RuntimeError(
                            "legacy branch snapshot mutation cannot be recovered "
                            "without a RequestId"
                        )
                    intent.request_id = new_request_id()
                request_id = intent.request_id
                if not any(
                    item.request_id == request_id for item in run.fs_requests
                ):
                    now = utc_timestamp()
                    run.fs_requests.append(
                        FsRequestRecord(
                            request_id=request_id,
                            operation="branch_snapshot",
                            context={
                                "candidate_id": candidate_id,
                                "branch_id": branch_id,
                                "intent_id": resolved_intent_id,
                                "purpose": purpose,
                            },
                            created_at=now,
                            updated_at=now,
                        )
                    )
            intent.state = "platform_mutation_started"
            intent.updated_at = utc_timestamp()
            self._write_candidate_record(run_id, record)
            self._write_run(run)

        try:
            result = self._invoke_durable_fs_operation(
                client=client,
                run_id=run_id,
                request_id=request_id,
                method="fs.branch.snapshot",
                params={"branchId": branch_id, "requestId": request_id},
                timeout_seconds=60,
            )
        except AgentPosixBridgeError:
            with self._run_transaction(run_id):
                record = self._load_candidate_record(run_id, candidate_id)
                intent = next(
                    item
                    for item in record.fs_snapshot_intents
                    if item.intent_id == resolved_intent_id
                )
                intent.state = "failed"
                intent.updated_at = utc_timestamp()
                self._write_candidate_record(run_id, record)
            raise
        except RuntimeError:
            with self._run_transaction(run_id):
                record = self._load_candidate_record(run_id, candidate_id)
                intent = next(
                    item
                    for item in record.fs_snapshot_intents
                    if item.intent_id == resolved_intent_id
                )
                intent.state = "needs_recovery"
                intent.updated_at = utc_timestamp()
                self._write_candidate_record(run_id, record)
            raise

        snapshot_id = result.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            self._mark_fs_recovery(
                run_id,
                reason=(
                    f"fs.branch.snapshot request {request_id} returned no snapshotId"
                ),
            )
            with self._run_transaction(run_id):
                record = self._load_candidate_record(run_id, candidate_id)
                intent = next(
                    item
                    for item in record.fs_snapshot_intents
                    if item.intent_id == resolved_intent_id
                )
                intent.state = "needs_recovery"
                intent.updated_at = utc_timestamp()
                self._write_candidate_record(run_id, record)
            raise RuntimeError("fs.branch.snapshot omitted snapshotId")

        with self._run_transaction(run_id):
            record = self._load_candidate_record(run_id, candidate_id)
            intent = next(
                item
                for item in record.fs_snapshot_intents
                if item.intent_id == resolved_intent_id
            )
            intent.state = "created"
            intent.snapshot_id = snapshot_id
            intent.updated_at = utc_timestamp()
            self._write_candidate_record(run_id, record)
        return request_id, snapshot_id

    def recover_pi_thinkthread_snapshot_requests(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Reconcile durable snapshot captures after a Goal Plus process crash."""

        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.worker_host != "pi-thinkthread":
            raise ValueError(
                "snapshot request recovery is only available for pi-thinkthread"
            )
        client = self._agent_posix_client()
        client.preflight()
        resolved: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []

        for persisted in list(run.fs_requests):
            if persisted.operation not in {"root_snapshot", "branch_snapshot"}:
                continue
            request_id = persisted.request_id
            method = (
                "fs.snapshot.create"
                if persisted.operation == "root_snapshot"
                else "fs.branch.snapshot"
            )
            branch_id = persisted.context.get("branch_id")
            params: dict[str, Any] = {"requestId": request_id}
            if persisted.operation == "branch_snapshot":
                if not isinstance(branch_id, str):
                    failed.append(
                        {
                            "request_id": request_id,
                            "error": "branch snapshot request omitted branch_id",
                        }
                    )
                    continue
                params["branchId"] = branch_id

            result = (
                persisted.result
                if persisted.state in {"succeeded", "closed"}
                and isinstance(persisted.result, dict)
                else None
            )
            try:
                if result is None:
                    result = self._invoke_durable_fs_operation(
                        client=client,
                        run_id=run_id,
                        request_id=request_id,
                        method=method,
                        params=params,
                        timeout_seconds=120,
                    )
                snapshot_id = result.get("snapshotId")
                if not isinstance(snapshot_id, str) or not snapshot_id.startswith(
                    "fsnap-"
                ):
                    raise RuntimeError(f"{method} recovery omitted snapshotId")

                if persisted.operation == "root_snapshot":
                    with self._run_transaction(run_id):
                        latest = self._load_run(run_id)
                        intent = next(
                            (
                                item
                                for item in latest.fs_snapshot_intents
                                if item.request_id == request_id
                            ),
                            None,
                        )
                        if intent is None:
                            raise RuntimeError(
                                "root snapshot request has no durable creation intent"
                            )
                        intent.state = "created"
                        intent.snapshot_id = snapshot_id
                        intent.updated_at = utc_timestamp()
                        if intent.purpose == "initial_baseline":
                            latest.baseline_artifact_ref = FsSnapshotArtifactRef(
                                snapshot_id=snapshot_id
                            )
                        self._write_run(latest)
                else:
                    candidate_id = persisted.context.get("candidate_id")
                    if not isinstance(candidate_id, str):
                        raise RuntimeError(
                            "branch snapshot request omitted candidate_id"
                        )
                    with self._run_transaction(run_id):
                        record = self._load_candidate_record(run_id, candidate_id)
                        intent = next(
                            (
                                item
                                for item in record.fs_snapshot_intents
                                if item.request_id == request_id
                            ),
                            None,
                        )
                        if intent is None:
                            raise RuntimeError(
                                "branch snapshot request has no durable creation intent"
                            )
                        intent.state = "created"
                        intent.snapshot_id = snapshot_id
                        intent.updated_at = utc_timestamp()
                        self._write_candidate_record(run_id, record)

                if persisted.state != "closed":
                    self._close_fs_requests_after_evidence(
                        run_id, [request_id], client
                    )
                resolved.append(
                    {"request_id": request_id, "snapshot_id": snapshot_id}
                )
            except (AgentPosixBridgeError, RuntimeError) as exc:
                failed.append({"request_id": request_id, "error": str(exc)})

        with self._run_transaction(run_id):
            latest = self._load_run(run_id)
            snapshot_request_ids = {
                item.request_id
                for item in latest.fs_requests
                if item.operation in {"root_snapshot", "branch_snapshot"}
            }
            recovery_reason = str(
                latest.budget_used.get("needs_recovery_reason") or ""
            )
            previous = latest.budget_used.get("fs_recovery_previous_state")
            other_fs_recovery = any(
                item.operation not in {"root_snapshot", "branch_snapshot"}
                and item.state in {"accepted", "running", "needs_recovery"}
                for item in latest.fs_requests
            )
            snapshot_recovery_owned = bool(
                previous is not None
                and any(request_id in recovery_reason for request_id in snapshot_request_ids)
            )
            if (
                not failed
                and latest.baseline_artifact_ref is not None
                and snapshot_recovery_owned
                and not other_fs_recovery
                and not (
                    latest.publication is not None
                    and latest.publication.state == "outcome_unknown"
                )
                and not latest.budget_used.get("fs_cleanup_recovery_active")
            ):
                previous = latest.budget_used.pop(
                    "fs_recovery_previous_state", None
                )
                latest.budget_used.pop("needs_recovery_reason", None)
                if latest.state == RunState.NEEDS_RECOVERY:
                    if isinstance(previous, str) and previous.startswith(
                        "RunState."
                    ):
                        previous = previous.removeprefix("RunState.").lower()
                    latest.state = (
                        RunState(previous)
                        if isinstance(previous, str)
                        else RunState.RUNNING
                    )
            self._write_run(latest)
            state = str(latest.state)
        return {"state": state, "resolved": resolved, "failed": failed}

    def _capture_pi_thinkthread_attempt(
        self,
        *,
        client: AgentPosixSdkClient,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        session: AgentSessionRecord,
        idempotency_key: str | None = None,
    ) -> _FsAttemptState:
        branch_id = record.task.fs_branch_id
        if not branch_id:
            raise RuntimeError("pi-thinkthread candidate has no bound fs branch")
        child_id = session.host_handle.external_id
        execution_state_before: str | None = None
        if child_id:
            try:
                child_before = client.invoke("thinkthread.get", {"id": child_id})
            except AgentPosixBridgeError:
                pass
            else:
                raw_execution_state = child_before.get("executionState")
                if isinstance(raw_execution_state, str):
                    execution_state_before = raw_execution_state
        base_ref = self._fs_snapshot_ref(
            record.settled_artifact_ref or run.baseline_artifact_ref,
            field="candidate settled artifact",
        )
        request_id, snapshot_id = self.capture_pi_thinkthread_branch_snapshot(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            branch_id=branch_id,
            purpose=(
                f"candidate {record.candidate_id} verifier iteration "
                f"{len(record.iterations) + 1}"
            ),
            client=client,
            intent_id=(
                f"rpc-verifier-{idempotency_key}"
                if idempotency_key is not None
                else None
            ),
        )
        attempt_ref = FsSnapshotArtifactRef(snapshot_id=snapshot_id)

        continuation_required = False
        if child_id:
            try:
                child = client.invoke(
                    "thinkthread.get",
                    {"id": child_id},
                )
            except AgentPosixBridgeError:
                continuation_required = execution_state_before in {None, "running"}
            else:
                continuation_required = bool(
                    execution_state_before in {None, "running"}
                    and child.get("executionState") != "running"
                )
            if continuation_required:
                latest_session = self._load_agent_session_by_id(
                    session.agent_session_id,
                    run_id=run.run_id,
                )
                metadata = dict(latest_session.host_handle.metadata)
                metadata.update(
                    {
                        "continuation_state": "needs_recovery",
                        "continuation_snapshot_id": snapshot_id,
                    }
                )
                latest_session.host_handle.metadata = metadata
                latest_session.updated_at = utc_timestamp()
                self._write_agent_session(latest_session)

        reader = FsSnapshotArtifactReader(client)
        baseline_ref = self._fs_snapshot_ref(
            run.baseline_artifact_ref,
            field="run baseline",
        )
        source_prefix = run.fs_source_relative_path or "."
        changed_root_paths = reader.changed_files(base_ref, attempt_ref)
        changed_files = [
            self._fs_source_projected_path(source_prefix, path)
            for path in changed_root_paths
        ]
        touched_denied = any(
            path_matches(path, frozen.spec.edit_surface.deny)
            for path in changed_files
        )
        outside_allowed = any(
            not path_matches(path, frozen.spec.edit_surface.allow)
            for path in changed_files
        )
        if (
            frozen.spec.edit_surface.max_file_changes is not None
            and len(changed_files) > frozen.spec.edit_surface.max_file_changes
        ):
            outside_allowed = True
        return _FsAttemptState(
            base_ref=base_ref,
            attempt_ref=attempt_ref,
            changed_files=changed_files,
            actual_diff=reader.diff(
                base_ref,
                attempt_ref,
                max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
            ),
            cumulative_diff=reader.diff(
                baseline_ref,
                attempt_ref,
                max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
            ),
            touched_denied_files=touched_denied,
            changed_outside_allowed=outside_allowed,
            artifact_hash=reader.canonical_digest(baseline_ref, attempt_ref),
            continuation_required=continuation_required,
            snapshot_request_id=request_id,
        )

    @staticmethod
    def _fs_iteration_eligible(iteration: IterationRecord) -> bool:
        return bool(
            iteration.process_passed is True
            and iteration.score is not None
            and math.isfinite(iteration.score)
            and isinstance(iteration.attempt_ref, FsSnapshotArtifactRef)
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
        )

    @classmethod
    def _fs_iteration_disposition(
        cls,
        iteration: IterationRecord,
        prior_best: IterationRecord | None,
        metric_direction: Literal["maximize", "minimize"],
    ) -> IterationDisposition:
        if not cls._fs_iteration_eligible(iteration):
            return "failure"
        if prior_best is None:
            return "keep"
        assert iteration.score is not None and prior_best.score is not None
        improved = (
            iteration.score > prior_best.score
            if metric_direction == "maximize"
            else iteration.score < prior_best.score
        )
        if improved:
            return "keep"
        if iteration.score == prior_best.score:
            return "retain"
        return "discard"

    def _write_best_fs_artifact(
        self,
        run: RunRecord,
        spec: SearchSpec,
        record: CandidateRecord,
        iteration: IterationRecord,
    ) -> None:
        artifact = self._fs_snapshot_ref(
            iteration.attempt_ref,
            field="best iteration",
        )
        if iteration.artifact_hash is None or iteration.score is None:
            raise RuntimeError("run best FsSnapshot iteration is incomplete")
        best = BestArtifactRecord(
            schema_version=2,
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            iteration=iteration.iteration,
            artifact_ref=artifact,
            score=iteration.score,
            metric_name=spec.metric_name,
            metric_direction=spec.metric_direction,
            artifact_hash=iteration.artifact_hash,
            changed_files=iteration.changed_files,
            updated_at=iteration.created_at,
        )
        write_json(
            self._run_dir(run.run_id) / "best.json",
            best.model_dump(mode="json"),
        )

    def _close_fs_requests_after_evidence(
        self,
        run_id: str,
        request_ids: list[str],
        client: AgentPosixSdkClient,
    ) -> None:
        self._retry_pending_fs_request_closes(run_id, client)
        for request_id in request_ids:
            try:
                client.invoke("fs.request.close", {"requestId": request_id})
            except AgentPosixBridgeError as exc:
                if exc.code == "RequestNotFound":
                    self._update_fs_request(
                        run_id,
                        request_id,
                        state="closed",
                        closed_at=utc_timestamp(),
                    )
                    continue
                with self._run_transaction(run_id):
                    run = self._load_run(run_id)
                    pending = next(
                        (
                            item
                            for item in reversed(run.fs_cleanup)
                            if item.get("kind") == "request_close"
                            and item.get("request_id") == request_id
                            and item.get("state") == "needs_recovery"
                        ),
                        None,
                    )
                    if pending is None:
                        pending = {
                            "kind": "request_close",
                            "state": "needs_recovery",
                            "request_id": request_id,
                            "created_at": utc_timestamp(),
                        }
                        run.fs_cleanup.append(pending)
                    pending["error"] = str(exc)
                    pending["updated_at"] = utc_timestamp()
                    pending["attempts"] = int(pending.get("attempts", 0)) + 1
                    self._write_run(run)
                continue
            self._update_fs_request(
                run_id,
                request_id,
                state="closed",
                closed_at=utc_timestamp(),
            )

    def _retry_pending_fs_request_closes(
        self,
        run_id: str,
        client: AgentPosixSdkClient,
    ) -> None:
        run = self._load_run(run_id)
        pending_ids = list(
            dict.fromkeys(
                str(item["request_id"])
                for item in run.fs_cleanup
                if item.get("kind") == "request_close"
                and item.get("state") == "needs_recovery"
                and isinstance(item.get("request_id"), str)
            )
        )
        for request_id in pending_ids:
            error: str | None = None
            try:
                client.invoke("fs.request.close", {"requestId": request_id})
            except AgentPosixBridgeError as exc:
                if exc.code != "RequestNotFound":
                    error = str(exc)
            if error is not None:
                with self._run_transaction(run_id):
                    latest = self._load_run(run_id)
                    for item in latest.fs_cleanup:
                        if (
                            item.get("kind") == "request_close"
                            and item.get("request_id") == request_id
                            and item.get("state") == "needs_recovery"
                        ):
                            item["error"] = error
                            item["updated_at"] = utc_timestamp()
                            item["attempts"] = int(item.get("attempts", 0)) + 1
                    self._write_run(latest)
                continue
            self._update_fs_request(
                run_id,
                request_id,
                state="closed",
                closed_at=utc_timestamp(),
            )
            with self._run_transaction(run_id):
                latest = self._load_run(run_id)
                for item in latest.fs_cleanup:
                    if (
                        item.get("kind") == "request_close"
                        and item.get("request_id") == request_id
                        and item.get("state") == "needs_recovery"
                    ):
                        item["state"] = "closed"
                        item["recovered_at"] = utc_timestamp()
                        item.pop("error", None)
                self._write_run(latest)

    def complete_pi_thinkthread_restore(
        self,
        *,
        run_id: str,
        candidate_id: str,
        branch_id: str,
        target_snapshot_id: str,
    ) -> None:
        target = FsSnapshotArtifactRef(snapshot_id=target_snapshot_id)
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            record = self._load_candidate_record(run_id, candidate_id)
            if record.task.fs_branch_id != branch_id:
                raise RuntimeError("restored branch does not match candidate binding")
            matching = [
                item
                for item in run.fs_cleanup
                if item.get("kind") == "branch_restore"
                and item.get("candidate_id") == candidate_id
                and item.get("branch_id") == branch_id
                and item.get("target_snapshot_id") == target_snapshot_id
                and item.get("state")
                in {"restore_required", "restoring", "restored"}
            ]
            if not matching:
                raise RuntimeError("no durable restore intent matches branch reset")
            matching[-1]["state"] = "restored"
            matching[-1]["restored_at"] = utc_timestamp()
            record.settled_artifact_ref = target
            if record.iterations:
                latest = record.iterations[-1]
                if latest.disposition in {"discard", "failure"}:
                    latest.settled_ref = target
                    prior = next(
                        (
                            item
                            for item in reversed(record.iterations[:-1])
                            if item.attempt_ref == target
                        ),
                        None,
                    )
                    latest.restored_to_iteration = (
                        prior.iteration if prior is not None else None
                    )
            self._write_candidate_record(run_id, record)
            self._write_run(run)

    def _pi_tool_copy_mutations(
        self,
        *,
        run: RunRecord,
        record: CandidateRecord,
        receipt: ToolCopyReceipt,
    ) -> list[dict[str, Any]]:
        tool = self._resolve_shared_tool(
            run.run_id,
            receipt.tool_id,
            receipt.snapshot_hash,
        )
        manager = SharedDirManager(self._run_dir(run.run_id))
        manager.tool_view_input(tool, max_content_bytes=0)
        destination_root = PurePosixPath(
            self._fs_join_path(
                run.fs_source_relative_path or ".",
                f"{TOOL_INBOX_RELATIVE_PATH}/{receipt.receipt_id}",
            )
        )
        directories: set[PurePosixPath] = set()
        mutations: list[dict[str, Any]] = []
        source_root = tool.read_only_path.resolve(strict=True)
        copied_bytes = 0
        for relative_text in tool.files:
            relative = PurePosixPath(relative_text)
            source = (source_root / Path(relative_text)).resolve(strict=True)
            if not source.is_file() or not source.is_relative_to(source_root):
                raise ValueError("shared tool source escaped immutable snapshot")
            target = destination_root / relative
            parent = target.parent
            while str(parent) not in {"", "."}:
                directories.add(parent)
                parent = parent.parent
            data = source.read_bytes()
            copied_bytes += len(data)
            if copied_bytes > tool.size_bytes:
                raise ValueError("shared tool changed during copy preparation")
            mutations.append(
                {
                    "kind": "put_file",
                    "path": target.as_posix(),
                    "dataBase64": base64.b64encode(data).decode("ascii"),
                    "mode": source.stat().st_mode & 0o777,
                }
            )
        if copied_bytes != tool.size_bytes:
            raise ValueError("shared tool copy byte count mismatch")
        return [
            {
                "kind": "make_directory",
                "path": path.as_posix(),
                "mode": 0o755,
            }
            for path in sorted(directories, key=lambda item: len(item.parts))
        ] + mutations

    def patch_pi_thinkthread_tool_copy(
        self,
        *,
        run_id: str,
        candidate_id: str,
        receipt_id: str,
        source_snapshot_id: str,
        client: AgentPosixSdkClient,
    ) -> tuple[str, str]:
        run = self._load_run(run_id)
        record = self._load_candidate_record(run_id, candidate_id)
        receipt = next(
            (item for item in record.pending_tool_copies if item.receipt_id == receipt_id),
            None,
        )
        if receipt is None:
            raise RuntimeError(f"unknown pending tool copy receipt: {receipt_id}")
        existing = next(
            (
                item
                for item in run.fs_requests
                if item.operation == "snapshot_patch"
                and item.context.get("receipt_id") == receipt_id
            ),
            None,
        )
        if existing is None:
            request_id = new_request_id()
            now = utc_timestamp()
            existing = FsRequestRecord(
                request_id=request_id,
                operation="snapshot_patch",
                context={
                    "candidate_id": candidate_id,
                    "receipt_id": receipt_id,
                    "source_snapshot_id": source_snapshot_id,
                },
                created_at=now,
                updated_at=now,
            )
            self._append_fs_request(run_id, existing)
        elif existing.context.get("source_snapshot_id") != source_snapshot_id:
            raise RuntimeError("tool copy patch source snapshot changed during recovery")
        request_id = existing.request_id
        result = existing.result if existing.state in {"succeeded", "closed"} else None
        params = {
            "snapshotId": source_snapshot_id,
            "requestId": request_id,
            "mutations": self._pi_tool_copy_mutations(
                run=run,
                record=record,
                receipt=receipt,
            ),
        }
        if result is None:
            if existing.state in {"accepted", "running", "needs_recovery"}:
                result = self._recover_fs_request_result(
                    client=client,
                    run_id=run_id,
                    request_id=request_id,
                    operation="fs.snapshot.patch",
                    deadline=time.monotonic() + 120.0,
                )
            if result is None:
                try:
                    result = client.invoke(
                        "workflow.fs.snapshot.patchBytes",
                        params,
                        timeout_seconds=120.0,
                    )
                except AgentPosixBridgeError as exc:
                    if not exc.completion_unknown and exc.code != "RequestInProgress":
                        self._update_fs_request(
                            run_id,
                            request_id,
                            state="failed",
                            error={"message": str(exc), "code": exc.code},
                        )
                        raise
                    result = self._recover_fs_request_result(
                        client=client,
                        run_id=run_id,
                        request_id=request_id,
                        operation="fs.snapshot.patch",
                        deadline=time.monotonic() + 120.0,
                    )
                    if result is None:
                        raise RuntimeError(
                            "ThinkThread tool copy patch outcome is not recoverable"
                        ) from exc
                else:
                    self._update_fs_request(
                        run_id,
                        request_id,
                        state="succeeded",
                        result=result,
                    )
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        target_snapshot_id = (
            snapshot.get("snapshotId") if isinstance(snapshot, dict) else None
        )
        if not isinstance(target_snapshot_id, str):
            raise RuntimeError("fs.snapshot.patch omitted target snapshot")
        return request_id, target_snapshot_id

    def complete_pi_thinkthread_tool_copy(
        self,
        *,
        run_id: str,
        candidate_id: str,
        receipt_id: str,
        target_snapshot_id: str,
        request_id: str,
        client: AgentPosixSdkClient,
    ) -> None:
        target = FsSnapshotArtifactRef(snapshot_id=target_snapshot_id)
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            record = self._load_candidate_record(run_id, candidate_id)
            receipt = next(
                (
                    item
                    for item in record.pending_tool_copies
                    if item.receipt_id == receipt_id
                ),
                None,
            )
            if receipt is None:
                raise RuntimeError("tool copy receipt disappeared before settlement")
            receipt.target_snapshot_id = target_snapshot_id
            record.settled_artifact_ref = target
            run.fs_cleanup.append(
                {
                    "kind": "tool_copy",
                    "state": "applied",
                    "candidate_id": candidate_id,
                    "receipt_id": receipt_id,
                    "request_id": request_id,
                    "target_snapshot_id": target_snapshot_id,
                    "applied_at": utc_timestamp(),
                }
            )
            self._write_candidate_record(run_id, record)
            self._write_run(run)
        self._close_fs_requests_after_evidence(run_id, [request_id], client)

    def _run_pi_thinkthread_verifier(
        self,
        *,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        scope: Literal["process", "promotion"],
        session: AgentSessionRecord | None,
        hypothesis: str | None,
        toolization_decision: ToolizationDecision | None,
        idempotency_key: str | None,
    ) -> ScoreReport:
        client = self._agent_posix_client()
        client.preflight()
        reader = FsSnapshotArtifactReader(client)
        if scope == "process":
            if session is None:
                raise PermissionError(
                    "pi-thinkthread process verifier requires a bound Child session"
                )
            if any(
                receipt.target_snapshot_id is None
                for receipt in record.pending_tool_copies
            ):
                raise RuntimeError(
                    "shared tool copy requires the current Child turn to end before "
                    "the next verifier attempt"
                )
            attempt = self._capture_pi_thinkthread_attempt(
                client=client,
                run=run,
                frozen=frozen,
                record=record,
                session=session,
                idempotency_key=idempotency_key,
            )
        else:
            selected = self._fs_snapshot_ref(
                run.selected_artifact_ref,
                field="selected artifact",
            )
            baseline = self._fs_snapshot_ref(
                run.baseline_artifact_ref,
                field="run baseline",
            )
            source_prefix = run.fs_source_relative_path or "."
            root_paths = reader.changed_files(baseline, selected)
            changed_files = [
                self._fs_source_projected_path(source_prefix, path)
                for path in root_paths
            ]
            touched_denied = any(
                path_matches(path, frozen.spec.edit_surface.deny)
                for path in changed_files
            )
            outside_allowed = any(
                not path_matches(path, frozen.spec.edit_surface.allow)
                for path in changed_files
            )
            attempt = _FsAttemptState(
                base_ref=baseline,
                attempt_ref=selected,
                changed_files=changed_files,
                actual_diff=reader.diff(
                    baseline,
                    selected,
                    max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
                ),
                cumulative_diff=reader.diff(
                    baseline,
                    selected,
                    max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
                ),
                touched_denied_files=touched_denied,
                changed_outside_allowed=outside_allowed,
                artifact_hash=reader.canonical_digest(baseline, selected),
                continuation_required=False,
            )

        record.detected_changed_files = list(attempt.changed_files)
        record.touched_denied_files = attempt.touched_denied_files
        record.changed_outside_allowed = attempt.changed_outside_allowed
        results: list[VerifierResult] = []
        request_ids: list[str] = (
            [attempt.snapshot_request_id]
            if attempt.snapshot_request_id is not None
            else []
        )
        if attempt.touched_denied_files or attempt.changed_outside_allowed:
            results.append(
                VerifierResult(
                    name="edit_surface_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={
                        "detected_changed_files": attempt.changed_files,
                        "touched_denied_files": attempt.touched_denied_files,
                        "changed_outside_allowed": attempt.changed_outside_allowed,
                    },
                    failure_class="EditSurfaceViolation",
                )
            )
        else:
            frozen_hash_failures = self._fs_frozen_hash_failures(
                reader=reader,
                artifact=attempt.attempt_ref,
                source_prefix=run.fs_source_relative_path or ".",
                frozen=frozen,
            )
            if frozen_hash_failures:
                results.append(
                    VerifierResult(
                        name="frozen_hash_check",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={"hash_failures": frozen_hash_failures},
                        failure_class="FrozenVerifierModified",
                    )
                )
        if not results:
            commands = (
                frozen.spec.process_verifiers
                if scope == "process"
                else frozen.spec.promotion_verifiers or frozen.spec.process_verifiers
            )
            phase: Literal["candidate", "promotion"] = (
                "candidate" if scope == "process" else "promotion"
            )
            for command in commands:
                result, request_id = self._fs_run_verifier_command(
                    client=client,
                    reader=reader,
                    run=run,
                    frozen=frozen,
                    record=record,
                    artifact=attempt.attempt_ref,
                    command=command,
                    verifier_phase=phase,
                    idempotency_key=idempotency_key,
                )
                results.append(result)
                if request_id is not None:
                    request_ids.append(request_id)
                if result.failure_class == "VerifierInfrastructureFailure":
                    break
        report = self._fs_score_report(
            run=run,
            record=record,
            frozen=frozen,
            results=results,
            scope=scope,
            touched_denied_files=attempt.touched_denied_files,
            changed_outside_allowed=attempt.changed_outside_allowed,
        )
        if scope == "promotion":
            report = report.model_copy(
                update={
                    "best_artifact_ref": attempt.attempt_ref,
                    "workspace_artifact_after_settlement": attempt.attempt_ref,
                }
            )
            with self._run_transaction(run.run_id):
                current_run = self._load_run(run.run_id)
                current_record = self._load_candidate_record(
                    run.run_id,
                    record.candidate_id,
                )
                current_record.promotion_report = report
                current_record.promotion_evidence = PromotionEvidence(
                    candidate_id=record.candidate_id,
                    selected_artifact_ref=attempt.attempt_ref,
                    artifact_ref=attempt.attempt_ref,
                    artifact_hash=attempt.artifact_hash,
                    passed=bool(report.promotion_passed),
                    created_at=utc_timestamp(),
                )
                self._write_candidate_record(run.run_id, current_record)
                self._write_run(current_run)
            self._close_fs_requests_after_evidence(run.run_id, request_ids, client)
            return report

        shared_settlement: SharedDirSettlement | None = None
        shared_settlement_error: str | None = None
        if (
            frozen.spec.shared_dir.enabled
            and report.process_passed
            and session is not None
            and record.pending_fs_tool_stages
        ):
            try:
                shared_settlement = self._settle_pi_thinkthread_shared_tools(
                    client=client,
                    run=run,
                    frozen=frozen,
                    record=record,
                    attempt_ref=attempt.attempt_ref,
                    iteration=len(record.iterations) + 1,
                    settlement_id=idempotency_key,
                )
            except Exception as exc:
                shared_settlement_error = (
                    f"shared tool snapshot failed: {type(exc).__name__}: {exc}"
                )
        staged_entries = [
            str(item.get("staged_name"))
            for item in record.pending_fs_tool_stages
            if item.get("staged_name")
        ]
        toolization_advisories = []
        if frozen.spec.shared_dir.enabled and session is not None:
            if toolization_decision is None:
                toolization_advisories.append("toolization_review_missing")
            elif toolization_decision.outcome == "staged" and not staged_entries:
                toolization_advisories.append("toolization_stage_missing")
            elif toolization_decision.outcome == "not_applicable" and staged_entries:
                toolization_advisories.append("toolization_decision_mismatch")

        prior_best = self._best_iteration_record(
            record,
            frozen.spec.metric_direction,
        )
        iteration_number = len(record.iterations) + 1
        iteration_hypothesis = self._iteration_hypothesis(
            hypothesis,
            record,
            iteration_number,
            scope="process",
            agent_session_id=session.agent_session_id if session else None,
        )
        created_at = utc_timestamp()
        failure_class = next(
            (
                result.failure_class
                for result in report.verifier_results
                if result.failure_class
            ),
            None,
        )
        iteration = IterationRecord(
            iteration=iteration_number,
            rpc_request_id=idempotency_key,
            agent_session_id=session.agent_session_id if session else None,
            selected_model=(
                session.selected_model.model
                if session and session.selected_model
                else None
            ),
            exact_model_ref=(
                session.model_provenance.get("exact_model_ref") if session else None
            ),
            adapter_version=(
                session.model_provenance.get("adapter_version") if session else None
            ),
            model_provenance=session.model_provenance if session else {},
            score=report.aggregate_score,
            process_passed=report.process_passed,
            attempt_base_ref=attempt.base_ref,
            attempt_ref=attempt.attempt_ref,
            verifier_request_ids=request_ids,
            actual_diff=attempt.actual_diff,
            cumulative_diff=attempt.cumulative_diff,
            attempt_changed_files=attempt.changed_files,
            failure_class=failure_class,
            summary=iteration_hypothesis,
            hypothesis=iteration_hypothesis,
            changed_files=attempt.changed_files,
            touched_denied_files=attempt.touched_denied_files,
            changed_outside_allowed=attempt.changed_outside_allowed,
            artifact_hash=attempt.artifact_hash,
            metrics={
                **{
                    result.name: result.metrics
                    for result in report.verifier_results
                },
                "thinkthread_continuation_required": attempt.continuation_required,
            },
            log_paths=[
                str(result.log_path)
                for result in report.verifier_results
                if result.log_path is not None
            ],
            shared_tools=(shared_settlement.tools if shared_settlement else []),
            shared_tool_errors=(
                shared_settlement.errors
                if shared_settlement
                else [shared_settlement_error]
                if shared_settlement_error
                else []
            ),
            shared_tool_staged_entries=staged_entries,
            shared_tool_staged_file_count=(
                shared_settlement.staged_file_count if shared_settlement else 0
            ),
            shared_tool_staged_bytes=(
                shared_settlement.staged_bytes if shared_settlement else 0
            ),
            shared_tool_consumed_entries=(
                shared_settlement.consumed_entries if shared_settlement else []
            ),
            shared_tool_deduplicated_entries=(
                shared_settlement.deduplicated_entries
                if shared_settlement
                else []
            ),
            shared_tool_publish_status=(
                "partially_published"
                if shared_settlement
                and shared_settlement.tools
                and shared_settlement.errors
                else "published"
                if shared_settlement and shared_settlement.tools
                else "consumed_unchanged"
                if shared_settlement and shared_settlement.consumed_entries
                else "snapshot_rejected"
                if shared_settlement and shared_settlement.errors
                else "snapshot_error"
                if shared_settlement_error
                else "skipped_failed_verifier"
                if staged_entries and not report.process_passed
                else "not_staged"
            ),
            adopted_tools=[
                ToolAdoptionRecord(
                    tool_id=receipt.tool_id,
                    snapshot_hash=receipt.snapshot_hash,
                    receipt_id=receipt.receipt_id,
                )
                for receipt in record.pending_tool_copies
                if receipt.target_snapshot_id is not None
            ],
            adoption_confounded=(
                None
                if not record.pending_tool_copies
                else len(record.pending_tool_copies) != 1
                or len(attempt.changed_files) > 2
            ),
            toolization_decision=toolization_decision,
            toolization_advisories=toolization_advisories,
            created_at=created_at,
        )
        disposition = self._fs_iteration_disposition(
            iteration,
            prior_best,
            frozen.spec.metric_direction,
        )
        iteration.disposition = disposition
        best_iteration = iteration if disposition in {"keep", "retain"} else prior_best
        settled_ref = (
            attempt.attempt_ref
            if disposition in {"keep", "retain"}
            else self._fs_snapshot_ref(
                (
                    prior_best.attempt_ref
                    if prior_best is not None
                    else attempt.base_ref
                ),
                field="restore target",
            )
        )
        iteration.settled_ref = settled_ref
        report = report.model_copy(
            update={
                "disposition": disposition,
                "best_iteration": (
                    best_iteration.iteration if best_iteration is not None else None
                ),
                "best_artifact_ref": (
                    best_iteration.attempt_ref
                    if best_iteration is not None
                    else None
                ),
                "workspace_artifact_after_settlement": settled_ref,
                "shared_tool_staged_entries": iteration.shared_tool_staged_entries,
                "shared_tool_staged_file_count": (
                    iteration.shared_tool_staged_file_count
                ),
                "shared_tool_staged_bytes": iteration.shared_tool_staged_bytes,
                "shared_tool_publish_status": iteration.shared_tool_publish_status,
                "shared_tool_errors": iteration.shared_tool_errors,
                "shared_tool_consumed_entries": (
                    iteration.shared_tool_consumed_entries
                ),
                "shared_tool_deduplicated_entries": (
                    iteration.shared_tool_deduplicated_entries
                ),
                "toolization_decision": iteration.toolization_decision,
                "toolization_advisories": iteration.toolization_advisories,
            }
        )
        with self._run_transaction(run.run_id):
            current_run = self._load_run(run.run_id)
            if (
                idempotency_key is not None
                and current_run.state == RunState.NEEDS_RECOVERY
            ):
                replay_requests = [
                    item
                    for item in current_run.fs_requests
                    if item.request_id in request_ids
                ]
                reason = str(
                    current_run.budget_used.get("needs_recovery_reason") or ""
                )
                if (
                    replay_requests
                    and all(
                        item.state
                        in {"succeeded", "failed", "cancelled", "closed"}
                        for item in replay_requests
                    )
                    and any(item.request_id in reason for item in replay_requests)
                ):
                    previous = current_run.budget_used.pop(
                        "fs_recovery_previous_state", RunState.RUNNING.value
                    )
                    current_run.budget_used.pop("needs_recovery_reason", None)
                    current_run.state = RunState(str(previous))
            self._assert_worker_iteration_allowed(
                current_run,
                "record verifier result",
            )
            current_record = self._load_candidate_record(
                run.run_id,
                record.candidate_id,
            )
            current_record.detected_changed_files = list(attempt.changed_files)
            current_record.touched_denied_files = attempt.touched_denied_files
            current_record.changed_outside_allowed = attempt.changed_outside_allowed
            current_record.iterations.append(iteration)
            consumed_receipts = {
                item.receipt_id
                for item in record.pending_tool_copies
                if item.target_snapshot_id is not None
            }
            if consumed_receipts:
                current_record.pending_tool_copies = [
                    item
                    for item in current_record.pending_tool_copies
                    if item.receipt_id not in consumed_receipts
                ]
            if shared_settlement is not None:
                consumed_stage_names = {
                    *shared_settlement.consumed_entries,
                    *shared_settlement.deduplicated_entries,
                }
                current_record.pending_fs_tool_stages = [
                    item
                    for item in current_record.pending_fs_tool_stages
                    if item.get("staged_name") not in consumed_stage_names
                ]
            current_record.results_ledger.append(
                ResultLedgerEntry(
                    source_run_id=run.run_id,
                    source_candidate_id=record.candidate_id,
                    iteration=iteration_number,
                    artifact_ref=attempt.attempt_ref,
                    metric_name=frozen.spec.metric_name,
                    score=report.aggregate_score,
                    status="pass" if report.process_passed else "fail",
                    hypothesis=iteration_hypothesis,
                    failure_class=failure_class,
                    created_at=created_at,
                )
            )
            current_record.status = "evaluated"
            current_record.score_report = report
            if disposition in {"keep", "retain"}:
                current_record.settled_artifact_ref = settled_ref
            else:
                current_run.fs_cleanup.append(
                    {
                        "kind": "branch_restore",
                        "state": "restore_required",
                        "candidate_id": record.candidate_id,
                        "branch_id": current_record.task.fs_branch_id,
                        "attempt_snapshot_id": attempt.attempt_ref.snapshot_id,
                        "target_snapshot_id": settled_ref.snapshot_id,
                        "created_at": utc_timestamp(),
                    }
                )
            self._write_candidate_record(run.run_id, current_record)
            if self._update_best_seen(current_run, frozen.spec, report):
                if best_iteration is None:
                    raise RuntimeError("run best has no FsSnapshot iteration")
                self._write_best_fs_artifact(
                    current_run,
                    frozen.spec,
                    current_record,
                    best_iteration,
                )
            current_run.candidates_evaluated = len(
                [
                    item
                    for item in self._load_candidate_records(run.run_id)
                    if item.status == "evaluated"
                ]
            )
            self._write_run(current_run)
            try:
                self._create_evidence_annotation_task(
                    run.run_id,
                    frozen,
                    record.candidate_id,
                    iteration,
                )
            except Exception:
                pass
        if session is not None:
            latest_session = self._load_agent_session_by_id(
                session.agent_session_id,
                run_id=run.run_id,
            )
            counters = dict(latest_session.counters)
            counters["verifier_runs"] = counters.get("verifier_runs", 0) + 1
            latest_session.counters = counters
            latest_session.updated_at = utc_timestamp()
            self._write_agent_session(latest_session)
        self._close_fs_requests_after_evidence(run.run_id, request_ids, client)
        return report

    def _settle_process_verifier(
        self,
        *,
        run_id: str,
        candidate_id: str,
        frozen: FrozenSpec,
        report: ScoreReport,
        attempt: _CandidateArtifactState,
        pre_attempt_settled_head: str | None,
        attempt_changed_files: list[str],
        hypothesis: str | None,
        agent_session_id: str | None,
        session: AgentSessionRecord | None,
        toolization_decision: ToolizationDecision | None,
    ) -> ScoreReport:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            self._assert_worker_iteration_allowed(run, "record verifier result")
            record = self._load_candidate_record(run_id, candidate_id)
            if agent_session_id is None and record.pending_tool_copies:
                raise RuntimeError(
                    "parent process verifier cannot settle a candidate with pending tool copies"
                )
            prior_best = self._best_git_iteration_record(
                record,
                frozen.spec.metric_direction,
            )
            iteration_number = len(record.iterations) + 1
            failure_class = next(
                (
                    result.failure_class
                    for result in report.verifier_results
                    if result.failure_class
                ),
                None,
            )
            iteration_hypothesis = self._iteration_hypothesis(
                hypothesis,
                record,
                iteration_number,
                scope="process",
                agent_session_id=agent_session_id,
            )
            created_at = utc_timestamp()
            pending_tool_copies = (
                list(record.pending_tool_copies) if agent_session_id is not None else []
            )
            for receipt in pending_tool_copies:
                if receipt.candidate_base_git_head != pre_attempt_settled_head:
                    raise RuntimeError(
                        "pending tool copy does not match settled candidate base"
                    )
            adopted_file_budget = sum(
                min(
                    len(
                        self._resolve_shared_tool(
                            run_id, item.tool_id, item.snapshot_hash
                        ).files
                    ),
                    4,
                )
                for item in pending_tool_copies
            )
            adoption_confounded = bool(
                pending_tool_copies
                and (
                    len(pending_tool_copies) != 1
                    or not attempt_changed_files
                    or len(attempt_changed_files) > adopted_file_budget + 2
                )
            )
            shared_settlement = None
            shared_settlement_error = None
            shared_inventory = None
            if frozen.spec.shared_dir.enabled and record.task.share_out_dir is not None:
                try:
                    shared_inventory = SharedDirManager(self._run_dir(run_id)).inspect_staging(
                        record.task.share_out_dir,
                        max_tools=frozen.spec.shared_dir.max_tools_per_iteration,
                        deep=False,
                    )
                except Exception as exc:
                    shared_inventory = {
                        "entries": [],
                        "errors": [f"staging inspection failed: {exc}"],
                    }
            if (
                frozen.spec.shared_dir.enabled
                and report.process_passed
                and agent_session_id is not None
                and record.task.share_out_dir is not None
            ):
                limits = frozen.spec.shared_dir
                try:
                    shared_settlement = SharedDirManager(self._run_dir(run_id)).settle_iteration(
                        candidate_id=candidate_id,
                        iteration=iteration_number,
                        source_commit=attempt.git_head,
                        source_artifact_ref=GitCommitArtifactRef(
                            commit=attempt.git_head
                        ),
                        share_out_dir=record.task.share_out_dir,
                        max_tools=limits.max_tools_per_iteration,
                        max_files=limits.max_files_per_iteration,
                        max_bytes=limits.max_bytes_per_iteration,
                        max_path_entries=limits.max_path_entries_per_iteration,
                        max_depth=limits.max_depth,
                    )
                except Exception as exc:
                    shared_settlement_error = f"shared tool snapshot failed: {exc}"
                    shared_settlement = None
            shared_inventory_observed = bool(
                (shared_inventory or {}).get("entries")
                or (shared_inventory or {}).get("errors")
            )
            staged_entries = (
                shared_settlement.staged_entries
                if shared_settlement
                else list((shared_inventory or {}).get("entries", []))
            )
            toolization_advisories = []
            if frozen.spec.shared_dir.enabled and agent_session_id is not None:
                if toolization_decision is None:
                    toolization_advisories.append("toolization_review_missing")
                elif toolization_decision.outcome == "staged" and not staged_entries:
                    toolization_advisories.append("toolization_stage_missing")
                elif (
                    toolization_decision.outcome == "not_applicable"
                    and staged_entries
                ):
                    toolization_advisories.append("toolization_decision_mismatch")
            iteration = IterationRecord(
                iteration=iteration_number,
                agent_session_id=agent_session_id,
                selected_model=(
                    session.selected_model.model
                    if session and session.selected_model
                    else None
                ),
                exact_model_ref=(
                    session.model_provenance.get("exact_model_ref") if session else None
                ),
                adapter_version=(
                    session.model_provenance.get("adapter_version") if session else None
                ),
                model_provenance=(session.model_provenance if session else {}),
                score=report.aggregate_score,
                process_passed=report.process_passed,
                attempt_base_ref=GitCommitArtifactRef(
                    commit=pre_attempt_settled_head
                ),
                attempt_ref=GitCommitArtifactRef(commit=attempt.git_head),
                git_head=attempt.git_head,
                attempt_base_git_head=pre_attempt_settled_head,
                attempt_changed_files=attempt_changed_files,
                git_artifact_clean=attempt.git_artifact_clean,
                git_status=attempt.git_status,
                failure_class=failure_class,
                summary=iteration_hypothesis,
                hypothesis=iteration_hypothesis,
                changed_files=list(attempt.changed_files),
                touched_denied_files=attempt.touched_denied_files,
                changed_outside_allowed=attempt.changed_outside_allowed,
                artifact_hash=attempt.artifact_hash,
                metrics={
                    result.name: result.metrics for result in report.verifier_results
                },
                log_paths=[
                    str(result.log_path)
                    for result in report.verifier_results
                    if result.log_path is not None
                ],
                shared_tools=(shared_settlement.tools if shared_settlement else []),
                shared_tool_errors=(
                    shared_settlement.errors if shared_settlement
                    else [shared_settlement_error] if shared_settlement_error
                    else list((shared_inventory or {}).get("errors", []))
                ),
                shared_tool_staged_entries=staged_entries,
                shared_tool_staged_file_count=(
                    shared_settlement.staged_file_count if shared_settlement else 0
                ),
                shared_tool_staged_bytes=(
                    shared_settlement.staged_bytes if shared_settlement else 0
                ),
                shared_tool_consumed_entries=(
                    shared_settlement.consumed_entries if shared_settlement else []
                ),
                shared_tool_deduplicated_entries=(
                    shared_settlement.deduplicated_entries if shared_settlement else []
                ),
                shared_tool_publish_status=(
                    "partially_published"
                    if shared_settlement
                    and shared_settlement.tools
                    and shared_settlement.errors
                    else "published" if shared_settlement and shared_settlement.tools
                    else "consumed_unchanged" if shared_settlement and shared_settlement.consumed_entries
                    else "snapshot_rejected" if shared_settlement and shared_settlement.errors
                    else "snapshot_error" if shared_settlement_error
                    else "skipped_unattributed_verifier"
                    if shared_inventory_observed and agent_session_id is None
                    else "skipped_failed_verifier"
                    if shared_inventory_observed and not report.process_passed
                    else "not_staged"
                ),
                adopted_tools=[
                    ToolAdoptionRecord(
                        tool_id=item.tool_id,
                        snapshot_hash=item.snapshot_hash,
                        receipt_id=item.receipt_id,
                    )
                    for item in pending_tool_copies
                ],
                adoption_confounded=(
                    None if not pending_tool_copies
                    else adoption_confounded
                ),
                toolization_decision=toolization_decision,
                toolization_advisories=toolization_advisories,
                created_at=created_at,
            )
            disposition = self._iteration_disposition(
                iteration,
                prior_best,
                frozen.spec.metric_direction,
            )
            iteration.disposition = disposition

            if disposition in {"keep", "retain"}:
                best_iteration = iteration
                settled_ref = iteration.attempt_ref
            else:
                target = (
                    prior_best.git_head
                    if prior_best is not None
                    else pre_attempt_settled_head
                )
                if target is None:
                    raise RuntimeError("candidate rollback has no restoration target")
                iteration.restored_to_iteration = (
                    prior_best.iteration if prior_best is not None else None
                )
                iteration.restored_to_git_head = target
                self._restore_candidate_artifact(
                    record,
                    frozen.spec.metric_name,
                    target,
                    (
                        f"goal-plus restore {candidate_id} best after "
                        f"iteration {iteration_number}"
                    ),
                )
                best_iteration = prior_best
                settled_ref = GitCommitArtifactRef(commit=target)

            iteration.settled_ref = settled_ref

            settled = self._candidate_artifact_state(run, frozen, record)
            if not settled.git_artifact_clean:
                raise RuntimeError("candidate artifact is dirty after settlement")
            if (
                best_iteration is not None
                and settled.artifact_hash != best_iteration.artifact_hash
            ):
                raise RuntimeError("candidate artifact does not match its best iteration")
            self._apply_candidate_artifact_state(record, settled)

            ledger_entry = ResultLedgerEntry(
                source_run_id=run_id,
                source_candidate_id=candidate_id,
                iteration=iteration_number,
                artifact_ref=iteration.attempt_ref,
                git_head=attempt.git_head,
                metric_name=frozen.spec.metric_name,
                score=report.aggregate_score,
                status="pass" if report.process_passed else "fail",
                hypothesis=iteration_hypothesis,
                failure_class=failure_class,
                created_at=created_at,
            )
            ledger_git_head = self._append_results_tsv(
                record,
                ledger_entry,
                frozen.spec.metric_name,
            )
            iteration.ledger_git_head = ledger_git_head
            iteration.workspace_git_head_after_settlement = ledger_git_head
            record.settled_artifact_ref = settled_ref
            record.iterations.append(iteration)
            if pending_tool_copies:
                consumed = {item.receipt_id for item in pending_tool_copies}
                record.pending_tool_copies = [
                    item for item in record.pending_tool_copies
                    if item.receipt_id not in consumed
                ]
                for item in pending_tool_copies:
                    shutil.rmtree(item.inbox_path, ignore_errors=True)
            report = report.model_copy(
                update={
                    "disposition": disposition,
                    "best_iteration": (
                        best_iteration.iteration if best_iteration is not None else None
                    ),
                    "best_git_head": (
                        best_iteration.git_head if best_iteration is not None else None
                    ),
                    "best_artifact_ref": (
                        best_iteration.attempt_ref
                        if best_iteration is not None
                        else None
                    ),
                    "workspace_artifact_after_settlement": settled_ref,
                    "workspace_git_head_after_settlement": ledger_git_head,
                    "shared_tool_staged_entries": iteration.shared_tool_staged_entries,
                    "shared_tool_staged_file_count": iteration.shared_tool_staged_file_count,
                    "shared_tool_staged_bytes": iteration.shared_tool_staged_bytes,
                    "shared_tool_publish_status": iteration.shared_tool_publish_status,
                    "shared_tool_errors": iteration.shared_tool_errors,
                    "shared_tool_consumed_entries": iteration.shared_tool_consumed_entries,
                    "shared_tool_deduplicated_entries": iteration.shared_tool_deduplicated_entries,
                    "toolization_decision": iteration.toolization_decision,
                    "toolization_advisories": iteration.toolization_advisories,
                }
            )
            record.status = "evaluated"
            record.score_report = report
            if record.promotion_evidence and (
                record.promotion_evidence.git_head != report.best_git_head
                or record.promotion_evidence.artifact_hash != settled.artifact_hash
            ):
                record.promotion_report = None
                record.promotion_evidence = None
            self._write_candidate_record(run_id, record)
            if agent_session_id is not None:
                try:
                    self._create_evidence_annotation_task(
                        run_id,
                        frozen,
                        candidate_id,
                        iteration,
                    )
                except Exception:
                    # Explanatory Views never invalidate settled verifier Evidence.
                    pass

            if self._update_best_seen(run, frozen.spec, report):
                if best_iteration is None:
                    raise RuntimeError("run best has no verifier-backed iteration")
                self._write_best_artifact(run, frozen.spec, record, best_iteration)
            run.candidates_evaluated = len(
                [
                    item
                    for item in self._load_candidate_records(run_id)
                    if item.status == "evaluated"
                ]
            )
            self._write_run(run)

            if session is not None and agent_session_id is not None:
                latest_session = self._load_agent_session_by_id(
                    agent_session_id,
                    run_id=run_id,
                )
                counters = dict(latest_session.counters)
                counters["verifier_runs"] = counters.get("verifier_runs", 0) + 1
                self._write_agent_session(
                    latest_session.model_copy(
                        update={
                            "updated_at": utc_timestamp(),
                            "counters": counters,
                        }
                    )
                )
            return report

    def select(self, run_id: str) -> dict[str, Any]:
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            self._assert_run_not_invalidated(run, "select")
            if run.state not in {
                RunState.RUNNING,
                RunState.WAITING_FOR_WORKERS,
                RunState.SELECTING,
                RunState.SELECTION_BLOCKED,
                RunState.READY_TO_PROMOTE,
            }:
                raise RuntimeError(f"cannot select candidate from state {run.state}")
            run.state = RunState.SELECTING
            run.budget_used.pop("selection_blocked_reason", None)
            self._write_run(run)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        records = self._load_candidate_records(run_id)
        options = self._selection_options(run, records, frozen.spec.metric_direction)
        if not options:
            self._mark_selection_blocked(
                run_id,
                "no verifier-backed candidate iteration is eligible for selection",
            )
            raise RuntimeError("no verified candidates available for selection")

        maximize = frozen.spec.metric_direction == "maximize"
        ranked = sorted(
            options,
            key=lambda item: (
                item[0] if maximize else -item[0],
                item[1].candidate_id == run.best_candidate_id,
            ),
            reverse=True,
        )
        selected_score: float | None = None
        selected_record: CandidateRecord | None = None
        selected_iteration: int | None = None
        selected_git_head: str | None = None
        final_report: ScoreReport | None = None
        selection_evidence_source = "parent_verifier"
        for option_score, option_record, option_iteration, option_git_head in ranked:
            option_record = self._load_candidate_record(
                run_id,
                option_record.candidate_id,
            )
            if option_git_head:
                self._restore_candidate_artifact(
                    option_record,
                    frozen.spec.metric_name,
                    option_git_head,
                    (
                        f"goal-plus select {option_record.candidate_id} "
                        f"iteration {option_iteration}"
                    ),
                )
            worker_iteration = next(
                (
                    iteration
                    for iteration in option_record.iterations
                    if iteration.iteration == option_iteration
                    and iteration.git_head == option_git_head
                    and iteration.agent_session_id is not None
                ),
                None,
            )
            current = self._candidate_artifact_state(run, frozen, option_record)
            if (
                worker_iteration is not None
                and self._git_iteration_eligible(worker_iteration)
                and worker_iteration.artifact_hash == current.artifact_hash
                and current.git_artifact_clean
            ):
                selected_score = option_score
                selected_record = option_record
                selected_iteration = option_iteration
                selected_git_head = option_git_head
                selection_evidence_source = "worker_evidence"
                break
            report = self.run_verifier(run_id, option_record.candidate_id)
            if report.process_passed and report.aggregate_score is not None:
                selected_score = report.aggregate_score
                selected_record = option_record
                selected_iteration = option_iteration
                selected_git_head = option_git_head
                final_report = report
                break

        if selected_record is None or selected_score is None:
            self._mark_selection_blocked(
                run_id,
                "all eligible candidate revisions failed verification",
            )
            raise RuntimeError("no selected candidate has passing verification")

        selected_changed_files = self._detect_changed_files(
            Path(run.source_path), selected_record.task.workspace
        )
        selected_artifact_hash = self._artifact_hash(
            selected_record.task.workspace,
            selected_changed_files,
        )
        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            self._assert_run_not_invalidated(run, "record selection")
            run.state = RunState.READY_TO_PROMOTE
            run.selected_candidate_id = selected_record.candidate_id
            run.best_candidate_id = selected_record.candidate_id
            run.best_score = selected_score
            run.selected_score = selected_score
            run.selected_iteration = selected_iteration
            run.selected_git_head = selected_git_head
            run.selected_artifact_hash = selected_artifact_hash
            run.budget_used.pop("selection_blocked_reason", None)
            selected_record = self._load_candidate_record(
                run_id, selected_record.candidate_id
            )
            selected_best = next(
                (
                    iteration
                    for iteration in selected_record.iterations
                    if iteration.iteration == selected_iteration
                    and iteration.git_head == selected_git_head
                    and self._git_iteration_eligible(iteration)
                ),
                None,
            )
            if selected_best is None:
                raise RuntimeError("selected candidate has no exact best iteration")
            self._write_best_artifact(run, frozen.spec, selected_record, selected_best)
            selected_record.promotion_report = None
            selected_record.promotion_evidence = None
            self._write_candidate_record(run_id, selected_record)
            self._write_run(run)
        return {
            "selected_candidate_id": selected_record.candidate_id,
            "selected_score": selected_score,
            "selected_iteration": selected_iteration,
            "selected_git_head": selected_git_head,
            "selected_artifact_hash": selected_artifact_hash,
            "selection_basis_score": (
                next(
                    (
                        score
                        for score, record, iteration, git_head in ranked
                        if record.candidate_id == selected_record.candidate_id
                        and iteration == selected_iteration
                        and git_head == selected_git_head
                    ),
                    selected_score,
                )
            ),
            "final_verifier_score": (
                final_report.aggregate_score if final_report else selected_score
            ),
            "selection_evidence_source": selection_evidence_source,
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
        }

    def _mark_selection_blocked(self, run_id: str, reason: str) -> None:
        run = self._load_run(run_id)
        run.state = RunState.SELECTION_BLOCKED
        run.budget_used["selection_blocked_reason"] = reason
        self._write_run(run)

    def _blocking_goal_report_records(self, run_id: str) -> list[tuple[str, str]]:
        blocking: list[tuple[str, str]] = []
        for path in sorted((self.root_dir / "goal-plus").glob("*/goal.json")):
            try:
                payload = load_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            search_tasks = payload.get("search_tasks")
            if not isinstance(search_tasks, list) or not any(
                isinstance(task, dict) and task.get("run_id") == run_id
                for task in search_tasks
            ):
                continue
            status = str(payload.get("status") or "active")
            if status not in {"blocked", "complete", "abandoned"}:
                blocking.append(
                    (str(payload.get("goal_plus_id") or path.parent.name), status)
                )
        return blocking

    def report(self, run_id: str) -> Path:
        blocking = self._blocking_goal_report_records(run_id)
        if blocking:
            details = ", ".join(
                f"{goal_plus_id}={status}" for goal_plus_id, status in blocking
            )
            raise RuntimeError(
                "cannot generate a report before every linked Goal Plus record "
                "reaches a terminal status (complete, blocked, or abandoned); "
                f"current: {details}"
            )
        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        records = self._load_candidate_records(run_id)
        plans = self._load_plans(run_id)
        report_path = self._run_dir(run_id) / "report.md"

        lines = [
            f"# Search Report: {run_id}",
            "",
            "- HTML report: [report.html](report.html)",
            f"- Frozen spec: `{frozen.frozen_spec_id}`",
            f"- State: `{run.state}`",
            f"- Source run: `{run.source_run_id}`",
            f"- Replacement run: `{run.replacement_run_id}`",
            f"- Spec hash: `{frozen.spec_hash}`",
            f"- Objective: {frozen.spec.objective}",
            f"- Metric: `{frozen.spec.metric_name}` ({frozen.spec.metric_direction})",
            f"- Initial strategy: `{frozen.spec.strategy.name}`",
            f"- Best candidate: `{run.best_candidate_id}`",
            f"- Best score: `{run.best_score}`",
            f"- Selected score: `{run.selected_score}`",
            f"- Selected iteration: `{run.selected_iteration}`",
            f"- Selected git head: `{run.selected_git_head}`",
            f"- Invalidated at: `{run.invalidated_at}`",
            f"- Invalidation reason: `{run.invalidation_reason}`",
            f"- Invalidation summary: {run.invalidation_summary or ''}",
            (
                "- Invalidation evidence: "
                f"{self._markdown_cell(canonical_json(run.invalidation_evidence))}"
            ),
            "",
            "## Strategy Plans",
            "",
            "| Plan | Status | Strategy | Orchestration | Requested | Planned | Started Candidates | Trace |",
            "|---|---|---|---|---:|---:|---|---|",
        ]
        for plan in plans:
            trace = plan.strategy_trace.get("reason") or plan.strategy_trace.get("selection_rule") or ""
            lines.append(
                f"| `{plan.plan_id}` | {plan.status} | `{plan.strategy.name}` | "
                f"`{plan.strategy.orchestration_mode}` | "
                f"{plan.requested_k} | {plan.planned_k} | "
                f"{self._markdown_cell(', '.join(plan.started_candidate_ids))} | "
                f"{self._markdown_cell(str(trace))} |"
            )
        lines.extend(
            [
                "",
                "## Candidates",
                "",
                "| Candidate | Plan | Agent Sessions | Parent/Base | Status | Score | Git Head | Best Iteration | Best Score | Best Git Head | Process | Summary | Key Metrics | Changed Files | Results Ledger |",
                "|---|---|---|---|---|---:|---|---:|---:|---|---|---|---|---|---|",
            ]
        )
        ledger_summaries: list[tuple[CandidateRecord, Path, list[ResultLedgerEntry]]] = []
        for record in records:
            score = ""
            passed = ""
            latest_iteration = record.iterations[-1] if record.iterations else None
            git_head = latest_iteration.git_head if latest_iteration else ""
            payload = self._history_candidate_payload(record, frozen.spec)
            key_metrics = ", ".join(
                f"{key}={value}" for key, value in payload["key_metrics"].items()
            )
            changed = ", ".join(record.detected_changed_files)
            agent_sessions = ", ".join(
                session["agent_session_id"] for session in payload["agent_sessions"]
            )
            best_iteration = self._best_iteration_record(record, frozen.spec.metric_direction)
            if best_iteration is not None:
                score = "" if best_iteration.score is None else str(best_iteration.score)
                passed = "True"
            elif record.score_report and record.score_report.process_passed:
                score = (
                    ""
                    if record.score_report.aggregate_score is None
                    else str(record.score_report.aggregate_score)
                )
                passed = "True"
            best_iteration_value = (
                "" if best_iteration is None else str(best_iteration.iteration)
            )
            best_score_value = (
                ""
                if best_iteration is None or best_iteration.score is None
                else str(best_iteration.score)
            )
            best_git_head = "" if best_iteration is None else best_iteration.git_head or ""
            parent_base = ", ".join(
                part
                for part in [
                    f"parent={record.task.parent_id}" if record.task.parent_id else "",
                    f"base={record.task.base_candidate_id}" if record.task.base_candidate_id else "",
                ]
                if part
            )
            results_path = self._results_tsv_path(record.task.workspace)
            results_entries = self._read_results_tsv(record)
            ledger_summaries.append((record, results_path, results_entries))
            try:
                ledger_link = results_path.relative_to(report_path.parent).as_posix()
            except ValueError:
                ledger_link = results_path.as_posix()
            ledger_display = (
                f"[results.tsv]({ledger_link}) ({len(results_entries)} rows)"
                if results_path.is_file()
                else "missing"
            )
            lines.append(
                f"| `{record.candidate_id}` | `{record.task.plan_id or ''}` | "
                f"{self._markdown_cell(agent_sessions)} | "
                f"{self._markdown_cell(parent_base)} | {record.status} | {score} | "
                f"{self._markdown_cell(git_head or '')} | "
                f"{best_iteration_value} | {best_score_value} | "
                f"{self._markdown_cell(best_git_head)} | {passed} | "
                f"{self._markdown_cell(payload['summary'])} | "
                f"{self._markdown_cell(key_metrics)} | {self._markdown_cell(changed)} | "
                f"{self._markdown_cell(ledger_display)} |"
            )
        lines.extend(
            [
                "",
                "## Results Ledgers",
                "",
                "Each candidate workspace owns the complete inherited verifier ledger.",
                "",
                "| Candidate | Ledger | Rows | Latest Commit | Latest Score | Latest Status | Latest Hypothesis |",
                "|---|---|---:|---|---:|---|---|",
            ]
        )
        for record, results_path, results_entries in ledger_summaries:
            latest = results_entries[-1] if results_entries else None
            try:
                ledger_link = results_path.relative_to(report_path.parent).as_posix()
            except ValueError:
                ledger_link = results_path.as_posix()
            ledger_display = (
                f"[results.tsv]({ledger_link})" if results_path.is_file() else "missing"
            )
            lines.append(
                f"| `{record.candidate_id}` | {self._markdown_cell(ledger_display)} | "
                f"{len(results_entries)} | "
                f"{self._markdown_cell(latest.git_head if latest else '')} | "
                f"{'' if latest is None or latest.score is None else latest.score} | "
                f"{self._markdown_cell(latest.status if latest else '')} | "
                f"{self._markdown_cell(latest.hypothesis if latest else '')} |"
            )
        lines.extend(
            [
                "",
                "## Toolization Reviews",
                "",
                "These worker decisions and runtime advisories are observational only; staging inventory and publication settlement remain authoritative.",
                "",
                "| Candidate | Iteration | Outcome | Signals | Exclusion | Tool Names | Rationale | Advisories | Staged Entries | Publish Status |",
                "|---|---:|---|---|---|---|---|---|---|---|",
            ]
        )
        for record in records:
            for iteration in record.iterations:
                decision = iteration.toolization_decision
                lines.append(
                    f"| `{record.candidate_id}` | {iteration.iteration} | "
                    f"{self._markdown_cell(decision.outcome if decision else '')} | "
                    f"{self._markdown_cell(', '.join(decision.signals) if decision else '')} | "
                    f"{self._markdown_cell(decision.exclusion or '' if decision else '')} | "
                    f"{self._markdown_cell(', '.join(decision.tool_names) if decision else '')} | "
                    f"{self._markdown_cell(decision.rationale if decision else '')} | "
                    f"{self._markdown_cell(', '.join(iteration.toolization_advisories))} | "
                    f"{self._markdown_cell(', '.join(iteration.shared_tool_staged_entries))} | "
                    f"{self._markdown_cell(iteration.shared_tool_publish_status)} |"
                )
        agent_sessions = self._load_agent_sessions(run_id)
        if agent_sessions:
            session_rows = [
                (session, self._display_host_handle(session)) for session in agent_sessions
            ]
            include_handle = any(
                handle and handle != session.agent_session_id
                for session, handle in session_rows
            )
            lines.extend(
                [
                    "",
                    "## Agent Sessions",
                    "",
                ]
            )
            if include_handle:
                lines.extend(
                    [
                        "| Session | Host | Handle | Candidate | Verifier Runs | Created | Updated |",
                        "|---|---|---|---|---:|---|---|",
                    ]
                )
            else:
                lines.extend(
                    [
                        "| Session | Host | Candidate | Verifier Runs | Created | Updated |",
                        "|---|---|---|---:|---|---|",
                    ]
                )
            for session, handle in session_rows:
                common = (
                    f"| `{session.agent_session_id}` | "
                    f"`{session.host}` | "
                )
                if include_handle:
                    display_handle = handle if handle != session.agent_session_id else ""
                    common += f"{self._markdown_cell(display_handle)} | "
                lines.append(
                    common
                    + f"`{session.candidate_id or ''}` | "
                    f"{session.counters.get('verifier_runs', 0)} | "
                    f"{session.created_at} | {session.updated_at} |"
                )
        lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        from goal_plus.reporting import write_html_report

        write_html_report(self.root_dir, run_id, report_path.with_suffix(".html"))
        return report_path

    def promote(self, run_id: str, candidate_id: str) -> Path:
        run = self._load_run(run_id)
        self._assert_run_not_invalidated(run, "promote")
        if run.selected_candidate_id != candidate_id:
            raise RuntimeError(
                "cannot promote candidate before search_select selects it"
            )
        frozen = self._load_frozen_spec(run.frozen_spec_id)

        def reject_promotion(message: str) -> None:
            latest_run = self._load_run(run_id)
            latest_run.state = RunState.READY_TO_PROMOTE
            self._write_run(latest_run)
            raise RuntimeError(message)

        record = self._load_candidate_record(run_id, candidate_id)
        if not run.selected_git_head:
            reject_promotion(
                "cannot promote candidate without an immutable selected Git revision"
            )
        current = self._candidate_artifact_state(run, frozen, record)
        if run.selected_artifact_hash is not None and (
            current.artifact_hash != run.selected_artifact_hash
            or not current.git_artifact_clean
        ):
            reject_promotion(
                "cannot promote candidate because the selected artifact changed"
            )
        self._restore_candidate_artifact(
            record,
            frozen.spec.metric_name,
            run.selected_git_head,
            f"goal-plus promote {candidate_id} selected revision",
        )
        selected = self._candidate_artifact_state(run, frozen, record)
        self._apply_candidate_artifact_state(record, selected)
        detected_changed = selected.changed_files
        artifact_hash = selected.artifact_hash
        self._write_candidate_record(run_id, record)
        if run.selected_artifact_hash is None:
            run.selected_artifact_hash = artifact_hash
            self._write_run(run)
        elif artifact_hash != run.selected_artifact_hash:
            reject_promotion(
                "cannot promote candidate because the selected artifact changed"
            )
        selected_iteration_evidence = next(
            (
                iteration
                for iteration in record.iterations
                if iteration.iteration == run.selected_iteration
                and iteration.git_head == run.selected_git_head
                and self._git_iteration_eligible(iteration)
                and iteration.artifact_hash == selected.artifact_hash
            ),
            None,
        )
        if selected_iteration_evidence is None and (
            not record.score_report or not record.score_report.process_passed
        ):
            reject_promotion(
                "cannot promote candidate without a passing score report"
            )
        if record.touched_denied_files or record.changed_outside_allowed:
            reject_promotion(
                "cannot promote candidate that changed denied/out-of-surface files"
            )

        if frozen.spec.promotion_verifiers:
            promotion_report = self.run_verifier(
                run_id,
                candidate_id,
                scope="promotion",
            )
            run = self._load_run(run_id)
            record = self._load_candidate_record(run_id, candidate_id)
            detected_changed = self._detect_changed_files(
                Path(run.source_path), record.task.workspace
            )
            artifact_hash = self._artifact_hash(
                record.task.workspace, detected_changed
            )
            git_head = self._git_head(record.task.workspace)
            evidence = record.promotion_evidence
            evidence_is_current = bool(
                evidence
                and evidence.candidate_id == candidate_id
                and evidence.selected_git_head == run.selected_git_head
                and evidence.git_head == run.selected_git_head
                and evidence.artifact_hash == artifact_hash
                and evidence.artifact_hash == run.selected_artifact_hash
                and evidence.passed
            )
            if not promotion_report.promotion_passed or not evidence_is_current:
                reject_promotion(
                    "cannot promote candidate without fresh passing promotion evidence"
                )

        with self._run_transaction(run_id):
            run = self._load_run(run_id)
            self._assert_run_not_invalidated(run, "record promotion")
            promotion_dir = self._run_dir(run_id) / "promotion"
            promotion_dir.mkdir(parents=True, exist_ok=True)
            patch_path = promotion_dir / f"{candidate_id}.patch"
            self._write_patch(
                Path(run.source_path),
                record.task.workspace,
                run.selected_git_head,
                detected_changed,
                patch_path,
            )
            run.state = RunState.PROMOTED
            run.selected_candidate_id = candidate_id
            self._write_run(run)
        report_path = self._run_dir(run_id) / "report.md"
        if report_path.exists() and not self._blocking_goal_report_records(run_id):
            self.report(run_id)
        return patch_path

    def _strategy_mode(self, strategy: StrategySpec) -> str:
        return strategy.name.strip().lower().replace("-", "_")

    def _display_host_handle(self, session: AgentSessionRecord) -> str:
        return (
            session.host_handle.external_id
            or session.host_handle.task_name
            or session.host_handle.nickname
            or ""
        )

    def _validate_host_strategy(self, strategy: StrategySpec) -> None:
        self._validate_strategy_config(strategy)
        self._validate_worker_launch_for_host(strategy)
        self._validate_worker_budget_for_host(
            worker_host=strategy.worker_host,
            worker_budget=strategy.worker_budget,
        )
        if not portable_strategy_mode(strategy.name):
            raise ValueError(
                f"{strategy.worker_host} worker_host does not support strategy "
                f"{strategy.name}; use default/agent_guided or random"
            )

    @staticmethod
    def _apply_global_evidence_mode_from_environment(spec: SearchSpec) -> SearchSpec:
        environment_mode = os.environ.get(GLOBAL_EVIDENCE_MODE_ENV)
        if environment_mode is None:
            return spec
        environment_mode = environment_mode.strip()
        if environment_mode not in GLOBAL_EVIDENCE_MODES:
            allowed = ", ".join(sorted(GLOBAL_EVIDENCE_MODES))
            raise ValueError(
                f"{GLOBAL_EVIDENCE_MODE_ENV} must be one of {allowed}"
            )
        configured_mode = spec.strategy.config.get("global_evidence_mode")
        if configured_mode is not None and configured_mode != environment_mode:
            raise ValueError(
                "strategy.config.global_evidence_mode conflicts with "
                f"{GLOBAL_EVIDENCE_MODE_ENV}"
            )
        config = {**spec.strategy.config, "global_evidence_mode": environment_mode}
        strategy = spec.strategy.model_copy(update={"config": config})
        return spec.model_copy(update={"strategy": strategy})

    @staticmethod
    def _validate_strategy_config(strategy: StrategySpec) -> None:
        evidence_mode = strategy.config.get("global_evidence_mode", "manual")
        if evidence_mode not in GLOBAL_EVIDENCE_MODES:
            allowed = ", ".join(sorted(GLOBAL_EVIDENCE_MODES))
            raise ValueError(
                "strategy.config.global_evidence_mode must be one of " + allowed
            )
        misplaced = sorted(
            {"min_runtime_seconds", "min_verifier_runs"}.intersection(
                strategy.config
            )
        )
        if misplaced:
            raise ValueError(
                "strategy.config cannot contain worker lease fields "
                f"{', '.join(misplaced)}; place them in strategy.worker_budget"
            )

    def _validate_worker_launch_for_host(self, strategy: StrategySpec) -> None:
        self._validate_worker_launch_options_for_host(
            strategy.worker_host,
            strategy.worker_launch,
            "worker_launch",
        )
        for model in strategy.models:
            launch = WorkerLaunchOptions(
                model=model.model,
                reasoning_effort=model.reasoning_effort,
                service_tier=model.service_tier,
            )
            self._validate_worker_launch_options_for_host(
                strategy.worker_host,
                launch,
                f"model {model.model!r}",
            )

    @staticmethod
    def _validate_worker_launch_options_for_host(
        worker_host: str,
        worker_launch: WorkerLaunchOptions | None,
        label: str,
    ) -> None:
        if worker_launch is None:
            return
        adapter = get_agent_host_adapter(worker_host)
        requested = worker_launch.model_dump(mode="json", exclude_none=True)
        capability_by_field = {
            "model": adapter.capabilities.supports_model_override,
            "reasoning_effort": adapter.capabilities.supports_reasoning_effort,
            "service_tier": adapter.capabilities.supports_service_tier,
        }
        unsupported = sorted(
            field for field in requested if not capability_by_field[field]
        )
        if unsupported:
            raise ValueError(
                f"{worker_host} worker_host does not support {label} fields: "
                f"{', '.join(unsupported)}"
            )

    def _validate_worker_budget_for_host(
        self,
        *,
        worker_host: str,
        worker_budget: WorkerBudget | None,
    ) -> None:
        if worker_host == "pi-rpc" and (
            worker_budget is None or worker_budget.max_runtime_seconds is None
        ):
            raise ValueError(
                "pi-rpc worker_budget requires max_runtime_seconds so the "
                "Pi RPC runner can enforce a process deadline"
            )
        if worker_budget is None:
            return
        if worker_host == "codex" and worker_budget.max_runtime_seconds is None:
            raise ValueError(
                "codex worker_budget requires max_runtime_seconds so the "
                "parent agent can enforce a watchdog deadline"
            )

    def _worker_budget_dict(self, strategy: StrategySpec) -> dict[str, Any] | None:
        if strategy.worker_budget is None:
            return None
        return strategy.worker_budget.model_dump(mode="json")

    def _worker_launch_dict(self, strategy: StrategySpec) -> dict[str, Any] | None:
        if strategy.worker_launch is None:
            return None
        return strategy.worker_launch.model_dump(mode="json", exclude_none=True)

    def _worker_policy(self, strategy: StrategySpec) -> dict[str, Any]:
        adapter = get_agent_host_adapter(strategy.worker_host)
        worker_agent_type = strategy.worker_agent_type
        worker_budget = self._worker_budget_dict(strategy)
        worker_launch = self._worker_launch_dict(strategy)
        return {
            "host": strategy.worker_host,
            "worker_agent_type": worker_agent_type,
            "worker_budget": worker_budget,
            "worker_launch": worker_launch,
            "pool": adapter.capabilities.pool.as_dict(),
            "reason": (
                f"主 agent 使用 search_start_agent_session 返回的 launch payload，"
                f"通过 host-pool 契约启动 {strategy.worker_host} worker。"
            ),
        }

    def _normalize_worker_policy(
        self,
        strategy: StrategySpec,
        worker_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_policy = self._worker_policy(strategy)
        policy = {**base_policy, **(worker_policy or {})}
        selected = (
            policy.get("worker_agent_type")
            or strategy.worker_agent_type
            or self._default_worker_agent_type(strategy.worker_host)
        )
        policy["worker_agent_type"] = selected
        return policy

    def _default_worker_agent_type(self, host: str) -> str:
        if host == "codex":
            return "search_candidate_agent"
        return "search-candidate-worker"

    def _candidate_worker_agent_type(
        self,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
    ) -> str:
        worker_policy = candidate_record.task.strategy_metadata.get("worker_policy", {})
        selected = (
            worker_policy.get("worker_agent_type")
            or frozen.spec.strategy.worker_agent_type
            or self._default_worker_agent_type(frozen.spec.strategy.worker_host)
        )
        return str(selected)

    def _candidate_worker_budget(
        self,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
    ) -> dict[str, Any] | None:
        worker_policy = candidate_record.task.strategy_metadata.get("worker_policy", {})
        budget = worker_policy.get("worker_budget")
        if budget is not None:
            return dict(budget)
        return self._worker_budget_dict(frozen.spec.strategy)

    def _candidate_worker_launch(
        self,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
    ) -> dict[str, Any] | None:
        worker_policy = candidate_record.task.strategy_metadata.get("worker_policy", {})
        launch = worker_policy.get("worker_launch")
        base = dict(launch) if launch is not None else (
            self._worker_launch_dict(frozen.spec.strategy) or {}
        )
        selected_launch = candidate_record.task.model_provenance.get("worker_launch")
        if isinstance(selected_launch, dict):
            base.update(
                {
                    key: value
                    for key, value in selected_launch.items()
                    if value is not None
                }
            )
        return base or None

    def _normalize_worker_budget_override(
        self,
        *,
        worker_host: str,
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if worker_budget is None:
            return None
        parsed = WorkerBudget.model_validate(worker_budget)
        self._validate_worker_budget_for_host(
            worker_host=worker_host,
            worker_budget=parsed,
        )
        return parsed.model_dump(mode="json")

    def _resolve_worker_budget_for_dispatch(
        self,
        *,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        worker_budget = (
            worker_budget_override
            if worker_budget_override is not None
            else self._candidate_worker_budget(frozen, candidate_record)
        )
        return self._normalize_worker_budget_override(
            worker_host=frozen.spec.strategy.worker_host,
            worker_budget=worker_budget,
        )

    def _build_launch_payload(
        self,
        frozen: FrozenSpec,
        candidate_id: str,
        agent_session_id: str,
        directive: dict[str, Any],
        candidate_record: CandidateRecord,
        worker_agent_type_override: str | None = None,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker_agent_type = (
            worker_agent_type_override
            or self._candidate_worker_agent_type(frozen, candidate_record)
        )
        proposal = candidate_record.task.proposal
        if proposal is not None and proposal.intent:
            short_intent = proposal.intent
        elif directive.get("goal"):
            short_intent = str(directive["goal"])
        else:
            short_intent = candidate_record.task.hypothesis

        idea_lines: list[str] = []
        if proposal is not None and proposal.intent:
            idea_lines.append(f"candidate_intent: {proposal.intent}")
            if proposal.hypothesis:
                idea_lines.append(f"candidate_hypothesis: {proposal.hypothesis}")
            if proposal.expected_tradeoff:
                idea_lines.append(f"expected_tradeoff: {proposal.expected_tradeoff}")
            if proposal.instructions:
                idea_lines.append(
                    "candidate_instructions: " + " | ".join(proposal.instructions)
                )
        else:
            idea_lines.append(f"candidate_hypothesis: {candidate_record.task.hypothesis}")
        if directive:
            idea_lines.extend(
                f"main_directive.{key}: {value}" for key, value in directive.items()
            )
        one_paragraph_idea = "; ".join(idea_lines)

        adapter = get_agent_host_adapter(frozen.spec.strategy.worker_host)
        return adapter.build_launch_payload(
            worker_agent_type=worker_agent_type,
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            short_intent=short_intent,
            one_paragraph_idea=one_paragraph_idea,
            worker_budget=(
                worker_budget_override
                if worker_budget_override is not None
                else self._candidate_worker_budget(frozen, candidate_record)
            ),
            worker_launch=self._candidate_worker_launch(frozen, candidate_record),
            root=str(self.root_dir),
            cwd=str(candidate_record.task.workspace),
            worker_prompt=self._worker_prompt_for_host(frozen.spec.strategy.worker_host),
        )

    def _build_continue_launch_payload(
        self,
        frozen: FrozenSpec,
        session: AgentSessionRecord,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker_agent_type = self._candidate_worker_agent_type(frozen, candidate_record)
        short_intent = "继续同一条自主候选循环"
        directive_text = (
            "根据最新提交的证据继续同一条自主搜索循环。刷新运行时上下文，"
            "自行选择下一个有证据支持的假设，并验证每项实质变更。"
        )

        adapter = get_agent_host_adapter(session.host)
        return adapter.build_continue_payload(
            worker_agent_type=worker_agent_type,
            candidate_id=session.candidate_id,
            agent_session_id=session.agent_session_id,
            external_id=session.host_handle.external_id,
            task_name=session.host_handle.task_name,
            short_intent=short_intent,
            one_paragraph_idea=directive_text,
            root=str(self.root_dir),
            cwd=str(candidate_record.task.workspace),
            worker_prompt=self._worker_prompt_for_host(session.host),
            worker_budget=(
                worker_budget_override
                if worker_budget_override is not None
                else self._candidate_worker_budget(frozen, candidate_record)
            ),
            worker_launch=self._candidate_worker_launch(frozen, candidate_record),
            host_metadata=session.host_handle.metadata,
        )

    def _worker_prompt_for_host(self, host: str) -> str | None:
        repository_root = Path(__file__).resolve().parents[2]
        if host == "codex":
            agent_path = repository_root / ".codex" / "agents" / "search_candidate_agent.toml"
            try:
                payload = tomllib.loads(agent_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return None
            prompt = payload.get("developer_instructions")
            return prompt if isinstance(prompt, str) and prompt.strip() else None
        if host != "pi-rpc":
            return None
        prompt_path = repository_root / ".pi" / "prompts" / "search-candidate-worker.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "首先调用 search_get_agent_context。只能在候选工作区中工作。"
            "首次编辑前读取 search_get_global_evidence；此后每完成 3 次 verifier iteration "
            "刷新一次，连续两轮没有提升或切换技术路线时提前刷新；verifier 已注入的 "
            "global_evidence_snapshot 算作刷新。修改后带一句话 hypothesis 调用 "
            "search_run_verifier。不得直接运行任务自带的 `runner`、`evaluator` 或 `grader`；"
            "所有正确性与指标反馈必须通过 `search_run_verifier`。使用运行时证据，不要依赖 "
            "transcript。"
            "如果 verifier 报告 VerifierWorkspaceSideEffect 或 "
            "candidate_action=stop_and_report，报告基础设施阻塞原因并直接返回，不要重试。"
        )

    def _next_plan_id(self, run: RunRecord) -> str:
        plan_id = f"plan_{run.next_plan_index:03d}"
        run.next_plan_index += 1
        return plan_id

    def _normalize_main_directive(
        self,
        main_directive: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        if main_directive is None:
            return {}
        if isinstance(main_directive, str):
            return {"goal": main_directive}
        if isinstance(main_directive, dict):
            return main_directive
        raise TypeError("main_directive must be a dict, string, or null")

    def _plan_independent(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        requested_k: int,
        planned_k: int,
        remaining: int,
    ) -> SearchPlan:
        work_orders = []
        for slot in range(1, planned_k + 1):
            hypothesis_index = run.candidates_total + slot - 1
            planned_candidate_id = f"c{run.next_candidate_index + slot - 1:03d}"
            hypothesis = (
                frozen.spec.root_hypotheses[hypothesis_index]
                if hypothesis_index < len(frozen.spec.root_hypotheses)
                else f"独立候选 {planned_candidate_id}"
            )
            work_orders.append(
                CandidateWorkOrder(
                    slot=slot,
                    intent=hypothesis,
                    hypothesis=hypothesis,
                    metadata={"strategy": "parallel_loops"},
                )
            )

        return SearchPlan(
            run_id=run.run_id,
            plan_id=self._next_plan_id(run),
            strategy=frozen.spec.strategy,
            requested_k=requested_k,
            planned_k=planned_k,
            remaining_budget=remaining,
            requires_agent_proposals=False,
            work_orders=work_orders,
            strategy_trace={
                "selection_rule": "独立源码分支",
                "reason": "每个候选都从冻结的源码工作区开始。",
            },
            created_at=utc_timestamp(),
        )

    def _plan_agent_guided(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        requested_k: int,
        planned_k: int,
        remaining: int,
    ) -> SearchPlan:
        return SearchPlan(
            run_id=run.run_id,
            plan_id=self._next_plan_id(run),
            strategy=frozen.spec.strategy,
            requested_k=requested_k,
            planned_k=planned_k,
            remaining_budget=remaining,
            requires_agent_proposals=True,
            strategy_trace={
                "selection_rule": "agent 引导的初始候选",
                "reason": "主 agent 只定义一次初始候选集合。",
            },
            created_at=utc_timestamp(),
        )

    def _proposal_from_work_order(self, work_order: CandidateWorkOrder) -> CandidateProposal:
        return CandidateProposal(
            hypothesis=work_order.hypothesis,
            intent=work_order.intent,
            instructions=work_order.instructions,
            metadata={
                **work_order.metadata,
                "slot": work_order.slot,
            },
        )

    def _validate_agent_proposals(
        self,
        plan: SearchPlan,
        proposals: list[CandidateProposal],
    ) -> None:
        if len(proposals) > plan.planned_k:
            raise ValueError("too many proposals for this plan")
        return

    def _create_candidate_task(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str,
        plan: SearchPlan,
        proposal: CandidateProposal,
        slot: int,
    ) -> CandidateTask:
        workspace = self._run_dir(run.run_id) / "workspace" / candidate_id

        materialization = materialize_candidate_workspace(
            backend=frozen.spec.workspace.backend,
            run_dir=self._run_dir(run.run_id),
            source=Path(run.source_path),
            workspace=workspace,
            run_id=run.run_id,
            candidate_id=candidate_id,
        )

        instructions = [
            "只能在此候选工作区内工作。",
            "使用此工作区的 .tmp/ 目录存放笔记和临时草稿。",
            "不要使用 /tmp、home 目录或候选工作区之外的路径处理候选工作。",
            "只能修改 allowed_files 中列出的文件；绝不能触碰 denied_files 或冻结的 verifier 产物。",
            "不要删除、移动或清理文件；禁止 rm、mv、rmdir、unlink、trash 和 find -delete 等破坏性命令。",
            "使用 git status、git diff 和 git log 分析工作区；runtime 拥有 verifier-backed iteration 的提交和回滚，不要自行 reset、restore 或 checkout 已验证状态。",
            "所有评分都必须通过 goal-plus_search_run_verifier；不要通过 bash 直接运行 process_verifiers 命令，也不要自行编写评分器。",
            "首次修改前调用 search_get_global_evidence；此后无需每轮读取，每完成 3 次 search_run_verifier iteration 刷新一次，连续两轮没有提升或准备切换技术路线时提前刷新。若 verifier 返回 global_evidence_injected=true，其中的 global_evidence_snapshot 已完成本次刷新，无需重复调用。结合 Evidence 和本地代码独立思考，不需要等待尚未生成的 View。",
            "把 context.agent_session_id 传给 search_run_verifier，并省略 scope 以使用 process verifier；同时用一句话 hypothesis 客观概括本轮实际尝试。",
            "每次 run_verifier 调用都会记录一个 iteration。在配置的 host 预算内工作。尽早完成并验证候选，在达到限制前停止启动新的优化 iteration，并留出足够时间返回简洁摘要。",
            "search_run_verifier 会在运行 verifier 前自动提交已修改的候选产物文件；使用 git status、git diff 和 git log 检查 iteration provenance。",
            "process verifier 返回 keep/retain/discard/failure disposition；严格硬分改善为 keep，同分为 retain 并成为 candidate-local 最新基线，只有退化或验证失败时 runtime 才恢复此前硬分最佳。开放式补充评价和 peer 比较不改变结算、硬分或最终验收。下一轮直接从返回后的已结算工作区继续。",
            "规划另一个变体前，检查 workspace/results.tsv 中继承的 iteration 日志。运行时拥有并提交这份仅追加账本，会验证已有记录未被修改，并为每份返回的 verifier 报告添加且只添加一条记录；绝不能重写、截断、删除或手动追加它。",
            "按 context.supplemental_evaluation_enabled 和 Evidence 的 supplemental_available 标记按需读取一次 search_get_evidence_detail；补充评价不参与结算。仅在当前 Git 能解析该 commit 且代码证据必要时用 git diff HEAD <commit> -- <allowed-file> 做只读比较；不要访问或 fetch peer workspace，也不要 checkout/reset peer commit。",
        ]
        share_out_dir = None
        if frozen.spec.shared_dir.enabled:
            SharedDirManager(self._run_dir(run.run_id)).ensure_layout()
            share_out_dir = workspace / SHARE_OUT_RELATIVE_PATH
            share_out_dir.mkdir(parents=True, exist_ok=True)
            instructions.extend(
                [
                    "shared_dir 发布方规则：工具化的目标是降低同一 run 内其他 candidate 重建诊断或检查流程的成本，不要求跨项目通用。每次 verifier 前回顾本轮及此前 iteration 的命令序列、临时代码片段和 scratch scripts。能在 peer workspace 运行、不依赖当前 candidate 临时私有状态，并命中至少一个正向信号时，默认提炼为最小工具：repeated_sequence（等价多步流程至少两次）、domain_probe（非显然领域对象构造、边界条件或断言）、parser_or_trace（解析、trace、复现、转换或 mutation 检查）、peer_setup_reduction（明显降低 peer 重建成本）。",
                    f"将显式源文件放在 {TOOL_DRAFTS_RELATIVE_PATH}/，再调用 search_stage_shared_tool 生成 {SHARE_OUT_RELATIVE_PATH}/ staging。不要因为工具短、任务专属、来自临时代码片段或只产生退出码而拒绝。只有具体排除项才支持 not_applicable：single_common_command、logic_free_wrapper、restricted_artifact（主产物、candidate 测试、冻结 verifier/grader、日志、数据、凭据或构建输出）、candidate_private_state 或 duplicate_snapshot。",
                    "每次归属于当前 worker 的 process verifier 都提交 toolization_decision。staged 至少列出一个正向 signal 和实际 tool_names；not_applicable 必须给出具体 exclusion，不能只写不可复用。runtime 以 staging inventory 和 publication settlement 为权威；决策缺失或与 staging 不匹配只生成 monitor/report advisory，不改变 score、disposition、selection 或 promotion。",
                    "工具只有在 annotator 生成并由 runtime 绑定 Tool View 后才会出现在 Global Evidence；需要时通过 search_copy_shared_tool 复制并在本候选中重新验证。",
                ]
            )
        if plan.worker_policy.get("worker_agent_type"):
            instructions.append(
                "对受管 agent session 使用 "
                f"worker_agent_type={plan.worker_policy['worker_agent_type']!r}。"
            )
        instructions.extend(proposal.instructions)

        hypothesis = proposal.hypothesis or proposal.intent or f"候选 {candidate_id}"
        selected_model = self._selected_model_for_slot(plan, slot)
        return CandidateTask(
            run_id=run.run_id,
            candidate_id=candidate_id,
            plan_id=plan.plan_id,
            hypothesis=hypothesis,
            workspace=workspace,
            workspace_backend=materialization.backend,
            workspace_branch=materialization.branch,
            workspace_base_revision=materialization.base_revision,
            share_out_dir=share_out_dir,
            allowed_files=frozen.spec.edit_surface.allow,
            denied_files=frozen.spec.edit_surface.deny,
            instructions=instructions,
            expected_artifacts=["patch", "notes", "logs"],
            stop_conditions={},
            proposal=proposal,
            selected_model=selected_model,
            model_provenance=(
                self._selected_model_provenance(selected_model)
                if selected_model
                else {}
            ),
            strategy_metadata={
                "strategy": plan.strategy.name,
                "worker_policy": plan.worker_policy,
                "plan_id": plan.plan_id,
                "slot": slot,
                "selected_model": selected_model.model if selected_model else None,
                "workspace_backend": materialization.backend,
                "workspace_branch": materialization.branch,
                "workspace_base_revision": materialization.base_revision,
            },
        )

    @staticmethod
    def _results_tsv_path(workspace: Path) -> Path:
        return workspace / RESULTS_TSV_RELATIVE_PATH

    @staticmethod
    def _legacy_results_tsv_path(workspace: Path) -> Path:
        return workspace / LEGACY_RESULTS_TSV_RELATIVE_PATH

    @staticmethod
    def _tsv_cell(value: str) -> str:
        return " ".join(value.replace("\t", " ").split()).strip()

    @classmethod
    def _iteration_hypothesis(
        cls,
        hypothesis: str | None,
        record: CandidateRecord,
        iteration_number: int,
        *,
        scope: str,
        agent_session_id: str | None,
    ) -> str:
        if hypothesis is not None:
            normalized = cls._tsv_cell(hypothesis)
            if normalized:
                return normalized
        if agent_session_id is None:
            return f"main {scope} verification"
        if iteration_number == 1:
            candidate_hypothesis = cls._tsv_cell(record.task.hypothesis)
            if candidate_hypothesis:
                return f"candidate baseline: {candidate_hypothesis}"
        return f"iteration {iteration_number}"

    @staticmethod
    def _format_result_score(score: float | None) -> str:
        if score is None:
            return ""
        if math.isfinite(score) and score.is_integer():
            return str(int(score))
        return str(score)

    def _read_results_tsv(self, record: CandidateRecord) -> list[ResultLedgerEntry]:
        paths = (
            self._results_tsv_path(record.task.workspace),
            self._legacy_results_tsv_path(record.task.workspace),
        )
        path = next((candidate for candidate in paths if candidate.is_file()), None)
        if path is None:
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        if not lines:
            return []
        header = lines[0].split("\t")
        if len(header) < 4 or header[0].strip() != "commit":
            return []
        metric_name = header[1].strip()
        if not metric_name:
            return []

        remaining_iterations = list(record.iterations)

        entries: list[ResultLedgerEntry] = []
        for line in lines[1:]:
            columns = line.split("\t", 3)
            if len(columns) != 4:
                continue
            git_head, score_text, status, hypothesis = columns
            try:
                score = float(score_text) if score_text.strip() else None
            except ValueError:
                score = None
            normalized_git_head = git_head.strip()
            matching_index = next(
                (
                    index
                    for index, candidate in enumerate(remaining_iterations)
                    if candidate.git_head
                    and normalized_git_head
                    and (
                        candidate.git_head == normalized_git_head
                        or candidate.git_head.startswith(normalized_git_head)
                        or normalized_git_head.startswith(candidate.git_head)
                    )
                ),
                None,
            )
            iteration = (
                remaining_iterations.pop(matching_index)
                if matching_index is not None
                else None
            )
            normalized_status = self._tsv_cell(status) or "unknown"
            entries.append(
                ResultLedgerEntry(
                    source_run_id=record.task.run_id,
                    source_candidate_id=record.candidate_id,
                    iteration=iteration.iteration if iteration else None,
                    git_head=git_head.strip() or None,
                    ledger_git_head=iteration.ledger_git_head if iteration else None,
                    metric_name=metric_name,
                    score=score,
                    status=normalized_status,
                    hypothesis=self._tsv_cell(hypothesis),
                    failure_class=iteration.failure_class if iteration else None,
                    created_at=iteration.created_at if iteration else None,
                )
            )
        for iteration in remaining_iterations:
            hypothesis = self._tsv_cell(iteration.hypothesis or iteration.summary)
            entries.append(
                ResultLedgerEntry(
                    source_run_id=record.task.run_id,
                    source_candidate_id=record.candidate_id,
                    iteration=iteration.iteration,
                    git_head=iteration.git_head,
                    ledger_git_head=iteration.ledger_git_head,
                    metric_name=metric_name,
                    score=iteration.score,
                    status="pass" if iteration.process_passed else "fail",
                    hypothesis=hypothesis or f"recovered iteration {iteration.iteration}",
                    failure_class=iteration.failure_class,
                    created_at=iteration.created_at,
                )
            )
        return entries

    def _backfill_results_ledger_from_iterations(
        self,
        record: CandidateRecord,
        metric_name: str,
    ) -> None:
        represented = {
            entry.iteration
            for entry in record.results_ledger
            if entry.source_run_id == record.task.run_id
            and entry.source_candidate_id == record.candidate_id
            and entry.iteration is not None
        }
        for iteration in record.iterations:
            if iteration.iteration in represented:
                continue
            hypothesis = self._tsv_cell(iteration.hypothesis or iteration.summary)
            record.results_ledger.append(
                ResultLedgerEntry(
                    source_run_id=record.task.run_id,
                    source_candidate_id=record.candidate_id,
                    iteration=iteration.iteration,
                    git_head=iteration.git_head,
                    ledger_git_head=iteration.ledger_git_head,
                    metric_name=metric_name,
                    score=iteration.score,
                    status="pass" if iteration.process_passed else "fail",
                    hypothesis=hypothesis or f"recovered iteration {iteration.iteration}",
                    failure_class=iteration.failure_class,
                    created_at=iteration.created_at,
                )
            )

    def _candidate_results_ledger(
        self,
        record: CandidateRecord,
    ) -> list[ResultLedgerEntry]:
        if record.results_ledger:
            return [entry.model_copy(deep=True) for entry in record.results_ledger]
        return self._read_results_tsv(record)

    def _preferred_source_candidate_record(
        self,
        source_run_id: str,
    ) -> CandidateRecord | None:
        source_run = self._load_run(source_run_id)
        records = self._load_candidate_records(source_run_id)
        by_id = {record.candidate_id: record for record in records}
        source_frozen = self._load_frozen_spec(source_run.frozen_spec_id)
        for candidate_id in (
            source_run.selected_candidate_id,
            source_run.best_candidate_id,
        ):
            if candidate_id and candidate_id in by_id:
                record = by_id[candidate_id]
                self._backfill_results_ledger_from_iterations(
                    record,
                    source_frozen.spec.metric_name,
                )
                return record
        if not records:
            return None
        frontier = self._top_records(records, source_frozen.spec, 1)
        if not frontier:
            return None
        record = frontier[0]
        self._backfill_results_ledger_from_iterations(
            record,
            source_frozen.spec.metric_name,
        )
        return record

    def _inherited_results_ledger(
        self,
        run: RunRecord,
        task: CandidateTask,
    ) -> list[ResultLedgerEntry]:
        source_record: CandidateRecord | None = None
        if task.base_candidate_id:
            source_record = self._load_candidate_record(
                run.run_id,
                task.base_candidate_id,
            )
        elif run.source_run_id:
            source_record = self._preferred_source_candidate_record(run.source_run_id)
        if source_record is None:
            return []
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        self._backfill_results_ledger_from_iterations(
            source_record,
            frozen.spec.metric_name,
        )
        return self._candidate_results_ledger(source_record)

    def _render_results_tsv(
        self,
        entries: list[ResultLedgerEntry],
        metric_name: str,
    ) -> str:
        normalized_metric_name = self._tsv_cell(metric_name) or "metric"
        lines = [f"commit\t{normalized_metric_name}\tstatus\thypothesis"]
        for entry in entries:
            if entry.metric_name != metric_name:
                continue
            lines.append(
                "\t".join(
                    [
                        entry.git_head or "",
                        self._format_result_score(entry.score),
                        self._tsv_cell(entry.status) or "unknown",
                        self._tsv_cell(entry.hypothesis),
                    ]
                )
            )
        return "\n".join(lines) + "\n"

    def _assert_results_tsv_unchanged(
        self,
        record: CandidateRecord,
        metric_name: str,
    ) -> Path:
        path = self._results_tsv_path(record.task.workspace)
        expected = self._render_results_tsv(record.results_ledger, metric_name)
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv is missing; "
                "the runtime-owned ledger may not be deleted or replaced"
            ) from exc
        if actual != expected:
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv changed outside "
                "the runtime; existing rows are immutable and only one "
                "runtime-appended row is allowed per returned verifier report"
            )
        try:
            status = self._git_output(
                record.task.workspace,
                [
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                    RESULTS_TSV_RELATIVE_PATH,
                ],
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "ResultsLedgerMutation: could not verify the Git state of "
                "workspace/results.tsv"
            ) from exc
        if status.strip():
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv must stay clean "
                "between runtime appends"
            )
        return path

    def _ensure_results_tsv(
        self,
        record: CandidateRecord,
        metric_name: str,
    ) -> Path:
        if record.results_ledger_git_head is not None:
            return self._assert_results_tsv_unchanged(record, metric_name)
        if not record.results_ledger and record.results_ledger_git_head is None:
            record.results_ledger = self._read_results_tsv(record)
        if record.results_ledger_git_head is None:
            self._backfill_results_ledger_from_iterations(record, metric_name)
        path = self._results_tsv_path(record.task.workspace)
        write_text(path, self._render_results_tsv(record.results_ledger, metric_name))
        self._initialize_workspace_git_for_results(record.task.workspace)
        ledger_git_head = self._commit_results_tsv(
            record.task.workspace,
            f"goal-plus results ledger sync {record.candidate_id}",
        )
        if ledger_git_head is None:
            raise RuntimeError(
                "ResultsLedgerCommitError: failed to commit workspace/results.tsv"
            )
        record.results_ledger_git_head = ledger_git_head
        self._assert_results_tsv_unchanged(record, metric_name)
        return path

    def _append_results_tsv(
        self,
        record: CandidateRecord,
        entry: ResultLedgerEntry,
        metric_name: str,
    ) -> str:
        path = self._assert_results_tsv_unchanged(record, metric_name)
        before = self._render_results_tsv(record.results_ledger, metric_name)
        after = self._render_results_tsv(
            [*record.results_ledger, entry],
            metric_name,
        )
        if not after.startswith(before) or len(after.splitlines()) != len(
            before.splitlines()
        ) + 1:
            raise RuntimeError(
                "ResultsLedgerAppendError: a verifier result must append exactly "
                "one row without changing the existing workspace/results.tsv"
            )
        write_text(path, after)
        ledger_git_head = self._commit_results_tsv(
            record.task.workspace,
            (
                f"goal-plus results ledger {record.candidate_id}:"
                f"{entry.iteration or 'legacy'}"
            ),
        )
        if ledger_git_head is None:
            raise RuntimeError(
                "ResultsLedgerCommitError: failed to commit the appended "
                "workspace/results.tsv row"
            )
        entry.ledger_git_head = ledger_git_head
        record.results_ledger.append(entry)
        record.results_ledger_git_head = ledger_git_head
        self._assert_results_tsv_unchanged(record, metric_name)
        return ledger_git_head

    def _record_ranking_score(
        self,
        record: CandidateRecord,
        spec: SearchSpec,
    ) -> float | None:
        best_iteration = self._best_iteration_record(record, spec.metric_direction)
        if best_iteration is not None:
            return best_iteration.score
        if (
            record.score_report
            and record.score_report.process_passed
            and record.score_report.aggregate_score is not None
        ):
            return record.score_report.aggregate_score
        return None

    def _scored_records(
        self,
        records: list[CandidateRecord],
        spec: SearchSpec,
    ) -> list[CandidateRecord]:
        return [
            record
            for record in records
            if self._record_ranking_score(record, spec) is not None
        ]

    def _best_record(self, records: list[CandidateRecord], spec: SearchSpec) -> CandidateRecord:
        reverse = spec.metric_direction == "maximize"
        return sorted(
            records,
            key=lambda record: self._record_ranking_score(record, spec),
            reverse=reverse,
        )[0]  # type: ignore[arg-type,return-value]

    def _top_records(
        self,
        records: list[CandidateRecord],
        spec: SearchSpec,
        top_n: int,
    ) -> list[CandidateRecord]:
        scored = self._scored_records(records, spec)
        if not scored:
            return self._records_by_created(records)[:top_n]
        reverse = spec.metric_direction == "maximize"
        return sorted(
            scored,
            key=lambda record: self._record_ranking_score(record, spec),
            reverse=reverse,
        )[:top_n]  # type: ignore[arg-type,return-value]

    def _records_by_created(self, records: list[CandidateRecord]) -> list[CandidateRecord]:
        def created_index(record: CandidateRecord) -> int:
            try:
                return int(record.candidate_id.removeprefix("c"))
            except ValueError:
                return 0

        return sorted(records, key=created_index)

    def _last_batch_records(self, records: list[CandidateRecord]) -> list[CandidateRecord]:
        plan_ids = [record.task.plan_id for record in records if record.task.plan_id]
        if not plan_ids:
            return self._records_by_created(records)
        last_plan_id = sorted(plan_ids)[-1]
        return self._records_by_created(
            [record for record in records if record.task.plan_id == last_plan_id]
        )

    def _markdown_cell(self, value: str) -> str:
        return value.replace("\n", " ").replace("|", "\\|")

    def _precheck_candidate(
        self,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> ScoreReport | None:
        results: list[VerifierResult] = []

        if record.touched_denied_files or record.changed_outside_allowed:
            outside_allowed = [
                path
                for path in record.detected_changed_files
                if not path_matches(path, frozen.spec.edit_surface.allow)
            ]
            frozen_paths = set(frozen.verifier_hashes)
            verifier_workspace_side_effects = bool(outside_allowed) and all(
                path.startswith(".goal-plus-verifiers/")
                and path not in frozen_paths
                for path in outside_allowed
            )
            failure_class = (
                "VerifierWorkspaceSideEffect"
                if verifier_workspace_side_effects
                else "EditSurfaceViolation"
            )
            results.append(
                VerifierResult(
                    name="edit_surface_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={
                        "detected_changed_files": record.detected_changed_files,
                        "touched_denied_files": record.touched_denied_files,
                        "changed_outside_allowed": record.changed_outside_allowed,
                        "infrastructure_failure": verifier_workspace_side_effects,
                        "candidate_action": (
                            "stop_and_report"
                            if verifier_workspace_side_effects
                            else "repair_candidate_edit_surface"
                        ),
                    },
                    failure_class=failure_class,
                )
            )

        hash_failures = self._frozen_hash_failures(frozen, record.task.workspace)
        if hash_failures:
            results.append(
                VerifierResult(
                    name="frozen_hash_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={"hash_failures": hash_failures},
                    failure_class="FrozenVerifierModified",
                )
            )

        if not results:
            return None

        return ScoreReport(
            run_id=record.task.run_id,
            candidate_id=record.candidate_id,
            parent_id=record.task.parent_id,
            validity_passed=False,
            process_passed=False,
            promotion_passed=None,
            aggregate_score=0.0,
            verifier_results=results,
            touched_denied_files=record.touched_denied_files,
            changed_outside_allowed=record.changed_outside_allowed,
            hardcoding_suspected=any(
                result.failure_class
                in {"EditSurfaceViolation", "FrozenVerifierModified"}
                for result in results
            ),
        )

    def _run_commands(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        commands: list[VerifierCommand],
        scope: str,
    ) -> ScoreReport:
        verifier_phase: Literal["candidate", "promotion"] = (
            "candidate" if scope == "process" else "promotion"
        )
        results: list[VerifierResult] = []
        for command in commands:
            result = self._run_command(
                run,
                frozen,
                record,
                command,
                verifier_phase,
            )
            results.append(result)
            if result.failure_class == "VerifierWorkspaceSideEffect":
                break
        hard_failed = any(
            not result.passed
            and result.role
            in {
                VerifierRole.VALIDITY_GATE,
                VerifierRole.PROCESS_GATE,
                VerifierRole.PROMOTION_GATE,
                VerifierRole.ANTI_CHEAT_GATE,
            }
            for result in results
        )
        process_passed = not hard_failed and all(
            result.passed or result.role == VerifierRole.DIAGNOSTIC_SIGNAL for result in results
        )
        score = self._aggregate_score(frozen.spec.metric_name, results)
        if not process_passed:
            score = 0.0

        return ScoreReport(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            parent_id=record.task.parent_id,
            validity_passed=process_passed,
            process_passed=process_passed,
            promotion_passed=process_passed if scope == "promotion" else None,
            aggregate_score=score,
            verifier_results=results,
            touched_denied_files=record.touched_denied_files,
            changed_outside_allowed=record.changed_outside_allowed,
            hardcoding_suspected=False,
        )

    def _run_command(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        command: VerifierCommand,
        verifier_phase: Literal["candidate", "promotion"],
    ) -> VerifierResult:
        if command.command[0] == "goal-plus-internal":
            return self._run_internal_command(frozen, record, command)

        log_scope = "process" if verifier_phase == "candidate" else "promotion"
        logs_dir = (
            self._run_dir(run.run_id)
            / "candidates"
            / record.candidate_id
            / "logs"
            / log_scope
        )
        logs_dir.mkdir(parents=True, exist_ok=True)
        command_log_name = safe_verifier_name(command.name)
        iteration_number = len(record.iterations) + 1
        log_path = logs_dir / (
            f"iteration-{iteration_number:04d}-{command_log_name}-"
            f"{uuid.uuid4().hex[:8]}.log"
        )
        diagnostics_dir = (
            logs_dir
            / "diagnostics"
            / f"{command_log_name}-{uuid.uuid4().hex[:12]}"
        )
        diagnostics_dir.mkdir(parents=True, exist_ok=False)
        cwd = (record.task.workspace / command.cwd).resolve()
        workspace_before = self._hash_verifier_workspace(record.task.workspace)
        git_head_before = self._git_head(record.task.workspace)
        start = time.perf_counter()
        try:
            with verifier_resource_lock(command.resource_lock):
                with tempfile.TemporaryDirectory(
                    prefix="goal-plus-verifier-command-"
                ) as verifier_tmp:
                    completed = self._execute_verifier_process(
                        command.command,
                        cwd=cwd,
                        env=self._verifier_environment(
                            cwd,
                            Path(verifier_tmp),
                            phase=verifier_phase,
                            diagnostics_dir=diagnostics_dir,
                            resource=command.resource_lock,
                        ),
                        text=True,
                        capture_output=True,
                        timeout=command.timeout_seconds,
                        check=False,
                    )
            elapsed = time.perf_counter() - start
            metrics = self._parse_metrics(completed.stdout)
            metrics.setdefault("returncode", completed.returncode)
            metrics.setdefault("elapsed_seconds", elapsed)
            metrics.update(self._verifier_diagnostics(diagnostics_dir))
            side_effects = self._hash_changes(
                workspace_before,
                self._hash_verifier_workspace(record.task.workspace),
            )
            if side_effects:
                cleanup_failures = self._restore_verifier_workspace(
                    record.task.workspace,
                    workspace_before,
                    side_effects,
                    git_head_before,
                )
                metrics.update(
                    {
                        "verifier_workspace_side_effects": side_effects,
                        "cleanup_failures": cleanup_failures,
                        "infrastructure_failure": True,
                        "candidate_action": "stop_and_report",
                    }
                )
                self._add_visible_verifier_feedback(
                    command,
                    metrics,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
                log_path.write_text(
                    _bounded_log(
                        "\n".join(
                            [
                                f"$ {' '.join(command.command)}",
                                f"cwd: {cwd}",
                                f"returncode: {completed.returncode}",
                                f"verifier_workspace_side_effects: {side_effects}",
                                f"cleanup_failures: {cleanup_failures}",
                                "",
                                "## stdout",
                                completed.stdout,
                                "## stderr",
                                completed.stderr,
                            ]
                        )
                    ),
                    encoding="utf-8",
                )
                return VerifierResult(
                    name=command.name,
                    role=command.role,
                    passed=False,
                    score=0.0,
                    metrics=metrics,
                    log_path=log_path,
                    failure_class="VerifierWorkspaceSideEffect",
                )
            score = self._score_from_metrics(frozen.spec.metric_name, metrics)
            has_verifier_error = self._has_verifier_error(metrics)
            missing_numeric_metric = (
                completed.returncode == 0
                and not has_verifier_error
                and command.role == VerifierRole.RANKING_SIGNAL
                and score is None
            )
            if missing_numeric_metric:
                metrics["expected_metric_name"] = frozen.spec.metric_name
            passed = (
                completed.returncode == 0
                and not has_verifier_error
                and not missing_numeric_metric
            )
            if not passed:
                self._add_visible_verifier_feedback(
                    command,
                    metrics,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            log_path.write_text(
                _bounded_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"returncode: {completed.returncode}",
                            "",
                            "## stdout",
                            completed.stdout,
                            "## stderr",
                            completed.stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=passed,
                score=score,
                metrics=metrics,
                log_path=log_path,
                failure_class=(
                    None
                    if passed
                    else (
                        "MissingNumericMetric"
                        if missing_numeric_metric
                        else "VerifierCommandFailed"
                    )
                ),
            )
        except subprocess.TimeoutExpired as exc:
            side_effects = self._hash_changes(
                workspace_before,
                self._hash_verifier_workspace(record.task.workspace),
            )
            cleanup_failures = self._restore_verifier_workspace(
                record.task.workspace,
                workspace_before,
                side_effects,
                git_head_before,
            ) if side_effects else []
            stdout = self._coerce_verifier_output(exc.stdout)
            stderr = self._coerce_verifier_output(exc.stderr)
            log_path.write_text(
                _bounded_log(
                    "\n".join(
                        [
                            f"$ {' '.join(command.command)}",
                            f"cwd: {cwd}",
                            f"timeout_seconds: {command.timeout_seconds}",
                            "",
                            "## stdout",
                            stdout,
                            "## stderr",
                            stderr,
                        ]
                    )
                ),
                encoding="utf-8",
            )
            metrics: dict[str, Any] = {
                "timeout_seconds": command.timeout_seconds,
                "verifier_workspace_side_effects": side_effects,
                "cleanup_failures": cleanup_failures,
                "infrastructure_failure": bool(side_effects),
                "candidate_action": (
                    "stop_and_report" if side_effects else "inspect_timeout"
                ),
            }
            metrics.update(self._verifier_diagnostics(diagnostics_dir))
            self._add_visible_verifier_feedback(
                command,
                metrics,
                stdout=stdout,
                stderr=stderr,
            )
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class=(
                    "VerifierWorkspaceSideEffect" if side_effects else "Timeout"
                ),
            )
        except OSError as exc:
            log_path.write_text(_bounded_log(str(exc)), encoding="utf-8")
            metrics: dict[str, Any] = {"error": str(exc)}
            metrics.update(self._verifier_diagnostics(diagnostics_dir))
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics=metrics,
                log_path=log_path,
                failure_class="VerifierStartFailed",
            )

    def _verifier_diagnostics(self, diagnostics_dir: Path) -> dict[str, Any]:
        files = sorted(
            path.relative_to(diagnostics_dir).as_posix()
            for path in diagnostics_dir.rglob("*")
            if path.is_file()
        )
        if not files:
            shutil.rmtree(diagnostics_dir, ignore_errors=True)
            return {}
        return {
            "diagnostics_dir": str(diagnostics_dir),
            "diagnostic_files": files,
        }

    @staticmethod
    def _coerce_verifier_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _add_visible_verifier_feedback(
        self,
        command: VerifierCommand,
        metrics: dict[str, Any],
        *,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
    ) -> None:
        if command.feedback_policy != FeedbackPolicy.VISIBLE_TO_WORKERS:
            return
        for key, value in (("stdout_tail", stdout), ("stderr_tail", stderr)):
            output = self._coerce_verifier_output(value).strip()
            if output:
                metrics[key] = output[-MAX_VERIFIER_FEEDBACK_CHARS:]

    def _restore_verifier_workspace(
        self,
        workspace: Path,
        before: dict[str, str],
        side_effects: list[str],
        git_head_before: str | None,
    ) -> list[str]:
        cleanup_failures: list[str] = []
        if git_head_before is not None:
            try:
                self._git_output(
                    workspace,
                    ["git", "reset", "--hard", git_head_before],
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                cleanup_failures.extend(
                    path for path in side_effects if path in before
                )
        else:
            cleanup_failures.extend(path for path in side_effects if path in before)

        for rel_path in side_effects:
            if rel_path in before:
                continue
            target = workspace / rel_path
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                parent = target.parent
                while parent != workspace:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except OSError:
                cleanup_failures.append(rel_path)

        remaining = self._hash_changes(
            before,
            self._hash_verifier_workspace(workspace),
        )
        cleanup_failures.extend(
            path for path in remaining if path not in cleanup_failures
        )
        return cleanup_failures

    def _run_internal_command(
        self,
        frozen: FrozenSpec,
        record: CandidateRecord,
        command: VerifierCommand,
    ) -> VerifierResult:
        if len(command.command) < 2 or command.command[1] != "check-frozen-hashes":
            return VerifierResult(
                name=command.name,
                role=command.role,
                passed=False,
                score=0.0,
                metrics={"error": "unknown internal command"},
                failure_class="UnknownInternalCommand",
            )
        failures = self._frozen_hash_failures(frozen, record.task.workspace)
        return VerifierResult(
            name=command.name,
            role=command.role,
            passed=not failures,
            score=1.0 if not failures else 0.0,
            metrics={"hash_failures": failures},
            failure_class=None if not failures else "FrozenVerifierModified",
        )

    def _parse_metrics(self, stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _has_verifier_error(self, metrics: dict[str, Any]) -> bool:
        """Treat a non-null top-level error value as verifier failure."""
        return metrics.get("error") is not None

    def _score_from_metrics(self, metric_name: str, metrics: dict[str, Any]) -> float | None:
        for key in (metric_name, "combined_score", "score", "overall_score"):
            value = metrics.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                score = float(value)
                if math.isfinite(score):
                    return score
        return None

    def _aggregate_score(self, metric_name: str, results: list[VerifierResult]) -> float | None:
        for result in results:
            if result.score is not None and result.role != VerifierRole.ANTI_CHEAT_GATE:
                return result.score
            score = self._score_from_metrics(metric_name, result.metrics)
            if score is not None:
                return score
        return None

    def _update_best_seen(
        self,
        run: RunRecord,
        spec: SearchSpec,
        report: ScoreReport,
    ) -> bool:
        if (
            report.disposition not in {"keep", "retain"}
            or report.aggregate_score is None
        ):
            return False
        is_best = run.best_score is None or (
            report.aggregate_score >= run.best_score
            if spec.metric_direction == "maximize"
            else report.aggregate_score <= run.best_score
        )
        if not is_best:
            return False
        run.best_score = report.aggregate_score
        run.best_candidate_id = report.candidate_id
        return True

    def _write_best_artifact(
        self,
        run: RunRecord,
        spec: SearchSpec,
        record: CandidateRecord,
        iteration: IterationRecord,
    ) -> None:
        if not self._git_iteration_eligible(iteration) or not iteration.artifact_hash:
            raise RuntimeError("run best iteration is not Git-backed")
        workspace = record.task.workspace.resolve().relative_to(
            self._run_dir(run.run_id).resolve()
        )
        best = BestArtifactRecord(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            iteration=iteration.iteration,
            commit=str(iteration.git_head),
            score=float(iteration.score),
            metric_name=spec.metric_name,
            metric_direction=spec.metric_direction,
            artifact_hash=str(iteration.artifact_hash),
            workspace=workspace.as_posix(),
            changed_files=iteration.changed_files,
            updated_at=iteration.created_at,
        )
        write_json(
            self._run_dir(run.run_id) / "best.json",
            best.model_dump(mode="json"),
        )

    def _resolve_shared_tool(
        self, run_id: str, tool_id: str, snapshot_hash: str
    ) -> SharedToolRecord:
        available = {
            tool.tool_id: tool
            for record in self._load_candidate_records(run_id)
            for iteration in record.iterations
            for tool in iteration.shared_tools
        }
        tool = available.get(tool_id)
        if tool is None:
            raise ValueError(f"tool_id is not published in this run: {tool_id}")
        if tool.snapshot_hash != snapshot_hash:
            raise ValueError(f"tool snapshot_hash mismatch for {tool_id}")
        if not self._tool_view_is_published(run_id, tool):
            raise ValueError(
                "tool is not discoverable before its Tool View is published: "
                f"{tool_id}"
            )
        return tool

    def _tool_view_is_published(self, run_id: str, tool: SharedToolRecord) -> bool:
        task = self._load_evidence_annotation_task(run_id, tool.candidate_id, tool.iteration)
        if task is None or task.state != "completed" or task.view is None:
            return False
        return any(
            view.tool_id == tool.tool_id
            and view.snapshot_hash == tool.snapshot_hash
            and view.source_commit == (tool.source_commit or task.attempt_commit)
            for view in task.view.tool_views
        )

    @staticmethod
    def _git_iteration_eligible(iteration: IterationRecord) -> bool:
        return bool(
            iteration.process_passed is True
            and iteration.score is not None
            and math.isfinite(iteration.score)
            and iteration.git_head
            and iteration.git_artifact_clean is True
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
        )

    @classmethod
    def _iteration_disposition(
        cls,
        iteration: IterationRecord,
        prior_best: IterationRecord | None,
        metric_direction: Literal["maximize", "minimize"],
    ) -> IterationDisposition:
        if not cls._git_iteration_eligible(iteration):
            return "failure"
        if prior_best is None:
            return "keep"
        assert iteration.score is not None and prior_best.score is not None
        improved = (
            iteration.score > prior_best.score
            if metric_direction == "maximize"
            else iteration.score < prior_best.score
        )
        if improved:
            return "keep"
        if iteration.score == prior_best.score:
            return "retain"
        return "discard"

    def _best_current_artifact_iteration(
        self,
        run: RunRecord,
        record: CandidateRecord,
        metric_direction: Literal["maximize", "minimize"],
    ) -> IterationRecord | None:
        current_changed = self._detect_changed_files(
            Path(run.source_path), record.task.workspace
        )
        current_artifact_hash = self._artifact_hash(
            record.task.workspace, current_changed
        )
        selectable = [
            iteration
            for iteration in record.iterations
            if iteration.process_passed is True
            and iteration.score is not None
            and iteration.artifact_hash == current_artifact_hash
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
            and iteration.disposition not in {"discard", "failure"}
        ]
        if not selectable:
            return None
        reverse = metric_direction == "maximize"
        return sorted(
            reversed(selectable),
            key=lambda iteration: iteration.score,
            reverse=reverse,
        )[0]

    def _best_iteration_record(
        self,
        record: CandidateRecord,
        metric_direction: Literal["maximize", "minimize"],
    ) -> IterationRecord | None:
        scored = [
            iteration
            for iteration in record.iterations
            if iteration.process_passed is True
            and iteration.score is not None
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
            and iteration.disposition not in {"discard", "failure"}
        ]
        if not scored:
            return None
        reverse = metric_direction == "maximize"
        return sorted(
            reversed(scored),
            key=lambda iteration: iteration.score,
            reverse=reverse,
        )[0]

    def _best_git_iteration_record(
        self,
        record: CandidateRecord,
        metric_direction: Literal["maximize", "minimize"],
    ) -> IterationRecord | None:
        scored = [
            iteration
            for iteration in record.iterations
            if self._git_iteration_eligible(iteration)
            and iteration.disposition not in {"discard", "failure"}
        ]
        if not scored:
            return None
        reverse = metric_direction == "maximize"
        return sorted(
            reversed(scored),
            key=lambda iteration: iteration.score,
            reverse=reverse,
        )[0]

    def _selection_options(
        self,
        run: RunRecord,
        records: list[CandidateRecord],
        metric_direction: Literal["maximize", "minimize"],
    ) -> list[tuple[float, CandidateRecord, int | None, str | None]]:
        options: list[tuple[float, CandidateRecord, int | None, str | None]] = []
        for record in records:
            current_changed = self._detect_changed_files(
                Path(run.source_path), record.task.workspace
            )
            current_artifact_hash = self._artifact_hash(
                record.task.workspace, current_changed
            )
            report_is_represented = False
            for iteration in reversed(record.iterations):
                if (
                    iteration.process_passed is not True
                    or iteration.score is None
                    or iteration.touched_denied_files
                    or iteration.changed_outside_allowed
                    or iteration.disposition in {"discard", "failure"}
                ):
                    continue
                if iteration.git_head and iteration.git_artifact_clean is True:
                    options.append(
                        (
                            iteration.score,
                            record,
                            iteration.iteration,
                            iteration.git_head,
                        )
                    )
                elif iteration.artifact_hash == current_artifact_hash:
                    options.append((iteration.score, record, iteration.iteration, None))
                if (
                    record.score_report
                    and iteration.artifact_hash == current_artifact_hash
                    and iteration.process_passed == record.score_report.process_passed
                    and iteration.score == record.score_report.aggregate_score
                ):
                    report_is_represented = True

            if (
                record.score_report
                and record.score_report.process_passed
                and record.score_report.aggregate_score is not None
                and not record.touched_denied_files
                and not record.changed_outside_allowed
                and not report_is_represented
            ):
                options.append(
                    (record.score_report.aggregate_score, record, None, None)
                )
        return options

    def _candidate_research_summary(
        self,
        run_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Return the latest bounded worker-authored research handoff.

        This is deliberately a compact cross-round summary, not a transcript
        or a generalized experience-memory layer. Older handoff keys remain
        readable so existing runs still contribute useful evidence.
        """

        def items(value: Any) -> list[Any]:
            if value is None or value == "":
                return []
            if isinstance(value, list):
                return value[:5]
            return [value]

        for session in reversed(self._load_agent_sessions(run_id)):
            if session.candidate_id != candidate_id:
                continue
            metadata = session.host_handle.metadata or {}
            progress = metadata.get("progress_handoff")
            if not isinstance(progress, dict):
                continue
            model_handoff = progress.get("model_handoff")
            if not isinstance(model_handoff, dict):
                legacy_keys = {
                    "key_results",
                    "what_was_tried",
                    "pitfalls",
                    "blockers",
                    "next_steps",
                    "next_action",
                    "verifier_assessment",
                }
                if legacy_keys.intersection(progress):
                    model_handoff = progress
                else:
                    continue
            summary = model_handoff.get("summary")
            if not isinstance(summary, str):
                summary = progress.get("summary")
            key_results = items(
                model_handoff.get("key_results", model_handoff.get("what_was_tried"))
            )
            verifier_assessment = model_handoff.get("verifier_assessment")
            if not isinstance(verifier_assessment, dict):
                verifier_assessment = {
                    "status": "unknown",
                    "evidence": [],
                    "impact": "",
                    "recommended_action": "keep_spec",
                }
            else:
                verifier_assessment = dict(verifier_assessment)
                status = verifier_assessment.get("status")
                evidence = items(verifier_assessment.get("evidence"))
                if status not in {"adequate", "concern", "unknown"}:
                    status = "unknown"
                if status == "concern" and not evidence:
                    status = "unknown"
                action = verifier_assessment.get("recommended_action")
                if action not in {"keep_spec", "investigate", "upgrade_spec"}:
                    action = "investigate" if status == "concern" else "keep_spec"
                verifier_assessment = {
                    "status": status,
                    "evidence": evidence,
                    "impact": str(verifier_assessment.get("impact") or ""),
                    "recommended_action": action,
                }
            return {
                "summary": summary if isinstance(summary, str) else "",
                "key_results": key_results,
                "feature_ledger": key_results,
                "pitfalls": items(model_handoff.get("pitfalls")),
                "blockers": items(model_handoff.get("blockers")),
                "next_steps": items(
                    model_handoff.get("next_steps", model_handoff.get("next_action"))
                ),
                "verifier_assessment": verifier_assessment,
                "source_agent_session_id": session.agent_session_id,
            }
        return {
            "summary": "",
            "key_results": [],
            "feature_ledger": [],
            "pitfalls": [],
            "blockers": [],
            "next_steps": [],
            "verifier_assessment": {
                "status": "unknown",
                "evidence": [],
                "impact": "",
                "recommended_action": "keep_spec",
            },
            "source_agent_session_id": None,
        }

    def _run_research_rollup(
        self,
        records: list[CandidateRecord],
        spec: SearchSpec,
        *,
        visible_candidate_ids: list[str],
    ) -> dict[str, Any]:
        """Keep portable discoveries visible even when candidates leave the frontier."""

        visible = set(visible_candidate_ids)
        feature_groups: list[list[dict[str, Any]]] = []
        verifier_assessments: list[dict[str, Any]] = []
        pitfall_groups: list[list[dict[str, Any]]] = []

        for record in self._records_by_created(records):
            research = self._candidate_research_summary(
                record.task.run_id,
                record.candidate_id,
            )
            best_iteration = self._best_iteration_record(record, spec.metric_direction)
            score = self._record_ranking_score(record, spec)
            best_git_head = best_iteration.git_head if best_iteration else None

            candidate_features: list[dict[str, Any]] = []
            for result in research["feature_ledger"]:
                item = (
                    dict(result)
                    if isinstance(result, dict)
                    else {"conclusion": str(result)}
                )
                candidate_features.append(
                    {
                        **item,
                        "candidate_id": record.candidate_id,
                        "candidate_visible": record.candidate_id in visible,
                        "candidate_score": score,
                        "best_git_head": best_git_head,
                    }
                )
            if candidate_features:
                feature_groups.append(candidate_features)

            assessment = research["verifier_assessment"]
            evidence = assessment.get("evidence")
            status = assessment.get("status", "unknown")
            if status != "unknown" or evidence:
                verifier_assessments.append(
                    {
                        **assessment,
                        "candidate_id": record.candidate_id,
                    }
                )

            candidate_pitfalls: list[dict[str, Any]] = []
            for pitfall in research["pitfalls"]:
                item = (
                    dict(pitfall)
                    if isinstance(pitfall, dict)
                    else {"observed_result": str(pitfall)}
                )
                scope = item.get("scope")
                if scope not in {
                    "candidate_local",
                    "feature_family",
                    "evaluation_contract",
                }:
                    scope = "candidate_local"
                confidence = item.get("confidence")
                if confidence not in {"single_observation", "reproduced"}:
                    confidence = "single_observation"
                candidate_pitfalls.append(
                    {
                        **item,
                        "scope": scope,
                        "confidence": confidence,
                        "candidate_id": record.candidate_id,
                    }
                )
            if candidate_pitfalls:
                pitfall_groups.append(candidate_pitfalls)

        def round_robin(
            groups: list[list[dict[str, Any]]],
            limit: int,
        ) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        rows.append(group[index])
                        if len(rows) == limit:
                            return rows
            return rows

        return {
            "feature_ledger": round_robin(feature_groups, 50),
            "verifier_assessments": verifier_assessments[:15],
            "pitfalls": round_robin(pitfall_groups, 30),
            "description": (
                "当前 run 中所有候选的有界研究汇总，包括可见排名 frontier 之外候选的发现。"
            ),
        }

    def _build_inherited_research(
        self,
        source_run_id: str,
    ) -> dict[str, Any]:
        source_run = self._load_run(source_run_id)
        source_frozen = self._load_frozen_spec(source_run.frozen_spec_id)
        records = self._load_candidate_records(source_run_id)
        frontier_records = self._top_records(records, source_frozen.spec, 5)
        visible_candidate_ids = [record.candidate_id for record in frontier_records]
        rollup = self._run_research_rollup(
            records,
            source_frozen.spec,
            visible_candidate_ids=visible_candidate_ids,
        )

        inherited_features = list(
            source_run.inherited_research.get("feature_ledger", [])
        )
        inherited_pitfalls = list(source_run.inherited_research.get("pitfalls", []))
        current_features = [
            {
                **entry,
                "source_run_id": source_run_id,
                "score_reusable": False,
            }
            for entry in rollup["feature_ledger"]
        ]
        current_pitfalls = [
            {**entry, "source_run_id": source_run_id}
            for entry in rollup["pitfalls"]
        ]
        features = current_features + inherited_features
        pitfalls = current_pitfalls + inherited_pitfalls
        features = features[:50]
        pitfalls = pitfalls[:30]
        frontier: list[dict[str, Any]] = []
        for record in frontier_records:
            payload = self._history_candidate_payload(record, source_frozen.spec)
            frontier.append(
                {
                    "source_run_id": source_run_id,
                    "candidate_id": record.candidate_id,
                    "score": payload["score"],
                    "score_reusable": False,
                    "best_iteration": payload["best_iteration"],
                    "best_git_head": payload["best_git_head"],
                    "summary": payload["summary"],
                    "feature_ledger": payload["feature_ledger"],
                }
            )

        return {
            "source_run_id": source_run_id,
            "source_frozen_spec_id": source_run.frozen_spec_id,
            "source_invalidation": {
                "reason": source_run.invalidation_reason,
                "summary": source_run.invalidation_summary,
                "evidence": source_run.invalidation_evidence,
            },
            "frontier": frontier,
            "feature_ledger": features,
            "pitfalls": pitfalls,
            "score_reusable": False,
            "description": (
                "由 policy 控制的继承研究上下文。来源分数仅作历史记录，"
                "每个导入产物或特性都必须重新验证。"
            ),
        }

    def _history_candidate_payload(
        self,
        record: CandidateRecord,
        spec: SearchSpec,
    ) -> dict[str, Any]:
        score_report = record.score_report
        best_iteration = self._best_iteration_record(record, spec.metric_direction)
        evidence_score = (
            best_iteration.score
            if best_iteration is not None
            else score_report.aggregate_score if score_report else None
        )
        evidence_passed = (
            True
            if best_iteration is not None
            else score_report.process_passed if score_report else None
        )
        metrics: dict[str, Any] = {}
        verifier_summaries: list[dict[str, Any]] = []
        failure_classes: list[str] = []
        log_paths: list[str] = []
        latest_verifier_summaries: list[dict[str, Any]] = []
        latest_failure_classes: list[str] = []
        latest_log_paths: list[str] = []

        score_report_is_evidence = bool(
            score_report
            and score_report.process_passed
            and score_report.aggregate_score == evidence_score
        )
        if best_iteration is not None:
            log_paths = list(best_iteration.log_paths)
            for verifier_metrics in best_iteration.metrics.values():
                if isinstance(verifier_metrics, dict):
                    metrics.update(
                        {
                            key: value
                            for key, value in verifier_metrics.items()
                            if key not in metrics
                        }
                    )
        if score_report:
            for result in score_report.verifier_results:
                if result.failure_class:
                    latest_failure_classes.append(result.failure_class)
                if result.log_path:
                    latest_log_paths.append(str(result.log_path))
                latest_verifier_summaries.append(
                    {
                        "name": result.name,
                        "role": result.role,
                        "passed": result.passed,
                        "score": result.score,
                        "failure_class": result.failure_class,
                        "log_path": str(result.log_path) if result.log_path else None,
                    }
                )
            if score_report_is_evidence:
                if not metrics:
                    for result in score_report.verifier_results:
                        if result.metrics:
                            metrics = result.metrics
                            break
                failure_classes = list(latest_failure_classes)
                log_paths = list(latest_log_paths)
                verifier_summaries = list(latest_verifier_summaries)

        key_metrics = {
            key: value
            for key, value in metrics.items()
            if key
            not in {
                "returncode",
                "elapsed_seconds",
            }
            and isinstance(value, int | float | bool | str)
        }

        agent_sessions = self._agent_session_payloads_for_candidate(
            record.task.run_id,
            record.candidate_id,
        )
        research_summary = self._candidate_research_summary(
            record.task.run_id,
            record.candidate_id,
        )
        risk_notes = [
            (
                "Condition: {condition}; failed approach: {failed_approach}; "
                "reason: {reason}; recommendation: {recommendation}".format(
                    condition=pitfall.get("condition", "the recorded condition"),
                    failed_approach=pitfall.get("failed_approach", "the approach"),
                    reason=pitfall.get("reason", "the recorded reason"),
                    recommendation=pitfall.get("recommendation", "avoid repeating it"),
                )
                if isinstance(pitfall, dict)
                else str(pitfall)
            )
            for pitfall in research_summary["pitfalls"]
        ]

        return {
            "candidate_id": record.candidate_id,
            "parent_id": record.task.parent_id,
            "parent_candidate_ids": record.task.parent_candidate_ids,
            "base_candidate_id": record.task.base_candidate_id,
            "plan_id": record.task.plan_id,
            "status": record.status,
            "hypothesis": record.task.hypothesis,
            "intent": record.task.proposal.intent if record.task.proposal else record.task.hypothesis,
            "expected_tradeoff": (
                record.task.proposal.expected_tradeoff if record.task.proposal else ""
            ),
            "strategy_metadata": record.task.strategy_metadata,
            "workspace": str(record.task.workspace),
            "agent_sessions": agent_sessions,
            "summary": research_summary["summary"],
            "key_results": research_summary["key_results"],
            "feature_ledger": research_summary["feature_ledger"],
            "next_ideas": research_summary["next_steps"],
            "risk_notes": risk_notes,
            "blockers": research_summary["blockers"],
            "verifier_assessment": research_summary["verifier_assessment"],
            "research_summary": research_summary,
            "search_action": (
                record.task.proposal.metadata.get("search_action")
                if record.task.proposal
                else None
            ),
            "artifact_status": None,
            "changed_files": record.detected_changed_files,
            "touched_denied_files": record.touched_denied_files,
            "changed_outside_allowed": record.changed_outside_allowed,
            "process_passed": evidence_passed,
            "score": evidence_score,
            "metric_name": spec.metric_name,
            "evidence_source": "best_iteration" if best_iteration else "latest_score_report",
            "best_iteration": best_iteration.iteration if best_iteration else None,
            "best_git_head": best_iteration.git_head if best_iteration else None,
            "latest_process_passed": score_report.process_passed if score_report else None,
            "latest_score": score_report.aggregate_score if score_report else None,
            "latest_disposition": score_report.disposition if score_report else None,
            "workspace_git_head_after_settlement": (
                score_report.workspace_git_head_after_settlement
                if score_report
                else None
            ),
            "latest_failure_classes": latest_failure_classes,
            "latest_verifiers": latest_verifier_summaries,
            "latest_log_paths": latest_log_paths,
            "key_metrics": key_metrics,
            "failure_classes": failure_classes,
            "verifiers": verifier_summaries,
            "log_paths": log_paths,
        }

    def _agent_session_payloads_for_candidate(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "agent_session_id": session.agent_session_id,
                "candidate_id": session.candidate_id,
                "host": session.host,
                "host_handle": session.host_handle.model_dump(mode="json"),
                "host_handle_display": self._display_host_handle(session),
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "directive": session.directive,
                "verifier_runs": session.counters.get("verifier_runs", 0),
            }
            for session in self._load_agent_sessions(run_id)
            if session.candidate_id == candidate_id
        ]

    def _frozen_hash_failures(self, frozen: FrozenSpec, workspace: Path) -> dict[str, dict[str, str | None]]:
        failures: dict[str, dict[str, str | None]] = {}
        for rel_path, expected_hash in frozen.verifier_hashes.items():
            path = workspace / rel_path
            actual_hash = sha256_file(path) if path.exists() and path.is_file() else None
            if actual_hash != expected_hash:
                failures[rel_path] = {"expected": expected_hash, "actual": actual_hash}
        return failures

    def _detect_changed_files(self, source: Path, workspace: Path) -> list[str]:
        source_hashes = self._hash_tree(source, source_view=True)
        workspace_hashes = self._hash_tree(workspace)
        changed: list[str] = []
        for rel_path in sorted(set(source_hashes) | set(workspace_hashes)):
            if rel_path == RESULTS_TSV_RELATIVE_PATH:
                continue
            if source_hashes.get(rel_path) != workspace_hashes.get(rel_path):
                changed.append(rel_path)
        return changed

    def _candidate_artifact_state(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> _CandidateArtifactState:
        workspace = record.task.workspace
        changed_files = self._detect_changed_files(Path(run.source_path), workspace)
        touched_denied = any(
            path_matches(path, frozen.spec.edit_surface.deny)
            for path in changed_files
        )
        outside_allowed = any(
            not path_matches(path, frozen.spec.edit_surface.allow)
            for path in changed_files
        )
        max_changes = frozen.spec.edit_surface.max_file_changes
        if max_changes is not None and len(changed_files) > max_changes:
            outside_allowed = True
        git_head = self._git_head(workspace)
        return _CandidateArtifactState(
            changed_files=changed_files,
            touched_denied_files=touched_denied,
            changed_outside_allowed=outside_allowed,
            artifact_hash=self._artifact_hash(workspace, changed_files),
            git_head=git_head,
            git_status=self._git_status(workspace),
            git_artifact_clean=self._git_artifact_clean(
                workspace,
                git_head,
            ),
        )

    @staticmethod
    def _apply_candidate_artifact_state(
        record: CandidateRecord,
        state: _CandidateArtifactState,
    ) -> None:
        record.detected_changed_files = list(state.changed_files)
        record.touched_denied_files = state.touched_denied_files
        record.changed_outside_allowed = state.changed_outside_allowed

    def _artifact_hash(self, workspace: Path, changed_files: list[str]) -> str:
        payload: dict[str, str | None] = {}
        for rel_path in sorted(changed_files):
            path = workspace / rel_path
            payload[rel_path] = sha256_file(path) if path.is_file() else None
        return sha256_text(canonical_json(payload))

    def _git_head(self, workspace: Path) -> str | None:
        try:
            value = self._git_output(
                workspace, ["git", "rev-parse", "--verify", "HEAD"]
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        return value.strip() or None

    def _git_status(
        self,
        workspace: Path,
        *,
        ignore_runtime_noise: bool = False,
    ) -> list[str]:
        command = ["git", "status", "--porcelain=v1", "--untracked-files=all"]
        if ignore_runtime_noise:
            exclusions = [
                pattern
                for name in sorted(IGNORED_NAMES - {".git"})
                for pattern in (
                    f":(exclude){name}/**",
                    f":(exclude)**/{name}/**",
                )
            ]
            exclusions.extend(
                f":(exclude)**/*{suffix}" for suffix in sorted(IGNORED_SUFFIXES)
            )
            command.extend(["--", ".", *exclusions])
        try:
            value = self._git_output(workspace, command)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return []
        return [line for line in value.splitlines() if line.strip()]

    def _git_artifact_clean(
        self,
        workspace: Path,
        git_head: str | None,
    ) -> bool:
        if not git_head:
            return False
        return not self._git_status(workspace, ignore_runtime_noise=True)

    @staticmethod
    def _candidate_git_pathspecs() -> list[str]:
        exclusions = [
            f":(exclude){name}/**"
            for name in sorted(IGNORED_NAMES - {".git"})
        ]
        exclusions.extend(
            f":(exclude)**/{name}/**"
            for name in sorted(IGNORED_NAMES - {".git"})
        )
        exclusions.extend(
            f":(exclude)**/*{suffix}" for suffix in sorted(IGNORED_SUFFIXES)
        )
        return [".", f":(exclude){RESULTS_TSV_RELATIVE_PATH}", *exclusions]

    def _git_changed_files(
        self,
        workspace: Path,
        base: str,
        head: str,
    ) -> list[str]:
        value = self._git_output(
            workspace,
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                base,
                head,
                "--",
                *self._candidate_git_pathspecs(),
            ],
        )
        return sorted(path for path in value.split("\0") if path)

    def _git_output(self, workspace: Path, command: list[str]) -> str:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                command,
                output=stdout,
                stderr=stderr,
            )
        return stdout

    def _git_output_bounded(
        self,
        workspace: Path,
        command: list[str],
        *,
        max_bytes: int,
    ) -> str:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout = _BoundedOutput(max_bytes + 1)
        stderr = _BoundedOutput(VERIFIER_OUTPUT_LIMIT_BYTES)

        def drain(stream: Any, capture: _BoundedOutput) -> None:
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    capture.append(chunk)
            except (OSError, ValueError):
                pass

        readers = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        returncode = process.wait()
        for reader in readers:
            reader.join(timeout=VERIFIER_TERM_GRACE_SECONDS)
        if any(reader.is_alive() for reader in readers):
            self._terminate_verifier_process_group(process)
            for reader in readers:
                reader.join(timeout=VERIFIER_TERM_GRACE_SECONDS)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if returncode:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                output=stdout.text(),
                stderr=stderr.text(),
            )
        if stdout.truncated or len(stdout.data) > max_bytes:
            raise RuntimeError(
                f"annotation diff exceeds {max_bytes} bytes"
            )
        return bytes(stdout.data).decode("utf-8", errors="replace")

    def _git_returncode(self, workspace: Path, command: list[str]) -> int:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.communicate()
        return process.returncode

    def _initialize_workspace_git_for_results(self, workspace: Path) -> None:
        if self._git_head(workspace) is not None:
            return
        files = [
            path.relative_to(workspace).as_posix()
            for path in list_files(workspace)
        ]
        try:
            self._git_output(workspace, ["git", "init", "-q"])
            self._git_output(workspace, ["git", "add", "-f", "--", *files])
            self._git_output(
                workspace,
                [
                    "git",
                    "-c",
                    "user.name=goal-plus",
                    "-c",
                    "user.email=goal-plus@example.invalid",
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    "search candidate baseline",
                ],
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "ResultsLedgerCommitError: candidate workspace has no usable "
                "Git repository for workspace/results.tsv"
            ) from exc

    def _commit_results_tsv(self, workspace: Path, message: str) -> str | None:
        try:
            self._git_output(
                workspace,
                ["git", "add", "--", RESULTS_TSV_RELATIVE_PATH],
            )
            staged_returncode = self._git_returncode(
                workspace,
                [
                    "git",
                    "diff",
                    "--cached",
                    "--quiet",
                    "--",
                    RESULTS_TSV_RELATIVE_PATH,
                ],
            )
            if staged_returncode == 0:
                return self._git_head(workspace)
            if staged_returncode != 1:
                return None
            self._git_output(
                workspace,
                [
                    "git",
                    "-c",
                    "user.name=goal-plus",
                    "-c",
                    "user.email=goal-plus@example.invalid",
                    "commit",
                    "-q",
                    "--no-verify",
                    "--only",
                    "-m",
                    message,
                    "--",
                    RESULTS_TSV_RELATIVE_PATH,
                ],
            )
            return self._git_head(workspace)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    def _commit_workspace_iteration(
        self,
        workspace: Path,
        message: str,
    ) -> str | None:
        pathspecs = self._candidate_git_pathspecs()
        try:
            self._git_output(
                workspace, ["git", "add", "-A", "-f", "--", *pathspecs]
            )
            staged_returncode = self._git_returncode(
                workspace,
                ["git", "diff", "--cached", "--quiet", "--", *pathspecs],
            )
            if staged_returncode == 0:
                return self._git_head(workspace)
            if staged_returncode != 1:
                return None
            self._git_output(
                workspace,
                [
                    "git",
                    "-c",
                    "user.name=goal-plus",
                    "-c",
                    "user.email=goal-plus@example.invalid",
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    message,
                ],
            )
            return self._git_head(workspace)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    def _restore_candidate_artifact(
        self,
        record: CandidateRecord,
        metric_name: str,
        revision: str,
        message: str,
    ) -> str:
        workspace = record.task.workspace
        self._assert_results_tsv_unchanged(record, metric_name)
        ledger_text = self._render_results_tsv(record.results_ledger, metric_name)
        try:
            self._git_output(
                workspace,
                [
                    "git",
                    "restore",
                    f"--source={revision}",
                    "--staged",
                    "--worktree",
                    "--",
                    ".",
                ],
            )
            write_text(self._results_tsv_path(workspace), ledger_text)
            self._git_output(
                workspace,
                ["git", "add", "--", RESULTS_TSV_RELATIVE_PATH],
            )
            staged_returncode = self._git_returncode(
                workspace,
                ["git", "diff", "--cached", "--quiet"],
            )
            if staged_returncode == 1:
                self._git_output(
                    workspace,
                    [
                        "git",
                        "-c",
                        "user.name=goal-plus",
                        "-c",
                        "user.email=goal-plus@example.invalid",
                        "commit",
                        "-q",
                        "--no-verify",
                        "-m",
                        message,
                    ],
                )
            elif staged_returncode != 0:
                raise RuntimeError("could not inspect staged restoration")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"failed to restore candidate artifact from {revision}"
            ) from exc
        self._assert_results_tsv_unchanged(record, metric_name)
        git_head = self._git_head(workspace)
        if git_head is None:
            raise RuntimeError("candidate restoration produced no Git HEAD")
        return git_head

    def _hash_tree(
        self,
        root: Path,
        *,
        source_view: bool = False,
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        paths = list_source_files(root) if source_view else list_files(root)
        for path in paths:
            rel_path = path.relative_to(root).as_posix()
            hashes[rel_path] = sha256_file(path)
        return hashes

    def _write_patch(
        self,
        source: Path,
        workspace: Path,
        selected_revision: str,
        changed_files: list[str],
        patch_path: Path,
    ) -> None:
        if not changed_files:
            patch_path.write_text("", encoding="utf-8")
            return

        with tempfile.TemporaryDirectory(prefix="goal-plus-patch-") as temporary:
            repository = Path(temporary) / "repository"
            copy_source_tree(source, repository)
            baseline = initialize_workspace_git_baseline(repository)
            if baseline is None:
                raise RuntimeError("cannot initialize temporary promotion repository")
            for rel_path in changed_files:
                staged = repository / rel_path
                if staged.exists() or staged.is_symlink():
                    if staged.is_dir() and not staged.is_symlink():
                        shutil.rmtree(staged)
                    else:
                        staged.unlink()
                entry = self._git_tree_entry(
                    workspace,
                    selected_revision,
                    rel_path,
                )
                if entry is None:
                    continue
                mode, object_type, object_id = entry
                if object_type == "tree":
                    staged.mkdir(parents=True, exist_ok=True)
                    continue
                if object_type != "blob":
                    raise RuntimeError(
                        "selected promotion revision contains unsupported Git "
                        f"object type {object_type!r} at {rel_path}"
                    )
                content = self._git_blob(workspace, object_id)
                staged.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    staged.symlink_to(os.fsdecode(content))
                else:
                    staged.write_bytes(content)
                    staged.chmod(int(mode, 8) & 0o777)
            self._git_output(
                repository,
                ["git", "add", "-A", "--", *changed_files],
            )
            patch = self._git_diff(
                repository,
                baseline,
                changed_files,
                cached=True,
            )
        patch_path.write_text(patch, encoding="utf-8")

    def _git_tree_entry(
        self,
        repository: Path,
        revision: str,
        rel_path: str,
    ) -> tuple[str, str, str] | None:
        command = [
            "git",
            "--no-replace-objects",
            "ls-tree",
            "-z",
            revision,
            "--",
            f":(literal){rel_path}",
        ]
        try:
            process = subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise RuntimeError(
                "failed to read immutable promotion revision: "
                f"{str(detail).strip()}"
            ) from exc
        if not process.stdout:
            return None
        entries = [entry for entry in process.stdout.split(b"\0") if entry]
        if len(entries) != 1:
            raise RuntimeError(
                f"selected promotion revision has ambiguous path {rel_path!r}"
            )
        metadata, _path = entries[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        return mode, object_type, object_id

    def _git_blob(self, repository: Path, object_id: str) -> bytes:
        command = ["git", "--no-replace-objects", "cat-file", "blob", object_id]
        try:
            return subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"failed to read immutable promotion blob: {str(detail).strip()}"
            ) from exc

    def _git_diff(
        self,
        repository: Path,
        baseline: str,
        changed_files: list[str],
        *,
        cached: bool,
    ) -> str:
        command = [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
        ]
        if cached:
            command.append("--cached")
        command.extend([baseline, "--", *changed_files])
        try:
            return subprocess.run(
                command,
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RuntimeError(
                f"failed to generate promotion patch: {detail.strip()}"
            ) from exc

    def _spec_dir(self, frozen_spec_id: str) -> Path:
        return self.specs_dir / frozen_spec_id

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    @contextmanager
    def _run_transaction(self, run_id: str):
        with exclusive_file_lock(self._run_dir(run_id) / "run.lock"):
            yield

    def _candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        return self._run_dir(run_id) / "candidates" / candidate_id

    def _evidence_annotation_task_path(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> Path:
        return (
            self._candidate_dir(run_id, candidate_id)
            / "evidence-annotations"
            / f"iteration-{iteration:04d}.json"
        )

    def _load_evidence_annotation_task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None:
        path = self._evidence_annotation_task_path(
            run_id, candidate_id, iteration
        )
        if not path.exists():
            return None
        return EvidenceAnnotationTask.model_validate(load_json(path))

    def _write_evidence_annotation_task(
        self,
        task: EvidenceAnnotationTask,
    ) -> None:
        write_json(
            self._evidence_annotation_task_path(
                task.run_id, task.candidate_id, task.iteration
            ),
            task.model_dump(mode="json"),
        )

    def evidence_annotation_usage(self, run_id: str) -> dict[str, Any]:
        """Return persisted annotator usage without exposing annotation tasks."""
        self._load_run(run_id)
        tasks = [
            EvidenceAnnotationTask.model_validate(load_json(path))
            for path in sorted(
                self._run_dir(run_id).glob(
                    "candidates/*/evidence-annotations/iteration-*.json"
                )
            )
        ]
        usage: dict[str, int | float] = {}
        for task in tasks:
            for key, value in task.usage.items():
                usage[key] = usage.get(key, 0) + value
        states: dict[str, int] = {}
        for task in tasks:
            states[task.state] = states.get(task.state, 0) + 1
        return {
            **usage,
            "tasks": len(tasks),
            "attempts": sum(task.attempts for task in tasks),
            "states": states,
            "coverage": "persisted host-native Evidence annotator turn usage",
        }

    @staticmethod
    def _outer_deadline_epoch(value: str | None) -> float | None:
        if not value:
            return None
        try:
            numeric = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        if not math.isfinite(numeric):
            return None
        if numeric > 10_000_000_000:
            numeric /= 1000
        return numeric

    def _resolve_evidence_annotator_profile(
        self,
        frozen: FrozenSpec,
        *,
        selected_model: str | None = None,
    ) -> tuple[ResolvedEvidenceAnnotatorProfile | None, str | None]:
        strategy = frozen.spec.strategy
        configured = strategy.evidence_annotator
        annotation_host = configured.host or strategy.worker_host
        worker_launch = strategy.worker_launch
        env_model = os.environ.get(EVIDENCE_ANNOTATOR_MODEL_ENV)
        if configured.model:
            model = configured.model
        elif annotation_host == strategy.worker_host and selected_model:
            model = selected_model
        elif annotation_host == strategy.worker_host and worker_launch is not None:
            model = worker_launch.model
        elif env_model:
            model = env_model.strip() or None
        elif annotation_host == "pi-rpc":
            model = (os.environ.get("PI_MODEL") or "").strip() or None
        else:
            model = None

        reasoning_effort = configured.reasoning_effort
        if (
            reasoning_effort is None
            and annotation_host == strategy.worker_host
            and worker_launch is not None
        ):
            reasoning_effort = worker_launch.reasoning_effort
        if reasoning_effort is None:
            reasoning_effort = (
                os.environ.get(EVIDENCE_ANNOTATOR_REASONING_ENV) or None
            )

        pi_provider: str | None = None
        if annotation_host == "pi-rpc":
            inherited_pi_provider = os.environ.get("PI_PROVIDER")
            if inherited_pi_provider is not None:
                inherited_pi_provider = inherited_pi_provider.strip() or None
            configured_pi_provider = configured.pi_provider
            pi_provider = configured_pi_provider or inherited_pi_provider
            if model and "/" in model:
                model_provider, _, model_id = model.partition("/")
                if not model_provider or not model_id:
                    return None, f"invalid Pi annotation model reference {model!r}"
                if (
                    configured_pi_provider is not None
                    and configured_pi_provider != model_provider
                ):
                    return None, (
                        "Pi annotation provider conflicts with its model reference: "
                        f"{configured_pi_provider!r} != {model_provider!r}"
                    )
                pi_provider = model_provider

        provider: ResolvedCodexProvider | None = None
        if annotation_host == "codex" and configured.pi_provider is not None:
            return None, (
                "evidence_annotator.pi_provider configures Pi only; Codex "
                "annotation uses evidence_annotator.provider"
            )
        if annotation_host == "pi-rpc" and configured.provider is not None:
            return None, (
                "evidence_annotator.provider configures Codex only; Pi annotation "
                "uses the provider/model configuration under PI_CODING_AGENT_DIR"
            )
        if annotation_host == "codex" and configured.provider is not None:
            provider = ResolvedCodexProvider(
                provider_id=configured.provider.provider_id,
                name=configured.provider.name,
                base_url=configured.provider.base_url,
                api_key_env=configured.provider.api_key_env,
                wire_api=configured.provider.wire_api,
            )
        elif annotation_host == "codex":
            base_url = os.environ.get(EVIDENCE_ANNOTATOR_BASE_URL_ENV)
            if base_url:
                provider = ResolvedCodexProvider(
                    provider_id=(
                        os.environ.get(EVIDENCE_ANNOTATOR_PROVIDER_ID_ENV)
                        or "goal-plus-evidence"
                    ),
                    name=(
                        os.environ.get(EVIDENCE_ANNOTATOR_PROVIDER_NAME_ENV)
                        or "Goal Plus Evidence provider"
                    ),
                    base_url_env=EVIDENCE_ANNOTATOR_BASE_URL_ENV,
                    base_url_sha256=sha256_text(base_url),
                    api_key_env=(
                        os.environ.get(EVIDENCE_ANNOTATOR_API_KEY_ENV)
                        or "OPENAI_API_KEY"
                    ),
                    wire_api=(
                        os.environ.get(EVIDENCE_ANNOTATOR_WIRE_API_ENV)
                        or "responses"
                    ),
                )

        codex_home = (
            os.environ.get("CODEX_HOME")
            if annotation_host == "codex"
            else None
        )
        if codex_home:
            codex_home = str(Path(codex_home).expanduser().resolve())
        pi_home = (
            os.environ.get("PI_CODING_AGENT_DIR")
            if annotation_host == "pi-rpc"
            else None
        )
        if pi_home:
            pi_home = str(Path(pi_home).expanduser().resolve())
        return (
            ResolvedEvidenceAnnotatorProfile(
                host=annotation_host,
                model=model,
                pi_provider=pi_provider,
                reasoning_effort=reasoning_effort,
                timeout_seconds=configured.timeout_seconds,
                codex_home=codex_home,
                pi_home=pi_home,
                provider=provider,
            ),
            None,
        )

    def _create_evidence_annotation_task(
        self,
        run_id: str,
        frozen: FrozenSpec,
        candidate_id: str,
        iteration: IterationRecord,
    ) -> EvidenceAnnotationTask:
        if (
            iteration.git_head is None
            or iteration.attempt_base_git_head is None
        ):
            raise RuntimeError("worker Evidence requires exact attempt commits")
        existing = self._load_evidence_annotation_task(
            run_id, candidate_id, iteration.iteration
        )
        if existing is not None:
            if (
                existing.attempt_commit != iteration.git_head
                or existing.attempt_base_commit
                != iteration.attempt_base_git_head
            ):
                raise RuntimeError("Evidence annotation task is immutable")
            return existing

        try:
            profile, error = self._resolve_evidence_annotator_profile(
                frozen,
                selected_model=iteration.selected_model,
            )
        except Exception as exc:
            profile = None
            error = f"invalid annotator profile: {type(exc).__name__}: {exc}"
        outer_deadline = os.environ.get(OUTER_DEADLINE_ENV) or None
        if outer_deadline:
            deadline_epoch = self._outer_deadline_epoch(outer_deadline)
            if deadline_epoch is None:
                error = f"invalid {OUTER_DEADLINE_ENV} value"
                profile = None
            elif deadline_epoch <= time.time():
                error = "annotation outer deadline already expired"
                profile = None
        now = utc_timestamp()
        supplemental_enabled = supplemental_evaluation_enabled()
        if supplemental_evaluation_required() and not supplemental_enabled:
            error = (
                f"{SUPPLEMENTAL_EVALUATION_REQUIRED_ENV}=1 requires "
                f"{SUPPLEMENTAL_EVALUATION_ENABLED_ENV}=1"
            )
            profile = None
        task_context, task_context_source, task_context_ref = (
            self._evidence_task_context(
                run_id,
                fallback=frozen.spec.objective,
            )
        )
        task = EvidenceAnnotationTask(
            run_id=run_id,
            candidate_id=candidate_id,
            iteration=iteration.iteration,
            attempt_base_commit=iteration.attempt_base_git_head,
            attempt_commit=iteration.git_head,
            attempt_changed_files=list(iteration.attempt_changed_files),
            task_context_source=task_context_source,
            task_context_ref=task_context_ref,
            task_context_sha256=sha256_text(task_context),
            supplemental_evaluation_enabled=supplemental_enabled,
            comparison_basis=(
                self._evidence_comparison_basis(
                    run_id,
                    target_candidate_id=candidate_id,
                )
                if supplemental_enabled
                else []
            ),
            profile=profile,
            outer_deadline_at=outer_deadline,
            state="terminal_error" if error else "pending",
            error_fingerprint=sha256_text(error) if error else None,
            last_error=error,
            created_at=now,
            updated_at=now,
        )
        self._write_evidence_annotation_task(task)
        return task

    @staticmethod
    def _global_evidence_entry(
        candidate_id: str,
        iteration: IterationRecord,
        view: EvidenceViewRecord | None,
    ) -> dict[str, Any]:
        tool_views = {
            item.tool_id: item
            for item in (view.tool_views if view is not None else [])
        }
        entry = {
            "candidate_id": candidate_id,
            "iteration": iteration.iteration,
            "commit": iteration.git_head,
            "score": iteration.score,
            "disposition": iteration.disposition,
            "view": view.description if view is not None else None,
            "view_created_at": view.created_at if view is not None else None,
            "shared_tools": [
                {
                    **tool.model_dump(mode="json", exclude={"read_only_path"}),
                    "tool_view": tool_views[tool.tool_id].model_dump(mode="json"),
                }
                for tool in iteration.shared_tools
                if tool.tool_id in tool_views
            ],
        }
        if view is not None and view.supplemental_evaluation is not None:
            entry["supplemental_available"] = True
        return entry

    def _global_evidence_view(self, run_id: str) -> list[dict[str, Any]]:
        evidence = [
            (iteration.created_at, record.candidate_id, iteration)
            for record in self._load_candidate_records(run_id)
            for iteration in record.iterations
            if iteration.agent_session_id is not None
        ]
        evidence.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2].iteration,
            )
        )
        result = []
        for _, candidate_id, iteration in evidence:
            task = self._load_evidence_annotation_task(
                run_id, candidate_id, iteration.iteration
            )
            view = (
                task.view
                if task is not None and task.state == "completed"
                else None
            )
            if view is not None and (
                view.run_id != run_id
                or view.candidate_id != candidate_id
                or view.iteration != iteration.iteration
                or view.attempt_commit != iteration.git_head
            ):
                raise RuntimeError("evidence view does not match iteration")
            result.append(
                self._global_evidence_entry(candidate_id, iteration, view)
            )
        return result

    @staticmethod
    def attach_external_evaluations(
        run_id: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        directory_value = os.environ.get(EXTERNAL_EVIDENCE_DIR_ENV)
        if not directory_value:
            return
        try:
            paths = sorted(
                path
                for path in Path(directory_value).glob("*.json")
                if not path.name.startswith(".") and path.is_file()
            )
        except OSError:
            return
        for path in paths:
            try:
                if path.stat().st_size > MAX_EXTERNAL_EVIDENCE_BYTES:
                    continue
                payload = load_json(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            artifact = payload.get("artifact")
            evaluation = payload.get("evaluation")
            if (
                not isinstance(artifact, dict)
                or artifact.get("source") != "goal_plus_best"
                or artifact.get("run_id") != run_id
                or not isinstance(evaluation, dict)
            ):
                continue
            for entry in evidence:
                if (
                    entry.get("candidate_id") == artifact.get("candidate_id")
                    and entry.get("iteration") == artifact.get("iteration")
                    and (entry.get("commit") or entry.get("git_head"))
                    == artifact.get("commit")
                ):
                    external = dict(evaluation)
                    source = payload.get("source")
                    external["source"] = (
                        source if isinstance(source, str) else "external"
                    )
                    entry.setdefault("external_evaluations", []).append(external)
                    break
        for entry in evidence:
            evaluations = entry.get("external_evaluations")
            if isinstance(evaluations, list):
                evaluations.sort(
                    key=lambda item: (
                        str(item.get("published_at") or ""),
                        str(item.get("round_id") or ""),
                    )
                )

    def _pending_evidence_annotations(
        self,
        run_id: str,
    ) -> list[tuple[str, int]]:
        return [
            (entry["candidate_id"], entry["iteration"])
            for entry in self._global_evidence_view(run_id)
            if entry["view"] is None
        ]

    def _evidence_annotation_run_active(self, run_id: str) -> bool:
        run = self._load_run(run_id)
        return (
            run.invalidated_at is None
            and run.state in EVIDENCE_ANNOTATION_RUN_STATES
        )

    def _eligible_evidence_annotations(
        self,
        run_id: str,
        *,
        now_epoch: float | None = None,
    ) -> list[tuple[str, int]]:
        if not self._evidence_annotation_run_active(run_id):
            return []
        now_epoch = time.time() if now_epoch is None else now_epoch
        eligible = []
        for candidate_id, iteration in self._pending_evidence_annotations(run_id):
            task = self._load_evidence_annotation_task(
                run_id, candidate_id, iteration
            )
            if task is None or task.state not in {"pending", "retry_wait"}:
                continue
            deadline = self._outer_deadline_epoch(task.outer_deadline_at)
            retry_at = self._outer_deadline_epoch(task.next_attempt_at)
            if deadline is not None and deadline <= now_epoch:
                continue
            if retry_at is not None and retry_at > now_epoch:
                continue
            eligible.append((candidate_id, iteration))
        return eligible

    def _evidence_annotation_context(
        self,
        run_id: str,
        candidate_id: str,
        iteration_number: int,
    ) -> dict[str, Any]:
        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        record = self._load_candidate_record(run_id, candidate_id)
        iteration = next(
            (
                item
                for item in record.iterations
                if item.iteration == iteration_number
                and item.agent_session_id is not None
            ),
            None,
        )
        if iteration is None or iteration.git_head is None:
            raise RuntimeError("annotation requires commit-backed worker evidence")
        task = self._load_evidence_annotation_task(
            run_id, candidate_id, iteration_number
        )
        if task is None or task.profile is None:
            raise RuntimeError("annotation requires a runnable immutable task")
        commit = iteration.git_head
        if (
            task.attempt_commit != commit
            or task.attempt_base_commit != iteration.attempt_base_git_head
            or task.attempt_changed_files != iteration.attempt_changed_files
        ):
            raise RuntimeError("annotation task does not match settled Evidence")
        if self._git_returncode(
            record.task.workspace,
            ["git", "cat-file", "-e", f"{task.attempt_base_commit}^{{commit}}"],
        ) != 0 or self._git_returncode(
            record.task.workspace,
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        ) != 0:
            raise RuntimeError("annotation Evidence commit is unavailable")
        diff = ""
        if task.attempt_changed_files:
            diff = self._git_output_bounded(
                record.task.workspace,
                [
                    "git",
                    "diff",
                    "--full-index",
                    "--no-ext-diff",
                    "--function-context",
                    "--unified=10",
                    task.attempt_base_commit,
                    commit,
                    "--",
                    *task.attempt_changed_files,
                ],
                max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
            )
        candidate_base_commit = (
            record.task.workspace_base_revision or task.attempt_base_commit
        )
        candidate_changed_files = self._git_changed_files(
            record.task.workspace,
            candidate_base_commit,
            commit,
        )
        candidate_diff = ""
        if candidate_changed_files:
            candidate_diff = self._git_output_bounded(
                record.task.workspace,
                [
                    "git",
                    "diff",
                    "--full-index",
                    "--no-ext-diff",
                    "--function-context",
                    "--unified=10",
                    candidate_base_commit,
                    commit,
                    "--",
                    *candidate_changed_files,
                ],
                max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
            )
        peer_evidence = self._evidence_comparison_peers(
            run_id,
            comparison_basis=task.comparison_basis,
        )
        task_context = frozen.spec.objective
        task_context_source = "frozen_objective"
        if task.task_context_source is not None:
            resolved_context, resolved_source, resolved_ref = (
                self._evidence_task_context(
                    run_id,
                    fallback=frozen.spec.objective,
                )
            )
            if (
                resolved_source != task.task_context_source
                or resolved_ref != task.task_context_ref
                or sha256_text(resolved_context) != task.task_context_sha256
            ):
                raise RuntimeError(
                    "annotation task context no longer matches its snapshot"
                )
            task_context = resolved_context
            task_context_source = resolved_source
        published_tools = []
        remaining_tool_bytes = TOOL_VIEW_MAX_CONTENT_BYTES
        if iteration.shared_tools:
            manager = SharedDirManager(self._run_dir(run_id))
            prior = next(
                (
                    item
                    for item in reversed(record.iterations)
                    if item.iteration < iteration.iteration and item.score is not None
                ),
                None,
            )
            for tool in iteration.shared_tools:
                tool_input, used = manager.tool_view_input(
                    tool, max_content_bytes=remaining_tool_bytes
                )
                tool_input["goal_evidence"] = {
                    "score": iteration.score,
                    "baseline_score": prior.score if prior is not None else None,
                    "goal_delta": (
                        iteration.score - prior.score
                        if iteration.score is not None and prior is not None
                        else None
                    ),
                    "goal_effect": (
                        "unknown" if prior is None
                        else "improved" if iteration.disposition == "keep"
                        else "unchanged" if iteration.disposition == "retain"
                        else "degraded" if iteration.disposition == "discard"
                        else "failed"
                    ),
                    "disposition": iteration.disposition,
                }
                published_tools.append(tool_input)
                remaining_tool_bytes = max(0, remaining_tool_bytes - used)
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "iteration": iteration.iteration,
            "agent_summary": iteration.hypothesis,
            "exact_attempt_commit": commit,
            "changed_files": list(task.attempt_changed_files),
            "actual_diff": diff,
            "candidate_base_commit": candidate_base_commit,
            "candidate_changed_files": candidate_changed_files,
            "candidate_diff": candidate_diff,
            "diff_context_policy": (
                "git function context with at least 10 unchanged lines around hunks; "
                "output remains byte-bounded and may omit definitions outside the diff"
            ),
            "verifier_result": {
                "score": iteration.score,
                "process_passed": iteration.process_passed,
                "disposition": iteration.disposition,
                "failure_class": iteration.failure_class,
            },
            "relevant_metrics": iteration.metrics,
            "verifier_contract": [
                {
                    "name": command.name,
                    "role": str(command.role),
                    "command": list(command.command),
                    "cwd": command.cwd,
                    "timeout_seconds": command.timeout_seconds,
                }
                for command in frozen.spec.process_verifiers
            ],
            "objective": frozen.spec.objective,
            "task_context": task_context,
            "task_context_source": task_context_source,
            "supplemental_evaluation_enabled": (
                task.supplemental_evaluation_enabled
            ),
            "peer_evidence": peer_evidence,
            "comparison_basis": [
                item.model_dump(mode="json") for item in task.comparison_basis
            ],
            "published_tools": published_tools,
            "tool_adoptions": [
                {
                    **item.model_dump(mode="json"),
                    "disposition": iteration.disposition,
                    "confounded": iteration.adoption_confounded,
                }
                for item in iteration.adopted_tools
            ],
            "annotator": task.profile.model_dump(mode="json"),
            "outer_deadline_at": task.outer_deadline_at,
            "runtime_root": str(self.root_dir),
        }

    def _evidence_task_context(
        self,
        run_id: str,
        *,
        fallback: str,
    ) -> tuple[
        str,
        Literal["goal_plus_raw_goal", "frozen_objective"],
        str,
    ]:
        matches: list[tuple[str, str]] = []
        for path in sorted((self.root_dir / "goal-plus").glob("*/goal.json")):
            try:
                payload = load_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            search_tasks = payload.get("search_tasks")
            tasks = search_tasks if isinstance(search_tasks, list) else []
            linked = payload.get("linked_search")
            if isinstance(linked, dict):
                tasks = [*tasks, linked]
            revision = next(
                (
                    item.get("goal_revision")
                    for item in reversed(tasks)
                    if isinstance(item, dict) and item.get("run_id") == run_id
                ),
                None,
            )
            if not isinstance(revision, int):
                continue
            revisions = payload.get("goal_revisions")
            raw_goal = None
            if isinstance(revisions, list):
                raw_goal = next(
                    (
                        item.get("raw_goal")
                        for item in revisions
                        if isinstance(item, dict)
                        and item.get("revision") == revision
                    ),
                    None,
                )
            if not isinstance(raw_goal, str) or not raw_goal.strip():
                if payload.get("goal_revision") == revision:
                    raw_goal = payload.get("raw_goal")
            if isinstance(raw_goal, str) and raw_goal.strip():
                goal_plus_id = str(payload.get("goal_plus_id") or path.parent.name)
                matches.append(
                    (raw_goal, f"goal_plus:{goal_plus_id}:revision:{revision}")
                )
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            raw_goal, reference = unique[0]
            return raw_goal, "goal_plus_raw_goal", reference
        return fallback, "frozen_objective", f"frozen_objective:{run_id}"

    def _evidence_comparison_basis(
        self,
        run_id: str,
        *,
        target_candidate_id: str,
    ) -> list[dict[str, Any]]:
        run = self._load_run(run_id)
        frozen = self._load_frozen_spec(run.frozen_spec_id)
        reverse = frozen.spec.metric_direction == "maximize"
        settled = []
        for record in self._load_candidate_records(run_id):
            if record.candidate_id == target_candidate_id:
                continue
            eligible = [
                iteration
                for iteration in record.iterations
                if iteration.agent_session_id is not None
                and self._git_iteration_eligible(iteration)
                and iteration.disposition in {None, "keep"}
            ]
            if not eligible:
                continue
            best = sorted(
                eligible,
                key=lambda iteration: iteration.score,
                reverse=reverse,
            )[0]
            settled.append((best.created_at, record, best))
        settled.sort(
            key=lambda item: (
                item[0],
                item[1].candidate_id,
                item[2].iteration,
            )
        )
        return [
            {
                "candidate_id": record.candidate_id,
                "iteration": iteration.iteration,
                "commit": iteration.git_head,
            }
            for _, record, iteration in settled[-MAX_EVIDENCE_COMPARISON_PEERS:]
        ]

    def _evidence_comparison_peers(
        self,
        run_id: str,
        *,
        comparison_basis: list[Any],
    ) -> list[dict[str, Any]]:
        peers: list[dict[str, Any]] = []
        records = {
            record.candidate_id: record
            for record in self._load_candidate_records(run_id)
        }
        for reference in comparison_basis:
            record = records.get(reference.candidate_id)
            if record is None:
                raise RuntimeError("annotation comparison candidate is unavailable")
            iteration = next(
                (
                    item
                    for item in record.iterations
                    if item.iteration == reference.iteration
                    and item.git_head == reference.commit
                ),
                None,
            )
            if iteration is None or iteration.git_head is None:
                raise RuntimeError("annotation comparison Evidence is unavailable")
            assert iteration.git_head is not None
            base_commit = (
                record.task.workspace_base_revision
                or iteration.attempt_base_git_head
            )
            changed_files: list[str] = []
            peer_diff: str | None = None
            diff_omitted: str | None = None
            if base_commit:
                try:
                    changed_files = self._git_changed_files(
                        record.task.workspace,
                        base_commit,
                        iteration.git_head,
                    )
                    if changed_files:
                        peer_diff = self._git_output_bounded(
                            record.task.workspace,
                            [
                                "git",
                                "diff",
                                "--full-index",
                                "--no-ext-diff",
                                base_commit,
                                iteration.git_head,
                                "--",
                                *changed_files,
                            ],
                            max_bytes=MAX_EVIDENCE_PEER_DIFF_BYTES,
                        )
                except (RuntimeError, subprocess.CalledProcessError) as exc:
                    diff_omitted = f"{type(exc).__name__}: {exc}"[:500]
                    peer_diff = None
            peers.append(
                {
                    "candidate_id": record.candidate_id,
                    "iteration": iteration.iteration,
                    "commit": iteration.git_head,
                    "score": iteration.score,
                    "process_passed": iteration.process_passed,
                    "disposition": iteration.disposition,
                    "agent_summary": iteration.hypothesis,
                    "changed_files": changed_files,
                    "candidate_diff": peer_diff,
                    "diff_omitted": diff_omitted,
                }
            )
        return peers

    def _kick_evidence_annotator(self, run_id: str) -> None:
        try:
            from goal_plus.evidence_annotator import kick_evidence_annotator

            kick_evidence_annotator(self.root_dir, run_id)
        except Exception:
            # Evidence settlement and reads never depend on explanatory Views.
            return

    def _plan_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "plans"

    def _agent_session_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "agent_sessions"

    @staticmethod
    def _make_agent_session_id(run_id: str, index: int) -> str:
        run_suffix = run_id.removeprefix("run_")
        return f"agent_{run_suffix}_{index:03d}"

    def _load_agent_session_by_id(
        self,
        agent_session_id: str,
        run_id: str | None = None,
    ) -> AgentSessionRecord:
        if run_id is not None:
            path = self._agent_session_dir(run_id) / f"{agent_session_id}.json"
            if path.exists():
                return AgentSessionRecord.model_validate(load_json(path))
            raise FileNotFoundError(
                f"agent session not found: {agent_session_id} in run {run_id}"
            )

        matches = sorted(self.runs_dir.glob(f"*/agent_sessions/{agent_session_id}.json"))
        if len(matches) == 1:
            return AgentSessionRecord.model_validate(load_json(matches[0]))
        if len(matches) > 1:
            match_runs = ", ".join(path.parents[1].name for path in matches)
            raise RuntimeError(
                f"ambiguous agent_session_id {agent_session_id}; matched runs: {match_runs}. "
                "Use a globally unique agent_session_id from search_start_agent_session."
            )
        raise FileNotFoundError(f"agent session not found: {agent_session_id}")

    def _write_agent_session(self, session: AgentSessionRecord) -> None:
        write_json(
            self._agent_session_dir(session.run_id) / f"{session.agent_session_id}.json",
            session.model_dump(mode="json"),
        )

    def _load_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]:
        session_dir = self._agent_session_dir(run_id)
        if not session_dir.exists():
            return []
        return [
            AgentSessionRecord.model_validate(load_json(path))
            for path in sorted(session_dir.glob("agent_*.json"))
        ]

    def _load_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec:
        data = load_json(self._spec_dir(frozen_spec_id) / "frozen_spec.json")
        spec_data = data.get("spec")
        if isinstance(spec_data, dict) and "workspace" not in spec_data:
            # Frozen specs created before workspace backends were persisted used
            # an independent copy for every candidate. Preserve that behavior
            # when resuming legacy runs even though new specs default to a
            # shared-object Git worktree layout.
            spec_data["workspace"] = {"backend": "copy"}
        return FrozenSpec.model_validate(data)

    def _load_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(load_json(self._run_dir(run_id) / "run.json"))

    def _write_run(self, run: RunRecord) -> None:
        write_json(self._run_dir(run.run_id) / "run.json", run.model_dump(mode="json"))

    def _load_plan(self, run_id: str, plan_id: str) -> SearchPlan:
        return SearchPlan.model_validate(load_json(self._plan_dir(run_id) / f"{plan_id}.json"))

    def _write_plan(self, plan: SearchPlan) -> None:
        write_json(
            self._plan_dir(plan.run_id) / f"{plan.plan_id}.json",
            plan.model_dump(mode="json"),
        )

    def _load_plans(self, run_id: str) -> list[SearchPlan]:
        plan_dir = self._plan_dir(run_id)
        if not plan_dir.exists():
            return []
        return [
            SearchPlan.model_validate(load_json(path))
            for path in sorted(plan_dir.glob("plan_*.json"))
        ]

    def _load_candidate_record(self, run_id: str, candidate_id: str) -> CandidateRecord:
        record = CandidateRecord.model_validate(
            load_json(self._candidate_dir(run_id, candidate_id) / "candidate.json")
        )
        if not record.results_ledger and record.results_ledger_git_head is None:
            record.results_ledger = self._read_results_tsv(record)
        return record

    def _write_candidate_record(self, run_id: str, record: CandidateRecord) -> None:
        candidate_dir = self._candidate_dir(run_id, record.candidate_id)
        write_json(candidate_dir / "candidate.json", record.model_dump(mode="json"))
        write_json(candidate_dir / "task.json", record.task.model_dump(mode="json"))

    def _load_candidate_records(self, run_id: str) -> list[CandidateRecord]:
        candidates_dir = self._run_dir(run_id) / "candidates"
        if not candidates_dir.exists():
            return []
        records = []
        for path in sorted(candidates_dir.glob("*/candidate.json")):
            records.append(CandidateRecord.model_validate(load_json(path)))
        return records

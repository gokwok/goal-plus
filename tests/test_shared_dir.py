from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

import pytest

from goal_plus.evidence_annotator import (
    EvidenceAnnotationResult,
    ToolViewOutput,
    drain_evidence_annotations,
)
from goal_plus.models import ScoreReport, SearchSpec
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.shared_dir import SharedDirManager
from goal_plus.tools import SearchTools
from tests._runtime_helpers import make_project, spec_for


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CandidateSession:
    candidate_id: str
    agent_session_id: str
    workspace: Path

    @property
    def share_out(self) -> Path:
        return self.workspace / ".tmp" / "share-out"

    @property
    def tool_drafts(self) -> Path:
        return self.workspace / ".tmp" / "tool-drafts"

    def write_program_value(self, value: str | int | float) -> None:
        (self.workspace / "initial_program.py").write_text(
            f"VALUE = {value}\n",
            encoding="utf-8",
        )

    def read_program(self) -> str:
        return (self.workspace / "initial_program.py").read_text(encoding="utf-8")


def _shared_run(
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_files: int = 64,
    max_bytes: int = 2 * 1024 * 1024,
    max_tools: int = 16,
    max_path_entries: int = 512,
    max_depth: int = 8,
    extra_allowed_files: int = 0,
) -> tuple[FileSearchRuntime, str, list[CandidateSession]]:
    project = make_project(tmp_path)
    for index in range(extra_allowed_files):
        (project / f"extra_{index}.py").write_text(
            f"VALUE_{index} = 0\n",
            encoding="utf-8",
        )
    (project / "evaluator.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "value = Path('initial_program.py').read_text().split('=', 1)[1].strip()\n"
        "print(json.dumps({'combined_score': float(value)}))\n",
        encoding="utf-8",
    )
    data = spec_for(project, max_parallel=2).model_dump(mode="json")
    data["edit_surface"]["allow"].extend(
        f"extra_{index}.py" for index in range(extra_allowed_files)
    )
    data["workspace"] = {"backend": "git_worktree"}
    data["shared_dir"] = {
        "enabled": enabled,
        "max_tools_per_iteration": max_tools,
        "max_files_per_iteration": max_files,
        "max_path_entries_per_iteration": max_path_entries,
        "max_depth": max_depth,
        "max_bytes_per_iteration": max_bytes,
    }
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    sessions: list[CandidateSession] = []
    for task in tasks:
        session = runtime.start_agent_session(run_id, task.candidate_id)
        sessions.append(
            CandidateSession(
                candidate_id=task.candidate_id,
                agent_session_id=session.agent_session_id,
                workspace=task.workspace,
            )
        )
    return runtime, run_id, sessions


def _write_tool(share_out: Path, name: str = "score-helper") -> None:
    tool = share_out / name
    tool.mkdir(parents=True)
    (tool / "manifest.json").write_text(
        json.dumps(
            {
                "name": "score-helper",
                "summary": "Parse the toy score from a source file.",
                "entrypoint": "helper.py:read_score",
            }
        ),
        encoding="utf-8",
    )
    (tool / "helper.py").write_text(
        "def read_score(text):\n    return float(text.split('=', 1)[1])\n",
        encoding="utf-8",
    )


def test_settlement_receipts_survive_later_tool_publications(tmp_path: Path) -> None:
    manager = SharedDirManager(tmp_path / "run")
    first_share_out = tmp_path / "first" / ".tmp" / "share-out"
    _write_tool(first_share_out, "first-helper")
    first = manager.settle_iteration(
        candidate_id="c001",
        iteration=1,
        source_commit="a" * 40,
        share_out_dir=first_share_out,
        max_tools=4,
        max_files=16,
        max_bytes=1024 * 1024,
        max_path_entries=64,
        max_depth=4,
        settlement_id="request-first",
    )
    second_share_out = tmp_path / "second" / ".tmp" / "share-out"
    _write_tool(second_share_out, "second-helper")
    manager.settle_iteration(
        candidate_id="c001",
        iteration=2,
        source_commit="b" * 40,
        share_out_dir=second_share_out,
        max_tools=4,
        max_files=16,
        max_bytes=1024 * 1024,
        max_path_entries=64,
        max_depth=4,
        settlement_id="request-second",
    )

    index = json.loads(manager.index_path.read_text(encoding="utf-8"))
    assert set(index["settlements"]) == {"request-first", "request-second"}
    replayed = manager.settle_iteration(
        candidate_id="c001",
        iteration=1,
        source_commit="a" * 40,
        share_out_dir=first_share_out,
        max_tools=4,
        max_files=16,
        max_bytes=1024 * 1024,
        max_path_entries=64,
        max_depth=4,
        settlement_id="request-first",
    )
    assert replayed.tools[0].tool_id == first.tools[0].tool_id


def _publish_pending_views(runtime: FileSearchRuntime, run_id: str) -> int:
    class PublishingAnnotator:
        def annotate(self, context):
            return EvidenceAnnotationResult(
                description="客观描述该次候选修改。",
                usage={},
                tool_views=[
                    ToolViewOutput(
                        tool_id=tool["tool_id"],
                        summary="解析候选分数。",
                        capabilities=["解析数值"],
                        when_to_use="需要相同解析逻辑时。",
                        entrypoint=tool["entrypoint"],
                        inputs=["源码文本"],
                        outputs=["数值"],
                        dependencies=["Python 标准库"],
                        adoption_steps=["复制并重新验证"],
                        limitations=["不保证独立收益"],
                    )
                    for tool in context["published_tools"]
                ],
            )

    return drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=PublishingAnnotator(),
    )


def _run_worker_verifier(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate: CandidateSession,
    hypothesis: str,
    **kwargs: Any,
) -> ScoreReport:
    return runtime.run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=candidate.agent_session_id,
        hypothesis=hypothesis,
        **kwargs,
    )


def _iterations(
    runtime: FileSearchRuntime,
    run_id: str,
    candidate: CandidateSession,
) -> list[dict[str, Any]]:
    return runtime.list_iterations(run_id, candidate.candidate_id)


def _shared_index(runtime: FileSearchRuntime, run_id: str) -> dict[str, Any]:
    index_path = runtime._run_dir(run_id) / "shared" / "index.json"
    return json.loads(index_path.read_text(encoding="utf-8"))


def test_process_verifier_publishes_share_out_into_global_evidence(
    tmp_path: Path,
) -> None:
    runtime, run_id, candidates = _shared_run(tmp_path)
    producer, peer = candidates
    producer_context = runtime.get_agent_context(producer.agent_session_id)
    share_out = Path(producer_context["candidate_task"]["share_out_dir"])
    shared_dir = runtime._run_dir(run_id) / "shared"

    assert share_out == producer.share_out
    assert "shared_dir" not in producer_context["candidate_task"]
    assert (shared_dir / "index.json").is_file()
    instructions = " ".join(producer_context["candidate_task"]["instructions"])
    assert "shared_dir 发布方规则" in instructions
    assert "同一 run 内其他 candidate" in instructions
    assert "repeated_sequence" in instructions
    assert "domain_probe" in instructions
    assert "parser_or_trace" in instructions
    assert "peer_setup_reduction" in instructions
    assert ".tmp/tool-drafts" in instructions
    assert "search_stage_shared_tool" in instructions
    assert "toolization_decision" in instructions
    assert "不改变 score、disposition、selection 或 promotion" in instructions
    assert "Tool View 后才会出现在 Global Evidence" in instructions
    assert str(shared_dir) not in instructions
    assert "shared/index.json" not in instructions

    draft = producer.tool_drafts / "score-helper" / "helper.py"
    draft.parent.mkdir(parents=True)
    draft.write_text(
        "def read_score(text):\n    return float(text.split('=', 1)[1])\n",
        encoding="utf-8",
    )
    staged = runtime.stage_shared_tool(
        producer.agent_session_id,
        "score-helper",
        "Parse the toy score from a source file.",
        "score-helper/helper.py:read_score",
        [".tmp/tool-drafts/score-helper"],
    )
    assert staged["files"] == ["manifest.json", "score-helper/helper.py"]
    assert staged["staging_path"] == str(share_out / "score-helper")
    producer.write_program_value(1)
    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Raise the score and export a reusable parser",
        toolization_decision={
            "outcome": "staged",
            "signals": ["domain_probe"],
            "exclusion": None,
            "rationale": "Encodes a non-trivial parser used during diagnosis.",
            "tool_names": ["score-helper"],
        },
    )
    assert report.process_passed is True
    assert report.shared_tool_staged_entries == ["score-helper"]
    assert report.shared_tool_staged_file_count == 2
    assert report.shared_tool_publish_status == "published"
    assert report.toolization_decision is not None
    assert report.toolization_decision.signals == ["domain_probe"]
    assert report.toolization_advisories == []
    assert report.shared_tool_consumed_entries == ["score-helper"]
    assert report.shared_tool_deduplicated_entries == []
    assert list(share_out.iterdir()) == []

    hidden = runtime.get_global_evidence(peer.agent_session_id)
    assert hidden[0]["shared_tools"] == []
    assert _publish_pending_views(runtime, run_id) == 1
    evidence = runtime.get_global_evidence(peer.agent_session_id)
    [tool] = evidence[0]["shared_tools"]
    assert tool["candidate_id"] == producer.candidate_id
    assert tool["iteration"] == 1
    assert tool["source_commit"] == evidence[0]["commit"]
    assert tool["name"] == "score-helper"
    assert tool["entrypoint"] == "score-helper/helper.py:read_score"
    assert tool["files"] == ["manifest.json", "score-helper/helper.py"]
    assert "read_only_path" not in tool
    snapshot = Path(
        _iterations(runtime, run_id, producer)[0]["shared_tools"][0][
            "read_only_path"
        ]
    )
    assert snapshot.is_relative_to(shared_dir)
    assert (snapshot / "score-helper" / "helper.py").is_file()

    index = _shared_index(runtime, run_id)
    assert index["schema_version"] == 2
    assert index["settlements"] == {}
    assert [item["tool_id"] for item in index["tools"]] == [tool["tool_id"]]
    iteration = _iterations(runtime, run_id, producer)[0]
    assert iteration["shared_tools"][0]["snapshot_hash"] == tool["snapshot_hash"]
    assert iteration["shared_tool_errors"] == []
    assert iteration["shared_tool_staged_entries"] == ["score-helper"]
    assert iteration["shared_tool_staged_file_count"] == 2
    assert iteration["shared_tool_publish_status"] == "published"
    assert iteration["toolization_decision"]["outcome"] == "staged"
    assert "toolization_advisories" not in iteration
    assert iteration["changed_files"] == ["initial_program.py"]
    assert "toolization_decision" not in evidence[0]
    assert "toolization_advisories" not in evidence[0]
    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    candidate_monitor = monitor["candidates"][producer.candidate_id]
    assert candidate_monitor["shared_tools_published_total"] == 1
    assert candidate_monitor["shared_tool_staged_file_count_last"] == 2
    assert candidate_monitor["shared_tool_publish_status_last"] == "published"
    assert candidate_monitor["shared_tool_publish_status_counts"] == {
        "published": 1
    }
    assert candidate_monitor["toolization_outcome_counts"] == {"staged": 1}
    assert candidate_monitor["toolization_signal_counts"] == {"domain_probe": 1}
    assert candidate_monitor["toolization_advisory_counts"] == {}


def test_annotator_publishes_bound_tool_view_into_global_evidence(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    producer.write_program_value(1)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a parser for peer adoption",
    )
    before = runtime.get_global_evidence(peer.agent_session_id)
    assert before[0]["shared_tools"] == []
    [published] = _iterations(runtime, run_id, producer)[0]["shared_tools"]

    class InspectingAnnotator:
        def annotate(self, context):
            [tool_input] = context["published_tools"]
            assert tool_input["tool_id"] == published["tool_id"]
            assert tool_input["snapshot_hash"] == published["snapshot_hash"]
            assert tool_input["source_commit"] == before[0]["commit"]
            assert tool_input["manifest"]["entrypoint"] == "helper.py:read_score"
            assert tool_input["goal_evidence"]["disposition"] == "keep"
            assert tool_input["goal_evidence"]["goal_effect"] == "unknown"
            assert not any(
                item.get("path") == "manifest.json" and "text" in item
                for item in tool_input["snapshot_excerpts"]
            )
            assert any(
                item.get("path") == "helper.py" and "read_score" in item.get("text", "")
                for item in tool_input["snapshot_excerpts"]
            )
            return EvidenceAnnotationResult(
                description="发布了一个解析候选分数的辅助工具。",
                usage={"input_tokens": 10, "output_tokens": 5},
                tool_views=[
                    ToolViewOutput(
                        tool_id=tool_input["tool_id"],
                        summary="从候选源码文本中解析数值分数。",
                        capabilities=["解析等号右侧的浮点数"],
                        when_to_use="复用相同的文本分数格式时。",
                        entrypoint="hallucinated.py:wrong_entrypoint",
                        inputs=["包含 VALUE=<number> 的文本"],
                        outputs=["浮点数"],
                        dependencies=["Python 标准库"],
                        adoption_steps=["复制 helper.py 到 allowed_files", "重新运行 verifier"],
                        limitations=["只支持包含等号的文本"],
                    )
                ],
            )

    assert drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=InspectingAnnotator(),
    ) == 1
    after = runtime.get_global_evidence(peer.agent_session_id)
    tool_view = after[0]["shared_tools"][0]["tool_view"]
    assert tool_view["tool_id"] == published["tool_id"]
    assert tool_view["snapshot_hash"] == published["snapshot_hash"]
    assert tool_view["source_commit"] == before[0]["commit"]
    assert tool_view["entrypoint"] == "helper.py:read_score"
    assert tool_view["capabilities"] == ["解析等号右侧的浮点数"]
    assert "不代表工具已被独立验证" in tool_view["evidence_scope"]
    assert "evidence_summary" not in after[0]["shared_tools"][0]


def test_annotator_rejects_tool_identity_mismatch(tmp_path: Path) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a parser with immutable identity",
    )

    class WrongIdentityAnnotator:
        def annotate(self, _context):
            return EvidenceAnnotationResult(
                description="尝试描述一个错误工具身份。",
                usage={"input_tokens": 3},
                tool_views=[
                    ToolViewOutput(
                        tool_id="invented-tool-id",
                        summary="错误身份。",
                        capabilities=[],
                        when_to_use="不适用。",
                        entrypoint=None,
                        inputs=[],
                        outputs=[],
                        dependencies=[],
                        adoption_steps=[],
                        limitations=["身份不匹配"],
                    )
                ],
            )

    assert drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=WrongIdentityAnnotator(),
    ) == 0
    annotation_task = runtime._load_evidence_annotation_task(
        run_id, producer.candidate_id, 1
    )
    assert annotation_task is not None
    assert annotation_task.state == "retry_wait"
    assert annotation_task.usage == {"input_tokens": 3}
    assert "identities do not match" in (annotation_task.last_error or "")
    hidden = runtime.get_global_evidence(peer.agent_session_id)[0]
    assert hidden["shared_tools"] == []


def test_annotator_rejects_tampered_tool_snapshot(tmp_path: Path) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a hash-bound parser snapshot",
    )
    published = _iterations(runtime, run_id, producer)[0]["shared_tools"][0]
    helper = Path(published["read_only_path"]) / "helper.py"
    helper.chmod(0o666)
    helper.write_text("def read_score(_text):\n    return 999.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot integrity mismatch"):
        runtime._evidence_annotation_context(run_id, producer.candidate_id, 1)


def test_tool_copy_requires_exact_discoverable_snapshot(tmp_path: Path) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a hash-bound helper",
    )
    [published_record] = _iterations(runtime, run_id, producer)[0]["shared_tools"]
    with pytest.raises(ValueError, match="not discoverable before its Tool View"):
        runtime.copy_shared_tool(
            consumer.agent_session_id,
            published_record["tool_id"],
            published_record["snapshot_hash"],
        )
    assert _iterations(runtime, run_id, consumer) == []
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0][
        "shared_tools"
    ]
    with pytest.raises(ValueError, match="snapshot_hash mismatch"):
        runtime.copy_shared_tool(
            consumer.agent_session_id,
            published["tool_id"],
            "incorrect-hash",
        )
    helper = Path(published_record["read_only_path"]) / "helper.py"
    helper.chmod(0o666)
    helper.write_text("def read_score(_text):\n    return 999.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot integrity mismatch"):
        runtime.copy_shared_tool(
            consumer.agent_session_id,
            published["tool_id"],
            published["snapshot_hash"],
        )
    assert _iterations(runtime, run_id, consumer) == []


def test_copy_receipts_accumulate_keep_and_discard_for_all_loops(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    producer.write_program_value(2)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a score parser from a valid source iteration",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]

    consumer.write_program_value(3)
    _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Establish the consumer baseline",
    )
    first_receipt = runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )
    assert Path(first_receipt["inbox_path"], "helper.py").is_file()
    consumer.write_program_value(2)
    discarded = _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Adapt the shared parser with a slower candidate path",
    )
    assert discarded.disposition == "discard"
    discard_iteration = _iterations(runtime, run_id, consumer)[-1]
    assert discard_iteration["adopted_tools"][0]["receipt_id"] == first_receipt[
        "receipt_id"
    ]
    assert discard_iteration["adoption_confounded"] is False
    assert not Path(first_receipt["inbox_path"]).exists()
    assert consumer.read_program() == "VALUE = 3\n"

    second_receipt = runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )
    consumer.write_program_value(4)
    kept = _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Adapt the shared parser with a faster candidate path",
    )
    assert kept.disposition == "keep"
    keep_iteration = _iterations(runtime, run_id, consumer)[-1]
    assert keep_iteration["adopted_tools"][0]["receipt_id"] == second_receipt[
        "receipt_id"
    ]
    assert keep_iteration["adoption_confounded"] is False

    class AdoptionAnnotator:
        def annotate(self, context):
            adoption = context["tool_adoptions"]
            return EvidenceAnnotationResult(
                description=(
                    "客观描述该次候选修改和验证结果。"
                    if not adoption
                    else (
                        f"采用 {adoption[0]['tool_id']} 后结算为 "
                        f"{adoption[0]['disposition']}；候选分析已结合实际 diff 核对。"
                    )
                ),
                usage={},
                tool_views=[
                    ToolViewOutput(
                        tool_id=tool["tool_id"],
                        summary="解析候选分数。",
                        capabilities=["解析数值"],
                        when_to_use="需要相同解析逻辑时。",
                        entrypoint=tool["entrypoint"],
                        inputs=["源码文本"],
                        outputs=["数值"],
                        dependencies=["Python 标准库"],
                        adoption_steps=["复制并重新验证"],
                        limitations=["不保证独立收益"],
                    )
                    for tool in context["published_tools"]
                ],
            )

    assert drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=AdoptionAnnotator(),
    ) == 3
    global_evidence = runtime.get_global_evidence(producer.agent_session_id)
    adoption_views = [
        entry["view"]
        for entry in global_evidence
        if entry["candidate_id"] == consumer.candidate_id
        and "采用 " in entry["view"]
    ]
    assert any("discard" in view for view in adoption_views)
    assert any("keep" in view for view in adoption_views)
    assert "evidence_summary" not in global_evidence[0]["shared_tools"][0]


def test_confounded_adoption_is_visible_but_excluded_from_tool_statistics(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(
        tmp_path,
        extra_allowed_files=5,
    )
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a small tool",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]
    consumer.write_program_value(1)
    _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Establish a consumer baseline",
    )
    consumer.write_program_value(2)
    for index in range(5):
        (consumer.workspace / f"extra_{index}.py").write_text(
            f"VALUE_{index} = 1\n",
            encoding="utf-8",
        )
    runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )
    report = _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Mix one tool adoption with a broad unrelated rewrite",
    )
    assert report.disposition == "keep"
    assert _iterations(runtime, run_id, consumer)[-1]["adoption_confounded"] is True
    source_tool = runtime.get_global_evidence(producer.agent_session_id)[0][
        "shared_tools"
    ][0]
    assert "evidence_summary" not in source_tool


def test_multiple_tool_adoption_is_confounded(tmp_path: Path) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path)
    _write_tool(producer.share_out, "first-tool")
    _write_tool(producer.share_out, "second-tool")
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish two independent helpers",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    published = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]
    consumer.write_program_value(1)
    _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Establish a consumer baseline",
    )
    consumer.write_program_value(2)
    for tool in published:
        runtime.copy_shared_tool(
            consumer.agent_session_id,
            tool["tool_id"],
            tool["snapshot_hash"],
        )
    report = _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Adopt two shared helpers in one combined trial",
    )
    assert report.disposition == "keep"
    iteration = _iterations(runtime, run_id, consumer)[-1]
    assert len(iteration["adopted_tools"]) == 2
    assert iteration["adoption_confounded"] is True
    source_tools = runtime.get_global_evidence(producer.agent_session_id)[0]["shared_tools"]
    assert all("evidence_summary" not in tool for tool in source_tools)


def test_adoption_without_candidate_changes_is_confounded(tmp_path: Path) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a helper for an isolated no-change trial",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]

    _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Establish the unchanged consumer baseline",
    )
    runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )
    report = _run_worker_verifier(
        runtime,
        run_id,
        consumer,
        "Declare adoption without integrating candidate files",
    )

    assert report.disposition == "retain"
    iteration = _iterations(runtime, run_id, consumer)[-1]
    assert iteration["adoption_confounded"] is True
    assert iteration["attempt_changed_files"] == []


def test_copy_limit_is_checked_before_tool_resolution(tmp_path: Path) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path, max_tools=1)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish one helper",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]
    runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )

    with pytest.raises(ValueError, match="pending tool copies exceed"):
        runtime.copy_shared_tool(consumer.agent_session_id, "unknown", "incorrect")
    assert _iterations(runtime, run_id, consumer) == []


def test_valid_non_improving_iteration_can_still_share_an_tool(tmp_path: Path) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    producer.write_program_value(2)
    first = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Establish the candidate best before sharing",
    )
    assert first.disposition == "keep"

    _write_tool(producer.share_out)
    producer.write_program_value(1)
    second = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Export a reusable parser from a valid lower-scoring attempt",
    )
    assert second.disposition == "discard"

    hidden = runtime.get_global_evidence(peer.agent_session_id)
    assert all(not item["shared_tools"] for item in hidden)
    assert _publish_pending_views(runtime, run_id) == 2
    evidence = runtime.get_global_evidence(peer.agent_session_id)
    assert evidence[-1]["disposition"] == "discard"
    assert len(evidence[-1]["shared_tools"]) == 1
    assert "evidence_summary" not in evidence[-1]["shared_tools"][0]
    assert producer.read_program() == "VALUE = 2\n"


def test_failed_process_verifier_does_not_publish_tools(tmp_path: Path) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    producer.write_program_value("not-a-number")

    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Try an invalid score while an tool is staged",
    )
    assert report.process_passed is False
    evidence = runtime.get_global_evidence(peer.agent_session_id)
    assert evidence[0]["shared_tools"] == []
    assert _shared_index(runtime, run_id)["tools"] == []
    [iteration] = _iterations(runtime, run_id, producer)
    assert iteration["shared_tool_staged_entries"] == ["score-helper"]
    assert iteration["shared_tool_publish_status"] == "skipped_failed_verifier"


def test_parent_fallback_records_staging_without_publishing(tmp_path: Path) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    report = runtime.run_verifier(run_id, producer.candidate_id)

    assert report.process_passed is True
    [iteration] = _iterations(runtime, run_id, producer)
    assert iteration["agent_session_id"] is None
    assert iteration["shared_tool_staged_entries"] == ["score-helper"]
    assert iteration["shared_tool_publish_status"] == (
        "skipped_unattributed_verifier"
    )
    assert runtime.get_global_evidence(peer.agent_session_id) == []
    assert _shared_index(runtime, run_id)["tools"] == []
    assert not list(
        (runtime._run_dir(run_id) / "shared" / "tools").rglob("helper.py")
    )


def test_parent_fallback_cannot_bypass_pending_tool_copy_receipt(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, consumer] = _shared_run(tmp_path)
    _write_tool(producer.share_out)
    _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a helper for an attributed adoption",
    )
    assert _publish_pending_views(runtime, run_id) == 1
    [published] = runtime.get_global_evidence(consumer.agent_session_id)[0]["shared_tools"]
    receipt = runtime.copy_shared_tool(
        consumer.agent_session_id,
        published["tool_id"],
        published["snapshot_hash"],
    )

    with pytest.raises(
        RuntimeError,
        match="parent process verifier cannot settle.*pending tool copies",
    ):
        runtime.run_verifier(run_id, consumer.candidate_id)

    assert Path(receipt["inbox_path"]).is_dir()
    consumer_record = runtime._load_candidate_record(run_id, consumer.candidate_id)
    assert [item.receipt_id for item in consumer_record.pending_tool_copies] == [
        receipt["receipt_id"]
    ]
    assert _iterations(runtime, run_id, consumer) == []
    assert runtime.status(run_id).state == "waiting_for_workers"


def test_empty_staging_is_reported_as_not_staged(tmp_path: Path) -> None:
    runtime, run_id, [worker, parent] = _shared_run(tmp_path)

    parent_report = runtime.run_verifier(run_id, parent.candidate_id)
    assert parent_report.shared_tool_staged_entries == []
    assert parent_report.shared_tool_publish_status == "not_staged"

    worker.write_program_value("not-a-number")
    worker_report = _run_worker_verifier(
        runtime,
        run_id,
        worker,
        "Fail without staging a shared tool",
    )
    assert worker_report.process_passed is False
    assert worker_report.shared_tool_staged_entries == []
    assert worker_report.shared_tool_publish_status == "not_staged"
    assert worker_report.toolization_advisories == ["toolization_review_missing"]


def test_toolization_advisories_are_observational_only(tmp_path: Path) -> None:
    runtime, run_id, [first, second] = _shared_run(tmp_path)

    first.write_program_value(1)
    missing_stage = _run_worker_verifier(
        runtime,
        run_id,
        first,
        "Record a positive toolization signal without staging",
        toolization_decision={
            "outcome": "staged",
            "signals": ["repeated_sequence"],
            "rationale": "A multi-step workflow was repeated.",
            "tool_names": ["workflow-helper"],
        },
    )
    assert missing_stage.aggregate_score == 1.0
    assert missing_stage.disposition == "keep"
    assert missing_stage.toolization_advisories == ["toolization_stage_missing"]

    _write_tool(second.share_out)
    second.write_program_value(1)
    mismatch = _run_worker_verifier(
        runtime,
        run_id,
        second,
        "Stage content while declaring a concrete exclusion",
        toolization_decision={
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "single_common_command",
            "rationale": "Only an ordinary command was considered reusable.",
            "tool_names": [],
        },
    )
    assert mismatch.aggregate_score == 1.0
    assert mismatch.disposition == "keep"
    assert mismatch.shared_tool_publish_status == "published"
    assert mismatch.toolization_advisories == ["toolization_decision_mismatch"]

    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert monitor["candidates"][first.candidate_id]["toolization_advisory_counts"] == {
        "toolization_stage_missing": 1
    }
    assert monitor["candidates"][second.candidate_id]["toolization_exclusion_counts"] == {
        "single_common_command": 1
    }
    assert monitor["candidates"][second.candidate_id]["toolization_advisory_counts"] == {
        "toolization_decision_mismatch": 1
    }


def test_stage_shared_tool_rejects_sources_outside_draft_root(tmp_path: Path) -> None:
    runtime, _run_id, [producer, _peer] = _shared_run(tmp_path)
    draft = producer.tool_drafts / "helper.py"
    draft.parent.mkdir(parents=True)
    draft.write_text("print('ok')\n", encoding="utf-8")
    outside = producer.workspace / "initial_program.py"

    with pytest.raises(ValueError, match="must be under .tmp/tool-drafts"):
        runtime.stage_shared_tool(
            producer.agent_session_id,
            "unsafe-helper",
            "Attempt to export a candidate artifact.",
            "initial_program.py",
            [str(outside.relative_to(producer.workspace)).replace("\\", "/")],
        )
    with pytest.raises(ValueError, match="without '..'"):
        runtime.stage_shared_tool(
            producer.agent_session_id,
            "escaping-helper",
            "Attempt to traverse outside drafts.",
            "helper.py",
            [".tmp/tool-drafts/../tool-drafts/helper.py"],
        )
    with pytest.raises(ValueError, match="candidate-relative POSIX path"):
        runtime.stage_shared_tool(
            producer.agent_session_id,
            "absolute-entrypoint",
            "Attempt to retain a host-specific entrypoint.",
            "C:/host/private.py:main",
            [".tmp/tool-drafts/helper.py"],
        )

    assert list(producer.share_out.iterdir()) == []


def test_stage_shared_tool_stops_scanning_when_draft_limit_is_reached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    draft = workspace / ".tmp" / "tool-drafts" / "bundle"
    draft.mkdir(parents=True)
    for name in ["entry.py", "extra.py", "unread.py"]:
        (draft / name).write_text(f"# {name}\n", encoding="utf-8")

    original_scandir = os.scandir
    scanned_entries = 0

    class GuardedScandir:
        def __init__(self, path: Path) -> None:
            self._entries = original_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self._entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal scanned_entries
            if scanned_entries >= 2:
                raise AssertionError("draft scan continued after reaching the file limit")
            scanned_entries += 1
            return next(self._entries)

    def guarded_scandir(path):
        if Path(path) == draft:
            return GuardedScandir(Path(path))
        return original_scandir(path)

    monkeypatch.setattr("goal_plus.shared_dir.os.scandir", guarded_scandir)
    share_out = workspace / ".tmp" / "share-out"

    with pytest.raises(ValueError, match="tool exceeds 2 files"):
        SharedDirManager(tmp_path / "run").stage_tool(
            workspace=workspace,
            share_out_dir=share_out,
            name="bounded-helper",
            summary="Stop draft discovery at the configured limit.",
            entrypoint="bundle/entry.py:main",
            candidate_relative_source_paths=[".tmp/tool-drafts/bundle"],
            max_tools=4,
            max_files=2,
            max_bytes=4096,
            max_path_entries=16,
            max_depth=4,
        )

    assert scanned_entries == 2
    assert list(share_out.iterdir()) == []


def test_shared_tool_limits_are_advisory_to_valid_verifier_evidence(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path, max_files=1)
    share_out = producer.share_out
    (share_out / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (share_out / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    producer.write_program_value(1)

    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish only tools within the configured bound",
    )
    assert report.process_passed is True
    assert _publish_pending_views(runtime, run_id) == 1
    [evidence] = runtime.get_global_evidence(peer.agent_session_id)
    assert len(evidence["shared_tools"]) == 1


def test_passing_settlement_consumes_staging_and_only_publishes_deltas(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    share_out = producer.share_out

    _write_tool(share_out)
    first = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish the first helper version",
    )
    assert first.shared_tool_publish_status == "published"
    assert list(share_out.iterdir()) == []

    _write_tool(share_out)
    unchanged = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Restage an unchanged helper",
    )
    assert unchanged.shared_tool_publish_status == "consumed_unchanged"
    assert unchanged.shared_tool_consumed_entries == ["score-helper"]
    assert unchanged.shared_tool_deduplicated_entries == ["score-helper"]
    assert list(share_out.iterdir()) == []

    _write_tool(share_out)
    (share_out / "score-helper" / "helper.py").write_text(
        "def read_score(text):\n    return float(text.rsplit('=', 1)[1])\n",
        encoding="utf-8",
    )
    changed = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Publish a modified helper version",
    )
    assert changed.shared_tool_publish_status == "published"

    assert _publish_pending_views(runtime, run_id) == 3
    evidence = runtime.get_global_evidence(peer.agent_session_id)
    assert [len(item["shared_tools"]) for item in evidence] == [1, 0, 1]
    index = _shared_index(runtime, run_id)
    assert len(index["tools"]) == 2
    assert len({item["snapshot_hash"] for item in index["tools"]}) == 2


def test_identical_content_from_peers_reuses_one_physical_snapshot(
    tmp_path: Path,
) -> None:
    runtime, run_id, [first, second] = _shared_run(tmp_path)
    _write_tool(first.share_out)
    _write_tool(second.share_out)

    first_report = _run_worker_verifier(
        runtime,
        run_id,
        first,
        "Publish a reusable helper",
    )
    second_report = _run_worker_verifier(
        runtime,
        run_id,
        second,
        "Publish identical helper content from another lane",
    )
    assert first_report.shared_tool_publish_status == "published"
    assert second_report.shared_tool_publish_status == "published"

    iterations = [
        _iterations(runtime, run_id, candidate)[0] for candidate in (first, second)
    ]
    paths = {
        item["shared_tools"][0]["read_only_path"] for item in iterations
    }
    assert len(paths) == 1


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_windows_share_out_junction_is_rejected_before_claim(tmp_path: Path) -> None:
    runtime, run_id, [producer, _peer] = _shared_run(tmp_path)
    share_out = producer.share_out
    outside = tmp_path / "outside-share-out"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must remain\n", encoding="utf-8")
    share_out.rmdir()
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(share_out), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"could not create test junction: {completed.stderr.strip()}")

    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Reject a share-out junction before claiming staging",
    )

    assert report.process_passed is True
    assert report.shared_tool_publish_status == "snapshot_rejected"
    assert sentinel.read_text(encoding="utf-8") == "must remain\n"
    assert share_out.exists()
    manager = SharedDirManager(runtime._run_dir(run_id))
    with pytest.raises(ValueError, match="must be a real directory"):
        manager._claim_staging(
            share_out,
            candidate_id=producer.candidate_id,
            iteration=2,
        )
    assert sentinel.read_text(encoding="utf-8") == "must remain\n"
    assert share_out.exists()


def test_depth_and_top_level_limits_stop_scanning_and_leave_staging_recoverable(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, _peer] = _shared_run(
        tmp_path,
        max_tools=1,
        max_depth=1,
    )
    share_out = producer.share_out
    (share_out / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (share_out / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    over_tools = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Exercise the top-level tool bound",
    )
    assert over_tools.process_passed is True
    assert over_tools.shared_tool_publish_status == "snapshot_rejected"
    assert sorted(path.name for path in share_out.iterdir()) == ["one.py", "two.py"]
    assert "top-level tools" in over_tools.shared_tool_errors[0]

    (share_out / "one.py").unlink()
    (share_out / "two.py").unlink()
    nested = share_out / "nested" / "level-one" / "level-two"
    nested.mkdir(parents=True)
    (nested / "deep.py").write_text("DEEP = 1\n", encoding="utf-8")
    over_depth = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Exercise the recursive depth bound",
    )
    assert over_depth.process_passed is True
    assert over_depth.shared_tool_publish_status == "snapshot_rejected"
    assert (nested / "deep.py").is_file()
    assert "maximum depth 1" in over_depth.shared_tool_errors[0]


def test_path_entry_limit_stops_recursive_scan_and_restores_staging(
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, _peer] = _shared_run(
        tmp_path,
        max_path_entries=2,
    )
    share_out = producer.share_out
    _write_tool(share_out)

    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Exercise the filesystem entry traversal bound",
    )
    assert report.process_passed is True
    assert report.shared_tool_publish_status == "snapshot_rejected"
    assert "exceeds 2 filesystem entries" in report.shared_tool_errors[0]
    assert (share_out / "score-helper" / "helper.py").is_file()


def test_index_failure_restores_claimed_staging_without_publishing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, peer] = _shared_run(tmp_path)
    share_out = producer.share_out
    _write_tool(share_out)

    def fail_index_update(self, tools):
        raise OSError("simulated index replace failure")

    monkeypatch.setattr(SharedDirManager, "_append_index", fail_index_update)
    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Keep staged source recoverable if index publication fails",
    )
    assert report.process_passed is True
    assert report.shared_tool_publish_status == "snapshot_error"
    assert (share_out / "score-helper" / "helper.py").is_file()
    assert runtime.get_global_evidence(peer.agent_session_id)[0]["shared_tools"] == []
    assert _shared_index(runtime, run_id)["tools"] == []


def test_failed_verifier_uses_only_cheap_staging_inventory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, _peer] = _shared_run(tmp_path)
    share_out = producer.share_out
    _write_tool(share_out)
    producer.write_program_value("not-a-number")

    def fail_if_scanned(*args, **kwargs):
        raise AssertionError("recursive staging scan should not run")

    monkeypatch.setattr(SharedDirManager, "_tool_files", fail_if_scanned)
    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Fail validity before recursive shared-tool scanning",
    )
    assert report.process_passed is False
    assert report.shared_tool_publish_status == "skipped_failed_verifier"
    assert report.shared_tool_staged_entries == ["score-helper"]
    assert report.shared_tool_staged_file_count == 0
    assert (share_out / "score-helper" / "helper.py").is_file()


def test_staging_inspection_failure_is_advisory_to_verifier_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime, run_id, [producer, _peer] = _shared_run(tmp_path)

    def fail_inspection(self, share_out_dir, **kwargs):
        raise OSError("diagnostic read failed")

    monkeypatch.setattr(SharedDirManager, "inspect_staging", fail_inspection)

    report = _run_worker_verifier(
        runtime,
        run_id,
        producer,
        "Settle valid verifier evidence despite diagnostic failure",
    )

    assert report.process_passed is True
    assert report.shared_tool_publish_status == "snapshot_error"
    assert report.shared_tool_errors is not None
    assert "diagnostic read failed" in report.shared_tool_errors[0]
    [iteration] = _iterations(runtime, run_id, producer)
    assert iteration["shared_tool_publish_status"] == "snapshot_error"
    assert runtime.status(run_id).state != "failed"


def test_shared_dir_is_disabled_by_default(tmp_path: Path) -> None:
    runtime, run_id, [candidate, _peer] = _shared_run(tmp_path, enabled=False)
    context = runtime.get_agent_context(candidate.agent_session_id)
    task = context["candidate_task"]
    assert task["share_out_dir"] is None
    assert "shared_dir" not in task
    assert not (runtime._run_dir(run_id) / "shared").exists()
    instructions = " ".join(task["instructions"])
    assert "manifest.json" not in instructions
    assert "adopted_tools" not in instructions
    assert "search_stage_shared_tool" not in instructions
    assert "search_copy_shared_tool" not in instructions
    assert "toolization_decision" not in instructions
    assert "Tool View 后才会出现在 Global Evidence" not in instructions

    candidate.write_program_value(1)
    report = SearchTools(runtime).search_run_verifier(
        run_id,
        candidate.candidate_id,
        agent_session_id=candidate.agent_session_id,
        hypothesis="Verify without shared tooling",
        toolization_decision={
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": None,
            "rationale": "Shared tooling is disabled.",
            "tool_names": [],
        },
    )
    assert report["process_passed"] is True
    assert "toolization_decision" not in report
    [iteration] = _iterations(runtime, run_id, candidate)
    assert "toolization_decision" not in iteration

    with pytest.raises(ValueError, match="requires shared_dir.enabled=true"):
        _run_worker_verifier(
            runtime,
            run_id,
            candidate,
            "Reject impossible shared-tool staging",
            toolization_decision={
                "outcome": "staged",
                "signals": ["domain_probe"],
                "rationale": "Claim a staged tool while sharing is disabled.",
                "tool_names": ["probe"],
            },
        )
    assert len(_iterations(runtime, run_id, candidate)) == 1


def test_torch_cpu_shared_dir_validation_files_cover_publication_and_adoption() -> None:
    target = ROOT / "examples" / "model-optimize" / "torch-cpu-target"
    treatment = json.loads(
        (target / "shared-dir-treatment-search-spec.json").read_text(
            encoding="utf-8"
        )
    )
    proposals = json.loads(
        (target / "shared-dir-proposals.json").read_text(encoding="utf-8")
    )
    treatment_spec = SearchSpec.model_validate(treatment)
    assert treatment_spec.shared_dir.enabled is True
    assert treatment_spec.strategy.name == "agent_guided"
    assert treatment_spec.budget.max_parallel == 2
    assert treatment_spec.strategy.worker_budget is not None
    assert treatment_spec.strategy.worker_budget.min_verifier_runs == 1
    assert [item.role for item in treatment_spec.process_verifiers] == [
        "validity_gate",
        "ranking_signal",
    ]
    assert len(proposals) == 2
    assert [item["metadata"]["shared_dir_role"] for item in proposals] == [
        "publisher",
        "consumer",
    ]
    experiment = (target / "shared-dir-experiment.md").read_text(encoding="utf-8")
    assert "shared_tool_publish_status" in experiment
    assert "producer staging" in experiment
    assert "adopter" in experiment
    assert "Tool View" in experiment
    assert "annotator" in experiment
    assert "search_copy_shared_tool" in experiment
    assert "adopted_tools" in experiment
    assert "evidence_summary" not in experiment
    assert "Tool Views" in proposals[1]["intent"]
    assert "search_copy_shared_tool" in proposals[1]["instructions"][0]
    assert "evidence_summary" not in " ".join(proposals[1]["instructions"])

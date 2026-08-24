from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from goal_plus.models import (
    AgentHostHandle,
    AgentSessionRecord,
    Budget,
    CandidateRecord,
    CandidateProposal,
    CandidateTask,
    EditSurface,
    GoalPlusSpecDraft,
    FsSnapshotArtifactRef,
    IterationRecord,
    SearchPlan,
    SearchSpec,
    SearchSpecDraft,
    StrategySpec,
    ToolAdoptionRecord,
    ToolizationDecision,
    VerifierCommand,
    WorkerBudget,
    ModelSpec,
)
from goal_plus.runtime import (
    FileSearchRuntime,
    SUPPLEMENTAL_EVALUATION_ENABLED_ENV,
    SUPPLEMENTAL_EVALUATION_REQUIRED_ENV,
)
from tests._runtime_helpers import make_project


def test_legacy_tool_adoption_declaration_drops_mode() -> None:
    adoption = ToolAdoptionRecord.model_validate(
        {
            "tool_id": "tool_001",
            "snapshot_hash": "abc123",
            "mode": "adapted",
        }
    )

    assert adoption.model_dump(mode="json") == {
        "tool_id": "tool_001",
        "snapshot_hash": "abc123",
    }


def test_candidate_task_drops_legacy_shared_dir_path() -> None:
    task = CandidateTask.model_validate(
        {
            "run_id": "run_1",
            "candidate_id": "c001",
            "hypothesis": "try a change",
            "workspace": ".",
            "shared_dir": ".gp/runs/run_1/shared",
            "allowed_files": ["initial_program.py"],
            "denied_files": ["evaluator.py"],
        }
    )

    assert "shared_dir" not in task.model_dump(mode="json")


def test_legacy_iteration_infers_shared_tool_publish_status() -> None:
    asset = {
        "asset_id": "legacy-asset",
        "candidate_id": "c001",
        "iteration": 1,
        "snapshot_hash": "abc123",
        "name": "legacy helper",
        "source_relative_path": "legacy-helper",
        "read_only_path": "/tmp/legacy-helper",
        "files": ["helper.py"],
        "size_bytes": 12,
        "created_at": "2026-08-03T00:00:00Z",
    }

    published = IterationRecord.model_validate(
        {
            "iteration": 1,
            "shared_tools": [asset],
            "created_at": "2026-08-03T00:00:00Z",
        }
    )
    partial = IterationRecord.model_validate(
        {
            "iteration": 1,
            "shared_tools": [asset],
            "shared_tool_errors": ["second asset rejected"],
            "created_at": "2026-08-03T00:00:00Z",
        }
    )
    unknown = IterationRecord.model_validate(
        {"iteration": 1, "created_at": "2026-08-03T00:00:00Z"}
    )

    assert published.shared_tool_publish_status == "published"
    assert partial.shared_tool_publish_status == "partially_published"
    assert unknown.shared_tool_publish_status == "legacy_unknown"
    assert not {
        "adopted_tools",
        "adoption_confounded",
        "toolization_decision",
        "toolization_advisories",
    } & set(
        unknown.model_dump(mode="json")
    )


def test_toolization_decision_enforces_positive_signals_and_exclusions() -> None:
    staged = ToolizationDecision.model_validate(
        {
            "outcome": "staged",
            "signals": ["repeated_sequence", "parser_or_trace"],
            "rationale": "  Encodes a repeated trace workflow.  ",
            "tool_names": ["trace-checker"],
        }
    )
    not_applicable = ToolizationDecision.model_validate(
        {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "single_common_command",
            "rationale": "Only one ordinary command was used.",
            "tool_names": [],
        }
    )

    assert staged.rationale == "Encodes a repeated trace workflow."
    assert not_applicable.exclusion == "single_common_command"

    for payload, message in [
        (
            {
                "outcome": "staged",
                "signals": [],
                "rationale": "No positive signal.",
                "tool_names": ["helper"],
            },
            "requires at least one signal",
        ),
        (
            {
                "outcome": "staged",
                "signals": ["domain_probe"],
                "rationale": "No named staged tool.",
                "tool_names": [],
            },
            "requires at least one tool name",
        ),
        (
            {
                "outcome": "not_applicable",
                "signals": [],
                "rationale": "No concrete exclusion.",
                "tool_names": [],
            },
            "requires an exclusion",
        ),
    ]:
        with pytest.raises(ValidationError, match=message):
            ToolizationDecision.model_validate(payload)


def valid_spec_dict() -> dict:
    return {
        "objective": "maximize toy score",
        "metric_name": "combined_score",
        "metric_direction": "maximize",
        "source_path": ".",
        "edit_surface": {
            "allow": ["initial_program.py"],
            "deny": ["evaluator.py"]},
        "budget": {
            "max_parallel": 2},
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["python", "evaluator.py"]}
        ]}


def test_search_spec_parses_nested_models_and_serializes_enums() -> None:
    spec = SearchSpec.model_validate(valid_spec_dict())

    assert isinstance(spec.budget, Budget)
    assert isinstance(spec.edit_surface, EditSurface)
    assert isinstance(spec.process_verifiers[0], VerifierCommand)
    assert isinstance(spec.strategy, StrategySpec)

    dumped = spec.model_dump(mode="json")
    assert dumped["process_verifiers"][0]["role"] == "ranking_signal"
    assert dumped["metric_direction"] == "maximize"
    assert dumped["strategy"]["name"] == "agent_guided"
    assert dumped["strategy"]["orchestration_mode"] == "parallel_loops"
    assert dumped["strategy"]["worker_host"] == "codex"
    assert "models" not in dumped["strategy"]


@pytest.mark.pi
def test_pi_thinkthread_spec_omits_workspace_and_rejects_selector() -> None:
    data = valid_spec_dict()
    data["strategy"] = {
        "worker_host": "pi-thinkthread",
        "worker_budget": {"max_runtime_seconds": 300},
    }

    spec = SearchSpec.model_validate(data)

    assert spec.workspace is None
    assert "workspace" not in spec.model_dump(mode="json")

    data["workspace"] = {"backend": "git_worktree"}
    with pytest.raises(ValidationError, match="must omit workspace"):
        SearchSpec.model_validate(data)


def test_fs_snapshot_artifact_ref_is_strict_and_discriminated() -> None:
    reference = FsSnapshotArtifactRef(snapshot_id="fsnap-1234")

    assert reference.model_dump(mode="json") == {
        "kind": "fs_snapshot",
        "snapshot_id": "fsnap-1234",
    }

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        FsSnapshotArtifactRef(snapshot_id="wrev-old-api")


def test_required_supplemental_evaluation_rejects_disabled_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    data = valid_spec_dict()
    data["source_path"] = str(project)
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_ENABLED_ENV, "0")
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_REQUIRED_ENV, "1")

    with pytest.raises(
        ValueError,
        match="requires GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1",
    ):
        FileSearchRuntime(tmp_path / ".gp-missing").freeze_spec(
            SearchSpec.model_validate(data), [project / "evaluator.py"]
        )


def test_supplemental_evaluation_does_not_change_frozen_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    data = valid_spec_dict()
    data["source_path"] = str(project)
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_ENABLED_ENV, "1")
    monkeypatch.setenv(SUPPLEMENTAL_EVALUATION_REQUIRED_ENV, "1")

    frozen = FileSearchRuntime(tmp_path / ".gp").freeze_spec(
        SearchSpec.model_validate(data), [project / "evaluator.py"]
    )

    frozen_spec = frozen.spec.model_dump(mode="json")
    assert "acceptance_view" not in frozen_spec
    assert "supplemental_evaluation" not in frozen_spec


def test_goal_plus_spec_draft_exposes_typed_partial_search_spec() -> None:
    draft = GoalPlusSpecDraft(
        baseline={},
        metric={"name": "combined_score"},
        correctness_gate={},
        edit_surface={},
        search_spec={
            "metric_name": "combined_score",
            "edit_surface": {"allow": ["solution.cpp"]},
            "process_verifiers": [
                {
                    "name": "public_score",
                    "role": "ranking_signal",
                    "command": ["python", "verify.py"],
                }
            ],
        },
        promotion_rule="highest public score",
        confidence="medium",
        open_questions=["Confirm the source path."],
    )

    assert isinstance(draft.search_spec, SearchSpecDraft)
    assert draft.search_spec.edit_surface is not None
    assert draft.search_spec.edit_surface.allow == ["solution.cpp"]
    assert draft.model_dump(mode="json")["search_spec"] == {
        "metric_name": "combined_score",
        "edit_surface": {
            "allow": ["solution.cpp"],
            "deny": [],
        },
        "process_verifiers": [
            {
                "name": "public_score",
                "role": "ranking_signal",
                "command": ["python", "verify.py"],
                "cwd": ".",
                "timeout_seconds": 300,
                "feedback_policy": "visible_to_workers",
                "expected_outputs": [],
            }
        ],
    }


def test_goal_plus_spec_draft_keeps_legacy_unstructured_search_spec_readable() -> None:
    draft = GoalPlusSpecDraft(
        baseline={},
        metric={},
        correctness_gate={},
        edit_surface={},
        search_spec={"allowed_paths": ["legacy.py"], "process_verifier": "old"},
        promotion_rule="legacy record",
        confidence="high",
    )

    assert isinstance(draft.search_spec, dict)
    assert draft.model_dump(mode="json")["search_spec"]["allowed_paths"] == [
        "legacy.py"
    ]


def test_expected_outputs_schema_describes_artifact_paths_not_stdout_parser() -> None:
    schema = VerifierCommand.model_json_schema()
    description = schema["properties"]["expected_outputs"]["description"]

    assert "产物路径或 glob" in description
    assert "不解析 verifier stdout metric" in description


def test_model_spec_requires_a_concrete_model_reference() -> None:
    with pytest.raises(ValidationError):
        ModelSpec(model="")

    model = ModelSpec(model="gpt", count=1)
    assert model.model_dump(mode="json") == {
        "model": "gpt",
        "count": 1,
        "provider": None,
        "adapter_version": None,
        "reasoning_effort": None,
        "service_tier": None,
        "context_policy": {},
    }


def test_verifier_resource_lock_rejects_blank_names() -> None:
    command = {
        "name": "score",
        "role": "ranking_signal",
        "command": ["python", "verify.py"],
        "resource_lock": "ascend-npu:0",
    }
    assert VerifierCommand.model_validate(command).resource_lock == "ascend-npu:0"
    command["resource_lock"] = "  ascend-npu:0  "
    assert VerifierCommand.model_validate(command).resource_lock == "ascend-npu:0"

    command["resource_lock"] = " "
    with pytest.raises(ValidationError, match="resource_lock must be non-empty"):
        VerifierCommand.model_validate(command)


def test_search_spec_supports_copy_and_git_worktree_workspace_backends() -> None:
    default_spec = SearchSpec.model_validate(valid_spec_dict())
    assert default_spec.workspace.backend == "git_worktree"
    assert default_spec.model_dump(mode="json")["workspace"] == {
        "backend": "git_worktree"
    }

    copy_data = valid_spec_dict()
    copy_data["workspace"] = {"backend": "copy"}
    copy_spec = SearchSpec.model_validate(copy_data)
    assert copy_spec.workspace.backend == "copy"

    invalid_data = valid_spec_dict()
    invalid_data["workspace"] = {"backend": "overlay"}
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(invalid_data)


def test_search_spec_requires_structured_strategy() -> None:
    data = valid_spec_dict()
    data["strategy"] = {
        "name": "agent_guided",
        "worker_host": "codex",
        "worker_agent_type": "search_candidate_agent",
    }
    spec = SearchSpec.model_validate(data)
    assert spec.strategy.name == "agent_guided"
    assert spec.strategy.worker_host == "codex"
    assert spec.strategy.worker_agent_type == "search_candidate_agent"

    legacy_string = valid_spec_dict()
    legacy_string["strategy"] = "evolve"
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(legacy_string)

    for retired_field, retired_value in (
        ("worker_mode", "agent-session-pool"),
        ("history_policy", {"scope": "top_n"}),
        ("driver", "builtin"),
        ("parent_policy", "best"),
    ):
        data = valid_spec_dict()
        data["strategy"] = {"name": "agent_guided", retired_field: retired_value}
        with pytest.raises(ValidationError):
            SearchSpec.model_validate(data)


def test_strategy_spec_accepts_supported_worker_hosts() -> None:
    assert StrategySpec(worker_host="codex").worker_host == "codex"
    assert StrategySpec(worker_host="pi-rpc").worker_host == "pi-rpc"

    with pytest.raises(ValidationError):
        StrategySpec(worker_host="unsupported")  # type: ignore[arg-type]


def test_strategy_spec_accepts_parallel_loop_orchestration() -> None:
    default = StrategySpec()
    parallel = StrategySpec(orchestration_mode="parallel_loops")

    assert default.orchestration_mode == "parallel_loops"
    assert parallel.orchestration_mode == "parallel_loops"

    with pytest.raises(ValidationError):
        StrategySpec(orchestration_mode="conductor")  # type: ignore[arg-type]


def test_strategy_spec_accepts_worker_budget() -> None:
    spec = StrategySpec(
        worker_host="codex",
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 2,
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
    )

    assert isinstance(spec.worker_budget, WorkerBudget)
    assert spec.worker_budget.min_runtime_seconds == 300
    assert spec.worker_budget.min_verifier_runs == 2
    assert spec.worker_budget.max_runtime_seconds == 600
    assert spec.worker_budget.max_turns == 8
    assert spec.worker_budget.on_exceed == "interrupt"
    assert spec.model_dump(mode="json")["worker_budget"] == {
        "min_runtime_seconds": 300,
        "min_verifier_runs": 2,
        "max_runtime_seconds": 600,
        "max_turns": 8,
        "on_exceed": "interrupt",
    }


@pytest.mark.codex
def test_strategy_spec_accepts_codex_worker_launch_options() -> None:
    spec = StrategySpec(
        worker_host="codex",
        worker_launch={
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "service_tier": "priority",
        },
    )

    assert spec.model_dump(mode="json")["worker_launch"] == {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "service_tier": "priority",
    }


def test_evidence_annotator_config_is_optional_and_overridable() -> None:
    inherited = StrategySpec()
    extended_timeout = StrategySpec(
        evidence_annotator={"timeout_seconds": 1800}
    )
    explicit = StrategySpec(
        evidence_annotator={
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "timeout_seconds": 90,
            "provider": {
                "base_url": "https://proxy.example/v1",
                "api_key_env": "ANNOTATOR_API_KEY",
            },
        }
    )
    pi_explicit = StrategySpec(
        worker_host="pi-rpc",
        evidence_annotator={
            "model": "deepseek-chat",
            "pi_provider": "deepseek",
        },
    )
    independent_codex = StrategySpec(
        worker_host="pi-rpc",
        evidence_annotator={
            "host": "codex",
            "model": "gpt-5.6-luna",
        },
    )

    assert inherited.model_dump(mode="json")["evidence_annotator"] == {
        "host": None,
        "model": None,
        "pi_provider": None,
        "reasoning_effort": None,
        "timeout_seconds": 1800,
        "provider": None,
    }
    assert extended_timeout.evidence_annotator.timeout_seconds == 1800
    with pytest.raises(ValidationError):
        StrategySpec(evidence_annotator={"timeout_seconds": 1801})
    assert explicit.model_dump(mode="json")["evidence_annotator"] == {
        "host": None,
        "model": "gpt-5.6-sol",
        "pi_provider": None,
        "reasoning_effort": "medium",
        "timeout_seconds": 90,
        "provider": {
            "provider_id": "goal-plus-evidence",
            "name": "Goal Plus Evidence provider",
            "base_url": "https://proxy.example/v1",
            "api_key_env": "ANNOTATOR_API_KEY",
            "wire_api": "responses",
        },
    }
    assert pi_explicit.model_dump(mode="json")["evidence_annotator"] == {
        "host": None,
        "model": "deepseek-chat",
        "pi_provider": "deepseek",
        "reasoning_effort": None,
        "timeout_seconds": 1800,
        "provider": None,
    }
    assert independent_codex.evidence_annotator.host == "codex"


def test_worker_budget_requires_runtime_or_turn_limit() -> None:
    with pytest.raises(ValidationError):
        WorkerBudget()

    with pytest.raises(ValidationError):
        WorkerBudget(max_runtime_seconds=0)

    with pytest.raises(ValidationError):
        WorkerBudget(max_turns=0)

    with pytest.raises(ValidationError, match="requires max_runtime_seconds"):
        WorkerBudget(min_runtime_seconds=300, max_turns=8)

    with pytest.raises(ValidationError, match="must be less than"):
        WorkerBudget(min_runtime_seconds=600, max_runtime_seconds=600)


def test_strategy_plan_models_capture_initial_independent_proposals() -> None:
    plan = SearchPlan(
        run_id="run_1",
        plan_id="plan_001",
        strategy=StrategySpec(name="agent_guided"),
        requested_k=4,
        planned_k=2,
        remaining_budget=2,
        requires_agent_proposals=True,
        created_at="2026-06-24T00:00:00Z",
    )
    proposal = CandidateProposal(
        intent="try an independent implementation",
        expected_tradeoff="higher score with more risk",
    )

    assert plan.requires_agent_proposals is True
    assert proposal.intent == "try an independent implementation"

    with pytest.raises(ValidationError):
        CandidateProposal(
            intent="mutate c001",
            parent_candidate_ids=["c001"],  # type: ignore[call-arg]
        )


def test_agent_session_record_is_context_handle_with_required_candidate() -> None:
    session = AgentSessionRecord(
        agent_session_id="agent_001",
        run_id="run_1",
        candidate_id="c001",
        created_at="2026-06-24T00:00:00Z",
        updated_at="2026-06-24T00:00:00Z",
        workspace=Path("/tmp/c001"),
        directive={"goal": "try one direction"},
        launch={
            "agent_type": "search_candidate_agent",
            "description": "c001 try one direction",
            "prompt": "agent_session_id=agent_001; candidate_id=c001; idea: try one direction",
        },
        counters={"verifier_runs": 0},
    )
    assert session.candidate_id == "c001"
    assert session.host == "codex"
    assert session.host_handle == AgentHostHandle(host="codex")
    assert session.launch["agent_type"] == "search_candidate_agent"

    # candidate_id is now required - a subagent session without a candidate
    # has no useful role in this runtime.
    with pytest.raises(ValidationError):
        AgentSessionRecord(  # type: ignore[call-arg]
            agent_session_id="agent_002",
            run_id="run_1",
            created_at="2026-06-24T00:00:00Z",
            updated_at="2026-06-24T00:00:00Z",
            workspace=Path("/tmp/c001"),
        )


def test_search_spec_rejects_invalid_budget_and_blank_source_path() -> None:
    data = valid_spec_dict()
    data["budget"]["max_parallel"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)

    data = valid_spec_dict()
    data["source_path"] = "   "
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_models_reject_extra_fields() -> None:
    data = valid_spec_dict()
    data["unexpected"] = True

    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_shared_dir_is_opt_in_and_bounded() -> None:
    default_spec = SearchSpec.model_validate(valid_spec_dict())
    assert default_spec.shared_dir.enabled is False

    data = valid_spec_dict()
    data["shared_dir"] = {
        "enabled": True,
        "max_tools_per_iteration": 4,
        "max_files_per_iteration": 12,
        "max_path_entries_per_iteration": 96,
        "max_depth": 5,
        "max_bytes_per_iteration": 4096,
    }
    spec = SearchSpec.model_validate(data)
    assert spec.shared_dir.enabled is True
    assert spec.shared_dir.max_tools_per_iteration == 4
    assert spec.shared_dir.max_files_per_iteration == 12
    assert spec.shared_dir.max_path_entries_per_iteration == 96
    assert spec.shared_dir.max_depth == 5
    assert spec.shared_dir.max_bytes_per_iteration == 4096

    data["shared_dir"]["max_files_per_iteration"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)

    data["shared_dir"]["max_files_per_iteration"] = 12
    data["shared_dir"]["max_depth"] = 0
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(data)


def test_candidate_record_rejects_submitted_status() -> None:
    task = CandidateTask(
        run_id="run_1",
        candidate_id="c001",
        hypothesis="try one",
        workspace=Path("/tmp/c001"),
        allowed_files=["initial_program.py"],
        denied_files=["evaluator.py"],
    )

    with pytest.raises(ValidationError):
        CandidateRecord(
            candidate_id="c001",
            status="submitted",  # type: ignore[arg-type]
            task=task,
        )


def test_candidate_record_accepts_created_and_evaluated() -> None:
    task = CandidateTask(
        run_id="run_1",
        candidate_id="c001",
        hypothesis="try one",
        workspace=Path("/tmp/c001"),
        allowed_files=["initial_program.py"],
        denied_files=["evaluator.py"],
    )

    for status in ("created", "evaluated", "failed"):
        CandidateRecord(
            candidate_id="c001",
            status=status,
            task=task,
        )

from __future__ import annotations

from io import StringIO

import pytest

import goal_plus.agent_hosts as agent_hosts
from goal_plus.agent_hosts import (
    UnsupportedHostCapability,
    get_agent_host_adapter,
    portable_strategy_mode,
)


def test_json_line_reader_skips_noise_until_matching_response() -> None:
    class FakeProcess:
        stdout = StringIO(
            'not-json\n{"id": "skip"}\n{"id": "target", "result": {}}\n'
        )

    response = agent_hosts._read_json_line_until(
        FakeProcess(),  # type: ignore[arg-type]
        lambda payload: payload.get("id") == "target",
        timeout_seconds=1,
    )

    assert response == {"id": "target", "result": {}}


def test_get_agent_host_adapter_returns_all_supported_hosts() -> None:
    assert get_agent_host_adapter("codex").name == "codex"
    assert get_agent_host_adapter("pi-rpc").name == "pi-rpc"
    assert get_agent_host_adapter("pi-thinkthread").name == "pi-thinkthread"
    assert get_agent_host_adapter("codex").capabilities.supports_model_discovery
    assert get_agent_host_adapter("pi-rpc").capabilities.supports_model_discovery
    assert get_agent_host_adapter(
        "pi-thinkthread"
    ).capabilities.supports_model_discovery


@pytest.mark.pi
def test_pi_thinkthread_lists_profile_delegated_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def preflight(self):
            return {}

        def self_view(self):
            return {
                "profiles": [
                    {
                        "alias": "self",
                        "modelCatalogRevision": "catalog-7",
                        "allowedModels": [
                            {"provider": "openai-codex", "model": "gpt-5.6-terra"}
                        ],
                    }
                ]
            }

    adapter = get_agent_host_adapter("pi-thinkthread")
    monkeypatch.setattr(adapter, "_client", lambda: FakeClient())

    assert adapter.list_available_models("terra") == [
        {
            "model": "openai-codex/gpt-5.6-terra",
            "model_id": "gpt-5.6-terra",
            "provider": "openai-codex",
            "display_name": "gpt-5.6-terra",
            "reasoning": None,
            "input_modalities": ["text"],
            "source": "thinkthread_agent_posix_self",
            "profile_aliases": ["self"],
            "model_catalog_revisions": ["catalog-7"],
        }
    ]


@pytest.mark.pi
def test_pi_thinkthread_launch_and_retained_continuation() -> None:
    adapter = get_agent_host_adapter("pi-thinkthread")
    launch = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        root="/runtime/goal-plus",
        worker_budget={"max_runtime_seconds": 300, "on_exceed": "interrupt"},
        worker_launch={"model": "openai-codex/gpt-5.6-terra"},
    )

    assert launch["tool"] == "pi_thinkthread_child"
    assert launch["fs"] == "private"
    assert launch["capabilities"] == ["thinkthread.message"]
    assert launch["model"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-terra",
    }
    assert "workspace" not in launch
    assert "cwd" not in launch

    continued = adapter.build_continue_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        external_id="tt-child-1",
        task_name=None,
        short_intent="continue",
        one_paragraph_idea="continue",
        root="/runtime/goal-plus",
    )
    assert continued["thinkthread_id"] == "tt-child-1"
    assert continued["wake"] is True
    assert continued["continuation"] == "retained_child_session"


@pytest.mark.pi
@pytest.mark.parametrize("field", ["reasoning_effort", "service_tier"])
def test_pi_thinkthread_rejects_unsupported_launch_fields(field: str) -> None:
    adapter = get_agent_host_adapter("pi-thinkthread")

    with pytest.raises(UnsupportedHostCapability, match=field):
        adapter.build_launch_payload(
            worker_agent_type=None,
            candidate_id="c001",
            agent_session_id="agent_0001",
            short_intent="try",
            one_paragraph_idea="try",
            worker_launch={field: "high"},
        )


@pytest.mark.codex
def test_codex_adapter_lists_models_from_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    process = object()
    responses = iter(
        [
            {"id": "goal-plus-initialize", "result": {}},
            {
                "id": "goal-plus-model-list",
                "result": {
                    "data": [
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "displayName": "GPT-5.6 Sol",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "high"}
                            ],
                            "inputModalities": ["text", "image"],
                        }
                    ]
                },
            },
        ]
    )
    monkeypatch.setattr(agent_hosts.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent_hosts, "_send_json_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent_hosts,
        "_read_json_line_until",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(agent_hosts, "_stop_probe_process", lambda process: None)

    models = get_agent_host_adapter("codex").list_available_models("sol")

    assert models == [
        {
            "model": "gpt-5.6-sol",
            "model_id": "gpt-5.6-sol",
            "provider": "codex",
            "display_name": "GPT-5.6 Sol",
            "reasoning": True,
            "reasoning_efforts": ["high"],
            "input_modalities": ["text", "image"],
            "source": "codex_app_server_model_list",
        }
    ]


@pytest.mark.pi
def test_pi_adapter_lists_models_from_native_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    process = object()
    monkeypatch.setattr(agent_hosts.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent_hosts, "_send_json_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent_hosts,
        "_read_json_line_until",
        lambda *args, **kwargs: {
            "id": "goal-plus-model-list",
            "type": "response",
            "success": True,
            "data": {
                "models": [
                    {
                        "provider": "openai-codex",
                        "id": "gpt-5.6-terra",
                        "name": "GPT-5.6 Terra",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 272000,
                        "maxTokens": 128000,
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(agent_hosts, "_stop_probe_process", lambda process: None)

    models = get_agent_host_adapter("pi-rpc").list_available_models("terra")

    assert models == [
        {
            "model": "openai-codex/gpt-5.6-terra",
            "model_id": "gpt-5.6-terra",
            "provider": "openai-codex",
            "display_name": "GPT-5.6 Terra",
            "reasoning": True,
            "input_modalities": ["text", "image"],
            "context_window": 272000,
            "max_tokens": 128000,
            "source": "pi_rpc_get_available_models",
        }
    ]


def test_portable_strategy_mode_classifies_all_builtin_aliases() -> None:
    portable = (
        "agent_guided",
        "agent",
        "default",
        "random",
        "random-mode",
        "random_mode",
    )
    for name in portable:
        assert portable_strategy_mode(name) is True
    for name in ("independent_branches", "evolve", "openevolve", "mcts"):
        assert portable_strategy_mode(name) is False


@pytest.mark.codex
def test_codex_adapter_builds_foreground_spawn_payload() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    assert payload["tool"] == "spawn_agent"
    assert payload["agent_type"] == "default"
    assert payload["fork_turns"] == "none"
    assert payload["task_name"] == "search_agent_0001"
    assert "agent_session_id=agent-0001" in payload["message"]
    assert "assigned_worker_budget=host 默认值" in payload["message"]


@pytest.mark.codex
def test_codex_launch_maps_candidate_contract_to_builtin_default_role() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    message = payload["message"]
    # Codex's built-in default role has no config layer, so it preserves the
    # inherited parent model. The project search role would reload config after
    # inheritance and can clear a runtime-only model before tier validation.
    assert payload["agent_type"] == "default"
    assert "候选 worker，不是搜索编排器" in message
    assert "search_get_agent_context" in message
    assert "search_run_verifier" in message
    assert "search_plan_next" in message
    assert "search_start_batch" in message
    assert "search_select" in message
    assert "search_report" in message
    assert "search_promote" in message
    assert "不要调用任何 `goal_plus_*` 工具" in message
    assert "不得直接运行任务自带的 `runner`、`evaluator` 或 `grader`" in message
    assert "所有正确性与指标反馈必须通过 `search_run_verifier`" in message


@pytest.mark.codex
def test_codex_launch_preserves_explicit_nondefault_agent_type() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type="search_candidate_agent_deep",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
    )

    assert payload["agent_type"] == "search_candidate_agent_deep"


@pytest.mark.codex
def test_codex_launch_and_continue_embed_full_worker_prompt() -> None:
    adapter = get_agent_host_adapter("codex")
    worker_prompt = "FULL WORKER CONTRACT: write .tmp/handoff.json before returning."

    launch = adapter.build_launch_payload(
        worker_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_prompt=worker_prompt,
    )
    continued = adapter.build_continue_payload(
        worker_agent_type="search_candidate_agent",
        candidate_id="c001",
        agent_session_id="agent-0001",
        external_id=None,
        task_name=launch["task_name"],
        short_intent="continue",
        one_paragraph_idea="continue",
        worker_prompt=worker_prompt,
    )

    assert worker_prompt in launch["message"]
    assert worker_prompt in continued["message"]
    assert "候选 worker，不是搜索编排器" in launch["message"]
    assert "候选 worker，不是搜索编排器" in continued["message"]


@pytest.mark.codex
def test_codex_adapter_maps_native_worker_launch_options() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_launch={
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "service_tier": "priority",
        },
    )

    assert payload["model"] == "gpt-5.6-terra"
    assert payload["reasoning_effort"] == "high"
    assert payload["service_tier"] == "priority"


@pytest.mark.codex
def test_codex_adapter_builds_watchdog_budget_payload() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
    )

    assert payload["budget_control"] == {
        "mode": "parent_watchdog",
        "max_runtime_seconds": 600,
        "initial_wait_timeout_ms": 555000,
        "soft_closeout_seconds": 45,
        "closeout_tool": "send_message",
        "closeout_target": "search_agent_0001",
        "closeout_message": (
            "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
            "search_run_verifier，写入 .tmp/handoff.json，并返回简洁摘要。"
        ),
        "final_wait_timeout_ms": 45000,
        "on_exceed": "interrupt",
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": "search_agent_0001",
        "max_turns_hint": 8,
    }


@pytest.mark.codex
def test_codex_adapter_separates_autoresearch_lease_from_parent_watchdog() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="cand-0001",
        agent_session_id="agent-0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 1,
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )

    control = payload["budget_control"]
    assert control["autoresearch_lease"] == {
        "mode": "subagent_stop",
        "min_runtime_seconds": 300,
        "min_verifier_runs": 1,
        "start_event": "native_child_session",
        "release_before_parent_closeout": True,
    }
    assert control["initial_wait_timeout_ms"] == 375000
    assert control["final_wait_timeout_ms"] == 45000
    assert 300 < control["initial_wait_timeout_ms"] / 1000 < 420


@pytest.mark.codex
def test_codex_adapter_rejects_lease_overlapping_parent_closeout() -> None:
    adapter = get_agent_host_adapter("codex")

    with pytest.raises(ValueError, match="before the parent watchdog soft-closeout"):
        adapter.build_launch_payload(
            worker_agent_type=None,
            candidate_id="cand-0001",
            agent_session_id="agent-0001",
            short_intent="try",
            one_paragraph_idea="try",
            worker_budget={
                "min_runtime_seconds": 360,
                "max_runtime_seconds": 400,
                "on_exceed": "interrupt",
            },
        )


@pytest.mark.pi
def test_pi_rpc_adapter_builds_worker_payload() -> None:
    adapter = get_agent_host_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "max_runtime_seconds": 600,
            "max_turns": 8,
            "on_exceed": "interrupt",
        },
        root="/tmp/project/.search",
        cwd="/tmp/project/.search/runs/run_1/candidates/c001/workspace",
        worker_prompt="first call search_get_agent_context",
    )

    assert payload["tool"] == "pi_rpc_worker"
    assert payload["agent_session_id"] == "agent_0001"
    assert payload["candidate_id"] == "c001"
    assert payload["root"] == "/tmp/project/.search"
    assert payload["cwd"].endswith("/c001/workspace")
    assert "session_dir" not in payload
    assert payload["continuation"] == "native_session"
    assert payload["session_persistence"] == "cross_process"
    assert "search_get_agent_context" in payload["prompt"]
    assert "agent_session_id=agent_0001" in payload["prompt"]
    assert "assigned_worker_budget={'max_runtime_seconds': 600" in payload["prompt"]
    assert payload["budget_control"] == {
        "mode": "pi_rpc_process_watchdog",
        "continuation": "native_session",
        "max_runtime_seconds": 600,
        "max_turns_hint": 8,
        "soft_closeout_seconds": 45,
        "on_exceed": "interrupt",
    }


@pytest.mark.pi
def test_pi_rpc_adapter_exposes_supervisor_autoresearch_lease() -> None:
    adapter = get_agent_host_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_budget={
            "min_runtime_seconds": 300,
            "min_verifier_runs": 2,
            "max_runtime_seconds": 420,
            "on_exceed": "interrupt",
        },
    )

    assert payload["budget_control"]["autoresearch_lease"] == {
        "mode": "pool_supervisor",
        "min_runtime_seconds": 300,
        "min_verifier_runs": 2,
        "start_event": "initial_pool_dispatch",
        "cumulative_across_dispatches": True,
    }
    assert payload["budget_control"]["soft_closeout_seconds"] == 120


@pytest.mark.pi
def test_pi_rpc_adapter_maps_native_worker_launch_options() -> None:
    adapter = get_agent_host_adapter("pi-rpc")

    payload = adapter.build_launch_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        short_intent="try",
        one_paragraph_idea="try",
        worker_launch={
            "model": "openai-codex/gpt-5.6-sol",
            "reasoning_effort": "high",
        },
    )

    assert payload["model_pattern"] == "openai-codex/gpt-5.6-sol"
    assert payload["thinking_level"] == "high"


@pytest.mark.pi
def test_pi_rpc_adapter_builds_cross_process_native_session_continuation() -> None:
    adapter = get_agent_host_adapter("pi-rpc")

    payload = adapter.build_continue_payload(
        worker_agent_type=None,
        candidate_id="c001",
        agent_session_id="agent_0001",
        external_id="agent_0001",
        task_name=None,
        short_intent="continue",
        one_paragraph_idea="continue from runtime context",
        root="/tmp/project/.search",
        cwd="/tmp/project/.search/runs/run_1/candidates/c001/workspace",
        worker_prompt="first call search_get_agent_context",
        host_metadata={
            "pi_metrics": {
                "final_last_entry_id": "entry_7",
                "final_entry_count": 7,
                "usage_total": {"input": 120},
                "duration_seconds": 3.5,
                "started_at": "2026-07-19T00:00:00Z",
            }
        },
    )

    assert payload["agent_session_id"] == "agent_0001"
    assert payload["session_id"] == "agent_0001"
    assert payload["continuation"] == "native_session"
    assert payload["session_persistence"] == "cross_process"
    assert payload["metrics_baseline"] == {
        "last_entry_id": "entry_7",
        "entry_count": 7,
        "usage_total": {"input": 120},
        "duration_seconds": 3.5,
        "started_at": "2026-07-19T00:00:00Z",
    }
    assert "continue_existing_agent_session=true" in payload["prompt"]
    assert "这条 launch 消息开始一次新的 host 派发" in payload["prompt"]
    assert "属于上一次派发，已不再生效" in payload["prompt"]
    assert "公开指标达到上限" in payload["prompt"]
    assert "至少完成一个实质性" in payload["prompt"]
    assert "同分保留或回滚的 Evidence 仍有信息价值" in payload["prompt"]


@pytest.mark.codex
def test_codex_continue_uses_followup_task_with_watchdog() -> None:
    adapter = get_agent_host_adapter("codex")

    payload = adapter.build_continue_payload(
        worker_agent_type="search_candidate_agent",
        candidate_id="cand_0001",
        agent_session_id="agent_0001",
        external_id=None,
        task_name="search_agent_0001",
        short_intent="continue",
        one_paragraph_idea="continue",
        worker_budget={"max_runtime_seconds": 900, "on_exceed": "interrupt"},
    )

    assert payload["tool"] == "followup_task"
    assert payload["target"] == "search_agent_0001"
    assert "continue_existing_agent_session=true" in payload["message"]
    assert "Global Evidence 的定期刷新节奏" in payload["message"]
    assert payload["budget_control"]["max_runtime_seconds"] == 900
    assert payload["budget_control"]["interrupt_target"] == "search_agent_0001"


@pytest.mark.codex
def test_codex_continue_requires_a_bound_native_handle() -> None:
    adapter = get_agent_host_adapter("codex")

    with pytest.raises(UnsupportedHostCapability, match="bound task name"):
        adapter.build_continue_payload(
            worker_agent_type="search_candidate_agent",
            candidate_id="cand_0001",
            agent_session_id="agent_0001",
            external_id=None,
            task_name=None,
            short_intent="continue",
            one_paragraph_idea="continue",
        )

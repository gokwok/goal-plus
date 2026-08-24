from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, Protocol

from goal_plus.agent_pool import HostPoolContract
from goal_plus.host_observability import (
    collect_codex_observability,
    collect_pi_observability,
)
from goal_plus.models import AgentHostKind, AgentSessionRecord
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
)


PORTABLE_STRATEGY_MODES = {
    "agent",
    "agent_guided",
    "default",
    "random",
    "random_mode",
}


class UnsupportedHostCapability(RuntimeError):
    """Raised when a host cannot provide a requested worker lifecycle action."""


@dataclass(frozen=True)
class HostCapabilities:
    supports_soft_closeout: bool = False
    supports_model_discovery: bool = False
    supports_model_override: bool = False
    supports_reasoning_effort: bool = False
    supports_service_tier: bool = False
    supports_usage_metadata: bool = False
    supports_process_kill: bool = False
    pool: HostPoolContract = field(default_factory=HostPoolContract)


class AgentHostAdapter(Protocol):
    name: AgentHostKind
    adapter_version: str
    capabilities: HostCapabilities

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def collect_observability(
        self,
        session: AgentSessionRecord,
    ) -> dict[str, Any]:
        ...

    def build_launch_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        ...

    def build_continue_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


def _normalize_mode(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def portable_strategy_mode(value: str) -> bool:
    return _normalize_mode(value) in PORTABLE_STRATEGY_MODES


def _send_json_line(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("host model discovery process has no stdin")
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _read_json_line_until(
    process: subprocess.Popen[str],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("host model discovery process has no stdout")

    responses: Queue[dict[str, Any] | Exception | None] = Queue(maxsize=1)

    def read_until_match() -> None:
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    responses.put(None)
                    return
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and predicate(payload):
                    responses.put(payload)
                    return
        except Exception as exc:  # pragma: no cover - defensive subprocess guard
            responses.put(exc)

    Thread(target=read_until_match, daemon=True).start()
    try:
        response = responses.get(timeout=timeout_seconds)
    except Empty as exc:
        raise RuntimeError(
            "host model discovery timed out or exited without a response"
        ) from exc
    if response is None:
        raise RuntimeError("host model discovery exited without a response")
    if isinstance(response, Exception):
        raise RuntimeError("host model discovery response reader failed") from response
    return response


def _stop_probe_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _filter_available_models(
    models: list[dict[str, Any]], query: str | None
) -> list[dict[str, Any]]:
    normalized = (query or "").strip().casefold()
    if not normalized:
        return models
    return [
        model
        for model in models
        if normalized
        in " ".join(
            str(model.get(key) or "")
            for key in ("model", "model_id", "provider", "display_name")
        ).casefold()
    ]


def _codex_task_name(agent_session_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", agent_session_id.lower()).strip("_")
    return f"search_{normalized or 'agent'}"


def _budget_max_runtime_ms(worker_budget: dict[str, Any]) -> int | None:
    seconds = worker_budget.get("max_runtime_seconds")
    if seconds is None:
        return None
    return int(seconds) * 1000


def _soft_closeout_seconds(max_runtime_seconds: int) -> int:
    return min(45, max(5, int(max_runtime_seconds) // 5))


def _pi_soft_closeout_seconds(
    max_runtime_seconds: int,
    min_runtime_seconds: int | None,
) -> int:
    if min_runtime_seconds:
        return max(1, max_runtime_seconds - min_runtime_seconds)
    return _soft_closeout_seconds(max_runtime_seconds)


CODEX_CLOSEOUT_MESSAGE = (
    "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
    "search_run_verifier，写入 .tmp/handoff.json，并返回简洁摘要。"
)

CODEX_WORKER_BOUNDARY = (
    "你是 Search 候选 worker，不是搜索编排器。首先使用提供的 agent_session_id 调用 "
    "search_get_agent_context。首次修改前调用 search_get_global_evidence；此后每完成 3 次 "
    "search_run_verifier iteration 刷新一次，连续两轮没有提升或切换技术路线时提前刷新；"
    "verifier 已注入的 global_evidence_snapshot 算作刷新。独立思考后编辑，"
    "并为该 agent session 调用 search_run_verifier，同时用一句话 hypothesis 概括实际尝试。"
    "不得直接运行任务自带的 `runner`、`evaluator` 或 `grader`，也不得直接执行或导入冻结 "
    "verifier 命令来获取 score、pass/fail 或 correctness。所有正确性与指标反馈必须通过 "
    "`search_run_verifier`，使运行时记录并结算 Evidence。可以进行不返回任务分数或通关判定的"
    "编译、lint、静态分析和局部调试，但这些结果不能替代 verifier Evidence。"
    "只在该候选工作区中工作。不要调用 search_plan_next、search_start_batch、"
    "search_select、search_report 或 search_promote。不要调用任何 `goal_plus_*` 工具。"
    "父级运行的规划、选择、报告、提升和最终审计不属于你的职责。如果 verifier 返回 "
    "failure_class=VerifierWorkspaceSideEffect 或 candidate_action=stop_and_report，"
    "不要清理 verifier 输出或重试；记录基础设施阻塞原因并立即返回。"
    "process verifier 返回 keep/retain/discard/failure disposition；严格改善为 keep，同分为 retain 并成为最新基线，只有退化或失败才恢复 candidate-local best；"
    "不要自行 reset、restore 或 checkout verifier-backed 状态。"
)


def _codex_worker_contract(worker_prompt: str | None) -> str:
    """Keep the portable worker boundary even when agent metadata is hidden."""
    prompt = (worker_prompt or "").strip()
    if not prompt or prompt == CODEX_WORKER_BOUNDARY:
        return CODEX_WORKER_BOUNDARY
    return f"{CODEX_WORKER_BOUNDARY}\n\n{prompt}"


def _codex_budget_control(
    target: str,
    worker_budget: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not worker_budget:
        return None
    max_runtime_seconds = worker_budget.get("max_runtime_seconds")
    budget_control: dict[str, Any] = {
        "mode": "parent_watchdog",
        "max_runtime_seconds": max_runtime_seconds,
        "on_exceed": worker_budget.get("on_exceed", "interrupt"),
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": target,
    }
    max_runtime_ms = _budget_max_runtime_ms(worker_budget)
    if max_runtime_seconds is not None and max_runtime_ms is not None:
        soft_closeout_seconds = _soft_closeout_seconds(int(max_runtime_seconds))
        final_wait_timeout_ms = soft_closeout_seconds * 1000
        min_runtime_seconds = worker_budget.get("min_runtime_seconds")
        if (
            min_runtime_seconds is not None
            and int(min_runtime_seconds)
            >= int(max_runtime_seconds) - soft_closeout_seconds
        ):
            raise ValueError(
                "codex worker_budget.min_runtime_seconds must end before the "
                "parent watchdog soft-closeout point; increase "
                "max_runtime_seconds to reserve worker closeout time"
            )
        budget_control.update(
            {
                "initial_wait_timeout_ms": max_runtime_ms - final_wait_timeout_ms,
                "soft_closeout_seconds": soft_closeout_seconds,
                "closeout_tool": "send_message",
                "closeout_target": target,
                "closeout_message": CODEX_CLOSEOUT_MESSAGE,
                "final_wait_timeout_ms": final_wait_timeout_ms,
            }
        )
        if min_runtime_seconds is not None or worker_budget.get(
            "min_verifier_runs"
        ) is not None:
            budget_control["autoresearch_lease"] = {
                "mode": "subagent_stop",
                "min_runtime_seconds": int(min_runtime_seconds or 0),
                "min_verifier_runs": int(
                    worker_budget.get("min_verifier_runs") or 1
                ),
                "start_event": "native_child_session",
                "release_before_parent_closeout": True,
            }
    if worker_budget.get("max_turns") is not None:
        budget_control["max_turns_hint"] = worker_budget["max_turns"]
    return budget_control


class CodexAdapter:
    name: AgentHostKind = "codex"
    adapter_version = "codex-app-server-v1"
    capabilities = HostCapabilities(
        supports_soft_closeout=True,
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_service_tier=True,
        supports_usage_metadata=True,
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="same_worker",
            deadline_mode="parent_watchdog",
            recovery_mode="host_resident",
            completion_stage="candidate_ready",
            submit_tool="spawn_agent",
            wait_tool="wait_agent",
            snapshot_tool="list_agents",
            continue_tool="followup_task",
            closeout_tool="send_message",
            interrupt_tool="interrupt_agent",
        ),
    )

    def collect_observability(self, session: AgentSessionRecord) -> dict[str, Any]:
        return collect_codex_observability(session)

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            process = subprocess.Popen(
                ["codex", "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise UnsupportedHostCapability(
                "codex model discovery requires the `codex` executable"
            ) from exc
        try:
            _send_json_line(
                process,
                {
                    "method": "initialize",
                    "id": "goal-plus-initialize",
                    "params": {
                        "clientInfo": {
                            "name": "goal_plus",
                            "title": "Goal Plus",
                            "version": self.adapter_version,
                        }
                    },
                },
            )
            initialized = _read_json_line_until(
                process,
                lambda payload: payload.get("id") == "goal-plus-initialize",
            )
            if initialized.get("error") is not None:
                raise RuntimeError(
                    f"codex app-server initialize failed: {initialized['error']}"
                )
            _send_json_line(process, {"method": "initialized", "params": {}})
            _send_json_line(
                process,
                {
                    "method": "model/list",
                    "id": "goal-plus-model-list",
                    "params": {"limit": 100, "includeHidden": False},
                },
            )
            response = _read_json_line_until(
                process,
                lambda payload: payload.get("id") == "goal-plus-model-list",
            )
            if response.get("error") is not None:
                raise RuntimeError(
                    f"codex model/list failed: {response['error']}"
                )
            data = response.get("result", {}).get("data", [])
            models = [
                {
                    "model": str(item.get("model") or item["id"]),
                    "model_id": str(item["id"]),
                    "provider": "codex",
                    "display_name": item.get("displayName") or item.get("id"),
                    "reasoning": bool(item.get("supportedReasoningEfforts")),
                    "reasoning_efforts": [
                        effort.get("reasoningEffort")
                        for effort in item.get("supportedReasoningEfforts", [])
                        if effort.get("reasoningEffort")
                    ],
                    "input_modalities": item.get("inputModalities")
                    or ["text", "image"],
                    "source": "codex_app_server_model_list",
                }
                for item in data
                if isinstance(item, dict) and item.get("id")
            ]
            return _filter_available_models(models, query)
        finally:
            _stop_probe_process(process)

    def build_launch_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        task_name = _codex_task_name(agent_session_id)
        worker_contract = _codex_worker_contract(worker_prompt)
        payload = {
            "tool": "spawn_agent",
            "task_name": task_name,
            "agent_type": "default",
            "fork_turns": "none",
            "message": (
                f"{worker_contract}\n\n"
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
                f"思路：{one_paragraph_idea}"
            ),
        }
        # The default worker contract is already embedded in ``message``. Map
        # it to Codex's built-in no-config role: selecting the project-local
        # role reloads config after model inheritance and can discard a
        # runtime-only parent model before service-tier validation. An explicit
        # ``default`` also prevents the orchestrator from inventing that custom
        # role when projecting the returned payload. Non-default roles remain
        # an explicit opt-in.
        if worker_agent_type and worker_agent_type != "search_candidate_agent":
            payload["agent_type"] = worker_agent_type
        if worker_launch:
            payload.update(
                {
                    key: value
                    for key, value in worker_launch.items()
                    if key in {"model", "reasoning_effort", "service_tier"}
                    and value is not None
                }
            )
        budget_control = _codex_budget_control(task_name, worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = task_name or external_id
        if not target:
            raise UnsupportedHostCapability(
                "codex continuation requires a bound task name or agent id"
            )
        worker_contract = _codex_worker_contract(worker_prompt)
        payload: dict[str, Any] = {
            "tool": "followup_task",
            "target": target,
            "message": (
                f"{worker_contract}\n\n"
                "continue_existing_agent_session=true; "
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                "刷新 search_get_agent_context，并继续遵循 Global Evidence 的定期刷新节奏；"
                f"继续同一个 candidate 和 workspace；指令：{one_paragraph_idea}"
            ),
        }
        budget_control = _codex_budget_control(target, worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class PiRpcAdapter:
    name: AgentHostKind = "pi-rpc"
    adapter_version = "pi-rpc-v1"
    capabilities = HostCapabilities(
        supports_soft_closeout=True,
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_usage_metadata=True,
        supports_process_kill=True,
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="native_session",
            deadline_mode="worker_watchdog",
            recovery_mode="supervisor_persisted",
            completion_stage="candidate_ready",
            open_tool="pi_search_pool_open",
            wait_tool="pi_search_pool_wait_any",
            snapshot_tool="pi_search_pool_snapshot",
            continue_tool="pi_search_pool_continue",
            closeout_tool="pi_search_pool_close",
            interrupt_tool="pi_search_pool_close",
        ),
    )

    def collect_observability(self, session: AgentSessionRecord) -> dict[str, Any]:
        return collect_pi_observability(session)

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            process = subprocess.Popen(
                [
                    "pi",
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise UnsupportedHostCapability(
                "pi-rpc model discovery requires the `pi` executable"
            ) from exc
        try:
            _send_json_line(
                process,
                {"id": "goal-plus-model-list", "type": "get_available_models"},
            )
            response = _read_json_line_until(
                process,
                lambda payload: (
                    payload.get("id") == "goal-plus-model-list"
                    and payload.get("type") == "response"
                ),
            )
            if not response.get("success"):
                raise RuntimeError(
                    "pi get_available_models failed: "
                    + str(response.get("error") or response)
                )
            data = response.get("data", {}).get("models", [])
            models = [
                {
                    "model": f"{item['provider']}/{item['id']}",
                    "model_id": str(item["id"]),
                    "provider": str(item["provider"]),
                    "display_name": item.get("name") or item.get("id"),
                    "reasoning": bool(item.get("reasoning")),
                    "input_modalities": item.get("input") or ["text"],
                    "context_window": item.get("contextWindow"),
                    "max_tokens": item.get("maxTokens"),
                    "source": "pi_rpc_get_available_models",
                }
                for item in data
                if isinstance(item, dict) and item.get("provider") and item.get("id")
            ]
            return _filter_available_models(models, query)
        finally:
            _stop_probe_process(process)

    def _budget_control(
        self,
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not worker_budget:
            return None
        max_runtime_seconds = worker_budget.get("max_runtime_seconds")
        min_runtime_seconds = worker_budget.get("min_runtime_seconds")
        budget_control: dict[str, Any] = {
            "mode": "pi_rpc_process_watchdog",
            "continuation": "native_session",
            "max_runtime_seconds": max_runtime_seconds,
            "on_exceed": worker_budget.get("on_exceed", "interrupt"),
        }
        if max_runtime_seconds is not None:
            budget_control["soft_closeout_seconds"] = _pi_soft_closeout_seconds(
                int(max_runtime_seconds),
                int(min_runtime_seconds) if min_runtime_seconds is not None else None,
            )
        min_verifier_runs = worker_budget.get("min_verifier_runs")
        if min_runtime_seconds is not None or min_verifier_runs is not None:
            budget_control["autoresearch_lease"] = {
                "mode": "pool_supervisor",
                "min_runtime_seconds": int(min_runtime_seconds or 0),
                "min_verifier_runs": int(min_verifier_runs or 1),
                "start_event": "initial_pool_dispatch",
                "cumulative_across_dispatches": True,
            }
        if worker_budget.get("max_turns") is not None:
            budget_control["max_turns_hint"] = worker_budget["max_turns"]
        return budget_control

    def _base_prompt(
        self,
        *,
        worker_prompt: str | None,
        agent_session_id: str,
        candidate_id: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> str:
        header = (worker_prompt or "首先调用 search_get_agent_context。").strip()
        labels = (
            f"agent_session_id={agent_session_id}; "
            f"candidate_id={candidate_id}; "
            f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
            f"思路：{one_paragraph_idea}"
        )
        if resume:
            labels = "continue_existing_agent_session=true; " + labels
            header += (
                "\n\n这条 launch 消息开始一次新的 host 派发。原生会话中更早的 deadline、"
                "closeout 或 time-advisory 消息属于上一次派发，已不再生效。"
                "只遵守本次 launch 之后收到的警告。"
                "在收到本次 closeout 或 deadline 警告前，不要仅因公开指标达到上限、"
                "当前没有未验证改动或出现同分而结束本次派发。刷新运行时证据后，"
                "至少完成一个实质性的泛化、反例、结构边界或简化 probe 并用 verifier "
                "验证；同分保留或回滚的 Evidence 仍有信息价值。"
            )
        return f"{header}\n\nLaunch 标签：{labels}"

    def build_launch_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": "pi_rpc_worker",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": agent_session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "cwd": cwd or ".",
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "session_persistence": "cross_process",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
            ),
        }
        if worker_agent_type:
            payload["worker_agent_type"] = worker_agent_type
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = (external_id or "").strip()
        if not session_id:
            raise UnsupportedHostCapability(
                "pi-rpc continuation requires a bound native session id"
            )
        payload: dict[str, Any] = {
            "tool": "pi_rpc_worker",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "cwd": cwd or ".",
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "session_persistence": "cross_process",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=True,
            ),
        }
        if worker_agent_type:
            payload["worker_agent_type"] = worker_agent_type
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        metrics = (host_metadata or {}).get("pi_metrics")
        if isinstance(metrics, dict):
            payload["metrics_baseline"] = {
                "last_entry_id": metrics.get("final_last_entry_id"),
                "entry_count": metrics.get("final_entry_count"),
                "usage_total": metrics.get("usage_total"),
                "duration_seconds": metrics.get("duration_seconds"),
                "started_at": metrics.get("started_at"),
            }
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class PiThinkThreadAdapter:
    name: AgentHostKind = "pi-thinkthread"
    adapter_version = "pi-thinkthread-agent-posix-v2"
    capabilities = HostCapabilities(
        supports_soft_closeout=True,
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=False,
        supports_service_tier=False,
        supports_usage_metadata=True,
        supports_process_kill=True,
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="native_session",
            deadline_mode="worker_watchdog",
            recovery_mode="supervisor_persisted",
            completion_stage="candidate_ready",
            open_tool="pi_search_pool_open",
            wait_tool="pi_search_pool_wait_any",
            snapshot_tool="pi_search_pool_snapshot",
            continue_tool="pi_search_pool_continue",
            closeout_tool="pi_search_pool_close",
            interrupt_tool="pi_search_pool_close",
        ),
    )

    @staticmethod
    def _client() -> AgentPosixSdkClient:
        return AgentPosixSdkClient()

    def collect_observability(self, session: AgentSessionRecord) -> dict[str, Any]:
        child_id = session.host_handle.external_id
        if not child_id:
            return {
                "host": self.name,
                "available": False,
                "reason": "agent session is not bound to a ThinkThread Child",
            }
        try:
            child = self._client().invoke("thinkthread.get", {"id": child_id})
        except AgentPosixBridgeError as exc:
            return {
                "host": self.name,
                "available": False,
                "reason": str(exc),
                "error_code": exc.code,
            }
        return {
            "host": self.name,
            "available": True,
            "thinkthread_id": child.get("thinkthreadId"),
            "agent_state": child.get("agentState"),
            "execution_state": child.get("executionState"),
            "pending_wake": child.get("pendingWake"),
            "completion": child.get("completion"),
            "last_wake_outcome": child.get("lastWakeOutcome"),
            "model": child.get("model"),
            "fs": child.get("fs"),
        }

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        client = self._client()
        client.preflight()
        self_view = client.self_view()
        profiles = self_view.get("profiles")
        if not isinstance(profiles, list):
            raise AgentPosixBridgeError("Agent POSIX self view omitted profiles")
        discovered: dict[str, dict[str, Any]] = {}
        for raw_profile in profiles:
            if not isinstance(raw_profile, dict):
                continue
            alias = raw_profile.get("alias")
            revision = raw_profile.get("modelCatalogRevision")
            allowed = raw_profile.get("allowedModels")
            if not isinstance(allowed, list):
                continue
            for raw_model in allowed:
                if not isinstance(raw_model, dict):
                    continue
                provider = raw_model.get("provider")
                model_id = raw_model.get("model")
                if not isinstance(provider, str) or not isinstance(model_id, str):
                    continue
                exact_ref = f"{provider}/{model_id}"
                entry = discovered.setdefault(
                    exact_ref,
                    {
                        "model": exact_ref,
                        "model_id": model_id,
                        "provider": provider,
                        "display_name": model_id,
                        "reasoning": None,
                        "input_modalities": ["text"],
                        "source": "thinkthread_agent_posix_self",
                        "profile_aliases": [],
                        "model_catalog_revisions": [],
                    },
                )
                if isinstance(alias, str) and alias not in entry["profile_aliases"]:
                    entry["profile_aliases"].append(alias)
                if (
                    isinstance(revision, str)
                    and revision not in entry["model_catalog_revisions"]
                ):
                    entry["model_catalog_revisions"].append(revision)
        models = [discovered[key] for key in sorted(discovered)]
        return _filter_available_models(models, query)

    @staticmethod
    def _validate_worker_launch(
        worker_launch: dict[str, Any] | None,
    ) -> dict[str, str] | None:
        if not worker_launch:
            return None
        for field_name in ("reasoning_effort", "service_tier"):
            if worker_launch.get(field_name) is not None:
                raise UnsupportedHostCapability(
                    f"pi-thinkthread does not support {field_name}"
                )
        model = worker_launch.get("model")
        if model is None:
            return None
        if not isinstance(model, str) or "/" not in model:
            raise UnsupportedHostCapability(
                "pi-thinkthread model selection requires exact provider/model"
            )
        provider, model_id = model.split("/", 1)
        if not provider or not model_id:
            raise UnsupportedHostCapability(
                "pi-thinkthread model selection requires exact provider/model"
            )
        return {"provider": provider, "model": model_id}

    @staticmethod
    def _budget_control(
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not worker_budget:
            return None
        max_runtime_seconds = worker_budget.get("max_runtime_seconds")
        control: dict[str, Any] = {
            "mode": "thinkthread_child_watchdog",
            "max_runtime_seconds": max_runtime_seconds,
            "on_exceed": worker_budget.get("on_exceed", "interrupt"),
            "interrupt_sequence": ["INT", "TERM"],
            "continuation": "retained_child_session",
        }
        if max_runtime_seconds is not None:
            control["soft_closeout_seconds"] = _pi_soft_closeout_seconds(
                int(max_runtime_seconds),
                int(worker_budget["min_runtime_seconds"])
                if worker_budget.get("min_runtime_seconds") is not None
                else None,
            )
        if (
            worker_budget.get("min_runtime_seconds") is not None
            or worker_budget.get("min_verifier_runs") is not None
        ):
            control["autoresearch_lease"] = {
                "mode": "thinkthread_pool_controller",
                "min_runtime_seconds": int(
                    worker_budget.get("min_runtime_seconds") or 0
                ),
                "min_verifier_runs": int(worker_budget.get("min_verifier_runs") or 1),
                "start_event": "initial_child_dispatch",
                "cumulative_across_wakes": True,
            }
        if worker_budget.get("max_turns") is not None:
            control["max_turns_hint"] = worker_budget["max_turns"]
        return control

    @staticmethod
    def _message(
        *,
        worker_prompt: str | None,
        agent_session_id: str,
        candidate_id: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None,
        resume: bool,
    ) -> str:
        header = (worker_prompt or "首先调用 search_get_agent_context。").strip()
        if resume:
            header += (
                "\n\n继续同一个 retained ThinkThread Child Session 和 private branch。"
                "刷新运行时上下文与 Global Evidence 后继续自主搜索。"
            )
        return (
            f"{header}\n\n"
            f"continue_existing_agent_session={'true' if resume else 'false'}; "
            f"agent_session_id={agent_session_id}; candidate_id={candidate_id}; "
            f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
            f"思路：{one_paragraph_idea}"
        )

    def build_launch_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "tool": "pi_thinkthread_child",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": agent_session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "description": f"{candidate_id} {short_intent}",
            "profile": "self",
            "fs": "private",
            "capabilities": ["thinkthread.message"],
            "continuation": "retained_child_session",
            "message": self._message(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=False,
            ),
        }
        if model is not None:
            payload["model"] = model
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        worker_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not external_id:
            raise UnsupportedHostCapability(
                "pi-thinkthread continuation requires a bound Child id"
            )
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "tool": "pi_thinkthread_child",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "thinkthread_id": external_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "description": f"{candidate_id} {short_intent}",
            "continuation": "retained_child_session",
            "wake": True,
            "message": self._message(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=True,
            ),
        }
        if model is not None:
            payload["model"] = model
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

_ADAPTERS: dict[AgentHostKind, AgentHostAdapter] = {
    "codex": CodexAdapter(),
    "pi-rpc": PiRpcAdapter(),
    "pi-thinkthread": PiThinkThreadAdapter(),
}


def get_agent_host_adapter(host: AgentHostKind) -> AgentHostAdapter:
    return _ADAPTERS[host]

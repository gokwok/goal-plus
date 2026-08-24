from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.pi_pool import (
    close_pi_search_pool,
    continue_pi_search_pool,
    open_pi_search_pool,
    snapshot_pi_search_pool,
    wait_any_pi_search_pool,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import GoalPlusTools, SearchTools


SEARCH_TOOL_NAMES = {
    "goal_plus_list_models",
    "search_freeze_spec",
    "search_create",
    "search_invalidate_run",
    "search_status",
    "search_recover_pi_thinkthread",
    "search_list_history",
    "search_plan_next",
    "search_start_batch",
    "search_start_agent_session",
    "search_redispatch_candidate",
    "search_bind_agent_handle",
    "search_continue_agent_session",
    "search_get_agent_context",
    "search_get_global_evidence",
    "search_stage_shared_tool",
    "search_copy_shared_tool",
    "search_get_evidence_detail",
    "search_get_agent_observability",
    "search_run_verifier",
    "search_list_iterations",
    "search_select",
    "search_report",
    "search_promote",
}

GOAL_PLUS_TOOL_NAMES = {
    "goal_plus_create",
    "goal_plus_status",
    "goal_plus_update_goal",
    "goal_plus_record_triage",
    "goal_plus_save_spec_draft",
    "goal_plus_link_search_run",
    "goal_plus_record_search_result",
    "goal_plus_prepare_final_check",
    "goal_plus_submit_final_check",
    "goal_plus_set_status",
    "goal_plus_gate",
}


def _pi_search_pool_open_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        run_id: str,
        candidate_ids: list[str] | None = None,
        worker_budgets: dict[str, dict[str, Any]] | None = None,
        final_verify: bool = True,
        max_parallel: int | None = None,
    ) -> dict[str, Any]:
        return open_pi_search_pool(
            root_dir=root_dir,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            final_verify=final_verify,
            max_parallel=max_parallel,
        )

    return call


def _pi_search_pool_wait_any_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(pool_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
        return wait_any_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
        )

    return call


def _pi_search_pool_snapshot_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return snapshot_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            run_id=run_id,
        )

    return call


def _pi_search_pool_continue_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        candidate_id: str,
        worker_budget: dict[str, Any] | None = None,
        final_verify: bool = True,
    ) -> dict[str, Any]:
        return continue_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            candidate_id=candidate_id,
            worker_budget=worker_budget,
            final_verify=final_verify,
        )

    return call


def _pi_search_pool_close_tool(root_dir: Path | str) -> Callable[..., dict[str, Any]]:
    def call(
        pool_id: str,
        mode: str = "drain",
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        return close_pi_search_pool(
            root_dir=root_dir,
            pool_id=pool_id,
            mode=mode,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
        )

    return call


def _registry(root_dir: Path | str) -> dict[str, Callable[..., Any]]:
    search_tools = SearchTools(FileSearchRuntime(root_dir))
    goal_tools = GoalPlusTools(FileGoalPlusRuntime(root_dir))
    tools: dict[str, Callable[..., Any]] = {}
    for name in SEARCH_TOOL_NAMES:
        tools[name] = getattr(search_tools, name)
    for name in GOAL_PLUS_TOOL_NAMES:
        tools[name] = getattr(goal_tools, name)
    tools["pi_search_pool_open"] = _pi_search_pool_open_tool(root_dir)
    tools["pi_search_pool_wait_any"] = _pi_search_pool_wait_any_tool(root_dir)
    tools["pi_search_pool_snapshot"] = _pi_search_pool_snapshot_tool(root_dir)
    tools["pi_search_pool_continue"] = _pi_search_pool_continue_tool(root_dir)
    tools["pi_search_pool_close"] = _pi_search_pool_close_tool(root_dir)
    tools["goal_plus_monitor_snapshot"] = search_tools.goal_plus_monitor_snapshot
    return tools


def call_pi_tool(
    root_dir: Path | str,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    tools = _registry(root_dir)
    if tool_name not in tools:
        raise ValueError(f"unsupported pi tool: {tool_name}")
    return tools[tool_name](**(args or {}))


def _read_args(args_json: str | None) -> dict[str, Any]:
    raw = args_json
    if raw is None:
        raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JSON CLI facade for Pi goal-plus extension tools."
    )
    parser.add_argument("tool", help="Tool name, e.g. search_get_agent_context")
    parser.add_argument(
        "--root",
        default=os.environ.get("GOAL_PLUS_ROOT", DEFAULT_RUNTIME_ROOT),
        help="Search runtime storage directory",
    )
    parser.add_argument(
        "--args-json",
        help="JSON object of tool arguments. Defaults to stdin.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parsed = parser.parse_args(argv)

    try:
        result = call_pi_tool(parsed.root, parsed.tool, _read_args(parsed.args_json))
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "tool": parsed.tool},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if parsed.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

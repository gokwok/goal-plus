from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.pi


def test_pi_assets_exist() -> None:
    for path in (
        ".pi/prompts/goal-plus.md",
        ".pi/skills/goal-plus/SKILL.md",
        ".pi/prompts/search-candidate-worker.md",
        ".pi/extensions/goal-plus.ts",
    ):
        assert (ROOT / path).exists(), f"missing {path}"

    skill_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / ".pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == [
        ".pi/skills/goal-plus-install/SKILL.md",
        ".pi/skills/goal-plus/SKILL.md",
    ]


def test_pyproject_exposes_pi_console_scripts() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in text
    assert 'goal-plus-pi-tool = "goal_plus.pi_tool:main"' in text
    assert 'goal-plus-pi-worker = "goal_plus.pi_worker:main"' in text
    assert 'goal-plus-pi-pool = "goal_plus.pi_pool:main"' in text


def test_pi_goal_plus_prompt_starts_with_create_call() -> None:
    text = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(encoding="utf-8")

    assert 'goal_plus_create(raw_goal="$ARGUMENTS")' in text
    assert 'worker_host: "pi-rpc"' in text
    assert 'worker_mode: "agent-session-pool"' not in text
    assert 'orchestration_mode: "parallel_loops"' in text
    assert '`workspace.backend="git_worktree"`' in text
    assert '有限数值类型的' in text
    assert '`spec.metric_name`' in text
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只列出产物路径或 glob" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "固定的 `/tmp`" in text
    assert "`goal_plus_record_triage` 之前不要读取或审计目标文件" in text
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只列出" in text
    assert "{{input}}" not in text
    assert text.index("goal_plus_create") < text.index("Goal Plus")
    assert text.index("goal_plus_create") < text.index("goal_plus_record_triage")


def test_pi_goal_plus_skill_records_modes_and_gate() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "name: goal-plus" in text
    assert "Goal Mode" in text
    assert "Spec Discovery Mode" in text
    assert "Search Mode" in text
    assert "自动升级到 Search Mode" in text
    assert "不要要求用户" in text
    assert "用户提示有用但可选" in text
    assert "goal_plus_create" in text
    assert "goal_plus_gate" in text
    assert "goal_plus_monitor_snapshot" in text
    assert "主要只读监控路径" in text
    assert "goal-plus-pi-tool goal_plus_monitor_snapshot" in text
    assert "不要把手动 tail 文件作为主要监控路径" in text
    assert "goal_plus_link_search_run" in text
    assert 'worker_host: "pi-rpc"' in text
    assert 'worker_mode: "agent-session-pool"' not in text
    assert 'orchestration_mode: "parallel_loops"' in text
    assert '`workspace.backend="git_worktree"`' in text
    assert "Pi 支持的 strategy name" in text
    assert "`agent_guided`、`agent` 或 `default`" in text
    assert "`random` 或 `random_mode`" in text
    assert "可复用现有 `frozen_spec_id`" in text
    assert "pi_search_pool_open" in text
    assert "max_parallel=<budget.max_parallel>" in text
    assert "pi_search_run_batch" not in text
    assert "pi_search_run_candidate" not in text
    assert "复用当前 durable Evidence" in text
    assert "缺失时补父级 `search_run_verifier`" in text
    assert "search_select" in text
    assert "search_report" in text
    assert "search_promote" in text
    assert "跨进程原生 session continuation" in text
    assert "search_continue_agent_session" in text
    assert "session_jsonl_restart" not in text
    assert "尽早运行" in text
    assert "未修改初始状态的验证" in text
    assert "verifier 记录运行时 iteration" in text
    assert "完整的面向用户 skill" in text
    assert "goal_plus_record_search_result" in text
    assert "最终原始目标审计" in text
    assert "/goal-plus-with-final-check" in text
    assert "goal_plus_update_goal" in text
    assert "把最新用户消息视为本轮权威依据" in normalized
    assert "范围、交付物或成功标准" in normalized
    assert "在修订或恢复前先澄清" in normalized
    assert "不要仅因 Goal Plus 记录处于 active" in normalized
    assert "goal_plus_prepare_final_check" in text
    assert "pi_goal_plus_run_final_check" in text
    assert "goal_plus_submit_final_check" in text
    assert "绝不能生成中间 Goal Plus" in text
    assert "只有 Goal Plus 记录达到终态" in text
    assert "原生 Pi `/goal-plus` 命令会" in text
    assert "/goal-plus mode=autonomous" in text
    assert "/goal-plus mode=probe" in text
    assert "`raw_goal` 的规范末行" in normalized
    assert "任何 worker lease 结束都不会完成" in text
    assert "不要编造单独的 Goal Plus deadline" in text
    assert "加入队列" in text
    assert "continuation prompt" in text
    assert "`goal_plus_record_triage` 前不要读取或审计目标文件" in text


def test_pi_prompt_and_extension_defer_report_until_terminal_state() -> None:
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "在 Search 执行" in prompt
    assert "绝不能调用 `search_report`" in prompt
    assert "Goal Plus 记录达到终态后" in prompt
    assert "调用且只调用一次 `search_report`" in prompt
    assert "active 的已链接 Goal Plus 记录会被拒绝" in extension


def test_pi_goal_plus_skill_documents_parallel_loop_policy() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert 'orchestration_mode: "parallel_loops"' in text
    assert "每个初始候选工作区都是长期自主循环" in normalized
    assert "调用且只调用一次" in normalized
    assert "运行时会拒绝" in normalized
    assert "第二份 plan" in normalized
    assert "首个 run 必须省略 `source_run_id`" in text
    assert "pi_search_pool_wait_any" in text
    assert "只是本次轮询超时" in normalized
    assert "不能调用 `pi_search_pool_close`" in normalized
    assert "有效最低 lease 和 closeout 边界只由 supervisor 判定" in normalized
    assert "等待准确 snapshot 的 `active_count=0`" in normalized
    assert "pi_search_pool_continue" in text
    assert "pi_search_pool_submit" not in text
    assert "pi_search_pool_close" in text
    assert "worker_budgets" in text
    assert "每个 proposal 都必须包含 `intent`" in normalized
    assert "`worker_budgets` 必须按 `candidate_id` 映射" in normalized
    assert "继续同一条自主搜索循环" in text
    assert "candidate_ready" in text
    assert "最低累计 lease" in normalized
    assert "自动恢复同一个 session 和 worktree" in normalized
    assert "创建新的 `agent_session_id`" in normalized
    assert "已有工作区改动和 durable Evidence 保留" in normalized
    assert "复用匹配当前产物的 durable Evidence" in normalized
    assert "同一工作区" in normalized
    assert "保留 `agent_session_id` 和候选身份" in normalized
    assert "不要调用 `search_plan_next`、`search_start_batch`" in normalized
    assert "不要根据排名或改进情况" in normalized
    assert "分数低" in normalized
    assert "停止或替换" in normalized
    assert "source_run_id" in text
    assert "search_invalidate_run" in text
    assert "active_count=0" in text
    assert "deepen_incumbent" not in text
    assert "transfer_feature" not in text
    assert "macro_restart" not in text


def test_pi_worker_prompt_requires_runtime_context_and_verifier() -> None:
    text = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    )

    assert "search_get_agent_context" in text
    assert "candidate_task.share_out_dir" not in text
    assert "search_stage_shared_tool" not in text
    assert "search_copy_shared_tool" not in text
    assert "toolization_decision" not in text
    assert "required-column-probe" not in text
    assert "mutation-check-trace" not in text
    assert "Astropy" not in text
    assert "search_get_global_evidence" in text
    assert "每完成 3 次 `search_run_verifier` iteration 刷新一次" in text
    assert "global_evidence_snapshot` 已完成本次刷新" in text
    assert "search_submit_iteration_plan" not in text
    assert "search_run_verifier" in text
    assert "不得直接运行任务自带的 `runner`、`evaluator` 或 `grader`" in text
    assert "所有正确性与指标反馈必须通过 `search_run_verifier`" in text
    assert "workspace/results.tsv" in text
    assert "且只追加一条已验证" in text
    assert "view=null" in text
    assert "hypothesis" in text
    assert "git diff HEAD <commit> -- <allowed-file>" in text
    assert "当前 Git 能解析该 commit" in text
    assert "不要访问或 fetch peer workspace" in text
    assert "尽早创建完整候选产物" in text
    assert "任何长优化循环前" in text
    assert "一条自主 Pi Search 循环" in text
    assert "不要等待主 agent" in text
    assert "最低时间与 verifier 次数在这些派发间累计" in text
    assert ".tmp/handoff.json" in text
    assert "key_results" in text
    assert "pitfalls" in text
    assert "condition" in text
    assert "failed_approach" in text
    assert "把分配的候选思路当作假设" in text
    assert "把任何有希望的方向" in text
    assert "不要自行 reset" in text
    assert "disposition" in text
    assert "固定产物数量" in text
    assert "理论或结构限制" in text
    assert "公开指标饱和" in text
    assert "同分保留或回滚的 Evidence" in text
    assert "补充评价仍保留在 Global Evidence" in text
    assert "10-15 distinct verifier-recorded artifacts" not in text
    assert "verifier 是评估器，不是分析服务" in text
    assert (
        "更早派发中的 deadline、closeout 和 time-advisory 消息都只是历史"
    ) in text
    assert "next_steps" in text
    assert "verifier_assessment" in text
    assert "code_surface" in text
    assert "measured_effect" in text
    assert "portability" in text
    assert "relation_to_incumbent" in text
    assert "candidate_local" in text
    assert "feature_family" in text
    assert "evaluation_contract" in text
    assert "single_observation" in text
    assert "先编辑允许的候选产物" in text
    assert "验证未修改的初始状态" in text
    assert "先记录一个有效 baseline iteration" not in text
    assert '`run_id`、`candidate_id`、你的 `agent_session_id`' in text
    assert '`scope="process"`' not in text
    assert "停止启动新的优化 iteration" in text
    assert "最终 verifier" in text
    assert "工具结果后的时间提示仅供参考" in text
    assert "以直接读取和运行时上下文为准" in text
    assert "只能在候选工作区中工作" in text
    assert "运行时历史" in text
    assert "原生会话上下文可以保留推理" in text
    assert "绝不能覆盖持久化运行时证据" in text
    assert '因 `stopReason="length"` 结束且没有 tool call' in text
    assert "避免继承被截断的 thinking 上下文" in text
    assert "刷新不会重置累计值" in text
    assert "VerifierWorkspaceSideEffect" in text
    assert "VerifierDeadlineInsufficient" in text
    assert "candidate_action=stop_and_report" in text
    assert "立即返回" in text
    assert "candidate-local analysis scripts" not in text


def test_pi_skill_documents_post_tool_time_advisory() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "完成 worker 工具后检查提示性时间估算" in normalized
    assert "最后一次 subagent verifier - 首个候选 session" in normalized
    assert "GOAL_PLUS_OUTER_DEADLINE_AT" in text
    assert "一次提示性 `steer`" in text
    assert "不会停止 worker" in text


def test_pi_extension_registers_role_tools_gate_and_workspace_guard() -> None:
    text = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "GOAL_PLUS_PI_ROLE" in text
    assert 'role === "main"' in text
    assert 'role === "worker"' in text
    assert 'const isThinkThreadWorker = role === "worker" && isThinkThreadProfile' in text
    assert "goal_plus_create" in text
    assert 'Type.Literal("pi-thinkthread")' in text
    assert "search_get_agent_context" in text
    assert "search_run_verifier" in text
    assert "workspace/results.tsv" not in text
    assert "pi-thinkthread 不创建 commit 或 results.tsv" in text
    assert "VerifierWorkspaceSideEffect" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "pi_rpc_run_worker" not in text
    assert "pi_search_run_batch" not in text
    assert "pi_search_run_candidate" not in text
    assert 'pi.registerCommand("goal-plus"' in text
    assert 'pi.registerCommand("goal-plus-with-final-check"' in text
    assert 'if (role === "main" && !isPrintLikeInvocation)' in text
    assert 'role === "main" && typeof pi.registerEntryRenderer' in text
    assert "mode=autonomous|probe" in text
    assert "parseGoalPlusRoleModels" in text
    assert "applyGoalPlusRoleModels" in text
    assert "await pi.setModel(main)" in text
    assert '"workers" | "models"' in text
    assert "workers= and models= are aliases" in text
    assert "strategy.evidence_annotator.model" in text
    assert "strategy.models" in text
    assert "goal-plus-native-state" in text
    assert 'pi.on("session_start"' in text
    assert 'pi.on("before_agent_start"' in text
    assert "if (isThinkThreadWorker) registerWorkerMessageTool" in text
    assert "else registerRuntimeTool" in text
    assert "if (isThinkThreadProfile) await validateThinkThreadRole(ctx)" in text
    assert "if (isThinkThreadWorker)" in text
    assert 'pi.on("agent_end"' in text
    assert 'lastMessage?.role === "assistant"' in text
    assert 'lastMessage.stopReason === "error"' in text
    assert 'lastMessage.stopReason === "aborted"' in text
    assert 'lastMessage.stopReason === "length"' in text
    assert "countToolCalls(lastMessage.content) === 0" in text
    assert "if (lengthWithoutToolCall) return;" in text
    assert 'recoveryReason: "length_without_tool_call"' not in text
    assert "上一轮因达到输出长度上限而结束" not in text
    assert 'pi.on("tool_call"' in text
    assert 'goal_plus_gate' in text
    assert "tool_name" in text
    assert "goal-plus-stop-continuation" in text
    assert "GOAL_PLUS_PI_WORKER_CONTINUE_UNTIL_MS" in text
    assert "goal-plus-worker-continuation" in text
    assert '{ triggerTurn: true, deliverAs: "followUp" }' in text
    assert "goal-plus-stats" in text
    assert "registerEntryRenderer<GoalPlusStatsEntry>" in text
    assert "appendEntry<GoalPlusStatsEntry>" in text
    assert 'customType: "goal-plus-stats"' not in text
    assert "assistantMessages" in text
    assert "estimated_cost" in text
    assert "sendUserMessage" in text
    assert "GOAL_PLUS_SOURCE_PATH" in text
    assert "sys.path.insert" in text
    assert "goal_plus.pi_tool" in text
    assert "goal_plus.pi_worker" in text
    assert "isPrintLikeInvocation" in text
    assert 'process.argv.includes("-p")' in text
    assert 'if (role === "main" && !isPrintLikeInvocation)' in text
    assert 'mode !== "print"' in text
    assert "function canPersistGoalState" in text
    assert 'pi.on("input"' in text
    assert 'action: "transform"' in text
    assert "goalPlusRequestFromSlashInput" in text
    assert "createGoalPlusStart" in text
    assert "updateGoalPlusStart" in text
    assert "resumeGoalPlusStart" in text
    assert "/goal-plus resume" in text
    assert 'action: "resume"' in text
    assert "不要将其降级为普通 Goal Mode" in text
    assert "以最新用户消息作为" in text
    assert "范围、交付物或成功标准" in text
    assert "恢复前澄清有歧义的意图" in text
    assert "绝不能编造 frozen_spec_id" in text
    assert '"goal_plus_update_goal", "goal_plus_submit_final_check"' in text
    assert "activateGoal(pi, result.details, startEntryCount, canPersistPiState)" in text
    assert 'if (name === "goal_plus_create" && canPersistPiState)' not in text
    assert "activateGoal(pi, status, startEntryCount, canPersistGoalState(ctx.mode))" in text
    assert "await ctx.waitForIdle()" not in text
    assert "在 goal_plus_record_triage 前不要读取或审计目标文件" in text
    assert "workspaceGuard" in text
    assert "resource_lock: Type.Optional(Type.String({ minLength: 1 }))" in text
    assert "MAIN_GATED_TOOLS" in text
    assert "pi_rpc_run_worker" not in text
    assert "GOAL_PLUS_PI_EXPOSE_LOW_LEVEL_WORKER" not in text
    assert '"pi_search_run_candidate"' not in text
    assert '"pi_search_run_batch"' not in text
    assert "block" in text
    assert 'role === "final-checker"' in text
    assert "registerPiFinalCheckTool" in text
    assert "最终检查审查员只能进行只读操作" in text
    assert 'verdict: "interrupted"' in text
    assert "timed out before submitting a verdict" in text


def test_pi_extension_has_precise_tool_schemas_and_error_classification() -> None:
    text = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "RuntimeToolSchemas" in text
    assert "parameters: toolParameters(name)" in text
    assert "parameters: JsonArgs" not in text
    assert "goal_plus_record_triage: Type.Object" in text
    assert "const SearchSpecSchema = Type.Object" in text
    assert "const AcceptanceViewSpec = Type.Object" not in text
    assert "acceptance_view: Type.Optional" not in text
    assert 'Type.Literal("parallel_loops")' in text
    assert "search_invalidate_run: Type.Object" in text
    assert "source_run_id: Type.Optional(" in text
    assert 'pattern: "^run_"' in text
    assert "初始 run 必须省略 source_run_id，或在 strict schema 下传 null" in text
    assert "const SearchSpecDraftSchema = Type.Partial(SearchSpecSchema)" in text
    assert "spec: SearchSpecSchema" in text
    assert "search_spec: SearchSpecDraftSchema" in text
    assert "metric_direction: Type.Union" in text
    assert "process_verifiers: Type.Array(VerifierCommand" in text
    assert "const SharedDirSpec = Type.Object" in text
    assert "shared_dir: Type.Optional(SharedDirSpec)" in text
    assert "worker_budget: Type.Optional(Type.Union" in text
    assert "min_runtime_seconds: Type.Optional(NullablePositiveInteger)" in text
    assert "min_verifier_runs: Type.Optional(NullablePositiveInteger)" in text
    assert "const CandidateProposal = Type.Object" in text
    assert "proposals: Type.Optional(Type.Array(CandidateProposal))" in text
    assert "inherited_feature_limit" not in text
    assert "inherited_pitfall_limit" not in text
    assert "const RuntimeToolDescriptions" in text
    assert "RuntimeToolDescriptions[name]" in text
    assert "search_get_agent_context:" in text
    assert "limit: 32" in text
    assert "limit: 64" not in text
    assert "search_copy_shared_tool:" in text
    assert "search_stage_shared_tool:" in text
    assert "candidate_task.share_out_dir 非空表示已启用 shared_dir" in text
    assert "repeated_sequence、domain_probe、parser_or_trace 或 peer_setup_reduction" in text
    assert "toolization_decision：staged 至少包含一个正向 signal" in text
    assert "toolization_review_missing、toolization_stage_missing 或 toolization_decision_mismatch" in text
    assert "初始创建并实际并行工作的候选 Agent 数量" in text
    assert "标准流程令 requested_k 等于 max_parallel" in text
    freeze_schema = text.split("search_freeze_spec: Type.Object", 1)[1].split(
        "search_create: Type.Object", 1
    )[0]
    assert "spec: LooseObject" not in freeze_schema
    create_schema = text.split("search_create: Type.Object", 1)[1].split(
        "search_status: Type.Object", 1
    )[0]
    assert "Type.Null()" in create_schema
    select_schema = text.split("search_select: Type.Object", 1)[1].split(
        "search_report: Type.Object", 1
    )[0]
    assert "run_id: Type.String()" in select_schema
    assert "strategy" not in select_schema
    annotator_schema = text.split("const EvidenceAnnotator = Type.Object", 1)[
        1
    ].split("const ModelSpec = Type.Object", 1)[0]
    assert "pi_provider: Type.Optional(NullableString)" in annotator_schema
    assert "goal_plus_monitor_snapshot: Type.Object" in text
    assert "search_get_agent_observability: Type.Object" in text
    assert "goal_plus_update_goal: Type.Object" in text
    assert "goal_plus_prepare_final_check: Type.Object" in text
    assert "goal_plus_submit_final_check: Type.Object" in text
    assert "pi_goal_plus_run_final_check: Type.Object" in text
    assert "worker_budget: Type.Optional(WorkerBudget)" in text
    assert "worker_budgets: Type.Optional(Type.Record(Type.String(), WorkerBudget))" in text
    assert "candidate_ids: Type.Optional(Type.Array(Type.String()))" in text
    assert "max_parallel: Type.Optional(PositiveInteger)" in text
    assert "pi_search_pool_open: Type.Object" in text
    assert "pi_search_pool_submit: Type.Object" not in text
    assert "pi_search_pool_wait_any: Type.Object" in text
    assert "pi_search_pool_snapshot: Type.Object" in text
    assert "pi_search_pool_continue: Type.Object" in text
    assert "pi_search_pool_close: Type.Object" in text
    assert "runtime_multiplier" not in text

    main_tools = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    assert '"search_start_agent_session"' not in main_tools
    assert '"search_bind_agent_handle"' not in main_tools
    assert '"search_continue_agent_session"' not in main_tools
    assert '"pi_search_pool_wait_any"' in main_tools
    assert '"search_invalidate_run"' in main_tools
    assert '"pi_search_pool_continue"' in main_tools
    assert '"search_get_agent_observability"' in main_tools
    worker_tools = text.split("const workerTools = [", 1)[1].split("];", 1)[0]
    assert '"search_stage_shared_tool"' in worker_tools
    assert "final_verify: Type.Optional(Type.Boolean())" in text
    assert "triage: GoalPlusTriage" in text
    assert "is_optimization: Type.Boolean()" in text
    assert 'Type.Literal("spec_discovery")' in text
    assert "isEnvironmentFailure" in text
    assert "ModuleNotFoundError" in text
    assert "INSTALL_HINT" in text


def test_pi_assets_use_open_posthoc_evaluation_as_non_gating_feedback() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    worker = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((skill, worker))

    assert "软 rubric 或预设评价维度" in skill
    assert "开放式补充评价发生在每次 Evidence 结算之后" in skill
    assert "不来自 FrozenSpec" in combined
    assert "动态比较" in combined
    assert "不改变结算、硬 score 或最终 PASS/FAIL" in combined


def test_pi_docs_record_runner_logs_and_native_stop_gate() -> None:
    pi_doc = (ROOT / "docs" / "pi.md").read_text(encoding="utf-8")
    adapters = (ROOT / "docs" / "agent-host-adapters.md").read_text(encoding="utf-8")
    debug = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")
    examples = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")

    combined = "\n".join([pi_doc, adapters, debug, examples])
    normalized = " ".join(combined.split())
    assert "worker_host=\"pi-rpc\"" in combined
    assert "goal-plus-pi-worker" in combined
    assert "goal-plus-pi-tool" in combined
    assert "pi_search_run_batch" not in combined
    assert "GOAL_PLUS_PI_EXPOSE_LOW_LEVEL_WORKER=1" not in combined
    assert "pi_search_run_candidate" not in combined
    assert "goal_plus_monitor_snapshot" in combined
    assert "search_get_agent_observability" in combined
    assert "read-only" in combined
    assert "one user-facing `goal-plus` skill" in combined
    assert "does not expose a separate user-facing `search` skill" in combined
    assert ".pi/skills/goal-plus/" in combined
    assert "pi_search_pool_open" in combined
    assert "pi_search_pool_continue" in combined
    assert "How Pi Differs From Codex" in combined
    assert "Pi currently supports the portable builtin strategies only" in combined
    assert "pre-model `/goal-plus` creation" in combined
    assert "pi -p" in combined
    assert "--session-dir" in combined
    assert ".gp/host-sessions/pi/" in combined
    assert "metadata-only" in combined
    assert "native session continuation across process boundaries" in normalized
    assert "session_jsonl_restart" not in combined
    assert ".gp/host-logs/pi-rpc-" in combined
    assert "metadata-only event log" in combined
    assert "GOAL_PLUS_PI_RAW_LOG=1" in combined
    assert "native turn-level stop gate" in combined
    assert "Goal Plus stats" in combined
    assert "custom entry" in combined
    assert "does not trigger another assistant turn" in combined
    assert "no host process Stop hook" in combined


def test_pi_goal_plus_skill_documents_multiple_search_tasks_and_monitoring() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "同一个 `goal_plus_id`" in text
    assert "`search_tasks` 是其" in text
    assert "仅追加的 Search 任务历史" in text
    assert "每项任务的规划/已启动" in text
    assert "聚合任务" in text
    assert "search_tasks?: unknown[]" in extension
    assert "search_tasks_total?: number" in extension
    assert "status.search_tasks_total" in extension


def test_pi_goal_plus_reassesses_spec_after_real_result() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join([skill, prompt])
    flattened_skill = " ".join(skill.split())

    assert "首次获得有意义的优化结果后" in combined
    assert "相对提升很大" in combined
    assert "绝对目标" in skill
    assert "验收阈值" in skill
    assert "成功标准" in flattened_skill
    assert "更深的结构优化" in skill
    assert "`upgrade_spec`" in skill
    assert "`keep_spec_with_justification`" in skill
    assert "`revise_goal`" in skill
    assert "goal_plus_update_goal" in skill
    assert "不是新的运行时状态" in skill
    assert "不是新的运行时阶段" in prompt
    assert "bootstrap" not in combined.lower()

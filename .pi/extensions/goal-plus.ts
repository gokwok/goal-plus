import type { ExtensionAPI, ExtensionContext, ToolCallEvent } from "@earendil-works/pi-coding-agent";
import { Box, Text } from "@earendil-works/pi-tui";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { type TSchema, Type } from "typebox";

const role = process.env.GOAL_PLUS_PI_ROLE || "main";
const isThinkThreadProfile = Boolean(process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY);
const isThinkThreadWorker = role === "worker" && isThinkThreadProfile;
const runtimeRoot = process.env.GOAL_PLUS_ROOT || ".gp";
const sourcePath = process.env.GOAL_PLUS_SOURCE_PATH;
const workerContinueUntilMs = Number(process.env.GOAL_PLUS_PI_WORKER_CONTINUE_UNTIL_MS || "0");
const modeArgIndex = process.argv.indexOf("--mode");
const isPrintLikeInvocation =
	process.argv.includes("-p") ||
	process.argv.includes("--print") ||
	process.argv.includes("--mode=json") ||
	(modeArgIndex >= 0 && process.argv[modeArgIndex + 1] === "json");
const STATE_ENTRY_TYPE = "goal-plus-native-state";
const GOAL_PLUS_STATS_ENTRY_TYPE = "goal-plus-stats";
let workspaceRoot: string | undefined;
let sawContext = false;
let activeGoalPlusId = process.env.GOAL_PLUS_ID;
let cachedGoalStatus: GoalPlusStatusPayload | undefined;
let continuationCount = 0;
let workerContinuationCount = 0;
let workerAgentSessionId: string | undefined;
let workerMessageCursor: string | undefined;
let workerRegistrationSent = false;
let workerSdkModulePromise: Promise<any> | undefined;
let workerSdkClientPromise: Promise<any> | undefined;
const workerAcknowledgedDispatches = new Set<string>();
let activeGoalStartedAt: string | undefined;
let activeGoalStartEntryCount = 0;

const INSTALL_HINT =
	'Install this project into the Python environment that launches Pi: python -m pip install -e ".[dev]".';
const AGENT_POSIX_CONTROL_PROTOCOL_VERSION = 2;
const AGENT_POSIX_CONTRACT_FINGERPRINT =
	"fcc80b665cd990f9d1e3681a9d384cb99994f2b739cd4fbddc97bdda01391131";
const LooseObject = Type.Object({}, { additionalProperties: true });
const GoalPlusConfidence = Type.Union([Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]);
const GoalPlusRecommendedPhase = Type.Union([
	Type.Literal("goal"),
	Type.Literal("spec_discovery"),
	Type.Literal("search"),
]);
const GoalPlusDiscoveryOrigin = Type.Union([Type.Literal("initial"), Type.Literal("in_progress")]);
const GoalPlusFinalCheckerHost = Type.Union([Type.Literal("codex"), Type.Literal("pi")]);
const GoalPlusTriage = Type.Object(
	{
		is_optimization: Type.Boolean(),
		confidence: GoalPlusConfidence,
		recommended_phase: GoalPlusRecommendedPhase,
		identified_at: Type.Optional(GoalPlusDiscoveryOrigin),
		scenario: Type.Optional(Type.String()),
		reasons: Type.Optional(Type.Array(Type.String())),
		missing: Type.Optional(Type.Array(Type.String())),
	},
	{ additionalProperties: false },
);
const GoalPlusNextAction = Type.Object(
	{
		kind: Type.String(),
		description: Type.String(),
		required: Type.Optional(Type.Boolean()),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const PositiveInteger = Type.Integer({ exclusiveMinimum: 0 });
const NullableString = Type.Union([Type.String(), Type.Null()]);
const NullablePositiveInteger = Type.Union([PositiveInteger, Type.Null()]);
const VerifierRole = Type.Union([
	Type.Literal("validity_gate"),
	Type.Literal("process_gate"),
	Type.Literal("ranking_signal"),
	Type.Literal("diagnostic_signal"),
	Type.Literal("promotion_gate"),
	Type.Literal("anti_cheat_gate"),
]);
const FeedbackPolicy = Type.Union([
	Type.Literal("visible_to_workers"),
	Type.Literal("summary_only"),
	Type.Literal("final_only"),
]);
const VerifierCommand = Type.Object(
	{
		name: Type.String({ minLength: 1 }),
		role: VerifierRole,
		command: Type.Array(Type.String(), { minItems: 1 }),
		cwd: Type.Optional(Type.String()),
		timeout_seconds: Type.Optional(PositiveInteger),
		feedback_policy: Type.Optional(FeedbackPolicy),
		expected_outputs: Type.Optional(Type.Array(Type.String())),
		resource_lock: Type.Optional(Type.String({ minLength: 1 })),
	},
	{ additionalProperties: false },
);
const EditSurface = Type.Object(
	{
		allow: Type.Array(Type.String(), { minItems: 1 }),
		deny: Type.Optional(Type.Array(Type.String())),
		max_file_changes: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const SearchBudget = Type.Object(
	{
		max_parallel: Type.Integer({
			exclusiveMinimum: 0,
			description:
				"一个 Search run 初始创建并实际并行工作的候选 Agent 数量；后续继续已有 candidate/session。",
		}),
		max_tokens: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const WorkerBudget = Type.Object(
	{
		max_runtime_seconds: Type.Optional(NullablePositiveInteger),
		max_turns: Type.Optional(NullablePositiveInteger),
		on_exceed: Type.Optional(Type.Literal("interrupt")),
		min_runtime_seconds: Type.Optional(NullablePositiveInteger),
		min_verifier_runs: Type.Optional(NullablePositiveInteger),
	},
	{ additionalProperties: false },
);
const CandidateProposal = Type.Object(
	{
		intent: Type.String({ minLength: 1 }),
		hypothesis: Type.Optional(NullableString),
		expected_tradeoff: Type.Optional(Type.String()),
		instructions: Type.Optional(Type.Array(Type.String())),
		metadata: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const WorkerLaunch = Type.Object(
	{
		model: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		service_tier: Type.Optional(NullableString),
	},
	{ additionalProperties: false },
);
const EvidenceAnnotatorProvider = Type.Object(
	{
		provider_id: Type.Optional(Type.String({ pattern: "^[A-Za-z0-9_-]+$" })),
		name: Type.Optional(Type.String({ minLength: 1 })),
		base_url: Type.String({ minLength: 1 }),
		api_key_env: Type.Optional(Type.String({ minLength: 1 })),
		wire_api: Type.Optional(Type.String({ minLength: 1 })),
	},
	{ additionalProperties: false },
);
const EvidenceAnnotator = Type.Object(
	{
		model: Type.Optional(NullableString),
		pi_provider: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		timeout_seconds: Type.Optional(PositiveInteger),
		provider: Type.Optional(Type.Union([EvidenceAnnotatorProvider, Type.Null()])),
	},
	{ additionalProperties: false },
);
const ModelSpec = Type.Object(
	{
		model: Type.String({ minLength: 1 }),
		count: Type.Optional(PositiveInteger),
		provider: Type.Optional(NullableString),
		adapter_version: Type.Optional(NullableString),
		reasoning_effort: Type.Optional(NullableString),
		service_tier: Type.Optional(NullableString),
		context_policy: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const StrategySpec = Type.Object(
	{
		name: Type.Optional(Type.String({ minLength: 1 })),
		orchestration_mode: Type.Optional(Type.Literal("parallel_loops")),
		worker_host: Type.Optional(
			Type.Union([Type.Literal("pi-rpc"), Type.Literal("pi-thinkthread")]),
		),
		worker_agent_type: Type.Optional(NullableString),
		worker_budget: Type.Optional(Type.Union([WorkerBudget, Type.Null()])),
		worker_launch: Type.Optional(Type.Union([WorkerLaunch, Type.Null()])),
		evidence_annotator: Type.Optional(EvidenceAnnotator),
		models: Type.Optional(Type.Array(ModelSpec)),
		config: Type.Optional(LooseObject),
	},
	{ additionalProperties: false },
);
const WorkspaceSpec = Type.Object(
	{
		backend: Type.Optional(Type.Union([Type.Literal("copy"), Type.Literal("git_worktree")])),
	},
	{ additionalProperties: false },
);
const SharedDirSpec = Type.Object(
	{
		enabled: Type.Boolean(),
		max_tools_per_iteration: Type.Optional(PositiveInteger),
		max_files_per_iteration: Type.Optional(PositiveInteger),
		max_path_entries_per_iteration: Type.Optional(PositiveInteger),
		max_depth: Type.Optional(PositiveInteger),
		max_bytes_per_iteration: Type.Optional(PositiveInteger),
	},
	{ additionalProperties: false },
);
const ToolizationSignal = Type.Union([
	Type.Literal("repeated_sequence"),
	Type.Literal("domain_probe"),
	Type.Literal("parser_or_trace"),
	Type.Literal("peer_setup_reduction"),
]);
const ToolizationExclusion = Type.Union([
	Type.Literal("single_common_command"),
	Type.Literal("logic_free_wrapper"),
	Type.Literal("restricted_artifact"),
	Type.Literal("candidate_private_state"),
	Type.Literal("duplicate_snapshot"),
]);
const ToolizationDecision = Type.Object(
	{
		outcome: Type.Union([Type.Literal("staged"), Type.Literal("not_applicable")]),
		signals: Type.Array(ToolizationSignal, { maxItems: 4 }),
		exclusion: Type.Optional(Type.Union([ToolizationExclusion, Type.Null()])),
		rationale: Type.String({ minLength: 1, maxLength: 1000 }),
		tool_names: Type.Array(Type.String({ minLength: 1, maxLength: 120 }), { maxItems: 16 }),
	},
	{ additionalProperties: false },
);
const SearchSpecSchema = Type.Object(
	{
		objective: Type.String({ minLength: 1 }),
		metric_name: Type.String({ minLength: 1 }),
		metric_direction: Type.Union([Type.Literal("minimize"), Type.Literal("maximize")]),
		source_path: Type.String({ minLength: 1 }),
		edit_surface: EditSurface,
		budget: SearchBudget,
		process_verifiers: Type.Array(VerifierCommand, { minItems: 1 }),
		promotion_verifiers: Type.Optional(Type.Array(VerifierCommand)),
		constraints: Type.Optional(LooseObject),
		root_hypotheses: Type.Optional(Type.Array(Type.String())),
		strategy: Type.Optional(StrategySpec),
		workspace: Type.Optional(WorkspaceSpec),
		shared_dir: Type.Optional(SharedDirSpec),
	},
	{ additionalProperties: false },
);
const SearchSpecDraftSchema = Type.Partial(SearchSpecSchema);
const GoalPlusSpecDraft = Type.Object(
	{
		baseline: LooseObject,
		metric: LooseObject,
		correctness_gate: LooseObject,
		edit_surface: LooseObject,
		verifier_artifacts: Type.Optional(Type.Array(Type.String())),
		search_spec: SearchSpecDraftSchema,
		promotion_rule: Type.String(),
		confidence: GoalPlusConfidence,
		origin: Type.Optional(GoalPlusDiscoveryOrigin),
		open_questions: Type.Optional(Type.Array(Type.String())),
	},
	{ additionalProperties: false },
);
const RuntimeToolSchemas: Record<string, TSchema> = {
	goal_plus_create: Type.Object(
		{
			raw_goal: Type.String(),
			source_path: Type.Optional(Type.String()),
			policy: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_status: Type.Object({ goal_plus_id: Type.String() }, { additionalProperties: false }),
	goal_plus_update_goal: Type.Object(
		{
			goal_plus_id: Type.String(),
			raw_goal: Type.String(),
			expected_revision: Type.Number(),
			reason: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_monitor_snapshot: Type.Object(
		{
			goal_plus_id: Type.Optional(Type.String()),
			run_id: Type.Optional(Type.String()),
			stale_after_seconds: Type.Optional(Type.Number()),
		},
		{ additionalProperties: false },
	),
	goal_plus_list_models: Type.Object(
		{
			host: Type.Union([
				Type.Literal("codex"),
				Type.Literal("pi-rpc"),
				Type.Literal("pi-thinkthread"),
			]),
			query: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_record_triage: Type.Object(
		{
			goal_plus_id: Type.String(),
			triage: GoalPlusTriage,
		},
		{ additionalProperties: false },
	),
	goal_plus_save_spec_draft: Type.Object(
		{
			goal_plus_id: Type.String(),
			spec_draft: GoalPlusSpecDraft,
		},
		{ additionalProperties: false },
	),
	goal_plus_link_search_run: Type.Object(
		{
			goal_plus_id: Type.String(),
			frozen_spec_id: Type.String(),
			run_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	goal_plus_record_search_result: Type.Object(
		{
			goal_plus_id: Type.String(),
			run_id: Type.String(),
			selected_candidate_id: Type.Optional(Type.String()),
			report_path: Type.Optional(Type.String()),
			promotion_artifact_path: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	goal_plus_prepare_final_check: Type.Object(
		{
			goal_plus_id: Type.String(),
			checker_host: GoalPlusFinalCheckerHost,
		},
		{ additionalProperties: false },
	),
	goal_plus_submit_final_check: Type.Object(
		{
			goal_plus_id: Type.String(),
			check_id: Type.String(),
			goal_revision: Type.Number(),
			verdict: Type.Union([
				Type.Literal("pass"),
				Type.Literal("fail"),
				Type.Literal("interrupted"),
			]),
			summary: Type.String(),
			findings: Type.Optional(Type.Array(LooseObject)),
			evidence: Type.Optional(Type.Array(LooseObject)),
			checker_metadata: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	goal_plus_set_status: Type.Object(
		{
			goal_plus_id: Type.String(),
			status: Type.Union([
				Type.Literal("active"),
				Type.Literal("needs_user"),
				Type.Literal("blocked"),
				Type.Literal("complete"),
				Type.Literal("abandoned"),
			]),
			reason: Type.Optional(Type.String()),
			evidence: Type.Optional(Type.Array(LooseObject)),
			next_action: Type.Optional(GoalPlusNextAction),
		},
		{ additionalProperties: false },
	),
	goal_plus_gate: Type.Object(
		{
			goal_plus_id: Type.String(),
			event: Type.Union([
				Type.Literal("stop"),
				Type.Literal("subagent_stop"),
				Type.Literal("pre_tool_use"),
				Type.Literal("user_prompt_submit"),
			]),
			context: LooseObject,
		},
		{ additionalProperties: false },
	),
	search_freeze_spec: Type.Object(
		{
			spec: SearchSpecSchema,
			verifier_artifact_paths: Type.Array(Type.String()),
		},
		{ additionalProperties: false },
	),
	search_create: Type.Object(
		{
			frozen_spec_id: Type.String(),
			source_run_id: Type.Optional(
				Type.Union(
					[Type.String({ pattern: "^run_" }), Type.Null()],
					{
						description:
							"初始 run 必须省略 source_run_id，或在 strict schema 下传 null；仅后继 run 传入真实已存在的 run_* ID。",
					},
				),
			),
		},
		{ additionalProperties: false },
	),
	search_status: Type.Object({ run_id: Type.String() }, { additionalProperties: false }),
	search_recover_pi_thinkthread: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
	search_invalidate_run: Type.Object(
		{
			run_id: Type.String(),
			reason: Type.Union([
				Type.Literal("verifier_contract_invalid"),
				Type.Literal("verifier_coverage_inadequate"),
				Type.Literal("verifier_nondeterministic"),
				Type.Literal("verifier_target_mismatch"),
				Type.Literal("verifier_infrastructure_failure"),
			]),
			summary: Type.String({ minLength: 1 }),
			evidence: Type.Array(LooseObject, { minItems: 1 }),
		},
		{ additionalProperties: false },
	),
	search_list_history: Type.Object(
		{
			run_id: Type.String(),
			top_n: Type.Optional(Type.Number()),
			sort_by: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
	search_plan_next: Type.Object(
		{
			run_id: Type.String(),
			requested_k: Type.Optional(
				Type.Integer({
					exclusiveMinimum: 0,
					description:
						"仅为本规划轮次请求的候选数。运行时按 min(requested_k, 剩余候选总预算, budget.max_parallel) 进行规划。默认值 4 是 batch size 请求，不是整个 run 的预算。",
				}),
			),
		},
		{ additionalProperties: false },
	),
	search_start_batch: Type.Object(
		{
			run_id: Type.String(),
			plan_id: Type.String(),
			proposals: Type.Optional(Type.Array(CandidateProposal)),
		},
		{ additionalProperties: false },
	),
	search_start_agent_session: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
			directive: Type.Optional(Type.Union([Type.String(), LooseObject])),
		},
		{ additionalProperties: false },
	),
		search_redispatch_candidate: Type.Object(
			{
				run_id: Type.String(),
				candidate_id: Type.String(),
				worker_agent_type: Type.Optional(Type.String()),
			worker_budget: Type.Optional(LooseObject),
		},
		{ additionalProperties: false },
	),
	search_bind_agent_handle: Type.Object(
		{
			agent_session_id: Type.String(),
			handle: LooseObject,
		},
		{ additionalProperties: false },
	),
		search_continue_agent_session: Type.Object(
			{
				agent_session_id: Type.String(),
				worker_budget: Type.Optional(WorkerBudget),
		},
		{ additionalProperties: false },
	),
	search_get_agent_context: Type.Object({ agent_session_id: Type.String() }, { additionalProperties: false }),
	search_get_global_evidence: Type.Object(
		{ agent_session_id: Type.String() },
		{ additionalProperties: false },
	),
	search_copy_shared_tool: Type.Object(
		{
			agent_session_id: Type.String(),
			tool_id: Type.String(),
			snapshot_hash: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_get_evidence_detail: Type.Object(
		{
			agent_session_id: Type.String(),
			candidate_id: Type.String(),
			iteration: PositiveInteger,
		},
		{ additionalProperties: false },
	),
	search_stage_shared_tool: Type.Object(
		{
			agent_session_id: Type.String(),
			name: Type.String({ minLength: 1, maxLength: 120 }),
			summary: Type.String({ minLength: 1, maxLength: 500 }),
			entrypoint: Type.String({ minLength: 1, maxLength: 300 }),
			candidate_relative_source_paths: Type.Array(Type.String({ minLength: 1 }), {
				minItems: 1,
			}),
		},
		{ additionalProperties: false },
	),
	search_get_agent_observability: Type.Object(
		{ agent_session_id: Type.String() },
		{ additionalProperties: false },
	),
	search_run_verifier: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
			scope: Type.Optional(Type.Union([Type.Literal("process"), Type.Literal("promotion")])),
			agent_session_id: Type.Optional(Type.String()),
			hypothesis: Type.Optional(Type.String()),
			toolization_decision: Type.Optional(ToolizationDecision),
		},
		{ additionalProperties: false },
	),
	search_list_iterations: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
		},
		{ additionalProperties: false },
	),
	search_select: Type.Object(
		{ run_id: Type.String() },
		{ additionalProperties: false },
	),
	search_report: Type.Object({ run_id: Type.String() }, { additionalProperties: false }),
	search_promote: Type.Object(
		{
			run_id: Type.String(),
			candidate_id: Type.String(),
		},
		{ additionalProperties: false },
	),
		pi_search_pool_open: Type.Object(
			{
				run_id: Type.String(),
				candidate_ids: Type.Optional(Type.Array(Type.String())),
				worker_budgets: Type.Optional(Type.Record(Type.String(), WorkerBudget)),
			final_verify: Type.Optional(Type.Boolean()),
			max_parallel: Type.Optional(PositiveInteger),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_wait_any: Type.Object(
		{
			pool_id: Type.String(),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_snapshot: Type.Object(
		{
			pool_id: Type.Optional(Type.String()),
			run_id: Type.Optional(Type.String()),
		},
		{ additionalProperties: false },
	),
		pi_search_pool_continue: Type.Object(
			{
				pool_id: Type.String(),
				candidate_id: Type.String(),
				worker_budget: Type.Optional(WorkerBudget),
				final_verify: Type.Optional(Type.Boolean()),
		},
		{ additionalProperties: false },
	),
	pi_search_pool_close: Type.Object(
		{
			pool_id: Type.String(),
			mode: Type.Optional(Type.Union([Type.Literal("drain"), Type.Literal("interrupt")])),
			timeout_seconds: Type.Optional(Type.Number({ minimum: 0 })),
		},
		{ additionalProperties: false },
	),
	pi_goal_plus_run_final_check: Type.Object(
		{ launch: LooseObject },
		{ additionalProperties: false },
	),
};
const RuntimeToolDescriptions: Record<string, string> = {
	goal_plus_save_spec_draft:
		"保存发现的 SearchSpec draft。新的 Pi spec 使用 orchestration_mode=parallel_loops，以 max_parallel 作为初始 candidate/subagent 数。",
	search_freeze_spec:
		"冻结不可变的 SearchSpec 和 verifier bundle。预检使用一次性源码副本，并拒绝 verifier 工作区副作用；并发 Search 下 verifier 临时文件必须放入唯一的 GOAL_PLUS_VERIFIER_TMPDIR/TMPDIR，绝不能使用固定 /tmp 路径。parallel_loops 模式由一份初始 plan 创建长期候选。",
	search_create:
		"从 frozen_spec_id 创建 Search run。初始 run 必须省略 source_run_id，或在 strict schema 下传 null；仅在已有真实前驱时传入准确的 run_* ID，绝不能传 initial 或 in_progress。",
	search_recover_pi_thinkthread:
		"仅用于 pi-thinkthread 崩溃恢复：按已持久化的 RequestId 查询或幂等重放 Root/Child snapshot capture，绑定准确 FsSnapshotId 后关闭 terminal request。不得创建新 RequestId 盲重试。",
	search_get_agent_context:
		"读取当前 worker 的权威 candidate 上下文。candidate_task.share_out_dir 非空表示已启用 shared_dir：同一 run 内可供 peer 使用的 repeated_sequence、domain_probe、parser_or_trace 或 peer_setup_reduction 默认应工具化；短小、任务专属、来自临时代码片段或只输出退出码都不是排除理由。只有 single_common_command、logic_free_wrapper、restricted_artifact、candidate_private_state 或 duplicate_snapshot 支持 not_applicable。",
	search_get_global_evidence:
		"读取当前 run 的窄 Global Evidence 视图。每项包含 verifier exact ArtifactRef、硬 score、keep/retain/discard/failure disposition、可能延迟的客观 View、可选 supplemental evaluation 的可用标记，以及启用 shared_dir 后已由 annotator 描述并由 runtime 绑定的 shared_tools/tool_view。任一 View 为 null 时都无需等待，可先依据 Evidence 独立探索。",
	search_copy_shared_tool:
		"请求采用 Global Evidence Tool View 对应的精确 shared-dir 快照。pi-thinkthread 只先持久化 receipt；当前 turn 结束后 Root 停止 execution、patch/reset private branch 并 wake 同一 Session。下一次 verifier 消费 receipt；采用本身不改变选择、排名或硬分。",
	search_stage_shared_tool:
		"登记当前 candidate 的 .tmp/tool-drafts/ 中显式选择的文件。pi-thinkthread 不在 Child 创建本地共享目录；Root 在下一次 exact snapshot verifier 结算时读取并发布。路径、链接和 frozen shared-dir 限额由 runtime 校验，发布仍要求归属于当前 worker 且通过的 process verifier。",
	search_get_evidence_detail:
		"按需展开一条已结算 Evidence 的 supplemental evaluation。仅当 agent context 声明该能力开启且目标行 supplemental_available=true 时调用；independent 模式只允许读取自己的 candidate。",
	search_run_verifier:
		"为一个候选评分。worker process verifier 必须提供一句话 hypothesis，并在 shared_dir 启用时提交 toolization_decision：staged 至少包含一个正向 signal 和实际 tool_names；not_applicable 必须给出具体 exclusion，不能只写不复用。runtime 以 exact ArtifactRef、staging inventory 和 publication settlement 为权威；Git host 保留兼容 ledger，pi-thinkthread 不创建 commit 或 results.tsv。toolization_review_missing、toolization_stage_missing 或 toolization_decision_mismatch 只记为 monitor/report advisory，不改变结算或硬分。process verifier 返回 keep/retain/discard/failure disposition；严格改善为 keep，同分为 retain 并成为 candidate-local 最新基线，只有退化或验证失败时恢复此前硬分最佳。开放式补充评价和动态 peer 比较不改变结算、硬 score 或最终 PASS/FAIL。带 candidate_action=stop_and_report 的 VerifierWorkspaceSideEffect 属于基础设施失败：worker 必须停止，不能清理或重试，使父级能够修复并重新冻结。",
	search_invalidate_run:
		"主 agent 确认 verifier 契约、覆盖范围、确定性、目标对齐或基础设施失败后，原子地隔离该 run。随后中断每个 host worker，等待 active worker 数归零，修复并重新冻结，再使用 source_run_id 创建后继项。",
	search_report:
		"生成最终 report.md 和 report.html。对已链接的 Goal Plus run，只能在 Goal Plus 记录达到终态后调用且只调用一次；独立 Search 在提升后调用。active 的已链接 Goal Plus 记录会被拒绝。",
	search_plan_next:
		"规划初始候选。parallel_loops 模式下只能调用一次；后续工作恢复现有候选。标准流程令 requested_k 等于 max_parallel。",
	pi_search_pool_open:
		"打开持久化 Pi 候选 pool，并启动完整初始候选集合。启动后立即返回，并强制执行冻结的 max_parallel 限制。",
	pi_search_pool_wait_any:
		"等待任一 Pi pool worker 的终态事件。candidate_ready 要求 handle 已绑定、最低累计 lease 已满足且存在持久化验证证据；最低 lease 到硬上限仍未满足时返回 timed_out。已有当前产物的 durable Evidence 时不会重复运行父级 process verifier；只对 candidate_ready 应用后续继续策略。",
	pi_search_pool_snapshot:
		"无需等待即可检查持久化 Pi pool 状态、active worker、终态结果和空闲 slot。主 session 中断后传入 run_id 重新发现 pool，或传入 pool_id 指定准确 pool。",
	pi_search_pool_continue:
		"通过显式状态重新派发，在现有工作区恢复同一条自主 Pi 候选循环；可选传入另一份单次派发预算。",
	pi_search_pool_close:
		"通过 drain 或中断 active worker 来关闭 Pi pool。select/promote 前必须关闭 pool。",
};
const MAIN_GATED_TOOLS = new Set([
		"bash",
		"edit",
		"write",
		"pi_search_pool_open",
	"pi_search_pool_wait_any",
	"pi_search_pool_snapshot",
	"pi_search_pool_continue",
	"pi_search_pool_close",
	"pi_goal_plus_run_final_check",
]);

interface GoalPlusNativeState {
	activeGoalPlusId?: string;
	continuationCount?: number;
	startedAt?: string;
	startEntryCount?: number;
	status?: string;
	phase?: string;
	updatedAt?: string;
}

interface GoalPlusNextActionPayload {
	kind?: string;
	description?: string;
	required?: boolean;
	metadata?: Record<string, unknown>;
}

interface GoalPlusStatusPayload {
	goal_plus_id?: string;
	raw_goal?: string;
	goal_revision?: number;
	goal_revisions?: unknown[];
	policy?: Record<string, unknown>;
	final_checks?: unknown[];
	status?: string;
	phase?: string;
	next_action?: GoalPlusNextActionPayload | null;
	triage?: unknown;
	spec_draft?: unknown;
	search_tasks?: unknown[];
	search_tasks_total?: number;
	current_search_run_id?: string | null;
	linked_search?: unknown;
}

interface GoalPlusGatePayload {
	decision?: string;
	reason?: string;
	continuation_prompt?: string;
	status?: string;
	phase?: string;
}

interface GoalPlusUsageTotals {
	assistantMessages: number;
	toolCalls: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	cost: number;
}

interface GoalPlusStatsEntry {
	goal_plus_id?: string;
	status?: string;
	startedAt?: string;
	endedAt: string;
	usage: GoalPlusUsageTotals;
	message: string;
}

interface CommandInvocation {
	command: string;
	argsPrefix: string[];
	label: string;
}

interface CommandRuntimeContext {
	cwd: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

function numberFrom(value: unknown): number {
	return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function commandContextFrom(ctx: ExtensionContext): CommandRuntimeContext {
	return { cwd: ctx.cwd };
}

function sourceRoot(ctx: CommandRuntimeContext): string {
	return sourcePath || ctx.cwd;
}

function projectModuleInvocation(ctx: CommandRuntimeContext, command: string, moduleName: string): CommandInvocation {
	const installedCommand = process.env.GOAL_PLUS_PI_TOOL;
	if (command === "goal-plus-pi-tool" && installedCommand) {
		return { command: installedCommand, argsPrefix: [], label: installedCommand };
	}
	const root = sourceRoot(ctx);
	const src = join(root, "src");
	const packageDir = join(src, "goal_plus");
	if (existsSync(packageDir)) {
		const code = [
			"import sys",
			`sys.path.insert(0, ${JSON.stringify(src)})`,
			`from ${moduleName} import main`,
			"raise SystemExit(main())",
		].join("; ");
		return { command: "python", argsPrefix: ["-c", code], label: `python -c ${moduleName}` };
	}
	return { command, argsPrefix: [], label: command };
}

function parseJsonObject(text: string): Record<string, unknown> | undefined {
	const trimmed = text.trim();
	if (!trimmed) return undefined;
	try {
		const parsed = JSON.parse(trimmed);
		return isRecord(parsed) ? parsed : undefined;
	} catch {
		return undefined;
	}
}

function isEnvironmentFailure(text: string): boolean {
	const normalized = text.toLowerCase();
	return (
		text.includes("ModuleNotFoundError") ||
		normalized.includes("no module named") ||
		normalized.includes("cannot find module") ||
		normalized.includes("command not found") ||
		normalized.includes("enoent") ||
		normalized.includes("not found:")
	);
}

function commandFailure(
	tool: string,
	invocation: CommandInvocation,
	result: { stdout: string; stderr: string; code: number },
): { text: string; details: Record<string, unknown> } {
	const output = (result.stderr || result.stdout || `${invocation.label} failed with exit code ${result.code}`).trim();
	const parsed = parseJsonObject(output);
	const baseError = typeof parsed?.error === "string" ? parsed.error : output;
	const text = isEnvironmentFailure(baseError) ? `${baseError}\n\n${INSTALL_HINT}` : baseError;
	return {
		text,
		details: {
			...(parsed ?? {}),
			tool: typeof parsed?.tool === "string" ? parsed.tool : tool,
			ok: false,
			error: text,
		},
	};
}

function toolParameters(name: string): TSchema {
	return RuntimeToolSchemas[name] ?? LooseObject;
}

function goalPlusIdFrom(value: unknown): string | undefined {
	if (!isRecord(value)) return undefined;
	const id = value.goal_plus_id;
	return typeof id === "string" && id.length > 0 ? id : undefined;
}

function statusFrom(value: unknown): GoalPlusStatusPayload | undefined {
	if (!isRecord(value)) return undefined;
	return value as GoalPlusStatusPayload;
}

function gateFrom(value: unknown): GoalPlusGatePayload | undefined {
	if (!isRecord(value)) return undefined;
	return value as GoalPlusGatePayload;
}

async function runJsonCli(pi: ExtensionAPI, ctx: CommandRuntimeContext, tool: string, args: Record<string, unknown>) {
	const invocation = projectModuleInvocation(ctx, "goal-plus-pi-tool", "goal_plus.pi_tool");
	const result = await pi.exec(invocation.command, [
		...invocation.argsPrefix,
		"--root",
		runtimeRoot,
		"--args-json",
		JSON.stringify(args),
		tool,
	]);
	if (result.code !== 0) {
		const failure = commandFailure(tool, invocation, result);
		return {
			content: [{ type: "text" as const, text: failure.text }],
			details: failure.details,
		};
	}
	const parsed = JSON.parse(result.stdout || "null");
	return {
		content: [{ type: "text" as const, text: JSON.stringify(parsed, null, 2) }],
		details: parsed,
	};
}

function canonicalJson(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
	if (isRecord(value)) {
		return `{${Object.keys(value)
			.sort()
			.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
			.join(",")}}`;
	}
	return JSON.stringify(value);
}

function sha256(value: string | Uint8Array): string {
	return createHash("sha256").update(value).digest("hex");
}

async function workerAgentPosixClient(): Promise<any> {
	if (!workerSdkModulePromise) {
		const configured = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
		if (!configured) {
			throw new Error("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY is required in the worker Profile");
		}
		workerSdkModulePromise = import(
			configured.startsWith("file:") ? configured : pathToFileURL(configured).href
		);
	}
	const sdk = await workerSdkModulePromise;
	if (
		sdk.CONTROL_PROTOCOL_VERSION !== AGENT_POSIX_CONTROL_PROTOCOL_VERSION ||
		sdk.CONTRACT_FINGERPRINT !== AGENT_POSIX_CONTRACT_FINGERPRINT
	) {
		throw new Error(
			`unsupported ThinkThread Agent POSIX SDK contract: protocol=${String(sdk.CONTROL_PROTOCOL_VERSION)}, fingerprint=${String(sdk.CONTRACT_FINGERPRINT)}`,
		);
	}
	if (!workerSdkClientPromise) {
		workerSdkClientPromise = Promise.resolve(sdk.AgentPosixClient.fromEnv());
	}
	return workerSdkClientPromise;
}

function registrationNonce(ctx: ExtensionContext): string | undefined {
	const serialized = JSON.stringify(ctx.sessionManager.getEntries());
	return /registration_nonce=([0-9a-f-]{36})/i.exec(serialized)?.[1];
}

async function ensureWorkerRegistration(client: any, parentId: string, ctx: ExtensionContext) {
	if (workerRegistrationSent) return;
	const nonce = registrationNonce(ctx);
	if (!nonce) throw new Error("ThinkThread worker registration nonce is missing from the initial Message");
	await client.invoke("message.send", {
		recipientThinkthreadId: parentId,
		text: JSON.stringify({
			protocol: "goal-plus.pi-thinkthread.v2",
			type: "registration",
			registration_nonce: nonce,
		}),
		wake: false,
	});
	workerRegistrationSent = true;
}

function latestDispatchNonce(ctx: ExtensionContext): string | undefined {
	const serialized = JSON.stringify(ctx.sessionManager.getEntries());
	const matches = [...serialized.matchAll(/dispatch_nonce=([0-9a-f-]{36})/gi)];
	return matches.at(-1)?.[1];
}

async function acknowledgeWorkerDispatch(ctx: ExtensionContext): Promise<void> {
	const dispatchNonce = latestDispatchNonce(ctx);
	if (!dispatchNonce || workerAcknowledgedDispatches.has(dispatchNonce)) return;
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = self.parentThinkthreadId;
	if (typeof parentId !== "string") {
		throw new Error("Goal Plus worker dispatch acknowledgement requires a ThinkThread Child");
	}
	await ensureWorkerRegistration(client, parentId, ctx);
	await client.invoke("message.send", {
		recipientThinkthreadId: parentId,
		text: JSON.stringify({
			protocol: "goal-plus.pi-thinkthread.v2",
			type: "dispatch_ack",
			dispatch_nonce: dispatchNonce,
		}),
		wake: false,
	});
	workerAcknowledgedDispatches.add(dispatchNonce);
}

async function validateThinkThreadRole(ctx: ExtensionContext): Promise<void> {
	const configured = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
	const environmentParentId = process.env.THINKTHREAD_PARENT_ID;
	if (!configured) return;
	// Root session_start can run before the Agent POSIX transport is ready.  Root
	// tools perform their own SDK preflight when first used; only a Child needs
	// synchronous role validation and registration before it can act.
	if (role !== "worker") {
		if (environmentParentId) {
			throw new Error("Goal Plus Root role cannot run inside a ThinkThread Child");
		}
		return;
	}
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = typeof self.parentThinkthreadId === "string" ? self.parentThinkthreadId : undefined;
	if (!parentId || !environmentParentId || parentId !== environmentParentId) {
		throw new Error("Goal Plus worker role does not match the authenticated ThinkThread Child");
	}
	const capabilities = Array.isArray(self.capabilities)
		? self.capabilities
			.filter((item: unknown) => isRecord(item) && typeof item.id === "string")
			.map((item: { id: string }) => item.id)
			.sort()
		: [];
	if (capabilities.length !== 1 || capabilities[0] !== "thinkthread.message") {
		throw new Error("Goal Plus Candidate Child must have a Message-only Capability grant");
	}
	await ensureWorkerRegistration(client, parentId, ctx);
}

async function runWorkerMessageRpc(
	name: string,
	params: Record<string, unknown>,
	ctx: ExtensionContext,
	signal: AbortSignal,
) {
	const suppliedSession = params.agent_session_id;
	if (typeof suppliedSession === "string" && suppliedSession.length > 0) {
		workerAgentSessionId = workerAgentSessionId ?? suppliedSession;
		if (workerAgentSessionId !== suppliedSession) {
			throw new Error("worker agent_session_id changed within one retained Session");
		}
	}
	if (!workerAgentSessionId) {
		throw new Error("call search_get_agent_context with agent_session_id first");
	}
	const client = await workerAgentPosixClient();
	const self = await client.invoke("self", {});
	const parentId = self.parentThinkthreadId;
	if (typeof parentId !== "string") {
		throw new Error("Message-backed Goal Plus tools require a ThinkThread Child");
	}
	await ensureWorkerRegistration(client, parentId, ctx);
	const requestId = `rpc_${randomUUID().replaceAll("-", "")}`;
	const hashPayload = {
		agent_session_id: workerAgentSessionId,
		tool: name,
		params,
	};
	const contentJson = canonicalJson(hashPayload);
	const request = {
		protocol: "goal-plus.pi-thinkthread.v2",
		type: "request",
		request_id: requestId,
		...hashPayload,
		content_json: contentJson,
		content_sha256: sha256(contentJson),
	};
	await client.invoke("message.send", {
		recipientThinkthreadId: parentId,
		text: JSON.stringify(request),
		wake: false,
	});

	const chunks = new Map<number, Uint8Array>();
	let expectedChunks: number | undefined;
	let responseHash: string | undefined;
	const deadline = Date.now() + 15 * 60 * 1000;
	while (Date.now() < deadline) {
		if (signal.aborted) throw new Error("Goal Plus worker Message request was cancelled");
		const receiveParams: Record<string, unknown> = {
			senderThinkthreadId: parentId,
			limit: 32,
		};
		if (workerMessageCursor) receiveParams.after = workerMessageCursor;
		const batch = await client.invoke("message.receive", receiveParams);
		if (typeof batch.nextCursor !== "string") {
			throw new Error("Agent POSIX message.receive omitted nextCursor");
		}
		workerMessageCursor = batch.nextCursor;
		for (const message of Array.isArray(batch.messages) ? batch.messages : []) {
			if (!isRecord(message) || typeof message.text !== "string" || message.truncated === true) continue;
			let envelope: unknown;
			try {
				envelope = JSON.parse(message.text);
			} catch {
				continue;
			}
			if (
				!isRecord(envelope) ||
				envelope.protocol !== "goal-plus.pi-thinkthread.v2" ||
				envelope.type !== "response_chunk" ||
				envelope.request_id !== requestId ||
				typeof envelope.chunk_index !== "number" ||
				typeof envelope.chunk_count !== "number" ||
				typeof envelope.data_base64 !== "string" ||
				typeof envelope.chunk_sha256 !== "string" ||
				typeof envelope.response_sha256 !== "string"
			) continue;
			const data = Buffer.from(envelope.data_base64, "base64");
			if (sha256(data) !== envelope.chunk_sha256) throw new Error("worker RPC chunk hash mismatch");
			if (expectedChunks !== undefined && expectedChunks !== envelope.chunk_count) {
				throw new Error("worker RPC chunk count changed");
			}
			if (responseHash !== undefined && responseHash !== envelope.response_sha256) {
				throw new Error("worker RPC response hash changed");
			}
			expectedChunks = envelope.chunk_count;
			responseHash = envelope.response_sha256;
			chunks.set(envelope.chunk_index, data);
		}
		if (expectedChunks !== undefined && chunks.size === expectedChunks) {
			const pieces = Array.from({ length: expectedChunks }, (_unused, index) => chunks.get(index));
			if (pieces.some((piece) => piece === undefined)) throw new Error("worker RPC response has missing chunks");
			const data = Buffer.concat(pieces as Uint8Array[]);
			if (sha256(data) !== responseHash) throw new Error("worker RPC response hash mismatch");
			const response = JSON.parse(data.toString("utf8"));
			if (!isRecord(response) || typeof response.ok !== "boolean") {
				throw new Error("worker RPC response envelope is invalid");
			}
			try {
				await client.invoke("message.send", {
					recipientThinkthreadId: parentId,
					text: JSON.stringify({
						protocol: "goal-plus.pi-thinkthread.v2",
						type: "response_ack",
						request_id: requestId,
						response_sha256: responseHash,
					}),
					wake: false,
				});
			} catch {
				// The response is already hash-verified. Root may safely replay its
				// idempotent chunks if this best-effort acknowledgement was lost.
			}
			if (!response.ok) {
				const error = isRecord(response.error) ? response.error : {};
				throw new Error(typeof error.message === "string" ? error.message : "worker RPC failed");
			}
			return {
				content: [{ type: "text" as const, text: JSON.stringify(response.result, null, 2) }],
				details: response.result,
			};
		}
		await new Promise((resolve) => setTimeout(resolve, 200));
	}
	throw new Error(`worker RPC ${name} timed out waiting for Root`);
}

function persistGoalState(pi: ExtensionAPI) {
	pi.appendEntry(STATE_ENTRY_TYPE, {
		activeGoalPlusId,
		continuationCount,
		startedAt: activeGoalStartedAt,
		startEntryCount: activeGoalStartEntryCount,
		status: cachedGoalStatus?.status,
		phase: cachedGoalStatus?.phase,
		updatedAt: new Date().toISOString(),
	} satisfies GoalPlusNativeState);
}

function canPersistGoalState(mode: string | undefined): boolean {
	return mode !== "print" && mode !== "json";
}

function restoreGoalState(ctx: ExtensionContext) {
	const entries = ctx.sessionManager.getEntries();
	const stateEntry = entries
		.filter((entry: { type: string; customType?: string }) => entry.type === "custom" && entry.customType === STATE_ENTRY_TYPE)
		.pop() as { data?: GoalPlusNativeState } | undefined;
	if (!stateEntry?.data) return;
	activeGoalPlusId = stateEntry.data.activeGoalPlusId ?? activeGoalPlusId;
	continuationCount = stateEntry.data.continuationCount ?? continuationCount;
	activeGoalStartedAt = stateEntry.data.startedAt ?? activeGoalStartedAt;
	activeGoalStartEntryCount = stateEntry.data.startEntryCount ?? activeGoalStartEntryCount;
}

function activateGoal(pi: ExtensionAPI, details: unknown, startEntryCount?: number, persist = true) {
	const id = goalPlusIdFrom(details);
	if (!id) return;
	if (id !== activeGoalPlusId || !activeGoalStartedAt) {
		activeGoalStartedAt = new Date().toISOString();
		activeGoalStartEntryCount = startEntryCount ?? activeGoalStartEntryCount;
		continuationCount = 0;
	}
	activeGoalPlusId = id;
	cachedGoalStatus = statusFrom(details);
	if (persist) persistGoalState(pi);
}

async function refreshActiveGoal(
	pi: ExtensionAPI,
	ctx: CommandRuntimeContext,
	persist = true,
): Promise<GoalPlusStatusPayload | undefined> {
	if (!activeGoalPlusId) return undefined;
	const result = await runJsonCli(pi, ctx, "goal_plus_status", { goal_plus_id: activeGoalPlusId });
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) return undefined;
	cachedGoalStatus = status;
	if (persist) persistGoalState(pi);
	return status;
}

function isTerminalStatus(status: string | undefined): boolean {
	return status === "blocked" || status === "complete" || status === "abandoned";
}

function formatDuration(ms: number): string {
	const seconds = Math.max(0, Math.floor(ms / 1000));
	const hours = Math.floor(seconds / 3600);
	const minutes = Math.floor((seconds % 3600) / 60);
	const remainingSeconds = seconds % 60;
	const parts: string[] = [];
	if (hours > 0) parts.push(`${hours}h`);
	if (minutes > 0 || hours > 0) parts.push(`${minutes}m`);
	parts.push(`${remainingSeconds}s`);
	return parts.join(" ");
}

function countToolCalls(content: unknown): number {
	if (!Array.isArray(content)) return 0;
	return content.filter((item) => isRecord(item) && item.type === "toolCall").length;
}

function collectGoalUsageFromEntries(entries: unknown[]): GoalPlusUsageTotals {
	const startIndex = Math.min(Math.max(0, activeGoalStartEntryCount), entries.length);
	const totals: GoalPlusUsageTotals = {
		assistantMessages: 0,
		toolCalls: 0,
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		cost: 0,
	};
	for (const entry of entries.slice(startIndex)) {
		if (!isRecord(entry) || entry.type !== "message" || !isRecord(entry.message)) continue;
		const message = entry.message;
		if (message.role !== "assistant") continue;
		const usage = isRecord(message.usage) ? message.usage : undefined;
		const cost = usage && isRecord(usage.cost) ? usage.cost : undefined;
		totals.assistantMessages += 1;
		totals.toolCalls += countToolCalls(message.content);
		totals.input += numberFrom(usage?.input);
		totals.output += numberFrom(usage?.output);
		totals.cacheRead += numberFrom(usage?.cacheRead);
		totals.cacheWrite += numberFrom(usage?.cacheWrite);
		totals.cost += numberFrom(cost?.total);
	}
	return totals;
}

function buildGoalStatsMessage(status: GoalPlusStatusPayload, usage: GoalPlusUsageTotals): string {
	const startedAtMs = activeGoalStartedAt ? Date.parse(activeGoalStartedAt) : NaN;
	const elapsedMs = Number.isFinite(startedAtMs) ? Date.now() - startedAtMs : 0;
	const totalTokens = usage.input + usage.output + usage.cacheRead + usage.cacheWrite;
	return [
			"Goal Plus 统计",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`status: ${status.status ?? "unknown"}`,
		`search_tasks: ${status.search_tasks_total ?? status.search_tasks?.length ?? 0}`,
		`elapsed: ${formatDuration(elapsedMs)}`,
		`assistant_messages: ${usage.assistantMessages}`,
		`tool_calls: ${usage.toolCalls}`,
		`tokens: input=${usage.input.toLocaleString()} output=${usage.output.toLocaleString()} cache_read=${usage.cacheRead.toLocaleString()} cache_write=${usage.cacheWrite.toLocaleString()} total=${totalTokens.toLocaleString()}`,
		`estimated_cost: $${usage.cost.toFixed(4)}`,
	].join("\n");
}

function appendGoalStats(pi: ExtensionAPI, status: GoalPlusStatusPayload, usage: GoalPlusUsageTotals): string {
	const endedAt = new Date().toISOString();
	const message = buildGoalStatsMessage(status, usage);
	pi.appendEntry<GoalPlusStatsEntry>(GOAL_PLUS_STATS_ENTRY_TYPE, {
		goal_plus_id: status.goal_plus_id ?? activeGoalPlusId,
		status: status.status,
		startedAt: activeGoalStartedAt,
		endedAt,
		usage,
		message,
	});
	return message;
}

function buildGoalPlusContext(status: GoalPlusStatusPayload): string {
	const action = status.next_action;
	const lines = [
		"[GOAL PLUS ACTIVE]",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`status: ${status.status ?? "unknown"}`,
		`phase: ${status.phase ?? "unknown"}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
		`final_check_policy: ${JSON.stringify(status.policy?.final_check ?? { mode: "disabled" })}`,
		"",
			"原始目标：",
		status.raw_goal ?? "",
		"",
			"规则：",
			"- 将原始目标与实现猜测分开。",
			"- 每次 phase 变化后更新 goal-plus 状态。",
			"- Search 是自主升级：spec draft 达到高置信度且无 open question 后，直接通过 Search Mode gate，不要请求用户批准。",
			"- 声称完成前，对照当前证据审计原始目标，并调用 goal_plus_set_status。",
			"- 如果 final_check.mode 为 required，必须由该目标修订版通过独立最终检查来完成。",
	];
	if (action) {
		lines.push(
			"",
				"当前 next_action：",
			`- kind: ${action.kind ?? "unknown"}`,
			`- required: ${action.required === false ? "false" : "true"}`,
			`- description: ${action.description ?? ""}`,
		);
	}
	return lines.join("\n");
}

interface GoalPlusRoleModels {
	main?: string;
	annotator?: string;
	workerDirective?: "workers" | "models";
}

function commandDirective(rawGoal: string, name: string): string | undefined {
	const marker = new RegExp(`(?:^|\\s)${name}=`, "gi");
	const markers = [...rawGoal.matchAll(marker)];
	if (markers.length > 1) {
		throw new Error(`/goal-plus accepts at most one ${name}= directive`);
	}
	if (markers.length === 0) return undefined;
	const value = rawGoal.match(new RegExp(`(?:^|\\s)${name}=([^\\s]+)`, "i"))?.[1]?.trim();
	if (!value) throw new Error(`${name}= requires a value`);
	return value;
}

function roleModelDirective(rawGoal: string, name: string): string | undefined {
	const value = commandDirective(rawGoal, name);
	if (value?.includes(",")) {
		throw new Error(`${name}= accepts exactly one model; use workers= for a list`);
	}
	return value;
}

function parseGoalPlusRoleModels(rawGoal: string): GoalPlusRoleModels | undefined {
	if (/(?:^|\s)model=/i.test(rawGoal)) {
		throw new Error("model= is not supported; use main=, annotator=, and workers=");
	}
	const main = roleModelDirective(rawGoal, "main");
	const annotator = roleModelDirective(rawGoal, "annotator");
	const workers = commandDirective(rawGoal, "workers");
	const models = commandDirective(rawGoal, "models");
	if (workers && models) {
		throw new Error("workers= and models= are aliases; specify only one");
	}
	const workerModels = workers ?? models;
	if (workerModels?.split(",").some((value) => value.trim().length === 0)) {
		throw new Error("workers=/models= contains an empty model reference");
	}
	if (!main && !annotator && !workerModels) return undefined;
	return {
		main,
		annotator,
		workerDirective: workers ? "workers" : models ? "models" : undefined,
	};
}

function resolvePiModelReference(ctx: ExtensionContext, requested: string) {
	const needle = requested.toLowerCase();
	const available = ctx.modelRegistry.getAvailable();
	const canonical = available.filter(
		(model) => `${model.provider}/${model.id}`.toLowerCase() === needle,
	);
	const exactIds = available.filter((model) => model.id.toLowerCase() === needle);
	const exactNames = available.filter((model) => model.name.toLowerCase() === needle);
	const matches = canonical.length > 0 ? canonical : exactIds.length > 0 ? exactIds : exactNames;
	if (matches.length === 0) {
		throw new Error(`requested Pi model is not available: ${requested}`);
	}
	if (matches.length > 1) {
		throw new Error(`requested Pi model is ambiguous; use provider/model: ${requested}`);
	}
	return matches[0]!;
}

async function applyGoalPlusRoleModels(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	rawGoal: string,
): Promise<GoalPlusRoleModels | undefined> {
	const requested = parseGoalPlusRoleModels(rawGoal);
	if (!requested) return undefined;
	const main = requested.main
		? resolvePiModelReference(ctx, requested.main)
		: undefined;
	const annotator = requested.annotator
		? resolvePiModelReference(ctx, requested.annotator)
		: undefined;
	if (main) {
		const selected = await pi.setModel(main);
		if (!selected) {
			throw new Error(`Pi cannot authenticate the requested main model: ${requested.main}`);
		}
	}
	return {
		main: main ? `${main.provider}/${main.id}` : undefined,
		annotator: annotator ? `${annotator.provider}/${annotator.id}` : undefined,
		workerDirective: requested.workerDirective,
	};
}

function buildGoalStartPrompt(
	status: GoalPlusStatusPayload,
	roleModels?: GoalPlusRoleModels,
): string {
	const lines = [
		"继续此 Goal Plus 任务。",
		"",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId ?? "unknown"}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
		"",
		"原始目标：",
		status.raw_goal ?? "",
	];
	if (roleModels) {
		lines.push(
			"",
			"已解析的模型路由：",
		);
		if (roleModels.main) lines.push(`- main: ${roleModels.main}（已切换）`);
		if (roleModels.annotator) lines.push(`- annotator: ${roleModels.annotator}`);
		if (roleModels.workerDirective) {
			lines.push(`- workers: 使用原始目标中的 ${roleModels.workerDirective}= 分配。`);
		}
		lines.push(
			"- 冻结 SearchSpec 时，只写入显式角色：annotator 写入 strategy.evidence_annotator.model；workers=/models= 按原有分配规则写入 strategy.models。未指定的角色保持现有 host 默认或继承语义。",
		);
	}
	lines.push(
		"",
		"重要事项：",
		"- goal_plus_create 工具已经创建此记录。不要为该目标再次调用 goal_plus_create。",
		"- 加载并遵循 goal-plus skill。",
		"- 以最新用户消息作为判断继续、修订或讨论无关内容的权威依据；不要仅因 Goal Plus 处于 active 就恢复工作。",
		"- 如果消息改变了实际范围、交付物或成功标准，使用完整修订后的原始目标和当前 expected_revision 调用 goal_plus_update_goal，然后重新 triage。否则保持修订版不变，并在恢复前澄清有歧义的意图。",
		"- 除了加载 goal-plus skill 之外，在 goal_plus_record_triage 前不要读取或审计目标文件。",
		"- 首先使用 goal_plus_record_triage 记录 triage。",
		"- 如果原始目标明确要求 verifier 引导的 Search Mode，并提供可度量的 verifier 或 metric，不要将其降级为普通 Goal Mode。",
		"- 如果任务已准备好进入 Search，通过 frozen-spec 和 Search Mode gate 自主进入 Search Mode；不要要求用户批准该转换。",
		"- 绝不能编造 frozen_spec_id、run_id、plan_id、candidate_id 或 agent_session_id。只使用紧邻的前序运行时工具返回的准确 id；在 goal_plus_link_search_run 前调用 search_create。",
		"- 如果尚未准备好进入 Search，在 Goal Mode 中继续，并在停止前更新 goal-plus 状态。",
		"- 如果该记录要求最终检查，调用 goal_plus_prepare_final_check(checker_host=\"pi\")，然后把其 launch payload 传给 pi_goal_plus_run_final_check。",
	);
	return lines.join("\n");
}

interface GoalPlusSlashRequest {
	action: "start" | "edit" | "resume";
	rawGoal: string;
	withFinalCheck: boolean;
}

function goalPlusRequestFromSlashInput(text: string): GoalPlusSlashRequest | undefined {
	const match = text.match(/^\/(goal-plus(?:-with-final-check)?)(?:\s+([\s\S]*))?$/);
	if (!match) return undefined;
	const command = match[1];
	const body = (match[2] ?? "").trim();
	if (command === "goal-plus" && body.toLowerCase() === "resume") {
		return { action: "resume", rawGoal: "", withFinalCheck: false };
	}
	if (command === "goal-plus" && body.toLowerCase().startsWith("edit ")) {
		return { action: "edit", rawGoal: body.slice(5).trim(), withFinalCheck: false };
	}
	return {
		action: "start",
		rawGoal: body,
		withFinalCheck: command === "goal-plus-with-final-check",
	};
}

async function createGoalPlusStart(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	rawGoal: string,
	withFinalCheck = false,
): Promise<string | undefined> {
	let roleModels: GoalPlusRoleModels | undefined;
	try {
		roleModels = await applyGoalPlusRoleModels(pi, ctx, rawGoal);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		ctx.ui.notify(message, "error");
		pi.sendMessage({
			customType: "goal-plus-error",
			content: message,
			display: true,
			details: { stage: "model-routing" },
		});
		return undefined;
	}
	const commandCtx = commandContextFrom(ctx);
	const startEntryCount = ctx.sessionManager.getEntries().length;
	const result = await runJsonCli(pi, commandCtx, "goal_plus_create", {
		raw_goal: rawGoal,
		source_path: ctx.cwd,
		policy: withFinalCheck ? { final_check: { mode: "required" } } : undefined,
	});
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) {
		const details =
			isRecord(result.details) && typeof result.details.error === "string"
				? result.details.error
				: "goal_plus_create did not return a goal_plus_id";
		pi.sendMessage({
			customType: "goal-plus-error",
			content: details,
			display: true,
			details: { tool: "goal_plus_create" },
		});
		return undefined;
	}
	activateGoal(pi, status, startEntryCount, canPersistGoalState(ctx.mode));
	pi.sendMessage({
		customType: "goal-plus-created",
		content: `Goal Plus ${status.goal_plus_id} created`,
		display: true,
		details: { goal_plus_id: status.goal_plus_id },
	});
	return buildGoalStartPrompt(status, roleModels);
}

async function updateGoalPlusStart(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	rawGoal: string,
): Promise<string | undefined> {
	if (!activeGoalPlusId) {
		ctx.ui.notify("No active Goal Plus record to edit", "error");
		return undefined;
	}
	const commandCtx = commandContextFrom(ctx);
	const current = await refreshActiveGoal(pi, commandCtx, canPersistGoalState(ctx.mode));
	if (!current?.goal_plus_id || typeof current.goal_revision !== "number") return undefined;
	const result = await runJsonCli(pi, commandCtx, "goal_plus_update_goal", {
		goal_plus_id: current.goal_plus_id,
		raw_goal: rawGoal,
		expected_revision: current.goal_revision,
		reason: "user edited the Goal Plus objective through Pi",
	});
	const status = statusFrom(result.details);
	if (!status?.goal_plus_id) return undefined;
	activateGoal(pi, status, undefined, canPersistGoalState(ctx.mode));
	return [
			"用户已编辑 Goal Plus 目标。",
		`goal_plus_id: ${status.goal_plus_id}`,
		`goal_revision: ${status.goal_revision ?? "unknown"}`,
			"新的原始目标取代之前的目标。继续前重新运行 goal_plus_record_triage。",
			"以前的 Search 任务仅作为历史证据保留；不要把旧修订版的结果或最终检查视为当前结果。",
	].join("\n");
}

async function resumeGoalPlusStart(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	if (isThinkThreadProfile) {
		lines.push(
			"",
			"ThinkThread Profile contract：",
			"- SearchSpec 使用 worker_host=pi-thinkthread 并省略 workspace。",
			"- Candidate 只通过 Message-backed tools 访问 Root runtime；不要启动 legacy Pi RPC worker。",
		);
	}
): Promise<string | undefined> {
	if (!activeGoalPlusId) {
		ctx.ui.notify("No interrupted Goal Plus record to resume", "error");
		return undefined;
	}
	const status = await refreshActiveGoal(
		pi,
		commandContextFrom(ctx),
		canPersistGoalState(ctx.mode),
	);
	if (!status || isTerminalStatus(status.status)) {
		ctx.ui.notify("The previous Goal Plus record is already terminal", "error");
		return undefined;
	}
	return [
			"从持久化运行时状态恢复被中断的 Goal Plus 任务。",
		`goal_plus_id: ${status.goal_plus_id ?? activeGoalPlusId}`,
		`goal_revision: ${status.goal_revision ?? 1}`,
			"将当前原始目标、修订版、next_action、Search history 和最终检查状态视为权威依据。",
			"不要重新创建 Goal Plus 记录，也不要静默重启已完成的 phase。",
	].join("\n");
}

function sendUserMessage(pi: ExtensionAPI, message: string, deliverAsFollowUp: boolean) {
	if (!deliverAsFollowUp) {
		pi.sendUserMessage(message);
		return;
	}
	pi.sendUserMessage(message, { deliverAs: "followUp" });
}

function registerRuntimeTool(pi: ExtensionAPI, name: string) {
	pi.registerTool({
		name,
		label: name,
		description: RuntimeToolDescriptions[name] ?? `调用 goal-plus facade 工具 ${name}。`,
		parameters: toolParameters(name),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const startEntryCount = ctx.sessionManager.getEntries().length;
			const canPersistPiState = canPersistGoalState(ctx.mode);
			const result = await runJsonCli(pi, commandCtx, name, params as Record<string, unknown>);
			if (["goal_plus_create", "goal_plus_update_goal", "goal_plus_submit_final_check"].includes(name)) {
				activateGoal(pi, result.details, startEntryCount, canPersistPiState);
			}
			if (name === "search_get_agent_context") {
				const details = result.details as { workspace?: string } | undefined;
				workspaceRoot = details?.workspace;
				sawContext = true;
			}
			return result;
		},
	});
}

function registerPiFinalCheckTool(pi: ExtensionAPI) {
	pi.registerTool({
		name: "pi_goal_plus_run_final_check",
		label: "Pi Goal Plus Final Check",
		description: "根据 goal_plus_prepare_final_check.launch 启动前台 Pi RPC 最终检查审查员。",
		parameters: toolParameters("pi_goal_plus_run_final_check"),
		executionMode: "sequential",
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const commandCtx = commandContextFrom(ctx);
			const invocation = projectModuleInvocation(commandCtx, "goal-plus-pi-worker", "goal_plus.pi_worker");
			const launch = (params as { launch: Record<string, unknown> }).launch;
			const result = await pi.exec(invocation.command, [
				...invocation.argsPrefix,
				"run",
				"--launch-json",
				JSON.stringify(launch),
			]);
			const goalPlusId = typeof launch.goal_plus_id === "string" ? launch.goal_plus_id : activeGoalPlusId;
			const checkId = typeof launch.check_id === "string" ? launch.check_id : undefined;
			const goalRevision = typeof launch.goal_revision === "number" ? launch.goal_revision : undefined;
			const handle = result.code === 0 ? JSON.parse(result.stdout || "{}") : undefined;
			let statusResult = goalPlusId
				? await runJsonCli(pi, commandCtx, "goal_plus_status", { goal_plus_id: goalPlusId })
				: undefined;
			const status = statusFrom(statusResult?.details);
			const latestCheck = Array.isArray(status?.final_checks) ? status.final_checks.at(-1) : undefined;
			const checkerTimedOut = isRecord(handle) && isRecord(handle.metadata) && handle.metadata.timed_out === true;
			if (
				goalPlusId && checkId && goalRevision !== undefined &&
				isRecord(latestCheck) && latestCheck.check_id === checkId && latestCheck.status === "pending"
			) {
				await runJsonCli(pi, commandCtx, "goal_plus_submit_final_check", {
					goal_plus_id: goalPlusId,
					check_id: checkId,
					goal_revision: goalRevision,
					verdict: "interrupted",
					summary: result.code !== 0
						? "Pi final checker process failed before submitting a verdict."
						: checkerTimedOut
							? "Pi final checker timed out before submitting a verdict."
							: "Pi final checker exited before submitting a verdict.",
					checker_metadata: { exit_code: result.code, timed_out: checkerTimedOut },
				});
				statusResult = await runJsonCli(pi, commandCtx, "goal_plus_status", { goal_plus_id: goalPlusId });
			}
			if (result.code !== 0) {
				const failure = commandFailure("pi_goal_plus_run_final_check", invocation, result);
				const details = { ...failure.details, status: statusResult?.details };
				return { content: [{ type: "text" as const, text: failure.text }], details };
			}
			const details = { handle, status: statusResult?.details };
			cachedGoalStatus = statusFrom(statusResult?.details);
			return {
				content: [{ type: "text" as const, text: JSON.stringify(details, null, 2) }],
				details,
			};
		},
	});
}

function extractCandidatePath(event: ToolCallEvent): string | undefined {
	const input = event.input as Record<string, unknown>;
	if (event.toolName === "bash") return String(input.command || "");
	for (const key of ["path", "file_path", "filePath"]) {
		if (typeof input[key] === "string") return input[key] as string;
	}
	return undefined;
}

function workspaceGuard(event: ToolCallEvent) {
	if (role === "final-checker" && ["edit", "write"].includes(event.toolName)) {
		return { block: true, reason: "最终检查审查员只能进行只读操作。" };
	}
	if (role !== "worker") return undefined;
	if (event.toolName === "search_get_agent_context") return undefined;
	if (!sawContext) {
		const readOnly = new Set(["read", "grep", "find", "ls"]);
		if (readOnly.has(event.toolName)) return undefined;
			return { block: true, reason: "使用变更类工具前调用 search_get_agent_context。" };
	}
	if (!workspaceRoot) return undefined;
	if (!["edit", "write", "bash"].includes(event.toolName)) return undefined;
	if (isThinkThreadProfile) {
		lines.push(
			"- 当前是 ThinkThread Root Profile：冻结 SearchSpec 时必须使用 strategy.worker_host=\"pi-thinkthread\"，并完全省略 workspace 字段。不要使用 pi-rpc、Git worktree、goal-plus-pi-worker 或 pi --mode rpc Candidate 链路。",
			"- Worker 模型发现使用 goal_plus_list_models(host=\"pi-thinkthread\")；只使用 Profile 实际 delegated 的 exact provider/model。reasoning effort 和 service tier 不受支持。",
		);
	}
	const target = extractCandidatePath(event);
	if (target && target.includes("..")) {
		return { block: true, reason: "workspaceGuard blocked parent-directory path." };
	}
	if (target && target.startsWith("/") && !target.startsWith(workspaceRoot)) {
		return { block: true, reason: "workspaceGuard blocked access outside candidate workspace." };
	}
	return undefined;
}

async function mainGate(event: ToolCallEvent, ctx: ExtensionContext) {
	if (role !== "main") return undefined;
	if (!event.toolName.startsWith("search_") && !MAIN_GATED_TOOLS.has(event.toolName)) return undefined;
	const goalPlusId = activeGoalPlusId;
	if (!goalPlusId) return undefined;
	const commandCtx = commandContextFrom(ctx);
	const gate = await runJsonCli(piForGate, commandCtx, "goal_plus_gate", {
		goal_plus_id: goalPlusId,
		event: "pre_tool_use",
		context: { tool_name: event.toolName, input: event.input },
	});
	const details = gateFrom(gate.details);
	if (details?.decision === "block") {
		return { block: true, reason: details.reason || "goal_plus_gate blocked search tool use" };
	}
	return undefined;
}

let piForGate: ExtensionAPI;

export default function (pi: ExtensionAPI) {
	piForGate = pi;
	if (role === "main" && typeof pi.registerEntryRenderer === "function") {
		pi.registerEntryRenderer<GoalPlusStatsEntry>(GOAL_PLUS_STATS_ENTRY_TYPE, (entry, { expanded }, theme) => {
			const data = entry.data;
			const lines = (data?.message ?? "Goal Plus stats").split("\n");
			const visibleLines = expanded ? lines : lines.slice(0, 2);
			const box = new Box(1, visibleLines.length, (text) => theme.bg("customMessageBg", text));
			visibleLines.forEach((line, index) => {
				const rendered = index === 0 ? `${theme.fg("accent", "[goal-plus]")} ${line}` : theme.fg("dim", line);
				box.addChild(new Text(rendered, 0, index));
			});
			return box;
		});
	}
	if (role === "main" && !isPrintLikeInvocation) {
		pi.registerCommand("goal-plus", {
			description: "运行、编辑或恢复原生 Pi Goal Plus（支持显式角色模型）",
			handler: async (args, ctx) => {
				const request = goalPlusRequestFromSlashInput(`/goal-plus ${args}`);
				if (!request || (request.action !== "resume" && !request.rawGoal)) {
					ctx.ui.notify("Usage: /goal-plus [mode=autonomous|probe] [main=model] [annotator=model] [workers=model,...] <goal>, /goal-plus edit [mode=...] <full revised goal>, or /goal-plus resume", "error");
					return;
				}
				const deliverAsFollowUp = !ctx.isIdle();
				const prompt = request.action === "resume"
					? await resumeGoalPlusStart(pi, ctx)
					: request.action === "edit"
						? await updateGoalPlusStart(pi, ctx, request.rawGoal)
						: await createGoalPlusStart(pi, ctx, request.rawGoal);
				if (prompt) sendUserMessage(pi, prompt, deliverAsFollowUp);
			},
		});
		pi.registerCommand("goal-plus-with-final-check", {
			description: "运行原生 Pi Goal Plus，并要求独立最终检查",
			handler: async (args, ctx) => {
				const rawGoal = args.trim();
				if (!rawGoal) {
					ctx.ui.notify("Usage: /goal-plus-with-final-check [mode=autonomous|probe] [main=model] [annotator=model] [workers=model,...] <goal>", "error");
					return;
				}
				const prompt = await createGoalPlusStart(pi, ctx, rawGoal, true);
				if (prompt) sendUserMessage(pi, prompt, !ctx.isIdle());
			},
		});
	}

	const mainTools = [
		"goal_plus_create",
		"goal_plus_status",
		"goal_plus_update_goal",
		"goal_plus_monitor_snapshot",
		"goal_plus_list_models",
		"goal_plus_record_triage",
		"goal_plus_save_spec_draft",
		"goal_plus_link_search_run",
		"goal_plus_record_search_result",
		"goal_plus_prepare_final_check",
		"goal_plus_submit_final_check",
		"goal_plus_set_status",
		"goal_plus_gate",
		"search_freeze_spec",
		"search_create",
		"search_status",
		"search_invalidate_run",
		"search_list_history",
		"search_plan_next",
		"search_start_batch",
		"search_get_agent_observability",
		"search_run_verifier",
		"search_select",
		"search_report",
		"search_promote",
			"pi_search_pool_open",
		"pi_search_pool_wait_any",
		"pi_search_pool_snapshot",
		"pi_search_pool_continue",
		"pi_search_pool_close",
	];
	const workerTools = [
		"search_get_agent_context",
		"search_get_global_evidence",
		"search_stage_shared_tool",
		"search_copy_shared_tool",
		"search_get_evidence_detail",
		"search_run_verifier",
		"search_list_iterations",
	];
	const finalCheckerTools = ["goal_plus_status", "goal_plus_submit_final_check"];
	const roleTools = role === "worker" ? workerTools : role === "final-checker" ? finalCheckerTools : mainTools;
	for (const tool of roleTools) {
		if (isThinkThreadWorker) registerWorkerMessageTool(pi, tool);
		else registerRuntimeTool(pi, tool);
	}
	if (role === "main") registerPiFinalCheckTool(pi);
	pi.on("input", async (event, ctx) => {
		if (role !== "main" || (ctx.mode !== "print" && ctx.mode !== "json")) {
			return { action: "continue" };
		}
		const request = goalPlusRequestFromSlashInput(event.text);
		if (request === undefined) return { action: "continue" };
		if (request.action !== "resume" && !request.rawGoal) {
			ctx.ui.notify("Goal Plus command requires a goal", "error");
			return { action: "handled" };
		}
		const prompt = request.action === "resume"
			? await resumeGoalPlusStart(pi, ctx)
			: request.action === "edit"
				? await updateGoalPlusStart(pi, ctx, request.rawGoal)
				: await createGoalPlusStart(pi, ctx, request.rawGoal, request.withFinalCheck);
		return prompt
			? { action: "transform", text: prompt, images: event.images }
			: { action: "handled" };
	});
	pi.on("tool_call", async (event, ctx) => {
		return workspaceGuard(event) || (await mainGate(event, ctx));
	});
	pi.on("session_start", async (_event, ctx) => {
		restoreGoalState(ctx);
		if (role !== "main" || !activeGoalPlusId) return;
		const commandCtx = commandContextFrom(ctx);
		const persist = canPersistGoalState(ctx.mode);
		try {
			const status = await refreshActiveGoal(pi, commandCtx, persist);
			if (isTerminalStatus(status?.status)) {
				activeGoalPlusId = undefined;
				activeGoalStartedAt = undefined;
				activeGoalStartEntryCount = 0;
				continuationCount = 0;
				if (persist) persistGoalState(pi);
			}
		} catch {
			// Keep startup non-fatal; the next explicit tool call will surface runtime errors.
		}
	});
	pi.on("before_agent_start", async (_event, ctx) => {
		if (role !== "main" || !activeGoalPlusId) return;
		const commandCtx = commandContextFrom(ctx);
function registerWorkerMessageTool(pi: ExtensionAPI, name: string) {
	pi.registerTool({
		name,
		label: name,
		description:
			RuntimeToolDescriptions[name] ??
			`通过 ThinkThread Message 向 Root 请求 Goal Plus 工具 ${name}。`,
		parameters: toolParameters(name),
		executionMode: "sequential",
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			try {
				const result = await runWorkerMessageRpc(
					name,
					params as Record<string, unknown>,
					ctx,
					signal,
				);
				if (name === "search_get_agent_context") {
					workspaceRoot = process.cwd();
					sawContext = true;
				}
				return result;
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				return {
					content: [{ type: "text" as const, text: message }],
					details: {
						tool: name,
						ok: false,
						error: message,
					},
				};
			}
		},
	});
}

		const status = await refreshActiveGoal(pi, commandCtx, canPersistGoalState(ctx.mode));
		if (!status || isTerminalStatus(status.status)) return;
		return {
			message: {
				customType: "goal-plus-native-context",
				content: buildGoalPlusContext(status),
				display: false,
				details: { goal_plus_id: status.goal_plus_id, phase: status.phase, status: status.status },
			},
		};
	});
	pi.on("agent_end", async (event, ctx) => {
		const lastMessage = event.messages.at(-1);
		const lengthWithoutToolCall =
			lastMessage?.role === "assistant" &&
			lastMessage.stopReason === "length" &&
			countToolCalls(lastMessage.content) === 0;
		if (
			lastMessage?.role === "assistant" &&
			(lastMessage.stopReason === "error" || lastMessage.stopReason === "aborted")
		) {
			return;
		}
		if (ctx.hasPendingMessages()) return;
		if (role === "worker") {
			if (lengthWithoutToolCall) return;
			if (
				!Number.isFinite(workerContinueUntilMs) ||
				workerContinueUntilMs <= 0 ||
				Date.now() >= workerContinueUntilMs
			) {
				return;
			}
			workerContinuationCount += 1;
			pi.sendMessage(
				{
					customType: "goal-plus-worker-continuation",
					content: "继续当前 Candidate 会话。lease 尚未进入 closeout；刷新运行时上下文和可见证据，推进一个实质方向。只有产物发生实质变化后才运行 verifier，不要重复验证未修改的产物。",
					display: false,
					details: { workerContinuationCount, workerContinueUntilMs },
				},
				{ triggerTurn: true, deliverAs: "followUp" },
			);
			return;
		}
		if (role !== "main" || !activeGoalPlusId) return;
		const commandCtx = commandContextFrom(ctx);
		const mode = ctx.mode;
		const persist = canPersistGoalState(mode);
		const usage = collectGoalUsageFromEntries(ctx.sessionManager.getEntries() as unknown[]);
		const gate = await runJsonCli(pi, commandCtx, "goal_plus_gate", {
			goal_plus_id: activeGoalPlusId,
			event: "stop",
			context: { mode, continuationCount },
		});
		const details = gateFrom(gate.details);
		if (!details) return;
		if (details.decision === "block") {
			continuationCount += 1;
			if (persist) persistGoalState(pi);
			pi.sendMessage(
				{
					customType: "goal-plus-stop-continuation",
					content: details.continuation_prompt || details.reason || "Goal Plus 仍处于 active。继续下一项必需 action。",
					display: true,
					details: { goal_plus_id: activeGoalPlusId, continuationCount },
				},
				{ triggerTurn: true, deliverAs: "followUp" },
			);
			return;
		}
		const status = await refreshActiveGoal(pi, commandCtx, persist);
		if (isTerminalStatus(status?.status)) {
			const statsMessage = appendGoalStats(pi, status, usage);
			ctx.ui.notify(statsMessage, "info");
			activeGoalPlusId = undefined;
			activeGoalStartedAt = undefined;
			activeGoalStartEntryCount = 0;
			continuationCount = 0;
			cachedGoalStatus = undefined;
			if (persist) persistGoalState(pi);
		}
	});
}
		"search_recover_pi_thinkthread",
		if (isThinkThreadProfile) await validateThinkThreadRole(ctx);
		if (role === "worker") return;
		if (isThinkThreadWorker) {
			await acknowledgeWorkerDispatch(ctx);
			return;
		}
		if (role === "worker") return;

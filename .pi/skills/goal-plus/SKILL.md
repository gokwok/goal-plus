---
name: goal-plus
description: 当 Pi 收到可能需要 Goal Mode、Spec Discovery Mode、有界 Search Mode 或独立最终审查员的 /goal-plus、/goal-plus edit 或 /goal-plus-with-final-check 请求时使用。
---

# Pi 的 Goal Plus

## 入口契约

原生 Pi `/goal-plus` 命令会在模型轮次开始前创建 Goal Plus 记录。
`/goal-plus-with-final-check` 创建记录时会设置 `policy.final_check.mode="required"`。
`/goal-plus edit <完整的修订目标>` 对 active 记录调用 `goal_plus_update_goal` 并递增
`goal_revision`；最新原始目标取代旧修订版。Pi 轮次中断后，`/goal-plus resume`
会继续同一个持久化 active 修订版。如果使用兼容 prompt 路径且尚无 active 的
`goal_plus_id`，第一次工具调用必须是 `goal_plus_create(raw_goal=...)`。
目标记录存在之前，不要 triage、Search 或编辑。除了加载 goal-plus skill 之外，
在 `goal_plus_record_triage` 前不要读取或审计目标文件。

`/goal-plus mode=autonomous <目标>` 使用冻结 SearchSpec 的 worker budget、host 强制执行的
lease、外层剩余时间和收尾预留来确定探索时间，并使用可续期的同候选 continuation；
不另行规定固定的分钟或小时上限。
`/goal-plus mode=probe <目标>` 选择短期可行性、潜力和阻塞因素探查。省略 mode 时默认
使用 `autonomous`；未指定 mode 的编辑保留当前选择。运行时只把它存为 `raw_goal`
的规范末行，不把它作为 phase、Search strategy 或运行时字段。

恢复 active 记录前，把最新用户消息视为本轮权威依据。消息只继续或引导现有目标时，
保留当前修订版。如果它改变了实际范围、交付物或成功标准，使用完整修订目标和当前
`expected_revision` 调用 `goal_plus_update_goal`，然后在继续工作前重新 triage。
如果消息无关，直接回复而不改变目标。如果它与目标的关系不明确，在修订或恢复前先澄清；
不要仅因 Goal Plus 记录处于 active 就恢复工作。

## Goal Mode

当请求尚不是可验证的优化/Search 任务时使用 Goal Mode。使用
`goal_plus_record_triage({ goal_plus_id, triage: { is_optimization, confidence, recommended_phase, identified_at, scenario, reasons, missing } })`
记录 triage，并将面向用户的目标与实现猜测分开。Goal Mode 下不要创建 SearchSpec。

如果原始目标明确要求 verifier 引导的 Search Mode，并提供可度量的 verifier 或 metric，
将其分类为优化/Search；不要仅因请求的 run 较小就将其降级为普通 Goal Mode。

## Spec Discovery Mode

当目标需要冻结的 verifier 或编辑范围时使用 Spec Discovery Mode。使用
`goal_plus_save_spec_draft` 保存候选细节。draft 达到高置信度且无 open question 后，
自动升级到 Search Mode。不要要求用户批准 verifier、metric、编辑范围、提升规则或 mode
变化。用户提示有用但可选；从工作区发现缺失细节，并依据证据决策。

ranking verifier 必须输出一个最终 JSON 对象，其中包含有限数值类型的
`spec.metric_name`，例如 `{"combined_score": 123.0}`。命令可以内联，也可以调用
仓库现有工具。只在必要时创建自定义 verifier 文件；在 Spec Discovery 期间且调用
`search_freeze_spec` 前，使用可用 host 工具将其写入源码拥有的路径，例如
`.goal-plus-verifiers/`，绝不能放在 `.gp/` 或 `.search/` 中。Spec Discovery 允许使用
`bash`、`write` 和 `edit` 检查公开契约并生成该文件。`expected_outputs` 只列出产物路径
或 glob，不解析 stdout。Pi freeze 工具会暴露完整的嵌套 `SearchSpec` schema；
直接填写，不要根据校验错误猜字段。`search_freeze_spec` 会重复 verifier 预检，
契约无效时会在候选 worker 启动前拒绝 spec。

原生 Pi 命令使用显式角色名：`main=A` 切换 Main，`annotator=B` 冻结 Evidence
Annotation 模型，`workers=C,D` 分配 Candidate Worker。只解析用户实际填写的角色；省略的
角色保持现有 host 默认或继承语义，不从另一个显式角色猜默认值。扩展在模型轮次开始前解析
并切换 Main，并把规范化后的完整 `provider/model` Main/Annotator 路由注入启动上下文。
冻结 SearchSpec 时把显式 Annotator 写入 `strategy.evidence_annotator.model`。

不要在 SearchSpec 中生成软 rubric 或预设评价维度。Spec Discovery 只能冻结硬 metric、
verifier、编辑范围、预算和 promotion 合同。开放式补充评价发生在每次 Evidence 结算之后：
独立 annotator 根据当前候选累计 diff 和当时其他已结算候选的快照，自行提出与任务实际
相关的观察维度并动态比较。它不读取 hidden 数据，不产生总分或最终推荐，也不改变硬
PASS/FAIL、数值排名、candidate-local 结算、selection 或 promotion。MainAgent
不负责定义这些维度，也不要根据 benchmark 类型向 annotator 预埋固定清单。

如果原始命令包含 `workers=...` 或兼容别名 `models=...`，先调用
`goal_plus_list_models`：ThinkThread Profile 使用 `host="pi-thinkthread"`，普通 Pi 使用
`host="pi-rpc"`。将用户
填写的名称解析为唯一可用模型并冻结到 `strategy.models`；不存在或不唯一时，在创建
run 前直接返回错误。`workers=A,B max_parallel=4` 表示 A、B、A、B；
`workers=A,B A1B3 max_parallel=4`（等价写法 `workers=A*1,B*3`）表示显式
A1B3，数量之和必须等于
`max_parallel`。`workers=A,B 每个一个` 表示 `max_parallel=2`。`workers=` 与
`models=` 不能同时出现。没有填写任一 Worker 参数时不要探测目录，保持 Pi 当前默认模型。

冻结预检在一次性源码副本中运行，并要求 verifier 保持该工作区只读。编译器产物和临时
输出应放入唯一的 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR` 或 Python
`tempfile.TemporaryDirectory()`。绝不能使用固定 `/tmp` 路径，因为受管 Pi pool
可能并发验证多个候选。任何 `VerifierWorkspaceSideEffect` 都必须修复并重新冻结，
然后 Search run 才能使用候选预算。

对于用语义、大致 shape/dtype 和参考提示描述的 AscendC Direct Invoke 算子目标，
记录 `scenario="ascendc_direct_invoke"`，并完整读取
`examples/ascendc-direct-search/SPEC_DISCOVERY.md`。遵循其中的 request schema
和源码模板。针对准确固定的 Git commit，使用 `knowledge.sources.json` 运行
`materialize_knowledge.py` 生成任务局部 `_skills/`；绝不能复制 live Skill 目录。
将精编的 AKG AscendC tree 作为主要知识，只对未覆盖的算子类别使用声明的 CANNBot
补充。主 agent 生成 Golden、cases、verifier、baseline 和 SearchSpec。调用
`search_freeze_spec` 前，使用 JSON Schema validator 按照
`examples/ascendc-direct-search/request.schema.json` 校验生成的
`_task/operator_request.json`；仅做 JSON 解析或手动字段清单检查不够，校验失败会阻止
冻结。绝不能要求用户运行任务准备器、提供任务目录或编写 verifier。仅支持 Direct Invoke；
生成的知识是只读的，不能启动源码 Agent 或 Plugin 工作流。

该场景自包含。不要调用外部 AscendC Agent、plugin 或编排工作流。

## Search Mode

目标已准备好进入 Search 时：

`origin="initial"` 和 `origin="in_progress"` 仅表示 provenance，遵循相同的自主准入规则。

调用 `search_freeze_spec` 前，确保 SearchSpec strategy 设置
`orchestration_mode: "parallel_loops"`，并依据启动上下文选择唯一 host：

- 通过 `tt pi-goal-plus` 启动的 ThinkThread Root Profile 使用
  `worker_host: "pi-thinkthread"`，且 SearchSpec 必须完全省略 `workspace`。Candidate 使用
  ThinkThread private COW branch、exact FsSnapshot verifier 和 retained Child Session；不得
  启动 Git worktree、`goal-plus-pi-worker` 或 `pi --mode rpc` Candidate 链路。
- 普通 Pi 使用 `worker_host: "pi-rpc"`，并显式设置
  `workspace.backend="git_worktree"`；只有用户明确要求兼容隔离时才能设置 `copy`。

不能省略 host 字段或把 ThinkThread 当作 `workspace.backend`；扩展注入的当前 Profile 合同
是权威依据。

Pi 支持的 strategy name 仅限以下可移植内置子集：

- `agent_guided`、`agent` 或 `default`
- `random` 或 `random_mode`

起草 Pi SearchSpec 时只能提供该子集中的名称。不要静默改写已经冻结但不受支持的 strategy；
让运行时校验拒绝它，并在冻结前创建修正后的 draft。

### Search Run 预算规划

调用 `search_freeze_spec` 前选择整个 run 的候选预算；预算一旦冻结，不能在该 run 内增长。
普通 `parallel_loops` 执行使用 `budget.max_parallel`：它唯一决定初始
candidate/subagent 数。
每个初始候选工作区都是长期自主循环，不创建后续规划轮次或基于质量的替代项。

用户或外层 harness 提供 wall-clock、attempt 或 token 预算时：

1. 为主 agent 最终验证、选择、报告和提升预留时间。
2. 选择 host 能支持的 `max_parallel`。没有更好的资源信号时，建议使用 4。
3. 为每条初始循环提供足够的不间断时间，以创建真实产物和 verifier 证据。
4. 冻结 spec 时根据外层预算和最终收尾预留设置 worker 预算，绝不能根据主 agent
   是否喜欢该候选来决定。
5. 没有全局停止事实时继续恢复。Pi pool 的有效最低 lease 和 closeout 边界只由
   supervisor 判定；主 agent 不得使用 spec 中配置的 `min_runtime_seconds` 再次推算
   提前关闭时间。

subagent 负责其候选工作区内的瓶颈分析、假设选择、特性迁移、结构重启和 rebase 决策。
主 agent 绝不发送偏好的技术方向。低分、一次没有改进的迭代或其他候选领先，
都不是停止或替换条件。

遵循 `raw_goal` 中的探索模式末行。`probe` 模式下，全局 policy 可以在可行性、潜力和阻塞
因素已经可信时停止。`autonomous` 模式下，只要还有外层时间，就为每个 active 候选提供
可续期 lease。任何 worker lease 结束都不会完成 Goal Plus 记录。

1. 调用 `search_freeze_spec`；如果后续 cycle 保持相同 verifier 和编辑契约，
   可复用现有 `frozen_spec_id`
2. `search_create`。首个 run 必须省略 `source_run_id`；如果 strict tool schema 要求
   该属性则传 `null`。只有创建真实后继 run 时才传入已存在的准确 `run_*` ID，绝不能
   传入 `initial` 或 `in_progress`。ThinkThread Root/branch snapshot capture 会在平台调用前
   持久化 caller-owned RequestId。若主进程在 capture admission 后中断，先通过
   `goal_plus_monitor_snapshot` 找回准确 run，再调用
   `search_recover_pi_thinkthread(run_id)`；它只查询或使用同一 RequestId 幂等重放并绑定
   exact FsSnapshotId。绝不能重新调用 `search_create` 或生成新 RequestId 盲重试
3. `goal_plus_link_search_run`
4. 调用且只调用一次 `search_plan_next(requested_k=budget.max_parallel)`，再调用且只调用
   一次 `search_start_batch`，创建全部初始候选。运行时会拒绝 `parallel_loops` 模式下的
   第二份 plan。需要传 proposals 时，每个 proposal 都必须包含 `intent`；`hypothesis`、
   `expected_tradeoff`、`instructions` 和 `metadata` 是可选字段。
5. 调用 `pi_search_pool_open(run_id, candidate_ids, directive?, worker_budgets?, final_verify=true, max_parallel=<budget.max_parallel>)`。
   `worker_budgets` 必须按 `candidate_id` 映射，例如
   `{"c001": {"min_runtime_seconds": 500, "min_verifier_runs": 1, "max_runtime_seconds": 600, "on_exceed": "interrupt"}}`，不能直接传一份
   WorkerBudget。它启动 detached 受管 worker，并立即返回持久化 `pool_id`。
6. 调用 `pi_search_pool_wait_any(pool_id, timeout_seconds=...)`。处理每个返回事件。
   如果顶层结果是 `timed_out=true`、`events=[]` 且 `active_count>0`，这只是本次轮询超时；
   继续调用 `pi_search_pool_wait_any`，不能调用 `pi_search_pool_close`、选择或提升。
   `candidate_ready` 表示 driver 已启动并绑定 agent session，最低累计 lease 已释放，且当前产物
   有 durable process Evidence；仅有一次原始进程退出不代表成功。原生 turn 在最低时间或最低
   verifier 次数前结束时，supervisor 会占用同一个 slot。普通 Pi 自动恢复同一个 session
   和 worktree；ThinkThread 自动 wake 同一个 retained Session 和 private branch。
   如果 worker assistant 因 `stopReason="length"` 结束，且该响应没有 tool call，worker-local
   扩展不会续跑旧上下文；driver 会返回 refresh 信号，pool supervisor 随即在同一 candidate/workspace
   上创建新的 `agent_session_id`。main agent 不参与判断或恢复，已有工作区改动和 durable Evidence 保留。
   最低 lease 到硬上限仍未满足时返回 `timed_out`；pool 关闭返回 `interrupted`，执行失败返回
   `failed`。这些事件都不是 candidate ready，必须按未完成处理。只要返回的
   `active_count>0`，就继续等待 supervisor 的准确状态，不自行推算 lease 剩余时间。
7. 对每个 `candidate_ready`，从 `search_list_history` 或
   `goal_plus_monitor_snapshot` 读取之前和当前的最佳结果。pool 的 `final_verify=true`
   路径会复用匹配当前产物的 durable Evidence；仅在没有匹配 Evidence 时补一次父级 process
   verifier，也就是缺失时补父级 `search_run_verifier`，因此持久化最佳候选/分数是最新的。检查
   `handle.metadata.progress_handoff.model_handoff` 和 `verifier_assessment` 以便恢复或
   识别具体 verifier 失败，但不要用它们选择下一技术方向。诊断稀疏、分数低或没有改进，
   都不是重新冻结、停止或替换候选的理由。如果主 agent 确认 verifier 契约、覆盖范围、
   确定性、目标对齐或基础设施失败，在任何其他 Search action 前执行以下强制停止/重冻结序列：
   1. 使用具体 reason、summary 和 evidence 调用 `search_invalidate_run`；
   2. 调用 `pi_search_pool_close(pool_id, mode="interrupt")`；
   3. 轮询准确 pool，直到 `active_count=0`，并保留终态 handoff；
   4. 修复或重新生成源码拥有的 verifier，并冻结新 spec；
   5. 调用 `search_create(new_frozen_spec_id, source_run_id=old_run_id)`，并把后继 run
      链接到同一个 `goal_plus_id`。
   绝不能选择或提升已失效的 run。其产物、限定范围的问题和特性仍可作为研究输入，
   但每个旧分数都只是历史，每个导入特性都必须在后继契约下重新验证。
8. 每个 `candidate_ready` 验证后，只执行全局停止 policy。如果 policy 为 false，使用能适应
   剩余时间的预算，对该准确 `candidate_id` 调用 `pi_search_pool_continue`。不要把
   `timed_out`、`interrupted` 或 `failed` 当作 candidate-ready continuation。运行时提供以下
   固定中性 continuation prompt：

   ```text
   根据最新提交的证据继续同一条自主搜索循环。
   刷新运行时上下文，自行选择下一个有证据支持的假设，
   验证每项实质变更，并在仍有分配预算时继续工作。
   ```

   普通 Pi continuation 会在同一 worktree 恢复持久化 Pi session；ThinkThread continuation
   则向同一个 retained Child Session 发送 wake Message，并保持同一个 private branch。
   两者都在同一工作区语义下保留 `agent_session_id` 和候选身份。初始 pool 创建后，不要调用
   `search_plan_next`、`search_start_batch` 或任何新候选提交。不要根据排名或改进情况
   改变 continuation。
   普通 Pi 的实现是跨进程原生 session continuation；ThinkThread 则是 retained Child Session continuation。
   主 Pi 轮次中断后，使用 `pi_search_pool_snapshot(run_id=...)` 重新发现 pool；
   后续准确 snapshot 使用 `pool_id`。
9. 正常选择前等待准确 snapshot 的 `active_count=0`，再调用
   `pi_search_pool_close(mode="drain")`。只有 run 已按第 7 步失效时，主 agent 才对仍有
   active job 的 pool 使用 `mode="interrupt"`。运行时会拒绝在真实 closeout reserve 之外
   提前关闭 active pool。pool 关闭后再按提升要求调用 `search_select` 和 `search_promote`。
   `search_select` 对 verifier 记录的 iteration 排名并绑定准确 passing ArtifactRef。普通 Pi
   使用 `git_commit`；ThinkThread 使用 exact `fs_snapshot`，不 checkout 或伪造 Git commit。
   配置的 promotion verifier 仍在选中 artifact 上执行最终 gate。
10. 调用 `goal_plus_record_search_result`。此时不要调用 `search_report`；结果记录只预留
    规范报告路径，不生成 Markdown 或 HTML。
11. 执行原始目标审计。评估/编辑契约仍充分时保留同一个 run。新 incumbent 或低性能路线
    绝不会开启替代 run。只有具体 spec/契约修订或不同的可度量子问题才能创建后继项，
    并使用 `source_run_id`；继承分数在重新验证前仍只是历史。
12. 执行最终原始目标审计。普通记录使用 `goal_plus_set_status` 完成。如果
    `policy.final_check.mode="required"`，则：
    - 调用 `goal_plus_prepare_final_check(checker_host="pi")`
    - 把准确返回的 `launch` 对象传给 `pi_goal_plus_run_final_check`
    - 让无状态、只读的 Pi 审查员自行调用 `goal_plus_submit_final_check`
    - 处理失败发现并准备新检查
    通过的必需检查会原子地把记录标记为 complete。
13. 只有 Goal Plus 记录达到终态（`complete`、`blocked` 或 `abandoned`）后，才对每个
    成功记录的 `run_id` 调用且只调用一次 `search_report`。绝不能生成中间 Goal Plus
    报告。向用户返回最终 Markdown 和 HTML 路径。

顶层 stop gate 会阻止每条仍处于 active 的 Goal Plus 记录，并返回完整当前原始目标以及
创建/检查时间戳和已用时间。使用该 prompt 审计全部要求和目标中已有的任何时间条件。
未完成时继续；否则在停止前记录真实终态。不要编造单独的 Goal Plus deadline。

### 结果后的 Spec 重新评估

首次获得有意义的优化结果后，不要仅因当前冻结 spec 的分数超过 baseline 或相对提升很大，
就推断该 spec 已充分。相对改进是有用证据，但不能证明原始目标已接近满足、重要失败模式
已覆盖，或不存在更深的结构优化。如果没有绝对目标、验收阈值、成功标准或已知上界，
说明该不确定性，并明确考虑表面改进是否仍远未达到有用的成功。

使用现有原始目标审计判断适当响应。分数提高后默认继续当前 run；改变搜索方向、迁移特性
或深入优化产物都不需要新冻结 spec。

- `upgrade_spec`：具体证据表明当前 verifier 或编辑契约错误表达了原始目标，或可度量子问题
  本身必须改变。保存更强 draft，冻结新 spec，并创建新 Search run。绝不能原地修改旧冻结
  产物。诊断稀疏、分数低、进展缓慢或更好的搜索思路都不是充分证据。
- `keep_spec_with_justification`：当前 spec 仍是原始目标的可信 proxy。说明保留它的证据，
  并继续 Search。每条候选循环都仍可探索更深或结构不同的方法；由各 subagent 自行决定。
- `revise_goal`：实际范围、交付物或成功标准需要改变。使用完整修订后的原始目标和当前
  `expected_revision` 调用 `goal_plus_update_goal`，重新 triage，然后为该修订版发现并冻结 spec。

这些标签描述现有原始目标审计中的主 agent 决策；它们不是新的运行时状态、额外工作流 phase
或用户审批 checkpoint。

每个异步完成事件都可能添加 `verifier_assessment`。主 agent 必须及时审查报告的
`concern` 证据，因为某个 worker 可能在其他 worker 仍 live 时发现评估契约缺陷。
核查期间暂停 continuation，但不要因未经确认的 worker 意见中断其他 worker。一旦确认，
`search_invalidate_run` 会阻止新 plan、session、verifier 记录、选择和提升；随后主 agent
必须先中断并等待每个 host worker，再改变 verifier 文件。基础设施失败遵循同一强制路径。
质量或覆盖问题需要证明排名不可靠或缺少原始目标覆盖；如果标准评估器与目标 judge 一致，
则保留它并继续同一个 run。

一条 Goal Plus 记录就是完整用户任务。`search_tasks` 是其仅追加的 Search 任务历史；
每项是一个冻结 spec 上的一个 `run_id`。`linked_search` 只是当前任务的兼容视图。
`parallel_loops` 模式下，一项 Search 任务只有一轮初始规划，但可以包含多个同候选 worker
session 和 verifier iteration。

`goal_revisions` 和 `final_checks` 是仅追加历史。中断的 Pi 轮次会保留 active Goal Plus id；
下一 session/轮次从原生状态恢复。检查员进程退出或超时会把该尝试记录为 `interrupted`；
调用 `goal_plus_prepare_final_check` 启动新尝试。编辑目标会使待处理检查失效，并要求重新
triage 和新检查，而旧 Search 任务仅保留为审计历史。

绝不能编造 `frozen_spec_id`、`run_id`、`plan_id`、`candidate_id` 或
`agent_session_id`。只使用紧邻的前序运行时工具返回的准确 id。尤其要先调用
`search_create`，再调用 `goal_plus_link_search_run`，并链接准确返回的 `run_id`。

`pi_search_pool_*` 工具是 host 拥有的 supervisor，不是 Search 运行时 lifecycle API。
其持久化状态位于 `.gp/host-pools/pi/`。它们强制执行 `max_parallel`，返回 `wait_any`
事件，并能在主 Pi 轮次断开后从持久化状态恢复。普通 Pi pool job 启动前台 Pi RPC worker；
ThinkThread pool job 从同一 baseline spawn private Child，绑定 sender/session/branch，通过 Message
结算 worker tool，并在 turn boundary 执行 restore/shared-tool apply。两条路径都在内部完成
`search_start_agent_session`、host handle 绑定和准确 Evidence 结算。
普通 Pi 的 `final_verify=true` 路径复用当前 durable Evidence，仅在当前产物没有匹配记录时
补一次父级 verifier；ThinkThread 的 worker verifier 已直接绑定 exact snapshot。
这些机械步骤不是公开的 Pi 主 agent 工具。

不要从 Pi 主 agent 调用 `search_start_agent_session`、`search_bind_agent_handle` 或
`search_continue_agent_session`；受管 pool 负责这些机械步骤。对现有候选进行另一次尝试时，
调用 `pi_search_pool_continue`。supervisor 随后在内部使用
`search_continue_agent_session`，记录该步骤，再针对同一个原生 session 启动下一进程。

初始 pool 启动和 continuation 都是非阻塞的。普通 Pi 使用 detached wrapper 管理前台 Pi RPC；
ThinkThread 由同一个 `goal_plus.pi_pool` facade 内的 Root controller 管理 Child、Message、
private branch 和 retained wake，不启动额外 daemon 或 Candidate RPC 进程。
`worker_budget.max_runtime_seconds` 是必需字段并由对应 host controller 强制执行。
`min_runtime_seconds`/`min_verifier_runs` 按一个 pool job 的累计时间和 worker verifier 计数执行；
提前结束会在剩余 max budget 内恢复同一原生 session。基础设施失败、
pool close 或外层 closeout 会停止自动恢复。最低 lease 到硬上限仍未满足的 job 终态为
`timed_out`，不会发布 `candidate_ready`。`worker_budget.max_turns` 只是 prompt 提示。没有公开
的同步候选/batch runner 或手动 pool 提交 API。

只有 pool 返回 `candidate_ready` 且全局停止 policy 为 false 时，才调用
`pi_search_pool_continue`。普通 Pi 重新加载其持久化 session JSONL；ThinkThread wake 同一个
native Child Session。若 host-native continuation 失败，Goal Plus iteration、ArtifactRef、
verifier Evidence 和有界 handoff 仍是权威恢复证据，不能把失败的 execution 当作丢失的
passing snapshot。

每次 continuation launch 都开始新的派发级预算。原生会话中早期派发保存的 deadline、
closeout 或时间提示都只是历史；只遵守最新 launch 之后收到的警告。

例外：出现 `failure_class=VerifierWorkspaceSideEffect`、
`metrics.infrastructure_failure=true` 或 `metrics.candidate_action=stop_and_report` 后，
绝不能重新派发。worker 必须停止，不能清理或重试；主 agent 不能在同一个
`frozen_spec_id` 上恢复候选。修复源码拥有的 verifier，冻结新 spec，并创建新 run。
由 host driver 而不是 MCP 运行时负责结束 pool 中仍在执行的其他 worker。

每个 worker 也会在其绑定 handle 中留下有界 `progress_handoff`。它将可选的
`.tmp/handoff.json` 恢复记录与 runtime 拥有的 artifact 和 verifier snapshot 合并。
`search_get_agent_context` 在 `context.resume` 下暴露它；使用该显式 resume 对象获取产物
和 verifier 事实。原生 Pi 会话可以保留推理和 continuation 指令，但绝不能覆盖持久证据
或 top-N history。

候选在首次派发后继续时，向 `pi_search_pool_continue` 传入显式 `worker_budget`。
这会为同一个原生 session 和候选工作区创建新 Pi 进程，其派发预算从外层剩余时间中选择；
它不会改变冻结 spec。

不要仅因 worker handle 含有 `timed_out=true` 就重新派发。如果候选已经有
`process_passed=true` 且有准确 ArtifactRef 支持的 iteration，该最佳 iteration仍是有效
Search 证据，
并符合后续规划和选择条件。正式 history 在 `score` 和 `best_iteration` 中报告该最佳证据，
而 `latest_score` 和 `latest_process_passed` 保留后续 timeout 或 regression 供诊断。

candidate-local history 由运行时拥有，不是本地 plan 文件。worker 必须先调用
`search_get_agent_context`，并使用 `context.resume`、`context.iterations`、
`context.results` 作为跨 host 恢复来源；普通 Pi 还可看到兼容的
`context.results_tsv`。每轮修改前读取
`search_get_global_evidence`。其他 candidate 的尝试只通过这个窄视图披露；`view=null`
只表示 annotator 尚未更新，worker 不等待或轮询，先依据 ArtifactRef、score、disposition 和
自己的推理独立探索。`context.supplemental_evaluation_enabled=false` 时不要
等待或尝试读取补充评价；启用时仅以 `supplemental_available` 标记可展开的行。仅在线路
停滞、结构性分数跃升、hidden 泛化风险或官方/本地结果
背离时，通过 `search_get_evidence_detail` 按需读取完整评价，且不重复读取同一不可变行。
完整内容包含 annotator 根据实际 Evidence 后验提出的观察维度，以及 annotation task 创建时
对其他已结算候选的动态比较。它不来自
FrozenSpec，不作为硬分、推荐或 promotion gate；worker 可据此形成假设，但应独立核对。仅在
worker 独立判断确有必要时，普通 Pi 才可在当前 workspace 使用
`git diff HEAD <commit> -- <allowed-file>` 做只读比较；ThinkThread 只消费 runtime 物化的
snapshot diff/文件上下文，不调用 Git 或访问 sibling branch。worker verifier 用一句话
`hypothesis` 客观概括本轮实际尝试。兼容 `results.tsv` 仅属于 Git host；ThinkThread 的
Result ledger 直接持久化 exact FsSnapshot ArtifactRef。worker 都不能直接编辑运行时账本。
process verifier 同时返回 candidate-local `disposition`：严格改善为 `keep`，同分为
`retain` 并成为最新工作基线，退化为 `discard`，无有效排名证据为 `failure`。runtime
保留实际被测 artifact，并只在 `discard`/`failure` 后恢复 candidate best；worker 不得
自行 reset verifier-backed 状态。开放式补充评价不改变结算、硬 score 或最终 PASS/FAIL。
如果 worker 提供 handoff，后续 iteration history 会包含最新结构化 `research_summary`；
应使用其中任务特定的结果和问题，避免重复失败变体。

需要候选工具共享时，冻结 spec 必须显式设置 `shared_dir.enabled=true`；
默认关闭。候选侧的发布、Tool View、复制与采用规则由
当前 host 的 `search-candidate-worker` prompt 执行。worker 将显式 source drafts 放在
`.tmp/tool-drafts/`，可用 `search_stage_shared_tool` 安全生成 staging，并在每次归属明确的
process verifier 中提交 `toolization_decision`。决策与 advisory 只进入 iteration、monitor 和
report；实际 staging inventory 与 passing verifier settlement 始终是发布权威，决策本身不进入
Global Evidence，也不改变 hard score、结算、选择或 promotion。

对优化任务，要求 worker 在长时间本地优化循环前创建完整候选产物，并尽早运行
`search_run_verifier`。对 fix/target 任务，要求先编辑允许文件再调用 verifier；
不要把未修改初始状态的验证计为 worker 证据。`search_run_verifier` 会在运行 verifier 前
捕获准确候选 artifact，因此 Search 进展必须体现为带真实 `git_commit` 或 `fs_snapshot`
ArtifactRef 的 verifier iteration，即由 verifier 记录运行时 iteration，不能隐藏在 worker transcript 或临时脚本中。

普通 Pi RPC worker 还会在完成 worker 工具后检查提示性时间估算。存在 verifier 证据后，runner
把可用 worker 或外层任务时间，与“最后一次 subagent verifier - 首个候选 session”的时间
除以 subagent verifier 次数所得平均值进行比较，并在采样候选间汇总。如果剩余时间已无法
容纳一次平均提交，它只向该 Search 候选发送一次提示性 `steer`。这不会停止 worker，
也不会替代 hard watchdog。外层 harness 有端到端 deadline 时，将
`GOAL_PLUS_OUTER_DEADLINE_AT` 设为 RFC 3339 时间戳或 Unix epoch。

## Skill 边界

Pi 将 `goal-plus` 暴露为完整的面向用户 skill。不要把 Search Mode 或特定场景优化指引拆成
其他可见 Pi skill。将领域约束保留在原始用户目标、目标工作区文档或示例文档中，
让 Goal Plus 在开启 Search Mode 前发现有 verifier 支持的 SearchSpec。

## Gates

使用 Search Mode 工具和主 agent 变更类工具（`bash`、`edit` 和 `write`）前，Pi extension
调用 `goal_plus_gate(event="pre_tool_use")`。轮次结束时，extension 调用
`goal_plus_gate(event="stop")`；如果 gate 阻止，它会把 continuation prompt 加入队列并
触发另一个模型轮次。如果 extension 不可用，手动调用相同 gate 并遵循 allow/block 决策。

## 监控

对 active 或已完成的 Goal Plus/Search run，首先使用
`goal_plus_monitor_snapshot(goal_plus_id?, run_id?, stale_after_seconds?)`。
它是主要只读监控路径。

monitor 汇总持久化 `.gp` 证据，包括目标状态、全部已链接 Search 任务、每项任务的规划/已启动
轮次数、聚合任务、候选、worker session、verifier 和 Pi 成本计数、所选候选、所选
ArtifactRef、报告与提升路径、候选分数、每个 iteration artifact、agent session、verifier
iteration、一次性时间提示证据、可用的 Pi host 指标以及 stale/timed-out 警告。
它不会启动、等待或停止 worker。

对一个 worker 使用 `search_get_agent_observability(agent_session_id)`。它跨 host 返回相同的
版本化 model/timing/terminal/usage/context/artifact/handoff schema，绝不返回 prompt、
推理或工具 payload 正文。

如果当前 host 未直接暴露 MCP 工具，使用匹配的 Pi facade，不要手动 tail 状态文件：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"goal_plus_id":"gp_...","run_id":"run_...","stale_after_seconds":120}' \
  --pretty
```

只有 monitor 输出缺少所需字段，或调试特定 transcript、verifier log 或 host 失败时，
才读取原始 `.gp/` 文件或 host log。不要把手动 tail 文件作为主要监控路径。

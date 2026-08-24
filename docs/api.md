# API

`goal-plus --root .gp` exposes the host-neutral MCP surface. Tool schemas and
descriptions from the running server are authoritative; this page is the short
index and ownership guide.

## Goal Plus Tools

| Tool | Purpose |
|---|---|
| `goal_plus_create` | create a durable goal before triage |
| `goal_plus_status` | read goal phase, revision, linked tasks, and evidence |
| `goal_plus_update_goal` | replace the complete effective objective and start a revision |
| `goal_plus_record_triage` | choose ordinary goal work or verifier/spec discovery |
| `goal_plus_save_spec_draft` | persist the typed candidate Search spec |
| `goal_plus_list_models` | list the selected Codex or Pi host's currently available models |
| `goal_plus_link_search_run` | append a frozen Search run to the goal |
| `goal_plus_record_search_result` | attach selected/promotion evidence and reserve canonical final report paths |
| `goal_plus_prepare_final_check` | create a required independent-review request |
| `goal_plus_submit_final_check` | record reviewer verdict for an exact revision |
| `goal_plus_set_status` | set evidence-backed terminal or paused state |
| `goal_plus_gate` | return a hook-friendly allow/block decision |

`goal_plus_update_goal` requires `expected_revision`, preventing a stale agent
from overwriting a newer objective. Search results are keyed by `run_id`, so one
goal can retain multiple search tasks.

## Search Tools

### Spec and run

| Tool | Purpose |
|---|---|
| `search_freeze_spec` | preflight and hash-pin a `SearchSpec` plus verifier artifacts |
| `search_create` | create a `run_id`; optional `source_run_id` snapshots bounded predecessor research |
| `search_status` | read budget use, candidates, and current best |
| `search_invalidate_run` | atomically fence a run after main-confirmed verifier inadequacy |
| `search_list_history` | rank candidates and return current-run feature/verifier research rollups |
| `search_list_iterations` | inspect every verifier iteration for one candidate |
| `goal_plus_monitor_snapshot` | read combined goal/run/session/host evidence without controlling workers |

### Initial candidate allocation

| Tool | Purpose |
|---|---|
| `search_plan_next` | persist the one initial candidate allocation |
| `search_start_batch` | materialize that plan's isolated candidate workspaces |

New Pi/Codex specs use `strategy.orchestration_mode="parallel_loops"`.
`search_plan_next(requested_k)` may be called exactly once; later work resumes
the existing candidates. It plans:

```text
min(requested_k, remaining max_parallel)
```

The standard flow passes `requested_k=budget.max_parallel` for that one planning
call. `budget.max_parallel` is the single initial candidate/live-worker count.

`search_invalidate_run` requires a typed verifier reason, non-empty summary,
and concrete evidence. It changes the run to `aborted` and blocks new planning,
sessions, verifier records, selection, and promotion. It does not own host
workers: the caller must next interrupt the complete host pool and wait for zero
active workers before repairing verifier files.

When a successor is unavoidable, use:

```text
search_create(new_frozen_spec_id, source_run_id=invalidated_or_exhausted_run)
```

For a multi-model run, call `goal_plus_list_models(host=...)`, then define the
user's choices once in frozen `strategy.models`. Entries without `count` are
round-robin expanded to `budget.max_parallel`: `A,B` with four lanes becomes
`A,B,A,B`. When every entry has an explicit count, the counts must sum to
`max_parallel`: a user-level `A1B3` allocation (or `A*1,B*3` shorthand)
becomes `A,B,B,B`. Mixed counted and uncounted
entries are invalid. The runtime resolves every requested name against the
host catalog before freezing and rejects missing or ambiguous names.

The resulting ordered `selected_models` are runtime state, not a second user
input. Each candidate and its agent session retain its selected model through
continuation. All models read the same run-scoped
`search_get_global_evidence` surface.
Each verifier-backed iteration is projected there with its commit, score,
disposition, and an asynchronously generated objective View when available.
Model identity is provenance only and does not alter plan admission or
iteration selection.

The new run exposes `inherited_research` containing a predecessor frontier,
feature ledger, and scoped pitfalls. It marks predecessor scores non-reusable.

### Worker context

| Tool | Caller | Purpose |
|---|---|---|
| `search_start_agent_session` | main | create a provenance handle and host-native launch payload |
| `search_redispatch_candidate` | main | create a fresh session in the same candidate workspace |
| `search_bind_agent_handle` | main/host driver | attach a Codex or Pi native handle |
| `search_continue_agent_session` | main | return native same-worker continuation fields when supported |
| `search_get_agent_context` | candidate worker | load authoritative ids, workspace, candidate-local iterations/results, and resume data |
| `search_get_global_evidence` | candidate worker | project settled worker attempts in the current run as score, disposition, exact attempt commit, and a possibly delayed objective View |
| `search_stage_shared_tool` | candidate worker | copy explicit sources from the caller's `.tmp/tool-drafts/` into bounded `.tmp/share-out` staging; this does not publish them |
| `search_copy_shared_tool` | candidate worker | copy a Tool View-bound shared-dir snapshot into the caller's local inbox for reversible verification |
| `search_get_evidence_detail` | candidate worker | expand one available supplemental evaluation from the caller's current run; independent mode is candidate-local |
| `search_get_agent_observability` | main/monitor | read normalized model, timing, terminal, usage, context, artifact, and handoff evidence for one session |

`search_start_agent_session` does not launch or supervise a worker. The caller
must use the returned `launch` object. A `worker_budget` can be passed to initial
launch, continuation, or redispatch without mutating the frozen spec. Pi pool
minimum fields are cumulative across the internal native-session resumes of one
pool job; ordinary overrides remain dispatch-scoped.

`search_get_agent_context` exposes `supplemental_evaluation_enabled`. When it is
false, workers do not wait for or request supplemental evaluation. When enabled,
`search_get_global_evidence` adds only `supplemental_available=true`; full summary,
dimensions, peer comparisons, and limitations are
fetched for a selected immutable row through `search_get_evidence_detail`.

Worker process verifier calls require a one-line `hypothesis` describing the
realized attempt. With `shared_dir` enabled they may also include a
`toolization_decision`. `staged` requires one or more positive signals and tool
names; `not_applicable` requires a concrete exclusion. Missing or contradictory
decisions produce iteration advisories only. The staging inventory remains
authoritative, and decisions/advisories do not affect score, disposition,
selection, or promotion. `view=null` in Global Evidence means annotation has not been
published yet; workers continue independently and do not wait or poll.
`strategy.config.global_evidence_mode` controls Evidence delivery without
changing the candidate-visible prompt or tool surface. `manual` is the default:
candidates explicitly read the shared run view. `auto` also injects that shared
view as `global_evidence_snapshot` after each successful worker process verifier.
`independent` does not inject and limits explicit reads to the calling
candidate's own Evidence. Parent and promotion verification are unchanged.
Snapshot failures add `global_evidence_warning` without changing the successful
verifier result. `GOAL_PLUS_GLOBAL_EVIDENCE_MODE` can supply the mode before
freeze; the effective value is persisted in the frozen spec, and a conflicting
explicit `strategy.config.global_evidence_mode` is rejected.

Every call persists a `global_evidence_reads` entry on the calling agent
session. The entry records the read timestamp and exact completed
candidate/iteration/commit View references visible at that moment, so reports
can distinguish a View published after the last verifier from one available
before a later attempt. These receipts are observational and never affect
settlement, selection, promotion, or hard PASS/FAIL.
When `shared_dir.enabled=true`, Global Evidence additionally projects only tools whose Tool View has
been generated and runtime-bound. A worker may call `search_copy_shared_tool` with that exact
`tool_id` and `snapshot_hash`; the next process verifier atomically consumes the local copy receipt.
This records a candidate-local adoption but does not create a separate tool score, recommendation, or
selection rule.
`ToolizationDecision` is an iteration-local review fact and never enters Global
Evidence. The publication path remains staging -> attributed passing process
verifier -> immutable shared snapshot -> annotator-bound Tool View -> Global
Evidence -> exact copy receipt -> adopted tool record.
Each worker settlement snapshots the exact attempt base/head, worker host, and
resolved annotator model/provider into an internal task. Codex runs annotations
through ephemeral `codex exec`; Pi runs them through ephemeral, tool-free
`pi --mode json`. Explicit `strategy.evidence_annotator.provider` applies to
Codex and stores only the API-key environment variable name, never the key
value. Pi inherits `PI_MODEL` and `PI_PROVIDER` by default; an annotator model
qualified as `provider/model` or an explicit
`strategy.evidence_annotator.pi_provider` selects an independent Pi provider.
Pi resolves that provider's model and credentials from `PI_CODING_AGENT_DIR`.

`search_get_agent_observability` has one versioned cross-host schema. Schema
version 2 adds `execution.provider` and `usage.processed_tokens`; Pi processed
tokens include input, cache read/write, and output tokens, while Codex uses its
native total-token counter because cached input is already included. Codex
reads its native subagent session JSONL (bound by `SubagentStop` or discovered
from the unique task name); Pi normalizes `metadata.pi_metrics`. Legacy
records may still contain bound metadata. The call never returns prompt,
reasoning, tool arguments, or tool output content,
and never waits for or controls a worker. `goal_plus_monitor_snapshot` embeds
the same object under each `subagents[].observability` while retaining legacy
Pi fields for backward compatibility.

`goal_plus_monitor_snapshot.statistics` is the unified statistical view. Its
selected-run payload reports baseline/target improvement, success rates,
stable terminal duration, time to first verifier/success, worker outcome and
model/provider distributions, candidate lineage, selection survival,
worker-vs-parent verifier counts, promotion report evidence, normalized usage,
efficiency, and data-completeness gaps.
When a Codex Goal Plus transcript is bound, `statistics.orchestrator` reports a
content-free usage delta since Goal Plus creation, and `statistics.total_usage`
combines that delta with worker usage. Per-task statistics are also retained in
`search_tasks[].statistics` and aggregated under
`search_task_aggregate.statistics`.

Worker handoffs remain one bounded protocol. `key_results` supplies feature
ledger entries (artifact, code surface/change, portability/dependencies,
measured effect, verifier result, and incumbent relation), while
`verifier_assessment` reports evidence-backed contract quality. Candidate
history preserves these fields, and top-level `feature_ledger` and
`verifier_assessments` aggregate the current run across candidates outside the
visible ranking frontier as well as those inside it.

Pitfalls are not a run-wide deny list. Their `scope` is `candidate_local`,
`feature_family`, or `evaluation_contract`, with `condition`, evidence artifact,
and `confidence`. Missing scope defaults to candidate-local. A worker's
`verifier_assessment` is advisory until the main agent confirms it and calls
`search_invalidate_run`.

### Verify and finish

| Tool | Purpose |
|---|---|
| `search_run_verifier` | commit and verify the exact attempt, return candidate-local `keep`/`retain`/`discard`/`failure`, keep the latest equal-score attempt, restore best code after regressions or verifier failures, then append exactly one inherited `workspace/results.tsv` row; worker calls pass the exact `run_id`, `candidate_id`, `agent_session_id`, and an objective `hypothesis`, while parent fallback verification omits the session id and does not require a hypothesis |
| `search_select` | restore ranked commits and select the first final-verifier passing state |
| `search_report` | generate final `report.md` and self-contained `report.html`; linked Goal Plus records must already be terminal |
| `search_promote` | export the selected commit as a patch; normal Goal Plus flow has no report to refresh yet |

`report.html` is the complete Goal Plus audit view for the run passed to
`search_report`. When that run belongs to a Goal Plus record, the page keeps
every linked Search task separate and then provides a cross-task aggregate.
Planning-round counts remain in normalized data but do not have a separate
report panel. The report includes unified statistics,
candidate/session/verifier evidence, normalized main-agent usage, explicit
metric gaps, and one independent execution timeline for each Search task. The
Goal Plus state is summarized at the top rather than repeated in a separate
lifecycle panel. Each Search timeline is assembled from run creation,
worker-session observability, verifier iterations, and promotion evidence.
Worker bars use observed host start/end timestamps. Configured maximum or
minimum budgets are not rendered as actual duration. The file has inline
CSS/JavaScript only and is readable without a web server. When the optional
`report` extra is installed, the generator embeds Plotly.js in the file and
replaces the compact best-score strip with a complete per-candidate trajectory
over verifier-call order plus a global best-so-far trace. The generator computes
the score scale and call density before rendering: large positive score ranges use
a logarithmic axis, the complete run stays on one call axis with adaptive tick and
marker density, and failed verifier attempts are marked separately without entering
the score scale or best-so-far trace. Without Plotly, the existing inline SVG score
strip remains the deterministic fallback. Search-space
contour and surface plots are intentionally omitted until durable Search state
contains explicit coordinates or embeddings for those axes. `report.md` remains
the stable text artifact. A recorded Goal Plus Search result reserves both
canonical paths before the files exist. Normal Goal Plus order is select,
promote, record result, final audit, terminal status, then one report generation
per recorded run. Intermediate Goal Plus reports are rejected.

## Pi Local Tools

Pi's extension uses `goal-plus-pi-tool`, a JSON CLI facade over the same Python
runtime. These pool tools are host-local and are not added to the shared MCP
server:

| Tool | Purpose |
|---|---|
| `pi_search_pool_open` | create/recover a fixed pool and launch the initial candidates |
| `pi_search_pool_wait_any` | return new terminal pool events; `candidate_ready` has satisfied its minimum lease and durable Evidence, while `timed_out` has not satisfied the lease |
| `pi_search_pool_snapshot` | inspect one pool or rediscover pools by `run_id` |
| `pi_search_pool_continue` | resume the same candidate and native Pi session in a new process |
| `pi_search_pool_close` | drain or terminate live pool jobs |
| `search_recover_pi_thinkthread` | reconcile caller-owned durable Root/branch snapshot requests after a Goal Plus process crash |

Normal Pi Search uses only these fixed-lane pool tools. The pool does not expose
a submit/refill API and does not plan candidates.

Example read-only call:

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

## SearchSpec Fields That Control Execution

| Field | Meaning |
|---|---|
| `objective` | measurable optimization target |
| `metric_name`, `metric_direction` | ranking value and direction |
| `source_path` | baseline source snapshot |
| `editable_globs`, `forbidden_globs` | candidate edit surface |
| `process_verifiers` | correctness gates |
| `ranking_signals` | metric-producing commands |
| `promotion_verifiers` | checks required before promotion |
| `budget.max_parallel` | single initial candidate/live-worker count |
| `strategy.worker_host` | maintained execution host: `pi-rpc`, `pi-thinkthread`, or `codex` |
| `strategy.worker_budget` | host-enforced upper bound and optional minimum lease |
| `workspace.backend` | `git_worktree` (default) or `copy` |

`pi-thinkthread` is a host selection, not a workspace backend. Its SearchSpec
must omit `workspace`; Root and private Child filesystem state is represented
by immutable `fs_snapshot` ArtifactRefs. Codex and legacy `pi-rpc` retain the
existing `copy`/`git_worktree` behavior.

Every ranking command must exit successfully and print a final JSON object with
a finite numeric value under `metric_name`. Temporary verifier outputs belong
under `GOAL_PLUS_VERIFIER_TMPDIR`; verifier artifacts and evaluation inputs are
hash-pinned.

## Error Semantics

- Validation errors mean the caller must fix the spec or tool arguments.
- Candidate verifier failures are normal search evidence.
- `VerifierWorkspaceSideEffect` with infrastructure-failure metrics means the
  evaluator violated isolation; stop the candidate and repair/refreeze.
- Frozen artifact hash mismatches invalidate scoring.
- A main-confirmed verifier defect requires `search_invalidate_run`, then host
  interruption/quiescence, then a repaired frozen spec and successor run.
- Host timeouts and runner failures are different: a timeout proves deadline
  enforcement, while a runner failure requires host recovery evidence.

See [Shared Plane](shared-plane.md) for call ordering, state, and ownership.

# Debugging Runtime State

How to inspect a running or finished `/goal-plus` run after it enters Search
Mode — what the agents are doing, what scores they have produced, and where to
look when something goes wrong.

This doc covers the project-specific runtime surface plus Codex and Pi RPC
logs.

## Two Layers of State

The runtime owns goal-plus records, specs, the initial candidate allocation,
candidate workspaces, iteration history, verifier scoring, reports, and
promotion patches. Codex or the Pi supervisor owns worker launch, lifecycle,
deadlines, and interruption.
The MCP runtime does not maintain worker lifecycle status, host-sync state, or
worker process cancellation. Its internal Evidence annotator is the exception:
annotation tasks and bounded retry state live under each candidate, while the
single-flight drainer state lives under the run. Debug host lifecycle through
native logs and goal/search state through `.gp/`.

Goal-plus records live under `.gp/goal-plus/<goal_plus_id>/`. One Goal Plus
record may append multiple search tasks; each task points to one Search run
under `.gp/runs/<run_id>/`. New Pi/Codex `parallel_loops` runs contain one
initial plan under `plans/` and may contain many same-candidate sessions and
verifier iterations. Legacy runs may contain multiple plans.

Host logs explain what the worker did, while `.gp/` records goal/search facts
such as candidates, iterations, scores, and verifier output.

## Host-Native Log Inspection

Keep raw host logs under `.gp/host-logs/` or another ignored directory. They
can include prompts, tool inputs, command output, file contents, and credentials
that a tool printed. Do not commit raw logs.

Cross-reference all host logs with the runtime IDs carried in the launch
payload:

- `agent_session_id`
- `candidate_id`
- `host_handle.external_id` or `host_handle.task_name`

### Codex

For scripted or reproducible runs, capture Codex's event stream directly:

```bash
mkdir -p .gp/host-logs
codex exec --json --cd "$PWD" "<goal-plus or search prompt>" \
  > ".gp/host-logs/codex-$(date +%Y%m%d-%H%M%S).jsonl"
```

`codex exec --json` emits JSONL events such as `thread.started`,
`turn.started`, `turn.completed`, `turn.failed`, `item.*`, and `error`. Items
include agent messages, reasoning, command executions, file changes, MCP tool
calls, web searches, and plan updates. `codex exec -o <file>` is useful for the
final answer only; it is not a full trace. `codex exec --ephemeral` intentionally
does not persist session rollout files.

Codex also persists local rollout transcripts by default:

```bash
find "${CODEX_HOME:-$HOME/.codex}/sessions" -name 'rollout-*.jsonl' -print
```

Related local state:

- Session index: `${CODEX_HOME:-$HOME/.codex}/session_index.jsonl`
- Session transcripts: `${CODEX_HOME:-$HOME/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Archived transcripts: `${CODEX_HOME:-$HOME/.codex}/archived_sessions`
- macOS app logs: `~/Library/Logs/com.openai.codex/YYYY/MM/DD`

Goal Plus report generation reads content-free usage from the persisted
`token_count` events. Token totals are native Codex evidence. USD values are
model-rate estimates calculated per response with the versioned
Pi-compatible catalog in `src/goal_plus/codex_pricing.py`; they are not
observed ChatGPT subscription charges. Inspect
`usage.cost_estimate.complete`, `priced_calls`, `unpriced_calls`, and
`catalog` in normalized observability when an HTML report shows partial cost
coverage.

For interactive CLI diagnostics, opt into a plaintext TUI log:

```bash
RUST_LOG=debug codex -c log_dir=./.codex-log
tail -F ./.codex-log/codex-tui.log
```

Useful search patterns for this adapter:

```bash
rg -n "agent_session_id|candidate_id|spawn_agent|wait_agent|send_input|interrupt|budget_control|turn.completed|turn.failed|error" \
  .gp/host-logs/codex-*.jsonl
```

If you are debugging Codex itself from the local `../codex` source checkout,
`codex-rs/rollout-trace/README.md` describes the opt-in
`CODEX_ROLLOUT_TRACE_ROOT` bundle format and offline reducer. The installed CLI
in this environment exposes only the stable `codex debug` subcommands shown by
`codex debug --help`, so treat rollout tracing as source/dev-only unless your
Codex binary exposes it.

For Goal Plus hook debugging on Codex 0.144.1+, inspect events for
`UserPromptSubmit`, `SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, and
`SubagentStop`. A missing precreated record usually means project hooks were
not trusted; a Stop block should identify the session-bound `goal_plus_id` and
the next required action. A Search candidate SubagentStop block should name its
`agent_session_id` and ask for its own verifier call. Once that session's
`counters.verifier_runs` is positive, parent-only selection/report/promotion
must not block the candidate return.

Each automatic `Stop` or `SubagentStop` host-hook invocation against an existing
runtime root writes one atomic, metadata-only record under
`.gp/host-logs/codex-hook-events/<invocation_id>.json`. The record contains the
event name, start/end time, duration, `block` / `allow` / `skipped` / `error`
decision, bounded reason and error text, host `stop_reason`, and any resolved
Goal/Search/session IDs. `SubagentStop` records also keep the opaque Codex
`host_agent_id` when supplied, so an unbound child remains distinguishable
without persisting its transcript path. It does not contain prompts,
transcripts, tool input, or continuation text. Separate files avoid
concurrent-worker append races.
Direct `goal_plus_gate` calls do not create these records, so their counters do
not masquerade as automatic Codex hook invocations. Disabled hooks and Stop
events received before the runtime root exists write nothing.

Generated `report.html` files include a Stop Hook Activity snapshot. It keeps
`Stop` and `SubagentStop` totals separate, breaks decisions down by type, and
groups candidate hooks by `agent_session_id` with a `host_agent_id` fallback.
The expandable invocation table preserves the underlying low-level evidence.
Because the HTML is static, it contains events durable when that report was
generated; regenerate the report to include later hook invocations.
If the event directory itself was not persisted, the report labels calls,
decisions, and hook time as `Not observed` rather than presenting an
unverifiable zero.

The same section includes Candidate Loop Activity. Its stable counters use
durable runtime facts: candidate records for submissions, candidates with at
least one settled iteration for completed Results, `discard` / `failure`
iteration dispositions for rejected Results, accepted native continuations plus
state redispatches for Agent resumes, and automatic Stop/SubagentStop `block`
decisions for stop-hook continuation triggers. Candidate/Result/resume counters
are host-neutral; the stop-hook trigger counter is Codex-specific because Pi
does not expose the same automatic host-hook evidence.

For Codex `parallel_loops`, Loop Agent Stop Outcomes correlates those
`SubagentStop` records with content-free native-session activity and durable
candidate iterations. The summary reports each lane's blocked/allowed stops,
blocked stops followed by a new immutable revision plus verifier, later
improvements, context/global-evidence reads, post-last-verifier reply classes,
and context pressure near the first block versus the final/max sample. The
per-stop continuation table uses the interval from one hook completion to the
next hook start. Tail labels such as `polling-only`, `answer-only`, and
`empty-output` are deterministic diagnostic heuristics, not model intent or
causal proof of a leader effect. If native Codex JSONL or hook records were not
persisted, those cells remain `Not observed`; the report never reconstructs
missing counts from prose.

### Pi RPC

Pi workers are launched by `goal-plus-pi-worker`, not by the MCP
server. Normal Pi Search Mode uses the durable `pi_search_pool_*` supervisor:
open launches the fixed initial lanes, wait-any returns terminal pool events,
continue resumes an existing candidate-ready lane, snapshot rediscovers pools
by `run_id`, and close drains or terminates them. `candidate_ready` requires a
satisfied minimum lease and durable Evidence; an exhausted unsatisfied lease is
`timed_out`. Pool jobs internally start agent sessions, run foreground Pi RPC
worker processes, bind returned handles, and reuse current durable Evidence or
run a fallback final verifier; these mechanical steps are not public main-agent
APIs. A configured minimum lease may span several such foreground processes
while retaining one pool job and native session.

The runner starts:

```bash
pi --mode rpc --approve \
  --session-dir <root>/.gp/host-sessions/pi \
  --session-id <agent_session_id> \
  -e <repo>/.pi/extensions/goal-plus.ts
```

Important paths:

- `.gp/host-logs/pi-rpc-<agent_session_id>.jsonl`: metadata-only event log

The default JSONL keeps event types, tool names/status, usage/counts, and
bounded error summaries. It omits streaming `message_update` events plus prompt,
reasoning, tool payload, and transcript content. Set
`GOAL_PLUS_PI_RAW_LOG=1` for a short debugging run when full RPC
payloads are required; raw mode also creates the duplicate
`.gp/host-logs/pi-rpc-<agent_session_id>.txt` stream.

Useful search pattern:

```bash
rg -n "agent_session_id|candidate_id|search_get_agent_context|search_run_verifier|tool_call|stderr|abort" \
  .gp/host-logs/pi-rpc-*.jsonl
```

For periodic monitoring, prefer the read-only MCP/Pi facade snapshot instead
of repeatedly opening logs:

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_...","stale_after_seconds":600}' \
  --pretty
```

For a detailed terminal dashboard that polls a project directory, use the
repository script. It discovers `.gp`, `.search`, or `.goal-plus`, selects the
latest linked task by default, and shows planning decisions, candidate
intent/hypothesis/tradeoff, verifier evidence, normalized host observability,
handoffs, artifacts, Pi persisted pool state, and monitor warnings:

```bash
./scripts/monitor_goal_plus.sh /path/to/project
./scripts/monitor_goal_plus.sh --once --goal gp_... /path/to/project
./scripts/monitor_goal_plus.sh --run run_... --no-clear /path/to/project
INTERVAL=2 RUN_LIMIT=0 ./scripts/monitor_goal_plus.sh /path/to/project
```

The dashboard is read-only. In particular, its Pi pool section reads the
persisted host snapshot and does not call pool reconciliation, wait, close, or
interrupt operations. The default view is a one-screen summary; add `--verbose`
for full per-worker usage, identity, directive, handoff, advisory, and artifact
details. Use `--json` when the assembled monitor/API payload is more useful
than either human view.

The snapshot summarizes the complete Goal Plus search-task history and the
selected run's detailed state. `search_tasks` contains per-run state, frozen
spec, strategy, and round summaries;
the top-level goal payload includes `goal_revision`, `goal_revisions_total`,
`final_check_policy`, and `latest_final_check`, which are the first fields to
inspect after an interrupted edit or reviewer run.
`search_task_aggregate` totals task, planning-round, started-round, candidate,
worker-session, verifier-run, and known cost counts. Its nested `statistics`
also totals worker outcomes, worker-vs-parent process verifiers, promotion
reports, target attainment, and normalized usage. The selected task retains
the detailed run, strategy, candidate, session, duration/cost/context,
file-mtime, and stale/timed-out views.

Use top-level `statistics.selected_run` for the canonical per-run statistical
view. It separates run age from stable terminal `observed_duration_seconds`,
recovers revision-scoped baseline and target evidence from the Goal Plus event
log, and reports success, timing, verifier provenance, model/provider mix,
candidate lineage, selection survival, usage, efficiency, and missing data.
Metrics requiring SearchEvent/footprint evidence, hardware telemetry, or
historical promotion attempts remain explicit in `unavailable_metrics` rather
than being guessed. `statistics.orchestrator` contains only normalized Codex
counts and identity metadata, never transcript content;
`statistics.total_usage` combines it with worker usage and preserves field
coverage so a known subtotal is not mistaken for complete cost data.

Each selected-run `subagents[]` entry also contains the versioned
`observability` object. Query the same object directly with
`search_get_agent_observability(agent_session_id)` when diagnosing one worker.
Codex resolves native session JSONL metrics; Pi normalizes `pi_metrics`. Both
paths omit prompt, reasoning, and tool payload bodies.
`subagents[].verifier_count` and `session_verifier_count` are session-scoped;
`candidate_verifier_count` is the candidate-wide total.

Pi RPC workers persist native sessions under `.gp/host-sessions/pi/`. Normal Pi
main-agent flow uses `pi_search_pool_continue`; the supervisor calls
`search_continue_agent_session`, launches a new process for the same native
session, and reads metrics with incremental `get_entries(since=...)`. Search MCP
`.gp/runs/...`, verifier iterations, candidate Git commits, and workspace files
remain authoritative. Runner failures are bound as synthetic failure handles,
so monitor warnings include `subagent_runner_failed` and bounded failure
metadata instead of leaving the session apparently running. Pi has a native
turn-level Goal Plus stop gate through the extension `agent_end` event, but no
host process Stop hook. Debug Goal Plus completion through extension pre-tool
guard events, stop continuation messages, and `.gp/goal-plus/...`.

### Pi ThinkThread

Start with the same read-only `goal_plus_monitor_snapshot`, then correlate its
`run_id`, `candidate_id`, `agent_session_id`, exact `fs_snapshot` ArtifactRefs,
publication intent, `fs_requests`, and `fs_cleanup` with the native tree:

```bash
tt ps --forest
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root "$PI_CODING_AGENT_SESSION_DIR/goal-plus" \
  --args-json '{"run_id":"run_..."}' --pretty
```

The Root pool record is under
`${GOAL_PLUS_ROOT}/host-pools/pi/<pool_id>/`. Each job durably binds one Child,
private `fsBranchId`, candidate, agent session, Message cursor, dispatch nonce,
settled request replay, verifier/restore intent, and wake outcome. Treat the
native Child/branch as host lifecycle state and the Goal Plus candidate record
as business Evidence; neither is a substitute for the other.

For a stuck run, inspect in this order:

1. pool `state`, `active_count`, `undelivered_terminal_count`, and job error;
2. `tt ps --forest` for the exact Child and execution state;
3. Message cursor/registration/dispatch acknowledgement and sender binding;
4. durable `fs.request.status` facts projected in the run;
5. snapshot cleanup summary and the final `fs.stat.storage` observation.

Never retry an outcome-unknown fs operation with a new RequestId. Current fs v1
also makes `fs.snapshot.create` and `fs.branch.snapshot` durable: Goal Plus
persists their caller-owned RequestId before capture, then uses
`fs.request.status` or an idempotent replay with that same ID. After a Goal Plus
process crash, run `search_recover_pi_thinkthread(run_id)` from the Root Profile;
it binds the exact recovered snapshot before closing the terminal request.

## `.gp/` Layout

```
.gp/
├── specs/<frozen_spec_id>/
│   ├── frozen_spec.json                          # the frozen SearchSpec
│   └── verifier_artifacts/<path>                 # frozen verifier files (hash-pinned)
└── runs/<run_id>/
    ├── run.json                                  # RunRecord: state, candidates_total/evaluated, best
    ├── best.json                                 # exact current best candidate/iteration/commit pointer
    ├── plans/<plan_id>.json                      # one initial SearchPlan allocation
    ├── candidates/<candidate_id>/
    │   ├── candidate.json                        # CandidateRecord: status, score_report, iterations[], results_ledger[]
    │   ├── task.json                             # CandidateTask snapshot
    │   ├── evidence-annotations/iteration-<n>.json # async View task, retry, usage, immutable iteration/Tool Views
    │   └── logs/iteration-<n>-<verifier>-<id>.log # durable stdout/stderr per call
    ├── workspace/<candidate_id>/                 # the agent's editable workspace
    │   ├── .git/                                 # agent and runtime ledger Git history
    │   ├── results.tsv                           # committed, runtime-owned inherited append-only ledger
    │   └── <allowed_files>
    ├── shared/index.json                         # optional runtime-internal tool publication index
    ├── shared/tools/<candidate_id>/<iteration>/  # runtime-owned peer-readable snapshots
    ├── agent_sessions/<agent_session_id>.json    # AgentSessionRecord: candidate/host binding, launch payload, counters
    ├── report.md / report.html                   # text and self-contained audit reports
    └── promotion/                                # selected patch outputs
```

Calling `search_report` writes both report files. For a linked Goal Plus run,
the record must already be terminal; active and needs-user records are rejected
so a static report cannot capture an in-progress status. The same terminal
check is enforced by the low-level HTML writer used by offline tooling; calling
that renderer directly cannot bypass the lifecycle boundary or publish an
`Active` Goal as if it were a final report. Open `report.html`
directly for the coverage-aware statistical view, multi-Search breakdown,
candidate/session tables, and timelines. Planning-round counts remain available
in normalized data but are not rendered as a separate panel. The Goal Plus
state appears in the report summary; there is no separate lifecycle panel.
Every Search task uses an independent execution scale: worker spans come from
normalized host observability, while verifier and promotion markers come from
candidate evidence. Configured worker budgets are limits rather than observed
durations and do not determine bar width. An endpoint derived from a duration
or last session update is labeled as inferred. The embedded complete normalized
report data contains statistics and evidence metadata, not raw prompt,
reasoning, or tool payload bodies.

Failed process verifiers with `feedback_policy=visible_to_workers` return
bounded `stdout_tail` and `stderr_tail` metrics to the caller. Complete output
stays in the unique per-call log, and each `iterations[]` entry records its
`log_paths`, so a later verifier run does not overwrite earlier failure evidence.

There is no `agent_events/` or `observations/` directory. The session record
carries `host_handle`, `launch`, `directive`, and `counters.verifier_runs`.

## Quick Diagnostic Queries

### Run summary

```bash
RUN=$(ls -td .gp/runs/* | head -1)
python3 -c "
import json
d = json.load(open('$RUN/run.json'))
print(f\"state={d['state']} candidates={d['candidates_total']}/{d.get('candidates_evaluated',0)} evaluated best={d.get('best_candidate_id')}/{d.get('best_score')}\")
"
```

### Iteration history per candidate

```bash
for f in $RUN/candidates/*/candidate.json; do
  python3 -c "
import json
d = json.load(open('$f'))
iters = d.get('iterations', [])
print(f\"{d['candidate_id']}: {len(iters)} iterations\")
for it in iters:
    print(f\"  iter {it['iteration']}: score={it['score']} \"
          f\"failure={it.get('failure_class')} \"
          f\"touched_denied={it['touched_denied_files']} \"
          f\"changed_files={it['changed_files']}\")
"
done
```

### Workspace git history (the autoresearch loop)

```bash
find $RUN/workspace -name ".git" -execdir sh -c 'echo "=== $(pwd) ===" && git log --oneline' \;
```

### Runtime-owned results.tsv

```bash
find $RUN/workspace -name "results.tsv" -exec sh -c 'echo "=== $1 ===" && cat "$1"' _ {} \;
```

The runtime writes this file from durable `candidate.json.results_ledger` and
commits the header plus every appended row. Every verifier call that returns a
report contributes exactly one row. Calls that raise before a report exists
contribute none. Before each verifier, the runtime checks that the old content
is unchanged and Git-clean; deletion, rewriting, truncation, or a worker append
raises `ResultsLedgerMutation`. The ledger survives same-candidate redispatch,
is inherited by derived child workspaces, and is seeded from the selected/best
source candidate for a successor run. It is runtime metadata, so it is excluded
from edit-surface checks and promotion patches. Each worker verifier call stores
the worker's one-line description of the realized attempt as `hypothesis`;
workers must not edit this file directly.
On first resume of an older candidate, a legacy `.tmp/results.tsv` is migrated
to the workspace root; verifier-backed `iterations[]` missing from that legacy
file are appended as recovered rows so old evidence is not silently dropped.

Columns (tab-separated):

| col | name | meaning |
|---|---|---|
| 1 | `commit` | runtime-recorded full Git head for the verified artifact |
| 2 | `<metric_name>` | the frozen `spec.metric_name` literal (e.g. `combined_score`, `val_bpb`) — set by the main agent at freeze time |
| 3 | `status` | `pass` when the returned report passed, otherwise `fail` |
| 4 | `hypothesis` | short description of what this iteration tried |

Example:

```
commit	combined_score	status	hypothesis
a1b2c3d	0.682	pass	baseline (concentric rings)
b2c3d4e	0.949	pass	hex lattice [5,4,5,4,5,3] s=0.1875
c3d4e5f	0.651	fail	switch to rectangular grid (regressed)
```

The `commit` column names the code snapshot tested by the verifier. The
subsequent ledger commit is stored as `ledger_git_head` in `candidate.json`.
For `keep`, workspace `HEAD` normally points one ledger commit past the tested
snapshot. For `discard`/`failure`, history is attempt -> optional restoration
-> ledger: the attempt remains readable by hash while workspace code is the
candidate-local best artifact.

## Live Monitoring

Use the read-only monitor snapshot as the primary live view:

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"goal_plus_id":"gp_...","stale_after_seconds":600}' \
  --pretty
```

Use `goal_plus_id` when you need the complete hierarchy:

```text
goal-plus (complete user task)
  search_tasks[] (one run_id + frozen_spec_id per search task)
    planning_rounds_total (all persisted plan files)
    started_rounds_total  (plans whose status is started)
```

`linked_search` and the top-level detailed `run` remain compatibility/current
views. Do not use `linked_search != null` as the task count. The monitor also
warns about missing linked runs, frozen-spec mismatches, non-terminal
superseded tasks, explicit runs not linked to the requested goal, and a
completed goal whose current run is not promoted.

The top-level `strategy` object identifies the initial planner independently of
candidate and plan counts:

```json
{
  "strategy": {
    "name": "agent_guided",
    "worker_host": "pi-rpc",
    "latest_plan": {
      "plan_id": "plan_001",
      "selection_rule": "initial parallel lanes",
      "state": {}
    }
  }
}
```

`plans_count` is a compatibility alias for the selected run's
`planning_rounds_total`. Maintained runs have exactly one initial plan.

## Common Failure Modes

### Worker appears idle but has no iteration history

- **Look at**: `.gp/runs/<run_id>/candidates/<id>/candidate.json` `iterations`
  and the matching Codex/Pi native log selected by `agent_session_id`.
- **Cause**: The worker never called `search_run_verifier`.
- **Verification**: Confirm the native worker session exists and inspect its
  tool calls. The runtime only records verifier calls that actually happened.

### Agent ran evaluator via bash (MCP bypass)

- **Look at**: the matching Codex rollout or Pi event log.
- **Symptom**: evaluator commands appear, but `search_run_verifier` is absent.
- **Cause**: Agent didn't trust MCP path, or prompt was unclear about MCP being the official scorer
- **Verification**: Look at bash command contents — if `python evaluator.py` appears, agent bypassed MCP

### Candidate has 0 iterations but launch payload exists

- **Look at**: `agent_sessions/<id>.json` `counters.verifier_runs` vs `candidates/<id>/candidate.json` `iterations` length
- **Cause**: They should always match (every run_verifier call appends an iteration). If they don't, the subagent called `run_verifier` against a different candidate_id, or the main agent called it without `agent_session_id`.
- **Verification**: Check the iteration's `agent_session_id` field — it tells you which session (if any) the verifier call was attributed to.

### Subagent is still running but I want to stop it

- **Cause**: Worker interruption belongs to the host. There is no MCP abort tool.
- **Action**: Use `interrupt_agent` for Codex or
  `pi_search_pool_close(mode="interrupt")` for Pi. The Search runtime does not
  need a separate lifecycle update.

### Verifier fails with "EditSurfaceViolation"

- **Look at**: `candidates/<id>/candidate.json` latest iteration's `touched_denied_files` and `changed_outside_allowed`
- **Cause**: Agent edited files outside `edit_surface.allow` or modified `deny`-listed files
- **List offending files**: `changed_files` field in the iteration record

### Verifier fails with "VerifierWorkspaceSideEffect"

- **Look at**: the failing verifier result's
  `metrics.verifier_workspace_side_effects`, `cleanup_failures`,
  `infrastructure_failure`, and `candidate_action`
- **Cause**: the frozen verifier wrote compiler products, temporary outputs, or
  other files into the candidate workspace. Legacy runs may first expose this
  as an extra non-frozen path under `.goal-plus-verifiers/`.
- **Worker action**: stop and report immediately. Do not delete the generated
  path, edit the frozen verifier, reset around the error, or retry.
- **Parent action**: repair the source-owned verifier to use
  `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR` or Python `tempfile`, freeze a new spec,
  and create a new run. Never use one fixed `/tmp` path when candidates can
  verify concurrently.
- **Runtime behavior**: freeze preflight runs in a disposable copy and rejects
  side effects before candidate creation. Runtime fallback detection reports
  side effects in the same verifier call and attempts to restore the candidate
  workspace; check `cleanup_failures` before reusing any candidate state.

## MCP APIs for Inspection

These tools are safe to call anytime — they're read-only:

| Tool | What it shows |
|---|---|
| `search_status(run_id)` | Run state, candidate counts, best score |
| `search_list_history(run_id, top_n, sort_by)` | Top candidates by score |
| `search_list_iterations(run_id, candidate_id)` | Full iteration history for a candidate |
| `search_get_agent_context(agent_session_id)` | What a specific subagent sees (including its own iterations) |
| `search_get_global_evidence(agent_session_id)` | Settled worker Evidence and possibly delayed objective Views across the session's current run |
| `search_get_evidence_detail(agent_session_id, candidate_id, iteration)` | One on-demand supplemental evaluation authorized by the caller's current run and Evidence mode |

## Cross-Referencing Layers

When something goes wrong, cross-reference both layers:

1. **Host-native transcript/log** — what the agent *did* (tool calls, bash commands, messages) and what the host lifecycle did (start, step cap or turn cap, stop/interrupt)
2. **`.gp/` runtime state** — what the runtime *recorded* (scores, iterations, verifier logs)

Example: "Worker session finished but the candidate shows no score"
- Host logs show: the matching worker never called `search_run_verifier`
- Runtime shows: `candidate.json` with empty `iterations`

→ Diagnosis: the subagent did not self-score. Have the main agent run `search_run_verifier(run_id, candidate_id, "process")` (without `agent_session_id`) to record the final score against the workspace state the subagent left behind.

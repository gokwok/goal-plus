# Agent Host Adapters

Adapters translate runtime launch/continue requests into host-native worker
operations. The Search runtime stays unchanged; [Shared Plane](shared-plane.md)
defines the shared loop and ownership boundary.

## Common Contract

`src/goal_plus/agent_pool.py` defines `HostPoolContract` and terminal
`WorkerPoolEvent` values. Each host declares:

| Field | Contract question |
|---|---|
| `launch_mode` | does launch return immediately? |
| `wait_mode` | can the parent wake on any completion? |
| `continuation_mode` | same worker or fresh state redispatch? |
| `deadline_mode` | which host component enforces runtime? |
| `recovery_mode` | how is a live pool rediscovered? |
| `completion_stage` | when is the candidate safe for parent evaluation? |

The adapter also returns authoritative launch fields. The main agent projects
only fields supported by the current host tool schema; for Codex, that means the
current `spawn_agent` schema rather than assumed optional metadata.

## Maintained Capability Matrix

| Capability | Codex | Pi RPC | Pi ThinkThread |
|---|---|---|---|
| Launch | async `spawn_agent` | detached local supervisor + foreground Pi child | direct Child through Agent POSIX `thinkthread.spawn` |
| Wait mode | `wait_agent` any-event wake + `list_agents` | `pi_search_pool_wait_any` | bounded Child/Message wait-any through the same logical pool tools |
| Continuation | same worker via `followup_task` | same native session in a new process | same retained Child Session via Message wake |
| Deadline | per-dispatch parent watchdog | cumulative pool lease + Pi process watchdog | cumulative pool lease + INT/TERM Child watchdog |
| Recovery | native agent registry + `.gp` | persisted `.gp/host-pools/pi/` + `.gp` | durable pool/Message/fs requests + direct-Child observation |
| Goal gate | `UserPromptSubmit`, `SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop` | extension input/tool/turn events | Root extension gate; Child exposes only Message-backed worker tools |
| Strategy coverage | initial parallel loops | initial parallel loops | initial parallel loops |
| Model discovery | Codex app-server `model/list` | Pi RPC `get_available_models` | Profile-delegated Agent POSIX catalog |
| Normalized observability | native session JSONL + bound metadata | `pi_metrics` + bound metadata | Child state/model/fs handle + durable pool/storage facts |

All adapters implement the read-only `collect_observability` contract exposed
as `search_get_agent_observability`. This is provenance and diagnostics only;
it does not add worker lifecycle state to Search records or turn the runtime
into a supervisor.

Adapters also expose read-only model discovery through
`goal_plus_list_models`. Discovery reports what the host currently advertises;
it does not claim that every catalog entry can be forwarded through every
version of a host launch tool. The user still chooses `strategy.models`, and
the authoritative launch payload is projected onto the actual host tool schema
at dispatch time. If no models are requested, Goal Plus skips discovery and
preserves native default-model inheritance.

Codex rollout JSONL exposes per-response input, cached-input, cache-write,
output, and reasoning-token usage but does not expose a billed USD amount.
Goal Plus therefore computes `usage.cost_usd` from each
`last_token_usage` event with the versioned Pi-compatible OpenAI Codex model
catalog in `src/goal_plus/codex_pricing.py`. Long-context tiers and
`flex`/`priority` multipliers follow Pi semantics. The result is an
API-equivalent model-rate estimate, not an observed ChatGPT subscription
charge; `usage.cost_estimate` retains the catalog, coverage, and billing note.
Unknown models keep `cost_usd` unavailable instead of applying a guessed rate.

The accepted initial planners are `agent_guided`/`agent`/`default` and
`random`/`random_mode`.

## Parallel Loops

Codex and Pi both satisfy asynchronous wait-any semantics:

- **Codex** launches the initial candidate set once, waits for any mailbox
  update, then uses `list_agents` to discover all newly terminal workers. It
  reuses exact worker Evidence and runs a parent verifier only when matching
  Evidence is absent. It then continues that same worker through
  `search_continue_agent_session` plus `followup_task` unless a global stop
  condition is true.
- **Pi** persists pool/job state, automatically resumes an early native turn
  inside the same job when a minimum lease is active, and never auto-refills.
  It returns `candidate_ready` only after the minimum lease and durable Evidence
  are satisfied; an exhausted unsatisfied lease returns `timed_out`. After each
  candidate-ready event main calls `continue` for that same candidate unless a
  global stop condition is true. Pi reloads the same native session in a new
  process.
- **Pi ThinkThread** persists the same logical pool contract but launches
  Message-only direct Children from one exact baseline. It wakes the same Child
  and branch, snapshots/verifies exact turn-boundary state, and resets that
  branch to the prior best snapshot on discard/failure.

New Pi/Codex specs set `orchestration_mode="parallel_loops"`; one initial round
creates the durable candidate loops. Neither adapter turns that round into a
completion barrier. Low score or no improvement never causes replacement.

## Worker Budgets

| Host | Required control | Enforcement |
|---|---|---|
| Codex | `worker_budget.max_runtime_seconds` | initial wait, one closeout message, final wait, interrupt |
| Pi RPC | `worker_budget.max_runtime_seconds` | closeout steer plus hard process watchdog |
| Pi ThinkThread | `worker_budget.max_runtime_seconds` | retained-Child lease plus INT/TERM watchdog |

`max_turns` is only a prompt hint for Codex and Pi. `max_parallel` uniquely
sets the initial candidate/live-worker count because later work continues the
same candidates.

Codex supports a lower-bound single-worker AutoResearch lease through
`worker_budget.min_runtime_seconds` and `min_verifier_runs`. Its
`SubagentStop` hook continues the same child turn until the lower bound is
satisfied, while `max_runtime_seconds` remains the independent parent-watchdog
upper bound. The adapter requires the lease to release before the parent soft
closeout, preventing the two controls from racing. Pi exposes the same fields,
and its worker-role `agent_end` handler continues the same live Pi process and
native session until closeout. The host-local pool supervisor remains the crash
recovery boundary and enforces the lease cumulatively across cross-process
resumes. Each resume receives only the remaining max runtime; infrastructure
failure and pool/outer closeout terminate the lease.

`strategy.worker_launch` carries optional host launch preferences. Codex maps
`model`, `reasoning_effort`, and `service_tier` when exposed. Legacy `pi-rpc`
maps model and thinking level through trusted process configuration.
`pi-thinkthread` maps only an exact provider/model binding through typed Child
derivation; reasoning effort and service tier are rejected because they are
not ThinkThread Child derivation fields. These values do not belong to Search
state.

## Resume And Handoff

State-level redispatch is the portable recovery path:

1. call `search_redispatch_candidate` for an existing candidate;
2. launch the fresh `agent_session_id` in the same workspace;
3. the worker reloads `search_get_agent_context`;
4. candidate-local artifact state, verifier iterations, `research_summary`,
   and the narrow `search_get_global_evidence` view replace dependence on a
   previous transcript. Git hosts retain Git state; `pi-thinkthread` retains
   the same private fs branch and exact settled snapshot.

Same-worker continuation is native on Codex. Pi provides native session
continuation across process boundaries: each dispatch has a new PID,
but retains the same native session, runtime `agent_session_id`, candidate, and
workspace. State-level redispatch remains the portable fallback for hosts or
legacy records without a resumable native session.

Every worker handoff should state the most important work, verifier-backed
feature entries, blockers, next steps, and at most five scoped conditional
pitfalls. Candidate-local pitfalls stay local; feature-family pitfalls transfer
only when mechanism and conditions match. Verifier concerns remain advisory
until the main agent confirms them.

## Confirmed Verifier Invalidation

The runtime fence is host-neutral; quiescence is adapter-specific:

| Step | Codex | Pi RPC | Pi ThinkThread |
|---|---|---|---|
| Fence | `search_invalidate_run` | `search_invalidate_run` | `search_invalidate_run` |
| Stop live work | `interrupt_agent` for every live candidate | `pi_search_pool_close(mode="interrupt")` | same logical close; INT/TERM, branch remove, Child destroy |
| Prove quiescence | `list_agents`/`wait_agent` until all terminal | snapshot/wait until `active_count=0` | pool cleanup observation + no direct Child/branch |
| Rebuild | repair/freeze only after quiescence | repair/freeze only after quiescence | same; take a new Root baseline |
| Successor | `search_create(..., source_run_id=old)` | same | same |

Adapters must not attempt to refill an invalidated run. The runtime also rejects
Pi pool open/submit and rejects a verifier result that finishes after the fence.
The old run remains readable for diagnosis and research inheritance, but its
scores cannot be promoted or reused by the successor.

## Verification Evidence

| Path | Repository evidence |
|---|---|
| Codex parallel-loop cycle | `codex_parallel_loop_cycle`: two initial candidates, one plan, same native worker continuation, best update, final selection/report |
| Pi managed pool | `pi_rpc_managed_pool_wait_any`: two detached real Pi workers, pool rediscovery, candidate-ready events, drain |
| Pi parallel-loop cycle | `pi_rpc_parallel_loop_cycle`: one initial plan, same-candidate redispatch with a new session, best update, final selection/report |

Fast tests prove schemas and adapter mappings. Only the opt-in real-host tests
prove native launch, waiting, continuation, hooks, and provider behavior.

## Adapter Responsibilities

An adapter may:

- build launch and continuation payloads;
- list the host's currently advertised models without starting a Search run;
- validate host-specific budget fields;
- declare pool capabilities;
- preserve native handles and bounded host metadata.

It must not create candidate workspaces, execute/rank verifiers, plan the next
hypothesis, generate reports, or export promotion patches.

Host integrations are limited to Codex and Pi. Changes to either adapter must
update its local assets and docs, cover launch/bind/budget/continuation in unit
tests, and retain a real multi-round smoke path.

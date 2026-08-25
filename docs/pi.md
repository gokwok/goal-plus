# Pi

Pi support is project-local: one user-facing `goal-plus` skill under
`.pi/skills/goal-plus/`, one extension, and Python worker/facade commands. It
does not patch Pi core and does not expose a separate user-facing `search` skill.

## Setup

```bash
python -m pip install -e ".[dev]"
pi -p "/goal-plus inspect this repository"
```

### ThinkThread Profile

`pi-thinkthread` is an additional Pi Search host. It does not replace the
legacy `pi-rpc` path above and does not change Codex or Git behavior. Install
the reusable Profile and its self-contained assets. The installer obtains the
official Agent POSIX SDK from the public `capsule_public` `v0.1.0` release:

```bash
./scripts/install_pi_goal_plus_thinkthread.sh \
  --model openai-codex/gpt-5.6-terra

cd /path/to/target-workspace
tt pi-goal-plus
```

The default source is
[`https://gitcode.com/aideveloper/capsule_public.git`](https://gitcode.com/aideveloper/capsule_public.git).
Use `--agent-posix-ref` to select another release tag, `--thinkthread-source`
for a local SDK checkout during development, or `--agent-posix-package` for an
offline prebuilt `.tgz`. The installer always validates the Control v2 contract
fingerprint before replacing the existing installation.

If the target keeps its Python environment inside the workspace, activate that
`.venv` before launch and use plain `tt pi-goal-plus`; the direct Root and COW
Children can read it through `fs="."`. If the virtual environment is outside
the target workspace, inherit its `PATH`/`VIRTUAL_ENV` and grant it read access
explicitly:

```bash
source /path/to/external/.venv/bin/activate
tt run --profile pi-goal-plus --read "$VIRTUAL_ENV"
```

Goal Plus forwards the inherited interpreter selection into exact `fs.run`
verifiers. It does not copy or install task dependencies into Candidate
branches.

The Profile uses the launch directory as `fs="."`, gives Root a direct
attachment and every Candidate Child a private copy-on-write branch, and grants
Children only `thinkthread.message`. It reuses `${HOME}/.pi/agent` for normal
Pi models/auth/settings but loads Goal Plus's Extension and skill explicitly
from `${HOME}/.local/share/goal-plus`; ordinary Pi sessions are not modified and
do not auto-load Goal Plus.

Every Root Session derives an independent runtime state directory at
`${PI_CODING_AGENT_SESSION_DIR}/goal-plus`. Child extensions detect their
worker role, expose only Message-backed worker tools, and never open that Root
runtime locally.

Use this host in a SearchSpec by setting
`strategy.worker_host="pi-thinkthread"` and omitting `workspace` entirely.
Typed `provider/model` lane assignment comes from the Profile's delegated
catalog. Reasoning effort and service tier remain unsupported. The managed pool
uses retained ThinkThread Child Sessions, exact `FsSnapshot` verifier Evidence,
strict `fs.replace` publication, durable fs request recovery, and snapshot
cleanup after pool close and final report generation. `shared_dir` remains an
opt-in tool-sharing layer; it never exposes a writable sibling workspace.

Caller-owned durable snapshot-capture recovery and native-state diagnosis are
documented in [Debugging](debugging-runtime.md#pi-thinkthread); the shared host
contract is summarized in [Agent host adapters](agent-host-adapters.md).

The extension provides pre-model `/goal-plus` creation in interactive,
RPC, print, and JSON modes. It persists the active id when the Pi session is
persistent, injects hidden context, gates selected writes/Search calls, and
runs a native turn-level stop gate. This is no host process Stop hook.

For Pi Search runs, asynchronous Evidence annotation also stays on Pi. The
run-scoped drainer uses an ephemeral `pi --mode json --no-session --no-tools`
process, inherits the Pi worker model/provider unless explicitly overridden,
and reads provider configuration from `PI_CODING_AGENT_DIR`. A qualified
annotator model (`provider/model`) or `evidence_annotator.pi_provider` can select
a provider independent of the Search worker. Verifier settlement never waits
for this process. The same turn also produces shared-dir Tool Views. The
host-neutral lifecycle is documented in
[`shared-plane.md`](shared-plane.md#工具复制与采用结算).

`/goal-plus edit`, `/goal-plus resume`, and `/goal-plus-with-final-check` share
the same goal revision semantics as Codex. Required checks run through a
separate read-only Pi RPC reviewer.

Use `/goal-plus mode=autonomous <goal>` for substantial renewable candidate
exploration (the default), or `/goal-plus mode=probe <goal>` for short
feasibility/potential/blocker probes. The choice is normalized into the final
line of `raw_goal`; it is not a Pi pool or Search runtime state.

At the end of a main turn, every still-active record is continued with its full
raw goal and elapsed-time context. Pi stops only after the agent records a
terminal status. Worker watchdog expiry remains a dispatch event, not goal
completion.

At terminal state, Pi writes a visible `Goal Plus stats` custom entry with
elapsed time, messages, tool calls, token use, and estimated cost. It is not an
LLM message and does not trigger another assistant turn.

## How Pi Differs From Codex

The main agent uses extension events rather than project hook files. Candidate
workers are Pi RPC processes supervised by a durable host-local pool. Each
detached wrapper owns one foreground `pi --mode rpc` child launched by
`goal-plus-pi-worker`; native session JSONL lives under
`.gp/host-sessions/pi/`.

Pool state lives under `.gp/host-pools/pi/`; Search records remain host-neutral.
`pi_search_pool_continue` starts a new process that reloads the same native Pi
session in the same candidate workspace and preserves `agent_session_id`.
Metrics use `get_entries(since=<last_entry_id>)`, so each dispatch transfers
only new entries while cumulative usage remains available in the bound handle.
This does not keep one OS process resident across completions.
The resume launch explicitly resets dispatch-scoped deadline semantics: a
closeout or time advisory persisted by an earlier process is historical, and
only warnings delivered after the latest launch apply to the new budget.
Each resume intentionally uses a new process; native session state and the
candidate workspace provide continuity without requiring a persistent worker
PID.

With `pi-thinkthread`, by contrast, the pool owns direct ThinkThread Children:
continuation sends a wake Message to the same native Child Session and private
fs branch. Candidate execution never starts `goal-plus-pi-worker`, a Git
worktree, or `pi --mode rpc`.

## Worker Spec

Legacy Pi RPC uses `worker_host="pi-rpc"` and a wall-clock budget:

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_host": "pi-rpc",
    "worker_budget": {
      "min_runtime_seconds": 500,
      "min_verifier_runs": 1,
      "max_runtime_seconds": 600,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

`max_runtime_seconds` is required. Optional `min_runtime_seconds` and
`min_verifier_runs` are enforced cumulatively by the pool wrapper: an early Pi
turn restarts the same native session in the same slot/worktree with only the
remaining upper budget. Infrastructure failure, pool close, or outer closeout
stops this automatic continuation. Before each remaining hard limit, the runner
sends one closeout steer. `max_turns` is only a prompt hint.

ThinkThread uses the same budget shape but omits the workspace selector:

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_host": "pi-thinkthread",
    "worker_budget": {
      "max_runtime_seconds": 600,
      "on_exceed": "interrupt"
    }
  }
}
```

Do not add `"workspace": {"backend": ...}` to this spec. Workspace authority
comes from the ThinkThread Profile and the pool's exact baseline snapshot.

### Explicit role models

The native Pi command can select the main, Evidence Annotation, and Search
worker models with role-named parameters:

```text
/goal-plus main=A annotator=B workers=C,D max_parallel=4 optimize ...
```

`main=` switches the current Pi model before the first model turn.
`annotator=` freezes `strategy.evidence_annotator.model`. `workers=` resolves
one or more models into `strategy.models`; uncounted entries round-robin and
counted entries retain the allocation rules below. Omitted roles keep their
existing host default or inheritance behavior rather than inheriting from an
unrelated explicit role.

Use qualified `provider/model` references when an id exists under more than one
provider. Existing `models=` remains a compatibility alias for `workers=` so
older commands retain exactly the same Candidate allocation semantics. Do not
specify both aliases in one request.

### Multi-model selection

Use `workers` (or the compatibility alias `models`) directly in the
natural-language command. Because Pi can expose
the same model id from multiple providers, canonical `provider/model` values
are the clearest form:

```text
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol max_parallel=4 optimize ...
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol A1B3 max_parallel=4 optimize ...
```

For legacy Pi RPC, Goal Plus obtains the catalog from Pi RPC
`get_available_models` through `goal_plus_list_models(host="pi-rpc")`. For
ThinkThread it reads the Profile-delegated catalog through the official Agent
POSIX SDK with `goal_plus_list_models(host="pi-thinkthread")`; selections must
use an exact `provider/model`. A short Pi RPC id is accepted only when it
matches one entry uniquely. Uncounted entries round-robin; `A1B3` (or
`A*1,B*3`) becomes explicit counts that must sum to `max_parallel`. The
runtime-generated `selected_models` are immutable
per candidate/native session. Omitting both `workers` and `models` keeps Pi's
current default.

## Parallel Loops

Normal Pi Search follows the [Shared Plane](shared-plane.md) flow:

1. set `orchestration_mode="parallel_loops"`, then plan and materialize the
   initial candidates exactly once;
2. `pi_search_pool_open(..., max_parallel=<frozen limit>)`;
3. `pi_search_pool_wait_any` for the first terminal event;
4. for `candidate_ready`, observe any verifier-backed best update and call
   `pi_search_pool_continue` for that exact candidate unless a global stop
   condition is true; treat `timed_out`, `interrupted`, and `failed` as
   incomplete rather than continuing them as ready candidates;
5. recover interrupted main turns with `pi_search_pool_snapshot(run_id=...)`;
6. `pi_search_pool_close`, then select and promote;
7. record the Search result, finish the raw-goal audit, set a terminal Goal Plus
   status, then generate the final report exactly once per recorded run.

The supervisor enforces `max_parallel` and never auto-refills. Main never calls
submit after initial pool creation and never replaces a candidate because of
low score or lack of improvement. A `candidate_ready` event is published only
after the driver has bound the handle, released any minimum lease, and confirmed
durable Evidence for the current artifact. An exhausted unsatisfied lease emits
`timed_out` instead.

For legacy `pi-rpc`, a minimum lease may queue another turn in the current
process and later reload the same native session in a new process. For
`pi-thinkthread`, a completed turn remains attached to the same Child/private
branch and continuation uses `message.send(wake=true)`; a parent turn-boundary
snapshot verifier catches edits made after the worker's last verifier. A
discard/failure persists restore intent, waits for absent execution, resets the
same branch to its prior best exact snapshot, and wakes that same Session.

There is no public synchronous candidate/batch runner. Pool open owns the
initial fixed lane set; pool continue owns later dispatches for those same
lanes.

## Worker Boundary

Worker-role extension tools are limited to `search_get_agent_context`,
`search_get_global_evidence`, `search_get_evidence_detail`,
`search_stage_shared_tool`, `search_copy_shared_tool`,
`search_run_verifier`, and `search_list_iterations`. The shared-tool methods
are useful only when the frozen spec enables `shared_dir`. Each iteration reads the
Global Evidence view, independently chooses a direction, edits only inside the
returned workspace, runs the verifier with a one-line hypothesis describing the
realized attempt, and updates a bounded `.tmp/handoff.json`. A `null` View means
the annotator has not published yet and never requires waiting.

The persisted native session is the normal continuation surface. Legacy Pi RPC
recovers through candidate Git state; ThinkThread recovers through exact
`FsSnapshot` Evidence, durable Message/pool records, and the private branch.

Every redispatched worker owns the next hypothesis, pivot, and rebase within
the same candidate workspace. Main sends a neutral continuation directive and
does not act as a technical conductor.

## Tool Facade

`goal-plus-pi-tool` exposes the same GoalPlusTools/SearchTools facade plus the
Pi-local pool tools:

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

`goal_plus_monitor_snapshot` is read-only and also exists on MCP. It never
starts, waits for, or stops a worker. The complete concise tool index is in
[API](api.md).

Use `search_get_agent_observability(agent_session_id)` for the same normalized
per-worker schema used by Codex. Pi maps the existing `pi_metrics` model,
thinking level, duration, usage/cost, context, and log/session paths into that
schema; the legacy bound fields remain readable.

## State And Logs

For legacy Pi RPC, Search state, candidate commits, and workspaces under `.gp/`
are authoritative. Worker logs default to a metadata-only event log:

```text
.gp/host-logs/pi-rpc-<agent_session_id>.jsonl
```

It stores event/tool status, bounded errors, timing, and usage without prompts
or reasoning. Set `GOAL_PLUS_PI_RAW_LOG=1` only for focused debugging; raw
streams can become very large.

Bound handles include `metadata.pi_metrics` (including resolved model and
thinking level), timeout/failure evidence, and a
bounded `metadata.progress_handoff`. A timeout is successful deadline
enforcement; runner failure is recorded separately with synthetic failure
metadata so monitoring never mistakes it for a live session.

For `pi-thinkthread`, `GOAL_PLUS_ROOT` is the Root Session directory derived by
the Profile. Pool/job records bind Child, branch, session, dispatch nonce,
Message cursor, settled request replay, restore/wake intent, and cleanup.
`goal_plus_monitor_snapshot` projects exact snapshot artifacts, publication,
durable fs-request state counts, and cleanup/storage observations. Native Child
state remains observable through `tt ps --forest` and Agent POSIX
`thinkthread.get`; it is not copied into Search lifecycle state.

## Supported Strategies

Pi currently supports the portable builtin strategies only:

- `agent_guided`, `agent`, `default`
- `random`, `random_mode`

## Verification

```bash
pytest -m pi -q
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
```

The real-host test launches two detached Pi RPC workers, rediscovers the pool
by `run_id`, observes wait-any completion, and drains cleanly. See
[Debugging](debugging-runtime.md) for cross-host diagnosis.

The ThinkThread focused/E2E path must run inside a real Root Session launched
with `tt pi-goal-plus`; host-free tests use a stateful typed-client fake and do
not prove platform snapshot creation or publication behavior.

The maintained Orb smoke covers replayable Root/private-branch snapshots,
Message-only typed-model Child launch, exact-snapshot verifier execution,
retained wake continuation, `shared_dir` snapshot patch/reset, strict
publication conflict handling, request close, and Child/branch/snapshot
cleanup. Interactive and initial-prompt launch both use the same Root readiness
contract before Goal Plus begins workspace operations.

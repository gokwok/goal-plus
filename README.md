# Goal Plus (GP)

English | [简体中文](README_zh.md)

Goal Plus is a host-neutral runtime for long-running agent work. `/goal-plus`
handles ordinary goals directly and upgrades measurable optimization tasks to
Search Mode: freeze the evaluation contract, explore isolated candidates, and
promote the best verifier-backed result.

Pi and Codex are the supported host paths.

## Quick Start

Install from Git or an existing checkout:

```bash
python -m pip install --user "git+https://github.com/ck0123/goal-plus.git"
# or
python -m pip install -e ".[dev]"
# add the optional self-contained Plotly trajectory to HTML reports
python -m pip install -e ".[dev,report]"
```

Every host launches the same stdio MCP server:

```text
goal-plus --root .gp
```

For the isolated Pi + ThinkThread host, install the reusable Profile and its
self-contained Goal Plus/official Agent POSIX SDK assets, then launch it from
the target workspace:

```bash
./scripts/install_pi_goal_plus_thinkthread.sh
cd /path/to/target-workspace
tt pi-goal-plus
```

The installer fetches the official `@thinkthread/agent-posix` SDK from the
[`capsule_public`](https://gitcode.com/aideveloper/capsule_public.git) `v0.1.0`
release by default. It does not require a ThinkThread source checkout.

This Profile reuses normal Pi models/auth/settings without installing Goal Plus
into ordinary Pi configuration. See [Pi](docs/pi.md#thinkthread-profile).

Then start a goal in the host:

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus Optimize p95 latency for two hours without changing correctness.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

Codex and Pi also expose:

```text
/goal-plus edit <full revised goal>
/goal-plus resume
/goal-plus-with-final-check <goal>
```

One request starts an autonomous run. The agent decides whether Goal Mode is
enough or a frozen verifier makes parallel Search useful; entering Search does
not require an extra approval step. `mode=autonomous` (the default) gives
every initial candidate substantial, renewable same-workspace exploration leases;
`mode=probe` asks for short feasibility probes first. This exploration mode is
stored as guidance in the final line of `raw_goal`, not as a scheduler state.

## Hosts

| Host | Project assets | Entry | Search worker path |
|---|---|---|---|
| Pi | `.pi/` | `/goal-plus` or `pi -p "/goal-plus ..."` | durable Pi RPC pool; see [Pi](docs/pi.md) |
| Pi + ThinkThread | `.pi/`, `.thinkthread/` | `tt pi-goal-plus`, then `/goal-plus ...` | retained Message-only Child Sessions on private fs branches; see [Pi setup and behavior](docs/pi.md#thinkthread-profile) |
| Codex | `.codex/` | `goal-plus` skill or `/goal-plus` prompt | fixed parallel loops with native same-worker continuation; Codex 0.144.1+ hooks cover `UserPromptSubmit`, `PreToolUse`, and `SubagentStop`; see [Codex](docs/codex.md) |

For Codex, copy `.codex/config.example.toml` to the ignored local
`.codex/config.toml`. Host differences and strategy coverage are summarized in
[Agent Host Adapters](docs/agent-host-adapters.md).

## Mental Model

- A **Goal Plus record** is the complete user task.
- A **search task** is one `run_id` over one frozen spec. A goal may link more
  than one search task.
- An initial **SearchPlan** allocates the fixed candidate lanes. It is not a
  per-iteration plan protocol.
- A **candidate** is a long-lived autonomous loop in one isolated workspace
  with verifier history.
- A **worker session** is a host context/provenance handle. Worker lifecycle
  belongs to the host, not the Search runtime.
- The **shared plane** contains the frozen contract, exact verifier artifacts
  (`git_commit` or `fs_snapshot`),
  Global Evidence, asynchronous objective Views, and selection state. It does
  not expose peer reasoning or peer workspaces.
- A **verifier concern** is worker advice. Only the main agent can confirm it;
  confirmation fences the run before all host workers are stopped and a
  successor spec/run is created.

New Pi/Codex Search uses fixed parallel loops: create the initial candidates
once, validate every completion, update the verifier-backed global best, and
resume that same candidate while no global stop condition is true. Main does
not choose later technical directions or replace low-scoring candidates.
Slower workers do not block completed work from being evaluated. See
[Shared Plane](docs/shared-plane.md).

Keep one run for one valid evaluation/edit contract. If a successor is
unavoidable, `source_run_id` preserves bounded frontier/features/scoped
pitfalls as research context, never as reusable scores.

Runtime state lives under `.gp/`. `search_tasks` is append-only; `linked_search`
is only the compatibility view of the current task.

When `promotion_verifiers` are configured, promotion is an independent check,
not a cached pass-through. The runtime checks out the selected verifier-backed
revision, reruns each promotion gate with
`GOAL_PLUS_VERIFIER_PHASE=promotion`, and binds the evidence to the selected
exact artifact. Git-backed hosts then emit a Git-applyable patch;
`pi-thinkthread` performs strict baseline-to-selected snapshot publication. A
failed promotion stays retryable in `ready_to_promote` and emits no output.

## Documentation

| Need | Read |
|---|---|
| Architecture, shared Evidence, rollback, and end-to-end flow | [Shared Plane](docs/shared-plane.md) |
| Current MCP and Pi-local tools | [API](docs/api.md) |
| Host capability comparison | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Runtime and host logs | [Debugging](docs/debugging-runtime.md) |
| Specs and runnable examples | [Examples](examples/README.md) |
| Tests and real-host evidence | [Tests](tests/README.md) |

Benchmark-specific tasks, evaluators, campaigns, and comparison evidence live
in [bench-goal-plus](https://github.com/ck0123/bench-goal-plus), not in this
runtime repository.

## Development

```bash
python -m pytest -q
git diff --check
```

The maintained strategy set for Pi and Codex is `agent_guided`
(`agent`/`default`) and `random` (`random_mode`).

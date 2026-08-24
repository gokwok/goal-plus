# Tests

Default tests are fast and host-free. System tests (ST) are opt-in and provide
the only evidence that a real host can complete the user-visible workflow.

## Test Layers

| Layer | Command | Proves |
|---|---|---|
| Default fast gate | `pytest -q` | models, runtimes, workspaces, verifiers, APIs, Codex/Pi assets |
| Integration slice | `pytest -m integration -q` | one-plan parallel search, freeze+plan+batch+verify end-to-end |
| Example slice | `pytest -m example -q` | `examples/*` fixtures drive real generated assets |
| Codex fast slice | `pytest -m codex -q` | Codex adapter, hooks, assets, pool contract |
| Pi fast slice | `pytest -m pi -q` | Pi extension, driver, supervisor, assets |
| Pi ThinkThread focused | `pytest tests/test_thinkthread_*.py -q` | Profile/SDK contract, Message pool, exact fs artifacts, recovery and cleanup with stateful fakes |
| Runtime-focused | `pytest tests/test_runtime_unit.py` | Search state machine without a host |
| Real-host ST | `pytest -m "(st and (st_codex or st_pi_rpc)) or st_pi" -v -s` | maintained native launch, hooks/events, worker lifecycle, final evidence |

Default tests must never launch Codex or Pi. A host behavior claim requires
the matching ST; if it cannot run, report that gap.

The host-free ThinkThread tests also never open an Agent Control socket. A real
claim requires launching `tt pi-goal-plus` inside Orb and checking native
Children/branches, exact snapshot verifier Evidence, strict publication, and
post-close storage cleanup. The maintained contract and diagnosis flow are in
[`docs/pi.md`](../docs/pi.md#thinkthread-profile) and
[`docs/debugging-runtime.md`](../docs/debugging-runtime.md#pi-thinkthread).
With the `dev` extra installed, `pytest -n 2 --dist=load -q` runs the default
gate with two workers. Keep real-host ST serial so host processes, model calls,
and machine resources do not interfere with one another.

`integration` and `example` tests are skipped by default via
`tests/conftest.py`. Add a marker name to `-m` only when intentionally auditing
that slice.

## System-Test Markers

| Marker | Runner | Default model |
|---|---|---|
| `st_codex` | `codex exec` | `gpt-5.6-terra` or `ST_CODEX_MODEL` |
| `st_pi_rpc` | `goal-plus-pi-worker` + Pi RPC | host default or `ST_PI_MODEL` |
| `st_pi` | native Pi `/goal-plus` print/TUI | host default or `ST_PI_MODEL` |

Every `tests/st/` case has `st` plus exactly one host marker. Native Pi command
tests live in `tests/st_pi/`. `-s` is required so failure log paths remain
visible.

## Common Real-Host Checks

```bash
# Codex state redispatch and fixed parallel-loop continuation
pytest -m "st and st_codex" -k codex_redispatch -v -s -rs
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest -m "st and st_codex" -k codex_parallel_loop_cycle -v -s -rs

# Codex one-worker AutoResearch lease: 5 minute lower bound, 7 minute watchdog
ST_CODEX_TIMEOUT=1200 \
  pytest -m "st and st_codex" -k codex_autoresearch_lease -v -s -rs

# Codex revises one goal after result 1, refreezes, and completes result 2
pytest -m "st and st_codex" -k goal_plus_spec_revision -v -s -rs

# Pi worker and durable wait-any pool
pytest -m "st and st_pi_rpc" -k pi_rpc_k_module -v -s -rs
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
ST_PI_MODEL=openai-codex/gpt-5.6-luna ST_PI_CYCLE_WORKER_SECONDS=90 \
  pytest -m "st and st_pi_rpc" -k parallel_loop_cycle -v -s -rs

# Native Pi Goal Plus lifecycle
pytest -m st_pi -v -s -rs

# Native Pi ends a turn early, then the Stop gate triggers a follow-up turn
pytest -m st_pi -k stop_gate_intercepts -v -s -rs

# Required independent final checks
pytest -m "st and st_codex" -k goal_plus_required_final_checker -v -s -rs
pytest -m st_pi -k with_final_check_runs_pi_reviewer -v -s -rs

# All maintained hosts
pytest -m "(st and (st_codex or st_pi_rpc)) or st_pi" -v -s -rs
```

Useful overrides:

```bash
ST_CODEX_MODEL=gpt-5.6-luna pytest -m "st and st_codex" -v -s
ST_PI_MODEL=openai-codex/gpt-5.6-luna ST_PI_THINKING=high \
  pytest -m "st and st_pi_rpc" -v -s
ST_CODEX_TIMEOUT=3600 pytest -m "st and st_codex" -v -s
```

## Contracts

Host changes require proportional evidence:

- worker launch, wait, continuation, or budget changes: fast adapter tests plus
  the matching real-host ST;
- hook/extension or `/goal-plus` lifecycle changes: host asset tests plus a
  native lifecycle ST;
- planner or round changes: a multi-candidate/multi-round scenario;
- verifier/workspace changes: runtime integration tests that restore and
  re-verify exact candidate commits.

ST prompts end with a fenced `st_report` JSON block. The shared required fields
are `scenario`, `run_id`, candidate summaries, `selected_candidate_id`,
`best_score`, and `report_path`; scenario additions are documented in
`tests/st/prompts/_schema.md`.

## Layout

```text
tests/
  test_*.py                 # default unit/integration/asset tests
  test_ascendc_goal_driven_example.py # AscendC generated-contract and knowledge tests
  fixtures/                 # shared source workspaces
  st/
    conftest.py             # host preflight and prompt loading
    hosts.py                # marker-to-runner mapping
    helpers/                # Codex runner and report parser
    prompts/                # scenario contracts
    test_st_*.py            # real host-worker scenarios
  st_pi/
    conftest.py             # native Pi preflight
    test_goal_plus_pi.py    # real Pi command/TUI lifecycle
```

ST preflight checks the selected host binary, `goal-plus`, MCP configuration,
fixtures, and the nested-ST guard. Host subprocesses set
`GOAL_PLUS_ST_ACTIVE=<scenario>` so an agent cannot recursively start ST from
inside a running scenario.

## Failure Logs

System tests write complete host output under:

```text
.tmp/st-logs/<test-node>/<scenario>.log
```

Search evidence remains under `.gp/runs/`; host-specific logs are under
`.gp/host-logs/`. Use [runtime debugging](../docs/debugging-runtime.md) to
correlate `run_id`, `candidate_id`, and `agent_session_id`.

## Pytest Configuration

`pytest.ini` defines markers. `tests/conftest.py` skips `integration`,
`example`, `st`, and `st_pi` tests unless their marker name appears in `-m`.
Keep marker registration, this page, and scenario files in sync whenever an
ST is added, renamed, or removed.

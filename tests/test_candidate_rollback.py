from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for


def _candidate(
    tmp_path: Path,
    *,
    direction: str = "maximize",
    backend: str = "copy",
) -> tuple[FileSearchRuntime, str, str, Path]:
    project = make_project(tmp_path)
    spec_data = spec_for(project, direction=direction).model_dump(mode="json")
    spec_data["workspace"] = {"backend": backend}
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    return runtime, run_id, task.candidate_id, task.workspace


def _git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=workspace, text=True).strip()


@pytest.mark.parametrize(
    ("direction", "scores"),
    [
        ("maximize", {"seed": 1.0, "improved": 3.0, "worse": 2.0, "equal": 3.0}),
        ("minimize", {"seed": 3.0, "improved": 1.0, "worse": 2.0, "equal": 1.0}),
    ],
)
def test_process_verifier_settles_workspace_to_candidate_best(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    scores: dict[str, float],
) -> None:
    runtime, run_id, candidate_id, workspace = _candidate(
        tmp_path,
        direction=direction,
        backend="git_worktree",
    )
    program = workspace / "initial_program.py"

    def fake_verify(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        value = program.read_text(encoding="utf-8").split("'", 2)[1]
        if value == "broken":
            return subprocess.CompletedProcess(command, 1, "", "invalid candidate")
        return subprocess.CompletedProcess(
            command,
            0,
            f'{{"combined_score": {scores[value]}}}\n',
            "",
        )

    monkeypatch.setattr(runtime, "_execute_verifier_process", fake_verify)

    reports = []
    expected_best = {
        "seed": "seed",
        "improved": "improved",
        "worse": "improved",
        "equal": "equal",
        "broken": "equal",
    }
    for value in ("seed", "improved", "worse", "equal", "broken"):
        program.write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        reports.append(
            runtime.run_verifier(
                run_id,
                candidate_id,
                hypothesis=f"try {value}",
            )
        )
        assert program.read_text(encoding="utf-8") == (
            f"VALUE = {expected_best[value]!r}\n"
        )

    assert [report.disposition for report in reports] == [
        "keep",
        "keep",
        "discard",
        "retain",
        "failure",
    ]
    assert [report.best_iteration for report in reports] == [1, 2, 2, 4, 4]

    record = runtime._load_candidate_record(run_id, candidate_id)
    assert [iteration.disposition for iteration in record.iterations] == [
        "keep",
        "keep",
        "discard",
        "retain",
        "failure",
    ]
    assert all(iteration.git_head for iteration in record.iterations)
    assert record.iterations[-1].process_passed is False
    best = json.loads(
        (runtime.root_dir / "runs" / run_id / "best.json").read_text(
            encoding="utf-8"
        )
    )
    assert best == {
        "artifact_hash": record.iterations[3].artifact_hash,
        "artifact_ref": {
            "commit": record.iterations[3].git_head,
            "kind": "git_commit",
        },
        "candidate_id": candidate_id,
        "changed_files": ["initial_program.py"],
        "commit": record.iterations[3].git_head,
        "iteration": 4,
        "metric_direction": direction,
        "metric_name": "combined_score",
        "run_id": run_id,
        "schema_version": 2,
        "score": scores["equal"],
        "updated_at": record.iterations[3].created_at,
        "workspace": f"workspace/{candidate_id}",
    }
    history = runtime.list_history(run_id, top_n=1)["candidates"][0]
    assert history["best_iteration"] == 4
    assert history["latest_disposition"] == "failure"
    assert (
        history["workspace_git_head_after_settlement"] == record.results_ledger_git_head
    )

    final_head = _git(workspace, "rev-parse", "HEAD")
    for iteration, value in zip(
        record.iterations,
        ("seed", "improved", "worse", "equal", "broken"),
        strict=True,
    ):
        assert _git(workspace, "show", f"{iteration.git_head}:initial_program.py") == (
            f"VALUE = {value!r}"
        )
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", iteration.git_head, final_head],
            cwd=workspace,
            check=True,
        )

    assert (
        len((workspace / "results.tsv").read_text(encoding="utf-8").splitlines()) == 6
    )
    assert (
        _git(
            workspace,
            "status",
            "--porcelain=v1",
            "--",
            "initial_program.py",
            "results.tsv",
        )
        == ""
    )
    messages = _git(workspace, "log", "--format=%s").splitlines()
    assert sum(message.startswith("goal-plus restore") for message in messages) == 2


def test_latest_run_level_equal_score_becomes_best(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    spec = spec_for(project, max_parallel=2)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    monkeypatch.setattr(
        runtime,
        "_execute_verifier_process",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, '{"combined_score": 1}\n', ""
        ),
    )

    reports = []
    for task in tasks:
        session = runtime.start_agent_session(run_id, task.candidate_id)
        (task.workspace / "initial_program.py").write_text(
            f"VALUE = {task.candidate_id!r}\n",
            encoding="utf-8",
        )
        reports.append(
            runtime.run_verifier(
                run_id,
                task.candidate_id,
                agent_session_id=session.agent_session_id,
                hypothesis=f"try {task.candidate_id}",
            )
        )

    best = json.loads(
        (runtime.root_dir / "runs" / run_id / "best.json").read_text(
            encoding="utf-8"
        )
    )
    assert best["candidate_id"] == tasks[1].candidate_id
    assert best["commit"] == reports[1].best_git_head
    assert runtime.status(run_id).best_candidate_id == tasks[1].candidate_id
    assert runtime.select(run_id)["selected_candidate_id"] == tasks[1].candidate_id
    selected_best = json.loads(
        (runtime.root_dir / "runs" / run_id / "best.json").read_text(
            encoding="utf-8"
        )
    )
    assert selected_best["candidate_id"] == tasks[1].candidate_id
    assert selected_best["commit"] == reports[1].best_git_head


def test_first_failed_iteration_restores_pre_attempt_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate_id, workspace = _candidate(tmp_path)
    program = workspace / "initial_program.py"

    monkeypatch.setattr(
        runtime,
        "_execute_verifier_process",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "invalid candidate"
        ),
    )
    program.write_text("VALUE = 'broken'\n", encoding="utf-8")

    report = runtime.run_verifier(run_id, candidate_id, hypothesis="break baseline")

    assert report.disposition == "failure"
    assert report.best_iteration is None
    assert program.read_text(encoding="utf-8") == "VALUE = 0\n"
    iteration = runtime._load_candidate_record(run_id, candidate_id).iterations[0]
    assert _git(workspace, "show", f"{iteration.git_head}:initial_program.py") == (
        "VALUE = 'broken'"
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", iteration.git_head, "HEAD"],
        cwd=workspace,
        check=True,
    )


def test_select_and_promote_keep_all_iteration_commits_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, candidate_id, workspace = _candidate(tmp_path)
    program = workspace / "initial_program.py"

    def fake_verify(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        value = program.read_text(encoding="utf-8").split("'", 2)[1]
        return subprocess.CompletedProcess(
            command,
            0,
            f'{{"combined_score": {1 if value == "seed" else 2}}}\n',
            "",
        )

    monkeypatch.setattr(runtime, "_execute_verifier_process", fake_verify)
    for value in ("seed", "best", "equal"):
        program.write_text(f"VALUE = {value!r}\n", encoding="utf-8")
        runtime.run_verifier(run_id, candidate_id, hypothesis=f"try {value}")

    selected = runtime.select(run_id)
    assert selected["selected_iteration"] == 3
    assert selected["selected_score"] == 2.0
    assert program.read_text(encoding="utf-8") == "VALUE = 'equal'\n"
    selected_best = json.loads(
        (runtime.root_dir / "runs" / run_id / "best.json").read_text(
            encoding="utf-8"
        )
    )
    assert selected_best["iteration"] == selected["selected_iteration"]
    assert selected_best["commit"] == selected["selected_git_head"]
    runtime.promote(run_id, candidate_id)

    record = runtime._load_candidate_record(run_id, candidate_id)
    assert [iteration.disposition for iteration in record.iterations] == [
        "keep",
        "keep",
        "retain",
        "retain",
    ]
    revisions = {
        revision
        for iteration in record.iterations
        for revision in (iteration.git_head, iteration.ledger_git_head)
        if revision is not None
    }
    for revision in revisions:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
            cwd=workspace,
            check=True,
        )
    unreachable = _git(workspace, "fsck", "--no-reflogs", "--unreachable")
    assert not any(
        line.startswith("unreachable commit ") for line in unreachable.splitlines()
    )

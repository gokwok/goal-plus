from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus import revision_verifier


def test_revision_verifier_runs_exact_argv_with_scoped_cwd_and_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    cwd = source / "bench"
    cwd.mkdir(parents=True)
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs["cwd"]
        seen["env"] = dict(kwargs["env"])
        command_cwd = Path(kwargs["cwd"])
        assert (command_cwd / kwargs["env"]["TMPDIR"]).is_dir()
        assert (command_cwd / kwargs["env"]["TORCH_EXTENSIONS_DIR"]).is_dir()
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(revision_verifier.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")

    returncode = revision_verifier.main(
        [
            "--source-path",
            str(source),
            "--cwd",
            "bench",
            "--phase",
            "promotion",
            "--",
            "python3",
            "verify.py",
            "--flag=value with spaces",
        ]
    )

    assert returncode == 7
    assert seen["command"] == [
        "python3",
        "verify.py",
        "--flag=value with spaces",
    ]
    assert seen["cwd"] == cwd
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["GOAL_PLUS_VERIFIER_PHASE"] == "promotion"
    assert environment["PYTHONPATH"].split(os.pathsep) == [
        ".",
        "/existing/pythonpath",
    ]
    assert environment["MAX_JOBS"] == "1"
    scratch = environment["GOAL_PLUS_VERIFIER_TMPDIR"]
    assert environment["TMPDIR"] == scratch
    assert environment["TMP"] == scratch
    assert environment["TEMP"] == scratch
    assert not (cwd / scratch).exists()


def test_revision_verifier_keeps_exact_snapshot_paths_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    cwd = source / "bench"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        seen["env"] = dict(kwargs["env"])
        command_cwd = Path(kwargs["cwd"])
        assert not command_cwd.is_absolute()
        assert (command_cwd / kwargs["env"]["TMPDIR"]).is_dir()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(revision_verifier.subprocess, "run", fake_run)

    assert revision_verifier.main(
        [
            "--source-path",
            "workspace",
            "--cwd",
            "bench",
            "--phase",
            "candidate",
            "--",
            "python",
            "verify.py",
        ]
    ) == 0

    assert seen["cwd"] == Path("workspace/bench")
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert not Path(environment["TMPDIR"]).is_absolute()
    assert not Path(environment["TORCH_EXTENSIONS_DIR"]).is_absolute()


def test_revision_verifier_creates_scratch_components_without_recursive_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    original_mkdir = Path.mkdir

    def sandlock_sensitive_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if parents:
            raise PermissionError("recursive mkdir receives EACCES in discard view")
        original_mkdir(path, mode=mode, parents=False, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", sandlock_sensitive_mkdir)
    monkeypatch.setattr(
        revision_verifier.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert revision_verifier.main(
        [
            "--source-path",
            str(source),
            "--cwd",
            ".",
            "--phase",
            "candidate",
            "--",
            "python",
            "verify.py",
        ]
    ) == 0


def test_revision_verifier_rejects_cwd_escape_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    monkeypatch.setattr(
        revision_verifier.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("escaped verifier must not launch"),
    )

    with pytest.raises(SystemExit, match="cwd escapes source_path"):
        revision_verifier.main(
            [
                "--source-path",
                str(source),
                "--cwd",
                "..",
                "--phase",
                "candidate",
                "--",
                "python3",
                "verify.py",
            ]
        )

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one existing Goal Plus verifier in exact snapshot execution."
    )
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--phase", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("verifier command argv is required")

    source = Path(args.source_path)
    command_cwd = Path(args.cwd)
    if command_cwd.is_absolute() or ".." in command_cwd.parts:
        raise SystemExit("verifier cwd escapes source_path")
    if not source.is_absolute() and ".." in source.parts:
        raise SystemExit("verifier source_path escapes execution root")
    cwd = source / command_cwd
    if not cwd.is_dir():
        raise SystemExit(f"verifier cwd does not exist: {cwd}")

    # Create one level at a time. Sandlock's exact-snapshot discard view can
    # report EACCES (rather than ENOENT) when mkdir is first attempted below a
    # missing parent. pathlib's parents=True implementation only retries on
    # ENOENT, so an otherwise writable snapshot would fail before the verifier
    # starts. Each component is runtime scratch and is discarded with fs.run.
    scratch_root = source / ".tmp"
    scratch_root.mkdir(exist_ok=True)
    scratch_parent = scratch_root / "goal-plus-verifier"
    scratch_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="exact-",
        dir=scratch_parent,
    ) as scratch:
        scratch_path = Path(scratch)
        torch_extensions = scratch_path / "torch-extensions"
        torch_extensions.mkdir()
        environment = os.environ.copy()
        environment["GOAL_PLUS_VERIFIER_PHASE"] = args.phase
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (".", environment.get("PYTHONPATH", ""))
            if value
        )
        scratch_for_command = os.path.relpath(scratch_path, start=cwd)
        torch_extensions_for_command = os.path.relpath(
            torch_extensions,
            start=cwd,
        )
        for name in ("TMPDIR", "TMP", "TEMP", "GOAL_PLUS_VERIFIER_TMPDIR"):
            environment[name] = scratch_for_command
        environment["TORCH_EXTENSIONS_DIR"] = torch_extensions_for_command
        environment["MAX_JOBS"] = "1"
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
        )
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import subprocess
from pathlib import Path
import re
import tomllib

from goal_plus.thinkthread_agent_posix import AGENT_POSIX_CONTRACT_FINGERPRINT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPOSITORY_ROOT / ".thinkthread" / "pi-goal-plus.toml"
INSTALLER_PATH = (
    REPOSITORY_ROOT / "scripts" / "install_pi_goal_plus_thinkthread.sh"
)
PI_EXTENSION_PATH = REPOSITORY_ROOT / ".pi" / "extensions" / "goal-plus.ts"


def _profile() -> dict:
    with PROFILE_PATH.open("rb") as stream:
        return tomllib.load(stream)


def test_pi_goal_plus_is_the_only_project_thinkthread_profile() -> None:
    profiles = sorted((REPOSITORY_ROOT / ".thinkthread").glob("*.toml"))
    assert profiles == [PROFILE_PATH]


def test_pi_goal_plus_profile_is_workspace_generic_and_isolated() -> None:
    profile = _profile()
    assert profile["schemaVersion"] == 4
    assert profile["agent"]["configDirDefault"] == "${HOME}/.pi/agent"
    assert profile["agent"]["configDirAccess"] == "read_write"
    assert "${HOME}/.local/share/pi-node/current/bin" in profile["agent"][
        "executableSearchPaths"
    ]
    assert profile["agent"]["executableReadAncestor"] == "pi-node"
    assert profile["agent"]["args"] == [
        "--extension",
        "${HOME}/.local/share/goal-plus/pi/goal-plus.ts",
        "--skill",
        "${HOME}/.local/share/goal-plus/pi/goal-plus/SKILL.md",
    ]
    assert profile["agent"]["extension"]["loader"] == {
        "kind": "process",
        "command": ["thinkthread-extension-pi"],
        "protocolVersion": 4,
    }

    environment = profile["environment"]
    assert environment["fs"] == "."
    assert environment["rootFsMode"] == "direct"
    assert environment["childFsMode"] == "copy_on_write"
    assert environment["write"] == []
    assert "${HOME}/.local/share/goal-plus" in environment["read"]
    assert "/sys/devices/system/cpu" in environment["read"]
    assert environment["root"]["GOAL_PLUS_ROOT"] == (
        "${PI_CODING_AGENT_SESSION_DIR}/goal-plus"
    )
    assert environment["root"]["GOAL_PLUS_PI_TOOL"] == (
        "${HOME}/.local/share/goal-plus/bin/goal-plus-pi-tool"
    )
    assert "GOAL_PLUS_ROOT" not in environment["child"]
    assert environment["child"]["GOAL_PLUS_PI_ROLE"] == "worker"
    assert profile["delegation"]["capabilities"]["allow"] == [
        "thinkthread.message"
    ]


def test_installer_is_shell_valid_and_does_not_modify_normal_pi_config() -> None:
    subprocess.run(["bash", "-n", str(INSTALLER_PATH)], check=True)
    text = INSTALLER_PATH.read_text(encoding="utf-8")
    assert 'install_root="$HOME/.local/share/goal-plus"' in text
    assert "pi-goal-plus.toml" in text
    assert "@thinkthread/agent-posix/dist/index.js" in text
    assert "CONTRACT_FINGERPRINT" in text
    assert "npm init --yes" in text
    assert "goal-plus-pi-tool-installed" in text
    assert "$HOME/.pi/agent" not in text
    assert ".pi/agent/extensions" not in text
    assert ".pi/agent/skills" not in text


def test_pi_worker_extension_pins_the_same_agent_posix_contract() -> None:
    text = PI_EXTENSION_PATH.read_text(encoding="utf-8")
    matched = re.search(
        r'const AGENT_POSIX_CONTRACT_FINGERPRINT =\s*"([0-9a-f]{64})";',
        text,
    )
    assert matched is not None
    assert matched.group(1) == AGENT_POSIX_CONTRACT_FINGERPRINT


def test_installer_help_documents_source_and_prebuilt_sdk_paths() -> None:
    completed = subprocess.run(
        ["bash", str(INSTALLER_PATH), "--help"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--thinkthread-source PATH" in completed.stdout
    assert "--agent-posix-package FILE" in completed.stdout
    assert "tt pi-goal-plus" in completed.stdout

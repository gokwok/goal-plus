from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.thinkthread_agent_posix import (
    AGENT_POSIX_CONTRACT_FINGERPRINT,
    AgentPosixBridgeError,
    AgentPosixSdkClient,
)


def test_sdk_bridge_preflight_uses_official_contract_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["request"] = json.loads(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "contractFingerprint": AGENT_POSIX_CONTRACT_FINGERPRINT,
                        "controlProtocolVersion": 2,
                        "methods": ["self"],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("goal_plus.thinkthread_agent_posix.subprocess.run", fake_run)
    result = AgentPosixSdkClient(bridge_path=bridge).preflight()

    assert seen["request"] == {"operation": "bridge.meta", "params": {}}
    assert result["contractFingerprint"] == AGENT_POSIX_CONTRACT_FINGERPRINT


def test_sdk_bridge_preserves_completion_unknown(monkeypatch, tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    monkeypatch.setattr(
        "goal_plus.thinkthread_agent_posix.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": {
                        "name": "TransportError",
                        "message": "socket closed",
                        "category": "transport",
                        "delivery": "completion_unknown",
                    },
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(AgentPosixBridgeError) as captured:
        AgentPosixSdkClient(bridge_path=bridge).invoke("fs.snapshot.create")

    assert captured.value.completion_unknown is True


def test_snapshot_diff_all_uses_sdk_page_limit_and_cursor(monkeypatch, tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    calls: list[tuple[str, dict]] = []

    def fake_invoke(operation: str, params: dict):
        calls.append((operation, params))
        if len(calls) == 1:
            return {
                "changes": [{"path": "first.py"}],
                "hasMore": True,
                "nextCursor": "cursor-1",
            }
        return {
            "changes": [{"path": "second.py"}],
            "hasMore": False,
            "nextCursor": None,
        }

    monkeypatch.setattr(client, "invoke", fake_invoke)

    assert client.snapshot_diff_all("fsnap-base", "fsnap-target") == [
        {"path": "first.py"},
        {"path": "second.py"},
    ]
    assert calls == [
        (
            "fs.snapshot.diff",
            {
                "baseSnapshotId": "fsnap-base",
                "targetSnapshotId": "fsnap-target",
                "limit": 256,
            },
        ),
        (
            "fs.snapshot.diff",
            {
                "baseSnapshotId": "fsnap-base",
                "targetSnapshotId": "fsnap-target",
                "limit": 256,
                "cursor": "cursor-1",
            },
        ),
    ]


def test_snapshot_read_file_respects_sdk_pread_length_limit(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    payload = b"x" * (64 * 1024 + 7)
    calls: list[dict] = []

    def fake_invoke(operation: str, params: dict):
        assert operation == "fs.snapshot.pread"
        calls.append(params)
        offset = params["offset"]
        length = params["length"]
        assert 1 <= length <= 64 * 1024
        chunk = payload[offset : offset + length]
        import base64

        return {
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "bytesRead": len(chunk),
            "eof": offset + len(chunk) >= len(payload),
        }

    monkeypatch.setattr(client, "invoke", fake_invoke)

    assert client.snapshot_read_file("fsnap-1", "model.py") == payload
    assert [call["length"] for call in calls] == [64 * 1024, 64 * 1024]

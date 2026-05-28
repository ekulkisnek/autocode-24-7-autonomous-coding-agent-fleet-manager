from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autocode import grok_watchdog


@pytest.fixture(autouse=True)
def isolated_grok_watchdog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pending = tmp_path / "grok-watchdog-pending.json"
    fake_bin = tmp_path / "autocode-grok-watchdog"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(grok_watchdog, "PENDING_PATH", pending)
    monkeypatch.setattr(grok_watchdog, "GROK_BIN", fake_bin)
    monkeypatch.setattr(grok_watchdog, "COALESCE_SEC", 0.15)
    monkeypatch.setattr(grok_watchdog, "FALLBACK_INTERVAL", 0)
    monkeypatch.setenv("AUTOCODE_GROK_WATCHDOG", "on")
    grok_watchdog.reset_state_for_tests()
    yield
    grok_watchdog.reset_state_for_tests()


def test_request_disabled_by_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTOCODE_GROK_WATCHDOG", "off")
    popen = MagicMock()
    monkeypatch.setattr(grok_watchdog.subprocess, "Popen", popen)
    grok_watchdog.request("daemon_tick")
    popen.assert_not_called()


def test_request_schedules_flush_subprocess(monkeypatch: pytest.MonkeyPatch):
    popen = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(grok_watchdog.subprocess, "Popen", popen)
    grok_watchdog.request("daemon_tick")
    assert popen.called
    cmd = popen.call_args[0][0]
    assert "time.sleep" in cmd[2]


def test_flush_pending_if_due_coalesces_reasons(monkeypatch: pytest.MonkeyPatch):
    launched: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        if cmd and cmd[0] == str(grok_watchdog.GROK_BIN):
            launched.append(cmd)
        return MagicMock()

    monkeypatch.setattr(grok_watchdog.subprocess, "Popen", fake_popen)
    grok_watchdog.request("daemon_tick")
    grok_watchdog.request("job_completed")
    data = json.loads(grok_watchdog.PENDING_PATH.read_text(encoding="utf-8"))
    data["scheduled_at"] = time.time() - grok_watchdog.COALESCE_SEC - 0.01
    grok_watchdog.PENDING_PATH.write_text(json.dumps(data) + "\n", encoding="utf-8")
    grok_watchdog.flush_pending_if_due()
    assert launched
    trigger = launched[0][launched[0].index("--trigger") + 1]
    assert "daemon_tick" in trigger
    assert "job_completed" in trigger
    data = json.loads(grok_watchdog.PENDING_PATH.read_text(encoding="utf-8"))
    assert data.get("reasons") == []


def test_fallback_requests_when_interval_elapsed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(grok_watchdog, "FALLBACK_INTERVAL", 60)
    grok_watchdog.PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    grok_watchdog.PENDING_PATH.write_text(
        json.dumps({"reasons": [], "last_run": time.time() - 120, "scheduled_at": 0}) + "\n",
        encoding="utf-8",
    )
    popen = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(grok_watchdog.subprocess, "Popen", popen)
    grok_watchdog.on_daemon_tick()
    data = json.loads(grok_watchdog.PENDING_PATH.read_text(encoding="utf-8"))
    assert "daemon_tick" in data.get("reasons", [])
    assert "fallback" in data.get("reasons", [])

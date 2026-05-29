from __future__ import annotations

from unittest.mock import patch

from autocode.coordination import kill_duplicate_l1_processes, l1_protected_pids


def test_l1_protected_pids_includes_self():
    protected = l1_protected_pids()
    import os

    assert os.getpid() in protected


def test_kill_duplicate_skips_protected(monkeypatch):
    import autocode.coordination as coord

    monkeypatch.setattr(coord, "l1_protected_pids", lambda **kw: {100, 200})
    monkeypatch.setattr(
        coord.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "100\n300\n", "returncode": 0})(),
    )
    killed = []
    monkeypatch.setattr(coord.os, "kill", lambda pid, sig: killed.append(pid))
    result = kill_duplicate_l1_processes()
    assert 100 not in result
    assert 200 not in result
    assert 300 in result

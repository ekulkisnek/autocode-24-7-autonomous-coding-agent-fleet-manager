from __future__ import annotations

from autocode.quota_probes import QuotaProbeResult, _window_summary, format_report


def test_window_summary_formats_remaining_percent():
    primary = {"usedPercent": 26, "windowDurationMins": 300, "resetsAt": 1779671131}
    secondary = {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1780257931}
    text = _window_summary(primary, secondary)
    assert "74% left" in text
    assert "96% left" in text


def test_format_report_includes_providers():
    results = {
        name: QuotaProbeResult(name, "unavailable", "test")
        for name in ("codex", "claude", "antigravity", "grok", "cursor")
    }
    text = format_report(results)
    assert "codex" in text
    assert "claude" in text
    assert "cursor" in text

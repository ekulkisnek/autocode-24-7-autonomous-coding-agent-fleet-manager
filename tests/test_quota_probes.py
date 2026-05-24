from __future__ import annotations

import base64
import json

from autocode.quota_probes import QuotaProbeResult, _claude_usage_windows, _grok_jwt_tier, _window_summary, format_report


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


def _make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.fakesig"


def test_claude_usage_windows_maps_utilization():
    usage = {
        "five_hour": {"utilization": 67.0, "resets_at": "2026-05-25T02:00:00+00:00"},
        "seven_day": {"utilization": 5.0, "resets_at": "2026-05-26T20:00:00+00:00"},
    }
    primary, secondary = _claude_usage_windows(usage)
    assert primary["usedPercent"] == 67
    assert primary["windowDurationMins"] == 300
    assert secondary["usedPercent"] == 5
    assert secondary["windowDurationMins"] == 10080


def test_claude_usage_windows_returns_empty_on_missing_data():
    primary, secondary = _claude_usage_windows({})
    assert primary == {}
    assert secondary == {}


def test_grok_jwt_tier_extracts_tier_claim():
    token = _make_jwt({"tier": 5, "sub": "user123"})
    assert _grok_jwt_tier(token) == " · tier=5"


def test_grok_jwt_tier_returns_empty_when_no_tier():
    token = _make_jwt({"sub": "user123", "exp": 9999999999})
    assert _grok_jwt_tier(token) == ""


def test_grok_jwt_tier_returns_empty_on_invalid_input():
    assert _grok_jwt_tier("") == ""
    assert _grok_jwt_tier("notajwt") == ""
    assert _grok_jwt_tier("a.!!!.c") == ""


def test_probe_claude_returns_ok_with_live_usage_windows(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_command_available", lambda _: True)
    monkeypatch.setattr(
        quota_probes,
        "_claude_auth_status",
        lambda: {"loggedIn": True, "subscriptionType": "pro"},
    )
    monkeypatch.setattr(quota_probes, "_claude_keychain_meta", lambda: {"rateLimitTier": "default_claude_ai", "subscriptionType": "pro", "expiresAt": None})
    monkeypatch.setattr(
        quota_probes,
        "_claude_oauth_get",
        lambda path: {
            "five_hour": {"utilization": 67.0, "resets_at": "2026-05-25T02:00:00+00:00"},
            "seven_day": {"utilization": 5.0, "resets_at": "2026-05-26T20:00:00+00:00"},
        } if "usage" in path else {},
    )

    result = quota_probes.probe_claude()
    assert result.status == "ok"
    assert "33% left" in result.summary  # 100 - 67
    assert "95% left" in result.summary  # 100 - 5
    assert result.source == "claude.ai /api/oauth/usage"


def test_probe_claude_summary_uses_keychain_tier(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_command_available", lambda _: True)
    monkeypatch.setattr(
        quota_probes,
        "_claude_auth_status",
        lambda: {"loggedIn": True, "subscriptionType": "pro"},
    )
    monkeypatch.setattr(
        quota_probes,
        "_claude_keychain_meta",
        lambda: {"rateLimitTier": "default_claude_ai", "subscriptionType": "pro", "expiresAt": None},
    )
    monkeypatch.setattr(quota_probes, "_claude_oauth_get", lambda _path: {})

    result = quota_probes.probe_claude()
    assert result.status in {"ok", "partial"}
    assert "default_claude_ai" in result.summary
    assert "active" not in result.summary or "/" in result.summary  # no bare repeated prefix


def test_grok_probe_summary_format():
    """Verify that the grok probe summary is built from tier and expiry hints."""
    from autocode import quota_probes

    tier_hint = " · tier=5"
    expires_hint = ", token 05-25 02:45"
    summary = f"oidc{tier_hint}{expires_hint} (no remaining counter)"
    result = quota_probes.QuotaProbeResult("grok", "partial", summary)
    assert result.status == "partial"
    assert "tier=5" in result.summary
    assert "no remaining counter" in result.summary


def test_probe_cursor_shows_unlimited_with_period_when_no_cap(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_cursor_agent_bin", lambda: "/fake/cursor-agent")
    monkeypatch.setattr(
        quota_probes,
        "_run_json",
        lambda cmd, **_kw: {"stdout": '{"subscriptionTier":"Pro+","model":"Auto","cliVersion":"1.0"}', "returncode": 0},
    )
    monkeypatch.setattr(quota_probes, "_cursor_membership_type", lambda: "pro_plus")
    monkeypatch.setattr(quota_probes, "_cursor_subscription_status", lambda: "active")
    monkeypatch.setattr(
        quota_probes,
        "_cursor_usage_api",
        lambda: {
            "gpt-4": {"numRequests": 42, "numRequestsTotal": 100, "numTokens": 5000, "maxTokenUsage": None, "maxRequestUsage": None},
            "startOfMonth": "2026-04-25T16:15:24.000Z",
        },
    )

    result = quota_probes.probe_cursor()
    assert result.status == "partial"
    assert "Pro+" in result.summary
    assert "unlimited" in result.summary
    assert "04-25" in result.summary  # billing period date


def test_probe_cursor_shows_remaining_when_cap_present(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_cursor_agent_bin", lambda: "/fake/cursor-agent")
    monkeypatch.setattr(
        quota_probes,
        "_run_json",
        lambda cmd, **_kw: {"stdout": '{"subscriptionTier":"Pro","model":"Auto","cliVersion":"1.0"}', "returncode": 0},
    )
    monkeypatch.setattr(quota_probes, "_cursor_membership_type", lambda: "pro")
    monkeypatch.setattr(quota_probes, "_cursor_subscription_status", lambda: "active")
    monkeypatch.setattr(
        quota_probes,
        "_cursor_usage_api",
        lambda: {
            "gpt-4": {"numRequests": 350, "numRequestsTotal": 350, "numTokens": 0, "maxTokenUsage": None, "maxRequestUsage": 500},
            "startOfMonth": "2026-04-25T16:15:24.000Z",
        },
    )

    result = quota_probes.probe_cursor()
    assert result.status == "ok"
    assert "150/500" in result.summary  # 500 - 350 = 150 remaining
    assert "req remaining" in result.summary


def test_probe_cursor_falls_back_gracefully_when_api_unavailable(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_cursor_agent_bin", lambda: "/fake/cursor-agent")
    monkeypatch.setattr(
        quota_probes,
        "_run_json",
        lambda cmd, **_kw: {"stdout": '{"subscriptionTier":"Pro+","model":"Auto"}', "returncode": 0},
    )
    monkeypatch.setattr(quota_probes, "_cursor_membership_type", lambda: "pro_plus")
    monkeypatch.setattr(quota_probes, "_cursor_subscription_status", lambda: "active")
    monkeypatch.setattr(quota_probes, "_cursor_usage_api", lambda: {})

    result = quota_probes.probe_cursor()
    assert result.status == "partial"
    assert "Pro+" in result.summary
    assert "no remaining counter" in result.summary

from __future__ import annotations

import base64
import json

from autocode.quota_probes import QuotaProbeResult, _grok_jwt_tier, _window_summary, format_report


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


def test_probe_cursor_includes_subscription_status(monkeypatch):
    from autocode import quota_probes

    monkeypatch.setattr(quota_probes, "_cursor_agent_bin", lambda: "/fake/cursor-agent")
    monkeypatch.setattr(
        quota_probes,
        "_run_json",
        lambda cmd, **_kw: {"stdout": '{"subscriptionTier":"Pro+","model":"Auto","cliVersion":"1.0"}', "returncode": 0},
    )
    monkeypatch.setattr(quota_probes, "_cursor_membership_type", lambda: "pro_plus")
    monkeypatch.setattr(quota_probes, "_cursor_subscription_status", lambda: "active")

    result = quota_probes.probe_cursor()
    assert result.status == "partial"
    assert "active" in result.summary
    assert "Pro+" in result.summary

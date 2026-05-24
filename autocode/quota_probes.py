from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import HOME
from .util import command_exists, json_loads, parse_ts


PROVIDERS = ("codex", "claude", "antigravity", "grok", "cursor")
CACHE_TTL_SECONDS = 120.0

_cache: dict[str, tuple[float, "QuotaProbeResult"]] = {}


@dataclass(frozen=True)
class QuotaProbeResult:
    provider: str
    status: str  # ok | partial | unavailable | error
    summary: str
    source: str = ""
    auth: str = ""
    refresh_hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def probe_all(*, use_cache: bool = True, ttl: float = CACHE_TTL_SECONDS) -> dict[str, QuotaProbeResult]:
    probes = {
        "codex": probe_codex,
        "claude": probe_claude,
        "antigravity": probe_antigravity,
        "grok": probe_grok,
        "cursor": probe_cursor,
    }
    out: dict[str, QuotaProbeResult] = {}
    for name, fn in probes.items():
        if use_cache:
            cached = _cache.get(name)
            if cached and time.time() - cached[0] < ttl:
                out[name] = cached[1]
                continue
        result = fn()
        _cache[name] = (time.time(), result)
        out[name] = result
    return out


def probe_codex() -> QuotaProbeResult:
    codex = _codex_bin()
    if not codex:
        return _unavailable("codex", "codex binary not found")
    try:
        payload = _codex_rate_limits(codex)
    except Exception as exc:
        return QuotaProbeResult("codex", "error", "probe failed", source="codex app-server", error=str(exc))
    limits = payload.get("rateLimits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return QuotaProbeResult("codex", "error", "unexpected rateLimits payload", source="codex app-server")
    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else {}
    secondary = limits.get("secondary") if isinstance(limits.get("secondary"), dict) else {}
    plan = limits.get("planType") or "unknown"
    summary = _window_summary(primary, secondary)
    credits = limits.get("credits") if isinstance(limits.get("credits"), dict) else {}
    return QuotaProbeResult(
        "codex",
        "ok",
        summary,
        source="codex app-server account/rateLimits/read",
        auth="~/.codex/auth.json (ChatGPT OAuth)",
        refresh_hint="120s; 0-cost read",
        data={
            "plan_type": plan,
            "primary": primary,
            "secondary": secondary,
            "credits": credits,
            "rate_limits_by_limit_id": payload.get("rateLimitsByLimitId"),
        },
    )


def probe_claude() -> QuotaProbeResult:
    if not _command_available("claude"):
        return _unavailable("claude", "claude CLI not found")
    auth = _claude_auth_status()
    if not auth.get("loggedIn"):
        return QuotaProbeResult("claude", "unavailable", "not logged in", source="claude auth status")
    keychain = _claude_keychain_meta()
    # /api/oauth/profile is Cloudflare-challenged headlessly; keychain is the reliable fallback
    rate_limit_tier = keychain.get("rateLimitTier") or auth.get("subscriptionType") or "unknown"
    sub = auth.get("subscriptionType") or "unknown"
    usage = _claude_oauth_get("/api/oauth/usage")
    profile = _claude_oauth_get("/api/oauth/profile")
    org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
    tier = org.get("rate_limit_tier") or rate_limit_tier
    sub_status = org.get("subscription_status") or sub
    extra = usage.get("extra_usage") if isinstance(usage.get("extra_usage"), dict) else {}
    has_remaining = any(
        extra.get(k) is not None
        for k in ("monthly_limit", "used_credits", "utilization")
    )
    exp_hint = ""
    exp_ts = keychain.get("expiresAt")
    if isinstance(exp_ts, (int, float)) and exp_ts > 0:
        exp_dt = datetime.fromtimestamp(exp_ts / 1000, timezone.utc).astimezone()
        exp_hint = f", token {exp_dt.strftime('%m-%d %H:%M')}"
    if has_remaining:
        summary = f"extra usage: {extra.get('used_credits')}/{extra.get('monthly_limit')}"
        status = "partial"
    else:
        summary = f"{sub_status}/{tier}{exp_hint} (no remaining counter)"
        status = "partial"
    return QuotaProbeResult(
        "claude",
        status,
        summary,
        source="claude auth status + macOS keychain",
        auth='macOS keychain "Claude Code-credentials" OAuth',
        refresh_hint="300s; local keychain read",
        data={
            "auth_status": {k: auth.get(k) for k in ("loggedIn", "authMethod", "subscriptionType", "orgName")},
            "rate_limit_tier": tier,
            "subscription_status": sub_status,
            "extra_usage": extra,
            "keychain": {k: keychain.get(k) for k in ("rateLimitTier", "subscriptionType")},
        },
    )


def probe_grok() -> QuotaProbeResult:
    grok = _grok_bin()
    if not grok:
        return _unavailable("grok", "grok binary not found")
    auth_path = HOME / ".grok" / "auth.json"
    if not auth_path.exists():
        return QuotaProbeResult("grok", "unavailable", "not logged in", source=str(auth_path))
    try:
        auth_doc = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return QuotaProbeResult("grok", "error", "auth parse failed", source=str(auth_path), error=str(exc))
    entry = auth_doc.get("https://accounts.x.ai/sign-in") or next(iter(auth_doc.values()), {})
    tier_hint = _grok_jwt_tier(entry.get("key") if isinstance(entry, dict) else "")
    expires_hint = ""
    expires_at = entry.get("expires_at") if isinstance(entry, dict) else None
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone()
            expires_hint = f", token {exp_dt.strftime('%m-%d %H:%M')}"
        except Exception:
            pass
    leader = _run_json([grok, "leader", "list"], timeout=8)
    leader_text = (leader.get("stdout") or "").strip()
    has_leader = bool(leader_text and "No leader candidates found" not in leader_text)
    summary = f"oidc{tier_hint}{expires_hint} (no remaining counter)"
    return QuotaProbeResult(
        "grok",
        "partial",
        summary,
        source=str(auth_path),
        auth=str(auth_path),
        # api.x.ai/v1/me works (user info only); /v1/usage, /v1/credits, /v1/me/usage all 404;
        # grok.com REST API is CF/auth blocked headlessly — no stable usage endpoint exists.
        refresh_hint="300s; local file read (x.ai API has no public usage endpoint for CLI tokens)",
        data={
            "auth_mode": entry.get("auth_mode") if isinstance(entry, dict) else None,
            "email": entry.get("email") if isinstance(entry, dict) else None,
            "expires_at": expires_at,
            "tier_from_jwt": tier_hint.lstrip(" · tier=") if tier_hint else None,
            "leader_running": has_leader,
            "checked_endpoints": [
                "api.x.ai/v1/me (ok, user info only)",
                "api.x.ai/v1/usage (404)",
                "api.x.ai/v1/credits (404)",
                "grok.com/rest/app-chat/conversations/quota (403/CF)",
            ],
        },
    )


def probe_cursor() -> QuotaProbeResult:
    agent = _cursor_agent_bin()
    if not agent:
        return _unavailable("cursor", "cursor-agent not found")
    about = _run_json([agent, "about", "--format", "json"], timeout=12)
    about_obj = json_loads(about.get("stdout") or "{}", {})
    if not isinstance(about_obj, dict):
        about_obj = {}
    tier = about_obj.get("subscriptionTier") or _cursor_membership_type() or "unknown"
    model = about_obj.get("model") or "auto"
    stripe_status = _cursor_subscription_status()

    # Live usage from api2.cursor.sh (Bearer JWT from state.vscdb)
    usage = _cursor_usage_api()
    gpt4 = usage.get("gpt-4") if isinstance(usage.get("gpt-4"), dict) else {}
    num_req = gpt4.get("numRequests") if isinstance(gpt4.get("numRequests"), int) else None
    max_req = gpt4.get("maxRequestUsage")  # None means no cap
    start_of_month = usage.get("startOfMonth", "") if usage else ""

    period_hint = ""
    if start_of_month:
        try:
            period_dt = datetime.fromisoformat(start_of_month.replace("Z", "+00:00")).astimezone()
            period_hint = f", period since {period_dt.strftime('%m-%d')}"
        except Exception:
            pass

    if isinstance(max_req, (int, float)) and max_req > 0 and isinstance(num_req, int):
        remaining = max(0, int(max_req) - num_req)
        summary = f"{tier} / model={model} · {remaining}/{int(max_req)} req remaining{period_hint}"
        probe_status = "ok"
    elif usage:
        status_hint = f" · {stripe_status}" if stripe_status else ""
        summary = f"{tier} / model={model}{status_hint} (unlimited{period_hint})"
        probe_status = "partial"
    else:
        status_hint = f" · {stripe_status}" if stripe_status else ""
        summary = f"{tier} / model={model}{status_hint} (no remaining counter)"
        probe_status = "partial"

    return QuotaProbeResult(
        "cursor",
        probe_status,
        summary,
        source="cursor-agent about + api2.cursor.sh/auth/usage + state.vscdb",
        auth="cursor-agent JWT session (cursorAuth/accessToken in state.vscdb)",
        refresh_hint="120s; authenticated GET to api2.cursor.sh",
        data={
            "about": {k: about_obj.get(k) for k in ("cliVersion", "subscriptionTier", "model")},
            "stripe_membership_type": _cursor_membership_type(),
            "stripe_subscription_status": stripe_status,
            "api_usage": {
                "numRequests": num_req,
                "maxRequestUsage": max_req,
                "startOfMonth": start_of_month,
            } if usage else "api2.cursor.sh/auth/usage unavailable",
        },
    )


def probe_antigravity() -> QuotaProbeResult:
    agentapi = HOME / ".gemini" / "antigravity" / "bin" / "agentapi"
    app = Path("/Applications/Antigravity.app")
    if not agentapi.exists() and not app.exists():
        return _unavailable("antigravity", "Antigravity not installed")
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
    summary = "FetchQuotaStatus gRPC (IDE-only; no local CLI quota)"
    status = "unavailable"
    if ls_addr:
        summary = f"language server at {ls_addr} (quota RPC not wired for headless probe)"
    return QuotaProbeResult(
        "antigravity",
        status,
        summary,
        source="Antigravity language_server PredictionService/FetchQuotaStatus",
        auth="Antigravity IDE session",
        # agentapi only has get-conversation-metadata and new-conversation subcommands;
        # antigravity_state.pbtxt has only onboarding state; no network quota endpoint found.
        refresh_hint="requires running Antigravity.app + LS address (agentapi has no quota subcommand)",
        data={
            "agentapi": str(agentapi),
            "antigravity_app": str(app),
            "ANTIGRAVITY_LS_ADDRESS": ls_addr or None,
        },
    )


def format_report(results: dict[str, QuotaProbeResult] | None = None) -> str:
    results = results or probe_all()
    lines = ["AutoCode quota probes (read-only prototype)", ""]
    for provider in PROVIDERS:
        result = results.get(provider)
        if not result:
            continue
        lines.append(f"{provider:<12} {result.status:<12} {result.summary}")
        if result.source:
            lines.append(f"             source: {result.source}")
        if result.error:
            lines.append(f"             error: {result.error}")
    lines.append("")
    lines.append("Legend: ok=remaining % available; partial=tier/subscription only; unavailable=no reliable counter")
    return "\n".join(lines)


def format_json(results: dict[str, QuotaProbeResult] | None = None) -> str:
    results = results or probe_all()
    payload = {name: asdict(result) for name, result in results.items()}
    return json.dumps(payload, indent=2, sort_keys=True)


def _codex_rate_limits(codex: str) -> dict[str, Any]:
    cmd = [codex, "app-server", "--listen", "stdio://"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdin and proc.stdout
    lines: list[str] = []

    def reader() -> None:
        for line in proc.stdout:
            line = line.strip()
            if line:
                lines.append(line)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "autocode-quota-probe", "version": "0.1"}, "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    ]
    for req in requests:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        time.sleep(1.5)
    time.sleep(1.0)
    proc.kill()
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
            return obj["result"]
    raise RuntimeError("account/rateLimits/read response missing")


def _claude_keychain_meta() -> dict[str, Any]:
    """Read non-secret metadata (tier, expiry, scopes) from the Claude Code keychain entry."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        doc = json.loads(raw)
        oauth = doc.get("claudeAiOauth") if isinstance(doc.get("claudeAiOauth"), dict) else doc
        return {
            "rateLimitTier": str(oauth.get("rateLimitTier") or ""),
            "subscriptionType": str(oauth.get("subscriptionType") or ""),
            "expiresAt": oauth.get("expiresAt"),
        }
    except Exception:
        return {}


def _grok_jwt_tier(token: str) -> str:
    """Decode tier claim from Grok JWT token payload (local, no network, no secret exposure)."""
    if not token or not isinstance(token, str):
        return ""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = base64.urlsafe_b64decode(parts[1] + "==")
        claims = json.loads(payload.decode("utf-8"))
        tier = claims.get("tier")
        if tier is not None:
            return f" · tier={tier}"
    except Exception:
        pass
    return ""


def _claude_auth_status() -> dict[str, Any]:
    proc = _run_json(["claude", "auth", "status"], timeout=12)
    return json_loads(proc.get("stdout") or "{}", {})


def _claude_oauth_get(path: str) -> dict[str, Any]:
    token = _claude_access_token()
    if not token:
        return {}
    url = "https://claude.ai" + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "autocode-quota-probe/0.1")
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json_loads(resp.read().decode("utf-8", errors="replace"), {})
    except Exception:
        return {}


def _claude_access_token() -> str:
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        doc = json.loads(raw)
        oauth = doc.get("claudeAiOauth") if isinstance(doc.get("claudeAiOauth"), dict) else doc
        return str(oauth.get("accessToken") or oauth.get("access_token") or "")
    except Exception:
        return ""


def _cursor_usage_api() -> dict[str, Any]:
    """Fetch live usage from api2.cursor.sh using the stored JWT session token."""
    token = _cursor_state_value("cursorAuth/accessToken").strip('"')
    if not token:
        return {}
    url = "https://api2.cursor.sh/auth/usage"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "cursor-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json_loads(resp.read().decode("utf-8", errors="replace"), {})
    except Exception:
        return {}


def _cursor_membership_type() -> str:
    return _cursor_state_value("cursorAuth/stripeMembershipType")


def _cursor_subscription_status() -> str:
    return _cursor_state_value("cursorAuth/stripeSubscriptionStatus")


def _cursor_state_value(key: str) -> str:
    db = HOME / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute("select value from ItemTable where key=?", (key,)).fetchone()
        con.close()
        if not row:
            return ""
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value).strip().strip('"')
    except Exception:
        return ""


def _window_summary(primary: dict[str, Any], secondary: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, window in (("primary", primary), ("secondary", secondary)):
        if not window:
            continue
        used = window.get("usedPercent")
        if used is None:
            continue
        remaining = max(0, 100 - int(used))
        mins = window.get("windowDurationMins")
        reset = _fmt_reset(window.get("resetsAt"))
        window_label = label
        if isinstance(mins, int) and mins > 0:
            if mins % (24 * 60) == 0:
                window_label = f"{mins // (24 * 60)}d"
            elif mins % 60 == 0:
                window_label = f"{mins // 60}h"
            else:
                window_label = f"{mins}m"
        chunk = f"{remaining}% left ({window_label}"
        if reset:
            chunk += f", resets {reset}"
        chunk += ")"
        parts.append(chunk)
    return " · ".join(parts) if parts else "rate limits returned without windows"


def _fmt_reset(value: Any) -> str:
    ts = parse_ts(value)
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%m-%d %H:%M")


def _run_json(cmd: list[str], timeout: int = 15) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _codex_bin() -> str:
    if command_exists("codex"):
        return shutil.which("codex") or "codex"
    app = Path("/Applications/Codex.app/Contents/Resources/codex")
    return str(app) if app.exists() else ""


def _grok_bin() -> str:
    if command_exists("grok"):
        return shutil.which("grok") or "grok"
    app = HOME / ".grok" / "bin" / "grok"
    return str(app) if app.exists() else ""


def _cursor_agent_bin() -> str:
    if command_exists("cursor-agent"):
        return shutil.which("cursor-agent") or "cursor-agent"
    app = HOME / ".local" / "bin" / "cursor-agent"
    return str(app) if app.exists() else ""


def _command_available(command: str) -> bool:
    if command_exists(command) or shutil.which(command):
        return True
    common = {
        "codex": ["/Applications/Codex.app/Contents/Resources/codex"],
        "claude": [str(HOME / ".local" / "bin" / "claude")],
        "grok": [str(HOME / ".grok" / "bin" / "grok")],
        "cursor-agent": [str(HOME / ".local" / "bin" / "cursor-agent")],
    }
    return any(Path(path).exists() for path in common.get(command, []))


def _unavailable(provider: str, reason: str) -> QuotaProbeResult:
    return QuotaProbeResult(provider, "unavailable", reason)


def main() -> None:
    import sys

    as_json = "--json" in sys.argv
    text = format_json() if as_json else format_report()
    print(text)


if __name__ == "__main__":
    main()

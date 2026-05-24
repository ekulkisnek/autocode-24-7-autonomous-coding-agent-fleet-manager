from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CURSOR_API_KEY is not configured")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("https://api.cursor.com" + path, data=payload, method=method.upper())
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{api_key}:".encode()).decode())
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "autocode/1.0")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cursor Cloud HTTP {exc.code}: {body_text[:500]}") from exc
    return json.loads(raw.decode("utf-8", errors="replace") or "{}")


def followup(agent_id: str, prompt_file: str, model: str = "auto") -> None:
    prompt = Path(prompt_file).read_text(encoding="utf-8", errors="replace")
    aid = urllib.parse.quote(agent_id, safe="")
    body: dict = {"prompt": {"text": prompt}}
    if model and model != "auto":
        body["model"] = {"id": model}
    data = request("POST", f"/v1/agents/{aid}/runs", body)
    run = data.get("run") or data
    run_id = run.get("id") or run.get("runId") or "(unknown)"
    status = run.get("status") or "(unknown)"
    print("CURSOR_CLOUD_FOLLOWUP_QUEUED")
    print(f"agent: {agent_id}")
    print(f"run: {run_id}")
    print(f"status: {status}")
    print(f"model: {model or 'auto'}")
    print(f"url: https://cursor.com/agents?id={agent_id}")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) not in {3, 4} or argv[0] != "followup":
        print("usage: python -m autocode.cursor_cloud followup <agent_id> <prompt_file> [model]", file=sys.stderr)
        return 2
    followup(argv[1], argv[2], argv[3] if len(argv) == 4 else "auto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

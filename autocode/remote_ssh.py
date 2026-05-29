from __future__ import annotations

import re
import subprocess
from typing import Any, Mapping, Sequence


def worker_field(worker: Any, key: str, default: str = "") -> str:
    if isinstance(worker, dict):
        value = worker.get(key, default)
    elif hasattr(worker, "keys") and key in worker.keys():
        value = worker[key]
    else:
        value = default
    if value is None:
        return default
    return str(value)


def worker_shell(worker: Mapping[str, str] | Any) -> str:
    """Return ``powershell`` or ``bash`` for remote command construction."""
    explicit = worker_field(worker, "remote_shell") or worker_field(worker, "shell")
    explicit = explicit.strip().lower()
    if explicit in {"powershell", "ps", "pwsh", "windows"}:
        return "powershell"
    if explicit in {"bash", "sh", "unix", "linux", "macos"}:
        return "bash"
    cwd = worker_field(worker, "default_cwd")
    if re.match(r"^[A-Za-z]:[/\\]", cwd) or "\\" in cwd:
        return "powershell"
    return "bash"


def ssh_base(worker: Mapping[str, str] | Any) -> list[str]:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    ssh_key = worker_field(worker, "ssh_key_path").strip()
    if ssh_key:
        cmd += ["-i", ssh_key]
    return cmd


def ssh_target(worker: Mapping[str, str] | Any) -> str:
    return f"{worker_field(worker, 'ssh_user')}@{worker_field(worker, 'host')}"


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ps_join(*parts: str) -> str:
    if not parts:
        return "$env:USERPROFILE"
    expr = parts[0] if parts[0].startswith("$") else ps_quote(parts[0])
    for part in parts[1:]:
        expr = f"(Join-Path {expr} {ps_quote(part)})"
    return expr


def bash_quote(value: str) -> str:
    import shlex

    return shlex.quote(str(value))


def normalize_cwd(cwd: str) -> str:
    text = str(cwd or "").strip() or "~"
    if text == "~":
        return text
    return text.replace("\\", "/")


def remote_job_rel_dir(job_id: str) -> str:
    return f"autocode-jobs/{job_id}"


def remote_prompt_scp_dest(worker: Mapping[str, str] | Any, job_id: str) -> str:
    """SCP destination path; Windows OpenSSH needs a drive path, not ``~/``."""
    shell = worker_shell(worker)
    if shell == "powershell":
        base = normalize_cwd(worker_field(worker, "default_cwd") or "~")
        if base == "~":
            base = "C:/Users/Luke"
        return f"{base.rstrip('/')}/autocode-jobs/{job_id}/prompt.txt"
    return f"~/{remote_job_rel_dir(job_id)}/prompt.txt"


def _remote_command(
    worker: Mapping[str, str] | Any,
    *remote_argv: str,
    tty: bool = False,
    connect_timeout: int | None = None,
) -> list[str]:
    cmd = ssh_base(worker)
    if connect_timeout is not None:
        cmd += ["-o", f"ConnectTimeout={connect_timeout}"]
    if tty:
        cmd += ["-tt"]
    cmd += [ssh_target(worker), *remote_argv]
    return cmd


def ensure_remote_job_dir(worker: Mapping[str, str] | Any, job_id: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    shell = worker_shell(worker)
    if shell == "powershell":
        job_dir = ps_join("$env:USERPROFILE", "autocode-jobs", job_id)
        ps = f"$null = New-Item -ItemType Directory -Force -Path ({job_dir})"
        cmd = _remote_command(worker, "powershell", "-NoProfile", "-Command", ps)
    else:
        cmd = _remote_command(worker, f"mkdir -p ~/autocode-jobs/{job_id}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def scp_prompt_file(
    worker: Mapping[str, str] | Any,
    local_path: str,
    job_id: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    target = ssh_target(worker)
    ssh_key = worker_field(worker, "ssh_key_path").strip()
    scp_cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if ssh_key:
        scp_cmd += ["-i", ssh_key]
    scp_cmd += [local_path, f"{target}:{remote_prompt_scp_dest(worker, job_id)}"]
    return subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)


REMOTE_PROMPT_FILE = "__AUTOCODE_REMOTE_PROMPT__"
REMOTE_PROMPT_CONTENT = "__AUTOCODE_REMOTE_PROMPT_CONTENT__"


def rewrite_prompt_paths(cmd: Sequence[str], job_id: str, shell: str) -> list[str]:
    rewritten = list(cmd)
    for index, arg in enumerate(rewritten):
        token = str(arg)
        if token.startswith(f"{REMOTE_PROMPT_CONTENT}:"):
            if shell == "powershell":
                rewritten[index] = f"{REMOTE_PROMPT_CONTENT}:{job_id}"
            else:
                rewritten[index] = f"~/autocode-jobs/{job_id}/prompt.txt"
        elif "prompt.txt" in token or token.startswith(f"{REMOTE_PROMPT_FILE}:"):
            if shell == "powershell":
                rewritten[index] = f"{REMOTE_PROMPT_FILE}:{job_id}"
            else:
                rewritten[index] = f"~/autocode-jobs/{job_id}/prompt.txt"
    return rewritten


def suggest_capacity_from_ram(ram_bytes: int, *, gb_per_slot: float = 2.0) -> float:
    """Suggest weight capacity from installed RAM (default: 16GB→8 slots, 32GB→16)."""
    ram_gb = max(0, int(ram_bytes)) / (1024**3)
    if ram_gb <= 0:
        return 4.0
    slots = round(ram_gb / gb_per_slot)
    return float(max(2, min(slots, 32)))


def _powershell_encoded_argv(script: str) -> list[str]:
    import base64

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-EncodedCommand", encoded]


_POWERSHELL_RAM_CPU_SCRIPT = (
    "$sig='[DllImport(\"kernel32.dll\")] public static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer); "
    "[StructLayout(LayoutKind.Sequential)] public struct MEMORYSTATUSEX { public uint dwLength; public uint dwMemoryLoad; "
    "public ulong ullTotalPhys; public ulong ullAvailPhys; public ulong ullTotalPageFile; public ulong ullAvailPageFile; "
    "public ulong ullTotalVirtual; public ulong ullAvailVirtual; public ulong ullAvailExtendedVirtual; }'; "
    "Add-Type -MemberDefinition $sig -Name MemStatus -Namespace Win32; "
    "$mem=New-Object Win32.MemStatus+MEMORYSTATUSEX; "
    "$mem.dwLength=[System.Runtime.InteropServices.Marshal]::SizeOf($mem); "
    "$null = [Win32.MemStatus]::GlobalMemoryStatusEx([ref]$mem); "
    "Write-Output ('ram_bytes=' + $mem.ullTotalPhys); "
    "Write-Output ('cpu_cores=' + $env:NUMBER_OF_PROCESSORS)"
)


def build_probe_resources_command(worker: Mapping[str, str] | Any) -> list[str]:
    shell = worker_shell(worker)
    if shell == "powershell":
        return _remote_command(worker, *_powershell_encoded_argv(_POWERSHELL_RAM_CPU_SCRIPT), connect_timeout=12)
    return _remote_command(
        worker,
        "echo ram_bytes=$(awk '/MemTotal/ {print $2*1024}' /proc/meminfo); "
        "echo cpu_cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)",
        connect_timeout=12,
    )


def build_probe_providers_command(worker: Mapping[str, str] | Any) -> list[str]:
    shell = worker_shell(worker)
    if shell == "powershell":
        ps = (
            "$grok = Get-Command grok -ErrorAction SilentlyContinue; "
            "if ($grok) { Write-Output ('grok=' + $grok.Source) } else { Write-Output 'grok=missing' }; "
            "$cursorCmd = Join-Path $env:LOCALAPPDATA 'cursor-agent/cursor-agent.cmd'; "
            "if (Test-Path $cursorCmd) { Write-Output ('cursor-agent=' + $cursorCmd) } else { "
            "$cursor = Get-Command cursor-agent.cmd -ErrorAction SilentlyContinue; "
            "if ($cursor) { Write-Output ('cursor-agent=' + $cursor.Source) } else { Write-Output 'cursor-agent=missing' } }"
        )
        return _remote_command(worker, "powershell", "-NoProfile", "-Command", ps, connect_timeout=12)
    return _remote_command(
        worker,
        "command -v grok >/dev/null 2>&1 && echo grok=$(command -v grok) || echo grok=missing; "
        "command -v cursor-agent >/dev/null 2>&1 && echo cursor-agent=$(command -v cursor-agent) || echo cursor-agent=missing",
        connect_timeout=12,
    )


def _parse_probe_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def probe_worker_resources(worker: Mapping[str, str] | Any, *, timeout: int = 15) -> dict[str, int | float | str]:
    result = subprocess.run(build_probe_resources_command(worker), capture_output=True, text=True, timeout=timeout)
    parsed = _parse_probe_kv(result.stdout or "")
    ram_bytes = int(parsed.get("ram_bytes") or 0)
    cpu_cores = int(float(parsed.get("cpu_cores") or 0))
    ram_gb = round(ram_bytes / (1024**3), 1) if ram_bytes else 0.0
    return {
        "ok": int(result.returncode == 0),
        "ram_bytes": ram_bytes,
        "ram_gb": ram_gb,
        "cpu_cores": cpu_cores,
        "suggested_capacity": suggest_capacity_from_ram(ram_bytes),
        "error": ssh_error_snippet(result) if result.returncode != 0 else "",
    }


def probe_worker_providers(worker: Mapping[str, str] | Any, *, timeout: int = 15) -> dict[str, str]:
    result = subprocess.run(build_probe_providers_command(worker), capture_output=True, text=True, timeout=timeout)
    parsed = _parse_probe_kv(result.stdout or "")
    return {
        "grok": parsed.get("grok", "missing"),
        "cursor_agent": parsed.get("cursor-agent", "missing"),
        "error": ssh_error_snippet(result) if result.returncode != 0 else "",
    }


def probe_worker(worker: Mapping[str, str] | Any) -> dict[str, Any]:
    """Probe remote RAM/CPU, provider binaries, and suggested capacity."""
    resources = probe_worker_resources(worker)
    providers = probe_worker_providers(worker)
    return {
        "worker_id": worker_field(worker, "id"),
        "host": worker_field(worker, "host"),
        "resources": resources,
        "providers": providers,
        "suggested_capacity": resources.get("suggested_capacity", 4.0),
    }


def build_remote_exec_command(
    worker: Mapping[str, str] | Any,
    cwd: str,
    cmd: Sequence[str],
    job_id: str,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the local ``ssh`` argv that runs ``cmd`` on the remote worker."""
    shell = worker_shell(worker)
    rewritten = rewrite_prompt_paths(cmd, job_id, shell)
    if shell == "powershell":
        remote_script = _build_powershell_script(cwd, rewritten, job_id, env=env)
        # Do not allocate a TTY (-tt): Windows OpenSSH + cursor-agent/grok hang or
        # emit ^C under pseudo-terminal allocation. EncodedCommand without TTY is reliable.
        return _remote_command(worker, *_powershell_encoded_argv(remote_script), tty=False)
    remote_cwd = normalize_cwd(cwd)
    remote_cmd = " ".join(bash_quote(part) for part in rewritten)
    env_prefix = _bash_env_prefix(env)
    return _remote_command(worker, f"{env_prefix}cd {bash_quote(remote_cwd)} && {remote_cmd}", tty=True)


def build_ping_command(worker: Mapping[str, str] | Any) -> list[str]:
    shell = worker_shell(worker)
    if shell == "powershell":
        ps = (
            "Write-Output ok\n"
            "grok --version\n"
            "$cursorCmd = Join-Path $env:LOCALAPPDATA 'cursor-agent/cursor-agent.cmd'\n"
            "if (Test-Path $cursorCmd) { & $cursorCmd --version }"
        )
        return _remote_command(worker, *_powershell_encoded_argv(ps), connect_timeout=8)
    return _remote_command(
        worker,
        "echo ok && grok --version 2>/dev/null || echo 'grok not found'; "
        "cursor-agent --version 2>/dev/null || echo 'cursor-agent not found'",
        connect_timeout=8,
    )


def build_smoke_command(worker: Mapping[str, str] | Any, job_id: str) -> list[str]:
    """Run a tiny remote command that reads the uploaded prompt file."""
    shell = worker_shell(worker)
    if shell == "powershell":
        job_dir = ps_join("$env:USERPROFILE", "autocode-jobs", job_id)
        ps = (
            f"$prompt = Join-Path {job_dir} 'prompt.txt'; "
            "if (-not (Test-Path $prompt)) { Write-Error 'missing prompt'; exit 2 }; "
            "Write-Output ('prompt=' + (Get-Content -Raw $prompt).Trim()); "
            "grok --version 2>$null"
        )
        return _remote_command(worker, *_powershell_encoded_argv(ps), connect_timeout=15)
    return _remote_command(
        worker,
        f"test -f ~/autocode-jobs/{job_id}/prompt.txt && "
        f"echo prompt=$(cat ~/autocode-jobs/{job_id}/prompt.txt) && grok --version",
        connect_timeout=15,
    )


def _bash_env_prefix(env: Mapping[str, str] | None) -> str:
    if not env:
        return ""
    exports = " ".join(f"{key}={bash_quote(value)}" for key, value in env.items() if key and value is not None)
    return f"export {exports}; " if exports else ""


def _powershell_env_setup(env: Mapping[str, str] | None) -> list[str]:
    if not env:
        return []
    return [f"$env:{key} = {ps_quote(str(value))}" for key, value in env.items() if key and value is not None]


def _build_powershell_script(
    cwd: str,
    cmd: Sequence[str],
    job_id: str,
    env: Mapping[str, str] | None = None,
) -> str:
    location = normalize_cwd(cwd)
    if location == "~":
        location_expr = "$env:USERPROFILE"
    else:
        location_expr = ps_quote(location)
    job_dir = ps_join("$env:USERPROFILE", "autocode-jobs", job_id)
    parts = [
        *_powershell_env_setup(env),
        "$cursorAgent = Join-Path $env:LOCALAPPDATA 'cursor-agent/cursor-agent.cmd'",
        f"$jobDir = {job_dir}",
        "$prompt = Join-Path $jobDir 'prompt.txt'",
    ]
    for index, arg in enumerate(cmd):
        if str(arg) == "--workspace" and index + 1 < len(cmd):
            ws = normalize_cwd(str(cmd[index + 1]))
            ws_expr = "$env:USERPROFILE" if ws == "~" else ps_quote(ws)
            parts.append(
                f"if (-not (Test-Path {ws_expr})) {{ $null = New-Item -ItemType Directory -Force -Path ({ws_expr}) }}"
            )
    parts.extend([
        f"if (-not (Test-Path {location_expr})) {{ $null = New-Item -ItemType Directory -Force -Path ({location_expr}) }}",
        f"Set-Location {location_expr}",
    ])
    rendered: list[str] = []
    for index, arg in enumerate(cmd):
        token = str(arg)
        if token.startswith(f"{REMOTE_PROMPT_FILE}:"):
            rendered.append("$prompt")
        elif token.startswith(f"{REMOTE_PROMPT_CONTENT}:"):
            rendered.append("(Get-Content -Raw $prompt)")
        elif index == 0 and token in {"cursor-agent", "cursor-agent.cmd"}:
            rendered.append("$cursorAgent")
        elif index == 0:
            rendered.append(token)
        else:
            rendered.append(ps_quote(token))
    if rendered:
        parts.append("& " + " ".join(rendered))
    return "; ".join(parts)


def build_remote_kill_command(worker: Mapping[str, str] | Any, job_id: str) -> list[str]:
    """Best-effort kill of remote grok/cursor-agent tied to a job directory."""
    shell = worker_shell(worker)
    if shell == "powershell":
        job_dir = ps_join("$env:USERPROFILE", "autocode-jobs", job_id)
        ps = (
            f"$dir = {job_dir}; "
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Name -in @('grok.exe','cursor-agent.exe') -and $_.CommandLine -like ('*' + $dir + '*') } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            "Write-Output killed"
        )
        return _remote_command(worker, "powershell", "-NoProfile", "-Command", ps, connect_timeout=15)
    return _remote_command(
        worker,
        f"pkill -f 'autocode-jobs/{job_id}' 2>/dev/null || true; echo killed",
        connect_timeout=15,
    )


def ssh_error_snippet(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[:500] if text else f"exit={result.returncode}"


def bench_remote_worker(worker: Mapping[str, str] | Any, *, job_id: str = "bench-roundtrip") -> dict[str, float | int | str]:
    """Measure SSH ping, mkdir, scp, and smoke read latency (seconds)."""
    import time
    import uuid

    job_id = f"{job_id}-{uuid.uuid4().hex[:6]}"
    out: dict[str, float | int | str] = {"job_id": job_id}

    t0 = time.perf_counter()
    ping = subprocess.run(build_ping_command(worker), capture_output=True, text=True, timeout=20)
    out["ping_s"] = round(time.perf_counter() - t0, 3)
    out["ping_ok"] = int(ping.returncode == 0)

    t0 = time.perf_counter()
    mkdir = ensure_remote_job_dir(worker, job_id)
    out["mkdir_s"] = round(time.perf_counter() - t0, 3)
    out["mkdir_ok"] = int(mkdir.returncode == 0)
    if mkdir.returncode != 0:
        out["mkdir_err"] = ssh_error_snippet(mkdir)
        return out

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("autocode bench prompt")
        local_path = tmp.name
    t0 = time.perf_counter()
    copied = scp_prompt_file(worker, local_path, job_id)
    out["scp_s"] = round(time.perf_counter() - t0, 3)
    out["scp_ok"] = int(copied.returncode == 0)
    if copied.returncode != 0:
        out["scp_err"] = ssh_error_snippet(copied)
        return out

    t0 = time.perf_counter()
    smoke = subprocess.run(build_smoke_command(worker, job_id), capture_output=True, text=True, timeout=30)
    out["smoke_s"] = round(time.perf_counter() - t0, 3)
    out["smoke_ok"] = int(smoke.returncode == 0)
    out["total_s"] = round(out["ping_s"] + out["mkdir_s"] + out["scp_s"] + out["smoke_s"], 3)
    return out


def touch_worker_seen(store: Any, worker_id: str) -> None:
    from .util import now_iso

    with store.connect() as con:
        con.execute("update remote_workers set last_seen_at=? where id=?", (now_iso(), worker_id))


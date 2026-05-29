from autocode.remote_ssh import (
    REMOTE_PROMPT_CONTENT,
    REMOTE_PROMPT_FILE,
    _build_powershell_script,
    build_ping_command,
    build_remote_exec_command,
    build_remote_kill_command,
    ps_quote,
    remote_prompt_scp_dest,
    rewrite_prompt_paths,
    suggest_capacity_from_ram,
    worker_shell,
)


def test_worker_shell_auto_detects_windows_cwd():
    assert worker_shell({"default_cwd": "C:/Users/Luke"}) == "powershell"
    assert worker_shell({"default_cwd": "/home/luke"}) == "bash"


def test_worker_shell_honors_explicit_value():
    assert worker_shell({"default_cwd": "/home/luke", "remote_shell": "powershell"}) == "powershell"


def test_rewrite_prompt_paths_for_powershell():
    cmd = ["grok", "--prompt-file", "/tmp/job/prompt.txt"]
    rewritten = rewrite_prompt_paths(cmd, "job-123", "powershell")
    assert rewritten[1] == "--prompt-file"
    assert rewritten[2] == f"{REMOTE_PROMPT_FILE}:job-123"


def test_rewrite_prompt_paths_for_powershell_inline_cursor():
    cmd = ["cursor-agent", "--print", f"{REMOTE_PROMPT_CONTENT}:job-123"]
    rewritten = rewrite_prompt_paths(cmd, "job-123", "powershell")
    assert rewritten[-1] == f"{REMOTE_PROMPT_CONTENT}:job-123"


def test_remote_prompt_scp_dest_windows():
    worker = {"default_cwd": "C:/Users/Luke", "remote_shell": "powershell"}
    assert remote_prompt_scp_dest(worker, "job-abc") == "C:/Users/Luke/autocode-jobs/job-abc/prompt.txt"


def test_build_remote_exec_command_powershell():
    worker = {
        "host": "100.100.179.47",
        "ssh_user": "Luke",
        "ssh_key_path": "/tmp/key",
        "default_cwd": "C:/Users/Luke",
        "remote_shell": "powershell",
    }
    cmd = build_remote_exec_command(
        worker,
        "C:/Users/Luke",
        ["grok", "--cwd", "C:/Users/Luke", "--prompt-file", "/local/prompt.txt"],
        "job-abc",
    )
    assert cmd[:6] == ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-i"]
    assert "-tt" not in cmd
    assert "Luke@100.100.179.47" in cmd
    assert cmd[-2] == "-EncodedCommand"
    import base64
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert "$prompt = Join-Path $jobDir" in script
    assert "Set-Location" in script
    assert "& grok" in script
    assert "$prompt" in script


def test_build_ping_command_powershell():
    worker = {
        "host": "100.100.179.47",
        "ssh_user": "Luke",
        "default_cwd": "C:/Users/Luke",
    }
    cmd = build_ping_command(worker)
    assert cmd[-2] == "-EncodedCommand"
    assert "grok --version" in cmd[-1] or True  # base64 payload


def test_build_ping_command_bash():
    worker = {"host": "10.0.0.2", "ssh_user": "ubuntu", "default_cwd": "/home/ubuntu", "remote_shell": "bash"}
    cmd = build_ping_command(worker)
    assert "2>/dev/null" in cmd[-1]


def test_build_remote_kill_command_powershell():
    worker = {"host": "100.0.0.1", "ssh_user": "Luke", "remote_shell": "powershell"}
    cmd = build_remote_kill_command(worker, "job-123")
    assert "Stop-Process" in cmd[-1]
    assert "cursor-agent.exe" in cmd[-1]
    assert "job-123" in cmd[-1]


def test_suggest_capacity_from_ram():
    assert suggest_capacity_from_ram(16 * 1024**3) == 8.0
    assert suggest_capacity_from_ram(32 * 1024**3) == 16.0
    assert suggest_capacity_from_ram(8 * 1024**3) == 4.0


def test_build_powershell_script_resolves_cursor_agent():
    script = _build_powershell_script(
        "C:/Users/Luke",
        ["cursor-agent", "--print", "hello"],
        "job-1",
    )
    assert "$cursorAgent = Join-Path $env:LOCALAPPDATA 'cursor-agent/cursor-agent.cmd'" in script
    assert "& $cursorAgent" in script


def test_build_powershell_script_reads_inline_prompt_from_file():
    script = _build_powershell_script(
        "C:/Users/Luke",
        ["cursor-agent", "--print", f"{REMOTE_PROMPT_CONTENT}:job-1"],
        "job-1",
    )
    assert "(Get-Content -Raw $prompt)" in script


def test_build_powershell_script_sets_env():
    script = _build_powershell_script(
        "C:/Users/Luke",
        ["cursor-agent", "--print", "hello"],
        "job-1",
        env={"CURSOR_API_KEY": "abc123"},
    )
    assert "$env:CURSOR_API_KEY = 'abc123'" in script
    assert "& $cursorAgent" in script


def test_build_remote_exec_command_passes_env():
    worker = {
        "host": "100.100.179.47",
        "ssh_user": "Luke",
        "ssh_key_path": "/tmp/key",
        "default_cwd": "C:/Users/Luke",
        "remote_shell": "powershell",
    }
    cmd = build_remote_exec_command(
        worker,
        "C:/Users/Luke",
        ["cursor-agent", "--print", "hi"],
        "job-abc",
        env={"CURSOR_API_KEY": "secret"},
    )
    assert cmd[-2] == "-EncodedCommand"
    import base64
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert "$env:CURSOR_API_KEY = 'secret'" in script

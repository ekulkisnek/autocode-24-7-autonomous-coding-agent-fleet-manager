from __future__ import annotations

import os
import plistlib
import subprocess

from .config import LABEL, LOGS, PLIST, ROOT


def plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/opt/homebrew/bin/python3",
            "-m",
            "autocode.cli",
            "daemon",
            "run",
            "--interval",
            "2",
        ],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PYTHONPATH": str(ROOT),
            "AUTOCODE_HOME": str(ROOT),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/lukekensik/.grok/bin:/Users/lukekensik/.local/bin:/Applications/Codex.app/Contents/Resources",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOGS / "launchd.out.log"),
        "StandardErrorPath": str(LOGS / "launchd.err.log"),
    }


def install() -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump(plist(), f)


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def start() -> tuple[int, str, str]:
    install()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True, text=True)
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], capture_output=True, text=True)
    if p.returncode != 0 and "already bootstrapped" not in (p.stderr or ""):
        return p.returncode, p.stdout, p.stderr
    k = subprocess.run(["launchctl", "kickstart", "-k", _target()], capture_output=True, text=True)
    return k.returncode, k.stdout, k.stderr


def stop() -> tuple[int, str, str]:
    p = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def status() -> tuple[bool, str]:
    p = subprocess.run(["launchctl", "print", _target()], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout or p.stderr)

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from sqlite3 import Row

from .config import DB
from .util import disk_free_gb, load1, memory_free_percent


@dataclass(frozen=True)
class ProcessResource:
    pid: int
    ppid: int
    cpu_percent: float
    rss_kb: int
    command: str


@dataclass(frozen=True)
class JobResource:
    cpu_percent: float
    rss_mb: float
    process_count: int
    sample: str = ""


@dataclass(frozen=True)
class SystemResource:
    load1: float
    cpu_percent: float | None
    cpu_count: int
    mem_free_percent: int | None
    disk_free_gb: float | None


def system_resource() -> SystemResource:
    rows = process_table()
    cpu_count = os.cpu_count() or 1
    cpu_percent = None
    if rows:
        # macOS ps reports CPU as a percentage of one core. Normalize to total host capacity.
        cpu_percent = min(100.0, sum(row.cpu_percent for row in rows) / cpu_count)
    return SystemResource(
        load1=load1(),
        cpu_percent=cpu_percent,
        cpu_count=cpu_count,
        mem_free_percent=memory_free_percent(),
        disk_free_gb=disk_free_gb(DB.parent),
    )


def job_resource(job: Row) -> JobResource:
    try:
        root_pid = int(job["pid"] or 0)
    except Exception:
        root_pid = 0
    if root_pid <= 0:
        return JobResource(0.0, 0.0, 0)
    return job_resource_for_pid(root_pid)


def job_resource_for_pid(root_pid: int) -> JobResource:
    rows = process_table()
    if not rows:
        return JobResource(0.0, 0.0, 0)
    by_parent: dict[int, list[ProcessResource]] = {}
    by_pid: dict[int, ProcessResource] = {}
    for row in rows:
        by_pid[row.pid] = row
        by_parent.setdefault(row.ppid, []).append(row)
    seen: set[int] = set()
    stack = [root_pid]
    tree: list[ProcessResource] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = by_pid.get(pid)
        if proc:
            tree.append(proc)
        stack.extend(child.pid for child in by_parent.get(pid, []))
    if not tree:
        return JobResource(0.0, 0.0, 0)
    sample = _sample_process(tree)
    return JobResource(
        cpu_percent=sum(row.cpu_percent for row in tree),
        rss_mb=sum(row.rss_kb for row in tree) / 1024,
        process_count=len(tree),
        sample=sample,
    )


def process_table() -> list[ProcessResource]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,%cpu=,rss=,command="],
            text=True,
            capture_output=True,
            timeout=4,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return parse_ps_output(result.stdout)


def parse_ps_output(text: str) -> list[ProcessResource]:
    rows: list[ProcessResource] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(\d+)\s+(\d+)\s+([0-9.]+)\s+(\d+)\s*(.*)$", stripped)
        if not match:
            continue
        pid, ppid, cpu, rss, command = match.groups()
        rows.append(
            ProcessResource(
                pid=int(pid),
                ppid=int(ppid),
                cpu_percent=float(cpu),
                rss_kb=int(rss),
                command=command.strip(),
            )
        )
    return rows


def format_system_resource(resource: SystemResource) -> str:
    cpu = f"cpu~{resource.cpu_percent:.0f}%" if resource.cpu_percent is not None else "cpu~?"
    mem = f"mem={resource.mem_free_percent}% free" if resource.mem_free_percent is not None else "mem=unknown"
    disk = f"disk={resource.disk_free_gb:.1f}GiB free" if resource.disk_free_gb is not None else "disk=unknown"
    return f"{cpu} ({resource.cpu_count} cores) | load1={resource.load1:.2f} | {mem} | {disk}"


def format_job_resource(resource: JobResource) -> str:
    if resource.process_count <= 0:
        return "cpu~0%, ram~0MB, procs=0"
    return f"cpu~{resource.cpu_percent:.0f}%, ram~{resource.rss_mb:.0f}MB, procs={resource.process_count}"


def _sample_process(rows: list[ProcessResource]) -> str:
    busiest = sorted(rows, key=lambda row: (row.cpu_percent, row.rss_kb), reverse=True)
    for row in busiest:
        command = row.command
        if command:
            return command[:120]
    return ""

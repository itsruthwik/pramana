"""Run one Vivado batch process, with a timeout and a free-memory gate."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_launch_lock = threading.Lock()
# Reservations are shared by every pramana process on the machine (file lock + json list of
# [launch time, reserved GB]), so concurrent campaigns don't each see the same free memory.
_SHARED = Path(os.environ.get("PRAMANA_LAUNCH_FILE", "/tmp/pramana_launches.json"))
_RECENT_S = 300


@dataclass
class VivadoResult:
    exit_code: int
    stdout: str
    wall_s: float
    timed_out: bool


def free_mem_gb() -> float:
    for ln in Path("/proc/meminfo").read_text().splitlines():
        if ln.startswith("MemAvailable:"):
            return int(ln.split()[1]) / 1e6
    return 0.0


def wait_for_memory(need_gb: float, poll_s: float = 20.0) -> None:
    """Block until at least need_gb of memory is available. The box runs earlyoom, which
    SIGTERMs the largest process when available memory drops below ~10%, so a run must only
    start when there's room for its own growth on top of a safety margin."""
    while free_mem_gb() < need_gb:
        time.sleep(poll_s)


def _read_shared() -> list[tuple[float, float]]:
    try:
        return [tuple(x) for x in json.loads(_SHARED.read_text())]
    except Exception:
        return []


def run_vivado(script: Path, cwd: Path, settings: str, timeout_s: float,
               need_gb: float = 4.0) -> VivadoResult:
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    script = Path(script).resolve()
    cmd = (f"source {settings} >/dev/null 2>&1 && exec vivado -mode batch -notrace "
           f"-source {script} -log {cwd/'vivado.log'} -journal {cwd/'vivado.jou'}")
    # Launches are serialised; jobs launched in the last few minutes haven't reached their
    # peak yet, so their growth is reserved on top of this job's need.
    with _launch_lock, open(str(_SHARED) + ".lock", "a+") as lk:
        while True:
            fcntl.flock(lk, fcntl.LOCK_EX)
            now = time.time()
            recent = [(t, g) for t, g in _read_shared() if now - t < _RECENT_S]
            if free_mem_gb() >= need_gb + 0.5 * sum(g for _, g in recent):
                break
            fcntl.flock(lk, fcntl.LOCK_UN)
            time.sleep(20)
        try:
            recent.append((time.time(), need_gb))
            _SHARED.write_text(json.dumps(recent))
            proc = subprocess.Popen(["bash", "-c", cmd], cwd=str(cwd), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
            time.sleep(5)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    t0 = time.time()
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        out, _ = proc.communicate()
    return VivadoResult(proc.returncode if proc.returncode is not None else -9,
                        out or "", time.time() - t0, timed_out)


def est_mem_gb(dcp_mb: float) -> float:
    """Rough peak memory for one run, from DCP size (measured on this corpus: ~2 GB floor)."""
    return 2.5 + 0.09 * dcp_mb

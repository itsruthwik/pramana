"""Run one Vivado batch process, with a timeout and a free-memory gate."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_launch_lock = threading.Lock()


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
    """Block until at least need_gb of memory is available (other users share this box)."""
    while free_mem_gb() < need_gb:
        time.sleep(poll_s)


def run_vivado(script: Path, cwd: Path, settings: str, timeout_s: float,
               need_gb: float = 4.0) -> VivadoResult:
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    script = Path(script).resolve()
    cmd = (f"source {settings} >/dev/null 2>&1 && exec vivado -mode batch -notrace "
           f"-source {script} -log {cwd/'vivado.log'} -journal {cwd/'vivado.jou'}")
    # Launches are serialised and spaced so memory readings reflect the previous launch.
    with _launch_lock:
        wait_for_memory(need_gb)
        proc = subprocess.Popen(["bash", "-c", cmd], cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        time.sleep(5)
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

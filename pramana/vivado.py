"""Run one Vivado batch process, with a timeout and a free-memory gate."""
from __future__ import annotations

import contextlib
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
# Machine-wide cap on concurrent Vivado runs from all pramana processes (training,
# evaluations, discovery trials): a run must hold one of MAX_VIVADO lock-file slots.
MAX_VIVADO = int(os.environ.get("PRAMANA_MAX_VIVADO", "4"))
_SLOT_DIR = Path(os.environ.get("PRAMANA_SLOT_DIR", "/tmp/pramana_vivado_slots"))


# Extra Vivado slots on another node: PRAMANA_REMOTE="<slurm jobid>:<slots>[,<jobid>:<slots>]".
# A run in a remote slot is started inside that (already running) Slurm job with
# `srun --jobid ... --overlap`; files are on the shared filesystem, so nothing else changes.
# The job only has to hold the node (slurm/worker_node.sbatch).
def _remote_pools() -> list[tuple[str, int]]:
    out = []
    for part in os.environ.get("PRAMANA_REMOTE", "").split(","):
        if ":" in part:
            j, n = part.split(":", 1)
            out.append((j.strip(), int(n)))
    return out


def _try_lock(path: Path):
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _acquire_slot(remote_ok: bool = False):
    """Block until a slot is free; returns (open locked slot file, remote job id or None).
    Local slots first; remote slots (PRAMANA_REMOTE) only for callers that can run there."""
    _SLOT_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(MAX_VIVADO):
            f = _try_lock(_SLOT_DIR / f"slot{i}")
            if f:
                return f, None
        if remote_ok:
            for job, n in _remote_pools():
                for i in range(n):
                    f = _try_lock(_SLOT_DIR / f"remote_{job}_{i}")
                    if f:
                        return f, job
        time.sleep(10)


@contextlib.contextmanager
def vivado_slot():
    """Hold one machine-wide Vivado slot for the duration of the block. For callers that
    launch Vivado indirectly (e.g. an external agent such as maya) and so cannot go
    through run_vivado."""
    slot, _ = _acquire_slot()           # local only: the caller starts Vivado itself
    try:
        yield
    finally:
        fcntl.flock(slot, fcntl.LOCK_UN)
        slot.close()


@dataclass
class VivadoResult:
    exit_code: int
    stdout: str
    wall_s: float
    timed_out: bool


def _cgroup_free_gb() -> float | None:
    """Headroom under this process's memory cgroup (e.g. a Slurm job's mem= limit), or None
    when there is no finite limit. cgroup v1 (memory controller line) and v2 ("0::") paths."""
    try:
        for ln in Path("/proc/self/cgroup").read_text().splitlines():
            _, ctrl, rel = ln.split(":", 2)
            if ctrl == "memory":                      # v1: walk up to the first finite limit
                p = Path("/sys/fs/cgroup/memory") / rel.lstrip("/")
                while True:
                    lim = p / "memory.limit_in_bytes"
                    if lim.exists():
                        limit = int(lim.read_text())
                        if limit < 1 << 60:
                            used = int((p / "memory.usage_in_bytes").read_text())
                            return (limit - used) / 1e9
                    if p == Path("/sys/fs/cgroup/memory") or p == p.parent:
                        return None
                    p = p.parent
            if ctrl == "" and Path("/sys/fs/cgroup/cgroup.controllers").exists():   # v2
                p = Path("/sys/fs/cgroup") / rel.lstrip("/")
                mx = (p / "memory.max").read_text().strip()
                if mx == "max":
                    return None
                return (int(mx) - int((p / "memory.current").read_text())) / 1e9
    except (OSError, ValueError):
        pass
    return None


def free_mem_gb() -> float:
    host = 0.0
    for ln in Path("/proc/meminfo").read_text().splitlines():
        if ln.startswith("MemAvailable:"):
            host = int(ln.split()[1]) / 1e6
            break
    cg = _cgroup_free_gb()
    return host if cg is None else min(host, cg)


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
    slot, job = _acquire_slot(remote_ok=True)
    try:
        if job:
            return _run_remote(job, cmd, cwd, timeout_s)
        return _run_in_slot(cmd, cwd, timeout_s, need_gb)
    finally:
        fcntl.flock(slot, fcntl.LOCK_UN)
        slot.close()


def _run_remote(job: str, cmd: str, cwd: Path, timeout_s: float) -> VivadoResult:
    """Run `cmd` inside Slurm job `job` (another node). The step gets the job's memory
    (--mem=0); its threads come from the script's maxThreads. On timeout srun is sent SIGTERM,
    which it forwards to the remote step, then SIGKILL."""
    threads = os.environ.get("PRAMANA_REMOTE_CPUS", "4")
    argv = ["srun", f"--jobid={job}", "--overlap", "--nodes=1", "--ntasks=1",
            f"--cpus-per-task={threads}", "--mem=0", f"--chdir={cwd}", "--export=ALL",
            "--quiet", "/bin/bash", "-c", cmd]
    proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    t0 = time.time()
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            out, _ = proc.communicate(timeout=60)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            out, _ = proc.communicate()
    return VivadoResult(proc.returncode if proc.returncode is not None else -9,
                        out or "", time.time() - t0, timed_out)


def _run_in_slot(cmd: str, cwd: Path, timeout_s: float, need_gb: float) -> VivadoResult:
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


def reap_under(root: Path, log=None) -> list[int]:
    """Kill processes whose working directory is under `root` (the leftovers of a finished agent
    run). The agents' MCP servers start Vivado through a pty (its own session), so it survives
    the agent's exit and spins at 100% CPU forever; cwd is the one reliable link back to the run."""
    root = str(Path(root).resolve())
    victims = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(p / "cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + "/"):
            victims.append(int(p.name))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(3)
    if victims and log:
        log(f"reaped {len(victims)} leftover process(es) under {root}: {victims}")
    return victims

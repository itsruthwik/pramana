"""Regular (interactive) Claude Code sessions, driven through tmux.

Every LLM step in the training loop is a normal `claude` session, not `claude -p`:
  - it runs in a detached tmux session named pr-<campaign>-<stage>-<name>; attach with
    `tmux attach -t <name>` to watch it or talk to it;
  - it is saved like any session (`claude --resume <id>` opens it later);
  - a session key (e.g. one per design for the analyst) maps to a fixed session id, so the
    next round resumes the same conversation instead of starting over.

Protocol: the task is written to a file in the session's working directory; the session is
started (or resumed) with a one-line instruction to read it. The answer is a file the
session writes. pramana waits until the file exists and validates, then closes the session.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .config import Config

TRUST_PROMPT = "Is this a project you created or one you trust"
READ_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
AGENT_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
_ids_lock = threading.Lock()


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def _alive(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def _pane(name: str) -> str:
    return _tmux("capture-pane", "-p", "-t", name).stdout


def transcript(sid: str) -> Path | None:
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def usage(sid: str) -> dict:
    """Token totals from the session transcript (interactive sessions report no cost)."""
    t = transcript(sid)
    tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0, "assistant_turns": 0}
    if not t:
        return tot
    for ln in t.read_text(errors="replace").splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        u = (e.get("message") or {}).get("usage") if e.get("type") == "assistant" else None
        if u:
            tot["assistant_turns"] += 1
            for k in tot:
                if k in u:
                    tot[k] += u[k] or 0
    return tot


class SessionStore:
    """session key -> session id, per campaign (so rounds resume the same session)."""

    def __init__(self, cfg: Config):
        self.path = cfg.root / "llm" / "sessions.json"

    def get(self, key: str) -> str | None:
        with _ids_lock:
            return json.loads(self.path.read_text()).get(key) if self.path.exists() else None

    def put(self, key: str, sid: str) -> None:
        with _ids_lock:
            d = json.loads(self.path.read_text()) if self.path.exists() else {}
            d[key] = sid
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(d, indent=1))


def run(cfg: Config, stage: str, name: str, task: str, cwd: Path, done: Callable[[], str | None],
        key: str | None = None, tools: list[str] = READ_TOOLS, system: str | None = None,
        timeout_s: float = 3600, idle_nudge_s: float = 300, max_nudges: int = 3,
        task_file: str = "task.md") -> dict:
    """Run one turn of a session and wait for its deliverable.

    done() returns None when the deliverable is ready and valid, or a message describing
    what is wrong/missing (sent to the session if it goes idle without finishing).
    Returns a record with session id, status, duration and token usage.
    """
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / task_file).write_text(task)
    store = SessionStore(cfg)
    sid = store.get(key) if key else None
    resume = sid is not None and transcript(sid) is not None
    if not resume:
        sid = str(uuid.uuid4())
        if key:
            store.put(key, sid)
    tname = re.sub(r"[^A-Za-z0-9_-]", "_", f"pr-{cfg.name}-{stage}-{name}")[:80]
    if _alive(tname):
        _tmux("kill-session", "-t", tname)
    cmd = ["claude", "--model", cfg.llm_model, "--effort", cfg.llm_effort,
           "--allowedTools", *tools, "-n", tname]
    cmd += ["--resume", sid] if resume else ["--session-id", sid]
    if system and not resume:
        cmd += ["--append-system-prompt", system]
    cmd.append(f"Read {task_file} in the current directory and follow its instructions.")
    shell = " ".join(_q(x) for x in cmd)
    _tmux("new-session", "-d", "-s", tname, "-x", "220", "-y", "50", "-c", str(cwd), shell,
          check=True)
    t0 = last_change = time.time()
    last_size, nudges, status, why = -1, 0, "timeout", None
    while time.time() - t0 < timeout_s:
        time.sleep(5)
        if not _alive(tname):
            why = done()
            status = "ok" if why is None else "exited"
            break
        if TRUST_PROMPT in _pane(tname):          # first session in a new directory
            _tmux("send-keys", "-t", tname, "Down")
            time.sleep(0.5)
            _tmux("send-keys", "-t", tname, "Enter")
            continue
        why = done()
        if why is None:
            status = "ok"
            time.sleep(5)                          # let the session finish its turn
            break
        t = transcript(sid)
        size = t.stat().st_size if t else 0
        if size != last_size:
            last_size, last_change = size, time.time()
        elif time.time() - last_change > idle_nudge_s:
            if nudges >= max_nudges:
                status = "stalled"
                break
            nudges += 1
            _tmux("send-keys", "-t", tname, "-l",
                  f"Your deliverable is not complete yet: {why} Please finish it now.")
            _tmux("send-keys", "-t", tname, "Enter")
            last_change = time.time()
    _close(tname)
    rec = {"stage": stage, "name": name, "session_id": sid, "tmux": tname, "resumed": resume,
           "status": status, "problem": why, "nudges": nudges, "model": cfg.llm_model,
           "effort": cfg.llm_effort, "duration_s": round(time.time() - t0, 1),
           "usage": usage(sid), "cwd": str(cwd)}
    (cwd / f"{Path(task_file).stem}.session.json").write_text(json.dumps(rec, indent=1))
    return rec


def _close(tname: str) -> None:
    if not _alive(tname):
        return
    _tmux("send-keys", "-t", tname, "Escape")
    _tmux("send-keys", "-t", tname, "-l", "/exit")
    _tmux("send-keys", "-t", tname, "Enter")
    for _ in range(10):
        time.sleep(1)
        if not _alive(tname):
            return
    _tmux("kill-session", "-t", tname)


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"

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
import os
import re
import signal
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


CODEX = "codex:"          # model prefix that selects Codex, e.g. "codex:gpt-6.1-terra"
CODEX_BIN = str(Path.home() / ".local" / "bin" / "codex")


def transcript(sid: str) -> Path | None:
    """The saved session file: ~/.claude/projects/*/<id>.jsonl, or for Codex ids (stored with
    the "codex:" prefix) ~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl."""
    if sid.startswith(CODEX):
        hits = list((Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{sid[len(CODEX):]}.jsonl"))
    else:
        hits = list((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "assistant_turns")


def usage(sid: str) -> dict:
    """Token totals over the WHOLE session transcript, all turns so far (interactive sessions
    report no cost). run() subtracts the totals from before its turn, so each call's record
    holds only that call's tokens."""
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
        task_file: str = "task.md", model: str | None = None) -> dict:
    """Run one turn of a session and wait for its deliverable.

    done() returns None when the deliverable is ready and valid, or a message describing
    what is wrong/missing (sent to the session if it goes idle without finishing).
    Returns a record with session id, status, duration and token usage.
    """
    model = model or cfg.llm_model
    if model.startswith(CODEX):
        return run_codex(cfg, stage, name, task, cwd, done, key=key, system=system,
                         timeout_s=timeout_s, max_nudges=max_nudges, task_file=task_file,
                         model=model)
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / task_file).write_text(task)
    store = SessionStore(cfg)
    sid = store.get(key) if key else None
    resume = sid is not None and transcript(sid) is not None
    before = usage(sid) if resume else {k: 0 for k in USAGE_KEYS}
    if not resume:
        sid = str(uuid.uuid4())
        if key:
            store.put(key, sid)
    tname = re.sub(r"[^A-Za-z0-9_-]", "_", f"pr-{cfg.name}-{stage}-{name}")[:80]
    if _alive(tname):
        _tmux("kill-session", "-t", tname)
    cmd = ["claude", "--model", model, "--effort", cfg.llm_effort,
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
           "status": status, "problem": why, "nudges": nudges, "model": model,
           "effort": cfg.llm_effort, "duration_s": round(time.time() - t0, 1),
           "usage": {k: v - before.get(k, 0) for k, v in usage(sid).items()},
           "usage_per_call": True, "cwd": str(cwd)}
    (cwd / f"{Path(task_file).stem}.session.json").write_text(json.dumps(rec, indent=1))
    return rec


def run_codex(cfg: Config, stage: str, name: str, task: str, cwd: Path,
              done: Callable[[], str | None], key: str | None = None,
              system: str | None = None, timeout_s: float = 3600, max_nudges: int = 3,
              task_file: str = "task.md", model: str = "") -> dict:
    """Same protocol as run() with `codex exec`, run inside a detached tmux session named like
    the Claude ones (watch it with `tmux attach -t <tmux>`; headless Codex takes no input).
    Its JSON events are tee'd to <task>.codex.jsonl, which pramana reads. Codex has no tool
    allow-list or system-prompt flag: the system text goes at the top of the task, and the
    sandbox is cfg.codex_sandbox (Codex's own sandbox fails inside Slurm jobs, so the default
    is danger-full-access). If Codex exits without a valid deliverable, the same thread is
    resumed with what is wrong, up to max_nudges times. Usage is per call from the
    `turn.completed` events of this call's output."""
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    store = SessionStore(cfg)
    sid = store.get(key) if key else None
    resume = sid is not None and sid.startswith(CODEX) and transcript(sid) is not None
    if not resume:
        sid = None
        if system:
            task = f"{system}\n\n{task}"
    (cwd / task_file).write_text(task)
    log = cwd / f"{Path(task_file).stem}.codex.jsonl"
    log.write_text("")
    tname = re.sub(r"[^A-Za-z0-9_-]", "_", f"pr-{cfg.name}-{stage}-{name}")[:80]
    t0 = time.time()

    def call(prompt: str) -> bool:
        """One codex exec (resuming `sid` if set); True if it hit the time limit."""
        nonlocal sid
        args = ["resume", sid[len(CODEX):]] if sid else []
        cmd = [CODEX_BIN, "exec", *args, "--json", "--skip-git-repo-check",
               "-m", model[len(CODEX):], "-c", f"model_reasoning_effort={cfg.llm_effort}",
               "-c", f'sandbox_mode="{cfg.codex_sandbox}"', prompt]   # resume has no -s
        start = log.stat().st_size
        if _alive(tname):
            _tmux("kill-session", "-t", tname)
        shell = (" ".join(_q(x) for x in cmd) + " < /dev/null 2>&1 | tee -a " + _q(str(log)))
        _tmux("new-session", "-d", "-s", tname, "-x", "220", "-y", "50", "-c", str(cwd), shell,
              check=True)
        pid = _tmux("display-message", "-p", "-t", tname, "#{pane_pid}").stdout.strip()
        timed_out = True
        while time.time() - t0 < timeout_s:
            time.sleep(2)
            if not _alive(tname):
                timed_out = False
                break
        if timed_out and pid.isdigit():
            # the pane's shell leads one process group (codex, tee and their children)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(int(pid), sig)
                except ProcessLookupError:
                    break
                time.sleep(30 if sig == signal.SIGTERM else 0)
        if _alive(tname):
            _tmux("kill-session", "-t", tname)
        if sid is None:
            with open(log) as f:
                f.seek(start)
                for ln in f:
                    try:
                        tid = json.loads(ln).get("thread_id")
                    except json.JSONDecodeError:
                        continue
                    if tid:
                        sid = CODEX + tid
                        if key:
                            store.put(key, sid)
                        break
        return timed_out

    timed_out = call(f"Read {task_file} in the current directory and follow its instructions.")
    why = done()
    nudges = 0
    while why and not timed_out and sid and nudges < max_nudges and time.time() - t0 < timeout_s - 60:
        nudges += 1
        timed_out = call(f"Your deliverable is not complete yet: {why} Please finish it now.")
        why = done()
    status = "ok" if why is None else ("timeout" if timed_out else "exited")
    rec = {"stage": stage, "name": name, "session_id": sid, "tmux": tname, "resumed": resume,
           "status": status, "problem": why, "nudges": nudges, "model": model,
           "effort": cfg.llm_effort, "duration_s": round(time.time() - t0, 1),
           "usage": codex_usage(log), "usage_per_call": True, "cwd": str(cwd)}
    (cwd / f"{Path(task_file).stem}.session.json").write_text(json.dumps(rec, indent=1))
    return rec


def run_print(cfg: Config, stage: str, name: str, task: str, cwd: Path, schema: dict,
              key: str | None = None, system: str | None = None, timeout_s: float = 1800,
              model: str | None = None) -> dict:
    """One structured answer from `claude -p` (print mode), run inside tmux like the others:
    no tools, `system` as the whole system prompt, --json-schema for the answer, resumes the
    session of `key`. Returns a record with `structured_output` (None on failure), per-call
    usage and Claude's own cost figure."""
    model = model or cfg.llm_model
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    stem = Path(name).name
    (cwd / f"{stem}_task.md").write_text(task)
    out = cwd / f"{stem}.print.json"
    out.unlink(missing_ok=True)
    store = SessionStore(cfg)
    sid = store.get(key) if key else None
    resume = sid is not None and not sid.startswith(CODEX) and transcript(sid) is not None
    if not resume:
        sid = str(uuid.uuid4())
        if key:
            store.put(key, sid)
    cmd = ["claude", "-p", "--model", model, "--effort", cfg.llm_effort, "--tools", "",
           "--strict-mcp-config", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--system-prompt", system or "You are a careful expert. Answer only from the given material."]
    cmd += ["--resume", sid] if resume else ["--session-id", sid]
    tname = re.sub(r"[^A-Za-z0-9_-]", "_", f"pr-{cfg.name}-{stage}-{name}")[:80]
    if _alive(tname):
        _tmux("kill-session", "-t", tname)
    shell = " ".join(_q(x) for x in cmd) + f" < {_q(stem + '_task.md')} > {_q(out.name)} 2>&1"
    _tmux("new-session", "-d", "-s", tname, "-c", str(cwd), shell, check=True)
    t0 = time.time()
    while _alive(tname) and time.time() - t0 < timeout_s:
        time.sleep(2)
    status = "ok"
    if _alive(tname):
        _tmux("kill-session", "-t", tname)
        status = "timeout"
    res = {}
    try:
        res = json.loads(out.read_text())
    except (OSError, json.JSONDecodeError):
        status = status if status != "ok" else "bad_output"
    u = res.get("usage") or {}
    so = res.get("structured_output")
    if status == "ok" and (res.get("is_error") or so is None):
        status = "error"
    return {"stage": stage, "name": name, "session_id": sid, "tmux": tname, "resumed": resume,
            "status": status, "problem": None if status == "ok" else (res.get("result") or "")[:300],
            "nudges": 0, "model": model, "effort": cfg.llm_effort, "mode": "print",
            "duration_s": round(time.time() - t0, 1),
            "usage": {"input_tokens": u.get("input_tokens") or 0,
                      "output_tokens": u.get("output_tokens") or 0,
                      "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
                      "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
                      "assistant_turns": res.get("num_turns") or 0},
            # total_cost_usd is the whole session's so far (cumulative on resume), so it is kept
            # for reference only; spend() prices this call from its usage
            "usage_per_call": True, "session_cost_usd": res.get("total_cost_usd"),
            "structured_output": so, "cwd": str(cwd)}


def codex_usage(log: Path) -> dict:
    """Token totals from `codex exec --json` output, in the same keys as usage(). Codex
    reports input including cached; cached input is counted as a cache read."""
    tot = {k: 0 for k in USAGE_KEYS}
    for ln in log.read_text(errors="replace").splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "turn.completed":
            u = e.get("usage") or {}
            cached = u.get("cached_input_tokens") or 0
            tot["assistant_turns"] += 1
            tot["input_tokens"] += (u.get("input_tokens") or 0) - cached
            tot["cache_read_input_tokens"] += cached
            tot["output_tokens"] += u.get("output_tokens") or 0
    return tot


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

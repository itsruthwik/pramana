"""LLM calls through Claude Code headless (`claude -p`), on the user's subscription.

Two modes:
  ask()   - one structured answer (JSON schema enforced), no tools. Analysts, critic, writer.
  agent() - a Claude Code agent with tools in a working directory. Discoverers.
Every call is logged under <campaign>/llm/<stage>/ with prompt, response, model and cost.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import Config


def _log_dir(cfg: Config, stage: str) -> Path:
    d = cfg.root / "llm" / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


def ask(cfg: Config, stage: str, name: str, prompt: str, schema: dict,
        timeout_s: float = 1800, retries: int = 2, system: str | None = None) -> dict | None:
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    base.with_suffix(".prompt.md").write_text(prompt)
    cmd = ["claude", "-p", "--model", cfg.llm_model, "--effort", cfg.llm_effort,
           "--output-format", "json", "--json-schema", json.dumps(schema),
           "--tools", "", "--no-session-persistence"]
    if system:
        cmd += ["--append-system-prompt", system]
    last_err = None
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=timeout_s, cwd=str(d))
            out = json.loads(p.stdout) if p.stdout.strip() else {}
            so = out.get("structured_output")
            rec = {"stage": stage, "name": name, "model": cfg.llm_model, "effort": cfg.llm_effort,
                   "attempt": attempt, "is_error": out.get("is_error"),
                   "cost_usd": out.get("total_cost_usd"), "duration_ms": out.get("duration_ms"),
                   "usage": out.get("usage"), "structured_output": so,
                   "result_text": out.get("result") if so is None else None,
                   "stderr": p.stderr[-2000:] if p.returncode else None}
            base.with_suffix(".response.json").write_text(json.dumps(rec, indent=1))
            if so is not None:
                return so
            last_err = p.stderr[-500:] or out.get("result")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last_err = repr(e)
        time.sleep(5 * (attempt + 1))
    base.with_suffix(".failed.txt").write_text(str(last_err))
    return None


def agent(cfg: Config, stage: str, name: str, prompt: str, cwd: Path,
          timeout_s: float = 4 * 3600, max_turns: int = 200) -> dict:
    """Run a Claude Code agent with tools (Bash, Read, Write, Edit) in cwd."""
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    base.with_suffix(".prompt.md").write_text(prompt)
    cmd = ["claude", "-p", "--model", cfg.llm_model, "--effort", cfg.llm_effort,
           "--output-format", "json", "--dangerously-skip-permissions",
           "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
           "--max-turns", str(max_turns), "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout_s, cwd=str(cwd))
        out = json.loads(p.stdout) if p.stdout.strip() else {"stderr": p.stderr[-2000:]}
    except subprocess.TimeoutExpired as e:
        out = {"is_error": True, "result": f"timeout: {e}"}
    except json.JSONDecodeError:
        out = {"is_error": True, "result": "unparseable output"}
    rec = {"stage": stage, "name": name, "model": cfg.llm_model, "cost_usd": out.get("total_cost_usd"),
           "num_turns": out.get("num_turns"), "is_error": out.get("is_error"),
           "result": out.get("result")}
    base.with_suffix(".response.json").write_text(json.dumps(rec, indent=1))
    return rec


def spend(cfg: Config) -> dict:
    """Total logged LLM cost and calls per stage."""
    out: dict = {}
    for f in (cfg.root / "llm").glob("*/*.response.json"):
        r = json.loads(f.read_text())
        s = out.setdefault(r["stage"], {"calls": 0, "cost_usd": 0.0})
        s["calls"] += 1
        s["cost_usd"] += r.get("cost_usd") or 0.0
    return out

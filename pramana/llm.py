"""LLM calls as regular Claude Code sessions (see session.py), on the user's subscription.

Two modes:
  ask()   - one structured answer, written to a file and checked against a JSON schema;
            file tools only. Analysts, report writer, note writer.
  agent() - a session with a shell, working in a directory until it writes a deliverable.
            Discoverers.
Every call is logged under <campaign>/llm/<stage>/ (prompt, answer, session id, tokens);
the sessions themselves are under <campaign>/llm/<stage>/sessions/ and in ~/.claude.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import jsonschema

from . import session
from .config import Config


def _log_dir(cfg: Config, stage: str) -> Path:
    d = cfg.root / "llm" / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


def ask(cfg: Config, stage: str, name: str, prompt: str, schema: dict,
        timeout_s: float = 1800, retries: int = 1, system: str | None = None,
        key: str | None = None) -> dict | None:
    """One structured answer from a regular session (no shell). With `key`, later calls
    with the same key resume the same session (e.g. one analyst per design)."""
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    base.with_suffix(".prompt.md").write_text(prompt)
    cwd = d / "sessions" / (key or name)
    ans = cwd / f"{name}_answer.json"
    task = (prompt + "\n\n## Output\nWrite your answer as one JSON object to the file "
            f"`{ans.name}` in the current directory. It must validate against this JSON "
            f"schema:\n```json\n{json.dumps(schema, indent=1)}\n```\nWrite only that file. "
            "Then reply with the single word DONE.")

    def done() -> str | None:
        if not ans.exists():
            return f"the answer file {ans.name} has not been written."
        try:
            obj = json.loads(ans.read_text())
            jsonschema.validate(obj, schema)
        except json.JSONDecodeError as e:
            return f"{ans.name} is not valid JSON ({e})."
        except jsonschema.ValidationError as e:
            return f"{ans.name} does not match the schema: {e.message[:300]}"
        return None

    rec, so = {}, None
    for attempt in range(retries + 1):
        if ans.exists():
            ans.unlink()
        rec = session.run(cfg, stage, name, task, cwd, done, key=key, system=system,
                          tools=session.READ_TOOLS, timeout_s=timeout_s,
                          task_file=f"{name}_task.md")
        rec["attempt"] = attempt
        if rec["status"] == "ok":
            so = json.loads(ans.read_text())
            break
    rec["structured_output"] = so
    base.with_suffix(".response.json").write_text(json.dumps(rec, indent=1))
    if so is None:
        base.with_suffix(".failed.txt").write_text(f"{rec.get('status')}: {rec.get('problem')}")
    return so


def agent(cfg: Config, stage: str, name: str, prompt: str, cwd: Path,
          timeout_s: float = 4 * 3600, deliverable: str = "candidates.json",
          key: str | None = None) -> dict:
    """A regular session with tools (Bash, Read, Write, Edit) working in cwd until it has
    written `deliverable`. Discoverers."""
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    base.with_suffix(".prompt.md").write_text(prompt)
    out = Path(cwd) / deliverable

    def done() -> str | None:
        if not out.exists():
            return f"{deliverable} has not been written yet."
        try:
            json.loads(out.read_text())
        except json.JSONDecodeError as e:
            return f"{deliverable} is not valid JSON ({e})."
        return None

    rec = session.run(cfg, stage, name, prompt, cwd, done, key=key or f"{stage}_{name}",
                      tools=session.AGENT_TOOLS, timeout_s=timeout_s, idle_nudge_s=900,
                      task_file="task.md")
    rec["num_turns"] = rec["usage"]["assistant_turns"]
    rec["is_error"] = rec["status"] != "ok"
    base.with_suffix(".response.json").write_text(json.dumps(rec, indent=1))
    return rec


def spend(cfg: Config) -> dict:
    """Total logged LLM cost and calls per stage."""
    out: dict = {}
    for f in (cfg.root / "llm").glob("*/*.response.json"):
        r = json.loads(f.read_text())
        s = out.setdefault(r["stage"], {"calls": 0, "cost_usd": 0.0, "output_tokens": 0})
        s["calls"] += 1
        s["cost_usd"] += r.get("cost_usd") or 0.0        # headless-era records only
        s["output_tokens"] += (r.get("usage") or {}).get("output_tokens") or 0
    return out

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


def _sfx(base: Path, suffix: str) -> Path:
    """base + suffix. Not Path.with_suffix: design ids contain dots (diffeq2_2025.2, proxy.3),
    and with_suffix would cut the name at the last dot, so every call for a design would
    overwrite one file."""
    return base.parent / (base.name + suffix)


def ask(cfg: Config, stage: str, name: str, prompt: str, schema: dict,
        timeout_s: float = 1800, retries: int = 1, system: str | None = None,
        key: str | None = None, model: str | None = None) -> dict | None:
    """One structured answer from a regular session (no shell). With `key`, later calls
    with the same key resume the same session (e.g. one analyst per design)."""
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    _sfx(base, ".prompt.md").write_text(prompt)
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
                          task_file=f"{name}_task.md", model=model)
        rec["attempt"] = attempt
        if rec["status"] == "ok":
            so = json.loads(ans.read_text())
            break
    rec["structured_output"] = so
    _sfx(base, ".response.json").write_text(json.dumps(rec, indent=1))
    if so is None:
        _sfx(base, ".failed.txt").write_text(f"{rec.get('status')}: {rec.get('problem')}")
    return so


def agent(cfg: Config, stage: str, name: str, prompt: str, cwd: Path,
          timeout_s: float = 4 * 3600, deliverable: str = "candidates.json",
          key: str | None = None) -> dict:
    """A regular session with tools (Bash, Read, Write, Edit) working in cwd until it has
    written `deliverable`. Discoverers."""
    d = _log_dir(cfg, stage)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = d / f"{name}_{ts}"
    _sfx(base, ".prompt.md").write_text(prompt)
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
    _sfx(base, ".response.json").write_text(json.dumps(rec, indent=1))
    return rec


# API list prices, $ per million tokens: (input, output, cache read, cache write).
# Cache writes assume the 5-minute cache (1.25x input). Sessions run on the subscription, so
# this is what the same tokens would cost on the API, not a bill.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50),
          "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "assistant_turns")


def _call_usage(recs: list[dict]) -> None:
    """Records from before per-call usage hold the session's running total; turn them into
    per-call numbers (difference from the previous call in the same session)."""
    by: dict = {}
    for r in recs:
        if not r.get("usage_per_call"):
            by.setdefault(r.get("session_id"), []).append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r["_ts"])
        prev = {k: 0 for k in TOKEN_KEYS}
        for r in rs:
            u = r.get("usage") or {}
            r["usage"] = {k: max(0, (u.get(k) or 0) - prev[k]) for k in TOKEN_KEYS}
            prev = {k: u.get(k) or 0 for k in TOKEN_KEYS}


def spend(cfg: Config) -> dict:
    """LLM calls, tokens and estimated API-price cost per stage, plus a total."""
    recs = []
    for f in (cfg.root / "llm").glob("*/*.response.json"):
        r = json.loads(f.read_text())
        r["_ts"] = f.name.rsplit("_", 1)[-1]          # name_<YYYYmmdd-HHMMSS>.response.json
        recs.append(r)
    _call_usage(recs)
    out: dict = {}
    for r in recs:
        for st in (r["stage"], "TOTAL"):
            s = out.setdefault(st, {"calls": 0, **{k: 0 for k in TOKEN_KEYS}, "est_usd": 0.0})
            s["calls"] += 1
            u = r.get("usage") or {}
            for k in TOKEN_KEYS:
                s[k] += u.get(k) or 0
            p = PRICES.get(r.get("model") or cfg.llm_model)
            if p:
                s["est_usd"] += ((u.get("input_tokens") or 0) * p[0]
                                 + (u.get("output_tokens") or 0) * p[1]
                                 + (u.get("cache_read_input_tokens") or 0) * p[2]
                                 + (u.get("cache_creation_input_tokens") or 0) * p[3]) / 1e6
            s["est_usd"] += r.get("cost_usd") or 0.0     # headless-era records only
    for s in out.values():
        s["est_usd"] = round(s["est_usd"], 2)
    return out


def spend_line(cfg: Config) -> str:
    """One log line: total and per-stage estimated cost."""
    sp = spend(cfg)
    if not sp:
        return "[llm] no LLM calls yet"
    t = sp.pop("TOTAL")
    per = ", ".join(f"{k} ${v['est_usd']} ({v['calls']})" for k, v in sorted(sp.items()))
    return (f"[llm] spend so far: ${t['est_usd']} est. at API prices, {t['calls']} calls, "
            f"{t['output_tokens']} out / {t['cache_read_input_tokens']} cache-read / "
            f"{t['cache_creation_input_tokens']} cache-write tokens; {per}")

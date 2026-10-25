"""Digest Vivado logs into per-step evidence: pass tables, WNS trajectory, key messages.

The pruner (LLM) reads these digests instead of raw logs: Explore runs every internal pass and
these tables show what each pass did on this design (e.g. phys_opt 'DSP Register  0 cells' ->
that option has nothing to work on). Mid-run signals for the KB are mined from the same data.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_STEP = re.compile(r"^STEP_BEGIN (\S+)\s*$", re.M)
_WNS = re.compile(r"WNS=(-?\d+\.\d+)")
_MSG = re.compile(r"^(INFO|WARNING|CRITICAL WARNING|ERROR): \[(\w+ \d+-\d+)\] (.*)$", re.M)
# messages worth keeping verbatim (a few each) for the LLM
_KEEP_IDS = re.compile(r"^(Physopt 32-(?:668|669|952|710|715|81|232|29|117|135|775)|"
                       r"Route 35-(?:416|57|327|449|61|62|20|86)|Place 30-(?:746|8xx|612|1906)|"
                       r"Opt 31-(?:389|49|138|10|1566|1287))")


def split_steps(log: str) -> dict[str, str]:
    marks = list(_STEP.finditer(log))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(log)
        out[m.group(1)] = log[m.end():end]
    return out


def parse_tables(text: str) -> list[dict]:
    """Every ASCII summary table: header row + data rows, as list of dicts."""
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|") and ("Optimization" in ln or "Phase" in ln) and ln.count("|") >= 4:
            header = [h.strip() for h in ln.strip().strip("|").split("|")]
            rows = []
            j = i + 1
            while j < len(lines) and (lines[j].startswith("|") or lines[j].startswith("-")
                                      or lines[j].startswith("+")):
                if lines[j].startswith("|"):
                    cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    if len(cells) == len(header):
                        rows.append(dict(zip(header, cells)))
                j += 1
            if rows:
                tables.append({"header": header, "rows": rows})
            i = j
        else:
            i += 1
    return tables


def digest_step(text: str) -> dict:
    tables = parse_tables(text)
    wns = [float(x) for x in _WNS.findall(text)]
    msgs = _MSG.findall(text)
    counts = Counter(f"{sev}:{mid}" for sev, mid, _ in msgs)
    kept, seen = [], Counter()
    for sev, mid, body in msgs:
        if sev in ("ERROR", "CRITICAL WARNING") or _KEEP_IDS.match(mid):
            seen[mid] += 1
            if seen[mid] <= 3:
                kept.append(f"{sev} [{mid}] {body[:220]}")
    return {"tables": tables, "wns_trajectory": wns[:3] + (["..."] if len(wns) > 6 else []) + wns[-3:]
            if len(wns) > 6 else wns,
            "message_counts": dict(counts.most_common(15)), "key_messages": kept[:40]}


def digest_run(run_dir: Path) -> dict[str, dict]:
    log = (Path(run_dir) / "vivado.log")
    if not log.exists():
        return {}
    return {name: digest_step(t) for name, t in split_steps(log.read_text(errors="replace")).items()}


def _fmt_table(t: dict, nonzero_only: bool = True) -> list[str]:
    out = []
    hdr = t["header"]
    numeric_cols = [h for h in hdr[1:] if h not in ("Elapsed", "Iterations", "Dont Touch")]
    out.append("  | " + " | ".join(hdr))
    hidden = 0
    for r in t["rows"]:
        vals = [r.get(h, "") for h in numeric_cols]
        if nonzero_only and all(re.fullmatch(r"-?0(\.0+)?", v or "0") for v in vals) \
                and r.get(hdr[0]) != "Total":
            hidden += 1
            continue
        out.append("  | " + " | ".join(r.get(h, "") for h in hdr))
    if hidden:
        out.append(f"  ({hidden} rows with all-zero effect hidden: "
                   + ", ".join(r[hdr[0]] for r in t["rows"]
                               if all(re.fullmatch(r"-?0(\.0+)?", r.get(h, "0") or "0")
                                      for h in numeric_cols) and r[hdr[0]] != "Total")[:300] + ")")
    return out


def digest_text(run_dir: Path, max_chars: int = 6000) -> str:
    """Compact human/LLM-readable digest of one run's log."""
    parts = []
    for name, d in digest_run(run_dir).items():
        parts.append(f"[step {name}] WNS trajectory: {d['wns_trajectory']}")
        for t in d["tables"]:
            parts += _fmt_table(t)
        for m in d["key_messages"][:12]:
            parts.append("  " + m)
    s = "\n".join(parts)
    return s[:max_chars] + ("\n...(truncated)" if len(s) > max_chars else "")


def pass_effects(run_dir: Path) -> dict[str, dict]:
    """{pass name: {cells/nets optimised, wns gain}} summed over a run (for mid-run signals)."""
    out: dict[str, dict] = {}
    for d in digest_run(run_dir).values():
        for t in d["tables"]:
            key = t["header"][0]
            if key != "Optimization":
                continue
            for r in t["rows"]:
                n = r.get(key)
                if not n or n == "Total":
                    continue
                e = out.setdefault(n, {"optimized": 0, "wns_gain": 0.0})
                try:
                    e["optimized"] += int(r.get("Optimized Cells/Nets", "0") or 0)
                except ValueError:
                    pass
                try:
                    e["wns_gain"] += float(r.get("WNS Gain (ns)", "0") or 0)
                except ValueError:
                    pass
    return out

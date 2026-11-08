#!/usr/bin/env python3
"""Status of the test-set evaluation jobs on Sol: one row per sbatch job, with what the agent is
doing now. Writes campaigns/test14/EVAL_STATUS.md (and prints its path).

  python3 scripts/eval_jobs_status.py              # once
  python3 scripts/eval_jobs_status.py --every 300  # refresh every 5 min
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
C = REPO / "campaigns" / "test14"
JOBS = REPO / "slurm" / "eval_official_jobs.txt"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def slurm_state() -> dict:
    """job id -> (state, node, elapsed, time left) for running/queued jobs, plus sacct for ended."""
    out = {}
    for ln in sh("squeue -h -u $USER -o '%i|%T|%N|%M|%L|%r'").splitlines():
        j, st, node, el, left, reason = ln.split("|")
        out[j] = {"state": st, "node": node or reason, "elapsed": el, "left": left}
    for ln in sh("sacct -h -X -u $USER -S $(date -d '-2 days' +%Y-%m-%d) -o JobID,State,NodeList,Elapsed -P").splitlines():
        j, st, node, el = (ln.split("|") + ["", "", "", ""])[:4]
        if j not in out:
            out[j] = {"state": st, "node": node, "elapsed": el, "left": ""}
    return out


def agent_activity(run_dir: Path) -> str:
    """Last meaningful line of the agent log: LLM call count and last tool."""
    log = run_dir.parent / "agent.log" if run_dir.name == "run" else run_dir / "agent.log"
    if not log.exists():
        return ""
    txt = log.read_text(errors="replace").splitlines()
    calls = sum(1 for l in txt if "LLM API call #" in l)
    best = next((m.group(1) for l in reversed(txt)
                 if (m := re.search(r"New best WNS[^:]*: (-?[\d.]+)", l))), None)
    last_tool = next((m.group(1) for l in reversed(txt) if (m := re.search(r"Calling (\w+)", l))), "")
    flags = []
    if any("step limit" in l for l in txt):
        flags.append("step limit hit")
    if any("TIME BUDGET" in l or "time budget" in l for l in txt):
        flags.append("budget reached")
    s = f"{calls} LLM calls; last tool `{last_tool}`"
    if best:
        s += f"; agent's best WNS {best}"
    if flags:
        s += "; " + ", ".join(flags)
    return s


def write() -> Path:
    sl = slurm_state()
    rows = []
    for ln in JOBS.read_text().splitlines() if JOBS.exists() else []:
        parts = ln.split()
        if len(parts) < 3:
            continue
        j, kb, d = parts[:3]
        out = C / "official_eval" / f"{d}.{kb}.flash"
        s = sl.get(j, {"state": "?", "node": "", "elapsed": "", "left": ""})
        res = out / "result.json"
        if res.exists():
            r = json.loads(res.read_text())
            per = None
            for c in (r.get("clocks") or []):
                per = c.get("period")
            note = (f"**{'valid' if r.get('valid') else 'INVALID'}**, ΔWNS {r.get('d_wns')} ns "
                    f"({r.get('input_wns')} → {r.get('final_wns')}, from {r.get('final_from')}), "
                    f"{r.get('llm_calls')} LLM calls, ${(r.get('cost_usd') or 0):.3f}")
        else:
            note = agent_activity(out) if out.exists() else ""
        rows.append((d, kb, j, s["state"], s["node"], s["elapsed"], s["left"], note))
    n = {k: sum(1 for r in rows if r[3] == k) for k in ("RUNNING", "PENDING", "COMPLETED")}
    done = sum(1 for r in rows if r[7].startswith("**"))
    L = [f"# test14 evaluation status", "",
         f"Snapshot {datetime.now():%Y-%m-%d %H:%M:%S}. Refresh: `python3 scripts/eval_jobs_status.py`.", "",
         f"**Official agent: {done}/{len(rows)} results; jobs running {n['RUNNING']}, queued {n['PENDING']}.**", "",
         "| design | KB | job | state | node | elapsed | left | result / what it is doing |",
         "|--|--|--|--|--|--|--|--|"]
    L += [f"| {d} | {kb} | {j} | {st} | {node} | {el} | {left} | {note} |"
          for d, kb, j, st, node, el, left, note in sorted(rows)]
    # preparation and LLM-free evaluator jobs
    L += ["", "## Other jobs", "", "| job | name | state | node | elapsed | left |", "|--|--|--|--|--|--|"]
    names = {}
    for ln in sh("squeue -h -u $USER -o '%i|%j'").splitlines() + \
            sh("sacct -h -X -u $USER -S $(date -d '-2 days' +%Y-%m-%d) -o JobID,JobName%40 -P").splitlines():
        j, name = (ln.split("|") + [""])[:2]
        names.setdefault(j, name.strip())
    for j, name in sorted(names.items()):
        if name.startswith(("pramana-prep", "pramana-mithya")):
            s = sl.get(j, {})
            L.append(f"| {j} | {name} | {s.get('state', '')} | {s.get('node', '')} | "
                     f"{s.get('elapsed', '')} | {s.get('left', '')} |")
    log = C / "pramana.log"
    if log.exists():
        L += ["", "## test14 pramana.log (last 8 lines)", "", "```"] + \
             [l[:180] for l in log.read_text().splitlines()[-8:]] + ["```"]
    p = C / "EVAL_STATUS.md"
    p.write_text("\n".join(L) + "\n")
    return p


if __name__ == "__main__":
    every = int(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 0
    while True:
        try:
            p = write()
            if not every:
                print(p)
        except Exception as e:
            print(f"status error: {e!r}", flush=True)
        if not every:
            break
        time.sleep(every)

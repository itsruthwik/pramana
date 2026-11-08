#!/usr/bin/env python3
"""Write a markdown snapshot of what is running for a campaign's agent evaluation.

  python3 scripts/eval_status.py [campaign]   -> campaigns/<campaign>/STATUS.md (default full1)
"""
import json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
camp = sys.argv[1] if len(sys.argv) > 1 else "full1"
C = REPO / "campaigns" / camp
AGENTS = ("official", "maya", "pact")
DESIGNS = ("amd_mini-isp", "vexriscv_re-place", "amd_mini-isp_v2", "vexriscv_re-place_v2",
           "corescore_500_mod", "vtr_mcml_v2", "vtr_mcml", "ispd16_example2")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def gb(n):
    return f"{int(n) / 1024**3:.0f}"


def job_info():
    jid = os.environ.get("SLURM_JOB_ID") or sh("squeue -h -u $USER -o %i | head -1")
    rows = []
    if jid:
        q = sh(f"squeue -h -j {jid} -o '%i|%P|%N|%C|%m|%L'").split("|")
        if len(q) == 6:
            rows += [("Slurm job", f"{q[0]} ({q[1]} partition, node {q[2]})"),
                     ("CPUs", q[3]), ("Time left", q[5])]
        d = Path(f"/sys/fs/cgroup/memory/slurm/uid_{os.getuid()}/job_{jid}")
        if d.exists():
            stat = dict(l.split() for l in (d / "memory.stat").read_text().splitlines())
            rows += [("RAM limit", gb((d / "memory.limit_in_bytes").read_text()) + " GB"),
                     ("RAM used (processes)", gb(stat.get("total_rss", 0)) + " GB"),
                     ("RAM peak", gb((d / "memory.max_usage_in_bytes").read_text()) + " GB")]
    load = os.getloadavg()
    rows += [("Load average (1/5/15 min)", f"{load[0]:.1f} / {load[1]:.1f} / {load[2]:.1f}"),
             ("Vivado processes", sh("pgrep -u $USER -c -x vivado || pgrep -u $USER -c vivado") or "0")]
    return rows


def starts():
    """{(agent, design, kbtag): start time} from the run_eval*.log queue logs."""
    out = {}
    for log in C.glob("run_eval*.log"):
        if ".failed_" in log.name:
            continue
        tag = "hand" if "hand" in log.name else "pramana"
        day = datetime.fromtimestamp(log.stat().st_ctime).date()
        for line in log.read_text(errors="replace").splitlines():
            m = re.match(r"(\d\d:\d\d:\d\d) (start|end) (\w+) (?:(hand|pramana) )?(\S+)", line)
            if m:
                t = datetime.combine(day, datetime.strptime(m[1], "%H:%M:%S").time())
                out.setdefault((m[3], m[5], m[4] or tag), {})[m[2]] = t
    return out


def cell(agent, design, tag, st):
    d = C / f"{agent}_eval" / f"{design}.{tag}.flash"
    r = d / "result.json"
    s = st.get((agent, design, tag), {})
    if r.exists():
        j = json.loads(r.read_text())
        T, i, o = j.get("clock_period_ns"), j.get("input_wns"), j.get("final_wns")
        fm = (f"{1000 / (T - i):.0f} → {1000 / (T - o):.0f} MHz"
              if T and i is not None and o is not None else "")
        dw = f"{j['d_wns']:+.3f}" if j.get("d_wns") is not None else "–"
        ok = "valid" if j.get("valid") else "**invalid**"
        took = f"{(s['end'] - s['start']).seconds // 60} min" if "end" in s and "start" in s else ""
        return ("done", took, f"{dw} ns, {fm}, {ok}")
    if d.exists() and "start" in s:
        mins = int((datetime.now() - s["start"]).total_seconds() // 60)
        logs = [p for p in d.rglob("*.log") if p.is_file()]
        quiet = int(time.time() - max(p.stat().st_mtime for p in logs)) if logs else None
        note = f"log updated {quiet}s ago" if quiet is not None else ""
        if quiet is not None and quiet > 900:
            note = f"**no log activity for {quiet // 60} min**"
        return ("running", f"{mins} min", note)
    if d.exists():
        return ("started?", "", "directory exists, no start in queue log")
    return ("queued", "", "")


st = starts()
L = [f"# {camp} evaluation status", "",
     f"Snapshot at {datetime.now():%Y-%m-%d %H:%M:%S} on {os.uname().nodename}. "
     f"Regenerate with `python3 scripts/eval_status.py {camp}`.", "",
     "## System", "", "| | |", "|--|--|"]
L += [f"| {k} | {v} |" for k, v in job_info()]

counts = {}
for tag, title in (("hand", "Own knowledge base (`--kb hand`), deepseek-v4-flash"),
                   ("pramana", "Trained full1 knowledge base (`--kb pramana`), deepseek-v4-flash")):
    rows = [(a, dz, *cell(a, dz, tag, st)) for a in AGENTS for dz in DESIGNS]
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
    L += ["", f"## {title}", "", "| agent | design | status | time | result / note |", "|--|--|--|--|--|"]
    L += [f"| {a} | {dz} | {s} | {t} | {n} |" for a, dz, s, t, n in rows]

L[4:4] = ["**" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + "**", ""]
procs = sh("ps -u $USER -o etime=,args= | grep -E 'pramana\\.(official|maya|pact)_eval' | grep -v grep")
L += ["", "## Driver processes", "", "```", "\n".join(l.strip() for l in procs.splitlines()) or "(none)", "```", ""]
(C / "STATUS.md").write_text("\n".join(L))
print(C / "STATUS.md")

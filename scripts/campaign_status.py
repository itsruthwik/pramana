#!/usr/bin/env python3
"""Write campaigns/<campaign>/PROGRESS.md: what a pramana training campaign has done and what is
running now. Read-only; safe to run while the campaign runs.

  python3 scripts/campaign_status.py train31            # once
  python3 scripts/campaign_status.py train31 --every 300  # refresh every 5 min until killed
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from pramana import store                     # noqa: E402
from pramana.config import Config             # noqa: E402

STAGES = ["space", "profile", "probe", "baseline", "prune", "search", "discover", "confirm",
          "distill", "compile"]
N_BASELINE = 5


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def system() -> list[tuple[str, str]]:
    rows = []
    jid = os.environ.get("SLURM_JOB_ID") or sh("squeue -h -u $USER -w $(hostname) -o %i | head -1")
    if jid:
        q = sh(f"squeue -h -j {jid} -o '%i|%P|%N|%C|%m|%L'").split("|")
        if len(q) == 6:
            rows += [("Slurm job", f"{q[0]} ({q[1]}, node {q[2]})"), ("CPUs", q[3]), ("Job time left", q[5])]
        d = Path(f"/sys/fs/cgroup/memory/slurm/uid_{os.getuid()}/job_{jid}")
        if d.exists():
            st = dict(l.split() for l in (d / "memory.stat").read_text().splitlines())
            rows += [("RAM limit", f"{int((d / 'memory.limit_in_bytes').read_text()) / 1e9:.0f} GB"),
                     ("RAM used by processes", f"{int(st.get('total_rss', 0)) / 1e9:.0f} GB")]
    la = os.getloadavg()
    rows.append(("Load average (1/5/15 min)", f"{la[0]:.1f} / {la[1]:.1f} / {la[2]:.1f}"))
    return rows


def stage_times(log: list[str]) -> dict[str, dict]:
    """Last start and done of each stage (a resumed stage restarts its timer)."""
    out: dict[str, dict] = {}
    for ln in log:
        m = re.match(r"(\d\d:\d\d:\d\d) === (\w+) done in (\d+)s ===", ln)
        if m:
            out.setdefault(m[2], {})["done"] = m[1]
            out[m[2]]["secs"] = int(m[3])
            continue
        m = re.match(r"(\d\d:\d\d:\d\d) === (\w+) ===", ln)
        if m:
            out[m[2]] = {"start": m[1]}
    return out


def running_vivado(root: Path) -> list[tuple[str, str, str, str]]:
    """(elapsed, stage-ish, design, run dir) for every live Vivado batch job of pramana."""
    out = []
    for ln in sh("ps -u $USER -o etime=,args= | grep '[u]nwrapped/lnx64.o/vivado -mode batch'").splitlines():
        et, _, args = ln.strip().partition(" ")
        m = re.search(r"-source (\S+)", args)
        if not m:
            continue
        src = Path(m.group(1))
        p = str(src)
        if "/profile/" in p:
            kind, design = "S1 profile", src.parent.parent.parent.name
        elif "/input/" in p:
            kind, design = "S2 input check", src.parent.parent.parent.name
        elif "/store/vivado_" in p:
            kind, design = "S2 baseline", src.parent.parent.name
        elif str(root) in p:
            kind, design = "campaign run (S4/S5/S6)", src.parent.parent.name
        else:
            continue
        design = re.sub(r"-[0-9a-f]{8}$", "", design)
        out.append((et, kind, design, str(src.parent.relative_to(REPO))))
    # runs started on another node through `srun --jobid ... --overlap` (PRAMANA_REMOTE)
    for ln in sh("ps -u $USER -o etime=,args= | grep '[s]run --jobid=' | grep -- '--chdir='").splitlines():
        et, _, args = ln.strip().partition(" ")
        m, j = re.search(r"--chdir=(\S+)", args), re.search(r"--jobid=(\d+)", args)
        if not m:
            continue
        d = Path(m.group(1))
        node = sh(f"squeue -h -j {j.group(1)} -o %N") if j else "?"
        out.append((et, f"remote ({node})", d.parent.name, str(d.relative_to(REPO)) if str(d).startswith(str(REPO)) else str(d)))
    return sorted(out, key=lambda r: r[1])


def write(camp: str) -> Path:
    cfg = Config.load(camp)
    C = cfg.root
    designs = json.loads((C / "designs.json").read_text())
    excluded = json.loads((C / "excluded.json").read_text()) if (C / "excluded.json").exists() else {}
    log = (C / "pramana.log").read_text().splitlines() if (C / "pramana.log").exists() else []
    times = stage_times(log)
    store_rs = jsonl(store.runs_root(cfg) / "results.jsonl")
    camp_rs = jsonl(C / "results.jsonl")
    sstate = json.loads((C / "search_state.json").read_text()) if (C / "search_state.json").exists() else {}
    pid = sh(f"pgrep -u $USER -f '^python3 -m pramana all {camp}' | head -1")
    alive = pid != ""
    slots = ""
    if pid:
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            slots = next((e.split(b"=", 1)[1].decode() for e in env
                          if e.startswith(b"PRAMANA_MAX_VIVADO=")), "4 (default)")
        except OSError:
            pass

    L = [f"# {camp}: training progress", "",
         f"Snapshot {datetime.now():%Y-%m-%d %H:%M:%S} on {os.uname().nodename}. "
         f"Campaign process: **{'running' if alive else 'NOT running'}**. "
         f"Refresh: `python3 scripts/campaign_status.py {camp}`.", "",
         "## System", "", "| | |", "|--|--|"]
    L += [f"| {k} | {v} |" for k, v in system()]
    L += [f"| Vivado limit | {slots or 'n/a'} slots × {cfg.threads} threads; workers {cfg.workers} |",
          f"| Designs | {len(designs)} registered, {len(excluded)} excluded |"]

    # stages
    current = next((s for s in STAGES if s in times and "done" not in times[s]), None)
    L += ["", "## Stages", "", "| stage | status | started | finished | duration |", "|--|--|--|--|--|"]
    for s in STAGES:
        t = times.get(s, {})
        if "done" in t:
            st = "done"
        elif s == current:
            st = "**running**" if alive else "**interrupted**"
        else:
            st = "pending"
        dur = f"{t['secs'] // 3600}h{t['secs'] % 3600 // 60:02d}m" if "secs" in t else ""
        L.append(f"| {s} | {st} | {t.get('start', '')} | {t.get('done', '')} | {dur} |")

    # per design
    L += ["", "## Per design", "",
          "| design | family | MB | profile | baseline | search | discover | best ΔWNS |",
          "|--|--|--|--|--|--|--|--|"]
    for d in sorted(designs, key=lambda d: d["dcp_mb"]):
        did = d["id"]
        if did in excluded:
            L.append(f"| {did} | {d['family']} | {d['dcp_mb']} | excluded: {excluded[did]} | | | | |")
            continue
        prof = store.profile_dir(cfg, d) / "profile.json"
        inp = store.input_dir(cfg, d) / "input.json"
        p_st = "done" if prof.exists() else ("input checked" if inp.exists() else "")
        base = [r for r in store_rs if r["design"] == did and r.get("dcp_md5") == d["md5"]
                and r["purpose"] == "baseline" and r.get("tag") in
                ("route_explore", "place_explore", "physopt_explore_postroute",
                 "physopt_explore_preroute", "opt_explore")]
        b_ok = sum(1 for r in base if r["status"] in ("ok", "error"))
        rs = [r for r in camp_rs if r["design"] == did]
        rnd = (sstate.get("design_round") or {}).get(did)
        stopped = did in (sstate.get("stopped") or [])
        s_st = (f"round {rnd}" + (" (stopped)" if stopped else "")) if rnd else ""
        n_s = sum(1 for r in rs if r["purpose"] in ("exploit", "test", "audit"))
        if n_s:
            s_st += f", {n_s} runs"
        trials = list((C / "discover" / did / "trials").glob("t*.json")) if (C / "discover" / did).exists() else []
        disc = f"{len(trials)} trials" if trials else ""
        inw = None
        if inp.exists():
            inw = (json.loads(inp.read_text()).get("input") or {}).get("wns")
        best = max([r["result"]["wns"] for r in base + rs if r.get("valid") and r.get("result")]
                   or [None], key=lambda x: -1e9 if x is None else x)
        gain = f"{best - inw:+.3f}" if best is not None and inw is not None else ""
        L.append(f"| {did} | {d['family']} | {d['dcp_mb']} | {p_st} | {b_ok}/{N_BASELINE} | {s_st} | {disc} | {gain} |")

    # running
    rv = running_vivado(C)
    L += ["", f"## Vivado jobs running now ({len(rv)})", ""]
    if rv:
        L += ["| elapsed | kind | design | run dir |", "|--|--|--|--|"]
        L += [f"| {e} | {k} | {d} | `{p}` |" for e, k, d, p in rv]
    else:
        L.append("none")
    llm = sh("tmux ls 2>/dev/null | cut -d: -f1")
    llm = [x for x in llm.splitlines() if camp in x]
    L += ["", f"## LLM sessions running now ({len(llm)})", ""]
    L += [f"- `{x}`" for x in llm] or ["none"]

    L += ["", "## Last 25 log lines (`pramana.log`)", "", "```"] + [l[:200] for l in log[-25:]] + ["```", ""]
    out = C / "PROGRESS.md"
    tmp = out.with_name("PROGRESS.md.tmp")
    tmp.write_text("\n".join(L))
    tmp.replace(out)
    return out


if __name__ == "__main__":
    camp = sys.argv[1]
    every = int(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 0
    while True:
        try:
            p = write(camp)
        except Exception as e:      # never die while refreshing
            print(f"{datetime.now():%H:%M:%S} status error: {e!r}", flush=True)
        else:
            if not every:
                print(p)
        if not every:
            break
        time.sleep(every)

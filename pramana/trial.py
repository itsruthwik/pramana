"""Trial runner for the discovery agent: submit a Tcl recipe, wait for its result.

  python -m pramana.trial <campaign> <design> submit <recipe.tcl>   -> prints trial id
  python -m pramana.trial <campaign> <design> wait <id> [max_s]       -> prints result JSON
  python -m pramana.trial <campaign> <design> list                   -> all trials so far

A recipe runs on the design's best checkpoint (the state discovery started from). After the
recipe, pramana finishes routing if needed and measures. Results go to results.jsonl with
purpose 'discover', so the rest of the framework sees them like any other run.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .campaign import Campaign
from .config import Config


def trial_dir(c: Campaign, did: str) -> Path:
    d = c.root / "discover" / did
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta(c: Campaign, did: str) -> dict:
    return json.loads((trial_dir(c, did) / "meta.json").read_text())


def submit(c: Campaign, did: str, recipe: Path) -> str:
    meta = _meta(c, did)
    td = trial_dir(c, did)
    trials = sorted((td / "trials").glob("t*.json")) if (td / "trials").exists() else []
    if len(trials) >= meta["max_trials"]:
        return f"REFUSED: trial budget of {meta['max_trials']} used"
    tid = f"t{len(trials) + 1:02d}"
    (td / "trials").mkdir(exist_ok=True)
    tcl = Path(recipe).read_text()
    (td / "trials" / f"{tid}.tcl").write_text(tcl)
    (td / "trials" / f"{tid}.json").write_text(json.dumps({"id": tid, "status": "running",
                                                             "recipe": str(recipe)}))
    subprocess.Popen([sys.executable, "-m", "pramana.trial", c.cfg.name, did, "_work", tid],
                     cwd=str(Path(__file__).resolve().parent.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return tid


def _work(c: Campaign, did: str, tid: str) -> None:
    meta = _meta(c, did)
    td = trial_dir(c, did)
    tcl = (td / "trials" / f"{tid}.tcl").read_text()
    st = c.state(did, meta["start"])
    job = c.job(did, st, [], "discover", tag=tid, extra_tcl=tcl)
    rec = c.runner.run(job)
    log_tail = ""
    try:
        out = Path(rec["run_dir"], "stdout.txt").read_text(errors="replace")
        i = out.find("STEP_BEGIN custom")
        log_tail = out[i:i + 6000] if i >= 0 else out[-4000:]
    except OSError:
        pass
    summary = {"id": tid, "status": rec["status"], "valid": rec["valid"],
               "start_wns": rec["start_wns"], "final_wns": (rec["result"] or {}).get("wns"),
               "d_wns": rec["d_wns"], "whs": (rec["result"] or {}).get("whs"),
               "opt_secs": rec["opt_secs"], "steps": rec["steps"], "run_id": rec["run_id"],
               "custom_step_log": log_tail}
    (td / "trials" / f"{tid}.json").write_text(json.dumps(summary, indent=1))


def wait(c: Campaign, did: str, tid: str, max_s: float = 540) -> str:
    p = trial_dir(c, did) / "trials" / f"{tid}.json"
    t0 = time.time()
    while time.time() - t0 < max_s:
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("status") != "running":
                return json.dumps(d, indent=1)
        time.sleep(10)
    return json.dumps({"id": tid, "status": "running", "note": "still running; call wait again"})


def list_trials(c: Campaign, did: str) -> str:
    td = trial_dir(c, did) / "trials"
    out = []
    for p in sorted(td.glob("t*.json")) if td.exists() else []:
        d = json.loads(p.read_text())
        out.append({k: d.get(k) for k in ("id", "status", "valid", "d_wns", "final_wns", "opt_secs")})
    return json.dumps(out, indent=1)


def main(argv=None) -> None:
    a = argv or sys.argv[1:]
    c = Campaign(Config.load(a[0]))
    did, cmd = a[1], a[2]
    if cmd == "submit":
        print(submit(c, did, Path(a[3])))
    elif cmd == "wait":
        print(wait(c, did, a[3], float(a[4]) if len(a) > 4 else 540))
    elif cmd == "list":
        print(list_trials(c, did))
    elif cmd == "_work":
        _work(c, did, a[3])


if __name__ == "__main__":
    main()

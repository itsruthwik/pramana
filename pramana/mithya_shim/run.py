#!/usr/bin/env python3
"""mithya: run the installed policy on one routed DCP.

    python run.py <design.dcp> [--budget 3000] [--out <dir>] [--threads 4]
                  [--vivado-settings <settings64.sh>]

The knowledge is installed offline next to this file (policy.json and selectors/, written by
pramana); at run time the only input is the DCP. All time counts against the budget:

  1. measure  one Vivado session: profile.tcl (base reports, input metrics) and profile_ext.tcl
              (only the extended reports the selector reads)
  2. choose   the selector picks one policy from the reports, features and the budget left
  3. run      a second session runs that policy, then the other candidates keep-if-better,
              until the deadline (mithya's start + budget)
  4. output   <out>/final.dcp if the result is legal and better than the input (else the input
              is the answer) and <out>/result.json

Vivado must be on PATH, or give --vivado-settings (or MITHYA_VIVADO_SETTINGS). The license is
the caller's environment.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import classify  # noqa: E402
import executor  # noqa: E402
import measure  # noqa: E402
import recipes  # noqa: E402
from policy import POLICY  # noqa: E402
from protocol import GRACE_S  # noqa: E402


def tcl_path(p) -> str:
    return "{" + str(p) + "}"


def vivado(script: Path, cwd: Path, settings: str, timeout_s: float) -> tuple[str, bool]:
    cmd = f"exec vivado -mode batch -notrace -source {script} -log vivado.log -journal vivado.jou"
    if settings:
        cmd = f"source {settings} >/dev/null 2>&1 && {cmd}"
    p = subprocess.Popen(["/bin/bash", "-c", cmd], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout_s)
        return out, False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        out, _ = p.communicate()
        return out, True


def result_line(tag: str, out: str):
    m = re.findall(rf"^{tag} (\{{.*\}})\s*$", out, re.M)
    return json.loads(m[-1]) if m else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mithya")
    ap.add_argument("dcp")
    ap.add_argument("--budget", type=float, default=3000.0, help="seconds, measuring included")
    ap.add_argument("--out", default="")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--vivado-settings", default=os.environ.get("MITHYA_VIVADO_SETTINGS", ""))
    a = ap.parse_args(argv)
    t_start = time.time()
    dcp = Path(a.dcp).resolve()
    out = Path(a.out).resolve() if a.out else dcp.parent / f"{dcp.stem}_mithya"
    prof, ext, work = out / "profile", out / "profile_ext", out / "work"
    for d in (prof, ext, work):
        d.mkdir(parents=True, exist_ok=True)

    # 1. measure
    s1 = out / "measure.tcl"
    s1.write_text("\n".join([
        f"set_param general.maxThreads {a.threads}",
        f"open_checkpoint {tcl_path(dcp)}",
        f"set _rd {tcl_path(prof)}", f"source {tcl_path(HERE / 'profile.tcl')}",
        'puts "INPUT [_result_json]"', 'puts "PART [get_property PART [current_design]]"',
        f"set _rd {tcl_path(ext)}", f"source {tcl_path(HERE / 'profile_ext.tcl')}",
        'puts "MITHYA_MEASURED"']) + "\n")
    o1, to1 = vivado(s1, work, a.vivado_settings, a.budget + GRACE_S)
    (out / "measure.stdout.txt").write_text(o1)
    inp = result_line("INPUT", o1)
    profile_s = round(time.time() - t_start, 1)
    rec = {"dcp": str(dcp), "budget_s": a.budget, "selector": POLICY["selector"]["name"],
           "kb_meta": POLICY.get("kb_meta"), "profile_s": profile_s, "input": inp}
    if inp is None or "MITHYA_MEASURED" not in o1:
        rec.update(status="measure_failed", timed_out=to1)
        (out / "result.json").write_text(json.dumps(rec, indent=1))
        print(f"mithya: measuring failed ({'timeout' if to1 else 'see measure.stdout.txt'})")
        return 1
    (prof / "profile.json").write_text(json.dumps({"input": inp}, indent=1))

    # 2. choose
    left = max(a.budget - profile_s, 0.0)
    prims = inp.get("prims")
    f = measure.design_features(prof, ext)
    rel = recipes.rel_budget(left, prims)
    pick = classify.choose_policy(prof, ext, f, rel)
    steps = recipes.sequence_for(pick, left, prims)
    rec.update(policy=pick, rel_budget=rel,
               plan=[{"name": s.name, "mode": s.mode, "est_s": round(s.est_secs)} for s in steps])

    # 3. run (deadline is absolute: mithya's start + budget)
    final = out / "final.dcp"
    s2 = out / "run.tcl"
    s2.write_text("\n".join([
        f"set_param general.maxThreads {a.threads}",
        (HERE / "measure_procs.tcl").read_text(),
        f"open_checkpoint {tcl_path(dcp)}",
        "namespace eval ::pramana {}",
        f"set ::pramana::deadline {int(t_start + a.budget)}",
        executor.policy_tcl(steps, None),
        "set _w [::pramana::wns]; set _ok [::pramana::legal]",
        'puts "FINAL [_result_json]"',
        f'if {{$_ok && $_w > {inp.get("wns") if inp.get("wns") is not None else -1e9}}} '
        f'{{write_checkpoint -force {tcl_path(final)}; puts "MITHYA_WROTE"}} '
        f'else {{puts "MITHYA_KEPT_INPUT legal=$_ok wns=$_w"}}',
        'puts "MITHYA_DONE"']) + "\n")
    o2, to2 = vivado(s2, work, a.vivado_settings,
                     max(t_start + a.budget + GRACE_S - time.time(), 60))
    (out / "run.stdout.txt").write_text(o2)

    # 4. output
    fin = result_line("FINAL", o2)
    wrote = "MITHYA_WROTE" in o2
    total_s = round(time.time() - t_start, 1)
    steps_log = re.findall(r"^PRAMANA (.+)$", o2, re.M)
    d_wns = round(fin["wns"] - inp["wns"], 4) if wrote and fin and inp.get("wns") is not None else 0.0
    rec.update(status="ok" if "MITHYA_DONE" in o2 else ("timeout" if to2 else "run_failed"),
               final=fin, wrote_final=wrote, final_dcp=str(final) if wrote else str(dcp),
               d_wns=d_wns, total_s=total_s, steps_log=steps_log)
    (out / "result.json").write_text(json.dumps(rec, indent=1))
    print(f"mithya: policy {pick} ({len(steps)} steps planned), dWNS {d_wns:+} ns, "
          f"{total_s:.0f} s of {a.budget:.0f} s (measuring {profile_s:.0f} s) -> {rec['final_dcp']}")
    return 0 if rec["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

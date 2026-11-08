"""S1: read-only profile of each training DCP (reports + input metrics + top paths)."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from . import inputs, tcl
from .config import Config
from .store import profile_dir
from .vivado import est_mem_gb, run_vivado

REPORTS = [
    ("timing_summary", "report_timing_summary -max_paths 20"),
    ("design_analysis", "report_design_analysis -timing -logic_level_distribution -max_paths 50"),
    ("congestion", "report_design_analysis -congestion"),
    ("complexity", "report_design_analysis -complexity"),
    ("utilization", "report_utilization"),
    ("control_sets", "report_control_sets"),
    ("high_fanout_nets", "report_high_fanout_nets -max_nets 30"),
    ("clock_utilization", "report_clock_utilization"),
    ("clocks", "report_clocks"),
    ("qor_assessment", "report_qor_assessment"),
    ("qor_suggestions", "report_qor_suggestions"),
    ("route_status", "report_route_status"),
    ("exceptions", "report_exceptions"),
]


def profile_script(dcp: str, out: Path, threads: int) -> str:
    p = [tcl.header(threads), tcl.PATHS_DUMP, f"open_checkpoint {tcl.tcl_path(dcp)}",
         f"set _rd {tcl.tcl_path(out)}",
         'puts "PART [get_property PART [current_design]]"',
         'puts "N_SLR [llength [get_slrs -quiet]]"',
         "_emit_result",
         "_paths_dump 200 [file join $_rd paths.tsv]"]
    for name, cmd in REPORTS:
        p.append(tcl.safe_report(name, cmd))
    p.append(f'puts "{tcl.DONE}"')
    return "\n".join(p) + "\n"


def profile_design(cfg: Config, d: dict) -> dict:
    out = profile_dir(cfg, d)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "profile.json").exists():
        prof = json.loads((out / "profile.json").read_text())
        if inputs.load(cfg, d) is None:      # profiles made before the input check existed
            inputs.save(cfg, d, {"id": d["id"], "md5": d["md5"], "status": prof["status"],
                                 "input": prof["input"], "part": prof["part"],
                                 "n_slr": prof["n_slr"], "dcp_unchanged": prof["dcp_unchanged"],
                                 "secs": prof.get("profile_secs"), "source": "profile"})
        return prof
    (out / "profile.tcl").write_text(profile_script(d["path"], out, cfg.threads))
    vr = run_vivado(out / "profile.tcl", out, cfg.vivado_settings, cfg.run_timeout_s,
                    need_gb=cfg.min_free_mem_gb + est_mem_gb(d["dcp_mb"]))
    (out / "stdout.txt").write_text(vr.stdout)
    rec = inputs.record(d, vr.stdout, vr.exit_code, vr.timed_out, vr.wall_s, "profile")
    # the same measurement S2 needs: publish it as the input check unless one exists
    if inputs.load(cfg, d) is None:
        inputs.save(cfg, d, rec)
    prof = {"id": d["id"], "status": rec["status"], "input": rec["input"],
            "part": rec["part"], "n_slr": rec["n_slr"], "profile_secs": rec["secs"],
            "dcp_unchanged": rec["dcp_unchanged"]}
    (out / "profile.json").write_text(json.dumps(prof, indent=1))
    return prof


def profile_all(cfg: Config, designs: list[dict], log=print) -> list[dict]:
    with ThreadPoolExecutor(cfg.workers) as ex:
        profs = list(ex.map(lambda d: profile_design(cfg, d), designs))
    for p in profs:
        inp = p.get("input") or {}
        log(f"[profile] {p['id']:28s} {p['status']} part={p['part']} wns={inp.get('wns')} "
            f"whs={inp.get('whs')} clocks={len(inp.get('clocks') or [])} unchanged={p['dcp_unchanged']}")
    # one part per campaign: unusable designs are excluded (recorded, not fatal); the same
    # rule S2 applies through its own input check (inputs.py)
    keep = {r["id"] for r in inputs.exclude(cfg, [{**p, "md5": d["md5"], "secs": p["profile_secs"],
                                                   "source": "profile"}
                                                  for p, d in zip(profs, designs)], log, "profile")}
    return [p for p in profs if p["id"] in keep]

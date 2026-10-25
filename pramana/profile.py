"""S1: read-only profile of each training DCP (reports + input metrics + top paths)."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tcl
from .config import Config
from .corpus import md5
from .runner import parse_stdout
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
    out = cfg.root / "profile" / d["id"]
    out.mkdir(parents=True, exist_ok=True)
    if (out / "profile.json").exists():
        return json.loads((out / "profile.json").read_text())
    (out / "profile.tcl").write_text(profile_script(d["path"], out, cfg.threads))
    vr = run_vivado(out / "profile.tcl", out, cfg.vivado_settings, cfg.run_timeout_s,
                    need_gb=max(cfg.min_free_mem_gb, est_mem_gb(d["dcp_mb"])))
    (out / "stdout.txt").write_text(vr.stdout)
    parsed = parse_stdout(vr.stdout, vr.exit_code, vr.timed_out)
    part = re.search(r"^PART (\S+)", vr.stdout, re.M)
    nslr = re.search(r"^N_SLR (\d+)", vr.stdout, re.M)
    prof = {"id": d["id"], "status": parsed["status"], "input": parsed["result"],
            "part": part.group(1) if part else None,
            "n_slr": int(nslr.group(1)) if nslr else None,
            "profile_secs": round(vr.wall_s, 1),
            "dcp_unchanged": md5(Path(d["path"])) == d["md5"]}
    (out / "profile.json").write_text(json.dumps(prof, indent=1))
    return prof


def profile_all(cfg: Config, designs: list[dict], log=print) -> list[dict]:
    with ThreadPoolExecutor(cfg.workers) as ex:
        profs = list(ex.map(lambda d: profile_design(cfg, d), designs))
    for p in profs:
        inp = p.get("input") or {}
        log(f"[profile] {p['id']:28s} {p['status']} part={p['part']} wns={inp.get('wns')} "
            f"whs={inp.get('whs')} clocks={len(inp.get('clocks') or [])} unchanged={p['dcp_unchanged']}")
    parts = {p["part"] for p in profs if p["part"]}
    if len(parts) > 1:
        raise SystemExit(f"campaign mixes parts {parts}; one part per campaign")
    return profs

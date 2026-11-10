"""S7a: extended profile, a second read-only Vivado session per design that collects a broad,
fixed set of reports for feature learning (S7b). It runs just before learning, so S0-S6 are
unchanged; the S1 profile and its features stay as they are.

Output, next to the S1 profile in the store: <dcp>/vivado_<ver>/profile_ext/
  <name>.txt per report (an error becomes the file body: "REPORT_ERROR: ..."),
  paths.tsv (1000 worst setup paths), hold_paths.tsv (200 worst hold paths),
  report_secs.json (seconds per report), done.json.

The same script is shipped in the mithya bundle (profile_ext.tcl), so a held-out design is
measured exactly as the training designs were.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tcl
from .config import Config
from .store import profile_dir
from .vivado import est_mem_gb, run_vivado

# (name, command). Fixed measurement protocol: not learned, the same for every design.
REPORTS_EXT = [
    # the S1 reports with higher caps
    ("timing_summary", "report_timing_summary -max_paths 50"),
    ("design_analysis", "report_design_analysis -timing -logic_level_distribution -max_paths 200"),
    ("high_fanout_nets", "report_high_fanout_nets -timing -max_nets 200"),
    ("congestion", "report_design_analysis -congestion -show_all"),
    ("complexity", "report_design_analysis -complexity -hierarchical_depth 3"),
    ("utilization", "report_utilization"),
    ("control_sets", "report_control_sets -verbose"),
    ("clock_utilization", "report_clock_utilization"),
    ("clocks", "report_clocks"),
    ("qor_assessment", "report_qor_assessment"),
    ("qor_suggestions", "report_qor_suggestions"),
    ("route_status", "report_route_status"),
    ("exceptions", "report_exceptions"),
    # hold
    ("timing_hold", "report_timing -hold -max_paths 50 -nworst 1"),
    # hierarchy
    ("utilization_hier", "report_utilization -hierarchical -hierarchical_depth 3"),
    ("design_analysis_hier", "report_design_analysis -timing -max_paths 200 -hierarchical_depth 2"),
    # clocks
    ("clock_interaction", "report_clock_interaction"),
    ("cdc", "report_cdc -details"),
    ("bus_skew", "report_bus_skew"),
    # physical
    ("ram_utilization", "report_ram_utilization"),
    ("pulse_width", "report_pulse_width"),
    ("utilization_slr", "report_utilization -slr"),
    # methodology and netlist checks
    ("methodology", "report_methodology"),
    ("drc", "report_drc"),
    # power (vectorless)
    ("power", "report_power"),
]

HOLD_DUMP = (tcl.PATHS_DUMP.replace("proc _paths_dump", "proc _paths_dump_hold")
             .replace("-setup", "-hold").replace("Top-N setup paths", "Top-N hold paths"))


def ext_dir(cfg: Config, d: dict) -> Path:
    return profile_dir(cfg, d).parent / "profile_ext"


def timed_report(name: str, cmd: str) -> str:
    """tcl.safe_report plus the report's seconds (puts EXT_SECS <name> <s>)."""
    return (f"set _t0 [clock milliseconds]\n" + tcl.safe_report(name, cmd) +
            f'puts "EXT_SECS {name} [expr {{([clock milliseconds] - $_t0) / 1000.0}}]"\n')


def body() -> str:
    """Reports on the open design (shared by training and the mithya bundle); needs $_rd."""
    p = [tcl.PATHS_DUMP, HOLD_DUMP,
         "set _t0 [clock milliseconds]",
         "_paths_dump 1000 [file join $_rd paths.tsv]",
         'puts "EXT_SECS paths [expr {([clock milliseconds] - $_t0) / 1000.0}]"',
         "set _t0 [clock milliseconds]",
         "_paths_dump_hold 200 [file join $_rd hold_paths.tsv]",
         'puts "EXT_SECS hold_paths [expr {([clock milliseconds] - $_t0) / 1000.0}]"']
    p += [timed_report(n, c) for n, c in REPORTS_EXT]
    return "\n".join(p) + "\n"


def script(dcp: str, out: Path, threads: int) -> str:
    return "\n".join([tcl.header(threads), f"open_checkpoint {tcl.tcl_path(dcp)}",
                      f"set _rd {tcl.tcl_path(out)}", body(), f'puts "{tcl.DONE}"']) + "\n"


def profile_ext_design(cfg: Config, d: dict) -> dict:
    out = ext_dir(cfg, d)
    if (out / "done.json").exists():
        return json.loads((out / "done.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    (out / "profile_ext.tcl").write_text(script(d["path"], out, cfg.threads))
    vr = run_vivado(out / "profile_ext.tcl", out, cfg.vivado_settings, cfg.run_timeout_s,
                    need_gb=cfg.min_free_mem_gb + est_mem_gb(d["dcp_mb"]))
    (out / "stdout.txt").write_text(vr.stdout)
    secs = {m.group(1): float(m.group(2))
            for m in re.finditer(r"^EXT_SECS (\S+) ([\d.]+)", vr.stdout, re.M)}
    (out / "report_secs.json").write_text(json.dumps(secs, indent=1))
    errors = sorted(f.stem for f in out.glob("*.txt")
                    if f.read_text(errors="replace").startswith("REPORT_ERROR"))
    ok = tcl.DONE in vr.stdout and not vr.timed_out
    rec = {"id": d["id"], "ok": ok, "wall_s": round(vr.wall_s, 1), "errors": errors,
           "n_reports": len(secs)}
    if ok:
        (out / "done.json").write_text(json.dumps(rec, indent=1))
    return rec


def run(cfg: Config, designs: list[dict], log=print) -> list[dict]:
    with ThreadPoolExecutor(max(1, cfg.workers)) as ex:
        recs = list(ex.map(lambda d: profile_ext_design(cfg, d), designs))
    for r in recs:
        log(f"[profile_ext] {r['id']:28s} {'ok' if r['ok'] else 'FAILED'} {r['wall_s']}s "
            f"reports={r['n_reports']} errors={r['errors'] or '-'}")
    return recs


def reports(cfg: Config, d: dict) -> dict[str, str]:
    """All report texts for a design: S1 profile files as '<name>', extended ones as
    'ext/<name>' (paths and hold paths included as TSV text)."""
    out = {}
    for base, pre in ((profile_dir(cfg, d), ""), (ext_dir(cfg, d), "ext/")):
        if base.exists():
            for f in sorted(base.iterdir()):
                if f.suffix in (".txt", ".tsv") and f.stem not in ("stdout",):
                    out[pre + f.stem] = f.read_text(errors="replace")
    return out

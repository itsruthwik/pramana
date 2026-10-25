"""Scale-free design features, computed from a profile directory.

Used by the KB's conditions and bins, and shipped to mithya as-is (the compiler copies this
file), so training and deployment compute features identically. Raw sizes are kept separately
under SIZE_FEATURES and are not used in learned rules: they don't transfer.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path

SIZE_FEATURES = {"log10_prims"}

FEATURE_DOC = {
    "wns_frac": "-WNS / period of worst clock (how far from closure)",
    "fe_frac": "failing setup endpoints / total endpoints",
    "tns_per_fe_frac": "mean slack of failing endpoints / period",
    "logic_frac": "top-20 paths: mean logic delay / datapath delay",
    "levels_mean": "top-20 paths: mean logic levels",
    "levels_max": "top-200 paths: max logic levels",
    "skew_frac": "top-20 paths: mean |skew| / requirement",
    "near_crit_frac": "share of top-200 paths within 10% of period of the worst slack",
    "end_bram_frac": "top-200 paths ending at block RAM",
    "end_dsp_frac": "top-200 paths ending at DSP",
    "end_uram_frac": "top-200 paths ending at URAM",
    "end_lutram_frac": "top-200 paths ending at LUTRAM/SRL",
    "start_bram_frac": "top-200 paths starting at block RAM",
    "start_dsp_frac": "top-200 paths starting at DSP",
    "carry_frac": "top-200 paths through CARRY8",
    "hf_path_frac": "top-200 paths with a net of fanout > 100",
    "log10_max_fanout": "log10 of max net fanout on the top-20 paths",
    "cross_clock_frac": "top-200 paths whose start and end clocks differ",
    "util_lut": "LUT utilisation (0-1)", "util_ff": "FF utilisation (0-1)",
    "util_bram": "block RAM utilisation (0-1)", "util_dsp": "DSP utilisation (0-1)",
    "util_uram": "URAM utilisation (0-1)",
    "ctrl_sets_per_ff": "control sets / registers",
    "congestion_level": "max placer congestion level reported (0 if none above 5)",
    "n_clocks": "number of clocks",
    "log10_prims": "log10 primitive count (size; not used in rules)",
}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _paths(prof_dir: Path) -> list[dict]:
    p = prof_dir / "paths.tsv"
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _util(text: str, label: str) -> float | None:
    m = re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*([\d.]+)\s*\|.*?\|\s*([\d.]+)\s*\|\s*$", text, re.M)
    return float(m.group(2)) / 100.0 if m else None


def from_profile(prof_dir: Path) -> dict:
    prof = json.loads((prof_dir / "profile.json").read_text())
    inp = prof.get("input") or {}
    per = inp.get("worst_period") or 1.0
    wns = inp.get("wns") or 0.0
    fe, te, tns = inp.get("failing_endpoints"), inp.get("total_endpoints"), inp.get("tns")
    rows = _paths(prof_dir)
    top20 = rows[:20]
    f: dict = {}
    f["wns_frac"] = round(-wns / per, 4)
    f["fe_frac"] = round(fe / te, 4) if fe is not None and te else None
    f["tns_per_fe_frac"] = round((tns / fe) / per, 4) if tns is not None and fe else 0.0
    f["logic_frac"] = _mean([_f(r["logic"]) / _f(r["datapath"]) for r in top20
                             if _f(r["datapath"])])
    f["levels_mean"] = _mean([_f(r["levels"]) for r in top20])
    f["levels_max"] = max((_f(r["levels"]) or 0 for r in rows), default=None)
    f["skew_frac"] = _mean([abs(_f(r["skew"]) or 0) / (_f(r["req"]) or per) for r in top20])
    n = len(rows) or 1
    if rows:
        worst = _f(rows[0]["slack"]) or 0
        f["near_crit_frac"] = sum(1 for r in rows if (_f(r["slack"]) or 0) <= worst + 0.1 * per) / n
    def frac(pred):
        return round(sum(1 for r in rows if pred(r)) / n, 4) if rows else None
    f["end_bram_frac"] = frac(lambda r: r["end_ref"].startswith("RAMB"))
    f["end_dsp_frac"] = frac(lambda r: r["end_ref"].startswith("DSP"))
    f["end_uram_frac"] = frac(lambda r: r["end_ref"].startswith("URAM"))
    f["end_lutram_frac"] = frac(lambda r: r["end_ref"].startswith(("RAMD", "RAMS", "SRL")))
    f["start_bram_frac"] = frac(lambda r: r["start_ref"].startswith("RAMB"))
    f["start_dsp_frac"] = frac(lambda r: r["start_ref"].startswith("DSP"))
    f["carry_frac"] = frac(lambda r: "CARRY" in r["cell_refs"])
    f["hf_path_frac"] = frac(lambda r: (_f(r["max_fanout"]) or 0) > 100)
    mf = max((_f(r["max_fanout"]) or 1 for r in top20), default=1)
    f["log10_max_fanout"] = round(math.log10(max(mf, 1)), 3)
    f["cross_clock_frac"] = frac(lambda r: r["start_clk"] != r["end_clk"])
    ut = (prof_dir / "utilization.txt").read_text() if (prof_dir / "utilization.txt").exists() else ""
    f["util_lut"] = _util(ut, "CLB LUTs")
    f["util_ff"] = _util(ut, "CLB Registers")
    f["util_bram"] = _util(ut, "Block RAM Tile")
    f["util_dsp"] = _util(ut, "DSPs")
    f["util_uram"] = _util(ut, "URAM")
    cs = (prof_dir / "control_sets.txt").read_text() if (prof_dir / "control_sets.txt").exists() else ""
    m = re.search(r"Total control sets\s*\|\s*(\d+)", cs)
    regs = re.search(r"^\|\s*CLB Registers\s*\|\s*(\d+)", ut, re.M)
    f["ctrl_sets_per_ff"] = (round(int(m.group(1)) / max(int(regs.group(1)), 1), 5)
                             if m and regs else None)
    cg = (prof_dir / "congestion.txt").read_text() if (prof_dir / "congestion.txt").exists() else ""
    levels = [int(x) for x in re.findall(r"^\|\s*\w+\s*\|\s*\w+\s*\|\s*(\d+)\s*\|", cg, re.M)]
    f["congestion_level"] = max(levels) if levels else 0
    f["n_clocks"] = len(inp.get("clocks") or [])
    f["log10_prims"] = round(math.log10(inp["prims"]), 3) if inp.get("prims") else None
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()}


def rule_features(feats: dict) -> dict:
    return {k: v for k, v in feats.items() if k not in SIZE_FEATURES}

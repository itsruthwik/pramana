#!/usr/bin/env python3
"""Pure parsing/decision helpers for Vivado reports.

THE ONE HOME for reading Vivado report text and for the ship-safety decisions
derived from it. Text in, dataclass/bool/int out -- no Vivado, no MCP, no LLM,
no I/O beyond atomic_copy's file move. That purity is the point: these are the
functions where WRONG != CRASH. They return None on failure, so a defect here
does not raise, it silently ships an invalid design and scores the benchmark
zero.

Two such defects were found and fixed when this module was created; see
parse_route_status and tests/test_ship_gates.py. Both were duplicate
implementations that had drifted -- hence one home.

New parsers belong here, not in the agent files.

Covered by tests/test_ship_gates.py against a real Vivado report corpus.
"""
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _f(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(s: str) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


@dataclass
class TimingSummary:
    """One parse of `report_timing_summary`. Setup AND hold AND pulse-width.

    What this replaced read only the first three columns of the Design Timing
    Summary row, so WHS/WPWS were structurally invisible -- and the contest
    rejects any design with WHS < 0 or WPWS < 0, scoring it ZERO. Every field the
    ship gate needs comes from this one parse of one design state.
    """
    setup_wns: Optional[float] = None
    setup_tns: Optional[float] = None
    setup_failing_endpoints: Optional[int] = None
    total_endpoints: Optional[int] = None
    hold_whs: Optional[float] = None
    hold_ths: Optional[float] = None
    hold_failing_endpoints: Optional[int] = None
    pulse_width_wns: Optional[float] = None
    complete: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_timing_summary(text: str) -> TimingSummary:
    """Parse `report_timing_summary`. Reads the Design Timing Summary data row:
        WNS  TNS  TNS_fail  TNS_total  WHS  THS  THS_fail  THS_total  WPWS ...
    Locates the WNS(ns)/TNS(ns) header, then the first all-numeric row after it,
    so it is robust to column spacing and the dashed separator line.

    Ported from the ruthwik branch (src/vivado_reports/parsers.py). Scanning for
    the first numeric row -- rather than assuming header+2 -- also stops the
    per-clock summary table further down the report from being read as the
    design-wide row.
    """
    out = TimingSummary()
    if not text:
        return out
    lines = text.splitlines()
    hdr = next((i for i, ln in enumerate(lines)
                if "WNS(ns)" in ln and "TNS(ns)" in ln), -1)
    if hdr == -1:
        return out
    for ln in lines[hdr + 1:hdr + 5]:
        nums = ln.split()
        if len(nums) >= 4 and _f(nums[0]) is not None and _f(nums[1]) is not None:
            out.setup_wns = _f(nums[0])
            out.setup_tns = _f(nums[1])
            out.setup_failing_endpoints = _i(nums[2])
            out.total_endpoints = _i(nums[3])
            if len(nums) >= 8:
                out.hold_whs = _f(nums[4])
                out.hold_ths = _f(nums[5])
                out.hold_failing_endpoints = _i(nums[6])
            if len(nums) >= 9:
                out.pulse_width_wns = _f(nums[8])
            out.complete = out.setup_wns is not None
            break
    return out


# Vivado prints these with a dot-leader run before the colon, e.g.
#     # of nets with routing errors.......... :           0 :
# The ruthwik original used `errors\s*[:.]?\s*(\d+)`, which matches a SINGLE
# separator character and therefore matched NOTHING on any of the 58 route
# reports in runs/ -- it then fell through to a `"fully routed" in text`
# substring test that is satisfied by the innocuous line
#     # of fully routed nets............. :        2938 :
# so the gate returned True unconditionally, including for a design WITH routing
# errors. Verified against the corpus before this rewrite; do not "simplify"
# `[.\s]*` back to `[:.]?`.
_ROUTE_ERR_RE = re.compile(
    r"#\s*of\s+nets\s+with\s+routing\s+errors[.\s]*:\s*(\d+)", re.IGNORECASE)
_ROUTE_UNROUTED_RE = re.compile(
    r"#\s*of\s+(?:unrouted|partially\s+routed)\s+nets[.\s]*:\s*(\d+)", re.IGNORECASE)


_ROUTE_ROUTABLE_RE = re.compile(
    r"#\s*of\s+routable\s+nets[.\s]*:\s*(\d+)", re.IGNORECASE)


def route_routable_count(text: str) -> Optional[int]:
    """How many nets actually needed routing, or None if the report did not say.

    This is the ONLY field that distinguishes an empty design from a perfect one.
    `route_bad_net_count` counts DEFECTIVE nets, so it returns 0 -- a clean pass --
    for a design in which nothing was placed and therefore nothing could be routed.
    One node in the sweep corpus recorded exactly that: routed, 0 errors, 0 unrouted,
    and 7086 logical nets of which ZERO were routable, because the placement had
    silently failed. Counting bad nets cannot see that; counting work done can.
    """
    if not text:
        return None
    m = _ROUTE_ROUTABLE_RE.search(text)
    return int(m.group(1)) if m else None


def route_bad_net_count(text: str) -> Optional[int]:
    """Unrouted + partially-routed + routing-error nets, or None if the report
    did not say. THE ONE HOME for reading report_route_status -- every consumer
    (the ship gate, the diagnosis EXTENDED_CHECKS, the explorers) goes through
    this, so a format change is fixed in one place instead of four."""
    if not text:
        return None
    counts = [int(m) for m in _ROUTE_ERR_RE.findall(text)]
    counts += [int(m) for m in _ROUTE_UNROUTED_RE.findall(text)]
    if not counts:
        return None
    return sum(counts)


def parse_route_status(text: str) -> Optional[bool]:
    """Is the design fully routed with ZERO routing errors?

    PURE: True / False / None. None means "the report did not say" -- an
    unrecognised format or an empty string. The caller applies the polarity,
    because the two decisions differ in cost: mid-run, unknown is survivable;
    at the ship boundary, unknown must be treated as failure (the contest scores
    an unroutable DCP zero, and the untouched input is valid by definition).

    Deliberately has no "looks fine" fallback -- that is precisely the bug this
    replaces.

    ZERO ROUTABLE NETS IS NOT A PASS. A design in which placement failed has no
    routable nets, so it also has no BAD nets, and a defect-counting check calls it
    perfectly routed. Verified against 1248 archived route reports: the field parses
    on all of them, and exactly one has routable == 0 -- a node whose placement had
    silently failed and which `route_bad_net_count` scores 0. Requiring work to have
    been done is what separates that design from a real one.
    """
    bad = route_bad_net_count(text)
    if bad is None:
        return None
    routable = route_routable_count(text)
    if routable is None:
        return None                       # unreadable -> "did not say", never a pass
    if routable <= 0:
        logger.warning("Route status: 0 routable nets -- nothing was placed; "
                       "a zero-defect count here is vacuous, not a pass")
        return False
    if bad:
        logger.warning(f"Route status: {bad} unrouted/partially-routed/error nets")
    return bad == 0


# \r-TOLERANT ON PURPOSE. The MCP/pexpect transport delivers Vivado output with CRLF
# line endings; the archived tool_outputs logs read back through Python's universal
# newlines as LF. A regex written against the archived text ("-+\n") therefore matches
# the fixture and NOT the live stream -- it silently returned None on every real run,
# which fell back to the probe rather than breaking, so nothing would have failed
# loudly. Every line break here is \r?\n.
_CLK_SUMMARY_RE = re.compile(
    r"\|[ \t]*Clock Summary[ \t]*\r?\n\|[ \t]*-+[ \t]*\r?\n-+[ \t]*\r?\n(.*?)(?:\r?\n-{10,}|\Z)",
    re.S)


def parse_clock_count(tsum_text: str) -> Optional[int]:
    """Number of clocks, read from report_timing_summary's Clock Summary table.

    Re-sources `llength [get_clocks]` from a report the caller already holds, saving a
    Vivado roundtrip. Returns None -- never a guess -- when the table is absent or
    yields no rows, because the count gates whether the multi-clock reports run: a
    wrong LOW value silently drops them from the digest, which looks like a clean
    single-clock design rather than a parse failure.

    Verified against all 12 benchmarks of runs/benchmark_logs_20260809_002643: matches
    MAYA_NCLK 12/12, including corescore_500_mod at 2. Note only ONE of those designs
    has more than one clock, so the multi-clock case rests on a single data point --
    hence the None-means-fall-back-to-the-probe contract rather than a hard swap.
    """
    m = _CLK_SUMMARY_RE.search(tsum_text or "")
    if not m:
        return None
    n = 0
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("Clock ") or set(ln) <= set("- "):
            continue
        if re.match(r"^\S+\s+[\{\d.]", ln):        # "<name>  {0.000 0.769}  1.538 ..."
            n += 1
    return n or None


_PRIM_RE = re.compile(r"MAYA_PRIMS=(\d+)")


def parse_primitive_count(text: str) -> Optional[int]:
    """Leaf-primitive count from a `puts "MAYA_PRIMS=[llength ...]"` probe.

    WHY THIS MATTERS MORE THAN IT LOOKS. It is the only cheap answer to "did that
    step change the design at all". Two results with the same WNS may be a genuine
    tie or the SAME NETLIST built twice; the slack cannot tell them apart and the
    primitive count can. In the sweep corpus that distinction exposed roughly half
    the measured "levers" as exact no-ops -- including directives that differ only
    in name (`Explore` vs `AggressiveExplore` for phys_opt were bit-identical on
    12 of 12 designs, every recorded field).

    Returns None when the marker is absent. None means "not measured" and must not
    be compared against a previous count -- an absent probe is not a change.
    """
    if not text:
        return None
    m = _PRIM_RE.search(text)
    return int(m.group(1)) if m else None


# Messages phys_opt_design prints when it finds nothing to do. Sourced from real
# Vivado output, not from the documentation.
_PHYS_OPT_INERT_MARKERS = (
    "Optimized 0 net or LUT",
    "No candidate cells found",
    "No nets found for optimization",
)


def phys_opt_inert(log_text: str) -> Optional[bool]:
    """Did phys_opt_design report that it had nothing to optimize?

    True  = the pass was inert; further phys_opt directives on this design will
            also be no-ops, so budget spent on them is wasted.
    False = it reported doing work.
    None  = no recognisable phys_opt output, so no verdict. Do NOT read None as
            "it worked" -- it means the text was not phys_opt's.

    Measured motivation: phys_opt is completely inert on 6 of 12 designs in the
    sweep corpus, at every flow position. Its own log says so immediately, long
    before a timing report shows a zero delta.

    STANDALONE AND UNWIRED BY DESIGN. Nothing calls this yet; it is not part of the
    state digest. Keep it that way until there is a decision it is meant to drive.
    """
    if not log_text:
        return None
    if any(m in log_text for m in _PHYS_OPT_INERT_MARKERS):
        return True
    if "phys_opt_design" in log_text or "Phys Opt Design" in log_text:
        return False
    return None


# ---------------------------------------------------------------------------
# Deep-diagnostic parsers + classifier.
#
# These mirror the manual report-mining work (manual_runs/DIAGNOSIS_CATEGORIES.md):
# the *fundamental* question for any DCP is "where is the worst path's delay spent?"
# (logic% vs net%), refined by WHY the wire is long (high-fanout / spread / congestion),
# with "compound" designs -- both wire-bound and deep in logic -- sitting outside that
# split. (DONT_TOUCH was once a fourth branch here; it is a reported fact, not a
# precondition -- see classify_design.) All inputs come from reports that
# Maya can already produce via run_tcl, so this needs no new MCP tools.
# ---------------------------------------------------------------------------
def parse_worst_path_delay_split(report: str) -> dict:
    """Parse `report_design_analysis -timing`: the worst path's logic%/net%, logic
    levels, endpoint, and the max High-Fanout across the top paths (+ how many top
    paths carry a >=100-fanout net). Returns a dict; values are None/0 if not found."""
    out = {"logic_pct": None, "net_pct": None, "logic_levels": None,
           "endpoint": None, "hf_max": 0, "hf_over": 0}
    rows = re.findall(r"^\|\s*Path #\d+\s*\|.*$", report, re.M)
    if not rows:
        return out
    # Pipe-column layout (1-indexed after split): 4 LogicDelay(L%), 5 NetDelay(N%),
    # 11 LogicLevels, 21 HighFanout, 26 EndPtPinPrimitive, 28 EndPtPin.
    c = [x.strip() for x in rows[0].split("|")]
    col = lambda i: c[i] if i < len(c) else ""
    m = re.search(r"\((\d+)%\)", col(4))
    out["logic_pct"] = int(m.group(1)) if m else None
    m = re.search(r"\((\d+)%\)", col(5))
    out["net_pct"] = int(m.group(1)) if m else None
    if col(11).isdigit():
        out["logic_levels"] = int(col(11))
    prim, pin = col(26), col(28)
    out["endpoint"] = (f"{prim} ({pin})" if prim and pin else (prim or pin or None))
    hf = []
    for r in rows:
        cc = [x.strip() for x in r.split("|")]
        if len(cc) > 21 and cc[21].isdigit():
            hf.append(int(cc[21]))
    out["hf_max"] = max(hf) if hf else 0
    out["hf_over"] = sum(1 for v in hf if v >= 100)
    return out


def parse_worst_path_structure(report: str, top_n: int = 10) -> dict:
    """Parse the `Logical Path` column of `report_design_analysis -timing`: WHAT the worst
    path is physically made of, and how much of the top-N shares that structure.

    Why this exists: logic-vs-route % and logic-level COUNT say how much delay is logic, but
    not what KIND. That distinction decides the lever. A path with 9 levels of CARRY8 is an
    adder chain -- place-and-route cannot shorten it and the ceiling is structural. A path with
    9 levels of LUTs is a deep cone -- restructuring can. And a shallow path ending at a memory
    enable pin is a broadcast problem -- replication is the lever, not placement.

    It must be PATH-SCOPED. Design-wide counts mislead: one design in this family carries
    CARRY8 cells and 10 logic levels, yet its worst path is a 12288-load write-enable
    broadcast with the carry nowhere on it. Reading `report_carry_chains` would have called it
    an arithmetic wall and sent the planner at the wrong lever entirely.

    The column looks like:
        FDRE/C-(185)-LUT3-(34)-LUT5-(207)-LUT5-(369)-LUT4-(203)-LUT5-(8)-RAMB18E2/ENBWREN
    i.e. cell types separated by '-', with (N) the fanout of the net driving the next cell.
    So the same string yields both the cell mix AND the per-hop fanouts -- the largest hop is
    the fanout that is actually ON the critical path, which is not the same as the design's
    largest net.

    Returns {} if the report has no path rows (e.g. an empty or unavailable report).
    """
    out = {"path": None, "cells": {}, "carry8": 0, "dsp": 0, "muxf": 0, "lutram": 0,
           "max_hop_fanout": 0, "paths_with_carry": 0, "paths_with_dsp": 0,
           "paths_with_mem_endpoint": 0, "n_paths": 0}
    rows = re.findall(r"^\|\s*Path #\d+\s*\|.*$", report, re.M)
    if not rows:
        return {}
    # Column 13 (0-indexed after split on '|') is `Logical Path`; see the header row of
    # `report_design_analysis -timing`. Same pipe layout as parse_worst_path_delay_split.
    LOGICAL_COL = 13
    for i, r in enumerate(rows[:top_n]):
        cc = [x.strip() for x in r.split("|")]
        if len(cc) <= LOGICAL_COL:
            continue
        lp = cc[LOGICAL_COL]
        if not lp or lp.lower() == "none":
            continue
        out["n_paths"] += 1
        # Cell tokens are the '-'-separated fields that are not a bare (fanout) group.
        toks = [t for t in lp.split("-") if t and not re.fullmatch(r"\(\d+\)", t)]
        # A token may carry a pin suffix (FDRE/C, RAMB18E2/ENBWREN) -- the type is before '/'.
        types = [t.split("/")[0] for t in toks]
        has_carry = any(t.startswith("CARRY") for t in types)
        has_dsp = any(t.startswith("DSP") for t in types)
        out["paths_with_carry"] += has_carry
        out["paths_with_dsp"] += has_dsp
        if re.search(r"(RAMB|URAM|RAMS|RAMD)", types[-1] if types else ""):
            out["paths_with_mem_endpoint"] += 1
        if i == 0:                                   # the worst path itself
            out["path"] = lp
            counts = {}
            for t in types:
                counts[t] = counts.get(t, 0) + 1
            out["cells"] = counts
            out["carry8"] = sum(v for k, v in counts.items() if k.startswith("CARRY"))
            out["dsp"] = sum(v for k, v in counts.items() if k.startswith("DSP"))
            out["muxf"] = sum(v for k, v in counts.items() if k.startswith("MUXF"))
            out["lutram"] = sum(v for k, v in counts.items()
                                if k.startswith(("RAMS", "RAMD", "SRL")))
            hops = [int(x) for x in re.findall(r"\((\d+)\)", lp)]
            out["max_hop_fanout"] = max(hops) if hops else 0
    return out


def format_worst_path_structure(st: dict) -> list:
    """Render parse_worst_path_structure as FACT lines for the diagnosis. No verdicts."""
    if not st or not st.get("path"):
        return []
    lines = []
    mix = ", ".join(f"{k}x{v}" if v > 1 else k
                    for k, v in sorted(st["cells"].items(), key=lambda kv: -kv[1]))
    lines.append(f"  Worst-path cells: {mix}")
    if st.get("max_hop_fanout"):
        lines.append(f"  Largest fanout ON the worst path: {st['max_hop_fanout']} loads "
                     f"(this is the on-path fanout, not the design's largest net)")
    n = st.get("n_paths", 0)
    if n:
        bits = []
        if st.get("paths_with_carry"):
            bits.append(f"{st['paths_with_carry']}/{n} carry")
        if st.get("paths_with_dsp"):
            bits.append(f"{st['paths_with_dsp']}/{n} DSP")
        if st.get("paths_with_mem_endpoint"):
            bits.append(f"{st['paths_with_mem_endpoint']}/{n} end at a memory pin")
        if bits:
            lines.append("  Top paths sharing that structure: " + ", ".join(bits))
    return lines


def parse_high_fanout_nets(report: str) -> list:
    """Parse `report_high_fanout_nets`: the per-net rows under the
    'Paths / Fanout / Parent Net Name' header, as (net_name, fanout, path_count).

    Keeps only real net names (contain '/', not a get_*/ERROR/WARNING echo). Pure:
    text in, list out -- moved here from maya.py so all report parsing has one home.
    """
    nets = []
    in_net_section = False
    for line in report.split("\n"):
        if "Paths" in line and "Fanout" in line and "Parent Net Name" in line:
            in_net_section = True
            continue
        if not in_net_section:
            continue
        if line.startswith("---") or not line.strip():
            continue
        if line.startswith("==="):
            break
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            path_count = int(parts[0])
            fanout = int(parts[1])
            net_name = parts[2]
        except ValueError:
            continue
        if (net_name and "/" in net_name
                and not net_name.startswith("get_")
                and not net_name.startswith("ERROR")
                and not net_name.startswith("WARNING")):
            nets.append((net_name, fanout, path_count))
    return nets


def worst_path_split_is_empty(split: dict) -> bool:
    """True when parse_worst_path_delay_split extracted no usable field. The signal
    for maya to fall back to handing the agent the RAW report section to read itself,
    rather than silently dropping the diagnosis. hf_max/hf_over default to 0 even on a
    total miss, so they are excluded -- only the four primary fields count."""
    if not split:
        return True
    return all(split.get(k) is None
               for k in ("logic_pct", "net_pct", "logic_levels", "endpoint"))


def parse_congestion_level(report: str) -> dict:
    """Parse `report_design_analysis -congestion`: the max congestion Level found.
    Level >=6 means routing is genuinely oversubscribed. Returns {max_level, congested}."""
    levels = [int(x) for x in re.findall(
        r"\|\s*(?:North|South|East|West|Global)\s*\|[^|]*\|\s*(\d+)\s*\|", report)]
    mx = max(levels) if levels else 0
    return {"max_level": mx, "congested": mx >= 6}


def parse_qor_assessment(report: str) -> dict:
    """Parse `report_qor_assessment`: overall score, DONT_TOUCH count (the LEVEL-0
    blocker), and number of paths above Max Net/LUT budgeting."""
    out = {"score": None, "dont_touch": 0.0, "budget_paths": None, "lut_combined": None}
    m = re.search(r"QoR Assessment Score\s*\|\s*([0-9])\s*-\s*([^|]+)", report)
    if m:
        out["score"] = f"{m.group(1)} ({m.group(2).strip()})"
    m = re.search(r"DONT_TOUCH \(cells/nets\)\s*\|\s*\d+\s*\|\s*([0-9.]+)", report)
    if m:
        out["dont_touch"] = float(m.group(1))
    m = re.search(r"above Max Net/LUT Budgeting\s*\|\s*\d+\s*\|\s*([0-9.]+)", report)
    if m:
        out["budget_paths"] = m.group(1)
    # "Max LUT Combined" — the S1 (uncombine) indicator. Real table row is
    #   | * Max LUT Combined | <threshold> | <actual%> | <used> | <avail> | | REVIEW |
    # so grab the 2nd number (actual %) and the trailing REVIEW/OK/WARNING status.
    m = re.search(r"Max LUT Combined\s*\|\s*[0-9.]+\s*\|\s*([0-9.]+)\s*\|.*?\|\s*"
                  r"(REVIEW|OK|WARNING)\s*\|", report, re.IGNORECASE)
    if m:
        out["lut_combined"] = f"{m.group(1).strip()}% ({m.group(2).strip()})"
    return out


def parse_lut_utilization(report: str) -> Optional[float]:
    """Parse the CLB LUTs Util% from `report_utilization`."""
    m = re.search(r"\|\s*CLB LUTs\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*([0-9.]+)",
                  report)
    return float(m.group(1)) if m else None


def parse_device_utilization(report: str) -> dict:
    """Extract device fill % per resource from `report_utilization` — LUT / FF / occupied
    CLB-slices / BRAM / DSP / URAM. Slice occupancy is the truest "how full is the device"
    number for how full the device really is (LUT% badly understates it). Missing rows -> None."""
    def pct(name):
        # columns: | name | Used | Fixed | Prohibited | Available | Util% |.  Used can be a
        # decimal (e.g. a FIFO uses 24.5 Block RAM tiles), so allow [0-9.] in the count columns.
        m = re.search(r"\|\s*" + name + r"\s*\|\s*[0-9.]+\s*\|\s*[0-9.]+\s*\|\s*[0-9.]*\s*\|\s*[0-9.]+\s*\|\s*(<?[0-9.]+)",
                      report)
        if not m:
            return None
        v = m.group(1)
        return 0.0 if v.startswith("<") else float(v)
    return {
        "LUT":    pct(r"CLB LUTs"),
        "FF":     pct(r"CLB Registers"),
        "slices": pct(r"CLB"),             # occupied CLB slices (the real fullness metric)
        "BRAM":   pct(r"Block RAM Tile"),
        "DSP":    pct(r"DSPs"),
        "URAM":   pct(r"URAM"),
    }


def format_device_utilization(du: dict) -> Optional[str]:
    """One-line device-fill string, e.g. 'LUT 73%, FF 30%, slices 95%, BRAM 53%, DSP 9%'.
    LUT/FF/slices always shown; BRAM/DSP/URAM only when present (>0) to avoid noise."""
    if not du:
        return None
    parts = []
    for k, lbl, always in [("LUT", "LUT", True), ("FF", "FF", True), ("slices", "slices", True),
                           ("BRAM", "BRAM", False), ("DSP", "DSP", False), ("URAM", "URAM", False)]:
        v = du.get(k)
        if v is None:
            continue
        if always or v >= 0.5:          # drop sub-0.5% BRAM/DSP/URAM (would round to "0%" noise)
            parts.append(f"{lbl} {v:.0f}%")
    return ", ".join(parts) if parts else None


def parse_top_hierarchies(util_hier: str, top_n: int = 4) -> list:
    """Top modules by Total-LUTs from `report_utilization -hierarchical` (which module is heavy
    -> where the logic actually sits). Skips the near-total wrapper/root rows. Returns [(instance, luts), ...]."""
    rows = []
    for ln in util_hier.splitlines():
        m = re.match(r"\|\s*(\S[^|]*?)\s*\|\s*[^|]*\|\s*(\d+)\s*\|", ln)
        if not m:
            continue
        inst = m.group(1).strip()
        if inst.startswith("(") or inst.lower() == "instance":
            continue
        rows.append((inst, int(m.group(2))))
    if not rows:
        return []
    maxlut = max(l for _, l in rows)
    # drop whole-design wrappers (>=85% of the max), then take the heaviest real sub-modules
    subs = [(i, l) for i, l in rows if l < 0.85 * maxlut and l > 0]
    subs.sort(key=lambda r: -r[1])
    return subs[:top_n]


# Clock-tree / global-buffer primitives sit on EVERY timing path — exclude them so the worst-path
# module signal reflects logic, not the clock network.
_CLOCK_CELL_RE = re.compile(r"(?i)(bufg|ibuf|obuf|mmcm|inbuf|ibufctrl|\bpll\b|bufgce|bufgctrl)")


def parse_crit_hierarchies(paths: list, max_depth: int = 3, top_n: int = 3) -> list:
    """Most common module prefixes across the worst-path cells -> WHICH module instances the worst
    paths run through. `paths` is a list of cell-name lists. Drops the leaf cell and caps the
    hierarchy depth (adapts to shallow and deeply-nested designs); skips clock-tree cells.
    Returns [(module_prefix, n_paths), ...]."""
    from collections import Counter
    c = Counter()
    for path in paths:
        seen = set()
        for cell in path:
            cs = str(cell)
            if _CLOCK_CELL_RE.search(cs):
                continue
            parts = cs.split("/")
            if len(parts) <= 1:
                pref = parts[0]
            else:
                pref = "/".join(parts[:min(len(parts) - 1, max_depth)])  # drop leaf, cap depth
            seen.add(pref)
        for p in seen:
            c[p] += 1
    return c.most_common(top_n)


# --- Extended-report checks: each returns a FACT string if abnormal, else None ---
def extract_qor_suggestions(text: str, max_entries: int = 8) -> list:
    """Return the RQS suggestion rows verbatim, INCLUDING wrapped-description
    continuation rows. Vivado wraps a long Description cell onto one or more extra
    table rows whose id cells are empty; those belong to the suggestion above them and
    must be kept. Path-detail rows in later sections (which reference an RQS id but have
    no 'Generated' status) are ignored.

    Groups rows into entries: an entry starts at a row containing 'RQS_' and 'Generated',
    and absorbs every following table row until the next such header, a table separator
    ('+...'), or a non-table line. Returns the lines of the first `max_entries` entries.
    """
    entries, cur = [], None
    for ln in text.splitlines():
        st = ln.rstrip()
        if not st.lstrip().startswith("|"):          # separator / blank / prose -> close
            if cur is not None:
                entries.append(cur); cur = None
            continue
        if "RQS_" in ln and "Generated" in ln:       # new suggestion header row
            if cur is not None:
                entries.append(cur)
            cur = [st]
        elif cur is not None:                         # continuation row of current entry
            cur.append(st)
    if cur is not None:
        entries.append(cur)
    out = []
    for e in entries[:max_entries]:
        out.extend(e)
    return out


def check_complexity(t: str):
    """Max Rent exponent from `report_design_analysis -complexity`.

    PRECISION-FIRST (facts-to-LLM layer). Rent is column 2 of the Complexity
    Characteristics table: `| Module | Rent | Average Fanout | ... |`. The old form
    scanned EVERY pipe cell for a 0.xx/1.xx float, so it read any fraction as a Rent
    -- confirmed against real xcvu3p 2025.2 output, where handing it the utilization
    report (the MCP transport-desync case) made it report a BRAM utilization of 0.83
    as `max Rent 0.83`. A wrong fact stated to the agent is worse than none, so this
    now reads ONLY the Rent column and only when the report is really the complexity
    report (the `Rent` header is absent from the -timing / -congestion variants and
    from report_utilization)."""
    if "Rent" not in t:
        return None
    rents = []
    for ln in t.splitlines():
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) > 3 and re.fullmatch(r"[01]\.\d+", cells[2] or ""):
            rents.append(float(cells[2]))
    hi = max(rents) if rents else 0.0
    return f"max Rent {hi:.2f} (>=0.65)" if hi >= 0.65 else None

def check_control_sets(t: str):
    m = re.search(r"Total control sets\s*\|\s*(\d+)", t)
    n = int(m.group(1)) if m else 0
    return f"{n} control sets (>5000)" if n > 5000 else None

def check_clock_interaction(t: str):
    return ("non-clean clock crossings present"
            if re.search(r"\b(Unconstrained|Partial False Path|No Common|Unsafe|Async)\b", t) else None)

def check_ram_util(t: str):
    u = [float(x) for x in re.findall(
        r"\|\s*(?:BlockRAM|URAM|Block RAM Tile)\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*([0-9.]+)\s*\|", t)]
    hi = max(u) if u else 0.0
    return f"{hi:.0f}% BRAM/URAM fill" if hi >= 50.0 else None

def check_carry_chains(t: str):
    """Longest carry chain, in CLBs, from `report_carry_chains`.

    PRECISION-FIRST. The length is the max "Height (CLBs)" in the Utilization table
    (`| Starting Instance | Height (CLBs) | Carry Height Utilization |`). The Summary
    table one section up has the SAME `| text | int | pct% |` shape, and its "Average
    Carry Chain Height" column would otherwise be read as a chain length -- so count
    only rows whose first cell is an instance NAME (contains a letter). Anchored on the
    Utilization header; confirmed against real xcvu3p 2025.2 output."""
    if "Height (CLBs)" not in t:
        return None
    heights = []
    for ln in t.splitlines():
        cells = [c.strip() for c in ln.split("|")]
        if (len(cells) > 3 and re.search(r"[A-Za-z]", cells[1] or "")
                and (cells[2] or "").isdigit() and (cells[3] or "").endswith("%")):
            heights.append(int(cells[2]))
    longest = max(heights) if heights else 0
    return f"carry chain {longest} CLBs (~{longest*8} bits)" if longest >= 8 else None


# Cheap subset of CORE_REPORTS re-run between turns to refresh the model's view of
# the design. Deliberately excludes report_qor_assessment and
# report_qor_suggestions: they are the slow ML-backed ones, and their output is
# about the design's character rather than its current state, so it ages slowly.
# design-analysis-timing + congestion come from ONE combined call (see
# DESIGN_ANALYSIS_COMBINED); this list holds only what still needs its own command.
REFRESH_REPORTS = [
    ("utilization",            "report_utilization"),
]
# Do not spend the endgame on reporting: below this FRACTION of the budget
# remaining, a refresh is skipped in favour of letting the model act.
#
# Expressed as a fraction, not seconds. It was 600 s absolute, which silently
# disabled the refresh for an ENTIRE 10-minute run (600 s budget => remaining
# < 600 from the first second). The model then had no measured state at all and
# compensated by calling get_wns after every lever -- 9 times in 21 calls. A
# threshold that scales with the budget cannot fail that way.
REFRESH_MIN_REMAINING_FRAC = 0.15
# On a heavy design the full bundle measured 3-5 minutes, and those designs get
# only a handful of LLM turns in the hour, so refreshing every turn would cost
# more than the staleness does. Refresh at most this often there.
REFRESH_HEAVY_MIN_TURNS = 4

# The ::opt:: Tcl recipe library, VENDORED into context/ from the ruthwik branch.
# It is what makes a script-emitting planner possible at all: keep-best/revert lives in
# ::opt::try_keep_if_better, in Tcl, so a directive sweep is a foreach the model can write
# — where today it is Python orchestration inside the MCP server that no Tcl script can
# reach. Sourcing vivado_opt_postroute.tcl pulls in the core (it sources its sibling).
RECIPES_DIR = Path(__file__).resolve().parent / "context"
RECIPE_LIB = RECIPES_DIR / "vivado_opt_postroute.tcl"

HEAVY_DCP_MB = 100.0        # DCP file size (MB) at/above which the design is heavy
HEAVY_OP_SECONDS = 60.0     # open_checkpoint OR first report_timing time (s) above which it is heavy


def assess_dcp_weight(input_dcp) -> tuple[bool, Optional[float], Optional[str]]:
    """Static (pre-open) heaviness check from the DCP file size. Returns
    (is_heavy, size_mb, reason). Best-effort — returns (False, None, None) if the size
    can't be read."""
    try:
        mb = Path(input_dcp).stat().st_size / (1024.0 * 1024.0)
    except Exception:
        return (False, None, None)
    if mb >= HEAVY_DCP_MB:
        return (True, mb, f"DCP {mb:.0f} MB >= {HEAVY_DCP_MB:.0f} MB")
    return (False, mb, None)


# ---------------------------------------------------------------- design-analysis merge
# report_design_analysis accepts -timing/-congestion/-complexity in ONE invocation and
# emits every requested section, so the three separate calls that used to sit in
# CORE_REPORTS / EXTENDED_CHECKS / REFRESH_REPORTS are now one. Measured on
# rosetta_digit-recognition: 6469 ms separate -> 2008 ms combined; the sections are
# byte-identical and Router Initial Congestion (present only on congested designs)
# survives the merge.
#
# THE COMBINED TEXT MUST BE SPLIT BEFORE PARSING -- never handed to a parser whole.
# check_complexity scans pipe cells for a Rent exponent, and the -timing table's
# `Requirement` column holds values like 1.570 that match its number shape, so the
# unsplit report yields a FABRICATED "max Rent 1.57 (>=0.65)" fact. Verified on
# vexriscv_re-place and rosetta_digit-recognition: split -> None (correct), whole ->
# 1.57 / 1.70 (invented). This is the exact mis-parse check_complexity's own docstring
# warns about, reached by a new route.
DESIGN_ANALYSIS_COMBINED = "report_design_analysis -timing -max_paths 10 -congestion -complexity"

# Which section titles from the combined report feed which raw-report key. Matched
# case-insensitively as substrings, in order; a key collects every section it matches
# (congestion has two on congested designs: Placer Final Level + Router Initial).
DESIGN_ANALYSIS_SECTIONS = [
    ("design-analysis-timing", ("Setup Path Characteristics",)),
    ("congestion",             ("Congestion",)),
    ("complexity",             ("Complexity Characteristics",)),
]

_DA_SECTION_HDR = re.compile(r"^(\d+)\.\s+(\S.*?)\s*$", re.M)


def split_design_analysis_sections(text: str) -> dict:
    """Split a combined `report_design_analysis` into {section_title: section_text}.

    Vivado prints each section title TWICE -- once in the Table of Contents (a bare
    line) and once as the section's own heading (followed by the body). Longest-body
    wins, which discards the one-line TOC entry without having to model the TOC.

    Returns {} when nothing matches, so the caller can fall back to separate calls
    rather than silently parse an empty string.
    """
    if not text:
        return {}
    marks = list(_DA_SECTION_HDR.finditer(text))
    out: dict = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        title, body = m.group(2), text[m.start():end]
        if len(body) > len(out.get(title, "")):
            out[title] = body
    return out


def design_analysis_raw(text: str) -> dict:
    """Combined-report text -> {raw-report key: section text}, using the same keys the
    three separate calls used to produce. Keys whose sections are absent are omitted,
    NOT set to "" -- an absent key is visible to the caller, an empty one silently
    parses to "no finding"."""
    sections = split_design_analysis_sections(text)
    out = {}
    for key, wants in DESIGN_ANALYSIS_SECTIONS:
        body = "".join(v for k, v in sections.items()
                       if any(w.lower() in k.lower() for w in wants))
        if body:
            out[key] = body
    return out


CORE_REPORTS = [
    ("qor-assessment",         "report_qor_assessment"),
    ("qor-suggestions",        "report_qor_suggestions"),
    ("utilization",            "report_utilization"),
]

# Extended robustness reports. (key, tcl-command, parser-or-None, multi_clock_only)
# parser=None means the report is captured (for raw mode) but not parsed into a fact.
# tcl=None means the text is ALREADY captured by an earlier step (the combined
# design-analysis report, or the ship gate's route-status read) -- the entry stays so
# _parse_extended still turns it into a fact, but gather runs no command for it.
#
# NO VALIDITY CHECKS HERE -- report_route_status, report_methodology and report_drc were
# REMOVED on 2026-08-10. Two independent reasons, either sufficient:
#
#   1. The contest input is a fully placed and routed, valid DCP by definition
#      (docs/details.md), so these could only ever confirm what is already known. Across
#      all 12 benchmarks of runs/benchmark_logs_20260809_002643 they produced a fact
#      ZERO times, at a cost of 114 s (29 s + 12 s of it on ispd16 alone).
#   2. This bundle runs ONCE, pre-LLM, and is NOT in REFRESH_REPORTS -- so it never saw a
#      mutated design in the first place. There was no post-mutation coverage to lose.
#
# Validity is enforced where it can actually fail, and all of it is untouched by this:
# _ship_gate reads report_route_status itself and fails closed on every promotion,
# _verify_snapshot_file re-opens and re-gates the snapshot file at finalize, and
# measure_state reads route status every refresh turn. If you want a pre-LLM validity
# check back, put it in REFRESH_REPORTS where it would see a changed design -- adding it
# here just re-measures the input.
#
# Extended robustness reports. (key, tcl-command, parser-or-None, multi_clock_only)
# parser=None means the report is captured (for raw mode) but not parsed into a fact.
# tcl=None means the text is ALREADY captured by an earlier step (the combined
# design-analysis report) -- the entry stays so _parse_extended still turns it into a
# fact, but gather runs no command for it.
EXTENDED_CHECKS = [
    ("util-hier",         "report_utilization -hierarchical -hierarchical_depth 2",  None,                    False),
    ("complexity",        None,                                                      check_complexity,        False),
    # NOT -verbose: check_control_sets reads one "Total control sets" line from the
    # summary table, and -verbose added ~72 KB of per-set detail that nothing parsed.
    ("control-sets",      "report_control_sets",                                     check_control_sets,      False),
    ("clock-interaction", "report_clock_interaction",                                check_clock_interaction, True),
    ("clock-util",        "report_clock_utilization",                                None,                    True),
    ("ram-util",          "report_ram_utilization",                                  check_ram_util,          False),
    ("carry-chains",      "report_carry_chains",                                     check_carry_chains,      False),
]


# ---------------------------------------------------------------- classification
# Not a parser: takes ALREADY-PARSED report values and returns a judgement about
# the design. It lives here because this module is the one home for the whole
# report phase -- parse, derive, render -- not because it reads text.

def classify_design(split: dict, cong: dict, qor: dict,
                    lut_pct: Optional[float], slices: Optional[float] = None) -> dict:
    """Classify the design per the logic-vs-wire taxonomy. Returns
    {category, rationale, notes}. `rationale` is facts; `notes` are actionable verdicts
    DERIVED from those facts (which lever the numbers point to) — the render marks them as
    derived, and the prompt's measured guidance outranks them on conflict. The LLM decides.

    LEANING IS DRIVEN BY THE DELAY SPLIT, NOT THE LOGIC-LEVEL COUNT. A path with many
    logic levels can still be WIRE-dominated — most of its delay is routing *between* the
    levels — in which case better PLACEMENT (not retiming) is the lever. Calling such a path
    "logic-leaning" just because it has many levels sends the LLM to retiming/"floor" when a
    placement-directive sweep would have recovered slack (see the spam-filter / vexriscv_v2
    Ralph-loop findings: 53-56% wire, high detour, +19-25 MHz from ExtraPostPlacementOpt /
    ExtraNetDelay_high after Explore plateaued).
    """
    nd = split.get("net_pct") or 0
    ld = split.get("logic_pct") or 0
    lv = split.get("logic_levels") or 0
    dt = qor.get("dont_touch") or 0.0
    rationale = []
    notes = []

    # PRECISION-FIRST: the leaning verdict and every actionable note below are derived
    # from the worst-path delay split. If that parse produced nothing (empty report, or
    # a layout this parser could not read), emit NO steering opinion -- an actionable
    # note built on a failed parse ("re-placement is the lever") is a wrong instruction
    # to the agent, worse than silence. maya falls back to handing over the raw report.
    if worst_path_split_is_empty(split):
        return {"category": "unknown (worst-path delay split not parsed)",
                "rationale": ["report_design_analysis -timing did not yield a delay split"],
                "notes": []}

    deep = lv >= 20
    congested = cong.get("congested")
    cong_lvl = cong.get("max_level", 0)
    fill = slices if slices is not None else lut_pct  # occupied-slice % preferred; LUT% fallback

    if nd >= 60 and deep:
        category = f"COMPOUND (wire + deep logic, {lv} levels)"
        rationale.append(f"{nd}% wire delay and {lv} logic levels on the worst path")
    elif nd >= 50:
        # Wire-dominated (route is at least half the delay) — regardless of level count.
        category = "WIRE-bound" + (f" (with {lv} logic levels)" if lv >= 10 else "")
        rationale.append(f"{nd}% wire / {ld}% logic on the worst path"
                         + (f", {lv} levels" if lv else ""))
    elif ld >= 50 or deep:
        category = "LOGIC-bound" + (" (deep)" if deep else "")
        rationale.append(f"{ld}% logic / {nd}% wire, {lv} levels")
    else:
        category = "mixed"
        rationale.append(f"{ld}% logic / {nd}% wire" + (f", {lv} levels" if lv else ""))

    if congested:
        rationale.append(f"congestion Level {cong_lvl} (>=6 = oversubscribed)")

    # DONT_TOUCH is a FACT, not a gate, and not a category. It used to early-return a
    # "BLOCKED" category that suppressed the delay-split classification entirely and told
    # the agent to clear the locks first -- measured false: stripping them is worth EXACTLY
    # 0.00 MHz on any re-placed chain (356.63 vs 356.63 after a plain re-place, 414.59 vs
    # 414.59 on the best chain, bit-identical), and +6.0 MHz only when phys_opt is applied
    # to the GOLDEN placement. A full re-place rebuilds what the lock was protecting. See
    # SYSTEM_PROMPT.TXT section 7; keep the two wordings in step.
    if dt and dt > 1000:
        rationale.append(
            f"{int(dt)} DONT_TOUCH cells/nets — irrelevant to a full re-place (measured "
            "0.00 MHz delta on every re-placed chain); matters only if you optimize the "
            "EXISTING placement")

    # --- Actionable verdicts -------------------------------------------------------------
    # PLACEMENT-IMPROVABLE: the worst path is wire-dominated, so re-placement (not retiming)
    # recovers slack. On an un-congested device it is safe to chase; and Explore is only ONE
    # placement point — sweep the alternative directives before ever calling it a floor.
    if nd >= 50:
        hint = f"worst path is {nd}% wire"
        if fill is not None:
            hint += f", device ~{fill:.0f}% full"
        v = (f"PLACEMENT-IMPROVABLE ({hint}): re-placement, NOT retiming, is the lever "
             "— the wire-heavy split above and the critical-path spread are the evidence "
             "(a stretched cone carrying mostly wire delay is a placement problem, not logic).")
        if not congested and cong_lvl <= 3:
            v += (" Device is not congested, so this is safe to push: if "
                  "place_design -directive Explore plateaus, SWEEP alternative placement "
                  "directives — ExtraTimingOpt, ExtraPostPlacementOpt, ExtraNetDelay_high — "
                  "keeping the best. Do NOT conclude a logic floor after only trying Explore.")
        notes.append(v)
    elif ld >= 50 and lv >= 10:
        # Genuinely logic-depth-bound: logic delay dominates AND the path is deep.
        notes.append(f"LOGIC-depth-bound ({ld}% logic, {lv} levels): placement has little "
                     "headroom — use retiming (phys_opt AlternateFlowWithRetiming / AddRetime). "
                     "A DSP/BRAM/CARRY hard-macro cascade on this path is the irreducible RTL "
                     "floor; stop iterating on it once confirmed.")

    # NOTE: a LUTRAM worst-path endpoint (RAMD32/RAMS64/…) is NOT a reliable trigger for the scoped
    # surgical levers (S1 uncombine / S2 scoped re-place). Measured: it helped only spam-filter
    # (which already reaches 510 via its own indicators); on 3d-rendering and vexriscv the surgical
    # levers tie-or-regress AND steal early budget from the phys_opt chain / full re-place that
    # actually win — 3d regressed 289->277 when steered to S1/S2 early. So we deliberately do NOT
    # emit a STEP-5 note off the endpoint primitive; STEP 5 stays gated by its existing in-prompt
    # indicators (a genuine "Max LUT Combined" congested-DOT hierarchy).

    return {"category": category, "rationale": rationale, "notes": notes}


# ---------------------------------------------------------------- rendering
# The RENDER half of the report phase (parse -> derive -> RENDER). Pure: already-
# gathered facts in, the digest STRING the LLM reads out. Moved here from maya's
# _build_summary/_build_raw_summary so the whole deterministic transformation lives
# in one place; maya keeps only the I/O that gathers the inputs.

def achievable_fmax(wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
    """Achievable fmax (MHz) = 1000 / (clock_period - WNS). None if not computable.
    Mirrors DCPOptimizerBase.calculate_fmax so the render stays behaviour-identical."""
    if clock_period is None or clock_period <= 0 or wns is None:
        return None
    period = clock_period - wns
    return 1000.0 / period if period > 0 else None


def render_raw(raw_reports: dict) -> str:
    """Raw mode: dump every captured report verbatim, unparsed."""
    s = ["=== Initial Design Analysis (RAW REPORTS) ===",
         "Complete Vivado reports below, unparsed — interpret them yourself.\n"]
    for key, txt in raw_reports.items():
        s.append("\n" + "=" * 72)
        s.append(f"REPORT: {key}")
        s.append("=" * 72)
        s.append(txt.strip() if txt.strip() else "(empty / unavailable for this design)")
    return "\n".join(s)


DERIVED_READ_LEGEND = (
    "  Plain lines and '·' lines are MEASURED FACTS about this design. Lines marked '=>' are\n"
    "  DERIVED READS: inferences from those facts about which lever the numbers point to, not\n"
    "  measurements. Weigh them — and where one CONTRADICTS sections 5-7 of the system prompt,\n"
    "  the PROMPT WINS: its claims are measured across the whole benchmark corpus, a derived\n"
    "  read only ever saw this design.")


def scattered_cone_recommendation(split: dict, detour_candidates: list,
                                  spread: Optional[dict], col_span, cong: dict) -> str:
    """When the golden signals show a SCATTERED CRITICAL CONE, recommend the selective
    cone re-place EARLY (rather than after a slow full-re-place plateau).

    The signature — worst path wire-heavy, high routing detour, critical cone stretched
    across the die, and NOT congestion-locked — physically means the worst-path cells are
    placed far from where they belong with room to move, which is exactly what
    ::opt::replace_critical_cone targets. Keyed to signals, not to any design. Returns the
    recommendation line, or "" when the signature does not hold. Pure.
    """
    wire = (split or {}).get("net_pct")
    worst_detour = detour_candidates[0][1] if detour_candidates else None
    if wire is None or worst_detour is None:
        return ""
    max_dist = (spread or {}).get("max_distance")
    span = (col_span[1] - col_span[0]) if col_span else None
    stretched = (max_dist is not None and max_dist >= 100) or (span is not None and span >= 100)
    if wire >= 45 and worst_detour >= 3.0 and stretched and not (cong or {}).get("congested"):
        bits = [f"detour {worst_detour:.0f}x", f"{wire}% wire"]
        if max_dist is not None:
            bits.append(f"spread {max_dist} tiles")
        if span is not None:
            bits.append(f"critical cells across ~{span} columns")
        return ("  => SCATTERED CRITICAL CONE (" + ", ".join(bits) + ", low congestion): the "
                "worst-path cells are placed far from where they belong, with room to move. The "
                "targeted lever is a SELECTIVE cone re-place — make it your VERY FIRST move, on THIS "
                "original design, e.g. ::opt::try_keep_if_better {::opt::replace_critical_cone 100} "
                "then a ::opt::safe_phys_opt -directive Explore. It re-places ONLY the worst-path "
                "cells and ends fully routed. Do NOT do a full re-place first: this detour/spread is "
                "a property of the ORIGINAL placement, and a full re-place lands in a different basin "
                "where the cone recovers far less (measured ~415 MHz from the original vs ~356 after "
                "a full re-place). Cone FIRST, then top-ups.")
    return ""


def render_analysis(*, diag: dict, diagnosis: Optional[dict], raw_reports: dict,
                    heavy: bool, dcp_size_mb: Optional[float], heavy_reason: str,
                    struct: dict, detour_candidates: list,
                    extended_findings: list, extended_checked: list,
                    clock_period: Optional[float], initial_wns: Optional[float],
                    initial_tns: Optional[float], initial_failing_endpoints: Optional[int],
                    spread: Optional[dict], high_fanout_nets: list) -> str:
    """Parsed mode: the measured FACTS, plus signal-derived reads marked as derived.

    The facts (category, delay split, congestion, fill, structure, detour, timing) are
    reported straight. The `=>` notes are INFERENCES from those facts about which lever
    the numbers point to; they are rendered under a header that says so and that gives
    the model a precedence rule -- the prompt's corpus-measured guidance wins on conflict.
    Do not describe this function as facts-only: it emits both, and an earlier docstring
    claiming otherwise is how a derived read came to outrank a measured one silently.

    Pure render of the initial deterministic analysis. Every input is already gathered
    (maya runs the reports + RapidWright analyses and passes the results in); this only
    turns them into the digest text.
    """
    split, cong, qor, lut = (diag.get("split", {}), diag.get("cong", {}),
                             diag.get("qor", {}), diag.get("lut"))
    s = ["=== Initial Design Analysis ==="]
    derived_legend_at = None  # index of the reserved '=>' legend slot; resolved at the end

    if heavy:
        sz = f"{dcp_size_mb:.0f} MB" if dcp_size_mb else "large"
        s.append(f"\n*** LARGE DESIGN ({sz}; {heavy_reason}). A full place+route may take much of "
                 "the hour, and nothing banks until a script returns — so your FIRST script must be "
                 "a SINGLE reduced-effort re-place to measure the cost: place_design -unplace; "
                 "place_design -directive RuntimeOptimized; route_design -directive Explore. "
                 "Do NOT start with phys_opt (a re-place discards it), and do NOT batch several "
                 "directives into one script — the harness reports the real cost, then plan from it. "
                 "One banked gain beats an unfinished sweep. ***")

    if diagnosis:
        s.append("\nDESIGN DIAGNOSIS — you decide the strategy.")
        # The '=>' legend, only emitted if a '=>' line actually fires. It cannot be gated on
        # diagnosis["notes"]: '=>' lines are also produced by the LUT-combined, detour and
        # scattered-cone branches further down, any of which can fire with notes empty. So
        # reserve the slot here and resolve it against the FINISHED body at the end.
        derived_legend_at = len(s)
        s.append(DERIVED_READ_LEGEND)
        s.append(f"  Category: {diagnosis['category']}")
        wp = []
        if split.get("logic_pct") is not None:
            wp.append(f"worst path {split['logic_pct']}% logic / {split.get('net_pct')}% wire")
        if split.get("logic_levels") is not None:
            wp.append(f"{split['logic_levels']} logic levels")
        if split.get("endpoint"):
            wp.append(f"ends at {split['endpoint']}")
        if wp:
            s.append("  Delay split: " + ", ".join(wp))
        elif worst_path_split_is_empty(split):
            # The positional delay-split parser read nothing. Rather than drop the
            # diagnosis silently, hand the agent the raw report to read itself (it
            # reads a Vivado timing table fine) -- bounded so it cannot dominate the
            # prompt. Preferred over bulletproofing the parser: the agent is a
            # capable reader, which is exactly what a code consumer would not be.
            raw_da = (raw_reports.get("design-analysis-timing") or "").strip()
            if raw_da:
                excerpt = raw_da[:2000]
                s.append("  Delay split: could not be parsed — raw "
                         "report_design_analysis -timing below, read it directly:")
                for ln in excerpt.splitlines():
                    s.append(f"    {ln}")
                if len(raw_da) > len(excerpt):
                    s.append(f"    ... (+{len(raw_da) - len(excerpt)} more bytes truncated)")
        if split.get("hf_max"):
            s.append(f"  High-fanout: up to {split['hf_max']} loads on "
                     f"{split.get('hf_over', 0)} of the top paths")
        if cong:
            s.append(f"  Congestion: max Level {cong.get('max_level', 0)}"
                     + (" (>=6 oversubscribed)" if cong.get('congested') else ""))
        devfill = format_device_utilization(diag.get("devutil"))
        if devfill:
            s.append(f"  Device fill: {devfill}  (occupied CLB-slices % is the real fullness "
                     f"— this is the real fullness; LUT% understates it)")
        elif lut is not None:
            s.append(f"  Utilization: {lut:.1f}% CLB LUTs")
        if qor.get("score"):
            s.append(f"  QoR score: {qor['score']}")
        if qor.get("dont_touch"):
            s.append(f"  DONT_TOUCH cells/nets: {int(qor['dont_touch'])}")
        if qor.get("budget_paths"):
            s.append(f"  Paths over Max Net/LUT budget: {qor['budget_paths']}")
        if qor.get("lut_combined"):
            s.append(f"  Max LUT Combined: {qor['lut_combined']}")
            if "REVIEW" in qor["lut_combined"].upper():
                s.append("  => LUT combining is high: combined LUT pairs (SOFT_HLUTNM) are "
                         "placement-locked, which caps a WIRE-bound design. UNCOMBINING them "
                         "(clear SOFT_HLUTNM on the combined cells) before a re-place frees the "
                         "placer — a DIFFERENT lever class than a placement-directive sweep. This "
                         "is the S1 indicator: reach for the ready-made recipe "
                         "::opt::try_keep_if_better {::opt::uncombine_and_replace} as your FIRST "
                         "surgical move here — it auto-scopes the combined hierarchy, un-pairs, and "
                         "re-places+routes. Prefer it over replace_critical_cone, which is an "
                         "AFTER-PLATEAU / scattered-cone floor-breaker, not a first move. Vivado's "
                         "QoR suggestions above may list the specific cells/moves.")
        for r in diagnosis.get("rationale", []):
            s.append(f"    · {r}")
        for nt in diagnosis.get("notes", []):
            s.append(f"  => {nt}")
        rqs = extract_qor_suggestions(diag.get("qsug", ""))
        if rqs:
            s.append("  Vivado QoR suggestions (Vivado's own, verbatim):")
            for ln in rqs:
                s.append(f"    {ln}")

    st = struct or {}
    # WHAT the worst path is physically made of (cell mix + on-path fanout), parsed from the
    # same report_design_analysis -timing text the delay-split came from. Golden-only: it goes
    # stale after a re-place, like the rest of this block.
    pathstruct = parse_worst_path_structure(raw_reports.get("design-analysis-timing", ""))
    pathstruct_lines = format_worst_path_structure(pathstruct)
    if (st.get("crit_hier") or st.get("top_hier") or st.get("unpiped_dsp")
            or st.get("col_span") or pathstruct_lines):
        s.append("\nDESIGN STRUCTURE (facts):")
        s.extend(pathstruct_lines)
        if st.get("crit_hier"):
            ch = ", ".join(f"{h} ({n} paths)" for h, n in st["crit_hier"])
            s.append(f"  Modules the worst paths run through: {ch}")
        if st.get("col_span"):
            lo, hi = st["col_span"]
            s.append(f"  Critical cells span device columns ~{lo}-{hi}")
        if st.get("top_hier"):
            th = ", ".join(f"{h} ({l} LUTs)" for h, l in st["top_hier"])
            s.append(f"  Heaviest modules by LUTs: {th}")
        if st.get("unpiped_dsp"):
            np_, tot = st["unpiped_dsp"]
            s.append(f"  DSP pipelining: {np_} of {tot} DSP48 are un-pipelined (PREG=0) "
                     f"(a DSP that is on the worst path is therefore RTL-bound)")

    if detour_candidates:
        s.append("\nRAPIDWRIGHT ROUTING DETOUR on critical-path cells (routed/direct; >2 = meander):")
        for cell, ratio in detour_candidates[:10]:
            s.append(f"  {ratio:5.1f}x  {cell}")
        n = len(detour_candidates); w = detour_candidates[0]
        s.append(f"  ({n} cell{'' if n == 1 else 's'} over 2.0x; worst {w[1]:.1f}x)")
        if w[1] >= 3.0:
            s.append(f"  => Worst detour {w[1]:.0f}x (routed path {w[1]:.0f}x longer than the "
                     "direct distance) => current placement is far from optimal; re-placement "
                     "/ alternative placement directives should help — this is NOT a logic floor.")

    # Signal-keyed EARLY recommendation for the selective cone re-place, when the scattered-
    # cone signature holds (wire-bound + high detour + stretched cone + low congestion).
    cone_rec = scattered_cone_recommendation(
        split, detour_candidates, spread, st.get("col_span"), cong)
    if cone_rec:
        s.append(cone_rec)

    if extended_findings or extended_checked:
        s.append("\nEXTENDED CHECKS (facts):")
        for key, fact in extended_findings:
            s.append(f"  {key}: {fact}")
        if extended_checked:
            s.append("  within normal range: " + ", ".join(extended_checked))

    s.append("\nTIMING STATUS:")
    if clock_period is not None:
        s.append(f"  Clock period: {clock_period:.3f} ns "
                 f"(target fmax: {1000.0 / clock_period:.2f} MHz)")
    if initial_wns is not None:
        met = "TIMING MET" if initial_wns >= 0 else "TIMING VIOLATED"
        s.append(f"  WNS: {initial_wns:.3f} ns - {met}")
        f = achievable_fmax(initial_wns, clock_period)
        if f is not None:
            s.append(f"  Achievable fmax: {f:.2f} MHz")
    if initial_tns is not None:
        s.append(f"  TNS: {initial_tns:.3f} ns")
    if initial_failing_endpoints is not None:
        s.append(f"  Failing endpoints: {initial_failing_endpoints}")

    if spread:
        s.append("\nCRITICAL PATH SPREAD:")
        s.append(f"  Max cell distance: {spread['max_distance']} tiles")
        s.append(f"  Avg cell distance: {spread['avg_distance']:.1f} tiles")
        s.append(f"  Paths analyzed: {spread['paths_analyzed']}")

    if high_fanout_nets:
        s.append("\nCRITICAL HIGH FANOUT NETS (top 10):")
        for i, (net, fo, pc) in enumerate(high_fanout_nets[:10]):
            s.append(f"  {i + 1}. {net}  (fanout {fo}, critical paths {pc})")
        if len(high_fanout_nets) > 10:
            s.append(f"  ... and {len(high_fanout_nets) - 10} more")

    # Resolve the reserved legend slot: keep it only if a '=>' line actually fired somewhere
    # in the finished body. Checked against the RENDERED text, not against diagnosis["notes"],
    # because four independent branches emit '=>' and only one of them is that list.
    if derived_legend_at is not None:
        others = s[:derived_legend_at] + s[derived_legend_at + 1:]
        if not any("=>" in ln for ln in others):
            del s[derived_legend_at]

    return "\n".join(s)


def hold_pw_ok(whs: Optional[float], pw: Optional[float],
               strict: bool = False, base_whs: Optional[float] = None) -> bool:
    """Are hold (WHS) and pulse-width (WPWS) acceptable?

    Two policies, because the two decisions differ in cost:

    strict=False (mid-run) -- LENIENT. Rejects only a violation worse than the
    input's, and treats None (unparseable) as ok. A false reject mid-run costs
    one iteration.

    strict=True (the ship decision) -- FAIL CLOSED. Requires ABSOLUTE
    whs >= 0 and pw >= 0, and treats None as FAILURE. Contest validation rejects
    any design with a hold or pulse-width violation, scoring the benchmark zero,
    so "we could not confirm this is safe" must fall back to the original input.
    Relative-to-input is not enough here: if the input itself violates hold, a
    design that merely matches it still fails validation.

    Ported from ruthwik's Runner._hold_pw_ok; `base_whs` is a parameter rather
    than self.initial_whs so the function stays pure and testable.
    """
    if strict:
        return (whs is not None and whs >= 0.0
                and pw is not None and pw >= 0.0)
    if pw is not None and pw < 0:
        return False
    if whs is not None and whs < 0:
        base = base_whs if base_whs is not None else 0.0
        if whs < base - 1e-9:   # introduced or worsened a hold violation
            return False
    return True


def atomic_copy(src, dst) -> bool:
    """Copy src -> dst ATOMICALLY: copy to a sibling temp file, then os.replace.

    The output DCP is the only thing that gets scored. Writing onto it in place
    means a kill at the wall-clock cap (or any mid-copy failure) truncates a
    *previously valid* file -> validation fails -> score 0, which is strictly
    worse than shipping the untouched input. os.replace is atomic within a
    filesystem, so a reader sees either the whole old file or the whole new one.
    """
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)          # atomic swap
        return True
    except Exception as e:
        logger.warning(f"Atomic copy {src} -> {dst} failed: {e}")
        try:
            tmp.unlink(missing_ok=True)   # never leave a stray partial file
        except Exception:
            pass
        return False


# ===========================================================================
# ORCHESTRATION (injected I/O) -- BELOW THIS LINE the functions do I/O, but ONLY
# through an injected async ``run_tcl(cmd) -> str`` callback (maya wires it to the
# raw MCP run_tcl path). They never import MCP; everything they compute is still
# pure given the returned text. Keep this clearly separated from the pure section
# above so the parsers stay import-safe and side-effect-free.
# ===========================================================================


@dataclass
class StateMeasurement:
    """One per-turn measurement of the live design. The SINGLE measurement a turn
    needs: its parsed verdicts feed both the model's state digest and the ship gate,
    and tsum_text is kept so the gate reuses it without a second read."""
    tsum: TimingSummary
    tsum_text: str
    routed: Optional[bool]
    route_bad: Optional[int]     # unrouted/partially-routed/error nets, or None
    prims: Optional[int]
    texts: dict                 # REFRESH_REPORTS raw texts, for worst-path/congestion/util


async def gather_vivado_reports(run_tcl, *, tsum_text: str = ""):
    """Run the initial-analysis report bundle -- the combined design-analysis report,
    every CORE_REPORT, then the EXTENDED_CHECKS (clock-domain ones skipped on
    single-clock designs) -- and return ``({key: raw_text}, nclocks)``.
    ``run_tcl(cmd) -> str`` is the injected runner; everything is issued with
    ``-return_string``.

    ``tsum_text`` is a timing summary the caller ALREADY measured on this design.
    Supplying it re-sources the clock count instead of probing for it; omitting it
    costs the probe, never a wrong answer.

    This is the report-RUNNING half of the initial ANALYZE stage. maya keeps only the
    RapidWright gathering (a different subsystem) and the adapter that stores the result."""
    raw = {}
    # One command, three sections. Falls back to the separate calls if the combined
    # report does not split -- a Vivado version that dropped a section would otherwise
    # silently hand the parsers an empty string, i.e. "no finding" for a design that
    # has one.
    da = design_analysis_raw(await run_tcl(f"{DESIGN_ANALYSIS_COMBINED} -return_string"))
    if len(da) == len(DESIGN_ANALYSIS_SECTIONS):
        raw.update(da)
    else:
        logger.warning(
            "Combined report_design_analysis yielded %d/%d sections (%s); falling back "
            "to separate calls", len(da), len(DESIGN_ANALYSIS_SECTIONS), sorted(da))
        for key, flag in (("design-analysis-timing", "-timing -max_paths 10"),
                          ("congestion", "-congestion"), ("complexity", "-complexity")):
            raw[key] = await run_tcl(f"report_design_analysis {flag} -return_string")

    for key, tcl in CORE_REPORTS:
        raw[key] = await run_tcl(f"{tcl} -return_string")

    # Clock count from the timing summary the caller already holds. FAILS SAFE: an
    # unparsable Clock Summary falls back to the probe rather than assuming 1 -- a
    # wrong low count silently SKIPS the multi-clock reports, which shrinks the
    # prompt without saying so.
    nclocks = parse_clock_count(tsum_text) if tsum_text else None
    if nclocks is None:
        out = await run_tcl('puts "MAYA_NCLK=[llength [get_clocks]]"')
        m = re.search(r"MAYA_NCLK=(\d+)", out)
        nclocks = int(m.group(1)) if m else 1

    for key, tcl, _parser, mc_only in EXTENDED_CHECKS:
        if tcl is None or key in raw:        # already captured above
            continue
        if mc_only and nclocks <= 1:
            continue
        raw[key] = await run_tcl(f"{tcl} -return_string")
    return raw, nclocks


async def measure_state(run_tcl) -> StateMeasurement:
    """Run the per-turn measurement ONCE: the REFRESH_REPORTS bundle, the timing
    summary, route status, and the primitive count. ``run_tcl(cmd) -> str`` is the
    injected raw-Tcl runner. report_timing_summary / report_route_status are issued
    as ``... -return_string`` -- identical to the dedicated MCP tools, which are
    themselves thin wrappers over the same run_tcl_command call."""
    texts = {}
    # design-analysis-timing + congestion in ONE call (see DESIGN_ANALYSIS_COMBINED).
    # This runs every turn, so the saving is per-turn, not once per run.
    da = design_analysis_raw(await run_tcl(f"{DESIGN_ANALYSIS_COMBINED} -return_string"))
    for key in ("design-analysis-timing", "congestion"):
        if key in da:
            texts[key] = da[key]
        else:                                  # same fallback contract as the initial gather
            flag = "-timing -max_paths 10" if key.endswith("timing") else "-congestion"
            texts[key] = await run_tcl(f"report_design_analysis {flag} -return_string")
    for key, tcl in REFRESH_REPORTS:
        texts[key] = await run_tcl(f"{tcl} -return_string")
    tsum_text = await run_tcl("report_timing_summary -return_string")
    rstat_text = await run_tcl("report_route_status -return_string")
    prims = parse_primitive_count(await run_tcl(
        'puts "MAYA_PRIMS=[llength [get_cells -hier -quiet '
        '-filter {IS_PRIMITIVE==1}]]"'))
    return StateMeasurement(parse_timing_summary(tsum_text), tsum_text,
                            parse_route_status(rstat_text), route_bad_net_count(rstat_text),
                            prims, texts)


def render_failed_attempt(error: Optional[str], best_saved_wns: Optional[float]) -> str:
    """The digest injected INSTEAD of a re-measurement when a plan script failed.

    A failed attempt is discarded and the next script runs from the banked best, so
    measuring the live design costs a full refresh bundle (6 Vivado calls, ~29 s on
    ispd16) to produce numbers that are then thrown away. This states what happened
    instead of measuring it.

    SAYS "your next script runs from", NOT "reverted to". The revert is DEFERRED --
    _plan_needs_revert is set here and _revert_to_best_if_needed re-opens the snapshot
    before the NEXT script (maya.py:1550), because open_checkpoint costs minutes on a
    large design and a run that ends here should never pay for it. At the moment this
    text is written the live session has NOT been reverted, so claiming it had would be
    false -- and the ship path's whole discipline is to never advertise a state that is
    not real.

    ACCEPTED TRADEOFF, DELIBERATE -- do not "fix" this back. A script that fails PARTWAY
    (place_design succeeds, a later command aborts) may have left a genuinely better
    design, and it is now discarded unmeasured. Decision 2026-08-10: the 6-call refresh
    on every failure is not worth the rare partial gain. If that is ever revisited, the
    cheap version is a single `get_timing_paths -max_paths 1` slack probe (~1 s vs ~10 s
    for a full timing summary), NOT a return to the full bundle.
    """
    banked = ("nothing banked yet — the untouched input is still the best design"
              if best_saved_wns is None else f"WNS {best_saved_wns:+.3f} ns")
    return "\n".join([
        "=== PLAN ATTEMPT FAILED — the design was NOT re-measured ===",
        f"  Error: {error or 'unknown'}",
        "  Nothing was banked, and this attempt is discarded.",
        f"  Your next script runs from the banked best: {banked}",
        "  So the state you reasoned about last turn still holds — do not assume your "
        "script's earlier commands survived.",
    ])


async def build_state_digest(run_tcl, *, clock_period: Optional[float],
                             prev_wns: Optional[float], prev_prims: Optional[int],
                             best_saved_wns: Optional[float]):
    """Re-measure the design and render ONLY freshly-measured facts, for injection
    between LLM turns. Returns ``(digest_str|None, StateMeasurement|None,
    new_prev_wns, new_prev_prims)`` -- the caller stores the two new prev values and
    may reuse the StateMeasurement for the ship gate (measure once, gate on the same
    numbers).

    Deliberately not render_analysis(): that also emits the structural, spread and
    RapidWright-detour sections gathered during the initial analysis, all
    placement-dependent and therefore stale the moment a re-place happens.
    """
    try:
        m = await measure_state(run_tcl)
    except Exception as e:
        logger.warning(f"State refresh failed: {e}")
        return None, None, prev_wns, prev_prims
    tsum = m.tsum

    lines = ["=== DESIGN STATE (re-measured just now, after your last step) ==="]
    if tsum.complete:
        fmax = achievable_fmax(tsum.setup_wns, clock_period)
        lines.append(f"  WNS {tsum.setup_wns:+.3f} ns"
                     + (f"  (achievable fmax {fmax:.2f} MHz)" if fmax else ""))
        lines.append(f"  TNS {tsum.setup_tns}  failing endpoints "
                     f"{tsum.setup_failing_endpoints}/{tsum.total_endpoints}")
        lines.append(f"  Hold WHS {tsum.hold_whs}   Pulse-width WPWS {tsum.pulse_width_wns}")
        if (tsum.hold_whs is not None and tsum.hold_whs >= 0
                and tsum.pulse_width_wns is not None and tsum.pulse_width_wns >= 0):
            lines.append("  Both currently pass — a WNS gain that pushes either "
                         "negative is worthless, the design would be rejected.")
        else:
            lines.append("  ** HOLD or PULSE-WIDTH is negative or unknown: this design "
                         "CANNOT be shipped until that is fixed. **")
    lines.append(f"  Fully routed: {m.routed}")
    if m.prims is not None:
        lines.append(f"  Primitives: {m.prims}")

    # What CHANGED since the previous digest. The model otherwise has to diff two
    # turns of text, which it does unreliably -- and the primitive count is the
    # only signal that separates "that lever tied" from "that lever was a no-op".
    # Roughly half the levers measured in the sweep corpus were exact no-ops.
    dw = (None if (tsum.setup_wns is None or prev_wns is None)
          else tsum.setup_wns - prev_wns)
    dp = (None if (m.prims is None or prev_prims is None)
          else m.prims - prev_prims)
    if dw is not None or dp is not None:
        bits = []
        if dw is not None:
            bits.append(f"WNS {dw:+.3f} ns")
        if dp is not None:
            bits.append("primitives unchanged" if dp == 0 else f"primitives {dp:+d}")
        lines.append("  SINCE YOUR LAST STEP: " + ", ".join(bits))
        if dw is not None and abs(dw) < 1e-9 and dp == 0:
            lines.append("  => Identical WNS AND identical primitive count: that step "
                         "rebuilt the SAME design. It was a no-op, not a tie. Do not "
                         "retry variants of it -- try a different kind of move.")
    new_prev_wns = tsum.setup_wns if tsum.complete else prev_wns
    new_prev_prims = m.prims if m.prims is not None else prev_prims

    split = parse_worst_path_delay_split(m.texts.get("design-analysis-timing", "") or "")
    if split.get("logic_pct") is not None:
        lines.append(f"  Worst path: {split['logic_pct']}% logic / {split.get('net_pct')}% wire"
                     + (f", {split['logic_levels']} logic levels" if split.get("logic_levels") else "")
                     + (f", ends at {split['endpoint']}" if split.get("endpoint") else ""))
    cong = parse_congestion_level(m.texts.get("congestion", "") or "")
    if cong:
        lines.append(f"  Congestion: max level {cong.get('max_level', 0)}"
                     + ("  (>=6 is oversubscribed)" if cong.get("congested") else ""))
    devfill = format_device_utilization(parse_device_utilization(m.texts.get("utilization", "") or ""))
    if devfill:
        lines.append(f"  Device fill: {devfill}")

    banked = ("none yet" if best_saved_wns is None else f"{best_saved_wns:+.3f} ns")
    lines.append(f"  Best design actually BANKED so far: {banked} "
                 f"(only a banked, gate-passing design can be shipped)")
    return "\n".join(lines), m, new_prev_wns, new_prev_prims


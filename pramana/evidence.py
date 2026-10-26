"""Build the text evidence the LLM stages read: design brief, baseline digests, results so far,
and a compact description of the search space."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .campaign import Campaign
from .features import FEATURE_DOC
from .logparse import digest_text


def _strip_header(text: str) -> str:
    """Drop Vivado's copyright/host banner."""
    lines = text.splitlines()
    for i, ln in enumerate(lines[:40]):
        if ln.startswith("| Design State") or ln.startswith("| Device"):
            j = i + 1
            while j < len(lines) and lines[j].startswith("-"):
                j += 1
            return "\n".join(lines[j:])
    return text


def _section(text: str, start: str, n: int) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    return "\n".join(text[i:].splitlines()[:n])


def _read(p: Path) -> str:
    return p.read_text(errors="replace") if p.exists() else ""


DA_COLS = ["Paths", "Path Delay", "Logic Delay", "Net Delay", "Clock Skew", "Slack",
           "Logic Levels", "Routes", "Logical Path", "DSP Block", "RAM Registers", "High Fanout",
           "Start Point Pin Primitive", "End Point Pin Primitive"]


def design_analysis_compact(text: str, n: int = 15) -> str:
    rows, hdr = [], None
    for ln in text.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [x.strip() for x in ln.strip().strip("|").split("|")]
        if hdr is None and "Paths" in cells and "Slack" in cells:
            hdr = cells
            continue
        if hdr and len(cells) == len(hdr) and cells[0].startswith("Path #"):
            rows.append(dict(zip(hdr, cells)))
    out = [" | ".join(DA_COLS)] + [" | ".join(r.get(k, "")[:90] for k in DA_COLS) for r in rows[:n]]
    i = text.find("Logic Level Distribution")
    j = text.find("Logic Level Distribution", i + 10)
    if j > 0:
        lld = [ln for ln in text[j:].splitlines()[:14] if ln.startswith("|")]
        out += ["", "Logic level distribution:"] + lld
    return "\n".join(out)


def report_excerpts(c: Campaign, did: str) -> str:
    d = c.profile_dir(did)
    out = []
    ts = _read(d / "timing_summary.txt")
    out.append("### timing summary\n" + _section(ts, "Design Timing Summary", 12)
               + "\n" + _section(ts, "Clock Summary", 12))
    out.append("### report_design_analysis: setup path characteristics (top 15, key columns)\n"
               + design_analysis_compact(_read(d / "design_analysis.txt")))
    ut = _strip_header(_read(d / "utilization.txt"))
    keep = [ln for ln in ut.splitlines() if ln.startswith("|") and
            re.search(r"CLB LUTs|CLB Registers|CARRY8|F7|F8|LUT as Memory|Block RAM Tile|URAM|DSPs|BUFG", ln)]
    out.append("### utilization (key rows: Used | Fixed | Prohibited | Available | Util%)\n"
               + "\n".join(dict.fromkeys(keep)))
    cg = _strip_header(_read(d / "congestion.txt"))
    out.append("### placer congestion\n" + "\n".join(
        ln for ln in cg.splitlines() if ln.startswith(("|", "*")))[:1500])
    hf = _strip_header(_read(d / "high_fanout_nets.txt"))
    out.append("### high fanout nets\n" + "\n".join(hf.splitlines()[:25]))
    cs = _strip_header(_read(d / "control_sets.txt"))
    out.append("### control sets\n" + _section(cs, "1. Summary", 14))
    qa = _strip_header(_read(d / "qor_assessment.txt"))
    out.append("### report_qor_assessment (excerpt)\n" + "\n".join(qa.splitlines()[:45]))
    qs = _strip_header(_read(d / "qor_suggestions.txt"))
    out.append("### report_qor_suggestions (excerpt)\n" + "\n".join(qs.splitlines()[:60]))
    return "\n\n".join(out)


def top_paths(c: Campaign, did: str, n: int = 15) -> str:
    p = c.profile_dir(did) / "paths.tsv"
    if not p.exists():
        return ""
    rows = list(csv.DictReader(p.open(), delimiter="\t"))[:n]
    hdr = ["slack", "req", "levels", "datapath", "logic", "net", "skew", "start_ref", "end_ref",
           "max_fanout", "cell_refs"]
    lines = ["\t".join(hdr)] + ["\t".join(r.get(h, "") for h in hdr) for r in rows]
    return "\n".join(lines)


def design_brief(c: Campaign, did: str) -> str:
    d = c.design(did)
    inp = c.profile(did)["input"]
    feats = c.features(did)
    flines = "\n".join(f"  {k} = {v}    ({FEATURE_DOC.get(k, '')})" for k, v in feats.items())
    clocks = ", ".join(f"{x['name']} {x['period']} ns" for x in inp.get("clocks") or [])
    return (f"## Design {did} (family {d['family']}, {d['dcp_mb']} MB DCP)\n"
            f"Input state: WNS {inp['wns']} ns, TNS {inp['tns']} ns, failing endpoints "
            f"{inp['failing_endpoints']}/{inp['total_endpoints']}, WHS {inp['whs']}, "
            f"clocks: {clocks}, primitives {inp['prims']}\n\n"
            f"### features\n{flines}\n\n### top setup paths\n{top_paths(c, did)}\n\n"
            f"{report_excerpts(c, did)}")


def baseline_brief(c: Campaign, did: str, digest_chars: int = 3500) -> str:
    out = ["## Explore baselines (each from the input DCP; chains end routed; 'auto' steps are "
           "default place/route added to finish)"]
    for r in c.results(did):
        if r["purpose"] != "baseline" or r.get("rep"):
            continue
        out.append(f"\n### {r['tag']}: {r['chain_key']}\nstatus={r['status']} valid={r['valid']} "
                   f"dWNS={r['d_wns']} ns  final WNS={(r['result'] or {}).get('wns')}  "
                   f"step seconds={[ (s['name'], s['secs']) for s in r['steps']]}")
        out.append(digest_text(Path(r["run_dir"]), max_chars=digest_chars))
    return "\n".join(out)


def results_table(c: Campaign, did: str, limit: int = 120) -> str:
    rs = [r for r in c.results(did) if r["purpose"] != "probe" or r["valid"]]
    lines = ["run_id | start | chain | purpose | round | valid | dWNS_vs_start | final_WNS | opt_secs"]
    for r in rs[-limit:]:
        lines.append(f"{r['run_id']} | {r['start']} | {r['chain_key']} | {r['purpose']} | "
                     f"{r.get('round')} | {r['valid']} | {r['d_wns']} | "
                     f"{(r['result'] or {}).get('wns')} | {r['opt_secs']}")
    return "\n".join(lines)


def space_brief(spec: dict) -> str:
    out = ["## Search space (Vivado " + spec["vivado_version"] + "), per command",
           "Step JSON: {\"cmd\": one of opt|place|phys_opt|route, \"directive\": name or null, "
           "\"opts\": {flag: true | value | [values]}}.",
           "Rules: opt and phys_opt take EITHER a directive OR individual options (individual "
           "options run ONLY those passes). place: a directive combines only with -no_psip, "
           "-net_delay_weight, -subdirective. route: a directive combines only with "
           "-tns_cleanup, -preserve. route '+ripup' = unroute everything first (else incremental). "
           "opt/place steps unplace the design first; missing place/route steps are added with "
           "defaults so every chain ends routed. phys_opt runs post-route when the design is "
           "routed, pre-route after a place step."]
    for short, cmd in spec["commands"].items():
        out.append(f"\n### {short} ({cmd['vivado_command']})")
        out.append("directives: " + ", ".join(cmd["directives"]))
        for k, o in cmd["options"].items():
            vals = f" values={o['values']}" if "values" in o else ""
            pre = " [PRE-ROUTE ONLY: Vivado rejects it on a routed design]" if o.get("postroute") is False else ""
            out.append(f"  {k} [{o['type']}{', max_pick=' + str(o['max_pick']) if o.get('max_pick') else ''}]{pre}"
                       f"{vals}: {o.get('help') or o.get('why', '')}"[:400])
    return "\n".join(out)

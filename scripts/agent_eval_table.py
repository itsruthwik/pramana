#!/usr/bin/env python3
"""Collect result.json files from campaigns/<c>/{official,maya,pact}_eval/<design>.<kb>[.<tag>]/ into one
markdown table (ΔWNS ns, independently re-measured where available) and a JSON summary.

  python3 scripts/agent_eval_table.py dev1 [> campaigns/dev1/agent_eval_summary.md]
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = [("official", "official contest agent"), ("maya", "maya"), ("pact", "PACT")]


def main(campaign: str) -> None:
    root = REPO / "campaigns" / campaign
    rows, summary = [], {}
    for key, label in AGENTS:
        for rj in sorted((root / f"{key}_eval").glob("*/result.json")):
            name = rj.parent.name                      # <design>.<kb>[.<tag>]
            if ".failed_" in name:                     # archived attempts that hit harness bugs
                continue
            r = json.loads(rj.read_text())
            parts = name.split(".")
            design, kb = parts[0], parts[1] if len(parts) > 1 else ""
            # maya_eval trusts maya's gate; prefer an independent measure_output when present
            m = rj.parent / "measure_output" / "measure.json"
            if key == "maya" and m.exists():
                mm = json.loads(m.read_text())
                if mm.get("valid") and mm.get("result"):
                    r["final_wns"], r["valid"] = mm["result"]["wns"], True
                    r["d_wns"] = round(r["final_wns"] - r["input_wns"], 4)
            calls = r.get("llm_calls")
            if calls is None:
                calls = r.get("attempts")
            rows.append((label, design, kb, (r.get("model") or "").split("/")[-1], r.get("input_wns"), r.get("final_wns"), r.get("d_wns"),
                         r.get("valid"), calls, r.get("cost_usd"),
                         r.get("agent_runtime_s") or r.get("maya_runtime_s") or r.get("pact_elapsed_s")))
            summary[f"{key}/{name}"] = {k: r.get(k) for k in
                                        ("input_wns", "final_wns", "d_wns", "valid", "cost_usd", "model")}
    print(f"# Agent evaluation, campaign {campaign}\n")
    print("ΔWNS in ns on the contest clock, re-measured by a fresh Vivado; valid = routed, 0 route "
          "errors, WHS ≥ 0, WPWS ≥ 0.\n")
    print("| agent | design | kb | model | input WNS | final WNS | ΔWNS | valid | LLM calls / attempts | cost USD | agent secs |")
    print("|--|--|--|--|--|--|--|--|--|--|--|")
    for a, d, kb, mo, i, f, dw, v, c, cost, secs in rows:
        print(f"| {a} | {d} | {kb} | {mo} | {i} | {f} | {dw:+.3f} | {v} | {c} | "
              f"{'' if cost is None else f'{cost:.2f}'} | {'' if secs is None else int(secs)} |"
              if dw is not None else f"| {a} | {d} | {kb} | {mo} | {i} | {f} | n/a | {v} | {c} | | |")
    (root / "agent_eval_summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dev1")

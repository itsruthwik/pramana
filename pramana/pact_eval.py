"""Evaluate PACT / FDAgents (consumers/pact, the public v2.0.0 artifact with two marked
patches, see consumers/pact_pramana/SOURCE.md) on one DCP with a knowledge-base variant.

  none    : as shipped and as the contest ran it: empty case corpus, empty skills guide
  hand    : PACT's own hand-written case library and skills guide (knowledge/cases.yaml,
            knowledge/skills_guide.yaml) via a config override
  pramana : pramana's cases (<bundle>/cases.yaml) as the corpus, and <bundle>/context.md shown to
            PACT's Decision LLM as an advisory `learned_prior` (the [pramana] patch)

PACT's knowledge loader rejects raw Tcl, so pramana's recipes enter only as PACT's typed presets
(phys_opt directives, named place-and-route recipes), never as sourced Tcl.

PACT starts Vivado itself (and xsim for its equivalence export), so this holds one machine-wide
Vivado slot. Layout of out_dir: pact.log, run/ (memory.json, candidates/, submission/),
output.dcp, measure_*/, result.json.

  python -m pramana.pact_eval --dcp X.dcp --out OUT --kb pramana --bundle campaigns/c/out/adapters/pact \
      --model deepseek/deepseek-v4-pro
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

from .config import REPO, VIVADO_SETTINGS
from .measure import measure_dcp
from .vivado import _run_in_slot, est_mem_gb, reap_under, vivado_slot

PACT_DIR = REPO / "consumers" / "pact"
PACT_PYTHON = os.environ.get(
    "PACT_PYTHON",
    "/mnt/vault1/kmhatre/workspace/FPL_26_contest_asu/contest_winners/4_Fu_Do_Fdo/.venv/bin/python")
JAVA11 = "/usr/lib/jvm/java-11-openjdk-amd64"
KB_VARIANTS = ("none", "hand", "pramana")
GRACE_S = 900
EXTRA_GB = 6.0      # RapidWright JVM, MCP servers and the xsim equivalence export


def _openrouter_pricing(model: str, key_env: str) -> dict | None:
    """USD per 1K tokens for `model` from OpenRouter's model list (PACT's pricing unit)."""
    import urllib.request
    key = os.environ.get(key_env, "")
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": f"Bearer {key}"} if key else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())["data"]
    except Exception:
        return None
    for m in data:
        if m.get("id") == model:
            p = m.get("pricing") or {}
            try:
                return {"input": 1000 * float(p["prompt"]), "output": 1000 * float(p["completion"]),
                        "cached_input": 1000 * float(p.get("input_cache_read") or p["prompt"])}
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _config_yaml(kb: str, bundle: Path | None, out: Path, model: str | None = None,
                 pricing: dict | None = None) -> Path:
    """A user config merged over PACT's defaults: the knowledge paths differ by variant, and the
    model gets a pricing entry (PACT refuses to call the LLM without one: it enforces its dollar
    cap from token counts)."""
    if kb == "none":
        cases, guide = "knowledge/no_replay_empty_cases.yaml", "knowledge/no_replay_empty_skills_guide.yaml"
    elif kb == "hand":
        cases, guide = "knowledge/cases.yaml", "knowledge/skills_guide.yaml"
    else:
        if bundle is None or not (Path(bundle) / "cases.yaml").exists():
            raise FileNotFoundError(f"kb=pramana needs a pact bundle with cases.yaml: {bundle}")
        cases = str((Path(bundle) / "cases.yaml").resolve())
        guide = "knowledge/no_replay_empty_skills_guide.yaml"
    p = out / "fdagents_config.yaml"
    # The public release ships min_match_score 1.0 and max_cases_in_prompt 0, which switches case
    # matching off. hand/pramana restore the loader's own defaults (0.35 / 5) so cases can match;
    # none keeps the shipped values (what the contest ran).
    match = "" if kb == "none" else "  min_match_score: 0.35\n  max_cases_in_prompt: 5\n"
    # Stage output budgets: the shipped selection stage allows 2000 output tokens, which a
    # reasoning model spends on hidden reasoning before the forced tool call (deepseek used 1318
    # of 1942 on the one turn that worked, then truncated). Raise the selector budgets.
    price = ("llm:\n  stages:\n"
             "    action_menu_selection: {effort: low, max_output_tokens: 8000}\n"
             "    replay_selection: {effort: low, max_output_tokens: 6000}\n"
             "    reconsider: {effort: medium, max_output_tokens: 8000}\n")
    if model and pricing:
        price += ("  pricing:\n"
                  f"    {json.dumps(model)}: {{input: {pricing['input']:.8f}, "
                  f"cached_input: {pricing['cached_input']:.8f}, output: {pricing['output']:.8f}}}\n")
    # [pramana] harness robustness for non-OpenAI models (see consumers/pact_pramana/SOURCE.md):
    # the shipped selector gives the model exactly one turn to call the terminal tool and stops
    # the whole run on the first miss. These knobs exist only in the patched copy.
    react = "react:\n  selector_turns: 3\n  retry_invalid_decisions: true\n"
    p.write_text("# written by pramana.pact_eval\nknowledge:\n"
                 f"  cases_path: {json.dumps(cases)}\n  skills_guide_path: {json.dumps(guide)}\n"
                 + match + price + react)
    return p


def parse_run(run_dir: Path) -> dict:
    r: dict = {"pact_input_wns": None, "pact_best_wns": None, "pact_best_fmax": None, "rounds": None,
               "cost_usd": None, "pact_elapsed_s": None, "attempts": None, "accepted": None}
    mem = Path(run_dir) / "memory.json"
    if mem.exists():
        m = json.loads(mem.read_text())
        cb, bl, bud = m.get("current_best") or {}, m.get("baseline") or {}, m.get("budget") or {}
        hist = m.get("history") or []
        r.update(pact_input_wns=bl.get("wns"), pact_best_wns=cb.get("wns"), pact_best_fmax=cb.get("fmax"),
                 rounds=m.get("round"), cost_usd=bud.get("tokens_spent"),
                 pact_elapsed_s=bud.get("time_elapsed"), attempts=len(hist),
                 accepted=sum(1 for h in hist if h.get("accepted") is True),
                 clock_period_ns=cb.get("clock_period") or bl.get("clock_period"),
                 learned_prior_present=bool(m.get("learned_prior")))
    return r


def run(bundle, dcp, out_dir, kb: str, budget_s: float = 3600, model: str | None = None, *,
        key_env: str = "OPENROUTER_API_KEY", python: str | None = None,
        pricing: dict | None = None, vivado_settings: str = VIVADO_SETTINGS) -> dict:
    """pricing: {input, cached_input, output} in USD per 1K tokens; fetched from OpenRouter when
    omitted (PACT enforces its dollar cap from token counts and stops without a pricing entry)."""
    if kb not in KB_VARIANTS:
        raise ValueError(f"kb must be one of {KB_VARIANTS}")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dcp = Path(dcp).resolve()
    if not os.environ.get(key_env):
        raise RuntimeError(f"API key env var {key_env} is not set")
    run_dir, output_dcp, log = out / "run", out / "output.dcp", out / "pact.log"
    if model and pricing is None:
        pricing = _openrouter_pricing(model, key_env)
        if pricing is None:
            raise RuntimeError(f"no pricing for {model}: pass pricing=(USD/1K in, cached, out); "
                               "PACT will not call the LLM without it")
    cfg = _config_yaml(kb, Path(bundle) if bundle else None, out, model=model, pricing=pricing)
    args = [str(dcp), "--output", str(output_dcp), "--submission", "--time-limit", f"{budget_s:.0f}",
            "--run-dir", str(run_dir), "--config", str(cfg)]
    if model:
        args += ["--model", model]
    py = python or PACT_PYTHON
    q = shlex.quote
    env = ""
    if kb == "pramana":
        env += f"export FDAGENTS_PRAMANA_CONTEXT={q(str((Path(bundle) / 'context.md').resolve()))}; "
    java = (f'export JAVA_HOME={q(JAVA11)}; ' if Path(JAVA11).exists() else
            'export JAVA_HOME="${JAVA_HOME:-$(ls -d "$XILINX_VIVADO"/tps/lnx64/jre* 2>/dev/null | head -n 1)}"; ')
    # PACT switches to OpenRouter by itself when OPENROUTER_API_KEY is set (FDAgents/llm.py).
    cmd = (f"source {q(vivado_settings)} >/dev/null 2>&1; " + java +
           'export PATH="$JAVA_HOME/bin:$PATH"; export VIVADO_EXEC="$(command -v vivado)"; '
           "unset RAPIDWRIGHT_PATH CLASSPATH; " + env +
           f'export OPENROUTER_API_KEY="${{{key_env}}}"; '
           f"cd {q(str(PACT_DIR))}; exec {q(py)} -m FDAgents.agent {' '.join(q(a) for a in args)} "
           f"> {q(str(log))} 2>&1")
    need = est_mem_gb(dcp.stat().st_size / 1e6) + EXTRA_GB
    t0 = time.time()
    with vivado_slot():
        res = _run_in_slot(cmd, out, budget_s + GRACE_S, need)
        reap_under(out, log=print)   # the agent's Vivado outlives the agent (see pramana.vivado)
    r = parse_run(run_dir)
    m_in = measure_dcp(dcp, out / "measure_input")
    # the output is scored under the INPUT's clock constraints (PACT's clock_tighten rewrites
    # the period; a checkpoint left at a looser clock must not be credited for it)
    in_clocks = [(c["name"], c["period"]) for c in ((m_in.get("result") or {}).get("clocks") or [])]
    m_out = (measure_dcp(output_dcp, out / "measure_output", clocks=in_clocks or None)
             if output_dcp.exists() else None)
    in_wns = (m_in.get("result") or {}).get("wns")
    out_wns = (m_out.get("result") or {}).get("wns") if m_out else None
    r.update(kb=kb, dcp=str(dcp), bundle=str(bundle) if bundle else None, model=model,
             budget_s=budget_s, secs=round(time.time() - t0, 1), exit_code=res.exit_code,
             timed_out=res.timed_out, output_exists=output_dcp.exists(),
             input_wns=in_wns, output_wns=out_wns, output_valid=bool(m_out and m_out["valid"]),
             final_wns=out_wns if (m_out and m_out["valid"]) else in_wns,
             valid=bool(m_out and m_out["valid"]),
             output_dcp=str(output_dcp), run_dir=str(run_dir), log=str(log), config=str(cfg))
    r["d_wns"] = (round(r["final_wns"] - in_wns, 4)
                  if r["final_wns"] is not None and in_wns is not None else None)
    (out / "result.json").write_text(json.dumps(r, indent=2))
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dcp", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--kb", required=True, choices=KB_VARIANTS)
    ap.add_argument("--bundle", type=Path, help="pramana pact bundle dir (cases.yaml, context.md)")
    ap.add_argument("--budget-s", type=float, default=3600)
    ap.add_argument("--model", default=None)
    ap.add_argument("--key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--python", default=None)
    ap.add_argument("--pricing", default=None, metavar="IN,CACHED,OUT",
                    help="USD per 1K tokens for --model (default: fetched from OpenRouter)")
    a = ap.parse_args(argv)
    pricing = None
    if a.pricing:
        i, c, o = (float(x) for x in a.pricing.split(","))
        pricing = {"input": i, "cached_input": c, "output": o}
    r = run(a.bundle, a.dcp, a.out, a.kb, a.budget_s, a.model, key_env=a.key_env, python=a.python,
            pricing=pricing)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()

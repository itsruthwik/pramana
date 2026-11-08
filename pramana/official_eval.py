"""Evaluate the official FPL'26 contest agent (consumers/official, a patched copy, see
consumers/official/SOURCE.md) on one DCP with a chosen knowledge-base variant.

  hand    : the agent's upstream prompt (its hand-written strategies)
  none    : protocol-only prompt (tools + rules), no strategy knowledge
  pramana : protocol prompt + <bundle>/context.md, with <bundle>/recipes.tcl sourced into Vivado

The agent starts Vivado itself, so this holds one machine-wide Vivado slot (pramana.vivado) for
the whole run. Layout of out_dir: agent.log, run/ (the agent's run dir with token_usage.json),
output.dcp, measure_*/ (independent re-measurement), result.json.

  python -m pramana.official_eval --dcp X.dcp --out OUT --kb pramana --bundle campaigns/c/out/maya \
      --model deepseek/deepseek-v4-pro --key-env OPENROUTER_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .config import REPO, VIVADO_SETTINGS
from .measure import measure_dcp
from .vivado import _run_in_slot, est_mem_gb, reap_under, vivado_slot

AGENT_DIR = REPO / "consumers" / "official"
AGENT = AGENT_DIR / "dcp_optimizer.py"
RAPIDWRIGHT_PATH = os.environ.get("MAYA_RAPIDWRIGHT_PATH",
                                  "/scratch/kmhatre/workspace/dac/tools/RapidWright")
JAVA11 = "/usr/lib/jvm/java-11-openjdk-amd64"
KB_VARIANTS = ("hand", "none", "pramana")
GRACE_S = 900
EXTRA_GB = 4.0


def _agent_args(dcp: Path, kb: str, bundle: Path | None) -> list[str]:
    if kb not in KB_VARIANTS:
        raise ValueError(f"kb must be one of {KB_VARIANTS}")
    a = [str(AGENT), str(Path(dcp).resolve()), "--kb", kb]
    if kb == "pramana":
        if bundle is None or not (Path(bundle) / "context.md").exists():
            raise FileNotFoundError(f"kb=pramana needs a maya bundle with context.md: {bundle}")
        a += ["--kb-dir", str(Path(bundle).resolve())]
    return a


def dry_run(bundle, dcp, out_dir, kb: str, python: str | None = None) -> dict:
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cmd = [python or sys.executable] + _agent_args(dcp, kb, bundle) + ["--dry-run-prompt", str(out)]
    p = subprocess.run(cmd, cwd=str(out), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"dry run failed ({p.returncode}): {p.stderr[-2000:]}")
    return json.loads((out / f"system_prompt.{kb}.json").read_text())


def parse_run(run_dir: Path) -> dict:
    r: dict = {"agent_input_wns": None, "agent_best_wns": None, "llm_calls": None, "iterations": None,
               "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
               "cached_tokens": None, "cost_usd": None, "agent_runtime_s": None, "tool_calls": None}
    tu = Path(run_dir) / "token_usage.json"
    if tu.exists():
        s = json.loads(tu.read_text()).get("summary", {})
        r.update(agent_input_wns=s.get("initial_wns"), agent_best_wns=s.get("best_wns"),
                 llm_calls=s.get("total_llm_calls"), iterations=s.get("total_iterations"),
                 prompt_tokens=s.get("total_prompt_tokens"),
                 completion_tokens=s.get("total_completion_tokens"),
                 total_tokens=s.get("total_tokens"), cached_tokens=s.get("total_cached_tokens"),
                 cost_usd=s.get("total_cost"), agent_runtime_s=s.get("total_runtime_seconds"),
                 tool_calls=s.get("total_tool_calls"), clock_period_ns=s.get("clock_period_ns"))
    return r


def run(bundle, dcp, out_dir, kb: str, budget_s: float = 3600, model: str | None = None, *,
        key_env: str = "OPENROUTER_API_KEY", base_url: str | None = None,
        python: str | None = None, vivado_settings: str = VIVADO_SETTINGS,
        step_timeout_s: float = 900) -> dict:
    """Run the official agent on `dcp` with one KB variant, holding a Vivado slot throughout, then
    re-measure input and output independently. Returns the result (also out_dir/result.json)."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dcp = Path(dcp).resolve()
    if not os.environ.get(key_env):
        raise RuntimeError(f"API key env var {key_env} is not set")
    run_dir, output_dcp, log = out / "run", out / "output.dcp", out / "agent.log"
    args = _agent_args(dcp, kb, bundle) + ["--output", str(output_dcp), "--run-dir", str(run_dir),
                                            "--time-budget-s", f"{budget_s:.0f}"]
    if model:
        args += ["--model", model]
    if base_url:
        args += ["--base-url", base_url]
    py = python or sys.executable
    q = shlex.quote
    java = (f'export JAVA_HOME={q(JAVA11)}; ' if Path(JAVA11).exists() else
            'export JAVA_HOME="${JAVA_HOME:-$(ls -d "$XILINX_VIVADO"/tps/lnx64/jre* 2>/dev/null | head -n 1)}"; ')
    # The agent reads OPENROUTER_API_KEY itself; the key is expanded inside bash, never in argv.
    cmd = (f"source {q(vivado_settings)} >/dev/null 2>&1; " + java +
           'export PATH="$JAVA_HOME/bin:$PATH"; '
           f"export RAPIDWRIGHT_PATH={q(RAPIDWRIGHT_PATH)}; "
           'export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"; '
           f'export OPENROUTER_API_KEY="${{{key_env}}}"; '
           f"export PRAMANA_STEP_TIMEOUT_S={int(step_timeout_s)}; "   # one tool call / step at most
           f"cd {q(str(out))}; exec {q(py)} {' '.join(q(a) for a in args)} > {q(str(log))} 2>&1")
    need = est_mem_gb(dcp.stat().st_size / 1e6) + EXTRA_GB
    t0 = time.time()
    with vivado_slot():
        res = _run_in_slot(cmd, out, budget_s + GRACE_S, need)
        reap_under(out, log=print)   # the agent's Vivado outlives the agent (see pramana.vivado)
    r = parse_run(run_dir)
    # Independent measurement (pramana.measure): input once, output if it exists.
    m_in = measure_dcp(dcp, out / "measure_input")
    # the output is scored under the INPUT's clock constraints (an agent may have loosened one)
    in_clocks = [(c["name"], c["period"]) for c in ((m_in.get("result") or {}).get("clocks") or [])]
    m_out = (measure_dcp(output_dcp, out / "measure_output", clocks=in_clocks or None)
             if output_dcp.exists() else None)
    in_wns = (m_in.get("result") or {}).get("wns")
    out_wns = (m_out.get("result") or {}).get("wns") if m_out else None
    # the harness safety net (run/pramana_best.dcp, banked by the agent at each new best WNS):
    # measured the same way; the better valid one of output and banked best is scored
    best_dcp = run_dir / "pramana_best.dcp"
    m_best = (measure_dcp(best_dcp, out / "measure_best", clocks=in_clocks or None)
              if best_dcp.exists() else None)
    best_wns = (m_best.get("result") or {}).get("wns") if m_best else None
    cands = []
    if m_out and m_out["valid"] and out_wns is not None:
        cands.append((out_wns, "output"))
    if m_best and m_best["valid"] and best_wns is not None:
        cands.append((best_wns, "banked_best"))
    final_wns, final_from = max(cands) if cands else (in_wns, "input")
    r.update(kb=kb, dcp=str(dcp), bundle=str(bundle) if bundle else None, model=model,
             budget_s=budget_s, secs=round(time.time() - t0, 1), exit_code=res.exit_code,
             timed_out=res.timed_out, output_exists=output_dcp.exists(),
             input_wns=in_wns, output_wns=out_wns, output_valid=bool(m_out and m_out["valid"]),
             banked_best_wns=best_wns, banked_best_valid=bool(m_best and m_best["valid"]),
             # what ships: the better valid one of output and banked best, else the input
             final_wns=final_wns, final_from=final_from,
             valid=bool(cands),
             output_dcp=str(output_dcp), run_dir=str(run_dir), log=str(log))
    r["d_wns"] = (round(r["final_wns"] - in_wns, 4)
                  if r["final_wns"] is not None and in_wns is not None else None)
    (out / "result.json").write_text(json.dumps(r, indent=2))
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dcp", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--kb", required=True, choices=KB_VARIANTS)
    ap.add_argument("--bundle", type=Path, help="pramana maya bundle dir (for --kb pramana)")
    ap.add_argument("--budget-s", type=float, default=3600)
    ap.add_argument("--model", default=None, help="model id (default: the agent's default)")
    ap.add_argument("--key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--python", default=None)
    ap.add_argument("--step-timeout-s", type=float, default=900,
                    help="longest one Vivado command / analysis step may run (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.dry_run:
        r = dry_run(a.bundle, a.dcp, a.out, a.kb, python=a.python)
    else:
        r = run(a.bundle, a.dcp, a.out, a.kb, a.budget_s, a.model, key_env=a.key_env, step_timeout_s=a.step_timeout_s,
                base_url=a.base_url, python=a.python)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()

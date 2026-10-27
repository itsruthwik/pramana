"""Evaluate the maya LLM agent on one DCP with a chosen knowledge-base variant.

  none    : maya's tool/protocol prompt only (SYSTEM_PROMPT_PROTOCOL.TXT), no recipe knowledge
  hand    : maya's hand-written knowledge, unchanged (SYSTEM_PROMPT.TXT + ::opt:: reference)
  pramana : protocol prompt + <bundle>/context.md, with <bundle>/recipes.tcl sourced

maya (consumers/maya, a patched copy, see consumers/maya/SOURCE.md) runs as a subprocess and
launches Vivado itself, so this holds one machine-wide Vivado slot (pramana.vivado) for the
whole run. Layout of out_dir: maya.log, run/ (maya's run dir), output.dcp, result.json.

  python -m pramana.maya_eval --dcp X.dcp --out OUT --kb pramana --bundle campaigns/c/out/maya \
      --provider openrouter --model deepseek/deepseek-v4-pro --key-env OPENROUTER_API_KEY
  python -m pramana.maya_eval --dcp X.dcp --out OUT --kb pramana --bundle ... --dry-run
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
from .vivado import _run_in_slot, est_mem_gb, vivado_slot

MAYA_DIR = REPO / "consumers" / "maya"
MAYA = MAYA_DIR / "maya.py"
# Built RapidWright checkout used by maya's RapidWrightMCP server (read-only use).
RAPIDWRIGHT_PATH = os.environ.get("MAYA_RAPIDWRIGHT_PATH",
                                  "/mnt/vault0/rsunketa/FPL_contest/RapidWright")
# maya's own provider names (consumers/maya/maya.py _PROVIDER_DEFAULTS); anything starting
# with http(s):// is used as an OpenAI-compatible base URL instead.
PROVIDERS = ("openrouter", "advent", "asu_rc", "nvidia")
KB_VARIANTS = ("none", "hand", "pramana")
GRACE_S = 900          # maya self-terminates at its budget; hard kill this much later
MAYA_EXTRA_GB = 4.0    # RapidWright JVM + MCP servers on top of Vivado's own estimate


def _maya_args(dcp: Path, kb_variant: str, bundle_dir: Path | None) -> list[str]:
    if kb_variant not in KB_VARIANTS:
        raise ValueError(f"kb_variant must be one of {KB_VARIANTS}")
    a = [str(MAYA), str(Path(dcp).resolve()), "--kb", kb_variant]
    if kb_variant == "pramana":
        if bundle_dir is None or not (Path(bundle_dir) / "context.md").exists():
            raise FileNotFoundError(f"kb=pramana needs a maya bundle with context.md: {bundle_dir}")
        a += ["--kb-dir", str(Path(bundle_dir).resolve())]
    return a


def dry_run(bundle_dir, dcp, out_dir, kb_variant, python: str | None = None) -> dict:
    """Build and save maya's system prompt for the variant (no Vivado, no LLM)."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cmd = [python or os.environ.get("MAYA_PYTHON", sys.executable)]
    cmd += _maya_args(dcp, kb_variant, bundle_dir) + ["--dry-run-prompt", str(out)]
    p = subprocess.run(cmd, cwd=str(out), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"maya dry run failed ({p.returncode}): {p.stderr[-2000:]}")
    return json.loads((out / f"system_prompt.{kb_variant}.json").read_text())


def parse_run(run_dir: Path, output_dcp: Path | None = None) -> dict:
    """Final result from maya's run dir: token_usage.json (WNS, tokens, cost) and
    attempts.jsonl (what the ship gate banked)."""
    run_dir = Path(run_dir)
    r: dict = {"valid": None, "input_wns": None, "final_wns": None, "d_wns": None,
               "maya_best_wns": None, "llm_calls": None, "prompt_tokens": None,
               "completion_tokens": None, "total_tokens": None, "cached_tokens": None,
               "cost_usd": None, "maya_runtime_s": None, "attempts": 0, "banked": 0}
    tu = run_dir / "token_usage.json"
    if tu.exists():
        s = json.loads(tu.read_text()).get("summary", {})
        r.update(input_wns=s.get("initial_wns"), maya_best_wns=s.get("best_wns"),
                 llm_calls=s.get("total_llm_calls"), prompt_tokens=s.get("total_prompt_tokens"),
                 completion_tokens=s.get("total_completion_tokens"),
                 total_tokens=s.get("total_tokens"), cached_tokens=s.get("total_cached_tokens"),
                 cost_usd=s.get("total_cost"), maya_runtime_s=s.get("total_runtime_seconds"),
                 clock_period_ns=s.get("clock_period_ns"))
    banked = []
    att = run_dir / "attempts.jsonl"
    if att.exists():
        rows = [json.loads(ln) for ln in att.read_text().splitlines() if ln.strip()]
        r["attempts"] = len(rows)
        banked = [a for a in rows if a.get("accepted") and a.get("wns") is not None]
        r["banked"] = len(banked)
    # What ships: the best gated attempt, else the untouched input.
    if banked:
        r["final_wns"] = max(a["wns"] for a in banked)
    else:
        r["final_wns"] = r["input_wns"]
    if r["final_wns"] is not None and r["input_wns"] is not None:
        r["d_wns"] = round(r["final_wns"] - r["input_wns"], 4)
    # valid = maya's ship gate (routed, 0 route errors, WHS>=0, WPWS>=0) held on every banked
    # design and the output checkpoint exists. Not an independent re-measurement.
    out_ok = output_dcp is None or Path(output_dcp).exists()
    r["valid"] = bool(out_ok and tu.exists() and all(a.get("gate_ok") for a in banked))
    return r


def run(bundle_dir, dcp, out_dir, kb_variant: str, budget_s: float = 3600,
        provider: str = "openrouter", model: str | None = None, *,
        key_env: str | None = None, python: str | None = None,
        vivado_settings: str = VIVADO_SETTINGS) -> dict:
    """Run maya on `dcp` with one KB variant, holding a Vivado slot throughout.

    provider: one of PROVIDERS or an OpenAI-compatible base URL. key_env: the env var that
    holds the API key (default OPENROUTER_API_KEY for openrouter, else LLM_API_KEY).
    Returns the parsed result (see parse_run) plus run metadata; also written to
    out_dir/result.json."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dcp = Path(dcp).resolve()
    key_env = key_env or ("OPENROUTER_API_KEY" if provider == "openrouter" else "LLM_API_KEY")
    if not os.environ.get(key_env):
        raise RuntimeError(f"API key env var {key_env} is not set")
    run_dir, output_dcp, log = out / "run", out / "output.dcp", out / "maya.log"
    args = _maya_args(dcp, kb_variant, bundle_dir) + [
        "--output", str(output_dcp), "--run-dir", str(run_dir),
        "--time-budget-min", f"{budget_s / 60.0:.3f}"]
    if provider.startswith(("http://", "https://")):
        args += ["--base-url", provider]
    elif provider in PROVIDERS:
        args += ["--provider", provider]
    else:
        raise ValueError(f"provider must be one of {PROVIDERS} or a base URL")
    if model:
        args += ["--model", model]
    py = python or os.environ.get("MAYA_PYTHON", sys.executable)
    q = shlex.quote
    # The key is passed by env-var NAME and expanded inside bash, so it never appears in argv.
    # maya reads OPENROUTER_API_KEY first, then LLM_API_KEY.
    key = (f'export LLM_API_KEY="${{{key_env}}}"; '
           + ("" if key_env == "OPENROUTER_API_KEY" else "unset OPENROUTER_API_KEY; "))
    cmd = (f"source {q(vivado_settings)} >/dev/null 2>&1; "
           'export JAVA_HOME="${JAVA_HOME:-$(ls -d "$XILINX_VIVADO"/tps/lnx64/jre21* 2>/dev/null | head -n 1)}"; '
           'export PATH="$JAVA_HOME/bin:$PATH"; '
           f"export RAPIDWRIGHT_PATH={q(RAPIDWRIGHT_PATH)}; "
           f'export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"; '
           + key +
           f"exec {q(py)} {' '.join(q(a) for a in args)} > {q(str(log))} 2>&1")
    need = est_mem_gb(dcp.stat().st_size / 1e6) + MAYA_EXTRA_GB
    t0 = time.time()
    with vivado_slot():
        res = _run_in_slot(cmd, out, budget_s + GRACE_S, need)
    r = parse_run(run_dir, output_dcp)
    r.update(kb=kb_variant, dcp=str(dcp), bundle=str(bundle_dir) if bundle_dir else None,
             provider=provider, model=model, budget_s=budget_s, secs=round(time.time() - t0, 1),
             exit_code=res.exit_code, timed_out=res.timed_out, output_dcp=str(output_dcp),
             run_dir=str(run_dir), log=str(log))
    (out / "result.json").write_text(json.dumps(r, indent=2))
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dcp", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--kb", required=True, choices=KB_VARIANTS)
    ap.add_argument("--bundle", type=Path, help="pramana maya bundle dir (for --kb pramana)")
    ap.add_argument("--budget-s", type=float, default=3600)
    ap.add_argument("--provider", default="openrouter",
                    help=f"one of {PROVIDERS} or an OpenAI-compatible base URL")
    ap.add_argument("--model", default=None, help="model id (default: the provider's default)")
    ap.add_argument("--key-env", default=None, help="env var holding the API key")
    ap.add_argument("--python", default=None, help="python with maya's requirements "
                    "(default: $MAYA_PYTHON or this interpreter)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only build + save the system prompt (no Vivado, no LLM)")
    a = ap.parse_args(argv)
    if a.dry_run:
        r = dry_run(a.bundle, a.dcp, a.out, a.kb, python=a.python)
    else:
        r = run(a.bundle, a.dcp, a.out, a.kb, a.budget_s, a.provider, a.model,
                key_env=a.key_env, python=a.python)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()

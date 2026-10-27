# Copyright (c) 2026, Advent Lab ASU. All rights reserved.
# Morpheus is the Greek god of dreams, specifically known as the shaper or moulder of dreams that appear in human form.
# Maya is a name of Sanskrit origin meaning "illusion" or "magic", often associated with creativity and transformation. 

"""
FPGA Design Optimization Agent

An autonomous AI agent that analyzes FPGA designs and applies optimizations
using RapidWright and Vivado via MCP servers.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

# API provider URLs
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ADVENT_BASE_URL     = "http://10.218.101.247:11434/v1"   # e.g. Ollama, vLLM, or any OpenAI-compatible endpoint
ASU_RC_BASE_URL     = "https://openai.rc.asu.edu/v1"     # ASU Research Computing OpenAI-compatible endpoint
NVIDIA_BASE_URL     = "https://integrate.api.nvidia.com/v1"        # NVIDIA OpenAI-compatible endpoint

# Active provider: "openrouter" | "advent" | "asu_rc"
PROVIDER = "openrouter"

# Models — one default per provider
OPENROUTER_MODEL = "deepseek/deepseek-v4-pro"
ADVENT_MODEL     = "gemma4-50k"  # model name as served by your Advent Petra server
ASU_RC_MODEL     = "minimax-m2-7-high"      # model name as served by ASU RC
NVIDIA_MODEL     = "nvidia/nemotron-3-ultra-550b-a55b"  # model name as served by NVIDIA

_PROVIDER_DEFAULTS = {
    "openrouter": (OPENROUTER_BASE_URL, OPENROUTER_MODEL),
    "advent":     (ADVENT_BASE_URL,     ADVENT_MODEL),
    "asu_rc":     (ASU_RC_BASE_URL,     ASU_RC_MODEL),
    "nvidia":     (NVIDIA_BASE_URL,     NVIDIA_MODEL),
}
assert PROVIDER in _PROVIDER_DEFAULTS, f"PROVIDER must be one of {list(_PROVIDER_DEFAULTS)}, got '{PROVIDER}'"

DEFAULT_BASE_URL = _PROVIDER_DEFAULTS[PROVIDER][0]
DEFAULT_MODEL    = _PROVIDER_DEFAULTS[PROVIDER][1]



# Pure Vivado-report parsing and the ship-safety decisions derived from it.
# ONE HOME (vivado_parse.py): these used to be duplicated across this file and
# dcp_optimizer.py, and the copies had drifted into two separate live bugs.
from vivado_parse import (  # noqa: F401  (TimingSummary/phys_opt_inert/route_* re-exported for tests)
    CORE_REPORTS,
    DESIGN_ANALYSIS_COMBINED,
    design_analysis_raw,
    EXTENDED_CHECKS,
    HEAVY_OP_SECONDS,
    RECIPE_LIB,
    RECIPES_DIR,
    REFRESH_MIN_REMAINING_FRAC,
    TimingSummary,
    assess_dcp_weight,
    atomic_copy,
    build_state_digest,
    classify_design,
    gather_vivado_reports,
    format_device_utilization,
    hold_pw_ok,
    parse_congestion_level,
    parse_crit_hierarchies,
    parse_device_utilization,
    parse_high_fanout_nets,
    parse_lut_utilization,
    parse_primitive_count,
    parse_qor_assessment,
    parse_route_status,
    parse_timing_summary,
    parse_top_hierarchies,
    parse_worst_path_delay_split,
    phys_opt_inert,
    render_analysis,
    render_failed_attempt,
    render_raw,
    route_bad_net_count,
    route_routable_count,
)
from plan_script import (
    PlanAttempt,
    advice_problems,
    extract_tcl_script,
    fatal_problems,
    label_script,
    normalize_script,
    parse_script_result,
    script_mutates,
    validate_script,
    wrap_for_source,
)


# Core diagnostic reports (always run; parsed in non-raw mode). (key, tcl-command)
# --- "Heavy" (large) design detection -----------------------------------------------------
# A design is HEAVY when a full place/route won't fit the 1-hour budget and slow analyses
# (e.g. RapidWright net-detour) can eat the whole window. Two complementary signals:
#   PRIMARY  (static, zero-cost, checkable BEFORE opening): DCP file size on disk.
#   BACKSTOP (live, adapts to machine load/contention): how long open/first-report took.
# With the 13 contest DCPs these thresholds flag exactly {boom_soc 139MB, ispd16 146MB}
# (next-largest is corescore at 64MB / 48s) — tune here if the benchmark set changes.
# LLM spend bounds. The contest allows $1 per benchmark; exceeding it is a rules
# violation, not a low score, so both are enforced rather than merely reported.
# The cost stop is deliberately below $1: OpenRouter reports a call's cost only
# AFTER it returns, so the check can only ever be "would another call of roughly
# this size still fit". The headroom absorbs one larger-than-average call.
MAX_LLM_CALLS = 60
MAX_LLM_COST_USD = 0.85

# Per-call LLM timeout. The client is SYNCHRONOUS and called from the async loop;
# without a bound the SDK default is 600 s x silent retries, and because the call
# does not yield, the outer asyncio.wait_for(deadline) cannot cancel it -- a hung
# provider call would block past the wall-clock cap and drop the run onto the
# os._exit backstop instead of the graceful finalize. The real fix is dispatching
# the call through asyncio.to_thread (so wait_for CAN cancel at the deadline); this
# bound additionally stops a single stuck attempt from wasting a large mid-run slice.
# 240 s is generous for a reasoning turn yet far under the 3600 s budget and the
# finalize reserve.
LLM_CALL_TIMEOUT_S = 240.0

# time_budget_seconds is the TOTAL wall-clock cap per DCP (the contest's 3600 s). It must
# contain EVERYTHING: optimization AND the graceful finalize + reporting tail. So the
# optimization loop stops FINALIZE_RESERVE_S before the cap, leaving that window for the
# tail to run and the process to self-terminate before the external clock kills it.
#
# The reserve is SMALL on purpose. It is NOT protecting the scored DCP from a runtime
# overage -- output_dcp is refreshed on every gated promotion and is valid at every
# instant, so even a hard kill at the cap ships a valid design (never a zero). The reserve
# only buys a clean end: flush token_usage.json + the summary and exit gracefully. 60 s
# is ample for that tail (a file copy + two small writes); the backstop covers the rest.
FINALIZE_RESERVE_S = 60.0

# Plan scripts run with NO per-command timeout (VivadoMCP run_tcl timeout=0): a full
# place+route on a giant design is 130+ min, and abandoning it mid-run left the session
# in a "no sentinel" limbo. The hard cap is the deadline backstop (os._exit at the cap),
# which ships the always-shippable best snapshot; the finalize tail is protected there,
# not by clipping each script.


class LLMBudgetExceeded(RuntimeError):
    """Raised instead of starting an LLM call that would breach the contest cap.
    The loop catches it and finalizes with whatever has been banked."""


# Tools that may CHANGE the open design, so a cached ship-gate verdict about the
# previous state is no longer valid. Names are the MCP tool names WITHOUT the
# server prefix, as passed to session.call_tool.
#
# run_tcl is included on purpose even though it is usually a query: it is opaque
# and the model does use it to mutate. The asymmetry is what matters -- an
# unnecessary bump costs one report_route_status, a missing one ships a design
# that was never verified.
_DESIGN_MUTATING_TOOLS = frozenset({
    "open_checkpoint", "place_design", "route_design", "phys_opt_design",
    "sweep_place_directives", "sweep_phys_opt_directives",
    "uncombine_and_replace", "scoped_replace",
    "run_tcl", "restart_vivado",
})

# SCRIPT-FIRST loop, NO tools: the model is given no OpenAI/MCP tool list at all. It
# replies with ONE Tcl script (a fenced code block); extract_tcl_script pulls it out and
# process_response runs it through the same validated path a run_tcl tool-call used to take
# (script_mutates routes mutation -> _run_plan_script; a query script -> the raw path).
# Every MCP tool -- queries, the discrete place/route/phys_opt/opt levers and sweeps,
# write_*, checkpoint open/close, and ALL RapidWright tools -- is reachable to the HARNESS
# via session.call_tool (that is how diagnosis, measurement and recovery run), but is never
# named to the model. The model's whole surface is "emit Tcl"; how it executes is the
# harness's business.

def _filter_opt_reference(text: str) -> str:
    """Drop the sections of the vendored ::opt:: reference that contradict this
    harness.

    The file was written for the ruthwik branch, whose planner drives the whole
    loop from a Tcl script. Two of its sections tell the model to do exactly what
    we have just spent the prompt telling it NOT to do:

      "Diagnosis & targeting"  -- diagnose / analyze_paths / wns / timing_summary /
                                  report_metric. We measure FOR the model and hand
                                  it the state; these are harness-side here.
      "Typical agentic loop"   -- opens with `report_metric base` -> `diagnose 10`
                                  and closes with "write_checkpoint to save", i.e.
                                  measure, re-diagnose, and save the output DCP.
                                  All three are forbidden in this harness.

    Appending the file verbatim put those instructions AFTER our rules, where they
    read as the most recent word. Keep the fix tables, which are the reason we load
    the library at all.
    """
    drop = ("## Diagnosis & targeting", "## Typical agentic loop")
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = any(line.startswith(d) for d in drop)
        if not skipping:
            out.append(line)
    return "\n".join(out)


# --- [pramana] knowledge-base variant -------------------------------------------------------
# --kb {none,hand,pramana} / --kb-dir <bundle>, carried in env vars so the dev-modes module
# (which re-imports this file as `maya`) sees the same setting. "hand" = original behaviour.
#   none    : SYSTEM_PROMPT_PROTOCOL.TXT (protocol only) + keep-best guard reference
#   pramana : the same + <kb-dir>/context.md + a ::pramana:: proc reference; recipes.tcl sourced
# In none/pramana the hand-written '=>' derived reads and the large-design advice are also
# stripped from the initial analysis, and the plateau panel is made knowledge-neutral.
KB_VARIANTS = ("none", "hand", "pramana")


def _kb_variant() -> str:
    v = os.environ.get("MAYA_KB", "hand")
    assert v in KB_VARIANTS, f"MAYA_KB must be one of {KB_VARIANTS}, got {v!r}"
    return v


def _kb_dir() -> Optional[Path]:
    d = os.environ.get("MAYA_KB_DIR")
    return Path(d).resolve() if d else None


_KB_GUARD_REF = (
    "\n\n==========================================================\n"
    "KEEP-BEST GUARD — ALREADY SOURCED INTO THIS SESSION\n"
    "==========================================================\n"
    "Write it DIRECTLY as a line in your Tcl script — do NOT wrap it in run_tcl or any other\n"
    "command; your reply IS the script. It MUST be called with its namespace prefix:\n"
    "    ::opt::try_keep_if_better {<one or more design-changing Tcl commands>}\n"
    "It checkpoints, runs the body, and KEEPS the result only if setup WNS did not get worse AND\n"
    "hold was not pushed further into violation — otherwise it reloads the checkpoint. Returns 1\n"
    "kept / 0 reverted. After a revert, cell/net handles captured earlier are stale: pass NAMES.\n")

_KB_PRAMANA_REF = (
    "\n\n==========================================================\n"
    "::pramana:: PROCS — recipes.tcl ALREADY SOURCED INTO THIS SESSION\n"
    "==========================================================\n"
    "The procs named in the learned knowledge below exist in this session. Call them with the\n"
    "::pramana:: prefix as plain lines of your script. Each assumes an open ROUTED design and\n"
    "leaves it routed.\n"
    "  ::pramana::policy_<name>                  the learned starting strategy (a chain of guarded steps)\n"
    "  ::pramana::keep_if_better <tag> {<Tcl>}   run <Tcl>; keep only if routed, hold met and WNS improved\n"
    "  ::pramana::recipe_<...>                   a discovered recipe; NOT self-guarding — wrap it:\n"
    "      ::opt::try_keep_if_better {::pramana::recipe_<...>}\n")


def _strip_derived_reads(text: str) -> str:
    """[pramana] none/pramana variants: drop the hand-written knowledge that render_analysis
    mixes into the initial analysis — the '=>' derived-read lines (one line each), their
    legend, and the large-design strategy advice (keeping the factual LARGE DESIGN marker)."""
    from vivado_parse import DERIVED_READ_LEGEND
    text = text.replace(DERIVED_READ_LEGEND + "\n", "").replace(DERIVED_READ_LEGEND, "")
    text = re.sub(r"(\*\*\* LARGE DESIGN \([^)]*\)\.).*?\*\*\*",
                  r"\1 Nothing banks until a script returns. ***", text, flags=re.DOTALL)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("=>"))


def _kb_prompt_parts(variant: str, recipes_loaded: bool) -> tuple[str, str]:
    """[pramana] (template, appendix) for the none/pramana variants. Only the template is
    .format()ted; everything knowledge-bearing goes in the appendix (never formatted)."""
    template = (Path(__file__).parent.resolve() / "SYSTEM_PROMPT_PROTOCOL.TXT").read_text()
    appendix = _KB_GUARD_REF if recipes_loaded else ""
    if variant == "pramana":
        kb = _kb_dir()
        if kb is None or not (kb / "context.md").exists():
            raise FileNotFoundError(f"--kb pramana needs --kb-dir <bundle> with context.md (got {kb})")
        if recipes_loaded:
            appendix += _KB_PRAMANA_REF
        appendix += ("\n\n==========================================================\n"
                     "LEARNED KNOWLEDGE BASE\n"
                     "==========================================================\n"
                     + (kb / "context.md").read_text())
    else:
        appendix += ("\n\nNo prior optimization knowledge is provided: choose levers from the "
                     "diagnosis, the measured feedback, and your own understanding of Vivado.\n")
    return template, appendix


def load_system_prompt(recipes_loaded: bool = False) -> str:
    """Load SYSTEM_PROMPT.TXT, appending the ::opt:: reference when the library is
    actually sourced into the session.

    Appended CONDITIONALLY and from the same vendored file the session loads, so the
    prompt cannot claim a capability the session does not have. Before this, maya
    sourced 40 KB of ::opt:: Tcl into every run and never told the model it existed
    -- the recipes were reachable through run_tcl and unusable in practice.

    THE RETURN VALUE IS NOT A .format() TEMPLATE. Only SYSTEM_PROMPT.TXT carries the
    {temp_dir} / {input_dcp} placeholders; the appended reference documents *Tcl*, and Tcl
    is made of braces -- `try_keep_if_better {...}`, `{cell physpin slack}`,
    `{I0:A6 I1:A5 ...}`. str.format() reads every one of those as a replacement field.
    Formatting the CONCATENATION therefore raised KeyError/IndexError and killed the run
    before its first LLM call, on all 12 benchmarks. SYSTEM_PROMPT.TXT alone formats fine,
    which is exactly why nothing caught it.

    Callers must format the TEMPLATE FIRST and append the reference AFTER --
    see `load_system_prompt_parts()`, which does that for you.
    """
    script_dir = Path(__file__).parent.resolve()
    prompt_file = script_dir / "SYSTEM_PROMPT.TXT"

    try:
        with open(prompt_file, 'r') as f:
            text = f.read()
    except FileNotFoundError:
        logger.error(f"System prompt file not found: {prompt_file}")
        raise
    except Exception as e:
        logger.error(f"Failed to load system prompt: {e}")
        raise

    ref = RECIPES_DIR / "opt_recipe_context.md"
    if recipes_loaded and ref.exists():
        try:
            body = _filter_opt_reference(ref.read_text())
            text += (
                "\n\n==========================================================\n"
                "::opt:: RECIPE LIBRARY — ALREADY SOURCED INTO THIS SESSION\n"
                "==========================================================\n"
                "Write these DIRECTLY as lines in your Tcl script — do NOT wrap them in run_tcl or\n"
                "any other command; your reply IS the script. EVERY recipe is in the ::opt::\n"
                "namespace and MUST be called with that prefix (e.g. ::opt::safe_phys_opt ..., not\n"
                "safe_phys_opt) — a bare name is an invalid Tcl command and aborts the rest of your\n"
                "script. For example, one line:\n"
                "    ::opt::try_keep_if_better {phys_opt_design -directive Explore}\n"
                "try_keep_if_better checkpoints, runs the body, and KEEPS it only if setup WNS did\n"
                "not get worse AND hold was not pushed further into violation — otherwise it\n"
                "reloads. That is the keep-best discipline for free, per stage, in one call.\n\n"
                + body)
            logger.info("Appended the ::opt:: recipe reference to the system prompt")
        except Exception as e:
            logger.warning(f"Could not append the ::opt:: reference: {e}")
    return text


def load_system_prompt_parts(recipes_loaded: bool = False) -> tuple[str, str]:
    """(template, appendix) — the ONLY safe way to build the system prompt.

    `template` is SYSTEM_PROMPT.TXT and is the only part that may be .format()ted.
    `appendix` is the ::opt:: Tcl reference (or "") and must be concatenated AFTER
    formatting, never before. See load_system_prompt() for what happens otherwise.

    NOTE: a generated "command contract" (per-command directive lists + forbidden tokens)
    used to be prepended here. It was REVERTED 2026-08-10: handing the model a COMPLETE,
    bounded directive menu made it treat the list as a checklist — it cycled the listed
    directives in a handful of attempts, declared "every lever class exhausted", and the
    done-stop honoured that EARLY, leaving 30-47 min of the budget unused (regression
    confirmed vs the pre-contract run, where designs used the full hour). validate_script
    still enforces the same directive/forbidden rules from the constants; the prompt just
    no longer enumerates them.
    """
    if _kb_variant() != "hand":                       # [pramana]
        return _kb_prompt_parts(_kb_variant(), recipes_loaded)
    template = load_system_prompt(recipes_loaded=False)
    full = load_system_prompt(recipes_loaded=recipes_loaded)
    opt_reference = full[len(template):]
    return template, opt_reference


class DCPOptimizerBase:
    """Base class with shared functionality for FPGA optimization."""
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None):
        self.debug = debug
        
        # Create run directory if not provided
        if run_dir is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.run_dir = Path.cwd() / f"maya_run-{timestamp}"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created run directory: {self.run_dir}")
        else:
            self.run_dir = run_dir
            self.run_dir.mkdir(parents=True, exist_ok=True)
        
        self.exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None
        
        # Use run directory for all temporary files
        self.temp_dir = self.run_dir
        logger.info(f"Working directory: {self.temp_dir}")
        
        # Timing tracking
        self.initial_wns = None
        self.initial_tns = None
        self.initial_failing_endpoints = None
        # Baseline hold (WHS) and pulse-width (WPWS) slack of the INPUT DCP,
        # populated by _gather_timing. Initialised here so an early-failure path
        # that reads them before the first timing report cannot AttributeError.
        self.initial_whs = None
        self.initial_pw = None
        self.high_fanout_nets = []
        self.clock_period = None
        self.target_clock = None  # Set to clock name (e.g. "clk_fpl26contest") for clock-specific Fmax
        
        # Log file handles
        self._rw_log_file = None
        self._v_log_file = None

        # Per-tool-call log files (one file per call, written to tools/ subdir)
        self._tool_call_count = 0
        self._tool_logs_dir: Optional[Path] = None
        self._prompt_logs_dir: Optional[Path] = None
    
    async def start_servers(self, log_prefix: str = ""):
        """Start and connect to both MCP servers."""
        script_dir = Path(__file__).parent.resolve()
        
        # Create log files in run directory
        rapidwright_log = self.run_dir / "rapidwright.log"
        rapidwright_mcp_log = self.run_dir / "rapidwright-mcp.log"
        vivado_log = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"
        vivado_mcp_log = self.run_dir / "vivado-mcp.log"
        # Create per-tool-call log directory
        self._tool_logs_dir = self.run_dir / "tool_outputs"
        self._tool_logs_dir.mkdir(exist_ok=True)
        logger.info(f"Per-call tool output logs: {self._tool_logs_dir}/")
        self._prompt_logs_dir = self.run_dir / "llm_prompts"
        self._prompt_logs_dir.mkdir(exist_ok=True)
        logger.info(f"Per-call LLM prompt logs: {self._prompt_logs_dir}/")

        # Open log files (if not in debug mode, redirect stderr to log)
        if self.debug:
            self._rw_log_file = None
            self._v_log_file = None
            logger.info("Debug mode: MCP server output will be shown in console")
            if log_prefix:
                print(f"{log_prefix} Debug mode: MCP server output will be shown in console")
        else:
            self._rw_log_file = open(rapidwright_mcp_log, 'w')
            self._v_log_file = open(vivado_mcp_log, 'w')
            logger.info(f"RapidWright Java output: {rapidwright_log}")
            logger.info(f"RapidWright MCP output: {rapidwright_mcp_log}")
            logger.info(f"Vivado output: {vivado_log}")
            logger.info(f"Vivado journal: {vivado_journal}")
            logger.info(f"Vivado MCP output: {vivado_mcp_log}")
            print(f"Log files in {self.run_dir.name}/: {rapidwright_log.name}, {rapidwright_mcp_log.name}, {vivado_log.name}, {vivado_journal.name}, {vivado_mcp_log.name}, tool_outputs/, llm_prompts/")
        
        # RapidWright MCP server config
        rapidwright_args = [str(script_dir / "RapidWrightMCP" / "server.py")]
        if not self.debug:
            rapidwright_args.extend([
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log)
            ])
        
        env = {**os.environ}
        rapidwright_submodule = script_dir / "RapidWright"
        if rapidwright_submodule.is_dir() and "RAPIDWRIGHT_PATH" not in env:
            env["RAPIDWRIGHT_PATH"] = str(rapidwright_submodule)
            env["CLASSPATH"] = f"{rapidwright_submodule}/bin:{rapidwright_submodule}/jars/*"
        
        rapidwright_config = {
            "command": sys.executable,
            "args": rapidwright_args,
            "cwd": str(self.run_dir),
            "env": env
        }
        
        # Vivado MCP server config
        vivado_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            vivado_args.extend([
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal)
            ])
        
        vivado_config = {
            "command": sys.executable,
            "args": vivado_args,
            "cwd": str(self.run_dir),
            "env": {**os.environ}
        }
        
        # Start RapidWright MCP
        logger.info("Starting RapidWright MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting RapidWright MCP server...")
        start_time = time.time()
        
        rw_params = StdioServerParameters(**rapidwright_config)
        rw_transport = await self.exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=self._rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self.exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await self.rapidwright_session.initialize()
        
        elapsed = time.time() - start_time
        logger.info(f"RapidWright MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} RapidWright MCP server started in {elapsed:.2f}s")
        
        # Start Vivado MCP
        logger.info("Starting Vivado MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting Vivado MCP server...")
        start_time = time.time()
        
        vivado_params = StdioServerParameters(**vivado_config)
        vivado_transport = await self.exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=self._v_log_file)
        )
        v_read, v_write = vivado_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await self.vivado_session.initialize()
        
        elapsed = time.time() - start_time
        logger.info(f"Vivado MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} Vivado MCP server started in {elapsed:.2f}s")
        
        logger.info("Both MCP servers connected")
        if log_prefix:
            print(f"{log_prefix} Both MCP servers connected successfully")

    def _write_tool_log(self, tool_name: str, arguments: dict, output: str):
        """Write a single tool call's output to its own numbered log file."""
        if self._tool_logs_dir is None:
            return
        self._tool_call_count += 1
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', tool_name)
        log_path = self._tool_logs_dir / f"{self._tool_call_count:04d}_{safe_name}.log"
        with open(log_path, 'w') as f:
            f.write(f"Tool:  {tool_name}\n")
            f.write(f"Args:  {json.dumps(arguments)}\n")
            f.write("----#" * 16 + "\n")
            f.write(output)
            f.write("\n")

    async def _shutdown_vivado(self) -> None:
        """Ask the Vivado process to exit, before its transport is torn down.

        Sends `exit` down the live Tcl console. That is the one instruction that
        reaches the actual vivado binary rather than the loader shell wrapping it,
        which is why it succeeds where killing the tracked PID does not.

        Purely best-effort: every failure mode here is survivable (the teardown
        guard and, on the deadline path, _kill_descendants both still run), and a
        run must never fail because its cleanup was untidy. Subclasses that never
        opened a Vivado session simply have nothing to do.
        """
        session = getattr(self, "vivado_session", None)
        if session is None:
            return
        try:
            await session.call_tool("run_tcl", {"command": "exit"})
            logger.info("cleanup: asked Vivado to exit")
        except Exception as e:
            # Expected: the console dies mid-command, so the call often does not
            # return cleanly. That is success, not failure.
            logger.debug(f"cleanup: Vivado exit returned {e!r} (expected)")

    async def cleanup(self):
        """Clean up resources. Robust: never hang on a stuck MCP/Vivado teardown (e.g. after a
        deadline stop where Vivado was mid-place/route), and never let a teardown error change
        the process exit code.

        NOTE: exit_stack.aclose() MUST be awaited in THIS task. The MCP stdio_client holds anyio
        cancel scopes entered in this task, and anyio requires a scope be exited in the task that
        entered it. Wrapping the close in asyncio.wait_for() runs it in a NEW task and raises
        "Attempted to exit cancel scope in a different task than it was entered in" — whose
        CancelledError (a BaseException) then escapes and clobbers a successful sys.exit(0).
        Hang protection therefore comes from a watchdog THREAD, not from wait_for.
        """
        killer = getattr(self, "_kill_descendants", None)
        guard = None
        if killer is not None:
            def _force_teardown():
                try:
                    sys.stderr.write("\n*** cleanup: MCP/Vivado teardown wedged — force-killing "
                                     "leftover children. ***\n")
                    sys.stderr.flush()
                except Exception:
                    pass
                try:
                    killer()
                except Exception:
                    pass
            guard = threading.Timer(60.0, _force_teardown)
            guard.daemon = True
            guard.start()

        # Tell Vivado to quit ITSELF, while the transport is still up. Closing the
        # MCP transport is not enough: the server tracks the pexpect PID, which is
        # the `loader` shell script, and that script FORKS vivado rather than
        # exec'ing it. Killing the loader therefore leaves the real vivado process
        # alive and reparented to init -- where _kill_descendants, which walks our
        # own descendants, can no longer see it either.
        #
        # Measured after a 12-benchmark sweep at concurrency 4: 21 orphaned
        # sessions (42 processes) holding 81 GB, hours after every driver had
        # exited. On a smaller box, or a longer sweep, that is an OOM.
        #
        # Best-effort and last-resort-guarded: if `exit` wedges, the 60 s teardown
        # timer armed just above still force-kills.
        await self._shutdown_vivado()

        try:
            await self.exit_stack.aclose()          # same task — see note above
        except BaseException as e:                  # incl. CancelledError / anyio ExceptionGroup
            logger.warning(f"cleanup: MCP/Vivado teardown error (ignored): {e!r}")
            if killer is not None:
                try:
                    killer()
                except Exception:
                    pass
        finally:
            if guard is not None:
                guard.cancel()

        if self._rw_log_file:
            try:
                self._rw_log_file.close()
            except Exception:
                pass
        if self._v_log_file:
            try:
                self._v_log_file.close()
            except Exception:
                pass

        logger.info(f"Run directory preserved at: {self.run_dir}")

    async def write_initial_diagnosis(self, vivado_tool) -> None:
        """Gather the core Vivado diagnostic reports, classify the design (FACTS ONLY),
        and write initial_diagnosis.{txt,json} to the run dir. Best-effort; safe to call
        from any flow once the checkpoint is open. `vivado_tool` is the subclass's async
        tool caller, invoked as ``vivado_tool("run_tcl", {"command": ...})``.
        See TODO.md item 1 — the diagnosis is dumped for every DCP, every flow.
        """
        async def run_tcl(cmd):
            try:
                return await vivado_tool("run_tcl", {"command": f"{cmd} -return_string"}) or ""
            except Exception:
                return ""
        try:
            raw = {}
            # design-analysis-timing / congestion / complexity come from ONE combined
            # report, not from CORE_REPORTS — see DESIGN_ANALYSIS_COMBINED. They must be
            # SPLIT before parsing: fed the whole report, check_complexity reads the
            # -timing table's Requirement column as a Rent exponent.
            raw.update(design_analysis_raw(await run_tcl(DESIGN_ANALYSIS_COMBINED)))
            for key, tcl in CORE_REPORTS:
                raw[key] = await run_tcl(tcl)
            split = parse_worst_path_delay_split(raw.get("design-analysis-timing", ""))
            cong = parse_congestion_level(raw.get("congestion", ""))
            qor = parse_qor_assessment(raw.get("qor-assessment", ""))
            lut = parse_lut_utilization(raw.get("utilization", ""))
            diag = classify_design(
                split, cong, qor, lut,
                slices=(parse_device_utilization(raw.get("utilization", "")) or {}).get("slices"))
            self.diagnosis = diag
            lines = ["=== Initial Diagnosis (facts) ===", f"Category: {diag['category']}"]
            if split.get("net_pct") is not None:
                lines.append(f"Delay split: {split.get('logic_pct')}% logic / "
                             f"{split.get('net_pct')}% wire, {split.get('logic_levels')} levels")
            if split.get("hf_max"):
                lines.append(f"High-fanout: up to {split['hf_max']} loads on "
                             f"{split.get('hf_over', 0)} top paths")
            if cong:
                lines.append(f"Congestion: max Level {cong.get('max_level', 0)}")
            devfill = format_device_utilization(parse_device_utilization(raw.get("utilization", "")))
            if devfill:
                lines.append(f"Device fill: {devfill}")
            elif lut is not None:
                lines.append(f"Utilization: {lut:.1f}% CLB LUTs")
            if qor.get("score"):
                lines.append(f"QoR score: {qor['score']}")
            if qor.get("dont_touch"):
                lines.append(f"DONT_TOUCH cells/nets: {int(qor['dont_touch'])}")
            if qor.get("lut_combined"):
                lines.append(f"Max LUT Combined: {qor['lut_combined']}")
                if "REVIEW" in qor["lut_combined"].upper():
                    lines.append("=> S1 INDICATOR: high LUT combining on a WIRE-bound design — the "
                                 "combined pairs are placement-locked. Reach for the appended "
                                 "reference's recipe, keep-best-wrapped: "
                                 "::opt::try_keep_if_better {::opt::uncombine_and_replace} as an "
                                 "early surgical move; prefer it over replace_critical_cone, which "
                                 "is an after-plateau / scattered-cone lever, not a first move.")
            for r in diag.get("rationale", []):
                lines.append(f"  · {r}")
            for nt in diag.get("notes", []):
                lines.append(f"  => {nt}")
            (self.run_dir / "initial_diagnosis.txt").write_text("\n".join(lines) + "\n")
            (self.run_dir / "initial_diagnosis.json").write_text(json.dumps(diag, indent=2))
            logger.info(f"Initial diagnosis written to {self.run_dir}/initial_diagnosis.txt")
        except Exception as e:
            logger.warning(f"write_initial_diagnosis failed: {e}")

    def calculate_fmax(self, wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
        """
        Calculate achievable fmax in MHz based on WNS and clock period.
        
        fmax = 1 / (clock_period - WNS) when WNS < 0 (timing violation)
        fmax = 1 / clock_period when WNS >= 0 (timing met)
        
        Returns fmax in MHz, or None if cannot be calculated.
        """
        if clock_period is None or clock_period <= 0:
            return None
        if wns is None:
            return None
        
        achievable_period_ns = clock_period - wns
        if achievable_period_ns <= 0:
            return None
        
        return 1000.0 / achievable_period_ns
    
    async def get_clock_period(self, call_tool_fn) -> Optional[float]:
        """
        Query the clock period of the target clock from Vivado in nanoseconds.
        
        First checks for the contest clock 'clk_fpl26contest'. If found, uses its
        period and sets self.target_clock. Otherwise falls back to the endpoint clock
        of the worst setup timing path.
        
        Args:
            call_tool_fn: Function to call Vivado tools, should accept (tool_name, arguments)
        
        Returns the period of the target clock, or None if no clocks found.
        """
        tcl_cmd = (
            "set contest_clk [get_clocks -quiet clk_fpl26contest]; "
            "if {$contest_clk ne {}} { "
            "  puts \"CLOCK:clk_fpl26contest\"; "
            "  puts [get_property PERIOD $contest_clk]; "
            "} else { "
            "  set tp [get_timing_paths -max_paths 1 -setup]; "
            "  if {$tp ne {}} { "
            "    set clk [get_property ENDPOINT_CLOCK $tp]; "
            "    if {$clk ne {}} { "
            "      puts \"CLOCK:$clk\"; "
            "      puts [get_property PERIOD [get_clocks $clk]]; "
            "    } "
            "  } "
            "}"
        )
        try:
            result = await call_tool_fn("run_tcl", {"command": tcl_cmd})
            
            clock_name = None
            for token in result.strip().split():
                if token.startswith('CLOCK:'):
                    clock_name = token[len('CLOCK:'):]
                    continue
                if token.startswith('ERROR') or token.startswith('WARNING'):
                    continue
                try:
                    period = float(token)
                    if period > 0:
                        if clock_name:
                            self.target_clock = clock_name
                            logger.info(f"Target clock: {clock_name}, period: {period:.3f} ns")
                        else:
                            logger.info(f"Critical clock period: {period:.3f} ns")
                        return period
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get clock period: {e}")
        
        logger.warning("Could not determine clock period from Vivado")
        return None
    
    async def get_wns_for_target_clock(self, call_tool_fn) -> Optional[float]:
        """
        Get WNS specifically for the target clock domain.
        
        When target_clock is set (e.g. 'clk_fpl26contest'), queries WNS filtered
        to that clock's timing paths. Falls back to overall WNS if no target clock.
        
        Args:
            call_tool_fn: Function to call Vivado tools, should accept (tool_name, arguments)
        
        Returns WNS in nanoseconds, or None if query fails.
        """
        if self.target_clock:
            tcl_cmd = (
                f"set clk_obj [get_clocks -quiet {{{self.target_clock}}}]; "
                f"if {{$clk_obj ne {{}}}} {{ "
                f"  set tp [get_timing_paths -max_paths 1 -setup -to $clk_obj]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts 0.0}} "
                f"}} else {{ "
                f"  set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts 0.0}} "
                f"}}"
            )
        else:
            tcl_cmd = (
                "set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                "if {[llength $tp] > 0} {get_property SLACK $tp} else {puts 0.0}"
            )
        
        try:
            result = await call_tool_fn("run_tcl", {"command": tcl_cmd})
            for token in result.strip().split('\n'):
                token = token.strip()
                if not token or token.startswith('ERROR') or token.startswith('WARNING'):
                    continue
                try:
                    wns = float(token)
                    clock_info = f" (clock: {self.target_clock})" if self.target_clock else ""
                    logger.info(f"WNS{clock_info}: {wns:.3f} ns")
                    return wns
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get WNS for target clock: {e}")
        
        return None
    
    def _format_fmax_results(
        self,
        clock_period: Optional[float],
        initial_wns: Optional[float],
        result_wns: Optional[float],
        result_label: str = "Final",
    ) -> list[str]:
        """Format Fmax/WNS results block as a list of lines.
        
        """
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        result_fmax = self.calculate_fmax(result_wns, clock_period)
        result_fmax_label = f"{result_label} Fmax:"
        result_wns_label = f"{result_label} WNS:"
        
        lines: list[str] = []
        if initial_fmax is not None and result_fmax is not None:
            target_fmax = 1000.0 / clock_period
            fmax_change = result_fmax - initial_fmax
            lines.append(f"  {'Target Fmax:':<21s}{target_fmax:8.2f} MHz  (clock period: {clock_period:.3f} ns)")
            lines.append(f"  {'Initial Fmax:':<21s}{initial_fmax:8.2f} MHz  (WNS: {initial_wns:.3f} ns)")
            lines.append(f"  {result_fmax_label:<21s}{result_fmax:8.2f} MHz  (WNS: {result_wns:.3f} ns)")
            lines.append(f"  {'Fmax Improvement:':<21s}{fmax_change:+8.2f} MHz  (WNS: {result_wns - initial_wns:+.3f} ns)")
        else:
            if clock_period is not None:
                target_fmax = 1000.0 / clock_period
                lines.append(f"  {'Clock period:':<21s}{clock_period:8.3f} ns (target: {target_fmax:.2f} MHz)")
            if initial_wns is not None:
                fmax_str = f"  (fmax: {initial_fmax:.2f} MHz)" if initial_fmax else ""
                lines.append(f"  {'Initial WNS:':<21s}{initial_wns:8.3f} ns{fmax_str}")
            if result_wns is not None:
                fmax_str = f"  (fmax: {result_fmax:.2f} MHz)" if result_fmax else ""
                lines.append(f"  {result_wns_label:<21s}{result_wns:8.3f} ns{fmax_str}")
            if initial_wns is not None and result_wns is not None:
                lines.append(f"  {'WNS Improvement:':<21s}{result_wns - initial_wns:+8.3f} ns")
        
        return lines
    
    
    def print_wns_change(
        self,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float]
    ):
        """Print Fmax/WNS change comparison with improvement/regression status."""
        if final_wns is None or initial_wns is None:
            return
        
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        final_fmax = self.calculate_fmax(final_wns, clock_period)
        
        if initial_fmax is not None and final_fmax is not None:
            fmax_improvement = final_fmax - initial_fmax
            pct = (fmax_improvement / initial_fmax) * 100 if initial_fmax else 0
            print(f"\n*** Fmax: {initial_fmax:.2f} -> {final_fmax:.2f} MHz ({fmax_improvement:+.2f} MHz, {pct:+.1f}%) ***")
            print(f"*** WNS:  {initial_wns:.3f} -> {final_wns:.3f} ns ***")
            if fmax_improvement > 0:
                print(f"IMPROVEMENT: Fmax improved by {fmax_improvement:.2f} MHz")
            elif fmax_improvement < 0:
                print(f"REGRESSION: Fmax got worse by {-fmax_improvement:.2f} MHz")
            else:
                print("NO CHANGE: Fmax is the same")
        else:
            wns_improvement = final_wns - initial_wns
            print(f"\n*** WNS: {initial_wns:.3f} -> {final_wns:.3f} ns ({wns_improvement:+.3f} ns) ***")
            if wns_improvement > 0:
                print(f"IMPROVEMENT: WNS improved by {wns_improvement:.3f} ns")
            elif wns_improvement < 0:
                print(f"REGRESSION: WNS got worse by {-wns_improvement:.3f} ns")
            else:
                print("NO CHANGE")
    
    def print_fmax_status(self, label: str, wns: Optional[float]):
        """Print Fmax (primary) and WNS (secondary) for a given measurement point."""
        if wns is None:
            print(f"*** {label}: WNS unknown ***")
            return
        fmax = self.calculate_fmax(wns, self.clock_period)
        clock_info = f" (clock: {self.target_clock})" if self.target_clock else ""
        if fmax is not None:
            print(f"*** {label} Fmax{clock_info}: {fmax:.2f} MHz (WNS: {wns:.3f} ns) ***")
        else:
            print(f"*** {label} WNS{clock_info}: {wns:.3f} ns ***")
    
    def print_test_summary(
        self,
        title: str,
        elapsed_seconds: float,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float],
        extra_info: str = ""
    ):
        """Print formatted test summary."""
        print("\n" + "="*70)
        print(title)
        print("="*70)
        print(f"Total runtime: {elapsed_seconds:.2f} seconds ({elapsed_seconds/60:.2f} minutes)")
        
        result_lines = self._format_fmax_results(clock_period, initial_wns, final_wns)
        if result_lines:
            print(f"\nFmax Results:")
            print("\n".join(result_lines))
        
        if extra_info:
            print(f"\n{extra_info}")
        print("="*70)


class DCPOptimizer(DCPOptimizerBase):
    """FPGA Design Optimization Agent using RapidWright and Vivado MCPs."""
    
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        debug: bool = False,
        run_dir: Optional[Path] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__(debug=debug, run_dir=run_dir)

        self.api_key = api_key
        self.model = model
        self.messages: list[dict] = []

        if base_url is not None:
            self.base_url = base_url
        else:
            self.base_url = DEFAULT_BASE_URL

        logger.info(f"Using API base URL: {self.base_url}")

        self.openai = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=LLM_CALL_TIMEOUT_S,   # bound each attempt (see LLM_CALL_TIMEOUT_S)
        )
        
        # Track optimization progress
        self.iteration = 0
        self.best_wns = float('-inf')
        self.no_improvement_count = 0
        self.llm_call_count = 0
        # Monotonic per-EXECUTED-plan counter for plan_NNN.tcl filenames. NOT the
        # iteration: the loop recurses on every script the model emits, so many plans
        # run within one iteration -- numbering by iteration overwrote all but the last.
        self._plan_file_seq = 0

        # Best-DCP auto-save (never lose a gain) + wall-clock budget.
        self._output_dcp_path = None        # final output DCP (set in optimize())
        self._best_snapshot = None          # protected best-DCP snapshot in run_dir
        self._best_saved_wns = None         # WNS of the last auto-saved best
        self.time_budget_seconds = 3600.0   # TOTAL cap incl. finalize; 60 min contest hour
        self._model_declared_done = False   # set when the run ended on the model's own "done"
        # Bumped by call_tool whenever a tool may have CHANGED the open design.
        # _ship_gate caches its verdict against this, so back-to-back promotions
        # on one unchanged design state cost one gate, not one per promotion.
        self._recipes_loaded = False
        self._design_gen = 0
        self._gate_gen = -1
        self._gate_verdict = None
        # Previous digest's measurements, so the NEXT digest can report the change
        # rather than making the model diff two turns of text itself. None means
        # "never measured" -- which is not the same as "unchanged" and must never
        # be rendered as a delta.
        self._prev_digest_wns = None
        self._prev_digest_prims = None
        # A plan attempt that was not accepted leaves the live design worse than
        # the snapshot. The revert is deferred to just before the NEXT attempt --
        # see _run_plan_script -- because open_checkpoint costs minutes on a large
        # design and output_dcp already holds the best verified one.
        self._plan_needs_revert = False
        # A plan attempt runs the reanalyze step and stashes its rendered digest here;
        # _maybe_refresh_state injects it after the tool result (ordering constraint).
        self._pending_digest = None
        # Anti-thrash: {(banked-WNS, normalized-script) -> measured WNS} of scripts
        # already run. Vivado is deterministic, so an identical mutation from the same
        # banked design is a guaranteed no-op -- short-circuit it instead of paying a
        # place+route to rediscover the same number.
        self._seen_scripts: dict = {}
        # Real per-lever wall cost (label -> execute_s) and the last plan's cost, so the
        # budget note can tell the model what a lever ACTUALLY costs on THIS design
        # instead of the LLM-latency figure it used to report.
        self._lever_costs: dict = {}
        self._last_plan_cost = None            # (label, execute_s, accepted)
        # Plateau detection: which lever CLASSES have been tried, and how many attempts
        # in a row banked nothing. Drives the "untried levers" panel that pushes the model
        # to PIVOT class instead of re-sweeping a plateaued one (see _plateau_panel).
        self._tried_classes: set = set()
        self._no_gain_streak = 0
        # Last design generation / turn whose state was fed back to the model.
        self._last_refresh_gen = 0
        self._last_refresh_turn = 0
        
        # Track token usage and costs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.api_call_details = []
        
        # Track all tool calls with timing and WNS
        self.tool_call_details = []
        
        # Track total runtime
        self.start_time = None
        self.end_time = None

        # When True, the initial-analysis summary dumps the COMPLETE Vivado reports
        # verbatim (no parsing, no derived classification) for the LLM to interpret.
        self.dump_raw_reports = False
    
    async def start_servers(self):
        """Start and connect to both MCP servers.

        The model is given NO tool list (script-first, no tools): it replies with a Tcl
        script, which the harness runs via call_tool below. The MCP tools exist only for
        the harness's own use, so there is nothing to collect for the model here."""
        await super().start_servers()
        logger.info("Connected to both MCP servers (harness-only; the model gets no tool list)")

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool call on the appropriate MCP server."""
        # Parse server prefix from tool name
        if tool_name.startswith("rapidwright_"):
            session = self.rapidwright_session
            actual_name = tool_name[len("rapidwright_"):]
        elif tool_name.startswith("vivado_"):
            session = self.vivado_session
            actual_name = tool_name[len("vivado_"):]
        else:
            return json.dumps({"error": f"Unknown tool prefix in: {tool_name}"})
        
        # The model is told to save its result to the output DCP, so it can put an
        # unverified -- or half-written -- design on the scored path at any moment.
        # Intercept in code rather than by asking the prompt not to: prompt text
        # drifts against the implementation, and a guard here cannot be forgotten.
        if actual_name == "write_checkpoint" and self._targets_output_dcp(arguments):
            return await self._staged_output_write(session, arguments)

        # SCRIPT-FIRST loop: a run_tcl that MUTATES the design is routed through the
        # validated plan-script path -- validate -> revert-if-needed -> wrap under
        # keep-best -> execute -> measure -> ship gate -> bank. This is the SINGLE
        # mutation surface: the model never calls this directly (it has no tools) --
        # process_response extracts the model's Tcl script and calls it here. A
        # pure-query run_tcl (report_*, get_*, puts ...) is NOT diverted -- it keeps the
        # fast raw path below.
        if actual_name == "run_tcl" and script_mutates(arguments.get("command", "")):
            attempt = await self._run_plan_script(
                arguments.get("command", ""), iteration=self.iteration)
            return self._format_plan_attempt(attempt)

        # Track timing for this tool call
        start_time = time.time()
        wns_measured = None

        try:
            result_text = await self._invoke_mcp(session, actual_name, arguments, tool_name)

            # Anything that may have CHANGED the design invalidates the cached
            # ship-gate verdict. Bumped AFTER the call (the design changed once it
            # ran) and BEFORE the WNS block below, which can promote and therefore
            # must not reuse a verdict describing the previous design.
            if actual_name in _DESIGN_MUTATING_TOOLS:
                self._design_gen += 1

            # Track WNS from timing reports and get_wns calls (sanity-checked; a new best
            # auto-saves the best DCP via _track_wns).
            if tool_name == "vivado_report_timing_summary":
                # If target clock is set, get clock-specific WNS instead of overall
                if self.target_clock:
                    try:
                        clock_wns = await super().get_wns_for_target_clock(self._call_vivado_tool)
                        if clock_wns is not None:
                            wns_measured = await self._track_wns(
                                clock_wns, f"(clock: {self.target_clock})",
                                tsum_text=result_text)
                    except Exception as e:
                        logger.warning(f"Failed to get clock-specific WNS, falling back to overall: {e}")
                        self.target_clock = None  # Fall through to overall WNS parsing

                if not self.target_clock or wns_measured is None:
                    timing_info = parse_timing_summary(result_text)
                    if timing_info.setup_wns is not None:
                        wns_measured = await self._track_wns(timing_info.setup_wns, "",
                                                             tsum_text=result_text)

            # Also track WNS from get_wns tool (returns just the numeric WNS value)
            elif tool_name == "vivado_get_wns":
                try:
                    wns_measured = await self._track_wns(float(result_text.strip()), "(from get_wns)")
                except (ValueError, AttributeError):
                    logger.warning(f"Could not parse WNS from get_wns output: {result_text[:100]}")

            # Track WNS from optimization tools that report it in their own output. These tools
            # leave the improved design in memory; parsing their reported WNS lets maya update
            # best_wns and save maya_best.dcp IMMEDIATELY (synchronously as the tool returns), so a
            # gain is captured even if the hard deadline cancels the loop before the LLM issues a
            # follow-up report_timing_summary. Without this, sweep/phys_opt gains were invisible to
            # best-tracking and thrown away on a deadline stop (observed: sweep reached -0.864 but
            # the finalized DCP stayed at the -1.686 baseline).
            elif tool_name in ("vivado_sweep_place_directives",
                               "vivado_sweep_phys_opt_directives",
                               "vivado_phys_opt_design"):
                w = self._parse_tool_wns(result_text)
                if w is not None:
                    wns_measured = await self._track_wns(w, f"(from {actual_name})")

            # ANY design change we have not already measured gets measured now.
            #
            # Promotion used to depend on WHICH TOOL the model happened to call:
            # only report_timing_summary, get_wns, the sweeps and phys_opt banked a
            # gain. place_design, route_design and anything through run_tcl mutate
            # the design and banked NOTHING. That was survivable only because the
            # prompt told the model to call get_wns after every lever -- and that
            # instruction is now gone, because the harness re-measures for it.
            #
            # Keying on "the design changed" instead of on a tool name also makes
            # ::opt:: recipes safe to expose: they all run through run_tcl, and a
            # tool-name-keyed rule can never see them.
            # 'run_tcl' is excluded here on purpose: a run_tcl that MUTATES the design was
            # already diverted to _run_plan_script (which measures + banks) and returned
            # above, so any run_tcl reaching this point is a PURE QUERY (report_*/get_*/puts)
            # that changed nothing. Measuring after it fired a redundant report_timing_summary
            # after EVERY query -- ~17 wasted timing reports in the initial-analysis phase alone.
            if wns_measured is None and actual_name in _DESIGN_MUTATING_TOOLS \
                    and actual_name not in ("open_checkpoint", "restart_vivado", "run_tcl"):
                measured = await self._raw_vivado("report_timing_summary", {})
                ts = parse_timing_summary(measured)
                if ts.setup_wns is not None:
                    wns_measured = await self._track_wns(
                        ts.setup_wns, f"(measured after {actual_name})", tsum_text=measured)


            elapsed_time = time.time() - start_time
            
            # Record tool call details
            self.tool_call_details.append({
                "tool_name": tool_name,
                "iteration": self.iteration,
                "elapsed_time": elapsed_time,
                "wns": wns_measured,
                "error": False
            })
            
            return result_text
            
        except Exception as e:
            elapsed_time = time.time() - start_time

            # Record failed tool call
            self.tool_call_details.append({
                "tool_name": tool_name,
                "iteration": self.iteration,
                "elapsed_time": elapsed_time,
                "wns": None,
                "error": True,
                "error_message": str(e)
            })

            logger.error(f"Tool call {tool_name} failed: {e}")
            self._write_tool_log(tool_name, arguments, f"ERROR: {e}")
            return json.dumps({"error": str(e)})

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        # Round ONCE, then split. Rounding minutes and seconds independently
        # prints "39m60s" for 2339.9.
        total = int(round(max(0.0, seconds)))
        m, s = divmod(total, 60)
        return f"{m}m{s:02d}s" if m else f"{s}s"

    @property
    def _opt_deadline_s(self) -> float:
        """When OPTIMIZATION must stop: the total cap minus the finalize reserve.

        Everything the model and the loop budget against is this deadline, not the total
        (time_budget_seconds) -- the reserve after it belongs to finalize + reporting.
        Floored so a tiny dev budget still leaves the loop some room instead of going
        non-positive."""
        return max(60.0, self.time_budget_seconds - FINALIZE_RESERVE_S)

    def _budget_note(self) -> str:
        """One line of wall-clock reality, appended to every tool result.

        The model is asked all through the prompt to budget its levers — "do not
        start one you cannot finish" — and was never given a clock. Measured
        consequence: all 12 benchmarks of the 2026-07-31 sweep ended with a heavy
        tool still in flight at the deadline, and ispd16 spent 23 min on a
        correct re-place, then started a route it had 11 min for. The re-place
        was right; it was unroutable in the time left, so the gate rejected it and
        the run banked nothing.

        Goes on EVERY tool result rather than into the state digest, because the
        digest is rate-limited on heavy designs — boom_soc and ispd16, the two
        that most needed a clock, received zero digests in that sweep.

        Also reports how long THIS call took, which is the only way the model can
        learn what a lever costs on THIS design; the prompt's figures are
        suite-wide averages.
        """
        if not self.start_time:
            return ""
        used = time.time() - self.start_time
        left = self._opt_deadline_s - used     # time left to ACT, not incl. the finalize reserve
        # Report the LAST SCRIPT's REAL execution time (place+route+measure) and whether
        # it banked -- NOT the LLM-call latency the old code read from tool_call_details,
        # which was ~1s and led the model to think a 10-20 min re-place was nearly free.
        last = ""
        last_cost = getattr(self, "_last_plan_cost", None)
        if last_cost is not None:
            lbl, secs, acc = last_cost
            last = (f"last script ({lbl}) took {self._fmt_dur(secs)} and "
                    f"{'BANKED a gain' if acc else 'did NOT improve'}; ")
        note = (f"\n\n[BUDGET] {last}{self._fmt_dur(used)} of "
                f"{self._opt_deadline_s/60:.0f}m used, ~{self._fmt_dur(left)} left.")
        # What levers have actually cost on THIS design, most expensive first -- so the
        # model budgets from measured cost, not the suite-wide averages in the prompt.
        lever_costs = getattr(self, "_lever_costs", None)
        if lever_costs:
            tbl = ", ".join(f"{k} {self._fmt_dur(v)}" for k, v in
                            sorted(lever_costs.items(), key=lambda kv: -kv[1])[:6])
            note += f"\n[LEVER COSTS on this design] {tbl}."
        if left <= 0:
            return note + " OVER BUDGET — the run stops now; do not start anything."
        if left < 300:
            return (note + " NOT ENOUGH TIME for a re-place, a route, or any sweep. "
                    "Measure and stop; anything you start now is discarded unfinished.")
        if left < 900:
            return (note + " Only a cheap incremental lever fits (phys_opt on a small design). "
                    "A place+route pair or a sweep will NOT finish — starting one wastes the rest "
                    "of the run.")
        return note + " Budget the pair, not the call: a re-place is worthless unless the route after it also fits."

    _LEVER_CLASSES = ("placement", "phys_opt", "routing",
                      "restructuring/retiming", "replication/uncombine")

    @staticmethod
    def _lever_class(label: str) -> str:
        """Map a plan label to its lever CLASS. Distinct place_design directives are all
        'placement' -- the point of the plateau panel is that a new directive is not a new
        class (see the prompt's LEVER CLASSES)."""
        l = (label or "").lower()
        if l.startswith("place"):
            return "placement"
        if "reroute" in l or "replicat" in l or "fanout" in l or "uncombine" in l:
            return "replication/uncombine"
        if "restruct" in l or "retim" in l or "lut" in l:
            return "restructuring/retiming"
        if "phys_opt" in l or "safe_phys" in l:
            return "phys_opt"
        if "route" in l:
            return "routing"
        return "other"

    def _plateau_panel(self) -> str:
        """When several attempts in a row banked nothing, tell the model which lever
        CLASSES it has spent and which remain -- so it PIVOTS class instead of re-sweeping
        a plateaued one. Empty until the streak crosses the threshold."""
        streak = getattr(self, "_no_gain_streak", 0)
        if streak < 2:
            return ""
        if _kb_variant() != "hand":                   # [pramana] no hand-written lever advice
            return f"\n\n[PLATEAU] {streak} attempts in a row banked no gain."
        tried = {c for c in getattr(self, "_tried_classes", set()) if c != "other"}
        untried = [c for c in self._LEVER_CLASSES if c not in tried]
        panel = (f"\n\n[PLATEAU] {streak} attempts in a row banked no gain. "
                 f"Classes tried: {', '.join(sorted(tried)) or 'none'}.")
        if untried:
            panel += (f" UNTRIED classes to PIVOT to: {', '.join(untried)}. A plateau in one "
                      "class does NOT mean the design is done — re-read the diagnosis's => notes "
                      "for the flagged lever (uncombine on high LUT-combining, retiming on a carry "
                      "path, replication on a broadcast net), and if FULL re-place has plateaued "
                      "try a SELECTIVE cone re-place (replace_critical_cone in the appended "
                      "reference). Try a class you have not used.")
        else:
            panel += " All lever classes have been tried without further gain — you may be at a real floor."
        return panel

    async def _ensure_recipes_loaded(self) -> bool:
        """Source the ::opt:: library into the live session if its procs are absent.

        Idempotent and cheap, so it can be called before every planner turn: if Vivado
        crashed and was relaunched, the fresh interpreter has lost the sourced procs and
        this puts them back. Goes through _raw_vivado — this is a harness action and must
        not perturb WNS tracking or the gate cache.

        revert_dir is set explicitly because the library defaults it to "." and
        try_keep_if_better drops temp .dcp files there — which would be the repo root.
        """
        if not RECIPE_LIB.exists():
            if self._recipes_loaded:
                logger.warning(f"::opt:: library vanished from {RECIPE_LIB}")
            self._recipes_loaded = False
            return False
        # Re-source only when ::opt::diagnose is missing (first load, or post-restart).
        src = (f"if {{[llength [info commands ::opt::diagnose]]}} {{puts OPT_LIB_PRESENT}} else {{"
               f"if {{[catch {{source {{{RECIPE_LIB}}}; "
               f"set ::opt::revert_dir {{{self.run_dir}}}}} _e]}} {{puts \"OPT_LIB_ERROR: $_e\"}} "
               f"else {{puts OPT_LIB_OK}}}}")
        res = await self._raw_vivado("run_tcl", {"command": src, "timeout": 120})
        if "OPT_LIB_ERROR" in res:
            logger.warning(f"Could not source the ::opt:: library: {res[:300]}")
            self._recipes_loaded = False
            return False
        if "OPT_LIB_OK" in res:
            logger.info(f"Sourced the ::opt:: recipe library from {RECIPE_LIB.name}")
        elif "OPT_LIB_PRESENT" in res and not self._recipes_loaded:
            logger.info("::opt:: recipe library already present in the session")
        elif "OPT_LIB_PRESENT" not in res:
            logger.warning(f"Unexpected reply while sourcing ::opt::: {res[:200]}")
            self._recipes_loaded = False
            return False
        if _kb_variant() == "pramana":                # [pramana] also source <kb-dir>/recipes.tcl
            lib = (_kb_dir() or Path("/nonexistent")) / "recipes.tcl"
            if not lib.exists():
                logger.warning(f"pramana recipes.tcl not found at {lib}")
                self._recipes_loaded = False
                return False
            src = (f"if {{[llength [info commands ::pramana::keep_if_better]]}} "
                   f"{{puts PRAMANA_LIB_PRESENT}} else {{"
                   f"if {{[catch {{source {{{lib}}}}} _e]}} {{puts \"PRAMANA_LIB_ERROR: $_e\"}} "
                   f"else {{puts PRAMANA_LIB_OK}}}}")
            if self.start_time and getattr(self, "_opt_deadline_s", None):
                src += f"; set ::pramana::deadline {int(self.start_time + self._opt_deadline_s)}"
            res = await self._raw_vivado("run_tcl", {"command": src, "timeout": 120})
            if "PRAMANA_LIB_OK" not in res and "PRAMANA_LIB_PRESENT" not in res:
                logger.warning(f"Could not source pramana recipes.tcl: {res[:300]}")
                self._recipes_loaded = False
                return False
        self._recipes_loaded = True
        return True

    async def _open_checkpoint(self, dcp_path) -> str:
        """Open a checkpoint AND (re)source the ::opt:: recipes, as ONE step.

        The ::opt:: procs live in the Tcl interpreter, so they survive a plain reopen in
        the SAME Vivado process -- but a RESTART brings up a fresh interpreter that has
        lost them. Binding the source to every open makes "a checkpoint is open" imply
        "the recipes are present", so no open path can silently leave the model without
        them. _ensure_recipes_loaded probes (info commands ::opt::diagnose) and no-ops
        when they are already there, so the extra call is ~free unless a restart really
        dropped them. Returns the raw open_checkpoint output for the caller's error check.

        This is the RAW open path (harness action, no WNS/gate bookkeeping). The initial
        MCP open in _open_design sources separately because it interleaves RapidWright
        loading; every OTHER open goes through here.
        """
        out = await self._raw_vivado(
            "open_checkpoint", {"dcp_path": str(Path(dcp_path).resolve())})
        await self._ensure_recipes_loaded()
        return out

    def _targets_output_dcp(self, arguments: dict) -> bool:
        """Does this write_checkpoint aim at the scored output path?

        Compares resolved paths, so a relative path, a symlink or a differently
        spelled but equivalent path is still caught. Anything unparseable is
        treated as NOT the output -- being wrong in that direction merely skips
        the staging, and the promotion gate still governs what gets shipped.
        """
        if self._output_dcp_path is None:
            return False
        raw = arguments.get("dcp_path")
        if not raw:
            return False
        try:
            return Path(raw).resolve() == Path(self._output_dcp_path).resolve()
        except Exception:
            return False

    async def _staged_output_write(self, session, arguments: dict) -> str:
        """Handle the model writing the scored DCP: stage it, gate it, and only
        then swap it in.

        The model's write says nothing about routing or hold, and Vivado takes
        tens of seconds on a large DCP, so writing the scored path directly is
        both unverified and a truncation window. We write to run_dir/llm_stage.dcp
        instead, run the ship gate, and atomically promote only on a pass.

        On a reject the model is told the truth, naming the failing metric: it is
        a useful correction signal, and a false "saved!" would have it move on
        believing the result is banked.
        """
        staged = Path(self.run_dir) / "llm_stage.dcp"
        args = dict(arguments)
        args["dcp_path"] = str(staged.resolve())
        try:
            out = await self._invoke_mcp(session, "write_checkpoint", args,
                                         "write_checkpoint_staged")
        except Exception as e:
            logger.error(f"Staged write_checkpoint failed: {e}")
            return json.dumps({"error": f"write_checkpoint failed: {e}"})

        if not staged.exists() or staged.stat().st_size == 0:
            logger.warning(f"Staged write produced no/empty file: {out[:200]}")
            return json.dumps({"error": "write_checkpoint produced no output file"})

        # The design just written IS the design currently open, so the cached
        # verdict for this generation applies.
        ok, reason = await self._ship_gate()
        if not ok:
            logger.warning(f"Ship gate REJECTED the model's write to the output DCP: {reason}")
            return (f"Checkpoint written to {staged}, but it was NOT promoted to the "
                    f"output DCP: {reason}. The output DCP still holds the last "
                    f"verified design. The contest rejects any design that is not "
                    f"fully routed with WHS >= 0 and WPWS >= 0, so fix that before "
                    f"saving again.")

        if not atomic_copy(staged, self._output_dcp_path):
            return json.dumps({"error": "could not promote the staged checkpoint"})
        logger.info(f"Promoted the model's checkpoint to {self._output_dcp_path} ({reason})")
        return f"Checkpoint saved to {self._output_dcp_path} (verified: {reason})."

    async def _invoke_mcp(self, session, actual_name: str, arguments: dict,
                          log_as: str) -> str:
        """TRANSPORT ONLY: call one MCP tool and return its text.

        Deliberately knows nothing about WNS, best-checkpoints or
        tool_call_details. Splitting this out of call_tool is what makes a
        side-effect-free measurement possible -- see _raw_vivado. Raises on
        transport failure; the caller decides what that means.
        """
        logger.info(f"Calling {log_as} with args: {json.dumps(arguments)[:200]}...")
        result = await session.call_tool(actual_name, arguments)
        if result.content:
            text_parts = [c.text for c in result.content if hasattr(c, 'text')]
            result_text = "\n".join(text_parts)
        else:
            result_text = "(no output)"
        logger.debug(f"Tool {log_as} output:\n{result_text}")
        self._write_tool_log(log_as, arguments, result_text)
        return result_text

    async def _raw_vivado(self, tool_name: str, arguments: dict) -> str:
        """Call a Vivado tool with NO side effects: no WNS tracking, no
        best-checkpoint promotion, no tool_call_details entry.

        call_tool derives a WNS from report_timing_summary / get_wns / the
        sweep+phys_opt tools and hands it to _track_wns, which can advance
        best_wns AND write the best snapshot. A ship gate that measured through
        call_tool would therefore re-enter the very promotion it is gating. This
        is the path the gate measures on.

        Returns "" on failure rather than raising: an unreadable report must read
        as "could not confirm", which the strict gate treats as do-not-ship. The
        log prefix keeps gate measurements separable in tool_outputs/.
        """
        try:
            return await self._invoke_mcp(
                self.vivado_session, tool_name, arguments, f"raw_{tool_name}") or ""
        except Exception as e:
            logger.warning(f"Raw Vivado call {tool_name} failed: {e}")
            return ""

    async def _call_vivado_tool(self, tool_name: str, arguments: dict) -> str:
        """Helper to call Vivado tools (for use with base class methods)."""
        return await self.call_tool(f"vivado_{tool_name}", arguments)

    @staticmethod
    def _parse_tool_wns(text: str) -> Optional[float]:
        """Pull the achieved worst-slack (ns) out of an optimization tool's OWN output:
          - sweep_* tools print  'WINNER: <directive>  WNS=<x>  (gain ...)'
          - phys_opt_design prints 'Post Physical Optimization Timing Summary | WNS=<x> | ...'
        Returns the WNS or None. Deliberately matches only these authoritative lines so it
        never picks up a 'WNS Gain' column header or a pre-optimization value."""
        m = re.search(r"WINNER:.*?WNS=\s*(-?\d+(?:\.\d+)?)", text)
        if m:
            return float(m.group(1))
        # phys_opt prints one summary per invocation; take the LAST (post-optimization) one.
        ms = re.findall(r"Post Physical Optimization Timing Summary\s*\|\s*WNS=\s*(-?\d+(?:\.\d+)?)",
                        text)
        if ms:
            return float(ms[-1])
        return None

    def _plausible_wns(self, wns) -> bool:
        """Reject implausible WNS values (parsing artifacts). Slack can never exceed the
        clock period; a value above it — or absurdly large — is a stray number scraped as
        WNS (e.g. a 'file size ... 1471' console line). This guards the best-WNS tracking."""
        if wns is None:
            return False
        try:
            wns = float(wns)
        except (TypeError, ValueError):
            return False
        if self.clock_period and wns > self.clock_period + 1e-3:
            return False
        if abs(wns) > 1000.0:          # no real FPGA path has |slack| > 1 us
            return False
        return True

    async def _ship_gate(self, tsum_text: Optional[str] = None,
                         route_ok: Optional[bool] = None) -> tuple:
        """Is the CURRENTLY OPEN design safe to ship? Returns (ok, reason).

        FAIL CLOSED. The contest requires the output DCP to be fully routed with
        zero routing errors, WHS >= 0 and WPWS >= 0; any failure scores the
        benchmark ZERO, and the untouched input is valid by definition. So
        anything we cannot positively confirm is a reject.

        This runs at PROMOTION time, not at finalize, because the deadline
        backstop is a synchronous daemon thread that ends in os._exit(1) -- it
        can copy a file but can never await a verification. The only way it can
        be correct is if whatever sits in the snapshot is ALREADY shippable.

        Cost control: the verdict is cached against _design_gen, so repeated
        promotions on one unchanged design pay for one gate. When the caller
        already holds a fresh report_timing_summary (the commonest promotion
        path) it passes the text in and the hold/PW half costs no Vivado call at
        all -- only report_route_status is unavoidable.
        """
        if self._gate_gen == self._design_gen and self._gate_verdict is not None:
            return self._gate_verdict

        # route_ok lets the caller supply the route verdict it already measured (the
        # reanalyze step), so the gate does not re-read report_route_status. None means
        # "not supplied" -> read it; an unreadable route still parses to None and rejects.
        routed = route_ok if route_ok is not None else \
            parse_route_status(await self._raw_vivado("report_route_status", {}))
        if routed is not True:
            why = ("route status could not be read" if routed is None
                   else "the design is not fully routed")
            verdict = (False, why)
        else:
            text = tsum_text or await self._raw_vivado("report_timing_summary", {})
            ts = parse_timing_summary(text)
            if hold_pw_ok(ts.hold_whs, ts.pulse_width_wns, strict=True):
                verdict = (True, f"routed, WHS={ts.hold_whs}, WPWS={ts.pulse_width_wns}")
            else:
                verdict = (False, f"hold/pulse-width not confirmed >= 0 "
                                  f"(WHS={ts.hold_whs}, WPWS={ts.pulse_width_wns})")

        self._gate_gen, self._gate_verdict = self._design_gen, verdict
        logger.info(f"Ship gate [gen {self._design_gen}]: "
                    f"{'PASS' if verdict[0] else 'REJECT'} — {verdict[1]}")
        return verdict

    async def _save_best_checkpoint(self, wns: float,
                                    tsum_text: Optional[str] = None) -> bool:
        """Persist the current design as the best-so-far, IF it is shippable.
        Returns True only when the snapshot now reflects `wns`.

        The gate sits here because this is the sole writer of _best_snapshot and
        the only place a promotion actually becomes persistent -- so gating here
        both keeps the snapshot always-shippable and costs nothing on
        measurements that were never going to be saved.
        """
        if self._best_snapshot is None:
            return False
        if self._best_saved_wns is not None and abs(self._best_saved_wns - wns) < 1e-9:
            return True  # already saved this exact best (avoid duplicate writes)

        ok, reason = await self._ship_gate(tsum_text)
        if not ok:
            logger.warning(f"Ship gate REJECTED a WNS {wns:.3f} ns improvement: {reason}. "
                           f"Snapshot and output DCP left untouched.")
            return False

        try:
            # Write to a temp path then atomically rename, so the deadline watchdog (which
            # copies this snapshot from another thread) can never observe a half-written DCP.
            # _raw_vivado, not _call_vivado_tool: this is a harness action, and routing it
            # through call_tool would add a spurious tool_call_details entry.
            tmp = str(self._best_snapshot) + ".tmp"
            await self._raw_vivado("write_checkpoint", {"dcp_path": tmp, "force": True})
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                logger.warning("Auto-save produced no/empty checkpoint; keeping the previous best")
                return False
            os.replace(tmp, self._best_snapshot)
            self._best_saved_wns = wns
            logger.info(f"Auto-saved best DCP (WNS {wns:.3f} ns) -> {self._best_snapshot}")
        except Exception as e:
            logger.warning(f"Auto-save of best DCP failed: {e}")
            return False

        # Refresh the scored output from the gated snapshot. This is what lets
        # the deadline backstop stop copying anything: output_dcp is already the
        # best VERIFIED design at all times.
        if self._output_dcp_path is not None:
            atomic_copy(self._best_snapshot, self._output_dcp_path)
        return True

    async def _track_wns(self, current_wns, label: str = "",
                         tsum_text: Optional[str] = None):
        """Sanity-check a measured WNS, update best (auto-saving the best DCP on a gain),
        and return the value to record (or None if implausible).

        best_wns is ROLLED BACK if the improvement could not be persisted. It
        used to advance regardless, so a design the gate rejects -- or a failed
        snapshot write -- left maya advertising a best it could not ship, and
        the run summary reported a gain that never reached the output DCP.
        """
        if not self._plausible_wns(current_wns):
            if current_wns is not None:
                logger.warning(f"Ignoring implausible WNS {current_wns} {label} "
                               f"(clock period {self.clock_period}) — likely a parsing artifact")
            return None
        current_wns = float(current_wns)
        fmax = self.calculate_fmax(current_wns, self.clock_period)
        fmax_str = f", fmax: {fmax:.2f} MHz" if fmax is not None else ""
        if current_wns > self.best_wns:
            logger.info(f"New best WNS {label}: {current_wns:.3f} ns{fmax_str} "
                        f"(improved from {self.best_wns:.3f} ns)")
            previous_best = self.best_wns
            self.best_wns = current_wns
            if not await self._save_best_checkpoint(current_wns, tsum_text):
                self.best_wns = previous_best
                logger.info(f"best_wns rolled back to {previous_best}: the WNS "
                            f"{current_wns:.3f} ns design was not shippable")
        else:
            logger.info(f"Current WNS {label}: {current_wns:.3f} ns{fmax_str} "
                        f"(best is still {self.best_wns:.3f} ns)")
        return current_wns

    # ------------------------------------------------------------------
    # Plan-script execution (C13 Step 1)
    #
    # NOT referenced by the contest path. The tool-calling loop is untouched;
    # this is reached only from --plan-script until C13 Step 2 switches the loop
    # over in one commit that also DELETES the tool-calling turn handler.
    # ------------------------------------------------------------------
    async def _recover_vivado_session(self) -> None:
        """Recover after Vivado GENUINELY DIED mid-script (the process crashed / EOF,
        e.g. an OOM or a segfaulting phys_opt on a giant). Restart the process, reopen
        the best verified design, and re-source the ::opt:: library so the next script's
        recipe calls resolve. Never raises.

        Gated on a REAL death signal only -- NOT on a plain timeout. A timed-out script
        produces no completion sentinel but the session is BUSY (still running the
        place/route/phys_opt); restarting THAT kills in-progress work and is what cost
        ispd16 its whole run. See the caller: it fires this only when the server reports
        'terminated unexpectedly'."""
        logger.warning("Vivado process died mid-script — restarting and reopening the best design")
        try:
            await self._raw_vivado("restart_vivado", {})
        except Exception as e:
            logger.warning(f"restart_vivado failed during recovery: {e}")
            return
        self._recipes_loaded = False      # a fresh session has not sourced ::opt::
        self._design_gen += 1
        target = (self._best_snapshot
                  if self._best_snapshot is not None and Path(self._best_snapshot).exists()
                  else getattr(self, "_input_dcp_path", None))
        if target is not None and Path(target).exists():
            try:
                await self._open_checkpoint(target)   # opens + re-sources ::opt:: as one step
            except Exception as e:
                logger.warning(f"reopen + recipe re-source after restart failed: {e}")
        # The live session now holds the best verified design again, recipes present.
        self._plan_needs_revert = False

    async def _revert_to_best_if_needed(self) -> None:
        """Undo a regression a prior non-accepted plan attempt left in the LIVE
        session, BEFORE the next script runs on top of it. Reverts by re-opening the
        best VERIFIED design -- the banked snapshot if one exists, else the untouched
        input (contest-valid by definition, and what best_wns was seeded from).

        Deferred to here (not done when the attempt was rejected) because
        open_checkpoint costs minutes on a large design and output_dcp already holds
        the best verified design regardless -- so a regression sitting in the live
        session is free as long as it is undone before the next attempt builds on it.
        This consumes the `_plan_needs_revert` flag that _run_plan_script sets.

        Best-effort: a failed revert leaves the flag set so a later attempt retries,
        and never aborts the run. The ship path is safe either way; not reverting only
        costs the next attempt a worse starting point.
        """
        if not self._plan_needs_revert:
            return
        target = None
        if self._best_snapshot is not None and Path(self._best_snapshot).exists():
            target = self._best_snapshot
        elif getattr(self, "_input_dcp_path", None) is not None and Path(self._input_dcp_path).exists():
            target = self._input_dcp_path
        if target is None:
            logger.warning("Plan revert requested but no best snapshot or input DCP to revert to")
            self._plan_needs_revert = False
            return
        try:
            out = await self._open_checkpoint(target)   # opens + re-sources ::opt::
        except Exception as e:
            logger.warning(f"Plan revert open_checkpoint raised: {e} -- will retry before next attempt")
            return
        if not out or "error" in out.lower():
            logger.warning(f"Plan revert open_checkpoint failed: {out[:160]} -- will retry before next attempt")
            return
        # Re-opening replaced the live design; _raw_vivado does not bump the
        # generation (it is not the model's call), so invalidate the gate cache
        # explicitly -- the same reason _verify_snapshot_file bumps it after opening.
        self._design_gen += 1
        self._plan_needs_revert = False
        logger.info(f"Reverted live design to best verified checkpoint before next plan "
                    f"attempt: {Path(target).name}")

    def _format_plan_attempt(self, a: PlanAttempt) -> str:
        """Render a PlanAttempt as the tool result the model reads. Tells it the whole
        truth -- fatal problems (fix and resubmit), the measured state, the ship-gate
        verdict, and whether the gain was banked -- so the next script is grounded in
        what actually happened, not in what the model hoped the script would do."""
        if a.fatal_problems:
            return ("PLAN SCRIPT NOT EXECUTED — fatal problems; fix and resubmit:\n"
                    + "\n".join(f"  - {p}" for p in a.fatal_problems))
        if a.duplicate:
            return (f"PLAN SCRIPT NOT RE-RUN (label: {a.label}) — this is IDENTICAL to a "
                    f"script you already ran from the same banked design. Vivado is "
                    f"deterministic here, so it produces the same WNS {a.wns} ns as before "
                    f"— no gain, and no place+route was spent. Do NOT resubmit variants of "
                    f"the same lever; change the CLASS of move: placement directive → "
                    f"phys_opt → route → retiming/restructuring/uncombine.")
        lines = [f"PLAN SCRIPT {'ran' if a.ok else 'FAILED'} (label: {a.label})"]
        if a.error:
            lines.append(f"  Tcl error: {a.error}")
        for p in a.advice_problems:
            lines.append(f"  advice (executed anyway): {p}")
        lines.append(f"  Measured: WNS {a.wns} ns, WHS {a.whs}, WPWS {a.wpws}, "
                     f"routed={a.routed}, route_errors={a.route_errors}")
        lines.append(f"  Ship gate: {'PASS' if a.gate_ok else 'REJECT'} ({a.gate_reason})")
        if a.accepted:
            lines.append("  Result: ACCEPTED — banked as the new best design.")
        else:
            lines.append("  Result: NOT accepted — best is unchanged; the live design "
                         "will be reverted to the best verified checkpoint before your "
                         "next script runs.")
        return "\n".join(lines)

    async def _run_plan_script(self, script: str, label: str = "",
                               iteration: Optional[int] = None) -> PlanAttempt:
        """Validate, execute and measure ONE planner script. Returns the record.

        The order here is the whole design:
          validate -> (fatal? stop) -> write file -> source under catch ->
          BUMP _design_gen -> measure -> ship gate -> accept or leave for revert

        Two things that look like details and are not:

        1. `self._design_gen += 1` is REQUIRED and is the highest-risk line in
           this method. _ship_gate caches its verdict against _design_gen, and
           only call_tool bumps it. We measure through _raw_vivado on purpose --
           routing through call_tool would re-enter the WNS promotion the gate
           exists to govern -- so nothing else will invalidate that cache. Omit
           the bump and the gate can serve a PASS describing the PRE-script
           design, promoting something that was never verified. maya.py's
           reopen path documents the same hazard for the same reason.

        2. The revert is LAZY. Reverting means open_checkpoint on the best
           snapshot, which is minutes on a large design; doing it here would
           spend that time even when the deadline is about to stop the run.
           output_dcp already holds the best VERIFIED design at all times, so a
           regression left in the live session costs nothing as long as the next
           attempt reverts first. `needs_revert` is what records that.
        """
        t0 = time.time()
        it = self.iteration if iteration is None else iteration
        label = label or label_script(script)

        problems = validate_script(script)
        fatal = fatal_problems(problems)
        advice = advice_problems(problems)
        attempt = PlanAttempt(
            iteration=it, label=label, script_path="", ok=False, error=None,
            fatal_problems=[p.render() for p in fatal],
            advice_problems=[p.render() for p in advice],
        )
        if fatal:
            # Not executed, and the planner is told exactly why. The harness never
            # edits the script -- rewriting it would hide the mistake from the
            # model, which then repeats it next turn.
            attempt.error = "not executed: " + "; ".join(attempt.fatal_problems)
            # NOT "REJECT". The gate never ran, and recording a rejection it did
            # not make would misreport why the attempt failed.
            attempt.gate_reason = "not evaluated: the script was not executed"
            attempt.elapsed_s = attempt.execute_s = time.time() - t0
            logger.warning(f"Plan script REJECTED before execution: {attempt.error}")
            self._record_plan_attempt(attempt)
            return attempt

        plans_dir = self.run_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        # Number by the per-plan sequence, NOT the iteration: several plans can execute in
        # one iteration (the loop recurses per emitted script), and keying the filename on
        # the iteration overwrote all but the last -- e.g. 6 executed plans left 1 .tcl.
        self._plan_file_seq += 1
        script_path = plans_dir / f"plan_{self._plan_file_seq:03d}.tcl"
        script_path.write_text(script if script.endswith("\n") else script + "\n")
        attempt.script_path = str(script_path)
        if advice:
            for p in advice:
                logger.info(f"Plan script advice (executed anyway): {p.render()}")

        # ANTI-THRASH: every script runs from the same banked design (the revert below),
        # and Vivado is deterministic, so an identical mutation is a guaranteed no-op.
        # Short-circuit it -- do NOT spend a place+route to rediscover the same WNS, and
        # tell the model to change CLASS of move instead. Keyed on the banked WNS at run
        # start (captured before execution can advance it) so a genuinely new base state
        # is allowed to re-run the same script.
        best_key = (round(self._best_saved_wns, 4)
                    if self._best_saved_wns is not None else None)
        dkey = (best_key, normalize_script(script))
        if dkey in self._seen_scripts:
            attempt.ok = True
            attempt.duplicate = True
            attempt.wns = self._seen_scripts[dkey]
            attempt.gate_reason = "not re-run (identical to an earlier attempt)"
            attempt.execute_s = attempt.elapsed_s = time.time() - t0
            logger.info("Plan script SKIPPED: identical to an earlier attempt from the "
                        f"same banked design; deterministic no-op (WNS {attempt.wns}).")
            self._note_plan_class(label, accepted=False)
            self._record_plan_attempt(attempt)
            return attempt

        # A prior attempt may have left the live session worse than the best verified
        # design; undo it before running this one on top (see _revert_to_best_if_needed).
        # Done only now that we know THIS script is executable -- a fatal, un-executed
        # script must leave the pending revert for the next real attempt.
        await self._revert_to_best_if_needed()

        # NO per-command timeout (0): let the place/route/phys_opt run to completion so it
        # produces its sentinel and is banked, instead of being abandoned mid-run. The hard
        # cap is the deadline backstop, which ships the always-shippable best snapshot.
        logger.info(f"Executing plan script {script_path.name} "
                    f"({len(script)} B, no timeout, label {label})")
        out = await self._raw_vivado(
            "run_tcl", {"command": wrap_for_source(script_path), "timeout": 0})
        attempt.execute_s = time.time() - t0
        # Real per-lever cost, for the budget note (was reporting LLM latency instead).
        self._lever_costs[label] = attempt.execute_s

        # The design may have changed even on failure (an abort discards only the
        # LATER stages; earlier ones already ran), so invalidate unconditionally.
        self._design_gen += 1

        attempt.ok, attempt.error = parse_script_result(out)
        if not attempt.ok:
            logger.warning(f"Plan script FAILED: {attempt.error}")
            # Two failure modes look identical to parse_script_result (both "no sentinel"),
            # but need OPPOSITE handling, so distinguish them from the RAW output:
            #   - GENUINE DEATH: the server says "terminated unexpectedly" (the Vivado
            #     process crashed / EOF -- an OOM or a segfaulting phys_opt on a giant).
            #     Restart + reopen the best design before the next iteration, else every
            #     later script fires into a fresh EMPTY session.
            #   - TIMEOUT: no such string; the session is BUSY (still running). Do NOT
            #     restart -- the measurement below goes through sync_after_timeout, which
            #     WAITS for the command to finish and banks its result. Restarting here is
            #     what killed in-progress work and cost ispd16 its whole run.
            if out and ("terminated unexpectedly" in out or "process terminated" in out):
                await self._recover_vivado_session()

            # DO NOT RE-MEASURE A FAILED ATTEMPT. It is discarded and the next script
            # runs from the banked best, so the full refresh bundle below (6 Vivado
            # calls, ~29 s on ispd16, ~1.1 s on the small designs) would measure a state
            # that is thrown away before it is ever used as a base. Observed 8 times in
            # the 12-run corpus -- e.g. amd_mini-isp plan_001 died in 84 ms on a Tcl
            # option typo and was followed by the whole bundle.
            #
            # The revert is UNCHANGED: still deferred, still via _plan_needs_revert.
            # See render_failed_attempt for the accepted trade-off (a partial gain from
            # a script that failed midway is discarded unmeasured) -- that is deliberate.
            self._plan_needs_revert = True
            self._pending_digest = render_failed_attempt(
                attempt.error, self._best_saved_wns)
            attempt.gate_reason = "not measured — the attempt failed and was discarded"
            attempt.elapsed_s = time.time() - t0
            # attempt.wns stays None, so _seen_scripts does NOT memoize this script: a
            # failed run may succeed on retry and must not be short-circuited as a
            # deterministic no-op.
            self._last_plan_cost = (label, attempt.execute_s, attempt.accepted)
            self._note_plan_class(label, attempt.accepted, measured=False)  # failed: not plateau
            self._record_plan_attempt(attempt)
            logger.info("Skipped the post-attempt re-measure: the attempt failed, so the "
                        "live design is discarded in favour of the banked best")
            return attempt

        # REANALYZE (refresh-style): the SINGLE per-turn measurement -- timing, route,
        # primitives, and the design-analysis/congestion/util bundle -- rendered as the
        # state digest AND returned as parsed verdicts. Runs BEFORE the gate, which then
        # reuses these numbers instead of re-reading. The digest is stashed and injected
        # after the tool result by _maybe_refresh_state (a tool result must immediately
        # follow the assistant tool-call, so it cannot be injected here). Enabled for
        # large designs too for now; a lite/full split by design size comes later.
        digest, m, new_wns, new_prims = await build_state_digest(
            lambda cmd: self._raw_vivado("run_tcl", {"command": cmd}),
            clock_period=self.clock_period,
            prev_wns=getattr(self, "_prev_digest_wns", None),
            prev_prims=getattr(self, "_prev_digest_prims", None),
            best_saved_wns=self._best_saved_wns)
        self._prev_digest_wns, self._prev_digest_prims = new_wns, new_prims
        self._pending_digest = digest
        if m is not None:
            attempt.wns, attempt.tns = m.tsum.setup_wns, m.tsum.setup_tns
            attempt.whs, attempt.wpws = m.tsum.hold_whs, m.tsum.pulse_width_wns
            attempt.routed = m.routed
            attempt.route_errors = m.route_bad
            attempt.gate_ok, attempt.gate_reason = await self._ship_gate(
                m.tsum_text, route_ok=m.routed)
            if attempt.gate_ok and attempt.wns is not None:
                before = self.best_wns
                await self._track_wns(attempt.wns, f"(plan {label})", m.tsum_text)
                attempt.accepted = self.best_wns > before
        if not attempt.accepted:
            # See the docstring: reverted immediately before the NEXT attempt.
            self._plan_needs_revert = True

        attempt.elapsed_s = time.time() - t0
        # Remember the outcome of THIS exact script (from the base it ran on) so an
        # identical resubmission is short-circuited above, and record its cost for the
        # budget note.
        # Only remember scripts that actually measured — a failed/aborted run may succeed
        # on retry, so it must not be short-circuited as a deterministic no-op.
        if attempt.wns is not None:
            self._seen_scripts[dkey] = attempt.wns
        self._last_plan_cost = (label, attempt.execute_s, attempt.accepted)
        self._note_plan_class(label, attempt.accepted)
        self._record_plan_attempt(attempt)
        return attempt

    def _note_plan_class(self, label: str, accepted: bool, measured: bool = True) -> None:
        """Record the lever class tried and update the no-gain streak (drives _plateau_panel).

        `measured` is False for an attempt that did NOT run to a real result — a Tcl
        syntax error, a rejected directive, or Vivado dying mid-script. Those are
        FAILURES, not plateau evidence: the lever never actually ran, so it neither
        counts as "this class was tried" nor as a no-gain step. Counting them would
        surface the [PLATEAU] panel (and push the model toward 'real floor' / done) off
        the back of broken scripts rather than genuine diminishing returns."""
        if not measured:
            return
        cls = self._lever_class(label)
        if cls != "other":
            self._tried_classes.add(cls)
        self._no_gain_streak = 0 if accepted else self._no_gain_streak + 1

    def _record_plan_attempt(self, attempt: PlanAttempt) -> None:
        """Append one attempt to run_dir/attempts.jsonl.

        Bookkeeping must never kill a run, so this swallows its own errors -- but
        it logs them, because a silently missing attempts.jsonl is what would make
        the analyze-cadence question unanswerable later.
        """
        try:
            with open(self.run_dir / "attempts.jsonl", "a") as f:
                f.write(json.dumps(attempt.to_json()) + "\n")
        except Exception as e:
            logger.warning(f"Could not append to attempts.jsonl: {e}")

    async def _finalize_output(self, output_dcp: Path, verify: bool = True) -> None:
        """Settle the scored DCP: ship the best candidate, never worse than the input.

        After the promotion gate this is a SAFETY NET rather than the mechanism.
        output_dcp is seeded from the input at t=0 and refreshed from the gated
        snapshot on every accepted promotion, so by the time we get here it
        already holds the best verified design. What is left for finalize is the
        one thing the gate cannot see: whether the snapshot FILE on disk is
        actually openable. The gate verified an in-memory design and checked that
        write_checkpoint produced a non-empty file; it did not prove the file
        parses. A truncated or corrupt DCP scores zero exactly like a
        hold-violating one.

        `verify=False` is for the deadline path: re-opening a large checkpoint
        can take minutes and the backstop fires 90 s after the limit, so there we
        keep what is already on disk rather than risk being killed mid-verify.
        """
        if self._best_snapshot is None or not Path(self._best_snapshot).exists():
            # No snapshot means no improvement was ever banked. output_dcp still
            # holds the input, seeded in optimize(), so there is nothing to do
            # and nothing is lost.
            logger.info(f"Finalize: no best snapshot; {output_dcp} retains the input DCP")
            return

        # Never ship worse than the input. best_wns is seeded from initial_wns and
        # only ever advances through the gate, so this should be unreachable --
        # it is kept because "should be unreachable" is not a guarantee, and the
        # cost of being wrong here is the whole benchmark.
        init = self.initial_wns if self.initial_wns is not None else float('-inf')
        saved = self._best_saved_wns
        if saved is not None and saved < init - 1e-9:
            logger.error(f"Finalize: snapshot WNS {saved} is worse than the input's {init}; "
                         f"keeping the input DCP")
            print(f"⚠ Best snapshot regressed against the input — shipping the input")
            return

        if verify:
            ok, reason = await self._verify_snapshot_file()
            if not ok:
                logger.error(f"Finalize: the best snapshot did not verify ({reason}); "
                             f"falling back to the input DCP, which is valid by definition")
                print(f"⚠ Best snapshot failed final verification ({reason}) — shipping the input")
                if self._input_dcp_path is not None:
                    atomic_copy(self._input_dcp_path, output_dcp)
                return

        # Atomic: on failure output_dcp keeps whatever valid DCP it already held.
        if atomic_copy(self._best_snapshot, output_dcp):
            logger.info(f"Finalized output DCP from best snapshot "
                        f"(WNS {self._best_saved_wns}) -> {output_dcp}")
        else:
            logger.error(f"Finalize: could not copy best snapshot onto {output_dcp}; "
                         f"it retains the last valid design")
            print(f"⚠ Final write failed — {output_dcp} retains the last valid design")

    async def _verify_snapshot_file(self) -> tuple:
        """Re-open the snapshot FILE and re-run the ship gate on it. Returns
        (ok, reason).

        FAIL CLOSED, and deliberately narrow about what it swallows: the previous
        finalize wrapped everything in `except Exception: logger.warning(...)`, so
        a finalize that never ran looked exactly like a clean one. Here anything
        unexpected is an explicit reject with a reason, because "we could not
        confirm" must fall back to the input rather than ship on hope.
        """
        try:
            out = await self._open_checkpoint(self._best_snapshot)   # opens + re-sources ::opt::
        except Exception as e:                       # transport died mid-open
            return False, f"open_checkpoint raised: {e}"
        if not out or "error" in out.lower():
            return False, f"open_checkpoint failed: {out[:160]}"

        # Opening replaced the design, so the cached verdict describes something
        # else. _raw_vivado does not bump the generation (it is not the model's
        # call), so invalidate explicitly.
        self._design_gen += 1
        try:
            return await self._ship_gate()
        except Exception as e:
            return False, f"ship gate raised: {e}"

    async def _handle_deadline(self, output_dcp: Path) -> bool:
        """The 1-hour hard limit was reached (asyncio.wait_for cancelled the in-flight tool
        call). STOP optimizing, but run ALL the normal later processing so downstream
        reporting/validation and the batch table are complete — do NOT hard-exit:
          (1) finalize the output DCP from the best on-disk snapshot (file copy, no Vivado),
          (2) write token_usage.json + print the summary (so the batch table has metrics).
        Vivado is torn down afterwards by the normal cleanup() (or the backstop if that hangs).

        Finalize runs with verify=False here on purpose: re-opening a large
        checkpoint takes minutes and the backstop fires 90 s after the limit, so a
        verification started now would be killed mid-flight. output_dcp already
        holds the gated best, so keeping it is both correct and the only option
        that fits in the remaining time."""
        self._deadline_hit = True
        self.end_time = time.time()
        opt_min = self._opt_deadline_s / 60.0
        cap_min = self.time_budget_seconds / 60.0
        logger.warning(f"Optimization deadline ({opt_min:.0f} min) reached — stopping, finalizing "
                       f"best DCP (WNS {self.best_wns:.3f}) within the {cap_min:.0f}-min cap")
        print(f"\n*** OPTIMIZATION DEADLINE ({opt_min:.0f} min) reached — stopping now; running "
              f"finalize + reporting inside the {cap_min:.0f}-min wall-clock cap. ***\n")
        try:
            await self._finalize_output(output_dcp, verify=False)
        except Exception as e:
            logger.error(f"finalize on deadline failed: {e}")
        try:
            # writes token_usage.json (what the batch table reads) + prints the summary
            self._print_optimization_summary(max_iterations_reached=False)
        except Exception as e:
            logger.error(f"summary on deadline failed: {e}")
        # Reaching the wall-clock cap is a NORMAL termination that shipped the best
        # verified DCP -- not a failure. Success means "produced a valid ship", which is
        # true by construction (output_dcp seeded from the input, refreshed on every
        # gated promotion). Only an inability to ship (couldn't seed the output) is False.
        return True

    def _install_deadline_backstop(self, output_dcp: Path, safety: float = 5.0) -> None:
        """Absolute last-resort guard: an independent thread that force-stops the process
        ONLY if the graceful deadline handling (wait_for cancel + finalize + cleanup) itself
        hangs. It fires at the TOTAL cap (time_budget_seconds), a hair early by `safety`
        seconds so the process self-terminates before the external clock; normal runs cancel
        it first. The optimization loop already stopped FINALIZE_RESERVE_S earlier, so the
        graceful tail owns that whole window and this only trips if the tail itself hung.
        On firing it saves the token report, kills all children, and hard-exits."""
        limit = max(1.0, float(getattr(self, "time_budget_seconds", 3600.0)) - safety)

        def _fire():
            try:
                sys.stderr.write(
                    "\n*** DEADLINE BACKSTOP: graceful finalize did not finish within the "
                    "reserve — killing to honor the wall-clock cap. ***\n")
                sys.stderr.flush()
            except Exception:
                pass
            # NO DCP COPY HERE, deliberately. This runs on a daemon thread with
            # no event loop and ends in os._exit(1), so it could only copy blind
            # -- it can never verify. It used to promote _best_snapshot sight
            # unseen. It no longer needs to: output_dcp is seeded from the input
            # at t=0 and atomically refreshed from the snapshot on every GATED
            # promotion, so the best verified design is already sitting there.
            try:
                self.end_time = time.time()
                self.save_token_usage_report(self.run_dir / "token_usage.json")
            except Exception:
                pass
            self._kill_descendants()
            os._exit(1)

        self._backstop_timer = threading.Timer(limit, _fire)
        self._backstop_timer.daemon = True
        self._backstop_timer.start()
        logger.info(f"Deadline backstop armed: {limit/60:.1f} min (total cap {self.time_budget_seconds/60:.1f} "
                    f"min − {safety:.0f}s; opt stops at {self._opt_deadline_s/60:.1f} min)")

    def _cancel_deadline_backstop(self) -> None:
        """Disarm the backstop on any normal completion (including a graceful deadline stop)."""
        t = getattr(self, "_backstop_timer", None)
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
            self._backstop_timer = None

    def _kill_descendants(self) -> None:
        """Best-effort kill of every descendant process (MCP servers, Java, Vivado, xsim)
        via /proc — independent of process groups/sessions, so it also reaches the
        pexpect-spawned Vivado. Leaves-first so parents don't respawn."""
        import signal as _sig
        try:
            me = os.getpid()
            kids: dict[int, list[int]] = {}
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat") as f:
                        # format: "pid (comm) state ppid ..."; comm may contain spaces/parens,
                        # so split after the LAST ')'.
                        after = f.read().rsplit(") ", 1)[-1].split()
                    ppid = int(after[1])
                    kids.setdefault(ppid, []).append(int(entry))
                except Exception:
                    continue
            order: list[int] = []
            stack = [me]
            while stack:
                p = stack.pop()
                for c in kids.get(p, []):
                    order.append(c)
                    stack.append(c)
            for pid in reversed(order):        # kill leaves before their parents
                try:
                    os.kill(pid, _sig.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
    
    async def process_response(self, response) -> tuple[str, bool]:
        """Process LLM response, execute tool calls, return final text and done flag."""
        # Validate response structure with detailed logging
        try:
            if not response:
                raise ValueError("Response is None")
            if not hasattr(response, 'choices'):
                raise ValueError(f"Response has no 'choices' attribute. Response type: {type(response)}, Response: {response}")
            if response.choices is None:
                raise ValueError("Response.choices is None")
            if len(response.choices) == 0:
                raise ValueError("Response choices list is empty")
            
            message = response.choices[0].message
            if not message:
                raise ValueError("Message is None")
        except Exception as e:
            logger.error(f"Failed to parse response structure: {e}")
            logger.error(f"Response object: {response}")
            raise
        
        # Convert message to dict, excluding None values which can cause issues
        message_dict = message.model_dump(exclude_none=True)
        self.messages.append(message_dict)
        
        if self.debug:
            logger.debug(f"Added message to conversation: {json.dumps(message_dict, indent=2)[:500]}...")
        
        content = message.content or ""

        # SCRIPT-FIRST, NO TOOLS: the reply IS the action. The model was asked to answer
        # with ONE Tcl script in a fenced block; extract it and run it through the SAME
        # validated path a run_tcl tool-call used to take (script_mutates routes a
        # mutation to _run_plan_script; a query script takes the raw path). The model is
        # never told about MCP or tools -- how the script executes is the harness's job.
        script = extract_tcl_script(content)
        if script:
            result = await self.call_tool("vivado_run_tcl", {"command": script})

            # A mutating script returns the compact plan-attempt record; a report/query
            # script can return a large report -- truncate that (unless --raw-reports).
            MAX_RESULT_LENGTH = 50000  # characters
            if not getattr(self, "dump_raw_reports", False) and len(result) > MAX_RESULT_LENGTH:
                logger.warning(f"run_tcl result is {len(result)} chars, truncating to {MAX_RESULT_LENGTH}")
                result = result[:MAX_RESULT_LENGTH] + f"\n...[truncated {len(result) - MAX_RESULT_LENGTH} characters]"
            # Appended last so the budget note is the final thing the model reads.
            result += self._budget_note()

            # The script's result and the re-measured state go back as USER messages
            # (there is no tool role without a tool call). We ran a script, so this turn
            # is NOT done -- return to the outer loop, which makes the next LLM call. The
            # refresh must happen HERE, before that next call, so the model sees fresh
            # state. (This used to `return await self.get_completion()`, recursing one
            # frame deep per script; the loop is flat now.)
            self.messages.append({"role": "user", "content": result})
            await self._maybe_refresh_state()
            return content, False

        # No script in the reply -- either a completion claim, or off-format drift.
        # The model saying it is finished is a HINT, not the decision. It used to
        # be the decision: any of these phrases in the reply ended the run and
        # finalized, so "the design meets timing" ended it whether or not the
        # design did. Treat the phrase as a trigger to MEASURE, and let the
        # measurement decide.
        # A false positive here is SAFE: it only routes the reply to the measurement gate
        # (_timing_is_actually_met) instead of the off-format nudge, and the gate decides
        # on real WNS/route/hold. So err toward catching the model's plain-English "done".
        # The old list missed exactly the phrases this run used -- "I am finished" (absent)
        # and "optimization run is complete" (the "run is" breaks "optimization complete"),
        # so the model's done-claims fell through to the "no Tcl script" nudge and it never
        # reached the gate. See runs/.../vexriscv_re-place call_050.
        lc = content.lower()
        claims_done = (
            any(phrase in lc for phrase in [
                "optimization complete",
                "i am finished", "i'm finished", "i am done", "i'm done",
                "timing is met",
                "wns >= 0",
                "no more optimizations",
                "design meets timing",
                "successfully saved",
                "final design saved",
            ])
            # "optimization <run is / has / now> complete|completed" and similar phrasings
            or re.search(r"optimization\b[\s\w,]{0,30}?\bcomplet(?:e|ed)\b", lc) is not None
        )
        if not claims_done:
            # Off-format: the model reasoned but emitted no script and did not claim to
            # be done. Nudge it back to the one required shape rather than re-send the
            # same context and risk a stuck turn.
            self.messages.append({
                "role": "user",
                "content": ("Your reply contained no Tcl script. Act by emitting EXACTLY one "
                            "Tcl script inside a ```tcl code block — that script is the only "
                            "way to change or inspect the design. If and only if you are truly "
                            "finished, say so explicitly.")
            })
            return content, False

        timing_met = await self._timing_is_actually_met()
        if timing_met:
            logger.info("Model declared completion and a shippable WNS>=0 design is banked "
                        "— stopping.")
        else:
            # The model declared it is finished but timing is not met. STOP anyway,
            # equivalent to reaching the wall-clock cap: on a design that cannot meet
            # timing, pushing back "continue optimizing" only makes it churn done-claims
            # against a gate it can never pass until the deadline (see runs/.../
            # vexriscv_re-place). The scored path is safe: output_dcp already holds the
            # best VERIFIED design (or the input, seeded at t=0), and _finalize_output
            # refuses to ship worse than the input -- so this ships a valid DCP, never a
            # zero. The cost of a premature/mistaken "done" is lost optimization time, not
            # a bad ship.
            logger.warning(f"Model declared completion with timing unmet (best banked WNS "
                           f"{self.best_wns:.3f} ns) — stopping and shipping the best design, "
                           f"as at the time cap.")
            self._model_declared_done = True
        return content, True

    async def _timing_is_actually_met(self) -> bool:
        """Has a design that is genuinely shippable AND meets setup timing been
        banked? Both halves matter: WNS >= 0 on a design that is unrouted or
        violates hold is worth nothing, and a design that only ever existed in
        memory cannot be shipped."""
        if self._best_saved_wns is None or self._best_saved_wns < 0:
            return False
        if self._best_snapshot is None or not Path(self._best_snapshot).exists():
            return False
        ok, reason = await self._ship_gate()
        if not ok:
            logger.info(f"Completion check: banked WNS {self._best_saved_wns} >= 0 "
                        f"but the design is not shippable ({reason})")
        return ok
    
    # ------------------------------------------------------------------
    # Small tool/report helpers (best-effort; never raise)
    # ------------------------------------------------------------------
    async def _tool(self, name: str, args: dict) -> str:
        """Call an MCP tool, return its text, log-and-swallow on failure."""
        try:
            return await self.call_tool(name, args) or ""
        except Exception as e:
            logger.warning(f"tool {name} failed: {e}")
            return ""

    async def _raw_report(self, tcl: str) -> str:
        """Run a report on the RAW path: no WNS tracking, no tool_call_details, and
        crucially no design-generation bump. Reports do not change the design, but
        they go through run_tcl, which is in _DESIGN_MUTATING_TOOLS because the
        model also mutates through it -- so routing them via call_tool would
        invalidate the ship-gate cache on every single report."""
        return await self._raw_vivado("run_tcl", {"command": f"{tcl} -return_string"})

    async def _refresh_state_digest(self) -> Optional[str]:
        """Adapter: inject the raw run_tcl runner into vivado_parse.build_state_digest
        (pure orchestration), store the returned delta-tracking state, return the digest."""
        digest, _m, new_wns, new_prims = await build_state_digest(
            lambda cmd: self._raw_vivado("run_tcl", {"command": cmd}),
            clock_period=self.clock_period,
            prev_wns=getattr(self, "_prev_digest_wns", None),
            prev_prims=getattr(self, "_prev_digest_prims", None),
            best_saved_wns=self._best_saved_wns)
        self._prev_digest_wns = new_wns
        self._prev_digest_prims = new_prims
        return digest

    async def _maybe_refresh_state(self) -> None:
        """Feed a re-measured state digest back to the model between turns.

        The model otherwise works from the initial diagnosis for the whole hour --
        which is why the system prompt has to spend 40 lines telling it not to
        re-run reports it was given once, at the start, and has had no update on
        since. Supplying the state is cheaper than arguing about it.

        Two sources, one injection point. A plan script already ran the reanalyze
        step and STASHED its digest (_pending_digest) -- inject that with no second
        measurement (a tool result must immediately follow the assistant tool-call,
        so _run_plan_script cannot inject it itself). Any other design change falls
        back to measuring here. The heavy-design rate-limit was removed: the reanalyze
        runs on every plan attempt regardless of size for now (a lite/full-report
        split by design size will replace this); gating the INJECTION would only hide
        freshly-measured state the run already paid to gather.
        """
        pending = getattr(self, "_pending_digest", None)
        if pending is not None:
            self._pending_digest = None
            self._last_refresh_gen = self._design_gen
            self._last_refresh_turn = self.llm_call_count
            logger.info("Injected the plan-attempt state digest into the conversation")
            self.messages.append({"role": "user", "content": pending + self._plateau_panel()})
            return

        if self._design_gen == self._last_refresh_gen:
            return                                    # nothing changed the design
        remaining = self._opt_deadline_s - (time.time() - self.start_time)
        if remaining < REFRESH_MIN_REMAINING_FRAC * self._opt_deadline_s:
            return                                    # spend what is left acting, not measuring

        digest = await self._refresh_state_digest()
        self._last_refresh_gen = self._design_gen
        self._last_refresh_turn = self.llm_call_count
        if not digest:
            return
        logger.info("Injected a re-measured design state into the conversation")
        self.messages.append({"role": "user", "content": digest + self._plateau_panel()})

    # ------------------------------------------------------------------
    # Gather steps
    # ------------------------------------------------------------------
    async def _open_design(self, input_dcp: Path) -> None:
        """Steps 1/2/5: init RapidWright, open the DCP in Vivado, load it in RapidWright."""
        print("Initializing RapidWright...")
        r = await self.call_tool("rapidwright_initialize_rapidwright", {})
        if "error" in r.lower() and "success" not in r.lower():
            raise RuntimeError(f"Failed to initialize RapidWright: {r}")
        print(f"Opening checkpoint: {input_dcp.name}")
        r = await self.call_tool("vivado_open_checkpoint", {"dcp_path": str(input_dcp.resolve())})
        if "error" in r.lower() and "opened successfully" not in r.lower():
            raise RuntimeError(f"Failed to open checkpoint: {r}")
        r = await self.call_tool("rapidwright_read_checkpoint", {"dcp_path": str(input_dcp.resolve())})
        self._rw_loaded = not ("error" in r.lower() and "success" not in r.lower())
        if not self._rw_loaded:
            print(f"⚠ Could not load design in RapidWright: {r}")

        # Source the ::opt:: library now that a design is open. Optional by design:
        # native Vivado commands still work without it, so a failure here degrades the
        # planner's vocabulary rather than the run.
        if await self._ensure_recipes_loaded():
            print("✓ Loaded the ::opt:: recipe library\n")
        else:
            print("⚠ ::opt:: recipe library not loaded; native Tcl only\n")

    async def _gather_timing(self) -> None:
        """Step 3: timing summary. Always parsed — the optimization loop needs WNS/Fmax."""
        timing_report = await self._tool("vivado_report_timing_summary", {})
        self._raw_reports["timing-summary"] = timing_report
        info = parse_timing_summary(timing_report)
        self.initial_tns = info.setup_tns
        self.initial_failing_endpoints = info.setup_failing_endpoints
        # Baseline hold / pulse-width. The input DCP is contest-valid by
        # definition, so these are the "must not be worsened" references for
        # the lenient mid-run policy in hold_pw_ok().
        self.initial_whs = info.hold_whs
        self.initial_pw = info.pulse_width_wns
        self.clock_period = await super().get_clock_period(self._call_vivado_tool)
        tw = await super().get_wns_for_target_clock(self._call_vivado_tool)
        self.initial_wns = tw if tw is not None else info.setup_wns
        self.best_wns = self.initial_wns if self.initial_wns is not None else float('-inf')

    async def _gather_fanout(self) -> None:
        """Step 4: critical high-fanout nets."""
        rep = await self._tool("vivado_get_critical_high_fanout_nets",
                               {"num_paths": 50, "min_fanout": 100})
        self._raw_reports["high-fanout-nets"] = rep
        self.high_fanout_nets = parse_high_fanout_nets(rep)

    async def _gather_reports(self) -> None:
        """Adapter: delegate the initial-analysis report loop to
        vivado_parse.gather_vivado_reports (injected run_tcl = the call_tool path, the
        same one _report used), then store the raw texts + clock count.

        Hands over the timing summary _gather_timing already measured, so the bundle
        re-sources the clock count from it instead of probing Vivado for it. Degrades
        to the probe if it cannot be read, so a miss costs time, never correctness."""
        raw, self._nclocks = await gather_vivado_reports(
            lambda cmd: self._tool("vivado_run_tcl", {"command": cmd}),
            tsum_text=self._raw_reports.get("timing-summary", ""))
        self._raw_reports.update(raw)

    def _parse_diagnosis(self) -> None:
        """Step 7 parse: classify the design from the captured core reports (facts only)."""
        r = self._raw_reports
        self._diag = {
            "split": parse_worst_path_delay_split(r.get("design-analysis-timing", "")),
            "cong":  parse_congestion_level(r.get("congestion", "")),
            "qor":   parse_qor_assessment(r.get("qor-assessment", "")),
            "lut":   parse_lut_utilization(r.get("utilization", "")),
            "devutil": parse_device_utilization(r.get("utilization", "")),
            "qsug":  r.get("qor-suggestions", ""),
        }
        self.diagnosis = classify_design(
            self._diag["split"], self._diag["cong"], self._diag["qor"], self._diag["lut"],
            slices=(self._diag.get("devutil") or {}).get("slices"))

    def _parse_extended(self) -> None:
        """Step 9 parse: turn each captured extended report into a fact (or 'within range')."""
        self.extended_findings = []
        self.extended_checked = []
        for key, _tcl, parser, mc_only in EXTENDED_CHECKS:
            if mc_only and self._nclocks <= 1:
                continue
            if parser is None:                       # captured raw only, not parsed
                continue
            txt = self._raw_reports.get(key, "")
            if not txt:
                continue
            fact = parser(txt)
            (self.extended_findings.append((key, fact)) if fact
             else self.extended_checked.append(key))

    async def _gather_spread(self, input_dcp: Path) -> None:
        """Step 6: RapidWright critical-path spread (Manhattan tile distance)."""
        if not getattr(self, "_rw_loaded", False):
            return
        temp_path = Path(self.temp_dir) / "initial_critical_paths.json"
        await self._tool("vivado_extract_critical_path_cells",
                         {"num_paths": 50, "output_file": str(temp_path)})
        res = await self._tool("rapidwright_analyze_critical_path_spread",
                               {"input_file": str(temp_path)})
        try:
            data = json.loads(res)
            self._spread = {"max_distance": data.get("max_distance_found", 0),
                            "avg_distance": data.get("avg_max_distance", 0),
                            "paths_analyzed": data.get("paths_analyzed", 0)}
            self._spread_worst = data.get("worst_paths", [])
        except Exception as e:
            logger.warning(f"spread parse failed: {e}")

    async def _gather_structure_facts(self) -> None:
        """Extra structural facts the agent reaches for when given the raw reports (verified from
        the raw-report experiment): top modules by utilization, which module instances the worst
        paths run through, the column span of the critical cells, and un-pipelined (RTL-bound) DSPs.
        Uses data already gathered (util-hier report, the critical-paths JSON, the spread result)
        plus one cheap DSP-register query."""
        self._struct = {"top_hier": [], "crit_hier": [], "col_span": None, "unpiped_dsp": None}
        # (a) heaviest modules by LUTs — where the logic mass sits
        try:
            self._struct["top_hier"] = parse_top_hierarchies(self._raw_reports.get("util-hier", ""))
        except Exception as e:
            logger.warning(f"top-hierarchy parse failed: {e}")
        # (b) which module instances the worst paths live in — the cone to target
        try:
            jpath = Path(self.temp_dir) / "initial_critical_paths.json"
            if jpath.exists():
                self._struct["crit_hier"] = parse_crit_hierarchies(json.loads(jpath.read_text()))
        except Exception as e:
            logger.warning(f"crit-hierarchy parse failed: {e}")
        # (c) column span of the critical cells — how far the cone is stretched
        try:
            cols = [loc["col"] for wp in getattr(self, "_spread_worst", [])
                    for loc in (wp.get("max_pair") or []) if isinstance(loc, dict) and "col" in loc]
            if cols:
                self._struct["col_span"] = (min(cols), max(cols))
        except Exception as e:
            logger.warning(f"col-span parse failed: {e}")
        # (d) un-pipelined DSPs (PREG=0) — a hard-macro path through these is RTL-bound (cheap query)
        try:
            out = await self._tool("vivado_run_tcl", {"command":
                'puts "DSPNP=[llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ DSP48* && PREG == 0}]]"; '
                'puts "DSPTOT=[llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ DSP48*}]]"'})
            mnp = re.search(r"DSPNP=(\d+)", out); mtot = re.search(r"DSPTOT=(\d+)", out)
            if mtot and int(mtot.group(1)) > 0 and mnp and int(mnp.group(1)) > 0:
                self._struct["unpiped_dsp"] = (int(mnp.group(1)), int(mtot.group(1)))
        except Exception as e:
            logger.warning(f"un-pipelined DSP query failed: {e}")

    async def _gather_detour(self) -> None:
        """Step 8: RapidWright routing-detour on the critical-path cells (deduped by cell)."""
        if not getattr(self, "_rw_loaded", False):
            return
        pins = Path(self.temp_dir) / "initial_critical_path_pins.json"
        await self._tool("vivado_extract_critical_path_pins",
                         {"num_paths": 50, "output_file": str(pins)})
        res = await self._tool("rapidwright_analyze_net_detour",
                               {"input_file": str(pins), "detour_threshold": 2.0})
        try:
            dd = json.loads(res)
            if dd.get("status") == "success":
                best = {}
                for c in dd.get("candidates", []):
                    cell = str(c.get("cell")); ratio = float(c.get("max_detour_ratio", 0))
                    if cell not in best or ratio > best[cell]:
                        best[cell] = ratio
                self.detour_candidates = sorted(best.items(), key=lambda kv: -kv[1])
        except Exception as e:
            logger.warning(f"detour parse failed: {e}")

    # ------------------------------------------------------------------
    # Summary builders
    # ------------------------------------------------------------------
    def _build_raw_summary(self) -> str:
        """Adapter: marshal gathered state into vivado_parse.render_raw (pure)."""
        return render_raw(self._raw_reports)

    def _build_summary(self) -> str:
        """Adapter: marshal gathered state into vivado_parse.render_analysis (pure)."""
        return render_analysis(
            diag=self._diag, diagnosis=self.diagnosis, raw_reports=self._raw_reports,
            heavy=getattr(self, "_heavy_design", False),
            dcp_size_mb=getattr(self, "_dcp_size_mb", None),
            heavy_reason=getattr(self, "_heavy_reason", "heavy"),
            struct=getattr(self, "_struct", {}) or {},
            detour_candidates=self.detour_candidates,
            extended_findings=self.extended_findings, extended_checked=self.extended_checked,
            clock_period=self.clock_period, initial_wns=self.initial_wns,
            initial_tns=self.initial_tns, initial_failing_endpoints=self.initial_failing_endpoints,
            spread=self._spread, high_fanout_nets=self.high_fanout_nets)

    async def perform_initial_analysis(self, input_dcp: Path) -> str:
        """Deterministic pre-LLM analysis (no LLM call). Opens the DCP, gathers the
        Vivado reports + RapidWright physical analyses, and returns a summary for the LLM.

        If ``self.dump_raw_reports`` is True, the summary is the COMPLETE reports verbatim
        (no parsing, no derived classification); the LLM interprets them itself. Otherwise
        the summary is structured FACTS only — there is intentionally no recommended lever.
        See docs/INITIAL_ANALYSIS.md.
        """
        logger.info("Performing initial design analysis...")
        print("\n=== Initial Design Analysis ===\n")
        raw = getattr(self, "dump_raw_reports", False)
        self._raw_reports = {}
        self.diagnosis = None
        self.detour_candidates = []
        self.extended_findings = []
        self.extended_checked = []
        self._diag = {}
        self._spread = None
        self._spread_worst = []
        self._struct = {"top_hier": [], "crit_hier": [], "col_span": None, "unpiped_dsp": None}

        # HEAVY-DESIGN GATE. Primary: DCP file size (checked BEFORE opening, so we can skip
        # slow analyses up front). Backstop: how long open + first timing report take (adapts
        # to machine load). On a heavy design a full place/route won't fit the hour and the
        # RapidWright detour analysis alone can consume the whole budget (it starved ispd16 to
        # 0 gain / 0 LLM calls), so we skip it and flag the design to the LLM.
        self._heavy_design, self._dcp_size_mb, self._heavy_reason = assess_dcp_weight(input_dcp)

        _t = time.time()
        await self._open_design(input_dcp)
        _open_s = time.time() - _t
        _t = time.time()
        await self._gather_timing()
        _report_s = time.time() - _t
        if not self._heavy_design and (_open_s > HEAVY_OP_SECONDS or _report_s > HEAVY_OP_SECONDS):
            self._heavy_design = True
            self._heavy_reason = f"open {_open_s:.0f}s / timing {_report_s:.0f}s > {HEAVY_OP_SECONDS:.0f}s"
        if self._heavy_design:
            logger.info(f"HEAVY design detected ({self._heavy_reason}) — will skip the slow "
                        f"RapidWright detour analysis to preserve the time budget")

        await self._gather_fanout()
        await self._gather_reports()

        if not raw:
            self._parse_diagnosis()
            self._parse_extended()
            await self._gather_spread(input_dcp)
            if self._heavy_design:
                # Skip the RapidWright net-detour cross-check on heavy designs — it can take
                # tens of minutes to >1h and would consume the optimization budget.
                self.detour_candidates = []
            else:
                await self._gather_detour()
            await self._gather_structure_facts()

        summary = self._build_raw_summary() if raw else self._build_summary()
        print(summary)
        print()

        # Always persist the initial diagnosis to the run dir — every DCP, every flow
        # (LLM agent, --diagnose, batch). See TODO.md item 1.
        try:
            (self.run_dir / "initial_diagnosis.txt").write_text(summary)
            if self.diagnosis:
                (self.run_dir / "initial_diagnosis.json").write_text(
                    json.dumps(self.diagnosis, indent=2))
            logger.info(f"Initial diagnosis written to {self.run_dir}/initial_diagnosis.txt")
        except Exception as e:
            logger.warning(f"Could not write initial diagnosis: {e}")

        return summary

    def _check_llm_budget(self) -> None:
        """Refuse to START a call that would breach the contest LLM budget.

        Checked before the call, never after: the point is not to notice the
        overrun but to not commit it. total_cost was tracked and printed but
        never enforced, so a run could sail past $1 -- a rules violation rather
        than a low score.
        """
        if self.llm_call_count >= MAX_LLM_CALLS:
            raise LLMBudgetExceeded(
                f"LLM call cap reached ({MAX_LLM_CALLS} calls)")
        if self.total_cost >= MAX_LLM_COST_USD:
            raise LLMBudgetExceeded(
                f"LLM cost cap reached (${self.total_cost:.4f} of "
                f"${MAX_LLM_COST_USD:.2f}; contest limit is $1.00)")

    async def get_completion(self) -> tuple[str, bool]:
        """Get LLM completion and process it."""
        self._check_llm_budget()
        try:
            self.llm_call_count += 1
            logger.info(f"LLM API call #{self.llm_call_count}")

            # Log the full prompt to a text file for each LLM call
            prompt_log_path = self._prompt_logs_dir / f"llm_prompt_call_{self.llm_call_count:03d}.txt"
            with open(prompt_log_path, 'w') as f:
                f.write(f"=== LLM Call #{self.llm_call_count} | Iteration {self.iteration} ===\n\n")
                for msg in self.messages:
                    role = msg.get("role", "unknown").upper()
                    content = msg.get("content", "")
                    f.write(f"--- {role} ---\n{content}\n\n")
            logger.info(f"LLM prompt logged to: {prompt_log_path}")

            # extra_body usage accounting is OpenRouter-specific; other providers reject it
            extra_kwargs = {}
            if PROVIDER == "openrouter":
                extra_kwargs["extra_body"] = {"usage": {"include": True}}

            # Run the SYNCHRONOUS SDK call off the event loop. The whole optimize
            # body runs under asyncio.wait_for(deadline); a blocking call here would
            # not yield, so wait_for could not cancel it and a hung provider call
            # would sail past the wall-clock cap into the os._exit backstop. Awaiting
            # a thread keeps the loop cancellable at the deadline. The client's own
            # timeout (LLM_CALL_TIMEOUT_S) bounds the detached thread.
            response = await asyncio.to_thread(
                lambda: self.openai.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    # No tools: script-first with NO tool list. The model replies with a
                    # Tcl script (a fenced block); process_response extracts and runs it.
                    # No max_tokens: a 4096 cap silently truncated reasoning models
                    # mid-response, so the reply never closed and the turn was
                    # wasted -- on the ruthwik branch that voided all 14 iterations of
                    # one run before it was diagnosed. Spend is bounded by
                    # MAX_LLM_CALLS and MAX_LLM_COST_USD, which is where it belongs.
                    **extra_kwargs
                ))
            
            # Validate response immediately
            if response is None:
                raise ValueError("API returned None response")
            
            # Extract token usage information from OpenRouter
            if hasattr(response, 'usage') and response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                total_tokens = response.usage.total_tokens
                
                # Update cumulative totals
                self.total_prompt_tokens += prompt_tokens
                self.total_completion_tokens += completion_tokens
                self.total_tokens += total_tokens
                
                # Get actual cost from OpenRouter (in credits/dollars)
                call_cost = 0.0
                if hasattr(response.usage, 'cost') and response.usage.cost is not None:
                    call_cost = float(response.usage.cost)
                    self.total_cost += call_cost
                else:
                    logger.warning("OpenRouter did not provide cost information")
                
                # Extract additional usage details if available
                cached_tokens = 0
                reasoning_tokens = 0
                if hasattr(response.usage, 'prompt_tokens_details') and response.usage.prompt_tokens_details:
                    if hasattr(response.usage.prompt_tokens_details, 'cached_tokens'):
                        cached_tokens = response.usage.prompt_tokens_details.cached_tokens or 0
                if hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details:
                    if hasattr(response.usage.completion_tokens_details, 'reasoning_tokens'):
                        reasoning_tokens = response.usage.completion_tokens_details.reasoning_tokens or 0
                
                # Store details for this call
                call_detail = {
                    "call_number": self.llm_call_count,
                    "iteration": self.iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "cost": call_cost,
                    "cached_tokens": cached_tokens,
                    "reasoning_tokens": reasoning_tokens
                }
                self.api_call_details.append(call_detail)
                
                # Log token usage
                cache_info = f", Cached: {cached_tokens:,}" if cached_tokens > 0 else ""
                reasoning_info = f", Reasoning: {reasoning_tokens:,}" if reasoning_tokens > 0 else ""
                cost_info = f" | Cost: ${call_cost:.4f}" if call_cost > 0 else ""
                
                logger.info(f"API call #{self.llm_call_count} - Tokens: {prompt_tokens} prompt + {completion_tokens} completion = {total_tokens} total{cost_info}{cache_info}{reasoning_info}")
                print(f"[API Call #{self.llm_call_count}] Tokens: {total_tokens:,} (Prompt: {prompt_tokens:,}, Completion: {completion_tokens:,}{cache_info}{reasoning_info}){cost_info}")
            else:
                logger.warning("No usage information in API response")
            
            # Debug logging
            if self.debug:
                logger.debug(f"Response type: {type(response)}")
                logger.debug(f"Response: {response}")
            
            # Check if response has error
            if hasattr(response, 'error') and response.error:
                raise ValueError(f"API returned error: {response.error}")

            # Append LLM response to the same prompt log file
            with open(prompt_log_path, 'a') as f:
                f.write("=== LLM RESPONSE ===\n\n")
                for choice in response.choices:
                    msg = choice.message
                    f.write(f"--- ASSISTANT (finish_reason: {choice.finish_reason}) ---\n")
                    if msg.content:
                        f.write(f"{msg.content}\n")
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            f.write(f"[TOOL CALL] {tc.function.name}({tc.function.arguments})\n")
                    f.write("\n")

            return await self.process_response(response)
            
        except Exception as e:
            logger.error(f"Error in get_completion: {e}")
            logger.error(f"Number of messages in conversation: {len(self.messages)}")
            if self.messages:
                logger.error(f"Last message: {self.messages[-1]}")
            raise
    
    async def optimize(self, input_dcp: Path, output_dcp: Path) -> bool:
        """Run the optimization workflow under a HARD, independent 1-hour wall-clock limit.

        The contest allows at most 1 hour per DCP; running longer violates the rules. An
        independent watchdog THREAD (see _install_deadline_watchdog) enforces this no matter
        what the loop is doing — even while blocked inside a multi-hour Vivado place/route or a
        slow RapidWright analysis (the between-iteration check alone could not, which is why
        earlier runs overran to 300+ min). At the deadline the watchdog finalizes the best DCP
        from the on-disk snapshot, kills the Vivado/RapidWright children, and hard-exits."""
        # Start timing the optimization process
        self.start_time = time.time()

        # Wire up best-DCP auto-save: every measured gain is written to a protected
        # snapshot, and the final output is taken from it (never lose a gain; survive a
        # crash / restart / time-budget kill / hard deadline).
        self._output_dcp_path = output_dcp
        self._input_dcp_path = input_dcp
        self._best_snapshot = Path(self.run_dir) / "maya_best.dcp"

        # SEED THE OUTPUT FROM THE INPUT, BEFORE ANY WORK.
        # The output DCP is the only thing that gets scored. Until now nothing
        # created it: if no improvement was ever tracked and the model never
        # wrote it itself, the run ended with NO FILE AT ALL -- which validates
        # as a failure and scores ZERO. The input is contest-valid by definition,
        # so seeding it makes the worst case alpha=0 (no gain) instead of 0 (no
        # output). Every later write only ever replaces this with something
        # verified to be at least as good.
        if not atomic_copy(input_dcp, output_dcp):
            # Nothing downstream can recover from this: we cannot produce a
            # scored artifact at all, so fail loudly rather than discover it at
            # the deadline.
            logger.error(f"Could not seed output DCP from input: {input_dcp} -> {output_dcp}")
            print(f"\n✗ FATAL: could not write {output_dcp} from the input DCP. "
                  f"Aborting rather than running an hour with nothing to ship.\n")
            return False
        logger.info(f"Seeded output DCP from input (valid from t=0): {output_dcp}")

        self._deadline_hit = False
        # A last-resort backstop thread (armed before any heavy work) only fires if the
        # graceful stop below ITSELF hangs. Normal runs cancel it in the finally.
        self._install_deadline_backstop(output_dcp)
        try:
            # asyncio.wait_for cancels the in-flight tool await at the OPTIMIZATION deadline
            # (total cap minus the finalize reserve; all heavy work — place/route/analysis —
            # runs through an awaited MCP call, so it is cancellable). We then STOP optimizing
            # and run finalize + reporting inside the remaining reserve, before the cap.
            return await asyncio.wait_for(
                self._optimize_body(input_dcp, output_dcp),
                timeout=self._opt_deadline_s)
        except asyncio.TimeoutError:
            return await self._handle_deadline(output_dcp)
        finally:
            self._cancel_deadline_backstop()

    async def _optimize_body(self, input_dcp: Path, output_dcp: Path) -> bool:
        """The optimization workflow itself (run under the hard-deadline watchdog)."""
        # Perform initial analysis without LLM
        try:
            initial_analysis = await self.perform_initial_analysis(input_dcp)
        except Exception as e:
            logger.exception(f"Initial analysis failed: {e}")
            print(f"\n✗ Initial analysis failed: {e}\n")
            self.end_time = time.time()
            return False
        
        # Check if timing is already met
        if self.initial_wns is not None and self.initial_wns >= 0:
            print("✓ Design already meets timing! No optimization needed.\n")
            logger.info("Design already meets timing")
            # Goes through call_tool, so the write is staged, gated and swapped in
            # atomically by _staged_output_write -- the same one path the model's
            # own writes take. Setup timing being met says nothing about routing,
            # hold or pulse-width, and this used to write the scored path with no
            # check at all. If the gate rejects, output_dcp keeps the input, which
            # is contest-valid by definition and already meets timing.
            result = await self.call_tool("vivado_write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            })
            print(f"{result}\n")
            
            # End timing
            self.end_time = time.time()
            total_runtime = self.end_time - self.start_time
            
            # Print summary even for early exit
            print("\n=== No Optimization Required ===")
            initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            if initial_fmax is not None:
                print(f"Design already meets timing - Fmax: {initial_fmax:.2f} MHz (WNS: {self.initial_wns:.3f} ns)")
            else:
                print(f"Design already meets timing (WNS: {self.initial_wns:.3f} ns)")
            print(f"Total runtime: {total_runtime:.2f} seconds ({total_runtime/60:.2f} minutes)")
            print(f"LLM API calls: 0 (analysis performed without LLM)")
            print(f"Estimated cost: $0.00")
            print("="*70 + "\n")
            return True

        if _kb_variant() != "hand":                   # [pramana] facts only, no hand-written reads
            initial_analysis = _strip_derived_reads(initial_analysis)

        # Load and fill in system prompt with temp directory and input DCP path
        # Format the TEMPLATE ONLY, then append the ::opt:: reference. The reference documents
        # Tcl, whose braces str.format() would read as replacement fields — formatting the
        # concatenation killed every benchmark before its first LLM call.
        system_prompt_template, opt_reference = load_system_prompt_parts(
            recipes_loaded=self._recipes_loaded)
        system_prompt = system_prompt_template.format(
            temp_dir=self.temp_dir,
            input_dcp=input_dcp.resolve()
        ) + opt_reference
        
        # Initialize conversation with analysis results
        self.messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"""Optimize this FPGA design for timing.

PATHS:
- Input DCP: {input_dcp.resolve()}
- Output DCP (save final result here): {output_dcp.resolve()}
- Run directory (for intermediate files): {self.temp_dir}
- Best verified checkpoint: {self._best_snapshot}
    Harness-written. Every gain that passes the ship gate is saved here, so this
    file is ALWAYS fully routed with WHS >= 0 and WPWS >= 0. open_checkpoint it
    to undo a regression. It does not exist until the first banked gain.

CURRENT STATE:
- Vivado has the input design ALREADY OPEN and analyzed
- RapidWright has the input design ALREADY LOADED (from initial analysis)

INITIAL ANALYSIS RESULTS:
{initial_analysis}

Proceed with optimization strategy based on the analysis above. Do NOT reload the design in either Vivado or RapidWright - both already have it loaded.

Act by emitting EXACTLY one Tcl script inside a ```tcl code block — that script is the only way to change the design. If and only if you are truly finished, say so explicitly."""
            }
        ]
        
        # process_response no longer recurses, so ONE iteration == ONE LLM call: the
        # outer loop IS the per-call loop now (self.iteration tracks llm_call_count 1:1).
        # This is a safety backstop, NOT the intended stop: the run ends when the model
        # declares done (measured) or a real cap trips -- MAX_LLM_CALLS(60) /
        # MAX_LLM_COST_USD (via _check_llm_budget) / the wall-clock deadline. 100 sits
        # above MAX_LLM_CALLS on purpose, so a runaway (e.g. cost cap disabled) still stops.
        max_iterations = 100
        budget_min = self.time_budget_seconds / 60.0

        print("=== Starting LLM-Driven Optimization ===")
        print(f"    (time budget: {budget_min:.0f} min — best DCP is auto-saved on every gain)\n")

        while self.iteration < max_iterations:
            # Fallback in-loop stop (primary is the wait_for at the opt deadline): stop at the
            # OPTIMIZATION deadline (total cap minus the finalize reserve), leaving the tail room.
            elapsed = time.time() - self.start_time
            if elapsed >= self._opt_deadline_s:
                logger.warning(f"Optimization deadline of {self._opt_deadline_s/60:.0f} min reached "
                               f"({elapsed/60:.1f} min elapsed) — stopping; using last best saved DCP.")
                print(f"\n*** Optimization deadline ({self._opt_deadline_s/60:.0f} min) reached — stopping, "
                      f"keeping best DCP (WNS {self.best_wns:.3f} ns) within the {budget_min:.0f}-min cap. ***\n")
                break

            self.iteration += 1
            logger.info(f"=== Iteration {self.iteration} (elapsed {elapsed/60:.1f} min) ===")

            try:
                response_text, is_done = await self.get_completion()
                print(f"\n{response_text}\n")

                if is_done:
                    logger.info("Optimization workflow completed")
                    self.end_time = time.time()
                    await self._finalize_output(output_dcp)
                    self._print_optimization_summary()
                    return True

            except LLMBudgetExceeded as e:
                # Caught BEFORE the generic handler on purpose: feeding this back
                # into the conversation would spin, because the next call trips
                # the same check and never reaches the model. Stop and ship what
                # has been banked.
                logger.warning(f"{e} — stopping the loop and finalizing")
                print(f"\n*** {e} — stopping; keeping the best verified design. ***\n")
                break

            except Exception as e:
                logger.exception(f"Error during optimization: {e}")
                # Add error context to conversation
                self.messages.append({
                    "role": "user",
                    "content": f"An error occurred: {e}. Please verify your approach and continue or report if unrecoverable."
                })

        self.end_time = time.time()
        # Ensure the final output DCP is the best one we measured (TODO: don't lose a gain).
        await self._finalize_output(output_dcp)
        if (time.time() - self.start_time) >= self._opt_deadline_s:
            self._print_optimization_summary(max_iterations_reached=False)
        else:
            logger.warning("Reached maximum iterations")
            self._print_optimization_summary(max_iterations_reached=True)
        # Every path here (LLM-call cap, time-budget, max-iterations) is a NORMAL
        # termination that finalized the best verified DCP. That is a completed run, not
        # a failure -- the batch's WNS/gain columns already say whether it IMPROVED. False
        # is reserved for a genuine inability to ship (see the seed-output guard in
        # optimize()). A run that hit a cap and shipped a valid, improved design (e.g.
        # amd_mini-isp +106 MHz) was previously misreported as "did not complete".
        return True
    
    def save_token_usage_report(self, output_path: Path):
        """Save detailed token usage report to JSON file."""
        # Calculate total cached and reasoning tokens
        total_cached = sum(detail.get('cached_tokens', 0) for detail in self.api_call_details)
        total_reasoning = sum(detail.get('reasoning_tokens', 0) for detail in self.api_call_details)
        
        # Calculate tool call statistics
        total_tool_time = sum(detail['elapsed_time'] for detail in self.tool_call_details)
        tool_counts = {}
        for detail in self.tool_call_details:
            tool_name = detail['tool_name']
            if tool_name not in tool_counts:
                tool_counts[tool_name] = 0
            tool_counts[tool_name] += 1
        
        # Calculate total runtime
        total_runtime = None
        if self.start_time is not None:
            total_runtime = (self.end_time or time.time()) - self.start_time
        
        # Calculate fmax values
        initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
        best_fmax = self.calculate_fmax(self.best_wns, self.clock_period) if self.best_wns > float('-inf') else None
        fmax_improvement = (best_fmax - initial_fmax) if (initial_fmax is not None and best_fmax is not None) else None
        
        report = {
            "model": self.model,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_runtime_seconds": total_runtime,
                "total_llm_calls": self.llm_call_count,
                "total_iterations": self.iteration,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "total_cached_tokens": total_cached,
                "total_reasoning_tokens": total_reasoning,
                "total_cost": self.total_cost,
                "clock_period_ns": self.clock_period,
                "initial_wns": self.initial_wns,
                "best_wns": self.best_wns,
                "wns_improvement": self.best_wns - self.initial_wns if self.initial_wns is not None else None,
                "initial_fmax_mhz": initial_fmax,
                "best_fmax_mhz": best_fmax,
                "fmax_improvement_mhz": fmax_improvement,
                "total_tool_calls": len(self.tool_call_details),
                "total_tool_time_seconds": total_tool_time,
                "tool_call_counts": tool_counts
            },
            "per_llm_call_details": self.api_call_details,
            "per_tool_call_details": self.tool_call_details
        }
        
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Token usage report saved to {output_path}")
    
    def _print_optimization_summary(self, max_iterations_reached: bool = False):
        """Print detailed optimization summary including token usage and costs."""
        title = "Optimization Summary (Max Iterations Reached)" if max_iterations_reached else "Optimization Summary"
        print(f"\n{'='*70}")
        print(f"{title}")
        print(f"{'='*70}")
        
        # Calculate total runtime
        if self.start_time is not None:
            total_runtime = (self.end_time or time.time()) - self.start_time
            print(f"\nTOTAL RUNTIME: {total_runtime:.2f} seconds ({total_runtime/60:.2f} minutes)")
        
        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        result_lines = self._format_fmax_results(
            self.clock_period, self.initial_wns, best_wns, result_label="Best"
        )
        if result_lines:
            print(f"\nFMAX RESULTS:")
            print("\n".join(result_lines))
        
        # Iteration stats
        print(f"\nITERATION STATS:")
        print(f"  Total iterations:    {self.iteration}")
        print(f"  LLM API calls:       {self.llm_call_count}")
        
        # Token usage
        print(f"\nTOKEN USAGE:")
        print(f"  Prompt tokens:       {self.total_prompt_tokens:,}")
        print(f"  Completion tokens:   {self.total_completion_tokens:,}")
        print(f"  Total tokens:        {self.total_tokens:,}")
        
        # Calculate total cached and reasoning tokens
        total_cached = sum(detail.get('cached_tokens', 0) for detail in self.api_call_details)
        total_reasoning = sum(detail.get('reasoning_tokens', 0) for detail in self.api_call_details)
        
        if total_cached > 0:
            print(f"  Cached tokens:       {total_cached:,} (saved cost)")
        if total_reasoning > 0:
            print(f"  Reasoning tokens:    {total_reasoning:,}")
        
        # Cost
        print(f"\nCOST:")
        print(f"  Model:               {self.model}")
        if self.total_cost > 0:
            print(f"  Total cost:          ${self.total_cost:.4f}")
        else:
            print(f"  Total cost:          Not available")
        
        # Tool call summary
        if self.tool_call_details:
            print(f"\nTOOL CALLS SUMMARY:")
            print(f"  Total tool calls:    {len(self.tool_call_details)}")
            
            # Calculate total time spent in tool calls
            total_tool_time = sum(detail['elapsed_time'] for detail in self.tool_call_details)
            print(f"  Total tool time:     {total_tool_time:.2f}s")
            
            # Count by tool type
            tool_counts = {}
            for detail in self.tool_call_details:
                tool_name = detail['tool_name']
                if tool_name not in tool_counts:
                    tool_counts[tool_name] = 0
                tool_counts[tool_name] += 1
            
            print(f"\n  Tool call breakdown:")
            for tool_name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
                print(f"    {tool_name}: {count}")
            
            # Detailed tool call list
            print(f"\n  Detailed tool call log:")
            print(f"  {'#':<5} {'Iter':<6} {'Tool':<40} {'Time (s)':<12} {'WNS (ns)':<12} {'Status':<10}")
            print(f"  {'-'*5} {'-'*6} {'-'*40} {'-'*12} {'-'*12} {'-'*10}")
            
            for i, detail in enumerate(self.tool_call_details, 1):
                tool_name = detail['tool_name']
                iteration = detail.get('iteration', 0)
                elapsed = detail['elapsed_time']
                wns = detail.get('wns')
                error = detail.get('error', False)
                
                # Format WNS column
                wns_str = f"{wns:.3f}" if wns is not None else "-"
                
                # Format status
                status_str = "ERROR" if error else "OK"
                
                print(f"  {i:<5} {iteration:<6} {tool_name:<40} {elapsed:<12.2f} {wns_str:<12} {status_str:<10}")
                
                # If error, show error message on next line
                if error and 'error_message' in detail:
                    print(f"        Error: {detail['error_message'][:80]}")
        
        # Per-call breakdown if debug mode
        if self.debug and self.api_call_details:
            print(f"\nPER-CALL BREAKDOWN:")
            
            # Check if we have cached or reasoning tokens to display
            has_cached = any(detail.get('cached_tokens', 0) > 0 for detail in self.api_call_details)
            has_reasoning = any(detail.get('reasoning_tokens', 0) > 0 for detail in self.api_call_details)
            has_cost = any(detail.get('cost', 0) > 0 for detail in self.api_call_details)
            
            # Build header
            header = f"  {'Call':<6} {'Iter':<6} {'Prompt':<10} {'Completion':<12}"
            if has_cached:
                header += f" {'Cached':<10}"
            if has_reasoning:
                header += f" {'Reasoning':<10}"
            header += f" {'Total':<10}"
            if has_cost:
                header += f" {'Cost':<12}"
            print(header)
            
            # Build separator
            separator = f"  {'-'*6} {'-'*6} {'-'*10} {'-'*12}"
            if has_cached:
                separator += f" {'-'*10}"
            if has_reasoning:
                separator += f" {'-'*10}"
            separator += f" {'-'*10}"
            if has_cost:
                separator += f" {'-'*12}"
            print(separator)
            
            # Print details
            for detail in self.api_call_details:
                line = (f"  {detail['call_number']:<6} {detail['iteration']:<6} "
                       f"{detail['prompt_tokens']:<10,} {detail['completion_tokens']:<12,}")
                if has_cached:
                    line += f" {detail.get('cached_tokens', 0):<10,}"
                if has_reasoning:
                    line += f" {detail.get('reasoning_tokens', 0):<10,}"
                line += f" {detail['total_tokens']:<10,}"
                if has_cost:
                    cost = detail.get('cost', 0)
                    line += f" ${cost:<11.4f}" if cost > 0 else f" {'N/A':<12}"
                print(line)
        
        print(f"\n{'='*70}\n")
        
        # Save detailed report to JSON in run directory
        try:
            report_path = self.run_dir / "token_usage.json"
            self.save_token_usage_report(report_path)
            print(f"Detailed token usage report saved to: {report_path}\n")
        except Exception as e:
            logger.warning(f"Failed to save token usage report: {e}")


async def run_plan_script_mode(
    input_dcp: Path,
    output_dcp: Path,
    script_files: list,
    debug: bool = False,
    run_dir: Optional[Path] = None,
) -> int:
    """DEV ONLY (C13 Step 1). Execute plan scripts against a DCP and report.

    Off the contest path: no LLM, no loop. This exists so `_run_plan_script` --
    including the `_design_gen` bump the ship gate depends on -- is exercised
    against a real Vivado session before C13 Step 2 puts it on the graded path.

    Several scripts run in ONE session on purpose. With a single script the gate
    has no cached verdict to reuse, so a missing `_design_gen` bump would look
    perfectly healthy; only the SECOND attempt can catch it. That bump is the
    highest-risk line in Step 1, so the entry point has to be able to reach it.

    The ship-safety wiring is set up exactly as `optimize()` does it (output
    seeded from the input at t=0, snapshot in run_dir), because the point is to
    test the real promotion path, not a simplified one.
    """
    optimizer = DCPOptimizer(api_key="unused-in-plan-script-mode",
                             debug=debug, run_dir=run_dir)
    try:
        await optimizer.start_servers()
        optimizer.start_time = time.time()
        optimizer._output_dcp_path = output_dcp
        optimizer._input_dcp_path = input_dcp
        optimizer._best_snapshot = Path(optimizer.run_dir) / "maya_best.dcp"
        if not atomic_copy(input_dcp, output_dcp):
            print(f"\n✗ FATAL: could not seed {output_dcp} from the input DCP.\n")
            return 1

        print(f"[PLAN] Opening {input_dcp.name} ...")
        await optimizer._open_checkpoint(input_dcp)   # opens + sources ::opt:: (plan scripts use it)
        optimizer._design_gen += 1

        worst = 0
        for n, script_file in enumerate(script_files, start=1):
            script = script_file.read_text()
            print(f"\n[PLAN] === attempt {n}/{len(script_files)}: {script_file.name} "
                  f"({len(script)} B) ===")
            gen_before = optimizer._design_gen
            attempt = await optimizer._run_plan_script(script, iteration=n)

            print(f"[PLAN]   ok           : {attempt.ok}")
            if attempt.error:
                print(f"[PLAN]   error        : {attempt.error}")
            for p in attempt.fatal_problems:
                print(f"[PLAN]   FATAL        : {p}")
            for p in attempt.advice_problems:
                print(f"[PLAN]   advice       : {p}")
            print(f"[PLAN]   WNS/TNS      : {attempt.wns} / {attempt.tns}")
            print(f"[PLAN]   WHS/WPWS     : {attempt.whs} / {attempt.wpws}")
            print(f"[PLAN]   routed       : {attempt.routed}")
            gate_word = ("PASS" if attempt.gate_ok
                         else ("NOT RUN" if attempt.gate_reason.startswith("not evaluated")
                               else "REJECT"))
            print(f"[PLAN]   ship gate    : {gate_word} — {attempt.gate_reason}")
            print(f"[PLAN]   accepted     : {attempt.accepted}")
            print(f"[PLAN]   execute_s    : {attempt.execute_s:.1f}")
            # Printed so the invalidation is VISIBLE rather than assumed: a gate
            # verdict logged under the same generation as the previous attempt
            # would be a stale verdict about a design that has since changed.
            print(f"[PLAN]   _design_gen  : {gen_before} -> {optimizer._design_gen}")
            if not attempt.ok:
                worst = 1

        print(f"\n[PLAN]   run dir      : {optimizer.run_dir}")
        return worst
    except KeyboardInterrupt:
        print("\n[PLAN] Interrupted by user")
        return 130
    except Exception as e:
        logger.exception(f"plan-script fatal error: {e}")
        print(f"\n[PLAN] Fatal error: {e}")
        return 1
    finally:
        await optimizer.cleanup()


def dry_run_prompt(args) -> int:
    """[pramana] Build the system prompt exactly as _optimize_body does (recipes sourced),
    write it to <out>/system_prompt.<kb>.txt plus a small JSON manifest, and return 0.
    No Vivado, no MCP servers, no LLM."""
    import hashlib
    out = args.dry_run_prompt.resolve()
    out.mkdir(parents=True, exist_ok=True)
    variant = _kb_variant()
    libs = [RECIPE_LIB] + ([(_kb_dir() or Path("/nonexistent")) / "recipes.tcl"]
                           if variant == "pramana" else [])
    missing = [str(p) for p in libs if not p.exists()]
    if missing:
        print(f"Error: Tcl libraries the session would source are missing: {missing}",
              file=sys.stderr)
        return 2
    template, appendix = load_system_prompt_parts(recipes_loaded=True)
    prompt = template.format(temp_dir=out, input_dcp=args.input_dcp.resolve()) + appendix
    path = out / f"system_prompt.{variant}.txt"
    path.write_text(prompt)
    ctx = (_kb_dir() / "context.md").read_text() if variant == "pramana" else ""
    manifest = {
        "kb": variant, "kb_dir": str(_kb_dir()) if _kb_dir() else None,
        "prompt_file": str(path), "chars": len(prompt),
        "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "template_file": "SYSTEM_PROMPT.TXT" if variant == "hand" else "SYSTEM_PROMPT_PROTOCOL.TXT",
        "sourced_tcl": [str(p) for p in libs],
        "contains_pramana_context": bool(ctx) and ctx in prompt,
        "contains_hand_recipe_reference": "::opt:: RECIPE LIBRARY" in prompt,
    }
    (out / f"system_prompt.{variant}.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


async def main():
    # Dev/exploration modes live in maya_dev_modes (off the contest path). Imported
    # here, not at module top, so the contest run never loads them -- and so the
    # from-maya import inside that module resolves against a fully-initialised maya.
    from maya_dev_modes import (
        DEFAULT_STRATEGIES,
        SUPPORTED_STRATEGIES,
        VIVADO_DEFAULT_STRATEGIES,
        VIVADO_SUPPORTED_STRATEGIES,
        run_rapwr_explore,
        run_test_mode,
        run_vivado_explore,
    )
    parser = argparse.ArgumentParser(
        description="FPGA Design Optimization Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dcp_optimizer.py input.dcp
  python dcp_optimizer.py input.dcp --output output.dcp
  python dcp_optimizer.py input.dcp --model anthropic/claude-sonnet-4
  python dcp_optimizer.py input.dcp --debug
  python dcp_optimizer.py fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp --test
  python dcp_optimizer.py fpl26_contest_benchmarks/vexriscv_re-place_2025.1.dcp --test
        """
    )
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        dest="output_dcp",
        help="Output optimized checkpoint (.dcp). Default: <input_name>_optimized-<timestamp>.dcp in same directory as input"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("OPENROUTER_API_KEY", os.environ.get("LLM_API_KEY")),
        help="API key (default: OPENROUTER_API_KEY or LLM_API_KEY env var)"
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help=(
            f"Override the LLM API base URL. "
            f"Override the active provider URL (current PROVIDER='{PROVIDER}', default: {DEFAULT_BASE_URL}). "
            f"Example: --base-url http://localhost:11434/v1"
        )
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"LLM model to use (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (verbose logging, save intermediate checkpoints)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: run without LLM. Cell re-placement flow for VexRiscv (see docs/optimization_example.md)."
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Diagnose-only mode: run the initial analysis (delay split / congestion / QoR / "
             "DONT_TOUCH / utilization / detour / robustness), write the facts to "
             "diagnosis.txt + diagnosis.json, and exit. No LLM, no API key, no design changes."
    )
    parser.add_argument(
        "--raw-reports",
        action="store_true",
        help="Pass the COMPLETE, unparsed context through the whole flow: the initial analysis "
             "dumps full Vivado reports verbatim AND tool outputs in the loop are not truncated."
    )
    parser.add_argument(
        "--time-budget-min",
        type=float,
        default=60.0,
        help="TOTAL wall-clock cap (minutes) per DCP — includes the graceful finalize+report "
             "tail, not just optimization. Enforced by an independent watchdog thread that fires "
             "even mid-tool (place/route/analysis). Optimization stops FINALIZE_RESERVE_S (60 s) "
             "before the cap, leaving the tail to finish inside it; the scored DCP is valid at "
             "every instant regardless. The contest allows 1 hour/DCP, so do not raise this above "
             "60 (default: 60)."
    )
    parser.add_argument(
        "--max-nets",
        type=int,
        default=5,
        help="Maximum number of high fanout nets to optimize in test mode (default: 5)"
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Directory for run outputs (logs, checkpoints). Default: maya_run-<timestamp>-<dcp_stem> in cwd."
    )
    parser.add_argument(
        "--rapwr-explore",
        action="store_true",
        help="RapidWright exploration mode: run configurable RapidWright strategies, re-route in Vivado, report timing improvement (no LLM)."
    )
    parser.add_argument(
        "--rapwr-strategies",
        type=str,
        default=",".join(DEFAULT_STRATEGIES),
        help=(
            f"Comma-separated RapidWright strategies for --rapwr-explore. "
            f"Default: {','.join(DEFAULT_STRATEGIES)}. "
            f"Supported: {', '.join(SUPPORTED_STRATEGIES)}"
        )
    )
    parser.add_argument(
        "--vivado-explore",
        action="store_true",
        help="Vivado exploration mode: run configurable Vivado-only strategies, report timing improvement (no LLM, no RapidWright)."
    )
    parser.add_argument(
        "--plan-script",
        type=Path,
        action="append",
        default=None,
        help="DEV ONLY: validate and execute a Tcl plan script against the DCP, then report the "
             "measured outcome and ship-gate verdict. No LLM, no loop. Off the contest path. "
             "Repeat the flag to run several scripts in ONE Vivado session — which is the only "
             "way to exercise the ship gate's cache invalidation between attempts."
    )
    parser.add_argument(
        "--vivado-strategies",
        type=str,
        default=",".join(VIVADO_DEFAULT_STRATEGIES),
        help=(
            f"Comma-separated Vivado strategies for --vivado-explore. "
            f"Default: {','.join(VIVADO_DEFAULT_STRATEGIES)}. "
            f"Supported: {', '.join(VIVADO_SUPPORTED_STRATEGIES)}"
        )
    )
    # [pramana] knowledge-base variant + provider selection + prompt dry run.
    parser.add_argument("--kb", choices=KB_VARIANTS, default="hand",
                        help="Knowledge in the prompt: none | hand (original) | pramana")
    parser.add_argument("--kb-dir", type=Path, default=None,
                        help="pramana maya bundle (context.md + recipes.tcl), for --kb pramana")
    parser.add_argument("--provider", choices=sorted(_PROVIDER_DEFAULTS), default=None,
                        help="Use this provider's base URL (and its default model unless --model)")
    parser.add_argument("--dry-run-prompt", type=Path, default=None, metavar="OUT_DIR",
                        help="Build the system prompt for --kb, write it to OUT_DIR and exit "
                             "(no Vivado, no LLM)")

    args = parser.parse_args()

    # [pramana] carried in env so maya_dev_modes' re-import of this module agrees.
    os.environ["MAYA_KB"] = args.kb
    if args.kb_dir is not None:
        os.environ["MAYA_KB_DIR"] = str(args.kb_dir.resolve())
    if args.provider:
        args.base_url = args.base_url or _PROVIDER_DEFAULTS[args.provider][0]
        if args.model == DEFAULT_MODEL:
            args.model = _PROVIDER_DEFAULTS[args.provider][1]
    if args.dry_run_prompt is not None:
        sys.exit(dry_run_prompt(args))

    # Validate inputs
    if not args.input_dcp.exists():
        print(f"Error: Input file not found: {args.input_dcp}", file=sys.stderr)
        sys.exit(1)
    
    # Generate default output DCP name if not provided
    if args.output_dcp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        input_stem = args.input_dcp.stem  # Filename without extension
        input_dir = args.input_dcp.parent  # Directory of input file
        args.output_dcp = input_dir / f"{input_stem}_optimized-{timestamp}.dcp"
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Create output directory if needed
    args.output_dcp.parent.mkdir(parents=True, exist_ok=True)
    
    # Diagnose-only mode - run the initial analysis and exit (no LLM, no API key)
    if args.diagnose:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or Path.cwd() / f"maya_diagnose-{timestamp}-{args.input_dcp.stem}"

        print(f"FPGA Design Diagnosis - DIAGNOSE MODE")
        print(f"======================================")
        print(f"Input:   {args.input_dcp.resolve()}")
        print(f"Run dir: {run_dir}")
        print()

        # DCPOptimizer needs an api_key arg, but the LLM client is never called here.
        optimizer = DCPOptimizer(
            api_key=args.api_key or "diagnose-only",
            model=args.model,
            debug=args.debug,
            run_dir=run_dir,
        )
        optimizer.dump_raw_reports = args.raw_reports
        try:
            await optimizer.start_servers()
            # perform_initial_analysis already writes initial_diagnosis.{txt,json} to run_dir.
            await optimizer.perform_initial_analysis(args.input_dcp)
            print(f"\n✓ Diagnosis written to:\n  {run_dir / 'initial_diagnosis.txt'}")
            if (run_dir / "initial_diagnosis.json").exists():
                print(f"  {run_dir / 'initial_diagnosis.json'}")
            sys.exit(0)
        except KeyboardInterrupt:
            print("\nInterrupted by user")
            sys.exit(130)
        except Exception as e:
            logger.exception(f"Diagnosis failed: {e}")
            print(f"\n✗ Diagnosis failed: {e}")
            sys.exit(1)
        finally:
            await optimizer.cleanup()

    # Test mode - run without LLM
    if args.test:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or Path.cwd() / f"maya_run-{timestamp}-{args.input_dcp.stem}"

        print(f"FPGA Design Optimization - TEST MODE")
        print(f"=====================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print(f"Max nets to optimize: {args.max_nets}")
        print()
        
        exit_code = await run_test_mode(
            args.input_dcp,
            args.output_dcp,
            debug=args.debug,
            max_nets=args.max_nets,
            run_dir=run_dir
        )
        sys.exit(exit_code)

    # rapwr_explore mode - run without LLM using RapidWright strategies
    if args.rapwr_explore:
        strategies = [s.strip() for s in args.rapwr_strategies.split(",") if s.strip()]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or Path.cwd() / f"maya_run-{timestamp}-{args.input_dcp.stem}"

        print(f"FPGA Design Optimization - RAPWR EXPLORE MODE")
        print(f"==============================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print(f"Strategies:  {', '.join(strategies)}")
        print()

        exit_code = await run_rapwr_explore(
            args.input_dcp,
            args.output_dcp,
            strategies=strategies,
            debug=args.debug,
            run_dir=run_dir,
        )
        sys.exit(exit_code)

    # vivado_explore mode — Vivado-only, no LLM, no RapidWright
    if args.vivado_explore:
        viv_strategies = [s.strip() for s in args.vivado_strategies.split(",") if s.strip()]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or Path.cwd() / f"maya_run-{timestamp}-{args.input_dcp.stem}"

        print(f"FPGA Design Optimization - VIVADO EXPLORE MODE")
        print(f"===============================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print(f"Strategies:  {', '.join(viv_strategies)}")
        print()

        exit_code = await run_vivado_explore(
            args.input_dcp,
            args.output_dcp,
            strategies=viv_strategies,
            debug=args.debug,
            run_dir=run_dir,
        )
        sys.exit(exit_code)

    # plan-script mode — DEV ONLY, no LLM (C13 Step 1)
    if args.plan_script:
        missing = [p for p in args.plan_script if not p.exists()]
        if missing:
            for p in missing:
                print(f"Error: plan script not found: {p}", file=sys.stderr)
            sys.exit(2)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or Path.cwd() / f"maya_run-{timestamp}-plan-{args.input_dcp.stem}"

        print(f"FPGA Design Optimization - PLAN SCRIPT MODE (dev)")
        print(f"=================================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Scripts:     {', '.join(str(p.resolve()) for p in args.plan_script)}")
        print(f"Run dir:     {run_dir}")
        print()

        exit_code = await run_plan_script_mode(
            args.input_dcp,
            args.output_dcp,
            args.plan_script,
            debug=args.debug,
            run_dir=run_dir,
        )
        sys.exit(exit_code)

    # Normal mode - requires API key and LLM
    if not args.api_key:
        print("Error: OpenRouter API key required. Set OPENROUTER_API_KEY or use --api-key", file=sys.stderr)
        print("       Use --test flag to run in test mode without LLM", file=sys.stderr)
        sys.exit(1)
    
    if OpenAI is None:
        print("Error: openai package not installed. Run: pip install openai", file=sys.stderr)
        sys.exit(1)
    
    # Create run directory with timestamp + DCP stem to avoid collisions in batch runs.
    # Honor --run-dir so batch runs (run_benchmarks.py) nest the run dir INSIDE the
    # benchmark_logs directory instead of scattering them in cwd.
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = args.run_dir or Path.cwd() / f"maya_run-{timestamp}-{args.input_dcp.stem}"
    
    print(f"FPGA Design Optimization Agent")
    print(f"================================")
    effective_base_url = args.base_url or DEFAULT_BASE_URL
    print(f"Input:       {args.input_dcp.resolve()}")
    print(f"Output:      {args.output_dcp.resolve()}")
    print(f"Run dir:     {run_dir}")
    print(f"Model:       {args.model}")
    print(f"API URL:     {effective_base_url}")
    print()

    optimizer = DCPOptimizer(
        api_key=args.api_key,
        model=args.model,
        debug=args.debug,
        run_dir=run_dir,
        base_url=args.base_url,
    )
    optimizer.dump_raw_reports = args.raw_reports
    optimizer.time_budget_seconds = args.time_budget_min * 60.0

    try:
        await optimizer.start_servers()
        success = await optimizer.optimize(args.input_dcp, args.output_dcp)
        
        if success:
            print("\n✓ Optimization completed successfully")
            print(f"\nOutput files:")
            print(f"  Optimized DCP: {args.output_dcp}")
            print(f"  Run directory: {run_dir}")
            sys.exit(0)
        else:
            print("\n✗ Optimization did not complete successfully")
            print(f"\nRun directory: {run_dir}")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        print(f"Run directory: {run_dir}")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        print(f"Run directory: {run_dir}")
        sys.exit(1)
    finally:
        await optimizer.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

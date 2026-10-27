"""Planner-script validation and the attempt record — PURE. No Vivado, no I/O.

Text in, verdicts out; the same contract `vivado_parse.py` holds for report parsing.
Nothing here executes a script or edits one. `_run_plan_script` in maya.py is the
only place that runs what this module approves.

WHY DETECTION AND NOT CORRECTION
    The harness NEVER edits what the planner emits. Rewriting a model's script
    hides the mistake from the model, so it makes the same one next turn, and it
    makes the run's log a record of what maya wrote rather than what the planner
    decided. Detect, report, re-ask.

TWO SEVERITIES, AND THE ASYMMETRY IS THE POINT
    "fatal"  -- would break the HARNESS itself (kill the Vivado session, or corrupt
                the checkpoint/revert bookkeeping every ship-safety guarantee rests
                on). Not executed; the planner is told why and corrects next turn.
    "advice" -- Vivado knowledge the planner can simply be told. Executed ANYWAY.
                Our whitelists have already been proven wrong once, so Vivado, not
                maya, is the authority on legality. Worst case is one aborted
                script, and that error feeds back to the planner.

    Mid-loop a false reject costs an iteration; at the ship boundary an unverified
    pass costs the benchmark. This validator sits mid-loop, so it is lenient by
    design. The ship gate, which is not, lives in maya.py.

PROVENANCE OF THE VOCABULARY
    Ported 2026-08-06 from ref/ruthwik/src/vivado_reports/vivado_vocab.py, and
    re-verified against ref/ruthwik/src/tests/fixtures/directives_probe_2025.2_xcvu3p.json
    (51/51 cases, xcvu3p-ffvc1517-2-e, Vivado 2025.2, post-route) before landing:
    35 whitelisted values, every probe-covered value agreed, no entry uncovered.

    A probe and not `help -long`, because Vivado accepts a directive at PARSE time
    and rejects it only after DRC:
        INFO:  [Vivado_Tcl 4-2302] The placer was invoked with the 'X' directive.
        ERROR: [Constraints 18-641] Directive 'X' is not a recognized directive.
    A parse-time check therefore records a FALSE PASS -- which is how
    AggressiveExplore, which aborts place_design, once sat on a whitelist that
    claimed to have probed it.

VERSION CAVEAT
    Eval is Vivado 2025.1; the probe is 2025.2. Nothing here is promoted to a
    `fatal` severity on 2025.2 evidence alone -- every directive/option rule below
    is `advice`, and only the harness rules are fatal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# ── vocabulary ───────────────────────────────────────────────────────────────
# place_design: 15 of 16 probed values ran. AggressiveExplore is the ONE that
# aborts, and it aborts STANDALONE -- so it is the directive, not a bad flag
# combination. It is valid for route_design and phys_opt_design, which is exactly
# the trap a single flat directive list walks into.
PLACE_DIRECTIVES = frozenset({
    "default", "explore",
    "earlyblockplacement", "wldrivenblockplacement",
    "extranetdelay_high", "extranetdelay_low", "extrapostplacementopt",
    "extratimingopt", "ssi_spreadlogic_high", "ssi_spreadlogic_low",
    "altspreadlogic_high", "altspreadlogic_medium", "altspreadlogic_low",
    "runtimeoptimized", "quick",
})

# route_design: all 10 probed values ran.
ROUTE_DIRECTIVES = frozenset({
    "default", "explore", "aggressiveexplore", "notimingrelaxation",
    "moreglobaliterations", "higherdelaycost", "advancedskewmodeling",
    "alternateclbrouting", "runtimeoptimized", "quick",
})

# phys_opt_design: all 10 ran. The primary post-route lever.
PHYS_OPT_DIRECTIVES = frozenset({
    "default", "explore", "aggressiveexplore",
    "explorewithholdfix", "explorewithaggressiveholdfix",
    "alternatereplication", "aggressivefanoutopt",
    "addretime", "alternateflowwithretiming", "runtimeoptimized",
})

DIRECTIVES_BY_COMMAND = {
    "place_design": PLACE_DIRECTIVES,
    "route_design": ROUTE_DIRECTIVES,
    "phys_opt_design": PHYS_OPT_DIRECTIVES,
}

# All six abort post-route with the SAME [Vivado_Tcl 4-265] message. An abort here
# costs the iteration AND every later stage in the same script.
POST_ROUTE_UNSUPPORTED_OPTIONS = (
    "-fanout_opt", "-dsp_register_opt", "-bram_register_opt",
    "-uram_register_opt", "-shift_register_opt",
    "-force_replication_on_nets",
)

# Probed as working post-route on this part/version.
POST_ROUTE_SUPPORTED_OPTIONS = (
    "-placement_opt", "-routing_opt", "-critical_cell_opt",
    "-restruct_opt", "-critical_pin_opt",
)

# Breaks the HARNESS, not the design -- the only fatal class. exit/quit/start_gui
# kill the session mid-run; open/write_checkpoint/close_design corrupt the
# checkpoint and revert bookkeeping that all ship-safety work depends on.
#
# Matched as the LEADING COMMAND TOKEN, never as a substring. A substring scan
# silently flags any line merely CONTAINING one of these words -- `set c
# [get_cells -hier *exit*]`, or a net named fifo_exit_reg.
HARNESS_FORBIDDEN = (
    "exit", "quit", "start_gui", "open_checkpoint", "write_checkpoint",
    "close_design", "write_edif", "write_verilog", "current_project",
)
_HARNESS_RE = re.compile(rf"^\s*({'|'.join(HARNESS_FORBIDDEN)})\b", re.IGNORECASE)

# Commands that CHANGE THE DESIGN. The whole script is sourced under ONE catch, so
# an error in any of these discards every LATER stage too.
_MUTATORS = (
    "place_design", "route_design", "phys_opt_design", "opt_design",
    "create_pblock", "resize_pblock", "add_cells_to_pblock", "delete_pblock",
    "::opt::move_cell", "::opt::swap_cells", "::opt::pack_lut_ff",
    "::opt::swap_lut_pins", "::opt::reroute_nets", "::opt::reroute_worst_nets",
    "::opt::move_ff_fast_bel", "::opt::lock_lut_fast_pins",
    "::opt::unlock_lut_pins", "::opt::lock_cells", "::opt::unlock_cells",
    "::opt::lock_region", "::opt::restore_placement",
    "::opt::freeze_routes", "::opt::unfreeze_routes",
)
# [pramana] + ::pramana::recipe_* (discovered recipes from a pramana bundle; not self-guarding).
_MUTATOR_RE = re.compile(
    rf"^({'|'.join(re.escape(m) for m in _MUTATORS)}|::pramana::recipe_\w+)\b", re.IGNORECASE)

# Already self-guarding: both checkpoint, run and revert on regression, so a
# failure inside one costs its own stage and nothing after it.
# [pramana] + ::pramana::keep_if_better and ::pramana::policy_* (chains of keep_if_better).
_GUARDS = ("::opt::try_keep_if_better", "::opt::safe_phys_opt")
_GUARD_RE = re.compile(
    rf"(^|\s)({'|'.join(re.escape(g) for g in _GUARDS)}"
    rf"|::pramana::keep_if_better|::pramana::policy_\w+)\b", re.IGNORECASE)


# render_command_contract() lived here — a prompt block that enumerated the COMPLETE valid
# directive set per command plus the forbidden tokens, generated from the constants above.
# REVERTED 2026-08-10: the bounded "here is every valid directive" menu made the model treat
# it as a checklist — it cycled the listed directives in a handful of attempts, declared
# "every lever class exhausted", and the done-stop honoured that early (30-47 min of budget
# left unused vs the pre-contract run). The constants stay (validate_script enforces them);
# the prompt just no longer enumerates them to the model.


def script_mutates(text: str) -> bool:
    """Does this Tcl CHANGE THE DESIGN? True if any line's leading token is a design
    mutator, or any line invokes a keep-best/safe guard (which wraps a mutation).

    The router for the script-first loop: a mutating run_tcl goes through the
    validated plan-script path (_run_plan_script -- validate, wrap, execute, gate,
    bank, revert), while a pure report/query run_tcl keeps the fast raw path. A
    query-only script (report_*, get_*, puts, set, get_cells ...) returns False.
    Pure: text in, bool out.
    """
    if not text:
        return False
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if _MUTATOR_RE.match(s) or _GUARD_RE.search(ln):
            return True
    return False


_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def extract_tcl_script(text: str) -> Optional[str]:
    """Pull the Tcl script out of a model's plain-text reply.

    The loop is script-first with NO tools: the model answers with one fenced code
    block and that block IS the mutation surface. This finds it. Prefer an explicitly
    ```tcl-tagged block; else an untagged ``` block; else any fenced block. Takes the
    LAST match of the chosen kind, because a reasoning model may show a draft before
    its final script. Returns None when the reply has no code block (prose only / a
    completion claim) -- the caller treats that as "no action this turn".

    Pure: text in, script (or None) out. The returned string is fed verbatim to the
    same path a run_tcl tool-call used to take (script_mutates routes it).
    """
    blocks = _FENCE_RE.findall(text or "")
    if not blocks:
        return None
    for prefer in (("tcl",), ("",), None):
        chosen = [body for lang, body in blocks
                  if prefer is None or lang.lower() in prefer]
        if chosen:
            return chosen[-1].strip() or None
    return None


def normalize_script(text: str) -> str:
    """Canonical form of a script for identity comparison: drop comment lines and
    blank lines, strip each line, collapse internal whitespace. Two scripts that
    differ only in comments or spacing normalize equal -- so a model that re-emits
    the same mutation with a fresh comment is still recognised as a repeat.

    Vivado is deterministic here, so an identical mutation run from the same banked
    design produces the same result; this is the key the harness dedups on. Pure.
    """
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(re.sub(r"\s+", " ", s))
    return "\n".join(out)


# ── records ──────────────────────────────────────────────────────────────────
@dataclass
class ScriptProblem:
    """One thing wrong with a planner script. DETECTION ONLY.

    `no_script` covers the turn producing no script at all; it has line_no=0 and an
    empty line, since there is no line to point at. Severity is what the caller
    acts on and means literally "do not execute", which fits both cases.
    """
    line_no: int
    line: str
    kind: str        # post_place_only_flag | unknown_directive | report_cmd
                     # | harness_forbidden | no_script | unwrapped_mutation
    severity: str    # "advice" (execute anyway) | "fatal" (do not execute)
    reason: str      # planner-readable; names the failure it would cause

    def render(self) -> str:
        if not self.line_no:
            return self.reason
        return f"line {self.line_no}: {self.reason}  [{self.line.strip()}]"


@dataclass
class PlanAttempt:
    """One planner script, executed and measured. Appended to run_dir/attempts.jsonl.

    This record is MAYA'S, defined in root, owned by root. It is deliberately not
    the sweep's corpus row: the submission must stand alone, and the two have
    opposite pressures (breadth/resumability vs one 3600 s shot that must never
    ship an invalid DCP).

    analyze_s / plan_s / execute_s exist so the analyze-cadence question is decided
    by a measured column rather than a guess -- analyze runs before every LLM call,
    and whether that is affordable on the largest designs has never been measured
    at the contest's 3600 s cap.
    """
    iteration: int
    label: str
    script_path: str
    ok: bool                      # the script ran to completion under Tcl catch
    error: Optional[str]          # Tcl error text when ok=False
    fatal_problems: list          # rendered ScriptProblems that BLOCKED execution
    advice_problems: list         # rendered ScriptProblems reported but executed
    wns: Optional[float] = None
    tns: Optional[float] = None
    whs: Optional[float] = None
    wpws: Optional[float] = None
    routed: Optional[bool] = None
    route_errors: Optional[int] = None
    gate_ok: bool = False
    gate_reason: str = ""
    accepted: bool = False
    duplicate: bool = False       # short-circuited: identical script already tried
    analyze_s: float = 0.0
    plan_s: float = 0.0
    execute_s: float = 0.0
    elapsed_s: float = 0.0
    analyze_kind: str = ""        # "full" | "digest" | "skipped:<guard>" | ""

    def to_json(self) -> dict:
        return asdict(self)


# ── validation ───────────────────────────────────────────────────────────────
def _brace_depth_at_line_start(lines: list[str]) -> list[int]:
    """Cumulative Tcl brace depth entering each line, ignoring comment lines.

    Approximate on purpose: this feeds an `advice` diagnostic, so a miscount on
    pathological quoting costs a spurious hint, never a rejected script. Braces
    inside a comment are skipped because Tcl does not read them either once the
    line is a comment; braces inside a quoted string are NOT tracked, which is the
    one known imprecision.
    """
    depths, depth = [], 0
    for ln in lines:
        depths.append(depth)
        s = ln.strip()
        if s.startswith("#"):
            continue
        depth += ln.count("{") - ln.count("}")
    return depths


def _unwrapped_mutations(lines: list[str]) -> list[ScriptProblem]:
    """Flag design-changing stages running OUTSIDE a self-guarding wrapper.

    A separate pass because the decision needs the whole script: whether a mutation
    is top-level depends on the brace depth accumulated before it, and the final
    route_design exemption depends on what comes AFTER it.

    Severity is `advice`, never fatal -- an unwrapped script is still worth running,
    and the caller reverts a regression regardless. The point is to teach the
    planner, not to gate it.
    """
    depths = _brace_depth_at_line_start(lines)
    found: list[tuple[int, str, str]] = []
    for i, ln in enumerate(lines, start=1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if depths[i - 1] > 0:
            continue            # nested inside SOME block -- a wrapper or a loop
        if _GUARD_RE.search(s):
            continue            # the guarded call itself, and its one-line body
        m = _MUTATOR_RE.match(s)
        if m:
            found.append((i, ln, m.group(1)))
    if not found:
        return []
    # The final route_design is DELIBERATELY unwrapped: the accept gate requires a
    # fully-routed design, so it must run last and unconditionally. Wrapping it
    # would let a revert ship an unrouted design.
    if found[-1][2].lower() == "route_design":
        found.pop()
    return [
        ScriptProblem(
            i, ln, "unwrapped_mutation", "advice",
            f"'{cmd}' changes the design at the top level, outside "
            f"::opt::try_keep_if_better. The whole script is sourced under ONE "
            f"catch, so if this stage errors every LATER stage is discarded too. "
            f"Wrap it: ::opt::try_keep_if_better {{{cmd} ...}} -- then a failure "
            f"costs this stage only. The FINAL route_design is the one exception "
            f"and must stay unwrapped.")
        for i, ln, cmd in found
    ]


def validate_script(script: str) -> list[ScriptProblem]:
    """Inspect a planner script and REPORT what is wrong with it.

    Pure: returns problems, never a modified script. See the module docstring for
    the two severities and why the asymmetry is deliberate.
    """
    if not script or not script.strip():
        return [ScriptProblem(
            0, "", "no_script", "fatal",
            "the turn produced no Tcl script to run. Emit one script; the harness "
            "executes it and reports back what it measured.")]

    problems: list[ScriptProblem] = []
    lines = script.splitlines()
    for i, ln in enumerate(lines, start=1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()

        m = _HARNESS_RE.match(ln)
        if m:
            problems.append(ScriptProblem(
                i, ln, "harness_forbidden", "fatal",
                f"'{m.group(1)}' is reserved by the harness -- it would end the "
                f"Vivado session or corrupt the checkpoint bookkeeping. The harness "
                f"opens, saves and reverts the design for you; never do it in a "
                f"script."))
            continue

        # Reporting is a Python-driven phase that runs after every attempt, so a
        # report here measures nothing new and costs runtime.
        if re.match(r"report_\w+", low):
            problems.append(ScriptProblem(
                i, ln, "report_cmd", "advice",
                "the harness runs the full report bundle after every attempt; a "
                "report command here measures nothing extra and burns runtime."))
            continue

        # POST-PLACE ONLY. Post-route Vivado answers [Vivado_Tcl 4-265] and ABORTS
        # THE WHOLE SCRIPT, so every later stage is lost too.
        if low.startswith("phys_opt_design"):
            for bad in POST_ROUTE_UNSUPPORTED_OPTIONS:
                if re.search(rf"{re.escape(bad)}\b", low):
                    problems.append(ScriptProblem(
                        i, ln, "post_place_only_flag", "advice",
                        f"'{bad}' is POST-PLACE ONLY. On this routed design Vivado "
                        f"answers [Vivado_Tcl 4-265] and ABORTS THE WHOLE SCRIPT, so "
                        f"every later stage is lost. For fanout work post-route use "
                        f"phys_opt_design -directive AggressiveFanoutOpt."))

        # Directives are per-command: AggressiveExplore is valid for route_design
        # and phys_opt_design and aborts place_design.
        for cmd, allowed in DIRECTIVES_BY_COMMAND.items():
            if not re.match(rf"{cmd}\b", low):
                continue
            d = re.search(r"-directive\s+(\w+)", s)
            if d and d.group(1).lower() not in allowed:
                problems.append(ScriptProblem(
                    i, ln, "unknown_directive", "advice",
                    f"'{d.group(1)}' is not a {cmd} directive we know of; if Vivado "
                    f"agrees it answers [Constraints 18-641] and aborts the script. "
                    f"Known {cmd} directives: {', '.join(sorted(allowed))}."))

    problems.extend(_unwrapped_mutations(lines))
    return problems


def fatal_problems(problems: list[ScriptProblem]) -> list[ScriptProblem]:
    return [p for p in problems if p.severity == "fatal"]


def advice_problems(problems: list[ScriptProblem]) -> list[ScriptProblem]:
    return [p for p in problems if p.severity == "advice"]


def label_script(script: str) -> str:
    """A short strategy label derived from a Tcl script, for the attempt record."""
    low = script.lower()
    m = re.search(r"phys_opt_design\s+-directive\s+(\w+)", script)
    if m:
        return f"phys_opt-{m.group(1)}"
    # maya no longer builds pblock scripts and no longer teaches them, but a model
    # wrote one unprompted once. If it does that again the attempt should be NAMED,
    # not filed as "custom" -- an unlabelled pblock attempt is how the lever hid.
    if "create_pblock" in low or "resize_pblock" in low:
        return "pblock-replace"
    m = re.search(r"place_design\s+-directive\s+(\w+)", script)
    if m:
        return f"place-{m.group(1)}"
    if "phys_opt_design" in low:
        return "phys_opt-default"
    if "route_design" in low:
        return "route"
    return "custom"


def wrap_for_source(script_path: Path) -> str:
    """The one short Tcl command that runs a plan script.

    NEVER send a script inline: run_tcl does proc.sendline(command) onto a
    canonical-mode pty, where anything past 4096 bytes is silently discarded,
    leaving unterminated Tcl and a hung session. Writing the script to a file and
    sourcing it keeps the issued command a fixed ~120 bytes no matter how long the
    plan is. This mirrors the ::opt:: loader maya already uses.

    Failure is detected by the SENTINEL, not by scanning output for the word
    "ERROR" -- phrase-matching missed `wrong # args` and voided 8 of 14 iterations
    in an earlier tree, and it also fires on errors a `catch` already handled.
    """
    return (f"if {{[catch {{source {{{script_path}}}}} _e]}} "
            f"{{puts \"MAYA_SCRIPT_ERROR: $_e\"}} else {{puts \"MAYA_SCRIPT_OK\"}}")


SCRIPT_OK_SENTINEL = "MAYA_SCRIPT_OK"
SCRIPT_ERROR_SENTINEL = "MAYA_SCRIPT_ERROR:"


def parse_script_result(output: str) -> tuple[bool, Optional[str]]:
    """(ok, error_text) from a wrapped script's Vivado output.

    Neither sentinel present means the session died or the output was truncated
    before the sentinel -- reported as a failure with that stated as the reason,
    never as a pass. An unverified pass here would promote a design nobody measured.
    """
    if SCRIPT_ERROR_SENTINEL in output:
        tail = output.split(SCRIPT_ERROR_SENTINEL, 1)[1].strip()
        return False, (tail.splitlines() or [""])[0].strip() or "unspecified Tcl error"
    if SCRIPT_OK_SENTINEL in output:
        return True, None
    return False, ("neither sentinel found in Vivado's output -- the session may "
                   "have died or the output was truncated")

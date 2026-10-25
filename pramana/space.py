"""S0: the full search space, parsed from Vivado's own `help <command>` output.

The spec is a generator, not a list: each command has directives (pick one), options (typed:
switch / choice / numeric levels / multi-choice), and legality rules. Points (steps) are built
from it; chains are sequences of steps.

Sources of truth:
  specs/vivado_<ver>/help_<command>.txt   - Vivado's help text (parsed here)
  OVERLAY below                           - what the help text can't express: option roles,
                                            numeric levels, which options combine with a
                                            directive, legacy directives to probe.
Every overlay entry says why it exists.
"""
from __future__ import annotations

import itertools
import json
import math
import random
import re
from pathlib import Path

from .config import REPO

COMMANDS = {"opt": "opt_design", "place": "place_design",
            "phys_opt": "phys_opt_design", "route": "route_design"}

# ---------------------------------------------------------------------------- overlay
# Options that are not optimisation levers: flow plumbing, reporting, targeting arguments,
# or other device families. Excluded from the space with the reason recorded.
NOT_LEVERS = {
    "-quiet": "message control", "-verbose": "message control", "-debug_log": "logging",
    "-timing_summary": "reporting only", "-ultrathreads": "runtime only, changes nothing else",
    "-release_memory": "memory housekeeping", "-eco": "ECO mode for added cells only",
    "-unplace": "flow plumbing (pramana inserts it)", "-unroute": "flow plumbing",
    "-nets": "targeting argument (L3)", "-pins": "targeting argument (L3)",
    "-physical_nets": "targeting argument", "-delay": "needs -nets/-pins",
    "-auto_delay": "needs -nets/-pins", "-max_delay": "needs -pins", "-min_delay": "needs -pins",
    "-finalize": "incremental-flow plumbing", "-property_opt_only": "property-driven only",
    "-path_groups": "targeting argument", "-force_replication_on_nets": "targeting argument (L3)",
    "-srl_remap_modes": "structured argument; deferred to L3",
    "-no_noc_opt": "Versal NoC only", "-clock_vtree_type": "Versal SSIT only",
    "-mbufg_opt": "Versal MBUFG only",
}
# Options that only act on multi-SLR (SSI) devices.
SSI_ONLY = {"-slr_crossing_opt", "-sll_reg_hold_fix", "-sll_align_opt"}
# Numeric options: the levels searched (Vivado gives no range in help; values from UG904).
NUMERIC_LEVELS = {"-hier_fanout_limit": [64, 256, 512, 1024]}
# Choice options with documented values.
CHOICES = {"-net_delay_weight": ["low", "medium", "high"]}
# Which options may be combined with -directive (from each command's help 'Note/Important').
DIRECTIVE_COMBINABLE = {
    "opt": set(), "phys_opt": set(),
    "place": {"-no_psip", "-net_delay_weight", "-subdirective"},
    "route": {"-tns_cleanup", "-preserve"},
}
# Directives the help text doesn't list but UG904 documents for UltraScale+ placement.
# They must pass the capability probe before use.
LEGACY_DIRECTIVES = {
    "place": ["WLDrivenBlockPlacement", "EarlyBlockPlacement", "ExtraNetDelay_high",
              "ExtraNetDelay_low", "AltSpreadLogic_high", "AltSpreadLogic_medium",
              "AltSpreadLogic_low", "ExtraPostPlacementOpt", "ExtraTimingOpt",
              "AltWLDrivenPlacement"],
}
# Directives excluded and why.
EXCLUDED_DIRECTIVES = {"RQS": "needs a report_qor_suggestions file; deferred"}
SSI_DIRECTIVE_PREFIX = "SSI_"

# Pseudo-options pramana adds to a step (not Vivado flags).
#   route +ripup : unroute everything first, then route (vs incremental re-route)
PSEUDO_OPTIONS = {"route": {"+ripup": {"type": "switch",
                                       "why": "full rip-up vs incremental re-route"}}}


# ---------------------------------------------------------------------------- parsing
def _syntax_flags(text: str) -> dict[str, str]:
    m = re.search(r"Syntax:\s*\n(.*?)\n\s*\nUsage:", text, re.S)
    block = m.group(1) if m else ""
    flags = {}
    for flag, arg in re.findall(r"(-\w+)(?:\s+<(\w+)>)?", block):
        if flag.startswith("-") and not re.match(r"-\d", flag):
            flags.setdefault(flag, arg or "")
    flags.pop("-directive", None)
    return flags


def _arg_section(text: str, flag: str) -> str:
    """Text of one argument's description in the 'Arguments:' section."""
    args = text.split("Arguments:", 1)[-1]
    m = re.search(rf"\n  {re.escape(flag)}\b.*?(?=\n\n  -\w|\nExamples:|\Z)", args, re.S)
    return m.group(0) if m else ""


def _bullets(section: str) -> list[str]:
    return re.findall(r"^\s+\*\s+([A-Za-z_][\w.]*)", section, re.M)


def _option_help(text: str, flag: str) -> str:
    m = re.search(rf"\[{re.escape(flag)}\]\s+(.*?)(?=\n\s*\[|\n\s*-\w+\s|\n\nDescription|\Z)",
                  text, re.S)
    return " ".join(m.group(1).split()) if m else ""


def parse_help(version: str) -> dict:
    d = REPO / "specs" / f"vivado_{version}"
    spec: dict = {"vivado_version": version, "commands": {}}
    for short, cmd in COMMANDS.items():
        text = (d / f"help_{cmd}.txt").read_text()
        directives = [b for b in _bullets(_arg_section(text, "-directive"))]
        flags = _syntax_flags(text)
        options, excluded = {}, {}
        for flag, arg in flags.items():
            if flag in NOT_LEVERS:
                excluded[flag] = NOT_LEVERS[flag]
                continue
            o = {"help": _option_help(text, flag)}
            if flag in SSI_ONLY:
                o["ssi_only"] = True
            if flag == "-subdirective":
                sec = " ".join(_arg_section(text, "-subdirective").split())
                vals = []
                for name, phases, allowed in re.findall(
                        r"\* (\w+): Supported phases? (?:is|are) ([\w, ]+?)\.\s*(Allowed value is low)?",
                        sec):
                    for ph in re.split(r",\s*(?:and\s+)?|\s+and\s+", phases):
                        ph = {"Gplace": "GPlace", "Dplace": "DPlace"}.get(ph.strip(), ph.strip())
                        if not ph:
                            continue
                        if allowed:
                            vals += [f"{ph}.{name}.{lv}" for lv in ("low", "med", "high")]
                        else:
                            vals.append(f"{ph}.{name}")
                o.update(type="multi", values=vals, max_pick=2)
            elif flag == "-psip_options":
                o.update(type="multi", values=_bullets(_arg_section(text, flag)), max_pick=3)
            elif flag in NUMERIC_LEVELS:
                o.update(type="numeric", values=NUMERIC_LEVELS[flag])
            elif flag in CHOICES:
                o.update(type="choice", values=CHOICES[flag])
            elif arg:
                excluded[flag] = f"takes <{arg}> with no documented levels"
                continue
            else:
                o["type"] = "switch"
            options[flag] = o
        for k, v in PSEUDO_OPTIONS.get(short, {}).items():
            options[k] = dict(v)
        dirs = {}
        for dname in directives:
            if dname in EXCLUDED_DIRECTIVES:
                continue
            dirs[dname] = {"source": "help"}
        for dname in LEGACY_DIRECTIVES.get(short, []):
            dirs.setdefault(dname, {"source": "UG904 legacy", "probe_required": True})
        spec["commands"][short] = {
            "vivado_command": cmd,
            "directives": dirs,
            "options": options,
            "excluded_options": excluded,
            "excluded_directives": {k: v for k, v in EXCLUDED_DIRECTIVES.items() if k in directives},
            "directive_combinable": sorted(DIRECTIVE_COMBINABLE[short]),
        }
    return spec


def apply_device(spec: dict, n_slr: int) -> dict:
    """Drop SSI-only options and directives on single-SLR parts."""
    if n_slr > 1:
        return spec
    for c in spec["commands"].values():
        for k in [k for k, o in c["options"].items() if o.get("ssi_only")]:
            c["excluded_options"][k] = "SSI-only; part has one SLR"
            del c["options"][k]
        for k in [k for k in c["directives"] if k.startswith(SSI_DIRECTIVE_PREFIX)]:
            del c["directives"][k]
    spec["n_slr"] = n_slr
    return spec


def apply_probe(spec: dict, probe: dict) -> dict:
    """Remove directives/values the capability probe found invalid on this part."""
    for short, c in spec["commands"].items():
        for d in probe.get(short, {}).get("invalid_directives", []):
            c["directives"].pop(d, None)
        for flag, bad in probe.get(short, {}).get("invalid_values", {}).items():
            o = c["options"].get(flag)
            if o and "values" in o:
                o["values"] = [v for v in o["values"] if v not in bad]
        for flag in probe.get(short, {}).get("invalid_options", []):
            if flag in c["options"]:
                c["excluded_options"][flag] = "failed capability probe"
                del c["options"][flag]
        for d in c["directives"].values():
            d.pop("probe_required", None)
    spec["probed"] = True
    return spec


# ---------------------------------------------------------------------------- size
def _opt_states(o: dict) -> int:
    t = o["type"]
    if t == "switch":
        return 2
    if t in ("choice", "numeric"):
        return len(o["values"]) + 1
    if t == "multi":
        n, k = len(o["values"]), o.get("max_pick", len(o["values"]))
        return sum(math.comb(n, i) for i in range(k + 1))
    return 1


def command_size(c: dict) -> int:
    """Number of distinct single steps for one command."""
    opts = c["options"]
    comb = set(c["directive_combinable"])
    pseudo = [o for k, o in opts.items() if k.startswith("+")]
    pseudo_n = math.prod(_opt_states(o) for o in pseudo) if pseudo else 1
    free = [o for k, o in opts.items() if not k.startswith("+")]
    no_dir = math.prod(_opt_states(o) for o in free)        # option sets, no directive
    with_dir = len(c["directives"]) * math.prod(
        _opt_states(o) for k, o in opts.items() if k in comb)
    return (no_dir + with_dir) * pseudo_n


def space_size(spec: dict, max_chain: int = 3) -> dict:
    per = {k: command_size(c) for k, c in spec["commands"].items()}
    singles = sum(per.values())
    chains = sum(singles ** n for n in range(1, max_chain + 1))
    return {"per_command": per, "single_steps": singles,
            f"chains_up_to_{max_chain}": chains,
            "log10_chains": round(math.log10(chains), 2)}


# ---------------------------------------------------------------------------- steps
def step_key(step: dict) -> str:
    """Canonical text of a step, e.g. 'phys_opt[-rewire,-critical_cell_opt]' or 'route:Explore'."""
    s = step["cmd"]
    if step.get("directive"):
        s += ":" + step["directive"]
    opts = step.get("opts") or {}
    if opts:
        parts = []
        for k in sorted(opts):
            v = opts[k]
            if v is True:
                parts.append(k)
            elif isinstance(v, list):
                parts.append(f"{k}={'+'.join(sorted(v))}")
            else:
                parts.append(f"{k}={v}")
        s += "[" + ",".join(parts) + "]"
    return s


def chain_key(chain: list[dict]) -> str:
    return " > ".join(step_key(s) for s in chain)


def step_factors(step: dict) -> list[str]:
    """The factor values a step uses (the units the bandit and the pruner reason about)."""
    f = [f"{step['cmd']}"]
    if step.get("directive"):
        f.append(f"{step['cmd']}:directive={step['directive']}")
    for k, v in (step.get("opts") or {}).items():
        if isinstance(v, list):
            f += [f"{step['cmd']}:{k}={x}" for x in v]
        elif v is True:
            f.append(f"{step['cmd']}:{k}")
        else:
            f.append(f"{step['cmd']}:{k}={v}")
    return f


def all_factors(spec: dict) -> list[str]:
    out = []
    for short, c in spec["commands"].items():
        out.append(short)
        out += [f"{short}:directive={d}" for d in c["directives"]]
        for k, o in c["options"].items():
            if o["type"] == "switch":
                out.append(f"{short}:{k}")
            else:
                out += [f"{short}:{k}={v}" for v in o["values"]]
    return out


def legal(step: dict, spec: dict) -> bool:
    c = spec["commands"].get(step["cmd"])
    if c is None:
        return False
    if step.get("directive") and step["directive"] not in c["directives"]:
        return False
    opts = step.get("opts") or {}
    for k, v in opts.items():
        o = c["options"].get(k)
        if o is None:
            return False
        if o["type"] == "switch" and v is not True:
            return False
        if o["type"] in ("choice", "numeric") and v not in o["values"]:
            return False
        if o["type"] == "multi":
            if not isinstance(v, list) or not v or any(x not in o["values"] for x in v):
                return False
            if len(v) > o.get("max_pick", len(o["values"])):
                return False
    if step.get("directive"):
        comb = set(c["directive_combinable"])
        if any(k not in comb and not k.startswith("+") for k in opts):
            return False
    elif step["cmd"] in ("opt", "phys_opt") and not [k for k in opts if not k.startswith("+")]:
        # no directive and no option = Default directive; represent it explicitly
        return False
    return True


def enumerate_simple_steps(spec: dict, cmd_filter=None) -> list[dict]:
    """Every directive alone and every option alone (each value) for each command.
    This is the 'single factor' slice of the space used for round 1 and the shared floor."""
    steps = []
    for short, c in spec["commands"].items():
        if cmd_filter and short not in cmd_filter:
            continue
        for d in c["directives"]:
            steps.append({"cmd": short, "directive": d})
        for k, o in c["options"].items():
            if k.startswith("+"):
                continue
            if o["type"] == "switch":
                st = {"cmd": short, "opts": {k: True}}
            else:
                for v in o["values"]:
                    st = {"cmd": short, "opts": {k: [v] if o["type"] == "multi" else v}}
                    if short in ("place", "route") or legal(st, spec):
                        steps.append(st)
                continue
            steps.append(st)
    return [s for s in steps if legal(s, spec)]


def random_step(spec: dict, rng: random.Random, allowed: dict | None = None) -> dict | None:
    """Sample a random legal step, optionally restricted to an allowed (pruned) subspace:
    allowed = {cmd: {"directives": [...], "options": {flag: [values] or True}}}."""
    cmds = list(allowed) if allowed else list(spec["commands"])
    if not cmds:
        return None
    for _ in range(50):
        short = rng.choice(cmds)
        c = spec["commands"][short]
        a = (allowed or {}).get(short)
        dirs = list(a["directives"]) if a else list(c["directives"])
        opts = dict(a["options"]) if a else {k: (True if o["type"] == "switch" else o["values"])
                                             for k, o in c["options"].items()}
        opts = {k: v for k, v in opts.items() if k in c["options"]}
        use_dir = dirs and (not opts or rng.random() < 0.5)
        step: dict = {"cmd": short}
        if use_dir:
            step["directive"] = rng.choice(dirs)
            pool = [k for k in opts if k in c["directive_combinable"] or k.startswith("+")]
            n = rng.randint(0, min(2, len(pool)))
        else:
            pool = list(opts)
            n = rng.randint(1, min(4, len(pool))) if pool else 0
        chosen = {}
        for k in rng.sample(pool, n) if n else []:
            o = c["options"][k]
            vals = opts[k]
            if o["type"] == "switch":
                chosen[k] = True
            elif o["type"] == "multi":
                vals = vals if isinstance(vals, list) else o["values"]
                chosen[k] = rng.sample(vals, min(len(vals), rng.randint(1, o.get("max_pick", 1))))
            else:
                vals = vals if isinstance(vals, list) else o["values"]
                chosen[k] = rng.choice(vals)
        if chosen:
            step["opts"] = chosen
        if legal(step, spec):
            return step
    return None


def build_spec(version: str, n_slr: int = 1) -> dict:
    spec = apply_device(parse_help(version), n_slr)
    spec["size"] = space_size(spec)
    return spec


def save(spec: dict, path: Path) -> None:
    spec["size"] = space_size(spec)
    path.write_text(json.dumps(spec, indent=1))


if __name__ == "__main__":
    s = build_spec("2025.1")
    for k, c in s["commands"].items():
        print(k, len(c["directives"]), "directives;", len(c["options"]), "options;",
              "excluded:", len(c["excluded_options"]))
        print("   directives:", list(c["directives"]))
        print("   options:", {f: o["type"] for f, o in c["options"].items()})
    print(json.dumps(s["size"], indent=1))
    print(len(enumerate_simple_steps(s)), "simple steps")
    print(itertools.islice((step_key(x) for x in enumerate_simple_steps(s)), 0, 5).__next__())

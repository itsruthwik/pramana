"""Turn a chain of steps into a legal Vivado command sequence.

Every training DCP is routed. Steps need different design states:
  opt      needs an unplaced netlist     -> pramana inserts `place_design -unplace`
  place    needs an unplaced netlist     -> same
  phys_opt needs a placed design         -> after opt, pramana inserts a default place
  route    needs a placed design; +ripup unroutes first
Every chain ends fully routed: missing place/route steps are added with default settings and
marked "auto" so the analysis knows they weren't part of the choice.
"""
from __future__ import annotations

from .space import COMMANDS, step_key


def _tcl_val(v) -> str:
    if isinstance(v, list):
        return "{" + " ".join(str(x) for x in v) + "}"
    return str(v)


# Plain Vivado Tcl (no pramana procs), so compiled bundles can use it too.
ROUTE_IF_NEEDED = (
    "set _rs [report_route_status -return_string]; set _u 0; set _p 0; "
    "regexp -line {unrouted nets[\\s.]*:\\s*([0-9]+)} $_rs -> _u; "
    "regexp -line {partially routed nets[\\s.]*:\\s*([0-9]+)} $_rs -> _p; "
    "if {$_u > 0 || $_p > 0} {route_design}")


def tcl_step(name: str, code: str) -> dict:
    """A custom-Tcl step (a discovered recipe). Like every unit it ends routed."""
    return {"cmd": "tcl", "name": name, "tcl": code.rstrip() + "\n" + ROUTE_IF_NEEDED}


def step_command(step: dict) -> str:
    parts = [COMMANDS[step["cmd"]]]
    if step.get("directive"):
        parts += ["-directive", step["directive"]]
    for k, v in sorted((step.get("opts") or {}).items()):
        if k.startswith("+"):
            continue
        parts.append(k if v is True else f"{k} {_tcl_val(v)}")
    return " ".join(parts)


def expand(chain: list[dict]) -> list[tuple[str, list[str], bool]]:
    """-> [(step_name, [tcl commands], is_auto)], ending with the design routed."""
    state = "routed"
    out: list[tuple[str, list[str], bool]] = []
    for i, st in enumerate(chain):
        name = f"s{i+1}_{st['cmd']}"
        cmds: list[str] = []
        c = st["cmd"]
        if c == "tcl":
            # assumes a routed or placed design; tcl_step re-routes if it left nets unrouted
            out.append((name, [st["tcl"]], False))
            state = "routed"
            continue
        if c in ("opt", "place"):
            if state != "unplaced":
                cmds.append("place_design -unplace")
            cmds.append(step_command(st))
            state = "unplaced" if c == "opt" else "placed"
        elif c == "phys_opt":
            if state == "unplaced":
                out.append((f"s{i+1}_auto_place", ["place_design"], True))
                state = "placed"
            cmds.append(step_command(st))
        elif c == "route":
            if state == "unplaced":
                out.append((f"s{i+1}_auto_place", ["place_design"], True))
                state = "placed"
            if state == "routed" and (st.get("opts") or {}).get("+ripup"):
                cmds.append("route_design -unroute")
            cmds.append(step_command(st))
            state = "routed"
        out.append((name, cmds, False))
    if state == "unplaced":
        out.append(("auto_place", ["place_design"], True))
        state = "placed"
    if state == "placed":
        out.append(("auto_route", ["route_design"], True))
    return out


def describe(chain: list[dict]) -> str:
    return " > ".join(step_key(s) for s in chain)

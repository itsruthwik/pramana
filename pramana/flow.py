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

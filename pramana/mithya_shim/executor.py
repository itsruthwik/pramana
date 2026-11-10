"""Turns steps into one Vivado Tcl script (run on an open routed design).

No step starts unless its estimated time fits before the deadline (a running Vivado command
cannot be stopped cleanly) and is at most STEP_LIMIT_S. The first step always runs unless it
alone exceeds the step limit. Candidate steps run keep-if-better (kib.tcl).
"""
from pathlib import Path

from protocol import STEP_LIMIT_S

KIB_TCL = (Path(__file__).resolve().parent / "kib.tcl").read_text()


def policy_tcl(steps, budget_s=None, step_limit_s=STEP_LIMIT_S):
    lines = [KIB_TCL]
    if budget_s:
        lines.append(f"set ::pramana::deadline [expr {{[clock seconds] + {int(budget_s)}}}]")
    for i, s in enumerate(steps):
        body = "; ".join(s.tcl)
        est = int(s.est_secs or 0)
        if step_limit_s and est > step_limit_s:
            lines.append(f"puts \"PRAMANA {s.name} skipped (estimated {est} s > step limit "
                         f"{int(step_limit_s)} s)\"")
            continue
        need = 0 if i == 0 else est
        if s.mode == "committed":
            lines.append(f"if {{[::pramana::time_left {need}]}} {{\nputs \"PRAMANA step {s.name} "
                         f"(committed)\"\n{body}\n}} else {{puts \"PRAMANA {s.name} skipped "
                         f"(does not fit the time left)\"}}")
        else:
            lines.append(f"::pramana::keep_if_better {{{s.name}}} {{{body}}} [pwd] {need}")
    return "\n".join(lines)

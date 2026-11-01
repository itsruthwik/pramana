"""Independent, read-only measurement of a DCP: the same RESULT block every pramana run ends
with (WNS, WHS, WPWS, route status, clocks), produced by a fresh Vivado on the file itself.

Used to score every consumer agent (official contest agent, maya, PACT) the same way, instead
of trusting each agent's own bookkeeping. Validity is pramana's rule: fully routed, 0 route
errors, WHS >= 0, WPWS >= 0.

  python -m pramana.measure <dcp> [<out_dir>]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from . import tcl
from .config import VIVADO_SETTINGS
from .runner import parse_stdout
from .vivado import est_mem_gb, run_vivado


def valid(res: dict | None) -> bool:
    if not res:
        return False
    try:
        return (res.get("routed") == 1 and (res.get("route_errors") or 0) == 0
                and res.get("whs") is not None and res["whs"] >= 0
                and (res.get("wpws") is None or res["wpws"] >= 0)
                and (res.get("routable_nets") or 0) > 0)
    except TypeError:
        return False


def _reclock_tcl(clocks: list[tuple[str, float]]) -> str:
    """Re-create each named clock at the given period on its own source (the contest scores
    under the ORIGINAL constraints; an agent that loosened a clock must be measured at the
    input's period). Clocks missing from the design are skipped."""
    lines = []
    for name, period in clocks:
        lines.append(
            f'set _c [get_clocks -quiet {{{name}}}]\n'
            f'if {{$_c ne ""}} {{\n'
            f'  set _src [get_property SOURCE_PINS $_c]\n'
            f'  set _obj [get_ports -quiet $_src]; if {{$_obj eq ""}} {{set _obj [get_pins -quiet $_src]}}\n'
            f'  if {{$_obj ne ""}} {{create_clock -name {{{name}}} -period {period} $_obj; '
            f'puts "RECLOCK {name} {period}"}} else {{puts "RECLOCK_SKIP {name}"}}\n'
            f'}}')
    return "\n".join(lines)


def measure_dcp(dcp: Path, out_dir: Path | None = None, threads: int = 4,
                vivado_settings: str = VIVADO_SETTINGS, timeout_s: float = 1800,
                clocks: list[tuple[str, float]] | None = None) -> dict:
    """Open `dcp` read-only, emit the RESULT block, return {status, result, valid, secs}.
    `clocks` = [(name, period_ns)] re-imposes those constraints before measuring."""
    dcp = Path(dcp).resolve()
    out = Path(out_dir).resolve() if out_dir else Path(tempfile.mkdtemp(prefix="pramana_measure_"))
    out.mkdir(parents=True, exist_ok=True)
    script = "\n".join([tcl.header(threads), f"open_checkpoint {tcl.tcl_path(str(dcp))}",
                        _reclock_tcl(clocks) if clocks else "",
                        "_emit_result", f'puts "{tcl.DONE}"']) + "\n"
    (out / "measure.tcl").write_text(script)
    vr = run_vivado(out / "measure.tcl", out, vivado_settings, timeout_s,
                    need_gb=est_mem_gb(dcp.stat().st_size / 1e6))
    (out / "stdout.txt").write_text(vr.stdout)
    parsed = parse_stdout(vr.stdout, vr.exit_code, vr.timed_out)
    res = parsed.get("result")
    m = {"dcp": str(dcp), "status": parsed["status"], "result": res, "valid": valid(res),
         "secs": round(vr.wall_s, 1), "measure_dir": str(out)}
    (out / "measure.json").write_text(json.dumps(m, indent=1))
    return m


def main(argv=None) -> None:
    a = argv or sys.argv[1:]
    m = measure_dcp(Path(a[0]), Path(a[1]) if len(a) > 1 else None)
    print(json.dumps(m, indent=1))


if __name__ == "__main__":
    main()

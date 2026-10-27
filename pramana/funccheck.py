"""Functional check for netlist-changing steps: does an output checkpoint still compute what
the input checkpoint computed?

Two parts:

* `edits_netlist(tcl)` -- a static, conservative scan of a Tcl recipe for commands that edit
  the logical netlist directly (create_cell, connect_net, set_property INIT, ...). Vivado's
  own implementation commands (opt_design, place_design, phys_opt_design, route_design,
  power_opt_design) are TRUSTED here: their netlist transforms (replication, retiming,
  restructuring, remapping) are Vivado's responsibility to keep equivalent, and pramana does
  not re-verify them unless asked (`include_vivado_opt=True`). Anything the scan cannot
  resolve statically (eval, command names held in variables, property names from variables)
  counts as an edit, so a False answer means "only placement/routing/constraint commands
  were found".

* `check(input_dcp, output_dcp, workdir)` -- a random-vector SIMULATION comparison. Both
  checkpoints are exported with `write_verilog -mode funcsim`, one generated testbench drives
  identical pseudo-random stimulus (from `seed`) into all top-level inputs of each netlist in
  two separate xsim runs, and every top-level output bit is sampled once per reference-clock
  cycle and compared after a warm-up. This is SAMPLED evidence, not a proof: a difference
  that the random stimulus never excites or never propagates to an output within `cycles`
  cycles is not detected (e.g. deep state machines, rare reset/handshake sequences, memories
  that random traffic never reads back). It is cheap, independent of how the netlist was
  edited, and catches gross breakage (wrong LUT equation, dropped/misconnected net). For a
  proof, use a formal equivalence checker on the two funcsim netlists.

Stimulus: primary clocks (from get_clocks / SOURCE_PINS, traced back to a top-level port)
toggle at their constrained period, quantised to 10 ps; the fastest one is the reference
clock. Inputs whose name looks like a reset (rst, reset, aresetn, rst_n, ...; polarity from
the name) are held active for `reset_cycles` cycles at the start and once mid-run, and
inactive otherwise. All other inputs get new random values every reference cycle (per port and
per epoch of ~cycles/12: uniform, biased to 0, biased to 1, or held; debug/jtag/scan/test-named
inputs are held at 0 until the mid-run reset, then randomised like the rest), applied
half-way between clock edges (all clock edges are on multiples of 10 ps, stimulus on 5 mod
10, sampling on 8 mod 10, so a zero-delay funcsim has no edge/stimulus races). Global set/
reset (glbl GSR, 100 ns) is released before the first sampled cycle.

Status: "equivalent" (no output difference in the compared window, and some output toggled),
"mismatch" (at least one difference), "inconclusive" (no output bit toggled after warm-up,
so the comparison carried no information), "error" (export/compile/sim failure, port lists
differ, black boxes, encrypted IP, ...).

Vivado and xsim runs hold a machine-wide slot from pramana.vivado (the export goes through
run_vivado; the xsim phase takes a slot with the same lock files).

CLI:  python3 -m pramana.funccheck in.dcp out.dcp workdir [--cycles N] [--seed S] [--force-sim]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import random
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import vivado
from .config import VIVADO_SETTINGS

# --------------------------------------------------------------------------------------------
# 1. static scan
# --------------------------------------------------------------------------------------------

# Commands that change the logical netlist (cells, nets, pins, ports, names, cell contents).
NETLIST_CMDS = (
    "create_cell", "remove_cell", "create_net", "remove_net", "connect_net", "disconnect_net",
    "create_pin", "remove_pin", "create_port", "remove_port", "create_interface",
    "rename_cell", "rename_net", "rename_pin", "rename_port", "rename_ref",
    "tie_unused_pins", "insert_buffer", "remove_buffer", "replace_cell", "swap_cells",
    "update_design", "read_checkpoint", "read_edif", "read_verilog", "read_vhdl", "link_design",
    "create_debug_core", "connect_debug_port", "implement_debug_core", "reset_property",
    "update_mem_init", "set_lut_equation",
)
# Vivado implementation commands that may restructure the netlist; trusted by default.
VIVADO_OPT_CMDS = ("opt_design", "phys_opt_design", "power_opt_design")
# Commands whose effect cannot be resolved statically.
DYNAMIC_CMDS = ("eval", "uplevel", "source", "interp", "after", "trace")

# set_property names that do not change logical function (placement, routing, packing hints,
# electrical/IO, debug, DRC, bitstream/config, tool directives).
SAFE_PROPS = {
    "LOC", "BEL", "IS_LOC_FIXED", "IS_BEL_FIXED", "IS_ROUTE_FIXED", "FIXED_ROUTE", "ROUTE",
    "LOCK_PINS", "PACKAGE_PIN", "IOSTANDARD", "DRIVE", "SLEW", "IOB", "IN_TERM", "ODT",
    "DIFF_TERM", "DIFF_TERM_ADV", "PULLTYPE", "PULLUP", "PULLDOWN", "KEEPER", "IBUF_LOW_PWR",
    "DONT_TOUCH", "KEEP", "KEEP_HIERARCHY", "MARK_DEBUG", "MAX_FANOUT", "ASYNC_REG",
    "CLOCK_DEDICATED_ROUTE", "CLOCK_ROOT", "USER_CLOCK_ROOT", "CLOCK_DELAY_GROUP",
    "CLOCK_BUFFER_TYPE", "CLOCK_LOW_FANOUT", "CLOCK_REGION_ASSIGNMENT", "CONTAIN_ROUTING",
    "EXCLUDE_PLACEMENT", "SNAPPING_MODE", "GRID_RANGES", "RLOC", "RLOC_ORIGIN", "U_SET",
    "HU_SET", "RPM_GRID", "LUTNM", "HLUTNM", "SOFT_HLUTNM", "SEVERITY", "IS_ENABLED",
    "PROHIBIT", "USER_SLL_REG", "USER_CROSSING_SLR", "USER_SLR_ASSIGNMENT", "PBLOCK",
    "DIRECT_ENABLE", "DIRECT_RESET", "EQUIVALENT_DRIVERS", "OPT_SKIP", "PHYS_OPT_SKIP",
    "PHYS_OPT_MODIFIED", "FORCE_MAX_FANOUT", "ROUTE_STATUS", "DCI_CASCADE", "INTERNAL_VREF",
    "IO_BUFFER_TYPE", "DELAY_BYPASS",
}
SAFE_PROP_PREFIXES = ("BITSTREAM.", "CONFIG.", "STEPS.", "HD.", "PHYS_OPT_", "POST_CRC",
                      "CFGBVS", "USER_", "SIM.", "DEBUG_")

_WORD = r"(?<![\w$\-])(?:::)?({})(?![\w\-])"


def _strip_comments(tcl: str) -> str:
    out = []
    for ln in tcl.replace("\\\n", " ").splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        # trailing ";# comment"
        out.append(re.sub(r";\s*#.*$", "", ln))
    return "\n".join(out)


def _tcl_words(s: str) -> list[str]:
    """Split one command's text into words, keeping {..}, [..] and ".." groups intact."""
    words, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n or s[i] in ";\n]":
            break
        j, depth = i, 0
        if s[i] in "{[\"":
            close = {"{": "}", "[": "]", "\"": "\""}[s[i]]
            depth, j = 1, i + 1
            while j < n and depth:
                if s[j] == "\\":
                    j += 2
                    continue
                if close != "\"" and s[j] == s[i]:
                    depth += 1
                elif s[j] == close:
                    depth -= 1
                j += 1
        while j < n and s[j] not in " \t;\n":
            if s[j] == "[":
                d = 1
                j += 1
                while j < n and d:
                    d += {"[": 1, "]": -1}.get(s[j], 0)
                    j += 1
                continue
            if s[j] == "]":
                break
            j += 1
        words.append(s[i:j])
        i = j
    return words


def _set_property_names(args: str) -> list[str] | None:
    """Property names set by one set_property call; None if not statically known."""
    w = _tcl_words(args)
    while w and w[0].lower() in ("-quiet", "-verbose"):
        w.pop(0)
    if not w:
        return None
    if w[0] == "-dict":
        if len(w) < 2:
            return None
        body = w[1].strip("{}\"")
        if "$" in body or "[" in body:
            return None
        items = _tcl_words(body)
        return [items[k].strip("{}\"") for k in range(0, len(items), 2)]
    name = w[0].strip("{}\"")
    if "$" in name or "[" in name:
        return None
    return [name]


def _safe_prop(name: str) -> bool:
    u = name.upper()
    return u in SAFE_PROPS or u.startswith(SAFE_PROP_PREFIXES)


_EXPR_OPS = {"ne", "eq", "in", "ni", "<", ">", "<=", ">=", "==", "!=", "&&", "||", "+", "-",
             "*", "/", "%"}


def scan_netlist_edits(tcl: str, include_vivado_opt: bool = False) -> list[str]:
    """Reasons (one string per finding) why `tcl` may change the logical netlist; [] if none.

    Conservative: unknown/dynamic constructs are reported. Vivado's opt/phys_opt/power_opt
    are only reported with include_vivado_opt=True (they are trusted by default)."""
    t = _strip_comments(tcl)
    found: list[str] = []
    cmds = list(NETLIST_CMDS) + (list(VIVADO_OPT_CMDS) if include_vivado_opt else [])
    for m in re.finditer(_WORD.format("|".join(map(re.escape, cmds))), t):
        found.append(m.group(1))
    for m in re.finditer(_WORD.format("|".join(DYNAMIC_CMDS)), t):
        # command position only: start of line, after ; [ or {
        pre = t[:m.start()].rstrip(" \t")
        if not pre or pre[-1] in "\n;[{":
            found.append(f"dynamic:{m.group(1)}")
    # a command word that is itself a substitution: `$cmd ...` / `[...] ...` at command start
    for m in re.finditer(r"(?:^|[;{\n])[ \t]*(\$\w+)[ \t]+(\S+)", t):
        # `{$n ne ""}` / `{$i < 3}`: an expression (if/for/while condition), not a command
        if m.group(2) in _EXPR_OPS:
            continue
        found.append(f"dynamic:{m.group(1)}")
    for m in re.finditer(_WORD.format("set_property"), t):
        rest = t[m.end():]
        end = len(rest)
        for k, ch in enumerate(rest):          # end of this command (newline/; outside groups)
            if ch in "\n;":
                end = k
                break
        names = _set_property_names(rest[:end])
        if names is None:
            found.append("set_property:<dynamic>")
            continue
        found += [f"set_property:{n}" for n in names if not _safe_prop(n)]
    # dedupe, keep order
    return list(dict.fromkeys(found))


def edits_netlist(tcl: str, include_vivado_opt: bool = False) -> bool:
    """True if the Tcl may edit the logical netlist directly (so the result needs `check`).
    See scan_netlist_edits for the rules; Vivado's own commands are trusted by default."""
    return bool(scan_netlist_edits(tcl, include_vivado_opt))


# --------------------------------------------------------------------------------------------
# 2. simulation comparison
# --------------------------------------------------------------------------------------------

EXPORT_TCL = r"""
proc _fc_dump {dcp tag} {
    if {[catch {open_checkpoint $dcp} e]} { puts "FC_ERR $tag open_checkpoint: $e"; return }
    set f [open "$tag.ports.tsv" w]
    puts $f "TOP\t[get_property TOP [current_design]]"
    foreach p [get_ports -quiet] {
        puts $f "PORT\t[get_property NAME $p]\t[get_property DIRECTION $p]\t[get_property BUS_NAME $p]\t[get_property BUS_START $p]\t[get_property BUS_STOP $p]"
    }
    foreach c [get_clocks -quiet] {
        if {[get_property IS_GENERATED $c] || [get_property IS_VIRTUAL $c]} continue
        foreach s [get_property SOURCE_PINS $c] {
            set ports [get_ports -quiet $s]
            if {$ports eq ""} {
                set pin [get_pins -quiet $s]
                if {$pin ne ""} {
                    foreach o [all_fanin -quiet -flat -startpoints_only $pin] {
                        if {[get_property CLASS $o] eq "port"} { lappend ports $o }
                    }
                }
            }
            foreach pp $ports {
                puts $f "CLOCK\t$c\t[get_property NAME $pp]\t[get_property PERIOD $c]\t[get_property WAVEFORM $c]"
            }
        }
    }
    puts $f "BLACKBOX\t[llength [get_cells -quiet -hierarchical -filter {IS_BLACKBOX}]]"
    close $f
    if {[catch {write_verilog -mode funcsim -force "$tag.v"} e]} {
        puts "FC_ERR $tag write_verilog: $e"
    } else {
        puts "FC_OK $tag"
    }
    close_design
}
"""

_RESET_HIGH = {"rst", "reset", "arst", "areset", "srst", "sreset", "clr", "aclr", "sclr", "rsti"}
_RESET_LOW = {"rstn", "resetn", "arstn", "aresetn", "srstn", "sresetn", "nrst", "nreset", "rstb",
              "resetb", "rstni", "rstl", "aclrn", "clrn", "nrsti"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_VKEYWORDS = {"input", "output", "inout", "wire", "reg", "module", "endmodule", "begin", "end",
              "always", "assign", "initial", "integer", "if", "else", "for", "case", "default"}


def _vname(n: str) -> str:
    return n if _IDENT.fullmatch(n) and n not in _VKEYWORDS else f"\\{n} "


def reset_polarity(name: str) -> int | None:
    """1 = active-high reset, 0 = active-low, None = not a reset (decided from the name)."""
    toks = [t.lower() for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name) if t]
    for i, t in enumerate(toks):
        if t in _RESET_LOW:
            return 0
        if t in _RESET_HIGH:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            return 0 if nxt in ("n", "ni", "b", "l", "low", "neg") else 1
    return None


_QUIET_TOKENS = {"debug", "dbg", "jtag", "tck", "tms", "tdi", "trst", "scan", "test", "testmode"}


def is_quiet_port(name: str) -> bool:
    """Debug/test/scan-style input (by name): random traffic there (e.g. a debug halt request)
    tends to park the design, so such inputs are held at 0 for the first half of the run."""
    toks = {t.lower() for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name) if t}
    return bool(toks & _QUIET_TOKENS)


def _parse_ports(path: Path) -> dict:
    top, ports, clocks, bb = None, [], {}, 0
    for ln in path.read_text().splitlines():
        f = ln.split("\t")
        if f[0] == "TOP":
            top = f[1]
        elif f[0] == "PORT":
            ports.append({"name": f[1], "dir": f[2], "bus": f[3],
                          "bs": int(f[4]) if f[4] else None, "be": int(f[5]) if f[5] else None})
        elif f[0] == "CLOCK":
            wave = [float(x) for x in f[4].split()] if len(f) > 4 and f[4] else [0.0, float(f[3]) / 2]
            clocks.setdefault(f[2], {"clock": f[1], "period": float(f[3]), "wave": wave})
        elif f[0] == "BLACKBOX":
            bb = int(f[1])
    return {"top": top, "ports": ports, "clocks": clocks, "blackboxes": bb}


def _netlist_hash(v: Path) -> str:
    h = hashlib.md5()
    with open(v, "rb") as fh:
        for ln in fh:
            if not ln.lstrip().startswith(b"//"):
                h.update(ln)
    return h.hexdigest()


def _bit_index(p: dict) -> int | None:
    if not p["bus"]:
        return None
    m = re.search(r"\[(\d+)\]$", p["name"])
    return int(m.group(1)) if m else None


def _groups(ports: list[dict]) -> list[dict]:
    """Verilog-level ports: buses (with range) and scalars, sorted by name."""
    g: dict[str, dict] = {}
    for p in ports:
        key = p["bus"] or p["name"]
        e = g.setdefault(key, {"vname": key, "dir": p["dir"], "bs": p["bs"], "be": p["be"],
                               "bits": {}})
        e["bits"][_bit_index(p)] = p["name"]
    return [g[k] for k in sorted(g)]


def _q10(ps: float) -> int:
    return max(10, int(round(ps / 10.0)) * 10)


def build_testbench(info: dict, stim_file: Path, cycles: int,
                    reset_cycles: int, seed: int) -> tuple[str, list[str], dict]:
    """Return (testbench verilog, list of output bit names in sample-column order, meta)."""
    groups = _groups(info["ports"])
    clocks = info["clocks"]
    # clock signals
    clk_sig, clk_lines = {}, []
    for k, (port, c) in enumerate(sorted(clocks.items())):
        per = _q10(c["period"] * 1000)
        rise = _q10(c["wave"][0] * 1000) if c["wave"][0] > 0 else 0
        high = _q10((c["wave"][1] - c["wave"][0]) * 1000)
        high = min(high, per - 10)
        clk_sig[port] = f"fc_clk{k}"
        clk_lines.append(
            f"  reg fc_clk{k} = 1'b0;\n  initial begin #{rise}; forever begin fc_clk{k} = 1'b1;"
            f" #{high}; fc_clk{k} = 1'b0; #{per - high}; end end")
    P = min((_q10(c["period"] * 1000) for c in clocks.values()), default=10000)
    P = max(P, 20)
    # data/reset input bits and output bits
    din_bits, rst_cols, out_bits = [], {}, []
    conns = []
    dgroups = []                                  # (first din column, width) of data groups
    quiet: set[int] = set()                       # debug/test groups: held 0 in the first half
    for g in groups:
        lo = len(din_bits)
        if g["bs"] is not None:
            rng = range(g["bs"], g["be"] - 1, -1) if g["bs"] >= g["be"] else range(g["bs"], g["be"] + 1)
            order = list(rng)                     # MSB-first as declared
        else:
            order = [None]
        exprs = []
        for b in order:
            pname = g["bits"].get(b)
            if pname is None:                    # bit absent from the netlist: tie low
                exprs.append("1'b0" if g["dir"] == "IN" else "")
                continue
            if g["dir"] == "IN":
                if pname in clk_sig:
                    exprs.append(clk_sig[pname])
                else:
                    pol = reset_polarity(g["vname"])
                    if pol is not None:
                        rst_cols[len(din_bits)] = pol
                    exprs.append(f"fc_din[{len(din_bits)}]")
                    din_bits.append(pname)
            else:                                  # OUT / INOUT: observed
                exprs.append(f"fc_dout[{len(out_bits)}]")
                out_bits.append(pname)
        if len(din_bits) > lo and not any(c in rst_cols for c in range(lo, len(din_bits))):
            dgroups.append((lo, len(din_bits) - lo))
            if is_quiet_port(g["vname"]):
                quiet.add(lo)
        exprs = [e for e in exprs if e] or [""]
        conn = exprs[0] if len(exprs) == 1 else "{" + ", ".join(exprs) + "}"
        conns.append(f"    .{_vname(g['vname'])}({conn})")
    W, OW = max(1, len(din_bits)), max(1, len(out_bits))
    # stimulus file (hex rows, fc_din[W-1:0])
    # Uniform random input on every port can keep a design in an uninteresting state (e.g. a
    # random debug/halt request every few cycles stalls a CPU), so after the first epoch each
    # input port group draws a mode per epoch: uniform, mostly-0, mostly-1, or held constant.
    rng = random.Random(seed)
    mid = cycles // 2
    epoch = max(64, cycles // 12)
    modes: dict = {}
    with open(stim_file, "w") as fh:
        for k in range(cycles):
            if k % epoch == 0:
                for lo, n in dgroups:
                    r = rng.random()
                    modes[lo] = ("rand",) if k == 0 or r < 0.4 else \
                        ("bias", 0.05) if r < 0.6 else ("bias", 0.95) if r < 0.8 else \
                        ("hold", rng.getrandbits(n))
            v = 0
            for lo, n in dgroups:
                m = modes[lo]
                if lo in quiet and k < mid:
                    bits = 0
                elif m[0] == "rand":
                    bits = rng.getrandbits(n)
                elif m[0] == "hold":
                    bits = m[1]
                else:
                    bits = sum(1 << j for j in range(n) if rng.random() < m[1])
                v |= bits << lo
            in_reset = k < reset_cycles or mid <= k < mid + reset_cycles
            for col, pol in rst_cols.items():
                active = pol if in_reset else 1 - pol
                v = (v | (1 << col)) if active else (v & ~(1 << col))
            fh.write(f"{v:0{(W + 3) // 4}x}\n")
    start = 110000                                # glbl GSR is released at 100 ns
    start -= start % P
    tb = f"""`timescale 1ps/1ps
// generated by pramana.funccheck (seed {seed}, {cycles} cycles, ref period {P} ps)
module pramana_fc_tb;
  reg  [{W - 1}:0] fc_din;
  wire [{OW - 1}:0] fc_dout;
  reg  [{W - 1}:0] fc_stim [0:{cycles - 1}];
  integer fc_k, fc_fd;
{chr(10).join(clk_lines)}
  {_vname(info['top'])} fc_dut (
{("," + chr(10)).join(conns)}
  );
  initial begin
    $readmemh("{stim_file}", fc_stim);
    fc_fd = $fopen(`FC_OUT, "w");
    fc_din = fc_stim[0];
    #{start + 5};
    for (fc_k = 0; fc_k < {cycles}; fc_k = fc_k + 1) begin
      fc_din = fc_stim[fc_k];
      #{P - 7};
      $fwrite(fc_fd, "%b\\n", fc_dout);
      #7;
    end
    $fclose(fc_fd);
    $finish;
  end
endmodule
"""
    meta = {"ref_period_ps": P, "data_inputs": len(din_bits), "outputs": len(out_bits),
            "clock_ports": sorted(clocks), "reset_inputs": sorted({din_bits[c] for c in rst_cols}),
            "reset_cycles": reset_cycles, "epoch_cycles": epoch,
            "quiet_inputs": sorted({din_bits[c].split("[")[0] for c in quiet})}
    return tb, out_bits, meta


def _sim_script(settings: str, d: Path, netlist: Path, tb: Path, glbl: bool, out: Path) -> str:
    srcs = f"{shlex.quote(str(netlist))} {shlex.quote(str(tb))}"
    if glbl:
        srcs += ' "$XILINX_VIVADO/data/verilog/src/glbl.v"'
    return f"""set -e
source {shlex.quote(settings)} >/dev/null 2>&1
cd {shlex.quote(str(d))}
xvlog -d FC_OUT='"{out}"' {srcs} > xvlog.out 2>&1
xelab -relax -debug off -L unisims_ver -L unimacro_ver -L secureip -s fc_snap \
  work.pramana_fc_tb work.glbl > xelab.out 2>&1
xsim fc_snap -R > xsim.out 2>&1
"""


def _run_sims(jobs: list[tuple[Path, str]], timeout_s: float, need_gb: float) -> list[tuple[int, float]]:
    """Run bash scripts one after the other while holding one machine-wide Vivado slot."""
    slot = vivado._acquire_slot()
    res = []
    try:
        vivado.wait_for_memory(need_gb)
        for d, script in jobs:
            (d / "sim.sh").write_text(script)
            t0 = time.time()
            try:
                p = subprocess.run(["bash", str(d / "sim.sh")], cwd=str(d), timeout=timeout_s,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = -9
            res.append((rc, time.time() - t0))
    finally:
        fcntl.flock(slot, fcntl.LOCK_UN)
        slot.close()
    return res


def _tail(p: Path, n: int = 15) -> str:
    try:
        lines = p.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    errs = [l for l in lines if l.startswith(("ERROR", "FATAL", "CRITICAL"))]
    return "\n".join((errs or lines)[-n:])


def check(input_dcp: str | Path, output_dcp: str | Path, workdir: str | Path,
          cycles: int = 20000, seed: int = 1, reset_cycles: int = 16, warmup: int | None = None,
          settings: str = VIVADO_SETTINGS, force_sim: bool = False,
          export_timeout_s: float = 3600, sim_timeout_s: float = 3600) -> dict:
    """Compare two checkpoints by random-vector simulation of their funcsim netlists.

    Sampled evidence, not a proof (see module docstring). Returns a dict with
    status ("equivalent" | "mismatch" | "inconclusive" | "error"), cycles (compared),
    mismatches (number of differing output-bit samples), mismatch_cycles, first_mismatch
    ({cycle, port, input, output} or None), mismatching_ports (top 10 with counts),
    secs (wall total) = export_secs + sim_secs + wait_secs (queued for a slot/memory),
    toggling_outputs (output bits that changed value during the compared window), x_bits
    (X/Z samples, input side), netlist_identical, secs (+ export/sim breakdown), meta.
    With identical netlists (after dropping comment lines) and force_sim=False the
    simulation is skipped: status "equivalent", method "identical_netlist".
    """
    t0 = time.time()
    wd = Path(workdir).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    ins, outs = Path(input_dcp).resolve(), Path(output_dcp).resolve()
    warmup = reset_cycles + 16 if warmup is None else warmup
    res: dict = {"status": "error", "input_dcp": str(ins), "output_dcp": str(outs),
                 "cycles": 0, "mismatches": 0, "first_mismatch": None, "seed": seed,
                 "workdir": str(wd)}

    def done(**kw):
        res.update(kw)
        res["secs"] = round(time.time() - t0, 1)
        # time spent queued for a Vivado slot / free memory (not export or simulation)
        res["wait_secs"] = round(res["secs"] - res.get("export_secs", 0)
                                 - sum(res.get("sim_secs", [])), 1)
        (wd / "funccheck.json").write_text(json.dumps(res, indent=1))
        return res

    for p in (ins, outs):
        if not p.exists():
            return done(error=f"missing checkpoint {p}")
    # --- export (one Vivado run for both checkpoints; reused if this workdir already holds
    # an export of the same two files, e.g. to re-run with another seed or more cycles)
    mb = max(ins.stat().st_size, outs.stat().st_size) / 1e6
    key = {t: [str(p), p.stat().st_size, p.stat().st_mtime] for t, p in (("in", ins), ("out", outs))}
    marker = wd / "export.json"
    try:
        reuse = json.loads(marker.read_text()) == key
    except (OSError, ValueError):
        reuse = False
    if reuse:
        res["export_secs"], res["export_reused"] = 0.0, True
    else:
        marker.unlink(missing_ok=True)
        script = wd / "export.tcl"
        script.write_text(EXPORT_TCL + f"\n_fc_dump {{{ins}}} in\n_fc_dump {{{outs}}} out\n"
                          "puts FC_EXPORT_DONE\n")
        vr = vivado.run_vivado(script, wd, settings, export_timeout_s,
                               need_gb=vivado.est_mem_gb(mb))
        res["export_secs"] = round(vr.wall_s, 1)
        errs = [l for l in vr.stdout.splitlines() if l.startswith("FC_ERR")]
        if vr.timed_out or errs or vr.stdout.count("FC_OK") != 2:
            return done(error="export failed: " + ("timeout" if vr.timed_out else
                                                    "; ".join(errs) or _tail(wd / "vivado.log")))
        marker.write_text(json.dumps(key))
    info_in, info_out = _parse_ports(wd / "in.ports.tsv"), _parse_ports(wd / "out.ports.tsv")
    if info_in["blackboxes"] or info_out["blackboxes"]:
        return done(error=f"black boxes in netlist (in={info_in['blackboxes']}, "
                          f"out={info_out['blackboxes']}); cannot simulate")
    sig = lambda i: (i["top"], sorted((p["name"], p["dir"]) for p in i["ports"]))
    if sig(info_in) != sig(info_out):
        a = {(p["name"], p["dir"]) for p in info_in["ports"]}
        b = {(p["name"], p["dir"]) for p in info_out["ports"]}
        return done(status="mismatch", error="top-level interface differs",
                    first_mismatch={"only_in": sorted(a - b)[:10], "only_out": sorted(b - a)[:10],
                                    "top": [info_in["top"], info_out["top"]]})
    for tag in ("in", "out"):
        txt = (wd / f"{tag}.v").read_text(errors="replace")
        if "`protect" in txt or "pragma protect" in txt:
            return done(error=f"{tag} netlist contains encrypted (protected) IP")
        if not re.search(rf"^module {re.escape(info_in['top'])}\b|^module \\{re.escape(info_in['top'])} ",
                         txt, re.M):
            return done(error=f"top module {info_in['top']} not found in {tag}.v")
    ident = _netlist_hash(wd / "in.v") == _netlist_hash(wd / "out.v")
    res["netlist_identical"] = ident
    if ident and not force_sim:
        return done(status="equivalent", method="identical_netlist")
    # --- testbench + two simulations
    stim = wd / "stim.hex"
    tb, out_bits, meta = build_testbench(info_in, stim, cycles, reset_cycles, seed)
    res["meta"] = meta
    (wd / "tb.v").write_text(tb)
    jobs = []
    for tag in ("in", "out"):
        d = wd / f"sim_{tag}"
        d.mkdir(exist_ok=True)
        glbl = "module glbl" not in (wd / f"{tag}.v").read_text(errors="replace")
        jobs.append((d, _sim_script(settings, d, wd / f"{tag}.v", wd / "tb.v", glbl,
                                    d / "samples.txt")))
    need = 2.0 + 0.3 * mb
    sims = _run_sims(jobs, sim_timeout_s, need)
    res["sim_secs"] = [round(s, 1) for _, s in sims]
    samples = {}
    for (d, _), (rc, _s), tag in zip(jobs, sims, ("in", "out")):
        f = d / "samples.txt"
        rows = f.read_text().split() if f.exists() else []
        if rc != 0 or len(rows) != cycles:
            why = "timeout" if rc == -9 else (_tail(d / "xsim.out") or _tail(d / "xelab.out")
                                              or _tail(d / "xvlog.out"))
            return done(error=f"simulation of {tag} failed (rc={rc}, {len(rows)}/{cycles} "
                              f"samples): {why}")
        samples[tag] = rows
    # --- compare (column i of a row is fc_dout[OW-1-i])
    OW = len(samples["in"][0])
    names = list(reversed(out_bits)) if out_bits else ["<none>"]
    nmis, mis_cycles, first, per_port, x_bits = 0, 0, None, {}, 0
    toggles = set()
    prev = None
    for k in range(warmup, cycles):
        a, b = samples["in"][k], samples["out"][k]
        x_bits += sum(ch not in "01" for ch in a)
        if prev is not None and a != prev:
            toggles.update(i for i in range(OW) if a[i] != prev[i])
        prev = a
        if a != b:
            mis_cycles += 1
            for i in range(OW):
                if a[i] != b[i]:
                    nmis += 1
                    per_port[names[i]] = per_port.get(names[i], 0) + 1
                    if first is None:
                        first = {"cycle": k, "port": names[i], "input": a[i], "output": b[i]}
    compared = max(0, cycles - warmup)
    status = "mismatch" if nmis else ("equivalent" if toggles else "inconclusive")
    top = sorted(per_port.items(), key=lambda kv: -kv[1])[:10]
    return done(status=status, method="random_sim", cycles=compared, warmup=warmup,
                mismatches=nmis, mismatch_cycles=mis_cycles, first_mismatch=first,
                mismatching_ports=dict(top), toggling_outputs=len(toggles),
                observed_outputs=len(out_bits), x_bits=x_bits)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m pramana.funccheck",
                                 description="Random-vector functional comparison of two DCPs "
                                             "(sampled simulation, not a proof).")
    ap.add_argument("input_dcp")
    ap.add_argument("output_dcp")
    ap.add_argument("workdir")
    ap.add_argument("--cycles", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reset-cycles", type=int, default=16)
    ap.add_argument("--warmup", type=int, default=None)
    ap.add_argument("--force-sim", action="store_true",
                    help="simulate even if the two funcsim netlists are identical")
    ap.add_argument("--settings", default=VIVADO_SETTINGS)
    a = ap.parse_args(argv)
    r = check(a.input_dcp, a.output_dcp, a.workdir, cycles=a.cycles, seed=a.seed,
              reset_cycles=a.reset_cycles, warmup=a.warmup, settings=a.settings,
              force_sim=a.force_sim)
    print(json.dumps(r, indent=1))
    return {"equivalent": 0, "mismatch": 1}.get(r["status"], 2)


if __name__ == "__main__":
    sys.exit(main())

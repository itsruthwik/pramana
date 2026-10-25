"""Tcl building blocks shared by every Vivado script pramana generates.

Wire protocol (all printed by `puts` at column 0, parsed by results.py):
  STEP_BEGIN <name>
  CMD_OK <name> secs=<s>   |   CMD_ERR <name> secs=<s> :: <msg>
  STEP_WNS <name> wns=<v>
  RESULT <json>
  PRAMANA_DONE
Failure is decided from these sentinels and the exit code, never from log wording.
"""
from __future__ import annotations

DONE = "PRAMANA_DONE"

MEASURE_PROCS = r"""
proc _q {s} { return "\"[string map {\\ \\\\ \" \\\"} $s]\"" }
proc _num {v} { if {$v eq "" || $v eq "na"} {return null}; return $v }
proc _worst_setup {} { return [get_timing_paths -quiet -setup -max_paths 1 -nworst 1] }
proc _wns {} { set p [_worst_setup]; if {$p eq ""} {return na}; return [get_property SLACK $p] }
proc _whs {} { set p [get_timing_paths -quiet -hold -max_paths 1 -nworst 1]; if {$p eq ""} {return na}; return [get_property SLACK $p] }
proc _wclk {} {
    set p [_worst_setup]; if {$p eq ""} {return ""}
    set c [get_property ENDPOINT_CLOCK $p]; return $c
}
proc _wper {} {
    set c [_wclk]; if {$c eq ""} {return na}
    set o [get_clocks -quiet $c]; if {$o eq ""} {return na}
    return [get_property PERIOD $o]
}
proc _wpws {} {
    if {[catch {set s [report_pulse_width -return_string]} e]} {return na}
    set v 1e9
    foreach ln [split $s "\n"] {
        if {[regexp {(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+\S+\s+\S+\s*$} $ln -> req act slk]} {
            if {$slk < $v} {set v $slk}
        }
    }
    if {$v == 1e9} {return na}; return $v
}
proc _routestat {} {
    if {[catch {set s [report_route_status -return_string]} e]} {return "na na na na"}
    set err na; set unr 0; set par 0; set rt na
    regexp -line {routing errors[\s.]*:\s*([0-9]+)} $s -> err
    regexp -line {unrouted nets[\s.]*:\s*([0-9]+)} $s -> unr
    regexp -line {partially routed nets[\s.]*:\s*([0-9]+)} $s -> par
    regexp -line {# of routable nets[\s.]*:\s*([0-9]+)} $s -> rt
    set routed 0
    if {$err eq "0" && $unr eq "0" && $par eq "0"} {set routed 1}
    return "$routed $err [expr {$unr + $par}] $rt"
}
proc _tns_fe {} {
    if {[catch {set t [report_timing_summary -quiet -no_detailed_paths -return_string]} e]} {return "na na na"}
    if {[regexp -line {^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)} $t -> w tns fe te]} {
        return "$tns $fe $te"
    }
    return "na na na"
}
proc _prims {} { if {[catch {set n [llength [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}]]} e]} {return na}; return $n }
proc _clocks_json {} {
    set out {}
    foreach c [get_clocks -quiet] { lappend out "{\"name\":[_q $c],\"period\":[_num [get_property PERIOD $c]]}" }
    return "\[[join $out ,]\]"
}
proc _result_json {} {
    set rs [_routestat]; set tf [_tns_fe]
    set wns [_wns]; set per [_wper]
    set fmax na
    if {$wns ne "na" && $per ne "na"} { set fmax [expr {1000.0/($per-$wns)}] }
    return "{\"wns\":[_num $wns],\"whs\":[_num [_whs]],\"wpws\":[_num [_wpws]],\"tns\":[_num [lindex $tf 0]],\"failing_endpoints\":[_num [lindex $tf 1]],\"total_endpoints\":[_num [lindex $tf 2]],\"routed\":[_num [lindex $rs 0]],\"route_errors\":[_num [lindex $rs 1]],\"unrouted\":[_num [lindex $rs 2]],\"routable_nets\":[_num [lindex $rs 3]],\"worst_clock\":[_q [_wclk]],\"worst_period\":[_num $per],\"fmax\":[_num $fmax],\"prims\":[_num [_prims]],\"clocks\":[_clocks_json]}"
}
proc _emit_result {} { puts "RESULT [_result_json]"; flush stdout }
proc _is_routed {} { return [lindex [_routestat] 0] }
"""


def tcl_path(p: str) -> str:
    return "{" + str(p) + "}"


def step_block(name: str, commands: list[str]) -> str:
    """Run a step's commands under one catch, with timing, then print the step's WNS."""
    body = "\n        ".join(commands)
    return f"""
puts "STEP_BEGIN {name}"; flush stdout
set _t0 [clock milliseconds]
if {{[catch {{
        {body}
    }} _msg]}} {{
    puts "CMD_ERR {name} secs=[expr {{([clock milliseconds]-$_t0)/1000.0}}] :: [string map {{"\\n" " "}} $_msg]"
}} else {{
    puts "CMD_OK {name} secs=[expr {{([clock milliseconds]-$_t0)/1000.0}}]"
}}
puts "STEP_WNS {name} wns=[_wns]"; flush stdout
"""


FINISH_ROUTE = """
if {[_is_routed] ne "1"} {
""" + step_block("finish_route", ["route_design"]) + """
}
"""


def header(threads: int) -> str:
    return (f'puts "VIVADO_VERSION [version -short]"\n'
            f"set_param general.maxThreads {threads}\n" + MEASURE_PROCS)


def write_if_better(dcp_out: str, min_wns: float | None) -> str:
    """Write the checkpoint only if the result is legal and beats min_wns (None = always)."""
    thr = "-1e9" if min_wns is None else repr(float(min_wns))
    return f"""
set _w [_wns]; set _h [_whs]
if {{[_is_routed] eq "1" && $_w ne "na" && $_h ne "na" && $_h >= 0 && $_w > {thr}}} {{
    if {{[catch {{write_checkpoint -force {tcl_path(dcp_out)}}} _we]}} {{puts "WRITE_ERR $_we"}} else {{puts "WROTE_DCP"}}
}} else {{ puts "NO_WRITE" }}
"""


PATHS_DUMP = r"""
# Top-N setup paths, one TSV row each, for feature extraction and LLM evidence.
proc _paths_dump {n fname} {
    set fh [open $fname w]
    puts $fh "slack\treq\tlevels\tdatapath\tlogic\tnet\tskew\tstart_ref\tend_ref\tstart_clk\tend_clk\tmax_fanout\tn_cells\tcell_refs"
    foreach p [get_timing_paths -quiet -setup -max_paths $n -nworst 1] {
        set sp [get_property STARTPOINT_PIN $p]; set ep [get_property ENDPOINT_PIN $p]
        set sr ""; set er ""
        catch {set sr [get_property REF_NAME [get_cells -quiet -of_objects $sp]]}
        catch {set er [get_property REF_NAME [get_cells -quiet -of_objects $ep]]}
        set mf 0
        foreach nt [get_nets -quiet -of_objects $p] {
            set f [get_property FLAT_PIN_COUNT $nt]
            if {$f ne "" && $f > $mf} {set mf $f}
        }
        set cells [get_cells -quiet -of_objects $p]
        set refs [lsort -unique [get_property REF_NAME $cells]]
        puts $fh "[get_property SLACK $p]\t[get_property REQUIREMENT $p]\t[get_property LOGIC_LEVELS $p]\t[get_property DATAPATH_DELAY $p]\t[get_property DATAPATH_LOGIC_DELAY $p]\t[get_property DATAPATH_NET_DELAY $p]\t[get_property SKEW $p]\t$sr\t$er\t[get_property STARTPOINT_CLOCK $p]\t[get_property ENDPOINT_CLOCK $p]\t$mf\t[llength $cells]\t[join $refs ,]"
    }
    close $fh
}
"""


def safe_report(name: str, cmd: str, dest_var: str = "_rd") -> str:
    """Write one report to <dest>/<name>.txt; an error becomes the file body, not a failure."""
    return f"""
if {{[catch {{set _s [{cmd} -return_string]}} _e]}} {{set _s "REPORT_ERROR: $_e"}}
set _fh [open [file join ${dest_var} {name}.txt] w]; puts $_fh $_s; close $_fh
"""

# ============================================================================
#  VENDORED from github.com/itsruthwik/vivado-opt-recipes @ b9fc031
#  maya.py loads THIS copy at runtime (RECIPES_DIR -> context/).
#  Edit upstream, then re-vendor -- do not edit here.
#  Why vendored: the outer vivado-opt-recipes/ dir is gitignored, so a clean
#  clone would otherwise run with no ::opt:: library at all -- silently, since
#  the loads are .exists()-guarded while the prompt still claims it is present.
# ============================================================================
# ============================================================================
#  vivado_opt_recipes.tcl
#  Reusable, parameterized Tcl recipes for Vivado-only frequency / timing
#  optimization on an open design (placed and/or routed).
#
#  STRICTLY VIVADO. No RapidWright. Source this into any Vivado Tcl session:
#       source /path/to/vivado-opt-recipes/vivado_opt_recipes.tcl
#  then call procs in the ::opt namespace, e.g.
#       ::opt::safe_phys_opt -directive Explore
#
#  Every recipe is a `proc` that takes the cells/nets to act on as arguments,
#  validates them, performs the operation, and (where useful) supports cheap
#  revert. Nothing runs at source-time.
#
#  VERIFIED BEHAVIOR this library relies on (from exp2 on xcvu3p, Vivado 2025.x):
#    IS_LOC_FIXED blocks place_design movement (not place_cell). [analysis doc]
# ============================================================================

namespace eval ::opt {
    variable verbose    1                ;# 0 = quiet, 1 = normal, 2 = debug
    variable revert_dir "."              ;# where try_keep_if_better drops temp dcps
}

# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
proc ::opt::log  {msg} { if {$::opt::verbose >= 1} { puts "\[opt\] $msg" } }
proc ::opt::dbg  {msg} { if {$::opt::verbose >= 2} { puts "\[opt:dbg\] $msg" } }
proc ::opt::warn {msg} { puts "\[opt:WARN\] $msg" }

# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

# Resolve an argument (name, pattern, or cell object) to exactly one cell object.
proc ::opt::_one_cell {spec} {
    set c [get_cells -quiet $spec]
    if {[llength $c] == 0} { error "::opt: no cell matches '$spec'" }
    if {[llength $c] >  1} { error "::opt: '$spec' matches [llength $c] cells; expected 1" }
    return $c
}

# ===========================================================================
#  TIMING METRICS  (cheap decision metric = setup/hold WNS via timing paths)
# ===========================================================================

# Worst-slack in ns for the worst path. type = setup|hold. Returns "" if none.
proc ::opt::wns {{type setup}} {
    set sw [expr {$type eq "hold" ? "-hold" : "-setup"}]
    set p [get_timing_paths -quiet -max_paths 1 -nworst 1 $sw]
    if {$p eq ""} { return "" }
    return [get_property SLACK $p]
}

# Full WNS/TNS/WHS/THS by parsing report_timing_summary. Heavier; use for reports.
# Returns a dict: setup_wns setup_tns hold_whs hold_ths failing_endpoints.
proc ::opt::timing_summary {} {
    set f [file join $::opt::revert_dir ".opt_tsum_[pid].rpt"]
    report_timing_summary -quiet -file $f
    set fh [open $f r]; set txt [read $fh]; close $fh; file delete -force $f
    set d [dict create setup_wns "" setup_tns "" hold_whs "" hold_ths "" failing_endpoints ""]
    # Design Timing Summary table: WNS TNS ... WHS THS ... Total Failing Endpoints
    if {[regexp -line {^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+\d+\s+\d+\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+\d+\s+\d+\s+(\d+)} \
            $txt -> w t wh th fe]} {
        dict set d setup_wns $w; dict set d setup_tns $t
        dict set d hold_whs $wh; dict set d hold_ths $th
        dict set d failing_endpoints $fe
    }
    return $d
}

proc ::opt::report_metric {{label ""}} {
    set s [::opt::wns setup]; set h [::opt::wns hold]
    ::opt::log "TIMING $label : setup WNS=$s ns  hold WNS=$h ns"
    return $s
}

# ===========================================================================
#  RECIPE 2 — CELL PLACEMENT MOVES  (BEL-precise; cheap to revert)
# ===========================================================================

# True if a cell belongs to a placer macro whose members must move together.
# Covers user/placer macros (get_macros) AND LUT-pair combines, which get_macros
# does NOT report. The authoritative signal for a combine is the SOFT_HLUTNM
# (soft pair) / HLUTNM (hard pair) property — verified on real designs: a
# soft_lutpair'd LUT5 unplace-cascades its partner even when the partner is NOT on
# the adjacent *5/*6 BEL, so a BEL-adjacency heuristic is insufficient.
proc ::opt::_in_placer_macro {c} {
    if {[get_macros -quiet -of_objects $c] ne ""}      { return 1 }
    if {[get_property -quiet SOFT_HLUTNM $c] ne ""}     { return 1 }
    if {[get_property -quiet HLUTNM $c] ne ""}          { return 1 }
    return 0
}

# Reason a cell can't be moved individually, or "" if it is freely movable.
# Checked BEFORE any unplace so callers never strand a cell or its macro siblings.
proc ::opt::_immovable_reason {c} {
    if {[get_property -quiet IS_LOC_FIXED $c]} { return "IS_LOC_FIXED" }
    if {[get_property -quiet IS_BEL_FIXED $c]} { return "IS_BEL_FIXED" }
    if {[::opt::_in_placer_macro $c]}          { return "placer-macro / LUT-combine member" }
    return ""
}

# Move a cell to a site or exact BEL. target = SLICE_X10Y10 | SLICE_X10Y10/A6LUT.
# Preserves the cell's original IS_LOC_FIXED state: place_cell sets it to 1, which
# would otherwise pin the cell and poison later moves.
proc ::opt::move_cell {cell target} {
    set c [::opt::_one_cell $cell]
    set why [::opt::_immovable_reason $c]
    if {$why ne ""} { error "::opt: cannot move '$c' ($why); move the whole macro or use phys_opt." }
    set from     [get_bels -quiet -of_objects $c]
    set wasfixed [get_property -quiet IS_LOC_FIXED $c]
    unplace_cell $c
    if {[catch {place_cell $c $target} msg]} {
        if {$from ne ""} { catch {place_cell $c $from} }   ;# roll back the single cell
        catch {set_property IS_LOC_FIXED $wasfixed $c}
        error "::opt: move_cell '$c' -> '$target' failed: $msg"
    }
    catch {set_property IS_LOC_FIXED $wasfixed $c}
    ::opt::log "$c: moved $from -> [get_bels -of_objects $c]"
}

# Swap the placement of two compatible cells (unplace both, cross-place).
proc ::opt::swap_cells {cellA cellB} {
    set a [::opt::_one_cell $cellA]
    set b [::opt::_one_cell $cellB]
    foreach x [list $a $b] {
        set why [::opt::_immovable_reason $x]
        if {$why ne ""} { error "::opt: cannot swap '$x' ($why)." }
    }
    set belA [get_bels -quiet -of_objects $a]
    set belB [get_bels -quiet -of_objects $b]
    if {$belA eq "" || $belB eq ""} { error "::opt: both cells must be placed (got '$belA','$belB')" }
    unplace_cell $a
    unplace_cell $b
    if {[catch {place_cell $a $belB; place_cell $b $belA} msg]} {
        catch {unplace_cell $a}; catch {unplace_cell $b}
        catch {place_cell $a $belA}; catch {place_cell $b $belB}   ;# restore
        error "::opt: swap_cells '$a'<->'$b' failed (likely incompatible BELs): $msg"
    }
    ::opt::log "swapped $a ($belA) <-> $b ($belB)"
}

# ===========================================================================
#  RECIPE 3 — LOCKING  (protect known-good placement from place_design)
# ===========================================================================
proc ::opt::lock_cells   {cells} { foreach c [get_cells $cells] { set_property IS_LOC_FIXED 1 $c }; ::opt::log "locked [llength [get_cells $cells]] cells" }
proc ::opt::unlock_cells {cells} { foreach c [get_cells $cells] { set_property IS_LOC_FIXED 0 $c }; ::opt::log "unlocked [llength [get_cells $cells]] cells" }

# Lock every placed cell inside a rectangular SLICE range, e.g.
#       ::opt::lock_region SLICE_X50Y10 SLICE_X55Y15
proc ::opt::lock_region {site_lo site_hi} {
    set sites [get_sites -quiet -range "$site_lo $site_hi"]
    set cells [get_cells -quiet -of_objects $sites]
    foreach c $cells { set_property IS_LOC_FIXED 1 $c }
    ::opt::log "locked [llength $cells] cells in region $site_lo:$site_hi"
    return [llength $cells]
}

# ===========================================================================
#  RECIPE 4 — TARGETED REROUTE  (fix specific nets without a full route)
#  Unlike the analysis doc, this UNROUTES the nets first so the router has work
#  to do on an already-routed design.
# ===========================================================================
proc ::opt::reroute_nets {nets {delay 1}} {
    set nl [get_nets -quiet $nets]
    if {[llength $nl] == 0} { error "::opt: no nets match '$nets'" }
    catch {set_property IS_ROUTE_FIXED 0 $nl}
    route_design -quiet -unroute -nets $nl
    if {$delay} { route_design -quiet -nets $nl -delay } else { route_design -quiet -nets $nl }
    ::opt::log "rerouted [llength $nl] net(s)"
}

# ===========================================================================
#  RECIPE 5 — SAFE phys_opt  (snapshot, run, auto-revert if WNS degrades)
# ===========================================================================
# Pass any phys_opt_design args, e.g.
#       ::opt::safe_phys_opt -directive ExploreWithHoldFix
#       ::opt::safe_phys_opt -critical_pin_opt
proc ::opt::safe_phys_opt {args} {
    return [::opt::try_keep_if_better [list phys_opt_design {*}$args]]
}

# ===========================================================================
#  REVERT INFRASTRUCTURE
# ===========================================================================

# Lightweight, checkpoint-free snapshot of placement + pin locks for a cell set.
# Ideal for PRE-ROUTE placement/pin experiments (no routing state involved).
# Cells that belong to a placer macro are skipped (with a warning): restoring
# them would unplace_cell-cascade their siblings out of the snapshot.
proc ::opt::save_placement {cells} {
    set st {}
    foreach c [get_cells $cells] {
        if {[::opt::_in_placer_macro $c]} {
            ::opt::warn "save_placement: skipping '$c' (placer-macro / LUT-combine member)"; continue
        }
        lappend st [list $c [get_bels -quiet -of_objects $c] \
                            [get_property LOCK_PINS $c] [get_property IS_LOC_FIXED $c]]
    }
    return $st
}
proc ::opt::restore_placement {state} {
    foreach e $state { catch {unplace_cell [lindex $e 0]} }       ;# clear first, avoid conflicts
    foreach e $state {
        lassign $e c bel lp fixed
        catch {set_property LOCK_PINS {} $c}
        if {$lp ne ""} { catch {set_property LOCK_PINS $lp $c} }
        if {$bel ne ""} { catch {place_cell $c $bel} }
        catch {set_property IS_LOC_FIXED $fixed $c}
    }
    ::opt::log "restored placement of [llength $state] cells"
}

# Heavy but general: checkpoint the whole design, run `script`, keep it only if
# the chosen metric did not get worse (and it did not error); else reload.
# Returns 1 if kept, 0 if reverted. metric = setup|hold.
# NOTE: reverting reloads the design, invalidating any stale cell handles.
# Acceptance policy for a trial change (pure logic — Vivado-tested in test_all_recipes.tcl).
#   - the primary metric (setup by default) must NOT regress, AND
#   - hold may degrade slightly but must NOT be pushed into a worse violation:
#     reject only if after_hold < 0 AND after_hold < before_hold.
# Slacks are in ns; "" means no such paths exist. Returns 1=accept, 0=reject.
proc ::opt::_accept {before_pri after_pri before_hold after_hold {guard_hold 1}} {
    if {$before_pri ne "" && $after_pri ne "" && $after_pri < $before_pri} { return 0 }
    if {$guard_hold && $after_hold ne ""} {
        set bh [expr {$before_hold eq "" ? 0.0 : $before_hold}]
        if {$after_hold < 0 && $after_hold < $bh} { return 0 }
    }
    return 1
}

# Fully-routed predicate: 1 only if report_route_status shows 0 nets with routing
# errors. Fail-closed — an unreadable or unwritable report returns 0. Used by
# try_keep_if_better to refuse a change that STRANDS routing (a lever that ends
# with unrouted pins would otherwise be "kept" on a setup gain and later rejected
# by the ship gate; the timing on an unrouted design is meaningless anyway).
proc ::opt::is_fully_routed {} {
    set f [file join $::opt::revert_dir ".opt_route_[pid].rpt"]
    if {[catch {report_route_status -quiet -file $f}]} { catch {file delete -force $f}; return 0 }
    if {[catch {set fh [open $f r]}]} { return 0 }
    set txt [read $fh]; close $fh; file delete -force $f
    # Mirror vivado_parse's ship-gate definition (route_bad_net_count + routable>0):
    # fully routed iff there ARE routable nets and every one of them is fully routed.
    # This equals "0 bad nets" (errors + unrouted + partially-routed), because a bad
    # net is exactly a routable net that is not fully routed. Checking only the
    # "routing errors" line MISSES cleanly-unrouted nets, which are not errors.
    # Fail-closed: an unreadable field or 0 routable nets (placement failed) is NOT a pass.
    if {![regexp -nocase {#\s*of\s+routable\s+nets[.\s]*:\s*([0-9]+)} $txt -> routable]} { return 0 }
    if {![regexp -nocase {#\s*of\s+fully\s+routed\s+nets[.\s]*:\s*([0-9]+)} $txt -> routed]}  { return 0 }
    if {$routable <= 0} { return 0 }
    return [expr {$routed == $routable ? 1 : 0}]
}

# Checkpoint the design, run `script`, keep it only if ::opt::_accept passes
# (primary metric not worsened AND hold not driven into a worse violation),
# else reload. metric = the primary slack to protect (setup|hold). guard_hold=1
# also protects hold from new/worse violations even while improving setup.
# Returns 1 if kept, 0 if reverted. NOTE: reverting reloads the design and
# invalidates any cell handles captured before the call — re-get_cells after.
#
# Both call forms are accepted, because the optional-positional signature was
# read as a flag signature by an LLM planner 8 times in one run, and each
# misread raised `wrong # args`, which aborts the WHOLE sourced script:
#     ::opt::try_keep_if_better {…} setup 1
#     ::opt::try_keep_if_better {…} -metric setup -guard_hold true
proc ::opt::try_keep_if_better {script args} {
    set metric setup
    set guard_hold 1
    if {[llength $args] && [string match -* [lindex $args 0]]} {
        if {[llength $args] % 2} {
            error "::opt::try_keep_if_better: flag form needs -flag value pairs, got: $args"
        }
        foreach {flag value} $args {
            switch -exact -- $flag {
                -metric     { set metric $value }
                -guard_hold { set guard_hold $value }
                default     { error "::opt::try_keep_if_better: unknown option '$flag' (want -metric | -guard_hold)" }
            }
        }
    } else {
        if {[llength $args] > 2} {
            error "::opt::try_keep_if_better: too many arguments: $args"
        }
        if {[llength $args] >= 1} { set metric    [lindex $args 0] }
        if {[llength $args] >= 2} { set guard_hold [lindex $args 1] }
    }
    # true/yes/on are natural to write but _accept uses guard_hold arithmetically,
    # so normalise to 1/0 here. Validate explicitly: `expr {$x}` RETURNS a
    # non-numeric string instead of erroring, so it cannot do this check.
    if {![string is boolean -strict $guard_hold]} {
        error "::opt::try_keep_if_better: guard_hold wants a boolean, got '$guard_hold'"
    }
    set guard_hold [expr {[string is true $guard_hold] ? 1 : 0}]
    if {$metric ni {setup hold}} {
        error "::opt::try_keep_if_better: -metric wants setup|hold, got '$metric'"
    }
    set before_p [::opt::wns $metric]
    set before_h [::opt::wns hold]
    # Route-completeness guard is expensive: report_route_status can take minutes on a
    # large design, and this wraps EVERY keep-best move. Pay it at most ONCE per call by
    # caching the verdict: the "before" state is the previous call's "after" (a keep-best
    # move is the only thing that changes routing between calls in the wrapped flow).
    # Seeded lazily on the first call. Staleness (a bare, unwrapped route change) is
    # backstopped by the ship gate, which re-reads route status at promotion.
    if {![info exists ::opt::_routed_cache]} { set ::opt::_routed_cache [::opt::is_fully_routed] }
    set before_routed $::opt::_routed_cache
    # Stable snapshot name (NOT keyed on [pid]): revert_dir is per-design-unique and only
    # one command runs at a time, so a fixed name cannot collide -- and it survives a
    # Vivado pid change, unlike the old .opt_revert_[pid].dcp that a restart orphaned.
    set tmp [file join $::opt::revert_dir ".opt_revert.dcp"]
    write_checkpoint -quiet -force $tmp
    set failed [catch {uplevel 1 $script} msg]
    if {$failed} { ::opt::warn "script error: $msg" }
    set after_p [::opt::wns $metric]
    set after_h [::opt::wns hold]
    # If the design was fully routed before, it MUST stay fully routed. A change that
    # strands nets is rejected regardless of the setup/hold numbers (see is_fully_routed).
    set after_routed [::opt::is_fully_routed]
    set routing_regressed [expr {$before_routed && !$after_routed}]
    set ok [expr {!$failed && !$routing_regressed \
                  && [::opt::_accept $before_p $after_p $before_h $after_h $guard_hold]}]
    if {!$ok} {
        ::opt::log "REVERT ($metric $before_p->$after_p, hold $before_h->$after_h, failed=$failed, routing_regressed=$routing_regressed)"
        close_design -quiet
        open_checkpoint $tmp
        file delete -force $tmp
        set ::opt::_routed_cache $before_routed   ;# reverted to the snapshot's routed state
        return 0
    }
    file delete -force $tmp
    set ::opt::_routed_cache $after_routed         ;# kept: cache the new routed state
    ::opt::log "KEEP ($metric $before_p->$after_p, hold $before_h->$after_h)"
    return 1
}

::opt::log "loaded vivado_opt_recipes.tcl — procs under ::opt:: (see header for usage)"

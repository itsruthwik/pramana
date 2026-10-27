# ============================================================================
#  VENDORED from github.com/itsruthwik/vivado-opt-recipes @ b9fc031
#  maya.py loads THIS copy at runtime (RECIPES_DIR -> context/).
#  Edit upstream, then re-vendor -- do not edit here.
#  Why vendored: the outer vivado-opt-recipes/ dir is gitignored, so a clean
#  clone would otherwise run with no ::opt:: library at all -- silently, since
#  the loads are .exists()-guarded while the prompt still claims it is present.
# ============================================================================
# ============================================================================
#  vivado_opt_postroute.tcl
#  Post-route DIAGNOSIS + RESTRUCTURE/ROUTE recipes. Extends the ::opt:: library.
#  STRICTLY VIVADO. Source after (or instead of) vivado_opt_recipes.tcl:
#       source /path/to/vivado-opt-recipes/vivado_opt_postroute.tcl
#
#  Design principle: DO NOT re-implement native Vivado commands. These recipes
#  WRAP and COMPOSE native commands, and only add custom logic where Vivado has
#  no equivalent. Natives leaned on here (see inspiration/vivado/*.md):
#    - report_design_analysis -timing      : per-path logic/net delay split, levels
#    - report_design_analysis -congestion   : routing congestion
#    - report_high_fanout_nets -clock_regions: replication candidates + load spread
#    - report_qor_suggestions / get_qor_suggestions : native ML QoR suggestions
#    - iphys_opt_design -critical_cell_opt   : steerable sink-aware replication
#    - phys_opt_design (-critical_cell_opt/-restruct_opt/-routing_opt/-hold_fix):
#                                              the sanctioned post-route optimizer
#    - get_timing_paths / report_timing      : path objects + per-path delay report
#    - route_design -nets / IS_ROUTE_FIXED    : targeted routing + route locking
# ============================================================================

if {[info procs ::opt::wns] eq ""} {
    source [file join [file dirname [info script]] vivado_opt_recipes.tcl]
}

# Resolve a spec to exactly one net object.
proc ::opt::_one_net {spec} {
    set n [get_nets -quiet $spec]
    if {[llength $n] == 0} { error "::opt: no net matches '$spec'" }
    if {[llength $n] >  1} { error "::opt: '$spec' matches [llength $n] nets; expected 1" }
    return $n
}

# ===========================================================================
#  A. TARGETING / DIAGNOSIS
# ===========================================================================

# Top-N worst timing path OBJECTS (thin wrapper over native get_timing_paths,
# which already returns slack-sorted path objects). type = setup|hold.
proc ::opt::critical_paths {{n 10} {type setup}} {
    set sw [expr {$type eq "hold" ? "-hold" : "-setup"}]
    return [get_timing_paths -quiet -max_paths $n -nworst 1 $sw]
}

# Parse one `report_timing` path report into a dict (PURE LOGIC — Vivado-tested
# against real report_timing output in test_all_recipes.tcl).
# Keys: slack datapath logic_ns logic_pct route_ns route_pct levels
# Relies on the stable "Data Path Delay:" / "Logic Levels:" / "Slack (...)" lines.
proc ::opt::_parse_path_report {txt} {
    set d [dict create slack "" datapath "" logic_ns "" logic_pct "" \
                       route_ns "" route_pct "" levels ""]
    if {[regexp -line {Slack\s*\([^)]*\)\s*:\s*(-?\d+\.\d+)} $txt -> s]} { dict set d slack $s }
    if {[regexp {Data Path Delay:\s*(-?\d+\.\d+)ns\s*\(logic\s+(-?\d+\.\d+)ns\s*\(([0-9.]+)%\)\s+route\s+(-?\d+\.\d+)ns\s*\(([0-9.]+)%\)\)} \
            $txt -> dp ln lp rn rp]} {
        dict set d datapath $dp; dict set d logic_ns $ln; dict set d logic_pct $lp
        dict set d route_ns  $rn; dict set d route_pct $rp
    }
    if {[regexp -line {Logic Levels:\s*(\d+)} $txt -> lv]} { dict set d levels $lv }
    return $d
}

# Classify a path dict as net-bound / logic-bound / balanced (PURE LOGIC).
#   net-bound   -> placement / replication / reroute fixes
#   logic-bound -> pin-swap / LUT restructure / retime fixes
proc ::opt::path_class {d {net_thresh 55} {levels_thresh 6}} {
    set rp [dict get $d route_pct]; set lv [dict get $d levels]
    if {$rp ne "" && $rp >= $net_thresh}                { return net-bound }
    if {$lv ne "" && $lv >= $levels_thresh}             { return logic-bound }
    if {$rp ne "" && $rp <= [expr {100 - $net_thresh}]} { return logic-bound }
    return balanced
}

# Structured analysis of the worst-N paths: a list of dicts with slack, the
# logic/route delay split, logic levels, endpoints, and a net/logic class.
proc ::opt::analyze_paths {{n 10} {type setup}} {
    set out {}
    set tmp [file join $::opt::revert_dir ".opt_path_[pid].rpt"]
    foreach p [::opt::critical_paths $n $type] {
        report_timing -quiet -of_objects $p -file $tmp
        set fh [open $tmp r]; set txt [read $fh]; close $fh
        set d [::opt::_parse_path_report $txt]
        dict set d startpoint [get_property -quiet STARTPOINT_PIN $p]
        dict set d endpoint   [get_property -quiet ENDPOINT_PIN   $p]
        dict set d class      [::opt::path_class $d]
        lappend out $d
    }
    file delete -force $tmp
    return $out
}

# Print a compact diagnosis table of the worst-N paths; returns the dict list.
proc ::opt::print_paths {{n 10}} {
    set rows [::opt::analyze_paths $n]
    ::opt::log [format "%-9s %-7s %-7s %-5s %-11s %s" slack route% logic% lvls class endpoint]
    foreach d $rows {
        ::opt::log [format "%-9s %-7s %-7s %-5s %-11s %s" \
            [dict get $d slack] [dict get $d route_pct] [dict get $d logic_pct] \
            [dict get $d levels] [dict get $d class] [dict get $d endpoint]]
    }
    return $rows
}

# Native congestion report (report_design_analysis -congestion). Returns the
# report text; pass a file to keep it. The format varies by device, so this just
# surfaces it rather than over-parsing.
proc ::opt::congestion {{file ""}} {
    set keep [expr {$file ne ""}]
    if {!$keep} { set file [file join $::opt::revert_dir ".opt_cong_[pid].rpt"] }
    report_design_analysis -quiet -congestion -file $file
    set fh [open $file r]; set txt [read $fh]; close $fh
    if {!$keep} { file delete -force $file }
    return $txt
}

# Native high-fanout report with timing + per-clock-region load distribution —
# the data you want before replicating a driver.
proc ::opt::high_fanout {{n 10} {min_fanout 100}} {
    report_high_fanout_nets -quiet -max_nets $n -fanout_greater_than $min_fanout -timing -clock_regions
}

# Native ML-based QoR suggestion engine. Surfaces report_qor_suggestions and
# returns the suggestion objects (get_qor_suggestions) for scripting.
proc ::opt::qor_suggestions {{file ""}} {
    if {$file ne ""} { report_qor_suggestions -quiet -file $file } else { report_qor_suggestions -quiet }
    return [get_qor_suggestions -quiet]
}

# Orchestrator: classify the worst-N paths, tally net- vs logic-bound, check hold
# and congestion, and print which recipes to reach for. Returns the path dicts.
proc ::opt::diagnose {{n 10}} {
    ::opt::report_metric "current"
    set rows [::opt::print_paths $n]
    array set t {net-bound 0 logic-bound 0 balanced 0}
    foreach d $rows { incr t([dict get $d class]) }
    ::opt::log "path mix: net-bound=$t(net-bound) logic-bound=$t(logic-bound) balanced=$t(balanced)"
    ::opt::log "--- recommended recipes ---"
    if {$t(net-bound) >= $t(logic-bound)} {
        ::opt::log " net-bound dominant: plan_replication / safe_phys_opt -critical_cell_opt,"
        ::opt::log "   pack_lut_ff / move_cell to shorten nets, reroute_worst_nets; inspect congestion."
    } else {
        ::opt::log " logic-bound dominant: safe_phys_opt -restruct_opt / -rewire;"
        ::opt::log "   retime is pre-route only."
    }
    set hold [::opt::wns hold]
    if {$hold ne "" && $hold < 0} { ::opt::log " HOLD VIOLATION ($hold ns): safe_phys_opt -hold_fix" }
    ::opt::log " also: report_qor_suggestions (native ML), safe_phys_opt -directive ExploreWithHoldFix."
    return $rows
}

# ===========================================================================
#  C. RESTRUCTURE  (placement micro-ops Vivado has no single command for)
# ===========================================================================

# Move <ff> into the same SLICE as <lut> to use the fast intra-slice connection.
# Tries each FREE FF BEL in the LUT's site, skipping any the placer rejects (a
# FF can only join a slice whose control set — CLK/CE/SR — matches; arbitrary
# packs hit "[Vivado 12-1409] Cannot set LOC and BEL"). Returns the new BEL, or
# errors if none accept the FF. Best used to pack a LUT with a FF it drives.
# (Occupancy is checked via get_cells -of <bel> — IS_OCCUPIED does not exist.)
proc ::opt::pack_lut_ff {lut ff {ff_priority {AFF BFF CFF DFF EFF FFF GFF HFF}}} {
    set l [::opt::_one_cell $lut]; set f [::opt::_one_cell $ff]
    set lbel [get_bels -quiet -of_objects $l]
    if {$lbel eq ""} { error "::opt: LUT '$l' is not placed" }
    set site [lindex [split $lbel /] 0]
    foreach b $ff_priority {
        set bel "$site/$b"
        if {[get_bels -quiet $bel] eq ""} continue                  ;# BEL doesn't exist here
        if {[get_cells -quiet -of_objects [get_bels $bel]] ne ""} continue   ;# occupied
        if {![catch {::opt::move_cell $f $bel}]} {
            ::opt::log "packed FF $f into $bel alongside LUT $l"
            return $bel
        }
    }
    error "::opt: no FF BEL in site $site accepts '$f' (control-set mismatch?)"
}

# Relocate <ff> to a faster FF BEL within its current SLICE (clock-to-Q). Tries
# the priority order fastest-first, skipping occupied/rejected BELs. Returns the
# new BEL, or the unchanged current BEL if no faster slot accepts the FF.
proc ::opt::move_ff_fast_bel {ff {priority {AFF BFF CFF DFF EFF FFF GFF HFF}}} {
    set f [::opt::_one_cell $ff]
    set cur [get_bels -quiet -of_objects $f]
    if {$cur eq ""} { error "::opt: FF '$f' is not placed" }
    set site [lindex [split $cur /] 0]
    foreach b $priority {
        set bel "$site/$b"
        if {$bel eq $cur} { return $cur }                ;# already on the fastest free pick
        if {[get_bels -quiet $bel] eq ""} continue
        if {[get_cells -quiet -of_objects [get_bels $bel]] ne ""} continue
        if {![catch {::opt::move_cell $f $bel}]} { return $bel }
    }
    return $cur
}

# Plan sink-aware replication of a high-fanout net's driver WITHOUT modifying the
# design. Reports the load distribution by clock region (the steering data) and
# emits a ready-to-edit `iphys_opt_design -critical_cell_opt` template — because
# choosing replica placements is a design decision, not something to guess.
# For hands-off replication, use `safe_phys_opt -critical_cell_opt` instead.
proc ::opt::plan_replication {net {file ""}} {
    set nl  [::opt::_one_net $net]
    set drv [get_pins -quiet -of_objects $nl -filter {DIRECTION == OUT}]
    ::opt::log "net $nl : driver $drv, fanout [get_property -quiet FLAT_PIN_COUNT $nl]"
    array set byreg {}
    foreach lp [get_pins -quiet -of_objects $nl -filter {DIRECTION == IN}] {
        set c [get_cells -quiet -of_objects $lp]
        set bel [get_bels -quiet -of_objects $c]
        if {$bel eq ""} continue
        set cr [get_clock_regions -quiet -of_objects [get_sites -quiet [lindex [split $bel /] 0]]]
        lappend byreg($cr) $lp
    }
    ::opt::log "load distribution by clock region:"
    foreach cr [lsort [array names byreg]] { ::opt::log "  $cr : [llength $byreg($cr)] loads" }
    set tmpl "iphys_opt_design -critical_cell_opt -net {$nl}"
    set i 0
    foreach cr [lsort [array names byreg]] {
        append tmpl " \\\n    -cluster {replica_$i {$byreg($cr)}}"
        incr i
    }
    append tmpl "\n# edit/add -place_cell {<cell> <site> ...} to steer replica placement, then run."
    ::opt::log "iphys_opt template (one replica per clock region):\n$tmpl"
    if {$file ne ""} { set fh [open $file w]; puts $fh $tmpl; close $fh }
    return $tmpl
}

# ===========================================================================
#  D. TARGETED ROUTING
# ===========================================================================

# Rip up and reroute the nets on the worst-N setup paths (uses ::opt::reroute_nets).
proc ::opt::reroute_worst_nets {{n 10} {delay 1}} {
    set nets {}
    foreach p [::opt::critical_paths $n] {
        foreach net [get_nets -quiet -of_objects $p] { lappend nets $net }
    }
    set nets [lsort -unique $nets]
    if {[llength $nets] == 0} { error "::opt: no nets on the worst $n paths" }
    ::opt::reroute_nets $nets $delay
    return [llength $nets]
}

# Lock routing of nets that currently MEET timing so reroute/phys_opt only perturb
# failing nets. Omit `nets` to freeze all routed nets. Returns the count.
proc ::opt::freeze_routes {{nets ""}} {
    if {$nets eq ""} { set nl [get_nets -quiet -hierarchical -filter {ROUTE_STATUS == ROUTED}] } \
    else            { set nl [get_nets $nets] }
    set_property IS_ROUTE_FIXED 1 $nl
    ::opt::log "froze routing on [llength $nl] net(s)"
    return [llength $nl]
}
proc ::opt::unfreeze_routes {{nets ""}} {
    if {$nets eq ""} { set nl [get_nets -quiet -hierarchical -filter {IS_ROUTE_FIXED}] } \
    else            { set nl [get_nets $nets] }
    set_property IS_ROUTE_FIXED 0 $nl
    ::opt::log "unfroze routing on [llength $nl] net(s)"
    return [llength $nl]
}

# Selective re-place of the CRITICAL CONE: unplace only the cells on the worst-N setup
# paths (RAMB/DSP/CARRY kept anchored), then place_design re-clusters just those and a full
# route_design legalises. This is the CONTROLLED form of a "selective" re-place -- the
# segfaults seen when selectively unplacing come from leaving cells unplaced at the end,
# which the place_design + full route here prevent. It is a DISTINCT lever class from a
# full place_design -directive sweep, and on designs that plateau under full re-place it can
# be the move that breaks the floor. Wrap it: ::opt::try_keep_if_better {::opt::replace_critical_cone 100}
proc ::opt::replace_critical_cone {{nworst 100} {place_directive Explore} {route_directive AggressiveExplore}} {
    # Exactly the move the sweep campaign measured to 414.6 MHz on corescore, fully routed
    # with 0 route errors and hold met (runs/place_variants/corescore_500_mod/pr-explore):
    # unroute the worst-N paths' NETS, unplace ALL their cells in one call, then re-place +
    # route. Deliberately does NOT anchor RAMB/DSP/CARRY, does NOT unplace cell-by-cell, and
    # does NOT call update_clock_routing -- adding any of those reintroduced clock-distribution
    # (CLK_HDISTR) routing conflicts and lost ~10 MHz. Keep it plain.
    set tn {}
    foreach p [get_timing_paths -setup -max_paths $nworst -nworst 1] {
        foreach n [get_nets -quiet -of_objects $p] { lappend tn $n }
    }
    set tn [lsort -unique $tn]
    if {[llength $tn] > 0} { route_design -unroute -nets $tn }
    set cc {}
    foreach p [get_timing_paths -setup -max_paths $nworst -nworst 1] {
        foreach x [get_cells -quiet -of_objects $p] { lappend cc $x }
    }
    set cc [lsort -unique $cc]
    if {[llength $cc] == 0} { error "::opt::replace_critical_cone: no cells on the worst $nworst paths" }
    unplace_cell $cc
    ::opt::log "replace_critical_cone: unrouted [llength $tn] net(s) and unplaced [llength $cc] cell(s) on the worst $nworst paths"
    place_design -directive $place_directive
    route_design -directive $route_directive
    return [::opt::wns setup]
}

# ===========================================================================
#  E. SCOPED SURGICAL RE-PLACE  (module/array cone — after full re-place plateaus)
# ===========================================================================
# These are MODULE-driven, distinct from ::opt::replace_critical_cone which is
# PATH-driven (worst-N-path cells, no hierarchy). Here the unit of work is one
# HIERARCHY SCOPE: re-solve just that subset in the fixed context of the rest of
# the design ($ one-pass, behavior-preserving). Both keep hard blocks anchored.
# Wrap with ::opt::try_keep_if_better {…} — they do NOT snapshot internally.

# Deepest hierarchy prefix that (a) covers >=30% of the worst-40-path cells and
# (b) has <= maxsize LUTs (the dose ceiling). Returns the prefix, or "" if none —
# a scope larger than the ceiling disturbs more than it gains. PURE targeting.
proc ::opt::auto_scope {{maxsize 3000}} {
    set wpc {}
    foreach p [get_timing_paths -quiet -setup -max_paths 40 -nworst 1] {
        foreach c [get_cells -quiet -of_objects $p] { lappend wpc $c }
    }
    set n [llength $wpc]
    set chosen ""; set cd -1
    for {set k 8} {$k >= 1} {incr k -1} {
        array unset cov
        foreach c $wpc {
            set parts [split $c "/"]
            if {[llength $parts] <= $k} continue
            set pref [join [lrange $parts 0 [expr {$k-1}]] "/"]
            if {[info exists cov($pref)]} { incr cov($pref) } else { set cov($pref) 1 }
        }
        foreach {pref cnt} [array get cov] {
            if {$n == 0 || double($cnt)/$n < 0.30} continue
            set sz [llength [get_cells -hierarchical -filter "NAME =~ $pref/* && REF_NAME =~ LUT*" -quiet]]
            if {$sz <= $maxsize && $sz > 0 && $k > $cd} { set chosen $pref; set cd $k }
        }
        if {$chosen ne ""} break
    }
    return $chosen
}

# S1 — HLUTNM UNCOMBINE + ANCHORED RE-PLACE of one scope.
# Within the scope: clear HLUTNM/SOFT_HLUTNM on every LUT (un-pair — paired LUTs
# double a slice's input-pin demand and starve routing), unroute + unplace those
# LUTs plus the scope's CARRY8 (FF/DSP/BRAM stay anchored), then re-place + route.
# INDICATOR: QoR "Max LUT Combined" (~40%+ of the scope's LUTs paired), OR short-
# hop worst paths (1-3 slice columns) with delay far above what distance predicts.
# Measured largest surgical gain on the sweep suite (rosetta_spam-filter, one pass).
# The recipe body is the PROVEN one: plain place_design, route -directive Explore,
# then phys_opt_design -directive Explore. Do NOT "improve" the directives — plain
# place + Explore route + Explore phys_opt route cleanly; place -directive Explore
# + route AggressiveExplore left 230 nets unrouted on spam-filter (measured).
# Wrap: ::opt::try_keep_if_better {::opt::uncombine_and_replace}
proc ::opt::uncombine_and_replace {{scope ""} {maxsize 5000} {place_dir ""} {route_dir Explore} {phys_dir Explore}} {
    if {$scope eq ""} { set scope [::opt::auto_scope $maxsize] }
    if {$scope eq ""} {
        error "::opt::uncombine_and_replace: no scope covers >=30% of the worst paths under the LUT ceiling"
    }
    set luts   [get_cells -hier -filter "NAME =~ $scope/* && REF_NAME =~ LUT*" -quiet]
    set carrys [get_cells -hier -filter "NAME =~ $scope/* && REF_NAME == CARRY8" -quiet]
    foreach c $luts { catch {set_property HLUTNM {} $c}; catch {set_property SOFT_HLUTNM {} $c} }
    set movable [concat $luts $carrys]
    if {[llength $movable] == 0} { error "::opt::uncombine_and_replace: scope '$scope' has no LUT/CARRY cells" }
    route_design -unroute -nets [get_nets -of_objects $movable -quiet]
    foreach c $movable { catch { unplace_cell $c } }
    ::opt::log "uncombine_and_replace: scope '$scope' — un-paired + unplaced [llength $luts] LUT + [llength $carrys] CARRY8"
    if {$place_dir eq ""} { place_design } else { place_design -directive $place_dir }
    route_design -directive $route_dir
    if {$phys_dir ne ""} { phys_opt_design -directive $phys_dir }
    return [::opt::wns setup]
}

# S2 — SCOPED HIERARCHY RE-PLACE (memory/array cone).
# Unplace every cell in the scope EXCEPT hard blocks (RAMB/DSP/CARRY stay
# anchored), unroute their nets, then re-place (re-clusters the freed cells near
# their readers — shortening a scattered array's address broadcast) + route.
# INDICATOR: worst path reads a distributed-RAM array (LUTRAM endpoint fed through
# a high-fanout >=~100 address net, 2-3 logic levels, route-dominated).
# DOSAGE LAW: ONE array per pass, then RE-CHECK the worst path. Re-applying to
# sibling arrays or bundling all at once REGRESSES. Stop after 2 no-improves.
# PROVEN body: plain place_design, route -directive Explore, phys_opt Explore
# (same as S1 — do NOT swap in AggressiveExplore/place-Explore; they left nets
# unrouted). Wrap: ::opt::try_keep_if_better {::opt::scoped_replace}
proc ::opt::scoped_replace {{scope ""} {maxsize 5000} {place_dir ""} {route_dir Explore} {phys_dir Explore}} {
    if {$scope eq ""} { set scope [::opt::auto_scope $maxsize] }
    if {$scope eq ""} {
        error "::opt::scoped_replace: no scope covers >=30% of the worst paths under the LUT ceiling"
    }
    set movable {}
    foreach c [get_cells -hier -filter "NAME =~ $scope/*" -quiet] {
        set ref [get_property REF_NAME $c]
        if {[string match "RAMB*" $ref] || [string match "DSP*" $ref] || [string match "CARRY*" $ref]} continue
        if {![catch { unplace_cell $c }]} { lappend movable $c }
    }
    if {[llength $movable] == 0} { error "::opt::scoped_replace: scope '$scope' had no movable (non-hard-block) cells" }
    route_design -unroute -nets [get_nets -of_objects $movable -quiet]
    ::opt::log "scoped_replace: scope '$scope' — unplaced [llength $movable] movable cell(s) (RAMB/DSP/CARRY anchored)"
    if {$place_dir eq ""} { place_design } else { place_design -directive $place_dir }
    route_design -directive $route_dir
    if {$phys_dir ne ""} { phys_opt_design -directive $phys_dir }
    return [::opt::wns setup]
}

::opt::log "loaded vivado_opt_postroute.tcl — diagnosis + restructure/route recipes"

<!-- VENDORED from github.com/itsruthwik/vivado-opt-recipes @ b9fc031 via the ruthwik branch.
     Edit upstream, then re-vendor. maya.py serves this to the planner as the ::opt:: reference. -->
# opt_recipe_context.md — agent reference for vivado-opt-recipes

Compact, single-file context for driving these recipes from an agent. All procs are in the
`::opt::` namespace. **Load once:** `source vivado_opt_postroute.tcl` (it pulls in the core).
Target: an OPEN placed and/or routed checkpoint in a Vivado Tcl session. Verified on Vivado
2025.2 / `xcvu3p-ffvc1517-2-e`. Slacks are in **ns**; `""` means "no such path".

## Operating rules (read before acting)
- **Decision metric:** `::opt::wns setup` (cheap, one path) for accept/reject; `::opt::timing_summary` for the full picture.
- **Always gate risky mutations** with `::opt::try_keep_if_better {…}` or `::opt::safe_phys_opt` — they checkpoint, run, and auto-revert if setup WNS regresses (and guard hold).
- **Handle invalidation:** `::opt::try_keep_if_better`/`::opt::safe_phys_opt` revert by RELOADING the checkpoint → every cell/net object captured earlier goes stale (stringifies to `null`). Pass **names** (strings), not handles, across these, and re-`get_cells` after a revert.
- **Scratch:** set `::opt::revert_dir <dir>` (default `"."`) so temp `.dcp`/`.rpt` don't land in cwd.

## Diagnosis & targeting (read-only — start here)
| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::diagnose ?n?` | Classify worst-N paths, tally net/logic-bound, check hold, **recommend recipes** | First step on any failing design | path dicts (+prints plan) |
| `::opt::analyze_paths ?n? ?type?` | Per-path logic/route delay split, levels, endpoints, class | Need structured path data to decide | list of dicts |
| `::opt::print_paths ?n?` | Same as analyze, as a compact table | Human/agent glance at worst paths | dict list |
| `::opt::path_class <dict> ?net_thresh? ?levels_thresh?` | Label one path `net-bound`/`logic-bound`/`balanced` | Routing a single path to a fix | string |
| `::opt::critical_paths ?n? ?type?` | Worst-N timing path **objects** (setup\|hold) | Need raw path handles to walk | path objects |
| `::opt::high_fanout ?n? ?min?` | Native high-fanout nets + per-clock-region load | Before replicating a driver | report text |
| `::opt::congestion ?file?` | Native `report_design_analysis -congestion` | Net-bound + suspect routing | report text |
| `::opt::qor_suggestions ?file?` | Native ML QoR suggestions | Want Vivado's own recommendations | suggestion objects |
| `::opt::wns ?setup\|hold?` | Worst slack of worst path | The cheap accept/reject metric | ns or `""` |
| `::opt::timing_summary` | Full setup/hold WNS/TNS + failing endpoints | Report / multi-metric decision | dict (5 keys) |
| `::opt::report_metric ?label?` | Print + return setup & hold WNS | Logging a checkpoint | setup WNS |

## Fixes — logic-bound paths (high logic %, many levels)
| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::safe_phys_opt -restruct_opt` (or `-rewire`) | Native LUT restructuring to cut levels, auto-reverted | Many logic levels | 1 kept / 0 reverted |

## Fixes — net-bound paths (high route %)
| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::safe_phys_opt -critical_cell_opt` | Native timing-driven driver replication, auto-reverted | High-fanout driver on critical net | 1/0 |
| `::opt::plan_replication <net> ?file?` | Report load-by-clock-region + emit `iphys_opt_design` template (**report-only**) | Want to steer replica placement by hand | template string |
| `::opt::reroute_worst_nets ?n? ?delay?` | Rip up + reroute nets on worst-N paths | Route delay dominates, placement ok | net count |
| `::opt::reroute_nets <nets> ?delay?` | Unroute + reroute a specific net list | Target known bad nets | — |

## Fixes — placement plateau (full re-place stopped improving; wire-bound / detour-heavy)

| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::replace_critical_cone ?nworst? ?place_dir? ?route_dir?` | SELECTIVE re-place: unplace all the cells on the worst-N setup paths (no anchoring — plain, by design), then `place_design` re-clusters them and a full `route_design` legalises. A DISTINCT lever class from a full `place_design -directive` sweep — always ends fully routed, so it avoids the unplaced-instance segfault of a raw selective unplace | Full re-place directives have PLATEAUED and the design is wire-bound / has high routing detours. Wrap it: `::opt::try_keep_if_better {::opt::replace_critical_cone 100}`, then optionally a `::opt::safe_phys_opt -directive Explore` | setup WNS |

## Fixes — scoped surgical re-place (module/array cone; after full re-place plateaus)

These are MODULE-driven (re-solve one HIERARCHY scope), distinct from `::opt::replace_critical_cone` which is PATH-driven (worst-N-path cells, no hierarchy). Reach for them when a single module or memory array dominates the worst paths and its scope LUT count is ≤ the recipe's dose ceiling (~5000; a larger scope disturbs more than it gains); `::opt::auto_scope` finds it. All keep hard blocks anchored and end fully routed. Wrap with `::opt::try_keep_if_better`.

| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::auto_scope ?maxsize?` | Deepest hierarchy prefix covering ≥30% of the worst-40-path cells with ≤ maxsize LUTs, else `""` (**targeting only**) | Pick a scope for the two recipes below | prefix or `""` |
| `::opt::uncombine_and_replace ?scope? ?maxsize? ?place_dir? ?route_dir?` | S1 — within the scope: clear `HLUTNM`/`SOFT_HLUTNM` on its LUTs (un-pair), unroute + unplace those LUTs plus the scope's CARRY8 (FF/DSP/BRAM anchored), then re-place + route | QoR flags "Max LUT Combined" (~40%+ of the scope's LUTs paired), OR worst paths are short-hop (1–3 slice cols) with delay far above what distance predicts (congestion, not distance). Empty scope = auto-detect | setup WNS |
| `::opt::scoped_replace ?scope? ?maxsize? ?place_dir? ?route_dir?` | S2 — unplace every cell in the scope EXCEPT hard blocks (RAMB/DSP/CARRY anchored), unroute their nets, re-place (re-clusters near readers), route | Worst path reads a distributed-RAM array: LUTRAM endpoint fed by a high-fanout (≥~100) address net, route-dominated. **Dosage law: ONE array per pass, re-check, stop after 2 no-improves** — bundling arrays regresses. Empty scope = auto-detect | setup WNS |

## Hold, locking, optimization, revert
| Recipe (signature) | Does | Use when | Returns |
|---|---|---|---|
| `::opt::safe_phys_opt -hold_fix` (or `-directive ExploreWithHoldFix`) | Native hold fixing, auto-reverted | `::opt::wns hold < 0` | 1/0 |
| `::opt::safe_phys_opt <args…>` | Any `phys_opt_design` run with keep-if-better wrapper | Any phys_opt; never regresses setup | 1/0 |
| `::opt::freeze_routes ?nets?` / `::opt::unfreeze_routes ?nets?` | Lock/unlock `IS_ROUTE_FIXED` | Protect met nets during incremental reroute/phys_opt | net count |
| `::opt::try_keep_if_better {…}` — defaults to setup + hold-guarded. Both arg styles work: `::opt::try_keep_if_better {…} setup 1` or `::opt::try_keep_if_better {…} -metric setup -guard_hold true` | Checkpoint→run→keep only if metric not worse AND hold not pushed to worse violation; else reload | Wrap ANY experimental change | 1 kept / 0 reverted |

## Typical agentic loop
1. `::opt::report_metric base` → `::opt::diagnose 10`.
2. Per dominant class: **logic-bound** → `::opt::safe_phys_opt -restruct_opt`. **net-bound** → `::opt::safe_phys_opt -critical_cell_opt`, `::opt::reroute_worst_nets`. **wire-bound plateau** → `::opt::replace_critical_cone` / the scoped re-place levers. **hold<0** → `::opt::safe_phys_opt -hold_fix`.
3. Every mutation is auto-gated (revert on regression). After any revert, re-`get_cells` (handles are stale).
4. Re-`::opt::diagnose`; stop when `::opt::wns setup >= 0` or no improving move remains. `write_checkpoint` to save.

<!-- Agent-facing recipe menu. The per-LUT pin-swap recipes and single-cell move/lock
micro-ops were dropped: too fine-grained for a script-emitting planner and never used.
Keep terse; this file is meant to fit in an agent prompt. -->

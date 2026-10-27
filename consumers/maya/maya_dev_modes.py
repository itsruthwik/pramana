# Copyright (c) 2026, Advent Lab ASU. All rights reserved.
"""Non-LLM developer/exploration modes for maya, split out of maya.py.

OFF THE CONTEST PATH. Nothing here runs under `make run_optimizer` (that is
DCPOptimizer in maya.py). These are the `--test`, `--explore` and `--vivado-explore`
dev tools: FPGAOptimizerTest, RapWrExplorer, VivadoExplorer and their runners.
They were moved here to shrink the shipping file's risk/lint surface; maya.py's
main() imports the runners lazily when those flags are set.

Imports DCPOptimizerBase from maya. maya runs as __main__, so that import
re-executes maya.py once as module 'maya' -- harmless (its only module-level side
effect, logging.basicConfig, is idempotent) and only ever triggered in these dev
modes, never on the contest path.
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from maya import DCPOptimizerBase
from vivado_parse import parse_high_fanout_nets, parse_timing_summary, route_bad_net_count

logger = logging.getLogger(__name__)


class FPGAOptimizerTest(DCPOptimizerBase):
    """
    Test mode for FPGA Design Optimization - hardcodes all tool calls to diagnose issues.
    
    This class runs a deterministic optimization flow without using any LLM, 
    making it easier to identify where MCP servers or Vivado might hang.
    """
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None):
        super().__init__(debug=debug, run_dir=run_dir)
        self.final_wns = None
    
    async def start_servers(self):
        """Start and connect to both MCP servers."""
        await super().start_servers(log_prefix="[TEST]")
    
    async def call_vivado_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a Vivado tool call with timing and logging."""
        logger.info(f"[VIVADO] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling vivado_{tool_name}...")
        start_time = time.time()
        
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            
            elapsed = time.time() - start_time
            logger.info(f"[VIVADO] {tool_name} completed in {elapsed:.2f}s")
            print(f"[TEST] vivado_{tool_name} completed in {elapsed:.2f}s")

            # Extract text content from result
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"

            logger.info(f"[VIVADO] {tool_name} output:\n{result_text}")
            self._write_tool_log(f"vivado_{tool_name}", arguments, result_text)
            return result_text

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: vivado_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: vivado_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise
    
    async def call_rapidwright_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a RapidWright tool call with timing and logging."""
        logger.info(f"[RAPIDWRIGHT] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling rapidwright_{tool_name}...")
        start_time = time.time()
        
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            
            elapsed = time.time() - start_time
            logger.info(f"[RAPIDWRIGHT] {tool_name} completed in {elapsed:.2f}s")
            print(f"[TEST] rapidwright_{tool_name} completed in {elapsed:.2f}s")

            # Extract text content from result
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"

            logger.info(f"[RAPIDWRIGHT] {tool_name} output:\n{result_text}")
            self._write_tool_log(f"rapidwright_{tool_name}", arguments, result_text)
            return result_text

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: rapidwright_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: rapidwright_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise
    
    def parse_wns_from_timing_report(self, timing_report: str) -> Optional[float]:
        """Extract WNS from timing report using shared parsing logic."""
        return parse_timing_summary(timing_report).setup_wns
    
    async def _call_vivado_for_clock(self, tool_name: str, arguments: dict) -> str:
        """Helper to call Vivado tools for clock period query."""
        return await self.call_vivado_tool(tool_name, arguments, timeout=60.0)
    
    async def fetch_clock_period(self) -> Optional[float]:
        """Query clock period with test-mode logging."""
        period = await super().get_clock_period(self._call_vivado_for_clock)
        if period is not None:
            clock_info = f" (target clock: {self.target_clock})" if self.target_clock else ""
            print(f"[TEST] Clock period: {period:.3f} ns{clock_info}")
        else:
            print("[TEST] WARNING: Could not parse clock period from Vivado")
        return period
    
    async def run_test(self, input_dcp: Path, output_dcp: Path, max_nets_to_optimize: int = 5) -> bool:
        """
        Run the deterministic test optimization flow.
        
        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado
        3. Get the critical high fan out nets from Vivado
        4. Open the DCP in RapidWright
        5. Apply the fanout optimization for each high fanout net
        6. Write a DCP out from RapidWright
        7. Read the RapidWright generated DCP into Vivado
        8. Route the design in Vivado
        9. Report timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print(f"Max nets to optimize: {max_nets_to_optimize}")
        print("="*70 + "\n")
        
        overall_start = time.time()
        
        try:
            # ================================================================
            # Step 0: Initialize RapidWright (Vivado starts automatically)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            
            # Initialize RapidWright (Vivado will auto-start when first used)
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")
            
            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            # Dump the initial diagnosis to the run dir (TODO.md item 1 — every flow)
            await self.write_initial_diagnosis(self.call_vivado_tool)

            # ================================================================
            # Step 2: Report timing in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report timing in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            
            # Get clock period for fmax calculation (also detects target clock)
            self.clock_period = await self.fetch_clock_period()
            
            # Get WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                self.initial_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Initial", self.initial_wns)
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            print()
            
            # ================================================================
            # Step 3: Get critical high fanout nets
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Get critical high fanout nets")
            print("-"*60)
            
            result = await self.call_vivado_tool("get_critical_high_fanout_nets", {
                "num_paths": 50,
                "min_fanout": 100,
                "exclude_clocks": True
            }, timeout=600.0)
            print(f"High fanout nets report:\n{result}")
            logger.info(f"High fanout nets: {result}")
            
            # Parse the nets
            self.high_fanout_nets = parse_high_fanout_nets(result)
            print(f"\nParsed {len(self.high_fanout_nets)} high fanout nets")
            
            if not self.high_fanout_nets:
                print("WARNING: No high fanout nets found to optimize!")
                logger.warning("No high fanout nets found to optimize")
            
            # Select top nets to optimize
            nets_to_optimize = self.high_fanout_nets[:max_nets_to_optimize]
            print(f"Will optimize {len(nets_to_optimize)} nets:")
            for net_name, fanout, path_count in nets_to_optimize:
                print(f"  - {net_name} (fanout={fanout}, paths={path_count})")
            
            # ================================================================
            # Step 4: Open the DCP in RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Open DCP in RapidWright")
            print("-"*60)
            
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")
            
            # ================================================================
            # Step 5: Apply fanout optimization for each high fanout net
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Apply fanout optimizations in RapidWright")
            print("-"*60)
            
            successful_optimizations = 0
            for i, (net_name, fanout, path_count) in enumerate(nets_to_optimize):
                print(f"\n[{i+1}/{len(nets_to_optimize)}] Optimizing net: {net_name}")
                print(f"    Fanout: {fanout}, Critical paths: {path_count}")
                
                # Calculate split factor: fanout/100, min 2, max 8
                split_factor = max(2, min(8, fanout // 100))
                print(f"    Split factor: {split_factor}")
                
                try:
                    result = await self.call_rapidwright_tool("optimize_fanout", {
                        "net_name": net_name,
                        "split_factor": split_factor
                    }, timeout=300.0)
                    print(f"    Result: {result[:500]}...")
                    logger.info(f"Optimize fanout {net_name}: {result}")
                    
                    # Check if successful
                    if "error" not in result.lower() or "success" in result.lower():
                        successful_optimizations += 1
                except Exception as e:
                    print(f"    FAILED: {e}")
                    logger.error(f"Failed to optimize {net_name}: {e}")
            
            print(f"\nSuccessfully optimized {successful_optimizations}/{len(nets_to_optimize)} nets")
            
            # ================================================================
            # Step 6: Write DCP from RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Write DCP from RapidWright")
            print("-"*60)
            
            rapidwright_dcp = Path(self.temp_dir) / "rapidwright_optimized.dcp"
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rapidwright_dcp),
                "overwrite": True
            }, timeout=600.0)
            print(f"Write checkpoint result:\n{result}")
            logger.info(f"RapidWright write checkpoint: {result}")
            
            # Check if the file was created
            if rapidwright_dcp.exists():
                print(f"DCP file created: {rapidwright_dcp} ({rapidwright_dcp.stat().st_size} bytes)")
            else:
                print("WARNING: DCP file was not created!")
                logger.warning("RapidWright DCP file not created")
            
            # ================================================================
            # Step 7: Read RapidWright DCP into Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Read RapidWright DCP into Vivado")
            print("-"*60)
            
            # Note: Opening a RapidWright-generated DCP takes MUCH longer than
            # opening the original DCP because:
            # 1. Vivado must reload encrypted IP blocks from disk
            # 2. Vivado must reconstruct internal data structures
            # For large designs, this can take 10-30 minutes
            RAPIDWRIGHT_DCP_TIMEOUT = 300.0  # 5 minutes
            
            # Check if there's a Tcl script we need to source first (for encrypted IP)
            tcl_script = rapidwright_dcp.with_suffix('.tcl')
            if tcl_script.exists():
                print(f"Found Tcl script for encrypted IP: {tcl_script}")
                print(f"Note: This may take 10-30 minutes for large designs...")
                # Source the Tcl script instead of directly opening the DCP
                result = await self.call_vivado_tool("run_tcl", {
                    "command": f"source {{{tcl_script}}}"
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Source Tcl script result:\n{result}")
            else:
                # Opening a RapidWright-generated DCP can take longer than original
                # because Vivado needs to reconstruct some internal data structures
                result = await self.call_vivado_tool("open_checkpoint", {
                    "dcp_path": str(rapidwright_dcp)
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Open RapidWright DCP result:\n{result}")
            logger.info(f"Open RapidWright DCP: {result}")
            
            # ================================================================
            # Step 8: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Route design in Vivado")
            print("-"*60)
            
            # First check route status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status before routing:\n{result[:1500]}...")
            logger.info(f"Route status before routing: {result}")
            
            # Route the design
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default",
            }, timeout=600.0)  # 2 hour timeout for routing
            print(f"Route design result:\n{result}")
            logger.info(f"Route design: {result}")
            
            # Check route status again
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")
            
            # ================================================================
            # Step 9: Report final timing
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Report final timing")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")
            
            # Get final WNS for the target clock domain
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                self.final_wns = self.parse_wns_from_timing_report(result)
            
            self.print_fmax_status("Final", self.final_wns)
            logger.info(f"Final WNS: {self.final_wns} ns")
            print()
            
            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)
            
            # Always write the final checkpoint (regardless of improvement)
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            
            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Nets optimized: {successful_optimizations}/{len(nets_to_optimize)}"
            )
            
            return True
            
        except Exception as e:
            logger.exception(f"Test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False
    

    async def run_test_vexriscv(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Cell re-placement optimization flow for VexRiscv.
        
        Mirrors the script in docs/optimization_example.md:
          Step 1 — Vivado baseline (open, get Fmax, extract critical path pins)
          Step 2 — RapidWright analysis (analyze_net_detour, filter candidates)
          Step 3 — RapidWright optimization (optimize_cell_placement, write DCP)
          Step 4 — Vivado verification (open optimized DCP, route, measure Fmax)
        """
        overall_start = time.time()
        
        try:
            # ==============================================================
            # Step 1: Vivado baseline
            # ==============================================================
            print("=" * 60)
            print("Step 1  Vivado baseline")
            print("=" * 60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            logger.info(f"Open checkpoint result: {result}")
            
            self.clock_period = await self.fetch_clock_period()
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.initial_wns = target_wns
            else:
                ts = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                self.initial_wns = self.parse_wns_from_timing_report(ts)
            
            baseline_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            print(f"  Clock period:   {self.clock_period} ns")
            print(f"  Baseline WNS:   {self.initial_wns} ns")
            if baseline_fmax is not None:
                print(f"  Baseline Fmax:  {baseline_fmax:.2f} MHz")
            
            pins_file = Path(self.temp_dir) / "critical_path_pins.json"
            result = await self.call_vivado_tool("extract_critical_path_pins", {
                "num_paths": 10,
                "output_file": str(pins_file)
            }, timeout=600.0)
            
            critical_paths = json.loads(Path(pins_file).read_text()) if pins_file.exists() else json.loads(result)
            print(f"  Extracted {len(critical_paths)} critical path pin lists")
            
            # ==============================================================
            # Step 2: RapidWright analysis
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 2  RapidWright analysis")
            print("=" * 60)
            
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            logger.info(f"RapidWright init: {result}")
            
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            logger.info(f"RapidWright read checkpoint: {result}")
            
            result = await self.call_rapidwright_tool("analyze_net_detour", {
                "input_file": str(pins_file),
                "detour_threshold": 2.0
            }, timeout=300.0)
            logger.info(f"analyze_net_detour: {result}")
            
            analysis = json.loads(result) if isinstance(result, str) else result
            if "error" in analysis:
                raise RuntimeError(f"analyze_net_detour failed: {analysis['error']}")
            candidates = analysis.get("candidates", [])
            print(f"  Cells analyzed: {analysis.get('cells_analyzed', '?')}")
            print(f"  Candidates (detour > 2.0): {len(candidates)}")
            for c in candidates[:5]:
                print(f"    {str(c['cell']):55s}  ratio={c['max_detour_ratio']}")
            
            if not candidates:
                print("\n  No candidates found — nothing to optimize")
                self.final_wns = self.initial_wns
                return True
            
            worst_path_cells = list(set(
                str(c["cell"]) for c in candidates if c.get("path", 0) <= 2
            ))
            if not worst_path_cells:
                worst_path_cells = [str(candidates[0]["cell"])]
            
            print(f"\n  Targeting {len(worst_path_cells)} cells on paths 1-2:")
            for name in worst_path_cells:
                print(f"    {name}")
            
            # ==============================================================
            # Step 3: RapidWright optimization
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 3  RapidWright optimization")
            print("=" * 60)
            
            result = await self.call_rapidwright_tool("optimize_cell_placement", {
                "cell_names": worst_path_cells
            }, timeout=300.0)
            logger.info(f"optimize_cell_placement: {result}")
            
            opt_result = json.loads(result) if isinstance(result, str) else result
            for r in opt_result.get("results", []):
                print(f"  {r['cell']}: {r['status']} — {r['message']}")
            
            rw_output = Path(self.temp_dir) / "vexriscv_rw_optimized.dcp"
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rw_output)
            }, timeout=600.0)
            print(f"  Wrote {rw_output.name}")
            
            # ==============================================================
            # Step 4: Vivado verification
            # ==============================================================
            print("\n" + "=" * 60)
            print("Step 4  Vivado verification")
            print("=" * 60)
            
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(rw_output)
            }, timeout=600.0)
            logger.info(f"Open optimized checkpoint: {result}")
            
            result = await self.call_vivado_tool("route_design", {
                "directive": "Default"
            }, timeout=3600.0)
            logger.info(f"Route design: {result}")
            
            route_result = await self.call_vivado_tool("report_route_status", {}, timeout=300.0)
            # One home for reading route status (route_bad_net_count); -1 keeps
            # this caller's existing "could not tell" sentinel.
            _bad = route_bad_net_count(route_result)
            error_count = _bad if _bad is not None else -1
            
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                self.final_wns = target_wns
            else:
                ts = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                self.final_wns = self.parse_wns_from_timing_report(ts)
            
            new_fmax = self.calculate_fmax(self.final_wns, self.clock_period)
            
            print(f"  Routing errors:  {error_count}")
            if baseline_fmax is not None and new_fmax is not None:
                print(f"  Baseline WNS:    {self.initial_wns} ns  →  Fmax {baseline_fmax:.2f} MHz")
                print(f"  Optimized WNS:   {self.final_wns} ns  →  Fmax {new_fmax:.2f} MHz")
                delta = new_fmax - baseline_fmax
                print(f"  Fmax improvement: {delta:+.2f} MHz")
            else:
                print(f"  Baseline WNS:  {self.initial_wns} ns")
                print(f"  Optimized WNS: {self.final_wns} ns")
            
            # Write final DCP
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            
            # Summary
            elapsed = time.time() - overall_start
            cells_info = ", ".join(worst_path_cells)
            self.print_test_summary(
                title="TEST SUMMARY - VEXRISCV CELL RE-PLACEMENT",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Cells re-placed: {cells_info}"
            )
            
            return True
            
        except Exception as e:
            logger.exception(f"VexRiscv test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def cleanup(self):
        """Clean up resources."""
        print("\n[TEST] Cleaning up...")
        await super().cleanup()
        print(f"[TEST] Run directory preserved at: {self.run_dir}")


# ---------------------------------------------------------------------------
# RapWrExplorer — non-LLM mode: configurable RapidWright strategy exploration
# ---------------------------------------------------------------------------

SUPPORTED_STRATEGIES = {
    "rwroute": "Full timing-driven re-routing via RapidWright RWRoute",
    "rwroute_non_timing": "Full non-timing-driven re-routing via RapidWright RWRoute",
    "rwroute_hus": "RWRoute timing-driven with Hybrid Updating Strategy (--hus)",
    "rwroute_lut_pin_swap": "RWRoute timing-driven with LUT pin swapping (--lutPinSwapping)",
    "rwroute_lut_routethru": "RWRoute timing-driven with LUT route-through (--lutRoutethru) Fails on some designs, use with caution",
    "router2": "Legacy Vivado-compatible timing-driven router (RapidWright Router2)",
}

DEFAULT_STRATEGIES = ["rwroute"]


class RapWrExplorer(DCPOptimizerBase):
    """
    Non-LLM optimization mode.

    Loads the design into Vivado (baseline timing) and RapidWright, applies
    a configurable list of RapidWright strategies, then re-routes in Vivado
    and reports timing improvement vs. baseline.
    """

    def __init__(self, strategies: list, debug: bool = False, run_dir: Optional[Path] = None):
        unknown = [s for s in strategies if s not in SUPPORTED_STRATEGIES]
        if unknown:
            raise ValueError(
                f"Unknown strategies: {unknown}. "
                f"Supported: {list(SUPPORTED_STRATEGIES)}"
            )
        super().__init__(debug=debug, run_dir=run_dir)
        self.strategies = strategies
        self.final_wns = None
        self.final_tns = None
        self.final_failing_endpoints = None

    # ------------------------------------------------------------------
    # Server startup
    # ------------------------------------------------------------------

    async def start_servers(self):
        await super().start_servers(log_prefix="[RAPWR_EXPLORE]")

    # ------------------------------------------------------------------
    # Tool-call helpers (mirror FPGAOptimizerTest with [RAPWR_EXPLORE] prefix)
    # ------------------------------------------------------------------

    async def call_vivado_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a Vivado MCP tool call with timing and logging."""
        logger.info(f"[VIVADO] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[RAPWR_EXPLORE] Calling vivado_{tool_name}...")
        start_time = time.time()
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            elapsed = time.time() - start_time
            logger.info(f"[VIVADO] {tool_name} completed in {elapsed:.2f}s")
            print(f"[RAPWR_EXPLORE] vivado_{tool_name} completed in {elapsed:.2f}s")
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"
            logger.info(f"[VIVADO] {tool_name} output:\n{result_text}")
            self._write_tool_log(f"vivado_{tool_name}", arguments, result_text)
            return result_text
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[RAPWR_EXPLORE] ERROR: vivado_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[RAPWR_EXPLORE] ERROR: vivado_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise

    async def call_rapidwright_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a RapidWright MCP tool call with timing and logging."""
        logger.info(f"[RAPIDWRIGHT] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[RAPWR_EXPLORE] Calling rapidwright_{tool_name}...")
        start_time = time.time()
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            elapsed = time.time() - start_time
            logger.info(f"[RAPIDWRIGHT] {tool_name} completed in {elapsed:.2f}s")
            print(f"[RAPWR_EXPLORE] rapidwright_{tool_name} completed in {elapsed:.2f}s")
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"
            logger.info(f"[RAPIDWRIGHT] {tool_name} output:\n{result_text}")
            self._write_tool_log(f"rapidwright_{tool_name}", arguments, result_text)
            return result_text
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[RAPWR_EXPLORE] ERROR: rapidwright_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[RAPWR_EXPLORE] ERROR: rapidwright_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise

    # ------------------------------------------------------------------
    # Clock / timing helpers
    # ------------------------------------------------------------------

    async def _call_vivado_for_clock(self, tool_name: str, arguments: dict) -> str:
        return await self.call_vivado_tool(tool_name, arguments, timeout=60.0)

    async def fetch_clock_period(self) -> Optional[float]:
        period = await super().get_clock_period(self._call_vivado_for_clock)
        if period is not None:
            clock_info = f" (target clock: {self.target_clock})" if self.target_clock else ""
            print(f"[RAPWR_EXPLORE] Clock period: {period:.3f} ns{clock_info}")
        else:
            print("[RAPWR_EXPLORE] WARNING: Could not parse clock period from Vivado")
        return period

    # ------------------------------------------------------------------
    # Results helpers
    # ------------------------------------------------------------------

    def _print_results_table(
        self,
        baseline_wns, baseline_tns, baseline_failing_ep,
        final_wns, final_tns, final_failing_ep,
        t_baseline, t_rapwr, t_vivado, t_total,
        strategy_timings, strategy_errors,
        output_dcp: Path,
    ) -> None:
        """Print the formatted rapwr_explore results table."""
        W = 70
        baseline_fmax = self.calculate_fmax(baseline_wns, self.clock_period)
        final_fmax    = self.calculate_fmax(final_wns,    self.clock_period)

        def fmt_float(v, decimals=3):
            return f"{v:.{decimals}f}" if v is not None else "N/A"

        def fmt_delta(a, b, decimals=3, invert=False):
            if a is None or b is None:
                return "N/A"
            d = b - a
            if invert:
                d = -d
            return f"{d:+.{decimals}f}"

        print("\n" + "=" * W)
        print(f"{'rapwr_explore Results':^{W}}")
        print("=" * W)
        print(f"  {'Metric':<22} {'Baseline':>12} {'Optimized':>12} {'Delta':>12}")
        print("  " + "-" * (W - 2))
        print(f"  {'WNS (ns)':<22} {fmt_float(baseline_wns):>12} {fmt_float(final_wns):>12} {fmt_delta(baseline_wns, final_wns):>12}")
        print(f"  {'TNS (ns)':<22} {fmt_float(baseline_tns):>12} {fmt_float(final_tns):>12} {fmt_delta(baseline_tns, final_tns):>12}")
        print(f"  {'Fmax (MHz)':<22} {fmt_float(baseline_fmax, 2):>12} {fmt_float(final_fmax, 2):>12} {fmt_delta(baseline_fmax, final_fmax, 2):>12}")
        print(f"  {'Failing EPs':<22} {str(baseline_failing_ep) if baseline_failing_ep is not None else 'N/A':>12} "
              f"{str(final_failing_ep) if final_failing_ep is not None else 'N/A':>12} "
              f"{fmt_delta(baseline_failing_ep, final_failing_ep, 0, invert=True) if baseline_failing_ep is not None and final_failing_ep is not None else 'N/A':>12}")
        print("=" * W)
        print(f"  {'Timing':}")
        print(f"  {'  Baseline (Vivado)':<28} {t_baseline:>8.1f} s")
        strat_detail = "  (" + ", ".join(
            f"{s}: {t:.1f}s{'  FAILED' if s in strategy_errors else ''}"
            for s, t in strategy_timings.items()
        ) + ")" if strategy_timings else ""
        print(f"  {'  RWRoute pass':<28} {t_rapwr:>8.1f} s{strat_detail}")
        print(f"  {'  Vivado re-route':<28} {t_vivado:>8.1f} s")
        print(f"  {'  Total wall time':<28} {t_total:>8.1f} s")
        print("=" * W)
        print(f"  Strategies:  {', '.join(self.strategies)}")
        print(f"  Output DCP:  {output_dcp}")
        print("=" * W)

    def _save_results_json(
        self,
        baseline_wns, baseline_tns, baseline_failing_ep,
        final_wns, final_tns, final_failing_ep,
        t_baseline, t_rapwr, t_vivado, t_total,
        strategy_timings, strategy_errors,
        output_dcp: Path,
    ) -> None:
        """Save results to token_usage.json in the run directory."""
        baseline_fmax = self.calculate_fmax(baseline_wns, self.clock_period)
        final_fmax    = self.calculate_fmax(final_wns,    self.clock_period)

        report = {
            "mode": "rapwr_explore",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_runtime_seconds": t_total,
                "strategies": self.strategies,
                "clock_period_ns": self.clock_period,
                "target_clock": self.target_clock,
                "baseline_wns": baseline_wns,
                "baseline_tns": baseline_tns,
                "baseline_failing_endpoints": baseline_failing_ep,
                "baseline_fmax_mhz": baseline_fmax,
                "final_wns": final_wns,
                "final_tns": final_tns,
                "final_failing_endpoints": final_failing_ep,
                "final_fmax_mhz": final_fmax,
                "wns_improvement": (final_wns - baseline_wns) if (final_wns is not None and baseline_wns is not None) else None,
                "fmax_improvement_mhz": (final_fmax - baseline_fmax) if (final_fmax is not None and baseline_fmax is not None) else None,
                "t_baseline_seconds": t_baseline,
                "t_rapwr_seconds": t_rapwr,
                "t_vivado_seconds": t_vivado,
                "strategy_timings": strategy_timings,
                "strategy_errors": strategy_errors,
            },
            "output_dcp": str(output_dcp),
        }

        report_path = self.run_dir / "token_usage.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info(f"[RAPWR_EXPLORE] Results saved to {report_path}")
        print(f"[RAPWR_EXPLORE] Results JSON: {report_path}")

    # ------------------------------------------------------------------
    # Strategy handlers — one method per SUPPORTED_STRATEGIES key
    # ------------------------------------------------------------------

    async def _strategy_rwroute(self) -> None:
        """RWRoute strategy: full timing-driven re-routing via RapidWright RWRoute."""
        print("[RAPWR_EXPLORE] Applying rwroute (RWRoute timing-driven re-route)...")
        result = await self.call_rapidwright_tool(
            "route_with_rwroute", {"timing_driven": True}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_rwroute result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_rwroute reported an error: {result[:300]}")

    async def _strategy_rwroute_non_timing(self) -> None:
        """RWRoute strategy: full non-timing-driven re-routing via RapidWright RWRoute."""
        print("[RAPWR_EXPLORE] Applying rwroute_non_timing (RWRoute non-timing-driven re-route)...")
        result = await self.call_rapidwright_tool(
            "route_with_rwroute", {"timing_driven": False}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_rwroute result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_rwroute reported an error: {result[:300]}")

    async def _strategy_rwroute_hus(self) -> None:
        """RWRoute strategy: timing-driven with Hybrid Updating Strategy (--hus)."""
        print("[RAPWR_EXPLORE] Applying rwroute_hus (RWRoute timing-driven + HUS)...")
        result = await self.call_rapidwright_tool(
            "route_with_rwroute", {"timing_driven": True, "hus": True}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_rwroute result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_rwroute reported an error: {result[:300]}")

    async def _strategy_rwroute_lut_pin_swap(self) -> None:
        """RWRoute strategy: timing-driven with LUT pin swapping (--lutPinSwapping)."""
        print("[RAPWR_EXPLORE] Applying rwroute_lut_pin_swap (RWRoute + --lutPinSwapping)...")
        result = await self.call_rapidwright_tool(
            "route_with_rwroute", {"timing_driven": True, "lut_pin_swapping": True}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_rwroute result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_rwroute reported an error: {result[:300]}")

    async def _strategy_rwroute_lut_routethru(self) -> None:
        """RWRoute strategy: timing-driven with LUT route-through (--lutRoutethru).

        Known issue: RapidWright may raise 'No mapped LCB to SitePinInst' for some
        designs due to gaps in the device model's LCB table. This is a RapidWright
        bug and not all designs are affected.
        """
        print("[RAPWR_EXPLORE] Applying rwroute_lut_routethru (RWRoute + --lutRoutethru)...")
        result = await self.call_rapidwright_tool(
            "route_with_rwroute", {"timing_driven": True, "lut_routethru": True}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_rwroute result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_rwroute reported an error: {result[:300]}")

    async def _strategy_router2(self) -> None:
        """Legacy Vivado-compatible timing-driven router (RapidWright Router2)."""
        print("[RAPWR_EXPLORE] Applying router2 (legacy Vivado-compatible timing-driven router)...")
        result = await self.call_rapidwright_tool(
            "route_with_router2", {}, timeout=3600.0
        )
        print(f"[RAPWR_EXPLORE] route_with_router2 result: {result[:400]}")
        if '"error"' in result:
            raise RuntimeError(f"route_with_router2 reported an error: {result[:300]}")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Full rapwr_explore flow:
          Phase 2 — baseline measurement in Vivado
          Phase 3 — RapidWright optimization pass  (TODO)
          Phase 4 — Vivado re-route + final timing  (TODO)
          Phase 5 — results report                  (TODO)
        """
        print("\n" + "=" * 70)
        print("RAPWR EXPLORE MODE")
        print("=" * 70)
        print(f"Input DCP:   {input_dcp}")
        print(f"Output DCP:  {output_dcp}")
        print(f"Strategies:  {', '.join(self.strategies)}")
        print(f"Run dir:     {self.run_dir}")
        print("=" * 70 + "\n")

        t_total_start = time.time()

        try:
            # ============================================================
            # Phase 1 — Baseline measurement (Vivado)
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 1: Baseline measurement in Vivado")
            print("-" * 60)

            t_baseline_start = time.time()

            # Step 1.0 — Initialize RapidWright JVM (needed even for Vivado-only
            #            steps so the RW session is live for Phase 3)
            print("\n[RAPWR_EXPLORE] Step 1.0: Initialize RapidWright JVM")
            rw_init_result = await self.call_rapidwright_tool(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            print(f"[RAPWR_EXPLORE] RapidWright init: {rw_init_result[:200]}")

            # Step 1.1 — Open input DCP in Vivado
            print("\n[RAPWR_EXPLORE] Step 1.1: Open input DCP in Vivado")
            await self.call_vivado_tool(
                "open_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                timeout=600.0,
            )

            # Step 1.2 — Timing summary → baseline WNS / TNS / failing endpoints
            print("\n[RAPWR_EXPLORE] Step 1.2: Report baseline timing")
            timing_report = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            print(f"[RAPWR_EXPLORE] Timing summary (first 2000 chars):\n{timing_report[:2000]}")

            parsed = parse_timing_summary(timing_report)
            baseline_wns = parsed.setup_wns
            baseline_tns = parsed.setup_tns
            baseline_failing_ep = parsed.setup_failing_endpoints

            # Dump the initial diagnosis to the run dir (TODO.md item 1 — every flow)
            await self.write_initial_diagnosis(self.call_vivado_tool)

            # Step 2.3 — Detect clock period (also sets self.target_clock)
            self.clock_period = await self.fetch_clock_period()

            # Prefer target-clock-specific WNS when available
            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                baseline_wns = target_wns

            self.initial_wns = baseline_wns
            self.initial_tns = baseline_tns
            self.initial_failing_endpoints = baseline_failing_ep

            t_baseline_end = time.time()
            t_baseline = t_baseline_end - t_baseline_start

            self.print_fmax_status("Baseline", baseline_wns)
            print(f"[RAPWR_EXPLORE] Baseline TNS:             {baseline_tns} ns")
            print(f"[RAPWR_EXPLORE] Baseline failing EPs:     {baseline_failing_ep}")
            print(f"[RAPWR_EXPLORE] Baseline phase wall time: {t_baseline:.1f} s")

            # ============================================================
            # Phase 2 — RapidWright optimization pass
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 2: RapidWright optimization pass")
            print("-" * 60)

            # Step 2.1 — Load DCP into RapidWright
            print("\n[RAPWR_EXPLORE] Step 2.1: Load input DCP into RapidWright")
            await self.call_rapidwright_tool(
                "read_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                timeout=600.0,
            )

            # Step 2.2 — Run each strategy in order; log per-strategy timing
            t_rapwr_start = time.time()
            strategy_timings = {}
            strategy_errors = {}

            for strategy in self.strategies:
                handler = getattr(self, f"_strategy_{strategy.replace('-', '_')}", None)
                if handler is None:
                    strategy_errors[strategy] = "No handler method found"
                    print(f"[RAPWR_EXPLORE] WARNING: no handler for strategy '{strategy}', skipping")
                    continue

                print(f"\n[RAPWR_EXPLORE] --- Strategy: {strategy} ---")
                t_strat = time.time()
                try:
                    await handler()
                    strategy_timings[strategy] = time.time() - t_strat
                    print(f"[RAPWR_EXPLORE] Strategy '{strategy}' finished in {strategy_timings[strategy]:.1f}s")
                except Exception as e:
                    strategy_timings[strategy] = time.time() - t_strat
                    strategy_errors[strategy] = str(e)
                    logger.exception(f"[RAPWR_EXPLORE] Strategy '{strategy}' failed: {e}")
                    print(f"[RAPWR_EXPLORE] Strategy '{strategy}' FAILED in {strategy_timings[strategy]:.1f}s: {e}")

            t_rapwr_end = time.time()
            t_rapwr = t_rapwr_end - t_rapwr_start

            # Step 2.3 — Write optimized DCP from RapidWright
            intermediate_dcp = self.run_dir / "rapwr_optimized.dcp"
            print(f"\n[RAPWR_EXPLORE] Step 2.3: Write optimized DCP → {intermediate_dcp}")
            await self.call_rapidwright_tool(
                "write_checkpoint",
                {"dcp_path": str(intermediate_dcp), "overwrite": True},
                timeout=300.0,
            )

            print(f"\n[RAPWR_EXPLORE] RapidWright pass total: {t_rapwr:.1f}s")
            for s, t in strategy_timings.items():
                status = "FAILED" if s in strategy_errors else "OK"
                print(f"[RAPWR_EXPLORE]   {s}: {t:.1f}s  [{status}]")

            # ============================================================
            # Phase 3 — Vivado final timing
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 3: Vivado final timing")
            print("-" * 60)

            t_vivado_start = time.time()

            # Step 3.1 — Open the RWRoute-optimized DCP in Vivado
            print(f"\n[RAPWR_EXPLORE] Step 3.1: Open optimized DCP in Vivado ({intermediate_dcp.name})")
            await self.call_vivado_tool(
                "open_checkpoint",
                {"dcp_path": str(intermediate_dcp)},
                timeout=600.0,
            )

            # Step 3.2 — Final timing report
            print("\n[RAPWR_EXPLORE] Step 3.2: Report final timing")
            final_timing_report = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            print(f"[RAPWR_EXPLORE] Final timing summary (first 2000 chars):\n{final_timing_report[:2000]}")

            final_parsed = parse_timing_summary(final_timing_report)
            self.final_wns = final_parsed.setup_wns
            self.final_tns = final_parsed.setup_tns
            self.final_failing_endpoints = final_parsed.setup_failing_endpoints

            # Prefer target-clock-specific WNS
            final_target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if final_target_wns is not None:
                self.final_wns = final_target_wns

            # Step 3.3 — Save final output DCP
            print(f"\n[RAPWR_EXPLORE] Step 3.3: Write final DCP → {output_dcp}")
            await self.call_vivado_tool(
                "write_checkpoint",
                {"dcp_path": str(output_dcp), "force": True},
                timeout=300.0,
            )

            t_vivado_end = time.time()
            t_vivado = t_vivado_end - t_vivado_start
            t_total = time.time() - t_total_start

            # ============================================================
            # Phase 5 — Results summary
            # ============================================================
            self._print_results_table(
                baseline_wns, baseline_tns, baseline_failing_ep,
                self.final_wns, self.final_tns, self.final_failing_endpoints,
                t_baseline, t_rapwr, t_vivado, t_total,
                strategy_timings, strategy_errors,
                output_dcp,
            )
            self._save_results_json(
                baseline_wns, baseline_tns, baseline_failing_ep,
                self.final_wns, self.final_tns, self.final_failing_endpoints,
                t_baseline, t_rapwr, t_vivado, t_total,
                strategy_timings, strategy_errors,
                output_dcp,
            )

            return self.final_wns is not None

        except Exception as e:
            logger.exception(f"[RAPWR_EXPLORE] run() failed: {e}")
            print(f"\n[RAPWR_EXPLORE] ERROR: {e}")
            return False

    async def cleanup(self):
        print("\n[RAPWR_EXPLORE] Cleaning up...")
        await super().cleanup()
        print(f"[RAPWR_EXPLORE] Run directory preserved at: {self.run_dir}")


async def run_rapwr_explore(
    input_dcp: Path,
    output_dcp: Path,
    strategies: list,
    debug: bool = False,
    run_dir: Optional[Path] = None,
) -> int:
    """Launch RapWrExplorer and return an exit code."""
    explorer = RapWrExplorer(strategies=strategies, debug=debug, run_dir=run_dir)
    try:
        await explorer.start_servers()
        success = await explorer.run(input_dcp, output_dcp)
        if success:
            print("\n[RAPWR_EXPLORE] Exploration completed successfully")
            print(f"[RAPWR_EXPLORE]   Output DCP:    {output_dcp}")
            print(f"[RAPWR_EXPLORE]   Run directory: {explorer.run_dir}")
            return 0
        else:
            print("\n[RAPWR_EXPLORE] Exploration did not complete successfully")
            print(f"[RAPWR_EXPLORE]   Run directory: {explorer.run_dir}")
            return 1
    except KeyboardInterrupt:
        print("\n[RAPWR_EXPLORE] Interrupted by user")
        print(f"[RAPWR_EXPLORE]   Run directory: {explorer.run_dir}")
        return 130
    except Exception as e:
        logger.exception(f"rapwr_explore fatal error: {e}")
        print(f"\n[RAPWR_EXPLORE] Fatal error: {e}")
        print(f"[RAPWR_EXPLORE]   Run directory: {explorer.run_dir}")
        return 1
    finally:
        await explorer.cleanup()


# ---------------------------------------------------------------------------
# VivadoExplorer — non-LLM mode: configurable Vivado-only strategy exploration
# ---------------------------------------------------------------------------

VIVADO_SUPPORTED_STRATEGIES = {
    "phys_opt":               "phys_opt_design (default, no directive)",
    "phys_opt_explore":       "phys_opt_design -directive Explore",
    "phys_opt_aggressive":    "phys_opt_design -directive AggressiveExplore",
    "phys_opt_runtime":       "phys_opt_design -directive RuntimeOptimized",
    "phys_opt_alt_replication": "phys_opt_design -directive AlternateReplication",
    "phys_opt_fanout":        "phys_opt_design -directive AggressiveFanoutOpt",
    "phys_opt_retime":        "phys_opt_design -directive AddRetime",
    "phys_opt_alt_flow_retime": "phys_opt_design -directive AlternateFlowWithRetiming",
    "phys_opt_rqs":           "phys_opt_design -directive RQS",
    "route_default":          "route_design -directive Default",
    "place_route":            "Full re-placement and re-routing (place_design + route_design)",
}
    # "route_delay":            "route_design -delay (delay-driven routing mode)", Commented since it needs (ERROR: [Common 17-69] Command failed: "-delay" and "-auto_delay" can be used only with "-nets" and "-pins")

VIVADO_DEFAULT_STRATEGIES = ["phys_opt"]


class VivadoExplorer(DCPOptimizerBase):
    """
    Non-LLM Vivado-only optimization mode.

    Loads the design into Vivado, applies a configurable list of Vivado
    optimization strategies within the same session, then reports timing
    improvement vs. baseline.  No RapidWright session is started.
    """

    def __init__(self, strategies: list, debug: bool = False, run_dir: Optional[Path] = None):
        unknown = [s for s in strategies if s not in VIVADO_SUPPORTED_STRATEGIES]
        if unknown:
            raise ValueError(
                f"Unknown Vivado strategies: {unknown}. "
                f"Supported: {list(VIVADO_SUPPORTED_STRATEGIES)}"
            )
        super().__init__(debug=debug, run_dir=run_dir)
        self.strategies = strategies
        self.final_wns = None
        self.final_tns = None
        self.final_failing_endpoints = None

    # ------------------------------------------------------------------
    # Server startup — Vivado only, no RapidWright
    # ------------------------------------------------------------------

    async def start_servers(self):
        """Start only the Vivado MCP server."""
        script_dir = Path(__file__).parent.resolve()

        vivado_log     = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"
        vivado_mcp_log = self.run_dir / "vivado-mcp.log"

        self._tool_logs_dir = self.run_dir / "tool_outputs"
        self._tool_logs_dir.mkdir(exist_ok=True)
        self._prompt_logs_dir = self.run_dir / "llm_prompts"
        self._prompt_logs_dir.mkdir(exist_ok=True)

        if self.debug:
            self._v_log_file = None
            print("[VIVADO_EXPLORE] Debug mode: Vivado MCP output shown in console")
        else:
            self._v_log_file = open(vivado_mcp_log, "w")
            print(f"[VIVADO_EXPLORE] Log files: {vivado_log.name}, {vivado_journal.name}, "
                  f"{vivado_mcp_log.name}, tool_outputs/")

        vivado_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            vivado_args.extend([
                "--vivado-log",     str(vivado_log),
                "--vivado-journal", str(vivado_journal),
            ])

        vivado_params = StdioServerParameters(
            command=sys.executable,
            args=vivado_args,
            cwd=str(self.run_dir),
            env={**os.environ},
        )

        print("[VIVADO_EXPLORE] Starting Vivado MCP server...")
        t0 = time.time()
        vivado_transport = await self.exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=self._v_log_file)
        )
        v_read, v_write = vivado_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await self.vivado_session.initialize()
        print(f"[VIVADO_EXPLORE] Vivado MCP server started in {time.time() - t0:.2f}s")

    # ------------------------------------------------------------------
    # Tool-call helper
    # ------------------------------------------------------------------

    async def call_vivado_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a Vivado MCP tool call with timing and logging."""
        logger.info(f"[VIVADO] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[VIVADO_EXPLORE] Calling vivado_{tool_name}...")
        start_time = time.time()
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            elapsed = time.time() - start_time
            logger.info(f"[VIVADO] {tool_name} completed in {elapsed:.2f}s")
            print(f"[VIVADO_EXPLORE] vivado_{tool_name} completed in {elapsed:.2f}s")
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, "text")]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"
            logger.info(f"[VIVADO] {tool_name} output:\n{result_text}")
            self._write_tool_log(f"vivado_{tool_name}", arguments, result_text)
            return result_text
        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[VIVADO_EXPLORE] ERROR: vivado_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[VIVADO_EXPLORE] ERROR: vivado_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise

    # ------------------------------------------------------------------
    # Clock / timing helpers
    # ------------------------------------------------------------------

    async def _call_vivado_for_clock(self, tool_name: str, arguments: dict) -> str:
        return await self.call_vivado_tool(tool_name, arguments, timeout=60.0)

    async def fetch_clock_period(self) -> Optional[float]:
        period = await super().get_clock_period(self._call_vivado_for_clock)
        if period is not None:
            clock_info = f" (target clock: {self.target_clock})" if self.target_clock else ""
            print(f"[VIVADO_EXPLORE] Clock period: {period:.3f} ns{clock_info}")
        else:
            print("[VIVADO_EXPLORE] WARNING: Could not parse clock period from Vivado")
        return period

    # ------------------------------------------------------------------
    # Strategy handlers
    # ------------------------------------------------------------------

    async def _strategy_phys_opt(self) -> None:
        """Default phys_opt_design (post-route)."""
        print("[VIVADO_EXPLORE] Applying phys_opt (default)...")
        await self.call_vivado_tool("phys_opt_design", {}, timeout=3600.0)

    async def _strategy_phys_opt_explore(self) -> None:
        """phys_opt_design -directive Explore."""
        print("[VIVADO_EXPLORE] Applying phys_opt_explore (directive=Explore)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "Explore"}, timeout=3600.0
        )

    async def _strategy_phys_opt_aggressive(self) -> None:
        """phys_opt_design -directive AggressiveExplore."""
        print("[VIVADO_EXPLORE] Applying phys_opt_aggressive (directive=AggressiveExplore)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "AggressiveExplore"}, timeout=3600.0
        )

    async def _strategy_phys_opt_runtime(self) -> None:
        """phys_opt_design -directive RuntimeOptimized."""
        print("[VIVADO_EXPLORE] Applying phys_opt_runtime (directive=RuntimeOptimized)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "RuntimeOptimized"}, timeout=3600.0
        )

    async def _strategy_phys_opt_alt_replication(self) -> None:
        """phys_opt_design -directive AlternateReplication."""
        print("[VIVADO_EXPLORE] Applying phys_opt_alt_replication (directive=AlternateReplication)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "AlternateReplication"}, timeout=3600.0
        )

    async def _strategy_phys_opt_fanout(self) -> None:
        """phys_opt_design -directive AggressiveFanoutOpt."""
        print("[VIVADO_EXPLORE] Applying phys_opt_fanout (directive=AggressiveFanoutOpt)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "AggressiveFanoutOpt"}, timeout=3600.0
        )

    async def _strategy_phys_opt_retime(self) -> None:
        """phys_opt_design -directive AddRetime."""
        print("[VIVADO_EXPLORE] Applying phys_opt_retime (directive=AddRetime)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "AddRetime"}, timeout=3600.0
        )

    async def _strategy_phys_opt_alt_flow_retime(self) -> None:
        """phys_opt_design -directive AlternateFlowWithRetiming."""
        print("[VIVADO_EXPLORE] Applying phys_opt_alt_flow_retime (directive=AlternateFlowWithRetiming)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "AlternateFlowWithRetiming"}, timeout=3600.0
        )

    async def _strategy_phys_opt_rqs(self) -> None:
        """phys_opt_design -directive RQS."""
        print("[VIVADO_EXPLORE] Applying phys_opt_rqs (directive=RQS)...")
        await self.call_vivado_tool(
            "phys_opt_design", {"directive": "RQS"}, timeout=3600.0
        )

    async def _strategy_route_default(self) -> None:
        """route_design -directive Default."""
        print("[VIVADO_EXPLORE] Applying route_default (directive=Default)...")
        await self.call_vivado_tool(
            "route_design", {"directive": "Default"}, timeout=3600.0
        )

    async def _strategy_route_delay(self) -> None:
        """route_design -delay (delay-driven routing mode)."""
        print("[VIVADO_EXPLORE] Applying route_delay (route_design -delay)...")
        await self.call_vivado_tool(
            "route_design", {"delay": True}, timeout=3600.0
        )

    async def _strategy_place_route(self) -> None:
        """Full re-placement followed by re-routing."""
        print("[VIVADO_EXPLORE] Applying place_route (place_design + route_design)...")
        await self.call_vivado_tool("place_design",  {}, timeout=3600.0)
        await self.call_vivado_tool("route_design",  {}, timeout=3600.0)

    # ------------------------------------------------------------------
    # Results helpers (same structure as RapWrExplorer)
    # ------------------------------------------------------------------

    def _print_results_table(
        self,
        baseline_wns, baseline_tns, baseline_failing_ep,
        final_wns, final_tns, final_failing_ep,
        t_baseline, t_strategies, t_total,
        strategy_timings, strategy_errors,
        output_dcp: Path,
    ) -> None:
        W = 70
        baseline_fmax = self.calculate_fmax(baseline_wns, self.clock_period)
        final_fmax    = self.calculate_fmax(final_wns,    self.clock_period)

        def fmt_float(v, d=3):
            return f"{v:.{d}f}" if v is not None else "N/A"

        def fmt_delta(a, b, d=3, invert=False):
            if a is None or b is None:
                return "N/A"
            delta = (b - a) if not invert else (a - b)
            return f"{delta:+.{d}f}"

        print("\n" + "=" * W)
        print(f"{'vivado_explore Results':^{W}}")
        print("=" * W)
        print(f"  {'Metric':<22} {'Baseline':>12} {'Optimized':>12} {'Delta':>12}")
        print("  " + "-" * (W - 2))
        print(f"  {'WNS (ns)':<22} {fmt_float(baseline_wns):>12} {fmt_float(final_wns):>12} {fmt_delta(baseline_wns, final_wns):>12}")
        print(f"  {'TNS (ns)':<22} {fmt_float(baseline_tns):>12} {fmt_float(final_tns):>12} {fmt_delta(baseline_tns, final_tns):>12}")
        print(f"  {'Fmax (MHz)':<22} {fmt_float(baseline_fmax, 2):>12} {fmt_float(final_fmax, 2):>12} {fmt_delta(baseline_fmax, final_fmax, 2):>12}")
        ep_b = str(baseline_failing_ep) if baseline_failing_ep is not None else "N/A"
        ep_f = str(final_failing_ep)    if final_failing_ep    is not None else "N/A"
        ep_d = fmt_delta(baseline_failing_ep, final_failing_ep, 0, invert=True) \
               if baseline_failing_ep is not None and final_failing_ep is not None else "N/A"
        print(f"  {'Failing EPs':<22} {ep_b:>12} {ep_f:>12} {ep_d:>12}")
        print("=" * W)
        print("  Timing")
        print(f"  {'  Baseline (Vivado)':<28} {t_baseline:>8.1f} s")
        strat_detail = "  (" + ", ".join(
            f"{s}: {t:.1f}s{'  FAILED' if s in strategy_errors else ''}"
            for s, t in strategy_timings.items()
        ) + ")" if strategy_timings else ""
        print(f"  {'  Strategies pass':<28} {t_strategies:>8.1f} s{strat_detail}")
        print(f"  {'  Total wall time':<28} {t_total:>8.1f} s")
        print("=" * W)
        print(f"  Strategies:  {', '.join(self.strategies)}")
        print(f"  Output DCP:  {output_dcp}")
        print("=" * W)

    def _save_results_json(
        self,
        baseline_wns, baseline_tns, baseline_failing_ep,
        final_wns, final_tns, final_failing_ep,
        t_baseline, t_strategies, t_total,
        strategy_timings, strategy_errors,
        output_dcp: Path,
    ) -> None:
        baseline_fmax = self.calculate_fmax(baseline_wns, self.clock_period)
        final_fmax    = self.calculate_fmax(final_wns,    self.clock_period)
        report = {
            "mode": "vivado_explore",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_runtime_seconds": t_total,
                "strategies": self.strategies,
                "clock_period_ns": self.clock_period,
                "target_clock": self.target_clock,
                "baseline_wns": baseline_wns,
                "baseline_tns": baseline_tns,
                "baseline_failing_endpoints": baseline_failing_ep,
                "baseline_fmax_mhz": baseline_fmax,
                "final_wns": final_wns,
                "final_tns": final_tns,
                "final_failing_endpoints": final_failing_ep,
                "final_fmax_mhz": final_fmax,
                "wns_improvement": (final_wns - baseline_wns) if (final_wns is not None and baseline_wns is not None) else None,
                "fmax_improvement_mhz": (final_fmax - baseline_fmax) if (final_fmax is not None and baseline_fmax is not None) else None,
                "t_baseline_seconds": t_baseline,
                "t_strategies_seconds": t_strategies,
                "strategy_timings": strategy_timings,
                "strategy_errors": strategy_errors,
            },
            "output_dcp": str(output_dcp),
        }
        report_path = self.run_dir / "token_usage.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info(f"[VIVADO_EXPLORE] Results saved to {report_path}")
        print(f"[VIVADO_EXPLORE] Results JSON: {report_path}")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, input_dcp: Path, output_dcp: Path) -> bool:
        print("\n" + "=" * 70)
        print("VIVADO EXPLORE MODE")
        print("=" * 70)
        print(f"Input DCP:   {input_dcp}")
        print(f"Output DCP:  {output_dcp}")
        print(f"Strategies:  {', '.join(self.strategies)}")
        print(f"Run dir:     {self.run_dir}")
        print("=" * 70 + "\n")

        t_total_start = time.time()

        try:
            # ============================================================
            # Phase 1 — Baseline measurement
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 1: Baseline measurement in Vivado")
            print("-" * 60)

            t_baseline_start = time.time()

            print("\n[VIVADO_EXPLORE] Step 1.1: Open input DCP in Vivado")
            await self.call_vivado_tool(
                "open_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                timeout=600.0,
            )

            print("\n[VIVADO_EXPLORE] Step 1.2: Report baseline timing")
            timing_report = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            print(f"[VIVADO_EXPLORE] Timing summary (first 2000 chars):\n{timing_report[:2000]}")

            parsed = parse_timing_summary(timing_report)
            baseline_wns       = parsed.setup_wns
            baseline_tns       = parsed.setup_tns
            baseline_failing_ep = parsed.setup_failing_endpoints

            # Dump the initial diagnosis to the run dir (TODO.md item 1 — every flow)
            await self.write_initial_diagnosis(self.call_vivado_tool)

            self.clock_period = await self.fetch_clock_period()

            target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if target_wns is not None:
                baseline_wns = target_wns

            self.initial_wns = baseline_wns
            self.initial_tns = baseline_tns
            self.initial_failing_endpoints = baseline_failing_ep

            t_baseline = time.time() - t_baseline_start

            self.print_fmax_status("Baseline", baseline_wns)
            print(f"[VIVADO_EXPLORE] Baseline TNS:             {baseline_tns} ns")
            print(f"[VIVADO_EXPLORE] Baseline failing EPs:     {baseline_failing_ep}")
            print(f"[VIVADO_EXPLORE] Baseline phase wall time: {t_baseline:.1f} s")

            # ============================================================
            # Phase 2 — Vivado strategy pass
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 2: Vivado optimization strategies")
            print("-" * 60)

            t_strat_start = time.time()
            strategy_timings = {}
            strategy_errors  = {}

            for strategy in self.strategies:
                handler = getattr(self, f"_strategy_{strategy.replace('-', '_')}", None)
                if handler is None:
                    strategy_errors[strategy] = "No handler method found"
                    print(f"[VIVADO_EXPLORE] WARNING: no handler for strategy '{strategy}', skipping")
                    continue

                print(f"\n[VIVADO_EXPLORE] --- Strategy: {strategy} ---")
                t0 = time.time()
                try:
                    await handler()
                    strategy_timings[strategy] = time.time() - t0
                    print(f"[VIVADO_EXPLORE] Strategy '{strategy}' finished in {strategy_timings[strategy]:.1f}s")
                except Exception as e:
                    strategy_timings[strategy] = time.time() - t0
                    strategy_errors[strategy]  = str(e)
                    logger.exception(f"[VIVADO_EXPLORE] Strategy '{strategy}' failed: {e}")
                    print(f"[VIVADO_EXPLORE] Strategy '{strategy}' FAILED in {strategy_timings[strategy]:.1f}s: {e}")

            t_strategies = time.time() - t_strat_start
            print(f"\n[VIVADO_EXPLORE] Strategy pass total: {t_strategies:.1f}s")
            for s, t in strategy_timings.items():
                status = "FAILED" if s in strategy_errors else "OK"
                print(f"[VIVADO_EXPLORE]   {s}: {t:.1f}s  [{status}]")

            # ============================================================
            # Phase 3 — Final timing + write output DCP
            # ============================================================
            print("\n" + "-" * 60)
            print("PHASE 4: Final timing and output DCP")
            print("-" * 60)

            print("\n[VIVADO_EXPLORE] Step 4.1: Report final timing")
            final_timing_report = await self.call_vivado_tool(
                "report_timing_summary", {}, timeout=300.0
            )
            print(f"[VIVADO_EXPLORE] Final timing (first 2000 chars):\n{final_timing_report[:2000]}")

            final_parsed = parse_timing_summary(final_timing_report)
            self.final_wns               = final_parsed.setup_wns
            self.final_tns               = final_parsed.setup_tns
            self.final_failing_endpoints = final_parsed.setup_failing_endpoints

            final_target_wns = await self.get_wns_for_target_clock(self._call_vivado_for_clock)
            if final_target_wns is not None:
                self.final_wns = final_target_wns

            print(f"\n[VIVADO_EXPLORE] Step 4.2: Write final DCP → {output_dcp}")
            await self.call_vivado_tool(
                "write_checkpoint",
                {"dcp_path": str(output_dcp), "force": True},
                timeout=300.0,
            )

            t_total = time.time() - t_total_start
            self.print_fmax_status("Final", self.final_wns)

            # ============================================================
            # Phase 5 — Results
            # ============================================================
            self._print_results_table(
                baseline_wns, baseline_tns, baseline_failing_ep,
                self.final_wns, self.final_tns, self.final_failing_endpoints,
                t_baseline, t_strategies, t_total,
                strategy_timings, strategy_errors,
                output_dcp,
            )
            self._save_results_json(
                baseline_wns, baseline_tns, baseline_failing_ep,
                self.final_wns, self.final_tns, self.final_failing_endpoints,
                t_baseline, t_strategies, t_total,
                strategy_timings, strategy_errors,
                output_dcp,
            )

            return self.final_wns is not None

        except Exception as e:
            logger.exception(f"[VIVADO_EXPLORE] run() failed: {e}")
            print(f"\n[VIVADO_EXPLORE] ERROR: {e}")
            return False

    async def cleanup(self):
        print("\n[VIVADO_EXPLORE] Cleaning up...")
        await super().cleanup()
        print(f"[VIVADO_EXPLORE] Run directory preserved at: {self.run_dir}")


async def run_vivado_explore(
    input_dcp: Path,
    output_dcp: Path,
    strategies: list,
    debug: bool = False,
    run_dir: Optional[Path] = None,
) -> int:
    """Launch VivadoExplorer and return an exit code."""
    explorer = VivadoExplorer(strategies=strategies, debug=debug, run_dir=run_dir)
    try:
        await explorer.start_servers()
        success = await explorer.run(input_dcp, output_dcp)
        if success:
            print("\n[VIVADO_EXPLORE] Exploration completed successfully")
            print(f"[VIVADO_EXPLORE]   Output DCP:    {output_dcp}")
            print(f"[VIVADO_EXPLORE]   Run directory: {explorer.run_dir}")
            return 0
        else:
            print("\n[VIVADO_EXPLORE] Exploration did not complete successfully")
            print(f"[VIVADO_EXPLORE]   Run directory: {explorer.run_dir}")
            return 1
    except KeyboardInterrupt:
        print("\n[VIVADO_EXPLORE] Interrupted by user")
        print(f"[VIVADO_EXPLORE]   Run directory: {explorer.run_dir}")
        return 130
    except Exception as e:
        logger.exception(f"vivado_explore fatal error: {e}")
        print(f"\n[VIVADO_EXPLORE] Fatal error: {e}")
        print(f"[VIVADO_EXPLORE]   Run directory: {explorer.run_dir}")
        return 1
    finally:
        await explorer.cleanup()


async def run_test_mode(input_dcp: Path, output_dcp: Path, debug: bool = False, max_nets: int = 5, run_dir: Optional[Path] = None):
    """Run the test mode optimization.
    
    Detects which example DCP is being used and applies the appropriate optimization flow:
    - vexriscv_re-place: Cell re-placement flow (same recipe as docs/optimization_example.md)
    """
    # Detect which DCP is being used based on filename
    dcp_name = input_dcp.name.lower()
    
    if "vexriscv" in dcp_name:
        print(f"[TEST] Detected VexRiscv design - using cell re-placement flow")
    else:
        print(f"\n[TEST] ERROR: Unsupported DCP file: {input_dcp.name}")
        print(f"[TEST] Test mode supports these benchmark DCPs:")
        print(f"[TEST]   - fpl26_contest_benchmarks/vexriscv_re-place_2025.1.dcp")
        print(f"[TEST]")
        print(f"[TEST] For custom DCPs, run without --test to use the LLM-guided optimizer.")
        return 1
    
    tester = FPGAOptimizerTest(debug=debug, run_dir=run_dir)
    
    try:
        await tester.start_servers()
        
        success = await tester.run_test_vexriscv(input_dcp, output_dcp)
        
        if success:
            print("\n[TEST] Test completed successfully")
            print(f"\n[TEST] Output files:")
            print(f"[TEST]   Optimized DCP: {output_dcp}")
            print(f"[TEST]   Run directory: {tester.run_dir}")
            return 0
        else:
            print("\n[TEST] Test failed")
            print(f"[TEST] Run directory: {tester.run_dir}")
            return 1
            
    except KeyboardInterrupt:
        print("\n[TEST] Interrupted by user")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 130
    except Exception as e:
        logger.exception(f"Test mode fatal error: {e}")
        print(f"\n[TEST] Fatal error: {e}")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 1
    finally:
        await tester.cleanup()

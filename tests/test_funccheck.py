"""Pure tests for pramana.funccheck's static scan and helpers (no Vivado)."""

from pramana.funccheck import (build_testbench, edits_netlist, is_quiet_port, reset_polarity,
                               scan_netlist_edits)


def test_vivado_signoff_commands_are_not_edits():
    tcl = """
    place_design -unplace
    place_design -directive Explore
    phys_opt_design -directive AggressiveExplore
    route_design -directive Explore -tns_cleanup
    opt_design -directive Explore
    set_clock_uncertainty -setup 0.1 [get_clocks *]
    foreach n [get_nets -of [get_timing_paths -max_paths 10]] { route_design -nets $n }
    set_property IS_LOC_FIXED 0 [get_cells -hier]
    set_property -dict {LOC SLICE_X1Y2 BEL B6LUT} [get_cells u0]
    set_property MAX_FANOUT 8 [get_nets n1]
    unplace_cell [get_cells a]; place_cell a SLICE_X0Y0/AFF
    report_timing_summary  ;# create_cell in a comment
    # remove_cell [get_cells x]
    puts "done"
    """
    assert scan_netlist_edits(tcl) == []
    assert not edits_netlist(tcl)


def test_vivado_opt_can_be_made_strict():
    assert edits_netlist("phys_opt_design -retime", include_vivado_opt=True)
    assert edits_netlist("opt_design -remap", include_vivado_opt=True)
    assert not edits_netlist("place_design", include_vivado_opt=True)


def test_direct_netlist_edits_are_found():
    cases = {
        "create_cell -reference LUT1 u_new": "create_cell",
        "remove_cell [get_cells u_old]": "remove_cell",
        "connect_net -net n1 -objects [get_pins u/I0]": "connect_net",
        "disconnect_net -net n1 -objects {u/I0}": "disconnect_net",
        "create_net n2": "create_net",
        "rename_cell -to b a": "rename_cell",
        "tie_unused_pins": "tie_unused_pins",
        "insert_buffer [get_nets n] BUFG": "insert_buffer",
        "if {1} { ::remove_net [get_nets x] }": "remove_net",
        "set c [get_cells u]; set_property INIT 64'hDEAD $c": "set_property:INIT",
        "set_property -dict {INIT 4'h6 LOC SLICE_X0Y0} [get_cells u]": "set_property:INIT",
        "set_property IS_C_INVERTED 1 [get_cells ff]": "set_property:IS_C_INVERTED",
        "set_property INIT_00 256'h0 [get_cells ram]": "set_property:INIT_00",
        "set_property $p 1 [get_cells u]": "set_property:<dynamic>",
        "eval $cmd": "dynamic:eval",
        "$cmd -net n -objects p": "dynamic:$cmd",
    }
    for tcl, why in cases.items():
        found = scan_netlist_edits(tcl)
        assert why in found, (tcl, found)
        assert edits_netlist(tcl)


def test_lookalike_words_are_not_edits():
    assert not edits_netlist("get_cells -filter {NAME =~ *create_cell_wrapper*}\n"
                             "set my_remove_cell_count 3\nputs $my_remove_cell_count")
    assert not edits_netlist("get_property INIT [get_cells u]")


def test_reset_polarity():
    assert reset_polarity("reset") == 1 and reset_polarity("debugReset") == 1
    assert reset_polarity("rst_n") == 0 and reset_polarity("aresetn") == 0
    assert reset_polarity("ap_rst_n") == 0 and reset_polarity("nrst") == 0
    assert reset_polarity("first") is None and reset_polarity("burst_len") is None
    assert reset_polarity("clk") is None
    assert is_quiet_port("debug_bus_cmd_valid") and is_quiet_port("jtag_tdi")
    assert not is_quiet_port("dBus_rsp_valid") and not is_quiet_port("latest")


def test_testbench_drives_inputs_and_holds_reset(tmp_path):
    info = {"top": "top", "blackboxes": 0,
            "clocks": {"clk": {"clock": "c0", "period": 1.57, "wave": [0.0, 0.785]}},
            "ports": [{"name": "clk", "dir": "IN", "bus": "", "bs": None, "be": None},
                      {"name": "rst_n", "dir": "IN", "bus": "", "bs": None, "be": None},
                      {"name": "d[1]", "dir": "IN", "bus": "d", "bs": 1, "be": 0},
                      {"name": "d[0]", "dir": "IN", "bus": "d", "bs": 1, "be": 0},
                      {"name": "q[0]", "dir": "OUT", "bus": "q", "bs": 0, "be": 0}]}
    stim = tmp_path / "stim.hex"
    tb, outs, meta = build_testbench(info, stim, cycles=100, reset_cycles=10, seed=3)
    assert outs == ["q[0]"] and meta["reset_inputs"] == ["rst_n"]
    assert meta["ref_period_ps"] == 1570 and ".clk(fc_clk0)" in tb
    rows = [int(r, 16) for r in stim.read_text().split()]
    rcol = 2                                    # din bits: d[1], d[0], rst_n (sorted groups)
    assert all(not (r >> rcol) & 1 for r in rows[:10])          # active-low: held 0
    assert all((r >> rcol) & 1 for r in rows[10:50])

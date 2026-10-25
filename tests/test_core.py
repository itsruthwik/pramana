"""Unit tests for the pure-Python parts (no Vivado, no LLM).  Run: python3 -m pytest tests -q"""
import random

from pramana import flow, prune, space
from pramana.logparse import parse_tables
from pramana.runner import is_valid, parse_stdout

SPEC = space.build_spec("2025.2")


def test_spec_has_all_commands_and_directives():
    assert set(SPEC["commands"]) == {"opt", "place", "phys_opt", "route"}
    assert "Explore" in SPEC["commands"]["route"]["directives"]
    assert "RQS" not in SPEC["commands"]["phys_opt"]["directives"]
    # SSI-only options removed on a single-SLR part
    assert "-slr_crossing_opt" not in SPEC["commands"]["phys_opt"]["options"]
    assert SPEC["size"]["single_steps"] > 1_000_000


def test_legality_rules():
    assert space.legal({"cmd": "phys_opt", "directive": "Explore"}, SPEC)
    assert not space.legal({"cmd": "phys_opt", "directive": "Explore",
                            "opts": {"-fanout_opt": True}}, SPEC)       # directive XOR options
    assert space.legal({"cmd": "route", "directive": "Explore", "opts": {"-tns_cleanup": True}}, SPEC)
    assert space.legal({"cmd": "place", "directive": "Explore",
                        "opts": {"-net_delay_weight": "high"}}, SPEC)
    assert not space.legal({"cmd": "opt"}, SPEC)                        # needs directive or option


def test_random_steps_are_legal():
    rng = random.Random(0)
    for _ in range(200):
        st = space.random_step(SPEC, rng)
        assert st is None or space.legal(st, SPEC)


def test_flow_expand_ends_routed():
    names = [n for n, _, _ in flow.expand([{"cmd": "opt", "directive": "Explore"}])]
    assert names[-2:] == ["auto_place", "auto_route"]
    cmds = flow.expand([{"cmd": "route", "directive": "Explore", "opts": {"+ripup": True}}])[0][1]
    assert cmds == ["route_design -unroute", "route_design -directive Explore"]
    cmds = flow.expand([{"cmd": "place", "directive": "Explore"}])
    assert cmds[0][1][0] == "place_design -unplace" and cmds[-1][0] == "auto_route"


def test_allowed_from_cuts_and_membership():
    cuts = {"opt", "route:directive=Quick", "phys_opt:-retime"}
    allowed = prune.allowed_from_cuts(SPEC, cuts)
    assert "opt" not in allowed
    assert "Quick" not in allowed["route"]["directives"]
    assert not prune.in_allowed({"cmd": "phys_opt", "opts": {"-retime": True}}, allowed)
    assert prune.in_allowed({"cmd": "phys_opt", "directive": "Explore"}, allowed)


def test_parse_stdout_status():
    out = ("STEP_BEGIN s1_route\nCMD_OK s1_route secs=3.0\nSTEP_WNS s1_route wns=-0.1\n"
           'RESULT {"wns": -0.1, "whs": 0.01, "wpws": 0.1, "routed": 1, "route_errors": 0, '
           '"routable_nets": 10}\nPRAMANA_DONE\n')
    p = parse_stdout(out, 0, False)
    assert p["status"] == "ok" and is_valid(p["result"])
    p = parse_stdout(out.replace("CMD_OK s1_route secs=3.0", "CMD_ERR s1_route secs=3.0 :: boom"), 0, False)
    assert p["status"] == "error"
    assert parse_stdout("nothing", 0, False)["status"] == "crash"


def test_parse_tables():
    t = ("|  Optimization  |  WNS Gain (ns)  |  Added Cells  |\n"
         "-----\n|  Fanout  |  0.010  |  3  |\n|  Total  |  0.010  |  3  |\n")
    tabs = parse_tables(t)
    assert tabs and tabs[0]["rows"][0]["Optimization"] == "Fanout"

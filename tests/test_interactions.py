"""interactions(): each type from a synthetic results database (no Vivado)."""
from pramana import distill


def rec(rid, d, start, chain, wns, start_wns, purpose="exploit"):
    return {"run_id": rid, "design": d, "start": start, "chain": chain, "valid": True,
            "result": {"wns": wns}, "start_wns": start_wns, "purpose": purpose,
            "chain_key": " > ".join(f"{s['cmd']}:{s.get('directive', '')}" for s in chain)}


class Fake:
    def __init__(self, rs, ds):
        self.rs, self.ds = rs, ds

    def designs(self):
        return [{"id": d, "family": d} for d in self.ds]

    def results(self, did=None, include_noise=False):
        return [r for r in self.rs if did is None or r["design"] == did]

    def input_state(self, d):
        return {"wns": -1.0}

    def noise_floor(self, d):
        return 0.01


P = [{"cmd": "place", "directive": "Default"}]
B = [{"cmd": "phys_opt", "directive": "AddRetime"}]


def build(after_gain, alone_gain, repeat=False):
    rs = []
    for d in ("d1", "d2", "d3"):
        a = rec(f"A{d}", d, "input", B if repeat else P, -0.8, -1.0)
        rs += [a, rec(f"B{d}", d, f"A{d}", B, -0.8 + after_gain, -0.8),
               rec(f"S{d}", d, "input", B, -1.0 + alone_gain, -1.0)]
    return distill.interactions(Fake(rs, ["d1", "d2", "d3"]))


def kind(xs):
    (x,) = [x for x in xs if x["step"] == "phys_opt:AddRetime"]
    return x["type"]


def test_complement():
    assert kind(build(0.3, 0.1)) == "complement"    # A then B beats either alone


def test_substitute():
    assert kind(build(0.0, 0.3)) == "substitute"    # B helps alone, nothing after A


def test_interfering():
    assert kind(build(-0.2, 0.1)) == "interfering"


def test_repeat():
    assert kind(build(0.0, 0.1, repeat=True)) == "repeat"

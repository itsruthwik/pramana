"""Runs a pramana-generated strategy selector. Fixed code, used by pramana's training (S7b, to
fit and score selectors) and by mithya (to choose a strategy for a new design).

A selector is a Python file defining
    fit(train) -> model          (training only; model must be JSON-serializable)
    predict(model, r, f, budget) -> policy name (or None to abstain)
where train is a list of designs, each {"id", "family", "features": {..}, "reports": {..},
"scores": {policy name: fraction of the best known gain, -1..1}}; r maps report name to text
(see extract.py: S1 files as '<name>', extended profile as 'ext/<name>') and f is the design's
base features {name: value} (access them by name). budget is the time budget in multiples of
the design's route_design -directive Explore time (None: unlimited); each training design
appears once per budget, with "budget" set and the scores reached within it.
"""
import json
from pathlib import Path


def load(path):
    ns = {}
    exec(compile(Path(path).read_text(), str(path), "exec"), ns)
    return ns


def fit(path, train):
    model = load(path)["fit"](train)
    json.dumps(model)                   # must be shippable as data
    return model


def predict(path, model, r, f, budget, policies):
    """The policy the selector picks, or None if it fails, abstains or names no known policy."""
    try:
        out = load(path)["predict"](model, r, f, budget)
    except Exception:
        return None
    return out if isinstance(out, str) and out in policies else None


def _load_reports(profile_dir, ext_dir):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mithya_extract", Path(__file__).resolve().parent / "extract.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.load_reports(profile_dir, ext_dir)


def choose(path, model, profile_dir, ext_dir, f, budget, policies, default):
    return predict(path, model, _load_reports(profile_dir, ext_dir), f, budget, policies) or default

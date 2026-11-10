"""classify(features) -> bin. The rule and the training ranges come from policy.json."""
from policy import POLICY
from protocol import OUT_OF_RANGE_BIN, RANGE_MARGIN


def in_range(name, value):
    lo, hi = POLICY["feature_ranges"].get(name, (None, None))
    if lo is None or value is None:
        return value is not None
    span = (hi - lo) or abs(hi) or 1.0
    return lo - RANGE_MARGIN * span <= value <= hi + RANGE_MARGIN * span


def classify(features):
    rule = POLICY["bin_rule"]
    if rule["type"] == "one":
        return "all"
    v = features.get(rule["feature"])
    if not in_range(rule["feature"], v):
        return OUT_OF_RANGE_BIN
    return "low" if v <= rule["threshold"] else "high"

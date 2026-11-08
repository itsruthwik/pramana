"""Split dataset/selection_45.csv into train (31) and test (14).

Rules, all inside each similarity group:
  1. Test size per group: Small 5, Large ordinary 5, Large deep 4.
  2. Each subgroup is split close to its group's test share (within one design);
     a single-member subgroup goes to train.
  3. Related designs stay on one side: proxy.3/4/6, vortex basic/large, nvdla basic/large.
  4. Sources spread: every source in train; as many distinct sources in test as possible.
  5. Large DCPs (> 100 MB) spread: about their share in test (9 * 14/45, i.e. 3).
Among assignments that satisfy 1–3, a seeded random search picks the one that best covers the
test set from train (smallest worst-case distance from a test DCP to its nearest train DCP),
then 4 and 5. Distances: the same 8-metric z-score space as scripts/select_dcps.py.

Usage (repo root): python3 scripts/split_dcps.py
Writes dataset/split_train.csv, dataset/split_test.csv, dataset/split.json.
"""
import csv
import json
import math
import random

import numpy as np

SEED = 20261104
SAMPLES = 200_000
TEST_PER_GROUP = {"Small": 5, "Large, ordinary": 5, "Large, deep and wire-heavy": 4}
FAMILIES = [["proxy.3_2025.2", "proxy.4_2025.2", "proxy.6_2025.2"],
            ["vortex_xbar_basic_01_routed", "vortex_xbar_large_01_routed"],
            ["nvdla_mularray_basic_01_routed", "nvdla_mularray_large_01_routed"]]
BIG_MB = 100

sel = list(csv.DictReader(open("dataset/selection_45.csv")))
names = [r["dcp"] for r in sel]
grp = list(csv.DictReader(open("dcp_database/_grouping/groups.csv")))
FEAT = [("logic_depth_max", 1), ("max_nonclock_fanout", 1), ("avg_wire_segments_per_net", 0),
        ("max_channel_utilization", 0), ("near_crit_connections", 1), ("blocks_before_clustering", 1),
        ("blocks_after_clustering", 1), ("max_wire_segments_net", 1)]
X = np.array([[math.log10(float(r[k]) + 1) if lg else float(r[k]) for k, lg in FEAT] for r in grp])
Z = (X - X.mean(0)) / X.std(0)          # scaled over all 119, as in the grouping
zi = {r["dcp"]: Z[i] for i, r in enumerate(grp)}
P = np.array([zi[n] for n in names])
D = np.sqrt(((P[:, None] - P[None]) ** 2).sum(-1))

# units: a family moves as one unit
fam_of = {n: tuple(f) for f in FAMILIES for n in f}
units = []
for n in names:
    u = fam_of.get(n, (n,))
    if u not in units:
        units.append(u)
idx = {n: i for i, n in enumerate(names)}
row = {r["dcp"]: r for r in sel}

subs: dict[str, list[str]] = {}
for r in sel:
    subs.setdefault(r["subgroup"], []).append(r["dcp"])
share = 14 / 45
gsize = {g: sum(1 for r in sel if r["group"] == g) for g in TEST_PER_GROUP}
gshare = {g: TEST_PER_GROUP[g] / gsize[g] for g in TEST_PER_GROUP}   # 5/17, 5/18, 4/10


def ok(test: set) -> bool:
    for g, k in TEST_PER_GROUP.items():
        if sum(1 for n in test if row[n]["group"] == g) != k:
            return False
    for s, mem in subs.items():
        t = sum(1 for n in mem if n in test)
        if len(mem) == 1 and t:
            return False
        if abs(t - len(mem) * gshare[row[mem[0]]["group"]]) > 1.0 + 1e-9 or t == len(mem):
            return False
    return True


def score(test: set):
    tr = [idx[n] for n in names if n not in test]
    te = [idx[n] for n in test]
    cover = D[np.ix_(te, tr)].min(1)
    srcs_train = {row[names[i]]["source"] for i in tr}
    srcs_all = {r["source"] for r in sel}
    srcs_test = {row[n]["source"] for n in test}
    big_test = sum(1 for n in test if float(row[n]["dcp_mb"]) > BIG_MB)
    big_target = round(sum(1 for r in sel if float(r["dcp_mb"]) > BIG_MB) * share)
    return (srcs_train != srcs_all,                     # every source must be in train
            round(float(cover.max()), 3),               # worst-covered test DCP
            -len(srcs_test),                            # more sources in test
            abs(big_test - big_target),                 # big files near their share
            round(float(cover.mean()), 3))


rng = random.Random(SEED)
best, best_s, tried = None, None, 0
for _ in range(SAMPLES):
    test: set = set()
    left = dict(TEST_PER_GROUP)
    us = units[:]
    rng.shuffle(us)
    for u in us:                     # a family counts against every group it touches
        need: dict[str, int] = {}
        for n in u:
            need[row[n]["group"]] = need.get(row[n]["group"], 0) + 1
        if all(left[g] >= k for g, k in need.items()):
            for g, k in need.items():
                left[g] -= k
            test |= set(u)
    if not ok(test):
        continue
    tried += 1
    s = score(test)
    if best_s is None or s < best_s:
        best, best_s = set(test), s

assert best is not None, "no assignment satisfies the rules"
cols = ["dcp", "group", "subgroup", "source", "dcp_mb", "path"]
for name, keep in (("train", lambda n: n not in best), ("test", lambda n: n in best)):
    with open(f"dataset/split_{name}.csv", "w") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(sel, key=lambda r: (r["group"], r["subgroup"], r["dcp"])):
            if keep(r["dcp"]):
                w.writerow([r[c] for c in cols])
tr = [idx[n] for n in names if n not in best]
cover = {n: round(float(D[idx[n], tr].min()), 2) for n in best}
nearest = {n: names[tr[int(np.argmin(D[idx[n], tr]))]] for n in best}
json.dump({"seed": SEED, "samples": SAMPLES, "valid_assignments": tried,
           "score": {"all_sources_in_train": not best_s[0], "worst_cover": best_s[1],
                     "test_sources": -best_s[2], "big_files_off_target": best_s[3],
                     "mean_cover": best_s[4]},
           "test": sorted(best), "nearest_train_for_test": nearest, "cover_distance": cover,
           "families": FAMILIES}, open("dataset/split.json", "w"), indent=1)
print(f"{tried} valid assignments of {SAMPLES}; best score {best_s}")
for name in ("train", "test"):
    rs = list(csv.DictReader(open(f"dataset/split_{name}.csv")))
    print(f"\n{name}: {len(rs)}")
    for r in rs:
        extra = f"  nearest train: {nearest[r['dcp']]} ({cover[r['dcp']]})" if name == "test" else ""
        print(f"  {r['group'][:5]:5} {r['subgroup']:3} {r['dcp']:40} {r['source']:15} {r['dcp_mb']:>4}MB{extra}")

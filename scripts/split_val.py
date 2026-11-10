"""Write dcp_database/split_val.csv: 6 validation DCPs, outside the 45 of selection_45.csv.

Validation picks among pramana's candidate strategy selectors; pramana never searches on these
designs, and the 14 test DCPs stay untouched. Rules used to choose them from the 74 DCPs of the
119 not in the selection:
  1. no design from a family of the TEST split, so the test set stays the only check on
     designs unlike anything pramana's choices were made on; families of the train split are
     allowed (e.g. corescore_*, KERAS_*/qkeras_*, diffeq*, dblclockfft_*, LU*PEEng are test
     families and are avoided);
  2. 2 DCPs per similarity group (Small, Large ordinary, Large deep);
  3. different subgroups within a group, sources spread.
Large deep outside the 45: fsa_systolic_array (D1, no family in the 45) and
nvdla_mularray_xl (D3, a sibling of the train design nvdla_mularray_large); the other deep
candidates are siblings of test designs.

Usage (repo root): python3 scripts/split_val.py
"""
import csv
import os

VAL = ["robot_rl_2025.2", "bgm_2025.2", "finn_mobilenetv1_2025.2", "mlperf_RN07_2025.2",
       "fsa_systolic_array_01_routed", "nvdla_mularray_xl_01_routed"]

g = {r["dcp"]: r for r in csv.DictReader(open("dcp_database/_grouping/groups.csv"))}
cat = {r["name"]: r for r in csv.DictReader(open("dcp_database/dcp_database.csv"))}
sel = {r["dcp"] for r in csv.DictReader(open("dcp_database/selection_45.csv"))}
assert not set(VAL) & sel
with open("dcp_database/split_val.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["dcp", "group", "subgroup", "source", "dcp_mb", "path"])
    for d in VAL:
        p = "dcp_database/" + cat[d]["relative_path"]
        w.writerow([d, g[d]["group_name"], g[d]["subgroup"], g[d]["source"],
                    round(os.path.getsize(p) / 1e6), p])
print(f"wrote dcp_database/split_val.csv ({len(VAL)} DCPs)")

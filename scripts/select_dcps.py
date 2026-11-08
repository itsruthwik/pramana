"""Pick 45 DCPs from dcp_database/ that cover all 3 similarity groups (11 of 12 subgroups).

Same feature scaling as dcp_database/_grouping/similarity.py (log10 for counts, z-scores over
all 119). Drops isolated outliers (nearest neighbour > Q3 + 1.5 IQR = 2.20), the two -3-e
DCPs and the _v2 re-placements, then thins each group by repeatedly removing the DCP closest to
another remaining one (never the last of a subgroup; on near-ties the larger file).

Usage (repo root): python3 scripts/select_dcps.py
Writes dataset/selection_45.csv and dataset/selection_45_dropped.json.
"""
import csv, math, os, sys, json
import numpy as np
DB = sys.argv[1] if len(sys.argv) > 1 else "dcp_database"
rows = list(csv.DictReader(open(f"{DB}/_grouping/groups.csv")))
FEAT = [("logic_depth_max",1),("max_nonclock_fanout",1),("avg_wire_segments_per_net",0),
        ("max_channel_utilization",0),("near_crit_connections",1),("blocks_before_clustering",1),
        ("blocks_after_clustering",1),("max_wire_segments_net",1)]
X = np.array([[math.log10(float(r[k])+1) if lg else float(r[k]) for k,lg in FEAT] for r in rows])
Z = (X-X.mean(0))/X.std(0)
D = np.sqrt(((Z[:,None,:]-Z[None,:,:])**2).sum(-1))
np.fill_diagonal(D, np.inf)
cat = {r["name"]: (r["relative_path"], r["category"]) for r in csv.DictReader(open(f"{DB}/dcp_database.csv"))}
out = []
for i,r in enumerate(rows):
    g = r["group"]; same = [j for j,s in enumerate(rows) if s["group"]==g and j!=i]
    cen = Z[[j for j,s in enumerate(rows) if s["group"]==g]].mean(0)
    nn = int(np.argmin(D[i]))
    p = cat[r["dcp"]][0]
    mb = os.path.getsize(f"{DB}/{p}")/1e6
    out.append(dict(i=i, dcp=r["dcp"], group=r["group_name"], sub=r["subgroup"], src=r["source"],
        nn=rows[nn]["dcp"], nn_d=round(float(D[i,nn]),2), cen_d=round(float(np.linalg.norm(Z[i]-cen)),2),
        mb=round(mb), path=p, cat=cat[r["dcp"]][1]))
A = {"rows": out, "D": np.where(np.isinf(D), 0, D).tolist()}
nn = np.array([o["nn_d"] for o in out]); cd = np.array([o["cen_d"] for o in out])
q1,q3 = np.percentile(nn,[25,75]); thr = q3+1.5*(q3-q1)
print(f"NN distance: median {np.median(nn):.2f}  Q1 {q1:.2f} Q3 {q3:.2f}  outlier threshold {thr:.2f}")
print("--- farthest from any other DCP (isolated) ---")
for o in sorted(out,key=lambda o:-o["nn_d"])[:14]: print(f"{o['nn_d']:5.2f} cen {o['cen_d']:4.2f} {o['sub']:3} {o['dcp']:45} nn={o['nn']} {o['mb']}MB")
print("--- closest pairs (near-duplicates) ---")
for o in sorted(out,key=lambda o:o["nn_d"])[:40]: print(f"{o['nn_d']:5.2f} {o['sub']:3} {o['dcp']:45} ~ {o['nn']}")
print("--- per group: centroid distance 90th pct ---")
for g in ["Small","Large, ordinary","Large, deep and wire-heavy"]:
    v=[o for o in out if o["group"]==g]; c=np.array([o["cen_d"] for o in v])
    print(g, len(v), "median", round(float(np.median(c)),2), "p90", round(float(np.percentile(c,90)),2), "farthest:", [(o["dcp"],o["cen_d"]) for o in sorted(v,key=lambda o:-o["cen_d"])[:4]])

R = A["rows"]; D = np.array(A["D"]); np.fill_diagonal(D, np.inf)
TARGET = {"Small": 17, "Large, ordinary": 18, "Large, deep and wire-heavy": 10}
drop = {}
for r in R:
    if r["nn_d"] > 2.20: drop[r["dcp"]] = f"isolated outlier (nearest neighbour {r['nn_d']} > 2.20, {r['nn']})"
for n in ["vtr_mcml_2025.1", "ispd16_example2_2025.1"]:
    drop.setdefault(n, "part -3-e (campaign part is -2-e)")
for v, base in [("amd_mini-isp_2025.1_v2","amd_mini-isp_2025.1"),("vexriscv_re-place_v2_2025.1","vexriscv_re-place_2025.1"),
                ("rosetta_3d-rendering_2025.1_v2","rosetta_3d-rendering_2025.1"),("boom_soc_2025.1_v2","boom_soc_2025.1")]:
    drop.setdefault(v, f"re-placed variant of {base}")
idx = {r["dcp"]: r["i"] for r in R}
keep = {}
for g, t in TARGET.items():
    S_ = [r["i"] for r in R if r["group"] == g and r["dcp"] not in drop]
    subs = {R[i]["sub"] for i in S_}
    while len(S_) > t:
        sub = D[np.ix_(S_, S_)]
        nn = sub.min(1)
        order = np.argsort(nn)
        lo = nn[order[0]]
        # most redundant: smallest NN distance; among near-ties drop the larger DCP; keep each subgroup
        cands = [S_[k] for k in order if nn[k] <= lo + 0.05]
        cands = [c for c in cands if sum(R[j]["sub"] == R[c]["sub"] for j in S_) > 1] or \
                [S_[k] for k in order if sum(R[j]["sub"] == R[S_[k]]["sub"] for j in S_) > 1][:1]
        c = max(cands, key=lambda c: R[c]["mb"])
        k = S_.index(c); partner = S_[int(np.argmin([D[c, j] for j in S_]))]
        drop[R[c]["dcp"]] = f"redundant: {D[c,partner]:.2f} from {R[partner]['dcp']} (kept)"
        S_.remove(c)
    keep[g] = S_
sel = [i for g in TARGET for i in keep[g]]
print("selected", len(sel))
for g in TARGET:
    print(f"\n== {g}: {len(keep[g])}  subgroups {sorted({R[i]['sub'] for i in keep[g]})}")
    for i in sorted(keep[g], key=lambda i: (R[i]["sub"], R[i]["dcp"])):
        r = R[i]; print(f"  {r['sub']:3} {r['dcp']:45} {r['src']:15} {r['mb']:4}MB")
# coverage check: farthest any dropped DCP is from the nearest selected one
cov = [(D[r["i"], sel].min(), r["dcp"]) for r in R if r["i"] not in sel]
cov.sort(reverse=True)
print("\nworst coverage (dropped DCP -> nearest selected):", [(round(d,2), n) for d, n in cov[:6]])
print("median coverage distance", round(float(np.median([d for d,_ in cov])),2))
srcs = {}
for i in sel: srcs[R[i]["src"]] = srcs.get(R[i]["src"], 0) + 1
print("sources", srcs, "| total MB", sum(R[i]["mb"] for i in sel), "| >100MB:", [R[i]["dcp"] for i in sel if R[i]["mb"] > 100])
os.makedirs("dataset", exist_ok=True)
with open("dataset/selection_45.csv", "w") as f:
    w = csv.writer(f); w.writerow(["dcp", "group", "subgroup", "source", "dcp_mb", "path"])
    for r in sorted((R[i] for i in sel), key=lambda r: (r["group"], r["sub"], r["dcp"])):
        w.writerow([r["dcp"], r["group"], r["sub"], r["src"], r["mb"], "dcp_database/" + r["path"]])
json.dump(drop, open("dataset/selection_45_dropped.json", "w"), indent=1)

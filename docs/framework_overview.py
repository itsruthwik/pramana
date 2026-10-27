W = 72  # inner width of the main boxes
def box(title, lines, note=""):
    out = ["   ┌" + "─"*W + "┐"]
    t = f" {title}"
    out.append("   │" + t + " "*(W-len(t)-len(note)-1) + note + " │" if note else "   │" + t.ljust(W) + "│")
    for l in lines: out.append("   │" + ("    " + l).ljust(W) + "│")
    out.append("   └" + "─"*W + "┘")
    return out
def arrow(label=""):
    c = 3 + 1 + W//2
    return [" "*c + "│" + (f" {label}" if label else ""), " "*c + "▼"]

def sub(w, lines):
    r = ["┌" + "─"*(w-2) + "┐"]
    r += ["│ " + l.ljust(w-3) + "│" for l in lines]
    r.append("└" + "─"*(w-2) + "┘")
    return r

L1 = sub(25, ["LLM analysts + critic", "(sessions per design,", "resumed) read reports,", "results, draft KB;", "prune the option", "space; state hypo-", "theses; propose runs", "with predicted ΔWNS;", "critic drops claims", "the evidence refutes"])
R1 = sub(33, ["Allocator", "selects runs per round:", " exploitation: bandit on", "  measured ΔWNS + LLM", "  predictions weighted by", "  their measured accuracy", " testing: hypothesis runs", " audit: runs on pruned options", "", ""])
L2 = sub(25, ["Result analysis", "validity, ΔWNS,", "hypothesis outcomes,", "prediction accuracy;", "stop after two rounds", "without gain"])
R2 = sub(33, ["Vivado", "executes runs from the", "best checkpoint so far", "→ results database", "", ""])
mid1 = ["", "", "", " pruned  ", " space,  ", " hypothe-", " ses,    ", " predic- ", " tions   ", "───────▶ "]
mid1 = mid1[:len(L1)]
mid2 = ["", "", "", " results ", "◀─────── ", "", "", ""]
mid2 = mid2[:len(L2)]
G = 11
def pair(L, R, mid):
    return ["  " + a + (m.ljust(G)) + b for a, m, b in zip(L, mid + [""]*(len(L)-len(mid)), R)]
inner = ["", "  Draft KB ──▶ analyst: action effects on other designs, best sequences",
         "  of the most similar designs, optional prior KB; recomputed every round", ""] + pair(L1, R1, ["", "", "", "", " pruned", " space,", " hypotheses", "─────────▶", " predicted", " ΔWNS", "", ""][:len(L1)])
lc, rc = 2 + 12, 2 + 25 + G + 16
def vline(ch_l, ch_r):
    s = [" "]*(W); s[lc] = ch_l; s[rc] = ch_r; return "".join(s).rstrip()
inner += [vline("▲", "│"), vline("│", "▼")]
inner += pair(L2, R2, ["", "", "", " results", "◀─────────", "", "", ""][:len(L2)])
inner += [""]

out = ["                       Training DCPs (held-out families excluded)"]
out += arrow()
out += box("1. Design Characterization", [
 "Profiling: run Vivado report commands (timing, design analysis,",
 "  congestion, utilization, control sets, fanout, clocks, QoR) and",
 "  extract the 200 worst setup paths → feature vector per DCP",
 "  (logic/net delay share, logic levels, endpoint types, congestion)",
 "Reference runs: Explore directive on place, route and phys_opt;",
 "  every placer directive; a second placement after phys_opt",
 "  → reference ΔWNS and runtime per DCP (controls for later stages)",
 "Noise: reference runs repeated under a 2/4 ps clock perturbation",
 "  → per-DCP noise floor (gains below it are not counted)"], "(once per DCP, shared)")
out += arrow("features, reports, reference results")
b2 = ["   ┌" + "─"*W + "┐", "   │" + (" 2. LLM-Guided Exploration" + " "*20 + "(rounds, per design) ").ljust(W)[:W] + "│"]
b2 += ["   │" + l.ljust(W) + "│" for l in inner]
b2.append("   └" + "─"*W + "┘")
out += b2
out += arrow("best checkpoint and results per design")
out += box("3. Recipe Discovery", [
 "One LLM agent per design writes custom Tcl recipes (targeted",
 "phys_opt, temporary clock over-constraint, partial re-route) and",
 "evaluates them in Vivado within a fixed trial budget",
 "  → candidate recipes with rationale"])
out += arrow("best sequences, candidate recipes")
out += box("4. Cross-Design Validation", [
 "Replay each design's best sequence from its input DCP; apply every",
 "sequence and every improving recipe to all other training designs",
 "  → design × sequence ΔWNS matrix",
 "Recipes that edit the netlist: random-vector simulation against",
 "  their start state; only matching recipes can be compiled"])
out += arrow("results database (all runs of stages 1–4)")
out += box("5. Knowledge Distillation", [
 "Action profiles: median ΔWNS, P(gain), P(regression), runtime",
 "Conditions: feature thresholds, kept only if they hold on held-out",
 "  design families and beat a label-permutation test",
 "Policies: ordered sequences per design bin and per time budget,",
 "  scored by fraction of best gain on held-out families; a set of",
 "  placements to try first, chosen the same way",
 "Interactions: effect of a step after earlier steps vs alone",
 "  (complement, substitute, interfering, repeat)",
 "Also: negative results, log signals predicting no gain, runtime",
 "  model vs design size, LLM-written mechanism notes (data-checked)",
 "Draft KB: the same statistics, recomputed every round → stage 2"])
c = 3 + 1 + W//2
out += [" "*c + "▼"]
kb = ["╔" + "═"*29 + "╗", "║" + "Knowledge Base".center(29) + "║", "║" + "every entry: evidence,".center(29) + "║", "║" + "grade, scope".center(29) + "║", "╚" + "═"*14 + "╤" + "═"*14 + "╝"]
off = c - 15
out += [" "*off + l for l in kb]
out += [" "*c + "│  6. Compilation"]
lb = 6; rb = 46
lc2, rc2 = lb + 13, rb + 14
def bar(l, m, r, mid):
    t = [" "]*(rc2+1)
    for i in range(lc2, rc2+1): t[i] = "─"
    t[lc2], t[rc2], t[c] = l, r, mid
    return "".join(t)
out += [bar("┌", "", "┐", "┴")]
t=[" "]*(rc2+1); t[lc2]="▼"; t[rc2]="▼"; out.append("".join(t))
LB = sub(26, ["LLM-based flow", "context document,", "Tcl recipe procedures;", "adapters for other", "agents (LATCH, PACT)"])
RB = sub(28, ["Deterministic flow", "classifier (abstains", "outside training range),", "command sequence per bin", "and time budget"])
for a, b in zip(LB, RB):
    out.append(" "*lb + a + " "*(rb - lb - 26) + b)
out += [bar("└", "", "┘", "┬")]
out += [" "*c + "▼"]
out += box("Deployment on unseen DCPs (held-out families)", [
 "Profile → features, then",
 "  LLM-based flow: agent plans with KB context and recipes",
 "  Deterministic flow: bin → command sequence",
 "Each Vivado step retained only if WNS improves; a step starts",
 "  only if its estimated runtime fits the remaining budget"], "(within time budget)")
out += ["", "   LLM: search-space pruning, hypothesis generation, recipe composition",
        "   Vivado measurements: sole source of quantitative KB content"]
print("\n".join(out))

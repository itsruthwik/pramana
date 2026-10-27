# pramana — framework architecture (what and why)

pramana takes a **training set of post-route DCPs** and produces a **knowledge base (KB)** of
timing-closure knowledge, compiled for an LLM-based flow (maya) or a deterministic flow
(mithya). It is judged by how well those flows do on **DCPs it was not trained on**.
Overview figure: [framework_overview.md](framework_overview.md).

## Why this exists

Every agent in this space (PACT, LATCH, maya, mithya) runs on hand-written knowledge: a
recipe library, a prompt, a classifier with hand-set thresholds. None of them say how that
knowledge was produced, or test that it transfers. When tested, it mostly doesn't: LATCH's
fitted heuristic drops from 30.1% Fmax gain on its development designs to 9.7% on held-out
ones; PACT from 27.7% to 5.7%. pramana makes producing the knowledge a measured, repeatable
process.

## The tension training has to manage

Training has two goals that pull against each other:

1. **Optimise**: find what works on each training design.
2. **Learn**: find rules that hold on designs we haven't seen.

An optimiser only runs what it expects to win, so it never sees where an action fails, and
rules learned from its data are biased. Every stage below serves one of the two goals, or
stops them corrupting each other.

## Two layers

Work that depends only on the tool, the part or the DCP is done **once** and shared; a
campaign owns only the work that depends on its choices (which designs, what budget, what the
LLM decided).

```
FRAMEWORK (once; pramana prepare)          CAMPAIGN (pramana all <campaign>)
  S0 space + probe  per (Vivado, part)        S3 prune ─▶ S4 search (rounds) ─▶ S5 discover
  S1 profile        per DCP                   ─▶ S6 confirm ─▶ S7 distill ─▶ S8 compile
  S2 reference runs per DCP                          ▲                     │
       │                                             └──── draft KB ◀──────┘ (every round)
       └──────────── read by every campaign ──────────┘
```

| layer | stored in | keyed by |
|--|--|--|
| space + probe | `specs/vivado_<ver>/<part>/` (committed) | Vivado version, part |
| profile | `store/dcp/<id>-<md5>/vivado_<ver>/profile/` | DCP hash, version |
| reference runs | `store/vivado_<ver>/t<threads>/results.jsonl` | DCP hash, version, threads |
| everything else | `campaigns/<name>/` | campaign |

**Why.** Vivado is deterministic, so re-running the same profile or reference run gives the
same answer; dev2 spent hours repeating dev1's. Keying by DCP hash means a changed DCP is
re-profiled automatically, and a new Vivado version or part starts fresh.

## Framework stages (done once)

### S0 — Search space and capability probe

**What.** Parse Vivado's own `help <command>` output (pinned version 2025.2, the only local
install with UltraScale+ parts) into a machine-readable spec: for opt_design, place_design,
phys_opt_design and route_design, every directive, every option and its type (switch,
choice, numeric levels), and legality rules (a directive cannot be combined with individual
options in opt/phys_opt; place needs `-unplace` on a placed design). Then probe the part: run
each uncertain choice once on the smallest design and drop what Vivado rejects (on xcvu3p:
place `-subdirective`, `-psip_options`, AggressiveExplore; six phys_opt options that are
rejected on a routed design).

**Why.** The LLM's job is to *prune* the space, and pruning can only be measured against a
known space ("searched 3% of N, reached 95% of the best"), DATuner's starting point. Parsing
`help` makes the spec complete and tied to a Vivado version instead of remembered; the probe
keeps the search from spending runs on choices that can't run.

**Why this way.** Directives are small categorical choices; options are combinatorial
(phys_opt alone ~2^20), so the space is a *generator*, not a list. Every run (bandit, tests,
audits, references) is built from the same spec. Free-form Tcl is excluded until S5:
LLM-written procedures were valid 17.9% of the time in LATCH vs 98.2% for fixed recipes.

### S1 — Profile (read-only)

**What.** Open each DCP, run 13 Vivado report commands (timing summary, design analysis,
congestion, complexity, utilisation, control sets, high-fanout nets, clocks, QoR assessment
and suggestions, route status, exceptions), dump the 200 worst setup paths, and compute
scale-free features (logic vs net delay share, logic levels, endpoint types on critical
paths, congestion, utilisation, control sets per FF, clocks). Check the DCP hash is unchanged.

**Why.** Reports are what the LLM reads; features are what learned rules are conditioned on.

**Why this way.** Features are code, so the deterministic flow computes them identically at
deployment. Scale-free (ratios, fractions) so raw design size doesn't leak into rules. Clocks
are discovered, never hardcoded.

### S2 — Reference runs

**What.** Per design, one run per command with `-directive Explore` (route with rip-up,
place with `-unplace` and re-route, phys_opt post-route, phys_opt after a default re-place,
opt), plus a place Default control (opt forces a re-place, so its effect must be separated
from the re-place).

**Why.** These runs are (a) the **controls** every later gain is compared against, (b) the
**evidence** for pruning: Explore runs every internal pass and logs what each did (e.g.
phys_opt "Optimized 0 nets" ⇒ that pass is unlikely to help), and (c) the first **cost**
measurements.

**Placer sweep.** Every remaining placer directive once from the input (10 per design), so
placement choice is measured on every design, not only where the search happened to try it.

**Noise.** Vivado is deterministic: identical repeats, and repeats with a different thread
count (tried in dev2: 0.000 ns spread on 20/20 pairs), give no spread. Sensitivity is measured
instead: two reference chains (place Default > phys_opt Explore; place Explore) are re-run
with every clock tightened by 2 ps and by 4 ps while the placer and phys_opt run, restored
before the final route and the measurement. The result moves only because the tool's
trajectory changed; half the spread is the design's noise floor (at least 0.005 ns). A gain
below it is not counted as a gain. Designs that set their own clock uncertainty are skipped.

## Campaign stages

### S3 — Prune (LLM analyst)

**What.** Per design, an LLM analyst reads the S1 reports, the S2 Explore logs (parsed
per-pass summaries plus excerpts), the space spec, and the **draft KB** (below), and returns
a pruned subspace (at most 30% of factors kept), each cut with a reason and a confidence,
plus hypotheses with test chains and proposed chains with predicted ΔWNS.

**Why.** Design-space selection done with evidence from what Vivado did on *this* design
rather than generic priors; it replaces DATuner's statistical space partitioning with an
evidence-reading LLM.

**Why this way.**
- **Prune factors, not points**: the LLM can't rate millions of points; it can judge "no DSP
  on critical paths ⇒ drop `-dsp_register_opt`".
- **Every cut is a hypothesis**: tested later, it becomes a KB condition or negative
  knowledge; confirmed cuts become deterministic pruning rules.
- **Hard prune with audit**: pruned-out regions get only the audit share of each round,
  giving a measured false-prune rate.
- **Re-pruned every round** by the same analyst session (one persistent session per design),
  which sees new results, audit outcomes and its own prediction accuracy.

**Several analysts and a critic.** `analysts` lists the models that analyse each design
independently (default: one). Their answers are merged: a factor is cut only if more than half
cut it, proposals and hypotheses are pooled. A critic session then checks every cut, proposal
and hypothesis against the same evidence; cuts and proposals whose reasons misread or invent
evidence are dropped (the factor stays in the search), hypotheses are marked. Numbers never
come from the LLM, so the critic only guards which runs get made.

**Draft KB.** Each prompt includes what other designs have taught so far, labelled as a prior
measured on other designs: per-action effects across the other designs (median ΔWNS, how
often it gained or hurt, number of designs; ranked with small samples shrunk toward zero),
the best sequences of the three most similar designs by features, and optionally the KB of an
earlier campaign (`prior_kb`). It is recomputed from the results database at every prune, so
each round sees the latest results. Test and audit runs keep checking it, so the KB can't
only confirm itself.

### S4 — Search (rounds per design)

**What.** Rounds of: prune or re-prune → allocate → run in Vivado → score. Each design gets
its own round counter (designs added later get their own rounds) and starts every round from
its best checkpoint so far.

- **Allocation (code), per design per round (8 runs):** ~70% exploit, ~20% hypothesis tests,
  ~10% audit of pruned-out factors.
  - Exploit: a factored UCB bandit over the design's allowed space (reward = ΔWNS; other
    designs' runs count at weight 0.25), blended with the LLM's predicted ΔWNS; one slot
    reserved for the pure bandit pick and one for a beam slot from the second-best state;
    a cap per command so one command can't take every slot.
  - Tests: the analyst's hypothesis chains, interleaved with a shared floor of directive
    singles from the input DCP (cross-design rules need the same action on many designs).
- **Trust in the LLM is measured:** its predictions are scored by rank correlation with the
  measured ΔWNS, and its weight in the blend equals that correlation (0.05–0.9). In dev1 it
  fell from 0.52 to about 0; the search then leaned on the bandit and tests by itself.
- **Stop per design:** rounds used, or two rounds without a gain above the noise floor.
- **Checkpoint report:** after each iteration an LLM writes a readable summary under
  `reports/` (for humans; not fed back).

### S5 — Discover (LLM agent)

**What.** Per design, an LLM agent session with a shell works in its own directory with a
`./trial submit|wait|list` command that runs a Tcl recipe on the design's best checkpoint
and returns the measured result. Within a fixed trial budget (6) it writes recipes
(targeted phys_opt, temporary clock over-constraint, partial re-route, property changes) and
lists its best in `candidates.json`. The prompt lists which phys_opt options Vivado accepts
on a routed design.

**Why after S4.** It starts from the best known state with a target to beat, instead of
rediscovering what a directive sweep finds cheaply.

### S6 — Confirm and transfer

**What.** Each design's best lineage is replayed from its input DCP as **units** (each
measured run, started and ended routed; discovered recipes are units that carry their Tcl),
and every lineage and reference policy is run on every other training design. Recipes that
beat their own start state are run on every other design's best state.

**Why.** Separates design-specific tricks (kept, labelled as such) from general knowledge,
and fills the design × policy matrix the policy and bin choices are made from. Only new
design × policy pairs run, so adding a design costs about 2n runs, not n².

### S7 — Distill

**What.** Code computes every number in the KB from the results database:
- action profiles: effect distribution (ΔWNS ns), P(gain), P(regression), validity, relative
  cost, effect from an improved state; ship mode plain only if it never regressed on ≥3
  designs, otherwise keep-if-better;
- conditions: one-feature thresholds, kept only if they beat the majority baseline on held-out
  families **and** a label-permutation test (otherwise graded D);
- policies: the design × policy matrix scored as fraction of each design's best gain, one bin
  vs two bins compared on held-out families plus a shuffle test; below 3 designs an in-sample
  pick, marked as such;
- policies per time budget: the same held-out-family pick, but each candidate scored by the
  best routed state it reached within the budget (from per-step times and WNS in its run);
  budgets are multiples of the design's route Explore time, so they transfer across sizes;
- interactions: for each step run on an earlier state, its gain on top of that state vs from
  the input, grouped by what produced the state (a re-placement, or post-route steps only):
  complement (both beat either alone), substitute (helps alone, adds nothing after),
  interfering (usually makes the earlier result worse), repeat;
- negative knowledge, pruning rules confirmed by audits, mid-run signals (e.g. phys_opt
  optimised nothing ⇒ no later phys_opt on that state gained, 0 of 40), a runtime model;
- mechanism notes written by the LLM, rejected if they contain numbers not in the KB.
Crashes (infrastructure kills) are excluded; only completed runs are evidence.

**Why.** PACT and LATCH lost most of their gain on held-out designs because their knowledge
wasn't checked this way.

### S8 — Compile

The same KB compiles to:
- **LLM-based flow (maya):** `context.md` (labelled prior; no `{`/`}` because maya formats
  with `str.format`), `recipes.tcl`, `knowledge.json`.
- **Deterministic flow (mithya):** `features.py`, `profile.tcl`, `classify.py`
  (`classify(features) -> bin`, abstains outside the training range), `recipes.py`
  (`sequence_for(bin, budget_s, prims) -> [Step]`, each step plain or keep-if-better; picks
  the policy for the budget, then extra single actions ranked by their measured gain after a
  re-placed state, dropping ones that interfere or add nothing there),
  `policy.json`, `POLICY.md`.

## Evaluation

**Held-out designs.** `pramana.evaluate` profiles each unseen DCP, classifies it, and runs the
compiled sequence in one Vivado session with each step kept only if WNS improves; results are
compared with the design's reference runs, singly and as best-of-all at their total time.

**Sequential training** (`pramana.sequential`). Designs are added one at a time. For design n:
evaluate KB(n−1) on it (out-of-sample; runs alongside training), train on it (S3–S8, with
the analyst seeing the draft KB of designs 1…n−1), snapshot KB(n), evaluate KB(n) on it, and
every few steps evaluate KB(n) on a held-out campaign. The out-of-sample score against n shows
when more training stops helping; a second design order checks that the answer doesn't depend
on order. Evaluations only read frozen snapshots, so they run in the background; discovery and
confirm run side by side.

**Tuning the generator** (`pramana.tune`). A finished campaign is replayed as if it had run
with fewer rounds or without audits, hypothesis tests or discovery: only runs whose whole
lineage passes the variant count, a policy is available only if the variant could have found
it, and the one-bin policy is scored on held-out families. Score against Vivado hours shows
which settings to use next; cheaper variants are optimistic (they can't show what they would
have run instead). On dev1 and dev2, audits changed nothing and cost ~1 h; dropping tests,
discovery or the third round lowered the best results found.

## Rules that apply everywhere

- Unit is ΔWNS in ns against a named control; MHz is display only.
- A result counts only if fully routed, 0 route errors, WHS ≥ 0, WPWS ≥ 0, routable nets > 0.
- Failure is decided from Tcl return codes and sentinels, never log phrasing.
- LLM: regular Claude Code sessions (Sonnet 5.5, effort low, user subscription), one tmux
  session each (`tmux ls`, `tmux attach -t pr-<campaign>-<stage>-<name>`), saved like any
  session (`claude --resume`). Tasks and answers are files; answers are checked against a
  JSON schema. Analysts get file tools only; discovery agents also get a shell. Every prompt,
  answer, session id and token count is logged under the campaign.
- Resources: at most 4 concurrent Vivado runs machine-wide across all pramana processes
  (`PRAMANA_MAX_VIVADO`), 4 threads each; a launch also waits for free memory (the machine
  runs earlyoom). Crashed runs are retried once and never counted as results.
- Vivado-only for v1 (2025.2). One part per campaign.
- Ship plain when the evidence is clear; keep-if-better only when it isn't.

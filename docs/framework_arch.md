# pramana — framework architecture (what and why)

pramana takes a **training set of post-route DCPs** and produces a **knowledge base (KB)** of
timing-closure knowledge, compiled for an LLM agent (maya) or a deterministic flow (mithya).
It is judged by how well those agents do on **DCPs it was not trained on**.

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

## Stages

```
S0 SPACE ─▶ S1 PROFILE ─▶ S2 BASELINE ─▶ S3 PRUNE ─▶ S4 SEARCH (rounds) ─▶ S5 DISCOVER ─▶ S6 CONFIRM ─▶ S7 DISTILL ─▶ S8 COMPILE
 what can     what is      Explore per    LLM cuts     bandit + tests +       custom Tcl     repeats +       rules that      maya / mithya
 run          the design   command        the space    audit; re-prune        per design     transfer        transfer        bundles
                                                       ▲   singles→stacks→deep │
                                                       └── checkpoint report ──┘
```

### S0 — Define the full search space

**What.** Parse Vivado's own `help <command>` output (pinned version, 2025.2 — the only install on this machine with UltraScale+ parts) into a
machine-readable spec: for opt_design, place_design, phys_opt_design and route_design, every
directive, every option and its type (switch, choice, numeric with 3–4 levels), and the
legality rules (e.g. in opt_design and phys_opt_design a directive cannot be combined with
individual optimisation options; post-route place needs `-unplace` first). Add a curated list
of settings/constraints (documented properties, clock over-constraint) as extra actions.
Code computes the size of the space.

**Why.** The user's goal for the LLM is to *prune* the space. Pruning can only be measured
against a known space ("searched 3% of N, reached 95% of the best"). This is DATuner's
starting point. Parsing `help` makes the spec complete and tied to a Vivado version instead of
remembered.

**Why this way.** Directives are small categorical choices (enumerable); options are
combinatorial (phys_opt alone ~2^20), so the space is a *generator*, not a list. Points are
built from the spec, so the bandit, random sample, audit and baselines all draw from the same
set. Free-form Tcl is excluded until S5: LLM-written procedures were valid 17.9% of the time
in LATCH vs 98.2% for fixed recipes.

**Three layers.**

| layer | contents | treatment |
|--|--|--|
| L1 directives | one per command | enumerated |
| L2 options | every option of every command, typed | enumerated; LLM prunes by option |
| L3 settings/constraints | curated documented properties, clock over-constraint | fixed actions; anything else is S5 |

### S1 — Profile (read-only)

**What.** Open each DCP, run reports, extract scale-free features (path makeup, logic vs
route delay, levels, which block types sit on critical paths, congestion, fanout, control
sets, utilisation, clocks). Check the DCP hash is unchanged.

**Why.** Reports are what the LLM reads to understand the design; features are what learned
rules are conditioned on.

**Why this way.** Features are code so mithya computes them identically at deployment.
Scale-free (ratios, fractions) so raw design size doesn't leak into rules. Clocks are
discovered, never hardcoded.

### S2 — Baseline: Explore per command (+ repeats)

**What.** Per design, one run per command family with `-directive Explore` (place runs with
`-unplace` first and re-routes; phys_opt runs post-route), plus a repeat of the identity
chain (re-route) to measure noise.

**Why.** These runs are (a) the **controls** every later gain is measured against, (b) the
**evidence** for pruning: Explore runs every internal pass and logs what each did (e.g.
phys_opt "Optimized 0 nets" for a pass ⇒ its option is unlikely to help), and (c) the first
**cost** and **noise floor** measurements.

### S3 — Prune (LLM)

**What.** Per design, an LLM reads the S1 reports, the S2 Explore logs (parsed per-pass
summaries plus log excerpts) and the space spec, and returns a pruned subspace: which
commands, directives, options and levels to keep, each cut with a reason and a confidence.

**Why.** This is the user's design-space-selection idea, done with evidence from what Vivado
did on *this* design rather than generic priors. It replaces DATuner's statistical
space-partitioning with an evidence-reading LLM.

**Why this way.**
- **Prune factors, not points**: the LLM can't rate millions of points; it can judge "no
  DSP on critical paths ⇒ drop phys_opt `-dsp_register_opt`".
- **Every cut is a hypothesis**: tested later, it becomes a KB condition or negative
  knowledge, and confirmed cuts become mithya's deterministic pruning rules.
- **Hard prune with audit** (decided): pruned-out regions get only the audit share (~10%)
  of each round, giving a measured false-prune rate.
- **Per design** (decided), plus a **shared floor** of points every design runs, because
  cross-design rules need the same action measured on different designs.
- **Re-pruned every round**: cuts can be reversed by new evidence.

### S4 — Search (rounds)

**What.** Rounds of: generate candidates from each design's pruned space at the current
depth → allocate → run in Vivado → settle → update → (LLM) re-prune and predict.

- **Depth progression:** round 1 single steps; round 2 two-step chains built only from
  steps that helped alone, applied on top of the design's best; then deeper chains and
  repeats until extra depth stops paying.
- **Allocation (code):** ~70% exploit (UCB bandit over candidates, reward = ΔWNS per
  second), ~20% hypothesis tests (cuts/keeps, shared floor on other designs), ~10% audit of
  pruned-out space.
- **LLM predictions:** before a round, the LLM predicts ΔWNS for proposed candidates; the
  predictions are scored and the bandit's prior weight on LLM proposals follows that score
  (LLMs help most early, when data is sparse: LLAMBO).
- **Chains start from the design's best checkpoint so far** (decided); the input DCP stays as
  the reference.
- **Budget:** shared across designs with a per-design minimum (decided).
- **Stop per design:** budget spent, or two rounds with no gain above noise.
- **Checkpoint report** (decided): at each depth change, an LLM writes a readable analysis
  of what was learned, saved under `reports/`.

**Why rounds instead of fixed phases.** The user's phases (Explore → directive sweep → stacks
→ custom) survive as *depth levels*; inside each, what runs next is chosen from data, so
budget isn't spent on stacks of steps that don't work alone.

### S5 — Discover (custom Tcl, LLM agent)

**What.** Per design, a Claude Code agent works in a trial directory with a `pramana trial`
command that runs a Tcl snippet on the design's best checkpoint and returns the result. It
edits recipes (set_property / get_* style, targeted phys_opt, constraint tweaks) to beat the
best search result. An archive keeps the best recipes at different gain/time trade-offs
(OpenEvolve's MAP-Elites idea).

**Why after S4.** It starts from the best known state with a target to beat, instead of
rediscovering what a directive sweep finds cheaply.

### S6 — Confirm and transfer

**What.** Every win from S4/S5 is re-run with repeats and run on the other training designs.
**Why.** Separates noise, design-specific tricks (kept, labelled design-specific), and
general knowledge (the only kind that enters the main KB).

### S7 — Distill

**What.** Code computes every number in the KB from the results database: action profiles
(effect distribution in ΔWNS ns, cost, validity, regression rate), conditions, interactions,
pruning rules, negative knowledge, mid-run signals, policies per budget. A rule is kept only
if it holds on design families it wasn't fitted on. The LLM writes mechanism notes only; no
LLM-produced number enters the KB.

**Why.** PACT and LATCH lost most of their gain on held-out designs because their knowledge
wasn't checked this way.

### S8 — Compile

The same KB compiles to:
- **maya:** `context.md` (labelled prior; no `{`/`}` because maya formats with
  `str.format`), `recipes.tcl`, `knowledge.json`.
- **mithya:** `features.py`, `classify.py` (`classify(features) -> bin`, abstains outside
  the training range), `recipes.py` (`sequence_for(bin, budget_s) -> [Step]`, each step
  plain or keep-if-better), `policy.json`.

## Rules that apply everywhere

- Unit is ΔWNS in ns against a named control; MHz is display only.
- A result counts only if fully routed, 0 route errors, WHS ≥ 0, WPWS ≥ 0.
- Failure is decided from Tcl return codes and sentinels, never log phrasing.
- Every LLM call is logged (prompt, response, model, cost) under the campaign.
- LLM: regular Claude Code sessions (Sonnet 5.5, effort low, user subscription), one tmux session each (`tmux ls`, `tmux attach -t pr-<campaign>-<stage>-<name>`); saved like any session (`claude --resume`). The analyst is one session per design, resumed every round.
- Vivado-only for v1 (2025.2: the local 2025.1 install has no UltraScale+ parts). One part per campaign.
- Ship plain when the evidence is clear; keep-if-better only when it isn't.

# mithya (shim)

A fixed executor for a pramana-trained policy. It holds no knowledge about Vivado or designs:

- `policy.json` (written by pramana's S7) holds everything learned: the candidate policies
  (each a list of steps with order, Tcl and mode committed / candidate), the default policy,
  the strategy selector (its code file and fitted model) and the
  runtime cost model. Budget left after the chosen policy goes to the other candidate
  policies in P order, each run whole and kept only if it improves WNS.
- `selectors/<name>.py` is the selector pramana's LLM wrote and pramana validated:
  `predict(model, reports, features, budget)` picks one policy for a design and time budget
  (budget in multiples of the design's estimated route Explore time; None: the default policy).
- `protocol.py` holds the fixed run protocol: default step estimate, step limit, grace period
  and what to do when the selector fails. Not learned; the same for every policy.
- `classify.py` (choose_policy), `recipes.py`, `executor.py`, `ladder.py` interpret
  `policy.json` under that protocol; `selector.py` and `extract.py` load and run the
  selector on the reports of `profile.tcl` and `profile_ext.tcl`; `features.py` computes the
  base features. `kib.tcl` is the keep-if-better Tcl.

These files are the same for every trained bundle; only `policy.json` and `selectors/` change.

## Running

Installing knowledge is offline: pramana's emit writes `policy.json` and `selectors/` into the
bundle. At run time the only input is a routed DCP:

    python run.py <design.dcp> [--budget 3000] [--out <dir>] [--threads 4] [--vivado-settings <settings64.sh>]

One Vivado session measures the design (`profile.tcl`, then `profile_ext.tcl`), the selector
picks a policy for the budget left, a second session runs it (then the other candidates,
keep-if-better) until mithya's start + budget. Output: `<out>/final.dcp` when the result is
legal and better than the input (otherwise the input stands) and `<out>/result.json`
(policy, plan, step log, input and final metrics, dWNS, measuring and total seconds). No LLM,
no pramana campaign or store.

---
name: peer-handoff
description: "Hand the current task to peer coding agents (Codex, OpenCode, Antigravity, Claude Code, Grok, Devin through porch) and orchestrate the result: a task-aware peer review of the work you did, or independent implementations of the same task in separate git worktrees that are collected, tested and compared so you can cherry-pick the best parts. Use when the user asks to get a second opinion from other agents, 'ask your buddies', have peers review a branch or PR, run a bake-off between agents, or compare independent implementations. Builds the handoff brief (task from Linear or a file, project context, what you did) so the user does not have to."
---

# Peer handoff

You orchestrate. Peers review or re-implement; you judge, merge, and own the
result. The `handoff` CLI does the mechanics: it packs the task, the project
context and your work into a bundle, launches peers through `porch` (the
`pragmatic-orchestration` skill in this plugin), and collects what they produce.

Resolve the entrypoint from this skill's directory, never from the caller's cwd:

```bash
HANDOFF="<this skill dir>/scripts/handoff"
test -x "$HANDOFF" || { echo "peer-handoff entrypoint not found" >&2; exit 1; }
```

`porch` is found as the sibling skill automatically; set `PEER_HANDOFF_PORCH`
to override. Load the `pragmatic-orchestration` skill too: its delegation and
acceptance rules (`references/delegate.md`, `references/review-acceptance.md`)
apply to every peer you launch here.

## 1. Build the bundle

Commit your work first, or pass `--include-uncommitted` to hand over the
working tree as it is (`ours.patch` is then base → working tree, untracked
files included, and reviews run in that checkout). Then, from the repository:

```bash
BUNDLE=$("$HANDOFF" init \
  --base origin/master \
  --task-linear ENG-123 \
  --context-file docs/architecture.md \
  --context-note "Python 3.12 FastAPI service; tests in api/tests" \
  --notes "What I did, decisions taken, known gaps" \
  --check "pytest api/tests/test_feature.py -q")
```

- The task comes from `--task-linear ID`, `--task-file PATH` (md, txt, or a
  Linear JSON export) or `--task-text "…"`; all three are repeatable.
- `--context-file` is repeatable: small text files are inlined into the
  briefs, large or binary ones are copied into the bundle and referenced.
- `--check` is repeatable: acceptance commands `collect` runs in every worktree.
- `--task-linear` needs `LINEAR_API_KEY` in the environment; it renders the
  issue with its description, sub-issues, attachments and comments.
  If no key is available, export the issue to a file and use `--task-file`
  (a Linear JSON export is rendered the same way).
- Pass the project context a new engineer would need: the relevant spec or
  architecture document, conventions, constraints. Peers start with nothing.
- `--notes` is your handover: what you changed, why, what you are unsure of.
  Reviewers see it; independent implementers never do.
- Bundles live under `$PEER_HANDOFF_HOME` (default `~/.peer-handoff`), outside
  the repository. They can contain task and project details: never commit them.

The bundle holds `task.md`, `context.md`, `work.md`, `ours.patch`, the two
generated briefs and `manifest.json`. Read `brief-review.md` once before
launching anything: it is exactly what peers receive.

## 2a. Peer review of your work

```bash
"$HANDOFF" review "$BUNDLE" -a codex,antigravity                       # task-aware review (porch review ask)
"$HANDOFF" review "$BUNDLE" --kind code --depth super -a codex          # porch multi-pass diff review
```

- `ask` (default) sends the full brief: task, context, your notes, where the
  diff is. Use it to check the work against the task.
- `code` pipes `ours.patch` into `porch review code --diff`; pick one depth
  (`basic`, `specialists`, `super`, `ultra`) as porch's review guide says.
- If the checkout has moved off the bundle's head commit, `review` uses a
  throwaway detached worktree at that commit and removes it afterwards;
  `--allow-head-mismatch` reviews the checkout as it is.
- Reports are saved under `$BUNDLE/reviews/`. Judge every finding by evidence
  and task impact (`review-acceptance.md`): fix, verify, defer or dismiss.
  Agreement between reviewers is a signal, not proof.
- Every review also records what each peer cost, in
  `$BUNDLE/reviews/<stamp>-<kind>.usage.json`, and prints the table. See
  section 2c.

## 2c. Cost of a round

porch keeps each peer's raw stream; `review` reads the tokens from it and
prices them:

- OpenCode and Claude Code report a cost themselves; that number is used.
- Codex, Antigravity and others report tokens only; they are priced with
  OpenRouter's public list prices (input, cached input, output; cached for a
  day under `$PEER_HANDOFF_HOME/cache/`).
- `metered` peers cost real money per token. `subscription` peers (Codex and
  Claude Code by default, OpenCode Go models) are covered by a flat plan: their
  figure is a list-price equivalent for comparing peers, not money spent.
- Overrides go in `$PEER_HANDOFF_HOME/pricing.json`: `billing` per agent id or
  backend, `models` (porch model → OpenRouter id), `prices` (USD per million
  tokens for `input`, `cached`, `output`).

```bash
"$HANDOFF" cost "$BUNDLE"                       # per peer, all reviews in the bundle
"$HANDOFF" report "$BUNDLE" --out rounds/2026-11-03-eng-123-r1.md \
  --ledger rounds/README.md --ticket ENG-123 --pr <url> --ambiguity 2
```

`report` writes a round record: size of the change (changed lines, size class
XS–XL, files, components, test files), the task (words, acceptance criteria),
the ambiguity you rate from 1 (precise) to 5 (exploratory), each peer's
tokens, time and cost, totals, and cost per 100 changed lines. It leaves a
findings table and a "peer value" section for you to fill in after verifying,
and appends a summary row to the ledger. Keep records in the project that owns
the work; they name its code and findings.

## 2b. Independent implementations

```bash
"$HANDOFF" compete "$BUNDLE" -a codex,opencode-go-kimi-k3,antigravity
```

- Each agent gets its own git worktree under the bundle, on branch
  `handoff/<bundle-id>/<agent>`, at the base commit, and the implement brief
  with that worktree as its only working root.
- Steerable backends start detached (`porch delegate --detach`) and print a
  run id; one-shot-only backends (Antigravity) run in the background.
- Peers implement the task from scratch and never see your branch or notes.
  That independence is the point: do not leak your approach in a steer.
- Supervise as porch's delegate guide requires: check each worker within its
  first minute (`porch delegate events RUN_ID --max-events 50`), then every
  5–15 minutes. `"$HANDOFF" status "$BUNDLE"` shows all workers at once.
  Steer a worker that drifts from the task; never steer it toward your design.

## 3. Collect and compare

```bash
"$HANDOFF" collect "$BUNDLE"                 # runs the --check commands in every worktree
"$HANDOFF" collect "$BUNDLE" --check "npm test" --no-checks   # extra or no checks
```

`collect` writes `$BUNDLE/comparison.md`: one row per implementation (yours
is `ours`) with state, commits, files, lines changed and check results, a
files-touched matrix, and paths to each patch, final answer and check log.

Then do the real comparison yourself:

1. Read every patch and final answer against the task's acceptance criteria,
   not against your own implementation.
2. For each meaningful difference, decide which version is better and why:
   correctness, coverage of the task, simplicity, fit with the codebase, tests.
3. Pick one base (often yours, but not by default). Bring in the better parts:

   ```bash
   "$HANDOFF" adopt "$BUNDLE" --from codex --path src/parser.py --check-only   # does it apply?
   "$HANDOFF" adopt "$BUNDLE" --from codex --path src/parser.py                 # apply, uncommitted
   ```

   Smaller ideas are often better re-implemented by hand than applied as a patch.
4. Re-run the checks, review the combined diff, and only then commit.
5. If a peer's approach is better overall, say so and change course.

## 4. Report and clean up

Tell the user, in plain terms: who did what, what each peer found or built
differently, what you adopted and why, what you rejected and why, and the
checks that now pass. Keep the comparison table if it helps.

```bash
"$HANDOFF" cleanup "$BUNDLE"            # dry run: lists worktrees and branches
"$HANDOFF" cleanup "$BUNDLE" --yes      # removes them (add --delete-bundle to drop the bundle too)
```

Cleanup refuses while a worker still looks active; cancel it with
`porch delegate cancel RUN_ID` first, or pass `--force`.

## Rules

- Delegated peers run with full access in their worktree (porch delegate has
  no sandbox). Work only in repositories the user put in scope.
- Never push, open PRs, or commit to the user's branch on a peer's behalf;
  adopted changes stay uncommitted until you have reviewed and tested them.
- Peers are other vendors' models: the brief (task, context, code) goes to
  them. Hand over only what the user has agreed to share with those tools.
- One review depth per round; another round needs a specific unresolved risk.

# peer-handoff

Let one coding agent hand its task to its peers and orchestrate the result.

The agent that did the work (for example Claude Code) packs the task, the
project context and what it did into a **handoff bundle**. Peers then either:

- **review** that work against the task, read-only, through `porch review`; or
- **compete**: implement the same task independently, each in its own git
  worktree, through `porch delegate`.

`collect` gathers every implementation, runs the acceptance checks in each
worktree, and writes a side-by-side comparison. The orchestrating agent reads
it, picks one base, adopts the better parts from the others, and owns the
single result that goes into the repository.

It replaces the manual routine of exporting a ticket, giving each tool the
repository and the background, explaining what was done, and comparing their
answers by hand.

## How it fits together

```
          you ──"get your peers to review / try this"──▶ orchestrator agent
                                                             │
                              handoff init ◀─────────────────┤ task (Linear / file)
                    bundle: task · context · work · briefs   │ context docs, notes, checks
                                                             │
               ┌──────────────── handoff review ─────────────┼──────── handoff compete ───────────────┐
               ▼                                             │                                        ▼
   porch review ask / code (read-only)                       │      git worktree per agent + porch delegate
   codex · antigravity · opencode · …                        │      codex · antigravity · opencode · …
               │                                             │                                        │
               └──▶ reviews/*.md                             │            handoff collect ◀───────────┘
                                                             │     patches · answers · checks · comparison.md
                                                             ▼
                                         orchestrator judges, adopts, re-tests, reports
```

## Requirements

- Python 3.11+ and git.
- The `pragmatic-orchestration` skill from this plugin (`porch`), with the
  agent CLIs you want as peers installed and signed in.
- For `--task-linear`: a Linear API key in `LINEAR_API_KEY`.

## Commands

| Command | What it does |
|---|---|
| `handoff init` | Build a bundle from the task (`--task-linear`, `--task-file`, `--task-text`), context (`--context-file`, `--context-note`), notes and acceptance checks; `--include-uncommitted` hands over the working tree as it is |
| `handoff review BUNDLE` | Task-aware review (`--kind ask`, default) or porch diff review (`--kind code --depth …`); records each peer's tokens and cost |
| `handoff cost BUNDLE` | Tokens, time and cost per peer across the bundle's reviews (`--json`) |
| `handoff report BUNDLE` | Round record: change size and components, task size, ambiguity, cost per peer; `--ledger` appends a summary row |
| `handoff compete BUNDLE -a a,b,c` | One worktree + branch per agent at the base commit; launch each peer on the implement brief |
| `handoff status BUNDLE` | State of every competing worker |
| `handoff collect BUNDLE` | Patches (committed, uncommitted and untracked), final answers, check results; writes `comparison.md` |
| `handoff adopt BUNDLE --from AGENT [--path P]` | Apply a peer's changes, or only some files, to the original checkout (uncommitted) |
| `handoff cleanup BUNDLE --yes` | Remove the bundle's worktrees and branches |
| `handoff list` | List bundles |

## Where things are stored

Bundles live under `$PEER_HANDOFF_HOME` (default `~/.peer-handoff/<repo>/<id>/`),
outside the repository, because they contain the task and project context.
Worktrees for competing peers sit inside the bundle.

## Environment

| Variable | Purpose |
|---|---|
| `PEER_HANDOFF_HOME` | Bundle root (default `~/.peer-handoff`) |
| `PEER_HANDOFF_PORCH` | porch executable (default: the sibling skill, then `porch` on PATH) |
| `PEER_HANDOFF_CHECK_TIMEOUT` | Seconds per acceptance command in `collect` (default 1800) |
| `PEER_HANDOFF_PRICES_URL` | Price catalogue for peers that report tokens only (default: OpenRouter's public model list) |
| `LINEAR_API_KEY` | Linear personal API key for `--task-linear` |

## Tests

```bash
python3 skills/peer-handoff/scripts/tests/handoff_test.py -v
```

Offline: a temporary git repository and a fake porch; no network, no models.

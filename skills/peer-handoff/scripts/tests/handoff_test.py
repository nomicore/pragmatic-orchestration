"""Offline peer-handoff tests: temp git repo + fake porch, no network, no models."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = Path(__file__).resolve().parent
HANDOFF = TESTS.parent / "handoff"
FAKE_PORCH = TESTS / "fakes/fake-porch"

_loader = importlib.machinery.SourceFileLoader("handoff", str(HANDOFF))
_spec = importlib.util.spec_from_loader("handoff", _loader)
handoff = importlib.util.module_from_spec(_spec)
_loader.exec_module(handoff)


def sh(*argv, cwd=None):
    return subprocess.run(list(argv), cwd=cwd, check=True, capture_output=True, text=True).stdout


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="peer-handoff-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        sh("git", "init", "-q", "-b", "master", cwd=self.repo)
        (self.repo / "app.py").write_text("print('hi')\n")
        sh(*g, "add", "-A", cwd=self.repo)
        sh(*g, "commit", "-qm", "base", cwd=self.repo)
        sh("git", "switch", "-qc", "feat/greeting", cwd=self.repo)
        (self.repo / "app.py").write_text("print('hello, world')\n")
        sh(*g, "commit", "-qam", "Add greeting", cwd=self.repo)
        self.task = self.tmp / "task.md"
        self.task.write_text("# ENG-1: Add a greeting\n\nPrint a friendly greeting.\n")
        self.ctx = self.tmp / "context.md"
        self.ctx.write_text("The app is a tiny CLI.\n")
        self.log = self.tmp / "porch.jsonl"
        self.env = dict(os.environ, PEER_HANDOFF_HOME=str(self.tmp / "home"),
                        PEER_HANDOFF_PORCH=str(FAKE_PORCH), FAKE_PORCH_LOG=str(self.log))
        self.env.pop("LINEAR_API_KEY", None)
        prices = self.tmp / "prices.json"
        prices.write_text(json.dumps({"data": [
            {"id": "openai/m", "pricing": {"prompt": "0.000002", "completion": "0.00001",
                                           "input_cache_read": "0.0000001"}}]}))
        self.env["PEER_HANDOFF_PRICES_URL"] = prices.as_uri()

    def handoff(self, *args, expect=0):
        res = subprocess.run([sys.executable, str(HANDOFF), *args], cwd=self.repo, env=self.env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, expect, res.stderr + res.stdout)
        return res

    def init(self, *extra):
        out = self.handoff("init", "--base", "master", "--task-file", str(self.task),
                           "--context-file", str(self.ctx), "--notes", "Chose print over logging.",
                           "--check", "test -f app.py", *extra).stdout
        return Path(out.strip().splitlines()[-1])

    def test_init_builds_bundle_outside_repo(self):
        bundle = self.init()
        self.assertTrue(str(bundle).startswith(str(self.tmp / "home")))
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["branch"], "feat/greeting")
        review = (bundle / "brief-review.md").read_text()
        for needle in ("Add a greeting", "tiny CLI", "Chose print over logging", "Add greeting",
                       "test -f app.py", manifest["base_sha"]):
            self.assertIn(needle, review)
        self.assertIn('<initial_relevant_files completeness="likely-partial">', review)
        self.assertIn("- app.py: changed by the implementation", review)
        implement = (bundle / "brief-implement.md").read_text()
        self.assertIn("Add a greeting", implement)
        # Independent implementers must not see the original work or notes.
        self.assertNotIn("Chose print over logging", implement)
        self.assertIn("hello, world", (bundle / "ours.patch").read_text())
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.repo), "")

    def test_init_requires_a_task(self):
        res = self.handoff("init", "--base", "master", expect=1)
        self.assertIn("no task", res.stderr)

    def test_linear_without_key_fails_clearly(self):
        res = self.handoff("init", "--base", "master", "--task-linear", "ENG-1", expect=1)
        self.assertIn("LINEAR_API_KEY", res.stderr)

    def test_linear_rendering(self):
        md = handoff.linear_issue_markdown({
            "identifier": "ENG-7", "title": "Fix login", "url": "https://linear.app/x/issue/ENG-7",
            "description": "Users cannot log in.", "state": {"name": "In Progress"},
            "labels": {"nodes": [{"name": "bug"}]},
            "comments": {"nodes": [{"body": "Repro attached", "createdAt": "2026-01-02T00:00:00Z",
                                    "user": {"name": "QA"}}]}})
        for needle in ("# ENG-7: Fix login", "In Progress", "bug", "Users cannot log in.", "Repro attached"):
            self.assertIn(needle, md)

    def test_linear_json_export_task_file(self):
        export = self.tmp / "issue.json"
        export.write_text(json.dumps({"identifier": "ENG-9", "title": "Exported", "description": "Body"}))
        self.assertIn("# ENG-9: Exported", handoff.task_file_markdown(export))

    def test_large_context_is_referenced_not_inlined(self):
        big = self.tmp / "spec.md"
        big.write_text("x" * (handoff.INLINE_CONTEXT_LIMIT + 10))
        bundle = self.init("--context-file", str(big))
        self.assertTrue((bundle / "context" / "spec.md").is_file())
        self.assertIn(str(bundle / "context" / "spec.md"), (bundle / "context.md").read_text())

    def test_review_ask_and_code(self):
        bundle = self.init()
        out = self.handoff("review", str(bundle), "-a", "codex").stdout
        self.assertIn("REVIEW_OK", out)
        self.assertIn("BRIEF_HAS_TASK", out)
        out = self.handoff("review", str(bundle), "--kind", "code", "--depth", "super").stdout
        self.assertIn("CODE_REVIEW_OK", out)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(len(manifest["reviews"]), 2)
        self.assertTrue(all(Path(r["file"]).is_file() for r in manifest["reviews"]))

    def test_review_refuses_unknown_or_unavailable_peers(self):
        bundle = self.init()
        res = self.handoff("review", str(bundle), "-a", "codex,no-such-peer", "--dry-run", expect=1)
        self.assertIn("unknown porch agent id(s): no-such-peer", res.stderr)
        res = self.handoff("review", str(bundle), "-a", "codex,grok", expect=1)
        self.assertIn("not installed", res.stderr)
        self.assertEqual(json.loads((bundle / "manifest.json").read_text())["reviews"], [])

    def test_review_dry_run_lists_resolved_peers(self):
        bundle = self.init()
        out = self.handoff("review", str(bundle), "--dry-run", "-a", "codex,antigravity").stdout
        self.assertIn("# peers: codex, antigravity", out)
        # Without -a: porch's enabled pool, minus agents whose CLI is missing.
        self.assertIn("# peers: codex\n", self.handoff("review", str(bundle), "--dry-run").stdout)

    def test_review_records_usage_and_cost_per_peer(self):
        bundle = self.init()
        res = self.handoff("review", str(bundle), "-a", "codex,oc-kimi-k3.1")
        self.assertIn("cost of this review", res.stderr)
        manifest = json.loads((bundle / "manifest.json").read_text())
        usage_file = Path(manifest["reviews"][-1]["usage"])
        data = json.loads(usage_file.read_text())
        rows = {r["agent"]: r for r in data["rows"]}
        codex = rows["codex"]
        # 200k uncached at $2/M + 1M cached at $0.1/M + 10k output at $10/M.
        self.assertEqual(codex["tokens"]["input"], 200_000)
        self.assertEqual(codex["tokens"]["cached"], 1_000_000)
        self.assertAlmostEqual(codex["cost_usd"], 0.6, places=6)
        self.assertEqual(codex["billing"], "subscription")
        self.assertEqual(codex["price_source"], "openrouter:openai/m")
        kimi = rows["oc-kimi-k3.1"]
        self.assertAlmostEqual(kimi["cost_usd"], 0.75, places=6)
        self.assertEqual(kimi["price_source"], "reported by backend")
        self.assertEqual(kimi["tokens"]["output"], 300)
        self.assertEqual(data["summary"]["metered_usd"], 0.75)
        self.assertEqual(data["summary"]["subscription_equiv_usd"], 0.6)
        out = self.handoff("cost", str(bundle)).stdout
        self.assertIn("| oc-kimi-k3.1 |", out)
        self.assertIn("Spent (metered):** $0.750", out)

    def test_report_writes_round_record_and_ledger(self):
        bundle = self.init()
        self.handoff("review", str(bundle), "-a", "codex,oc-kimi-k3.1")
        rec = self.tmp / "rounds" / "r1.md"
        ledger = self.tmp / "rounds" / "README.md"
        self.handoff("report", str(bundle), "--out", str(rec), "--ledger", str(ledger),
                     "--ticket", "ENG-1", "--ambiguity", "2")
        text = rec.read_text()
        self.assertIn("# Peer review round: ENG-1: Add a greeting", text)
        self.assertIn("**XS** (2 changed lines", text)
        self.assertIn("2/5 (clear", text)
        self.assertIn("| codex |", text)
        record = json.loads(text.split("<!-- round-record", 1)[1].split("-->", 1)[0])
        self.assertEqual(record["metrics"]["files"], 1)
        self.assertEqual(sorted(record["peers"]), ["codex", "oc-kimi-k3.1"])
        rows = ledger.read_text().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertIn("[record](r1.md)", rows[-1])

    def test_extract_usage_antigravity_sums_model_calls(self):
        raw = self.tmp / "agy.jsonl"
        raw.write_text("\n".join(json.dumps({"step_update": {"usage": u}}) for u in (
            {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 5, "cache_read_tokens": 1000},
            {"input_tokens": 50, "output_tokens": 20, "thinking_tokens": 0, "cache_read_tokens": 2000},
        )) + "\n")
        u = handoff.usage.extract_usage(raw, "antigravity")
        self.assertEqual((u["input"], u["cached"], u["output"], u["calls"]), (150, 3000, 35, 2))
        self.assertIsNone(u["reported_cost"])

    def test_init_include_uncommitted_hands_over_the_working_tree(self):
        (self.repo / "app.py").write_text("print('hello, uncommitted')\n")
        (self.repo / "new_module.py").write_text("X = 1\n")
        bundle = self.init("--include-uncommitted")
        patch = (bundle / "ours.patch").read_text()
        self.assertIn("hello, uncommitted", patch)
        self.assertIn("new_module.py", patch)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertTrue(manifest["uncommitted"])
        self.assertIn("uncommitted changes in the working tree", (bundle / "brief-review.md").read_text())
        # Reviews run in the checkout itself, which holds the uncommitted work.
        self.handoff("review", str(bundle), "-a", "codex")
        call = [json.loads(l) for l in self.log.read_text().splitlines()][-1]
        self.assertEqual(call["cwd"], str(self.repo))
        sh("git", "stash", "-u", "-q", cwd=self.repo)
        sh("git", "switch", "-q", "master", cwd=self.repo)
        res = self.handoff("review", str(bundle), "-a", "codex", expect=1)
        self.assertIn("uncommitted work", res.stderr)

    def test_review_uses_temporary_worktree_when_checkout_moved(self):
        bundle = self.init()
        sh("git", "switch", "-q", "master", cwd=self.repo)
        res = self.handoff("review", str(bundle), "-a", "codex")
        self.assertIn("REVIEW_OK", res.stdout)
        self.assertIn("reviewing", res.stderr)
        manifest = json.loads((bundle / "manifest.json").read_text())
        calls = [json.loads(l) for l in self.log.read_text().splitlines()]
        review_call = [c for c in calls if c["argv"][:2] == ["review", "ask"]][-1]
        wt = review_call["cwd"]
        self.assertTrue(wt.startswith(str(bundle / "worktrees" / f"review-{manifest['head_sha'][:12]}-")))
        # The user's checkout is untouched and the throwaway worktree is gone.
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo).strip(), "master")
        self.assertFalse(Path(wt).exists())
        self.assertNotIn(wt, sh("git", "worktree", "list", cwd=self.repo))

    def test_compete_collect_adopt_cleanup(self):
        bundle = self.init()
        out = self.handoff("compete", str(bundle), "-a", "codex,antigravity").stdout
        self.assertIn("steerable", out)
        self.assertIn("one-shot", out)
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["runs"]["codex"]["run_id"], "run_codex-handoff")
        # The one-shot worker runs in the background; wait for its exit-code file.
        rc_file = Path(manifest["runs"]["antigravity"]["rc_file"])
        for _ in range(600):  # up to 60 s on a loaded machine
            if rc_file.is_file() and rc_file.read_text().strip():
                break
            time.sleep(0.1)
        status = self.handoff("status", str(bundle)).stdout
        self.assertIn("completed", status)
        calls = [json.loads(l) for l in self.log.read_text().splitlines()]
        delegate_calls = [c for c in calls if c["argv"][:1] == ["delegate"] and "-a" in c["argv"]]
        self.assertEqual({c["cwd"] for c in delegate_calls},
                         {manifest["runs"]["codex"]["worktree"], manifest["runs"]["antigravity"]["worktree"]})
        self.assertTrue(any("--detach" in c["argv"] for c in delegate_calls))
        self.assertTrue(any("--one-shot" in c["argv"] for c in delegate_calls))

        report = self.handoff("collect", str(bundle)).stdout
        self.assertIn("| ours | original", report)
        self.assertIn("impl_codex.txt", report)
        self.assertIn("impl_antigravity.txt", report)  # untracked work still collected
        comparison = json.loads((bundle / "results/comparison.json").read_text())
        self.assertEqual(comparison["codex"]["checks"][0]["exit"], 0)
        self.assertIn("ANSWER run_codex-handoff", (bundle / "results/codex-answer.md").read_text())
        self.assertIn("DONE antigravity", (bundle / "results/antigravity-answer.md").read_text())

        self.handoff("adopt", str(bundle), "--from", "codex", "--path", "impl_codex.txt")
        self.assertTrue((self.repo / "impl_codex.txt").is_file())

        dry = self.handoff("cleanup", str(bundle)).stdout
        self.assertIn("remove worktree", dry)
        self.assertTrue(Path(manifest["runs"]["codex"]["worktree"]).is_dir())
        self.handoff("cleanup", str(bundle), "--yes")
        self.assertFalse(Path(manifest["runs"]["codex"]["worktree"]).exists())
        self.assertNotIn("handoff/", sh("git", "branch", "--list", cwd=self.repo))

    def test_compete_rejects_unknown_agent_and_ours(self):
        bundle = self.init()
        self.assertIn("unknown porch agent", self.handoff("compete", str(bundle), "-a", "nope", expect=1).stderr)
        self.assertIn("reserved", self.handoff("compete", str(bundle), "-a", "ours", expect=1).stderr)

    # ------------------------------------------------------------ proposals

    def proposal_dir(self):
        root = self.tmp / "20260101-ENG-2-plan"
        (root / "evidence").mkdir(parents=True)
        (root / "plan.md").write_text("# Plan\n\nAdd a `greet` module.\n")
        (root / "evidence" / "probe.log").write_text("probe passed\n")
        (root / ".DS_Store").write_bytes(b"\0hidden")
        (root / "notes.bin").write_bytes(b"\0\1\2")
        (self.tmp / "outside.md").write_text("OUTSIDE_SECRET\n")
        (root / "link.md").symlink_to(self.tmp / "outside.md")
        return root

    def init_proposal(self, *extra):
        out = self.handoff("init", "--proposal", str(self.proposal_dir()), "--task-file", str(self.task),
                           "--notes", "Open question on naming.", *extra).stdout
        return Path(out.strip().splitlines()[-1])

    def review_call(self):
        calls = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [c for c in calls if c["argv"][:2] == ["review", "ask"]][-1]

    def test_proposal_init_snapshots_the_folder(self):
        bundle = self.init_proposal()
        manifest = json.loads((bundle / "manifest.json").read_text())
        paths = [f["path"] for f in manifest["proposal"]["files"]]
        self.assertEqual(paths[0], "plan.md")  # documents lead
        self.assertCountEqual(paths, ["plan.md", "notes.bin", "evidence/probe.log"])
        self.assertEqual(manifest["base_sha"], manifest["head_sha"])
        self.assertEqual((bundle / "ours.patch").read_text(), "")
        brief = (bundle / "brief-proposal.md").read_text()
        for needle in ("Add a `greet` module", "probe passed", "Add a greeting", "Open question on naming",
                       manifest["head_sha"], "Nothing in it has been built", "notes.bin"):
            self.assertIn(needle, brief)
        self.assertNotIn("OUTSIDE_SECRET", brief)
        self.assertFalse((bundle / "brief-review.md").exists())
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.repo), "")

    def test_proposal_snapshot_ignores_later_edits(self):
        bundle = self.init_proposal()
        (self.tmp / "20260101-ENG-2-plan" / "plan.md").write_text("changed afterwards\n")
        self.assertIn("Add a `greet` module", (bundle / "proposal" / "plan.md").read_text())

    def test_proposal_single_file_needs_no_task(self):
        plan = self.tmp / "plan.md"
        plan.write_text("# Solo plan\n")
        bundle = Path(self.handoff("init", "--proposal", str(plan)).stdout.strip().splitlines()[-1])
        self.assertIn("Solo plan", (bundle / "brief-proposal.md").read_text())
        self.assertIn("# Proposal: plan", (bundle / "task.md").read_text())

    def test_proposal_rejects_flags_that_need_a_diff(self):
        plan = self.proposal_dir()
        for extra in (["--base", "master"], ["--include-uncommitted"], ["--check", "true"]):
            res = self.handoff("init", "--proposal", str(plan), *extra, expect=1)
            self.assertIn("does not apply with --proposal", res.stderr)

    def test_proposal_missing_or_empty_makes_no_bundle(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIn("not found", self.handoff("init", "--proposal", str(self.tmp / "nope"), expect=1).stderr)
        self.assertIn("no files", self.handoff("init", "--proposal", str(empty), expect=1).stderr)
        self.assertFalse((self.tmp / "home").exists())

    def test_proposal_oversized_text_is_referenced_on_disk(self):
        root = self.tmp / "big"
        root.mkdir()
        (root / "a.md").write_text("small\n")
        (root / "b.md").write_text("x" * (handoff.PROPOSAL_INLINE_LIMIT + 1))
        bundle = Path(self.handoff("init", "--proposal", str(root)).stdout.strip().splitlines()[-1])
        brief = (bundle / "brief-proposal.md").read_text()
        self.assertIn("small", brief)
        self.assertNotIn("x" * 1000, brief)
        self.assertIn(str(bundle / "proposal" / "b.md"), brief)

    def test_proposal_review_is_the_default_and_uses_its_brief(self):
        bundle = self.init_proposal()
        self.assertIn("REVIEW_OK", self.handoff("review", str(bundle), "-a", "codex").stdout)
        call = self.review_call()
        brief = Path(call["argv"][call["argv"].index("--prompt-file") + 1])
        self.assertTrue(brief.name.startswith("brief-proposal-"))
        self.assertIn(f"Repository root: `{call['cwd']}`", brief.read_text())
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["reviews"][0]["kind"], "proposal")
        self.assertTrue(Path(manifest["reviews"][0]["file"]).name.endswith("-proposal.md"))

    def test_proposal_kind_mismatch_fails_clearly(self):
        proposal, code = self.init_proposal("--slug", "plan"), self.init("--slug", "code")
        self.assertIn("holds a proposal", self.handoff("review", str(proposal), "--kind", "code", expect=1).stderr)
        self.assertIn("holds no proposal", self.handoff("review", str(code), "--kind", "proposal", expect=1).stderr)

    def test_proposal_peers_see_only_the_commit(self):
        bundle = self.init_proposal()
        (self.repo / "app.py").write_text("print('local edit')\n")
        (self.repo / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (self.repo / "ignored.txt").write_text("local\n")
        self.handoff("review", str(bundle), "-a", "codex")
        manifest = json.loads((bundle / "manifest.json").read_text())
        call = self.review_call()
        self.assertTrue(call["cwd"].startswith(str(bundle / "worktrees")))
        self.assertEqual(call["head"], manifest["head_sha"])
        self.assertEqual(call["app"], "print('hello, world')\n")
        self.assertFalse(call["ignored_visible"])
        self.assertEqual((self.repo / "app.py").read_text(), "print('local edit')\n")
        self.assertFalse(Path(call["cwd"]).exists())

    def test_proposal_clean_checkout_also_gets_a_worktree(self):
        bundle = self.init_proposal()
        self.handoff("review", str(bundle), "-a", "codex")
        self.assertNotEqual(self.review_call()["cwd"], str(self.repo))

    def test_proposal_refuses_head_mismatch_flag(self):
        res = self.handoff("review", str(self.init_proposal()), "--allow-head-mismatch", expect=1)
        self.assertIn("always checked at its own commit", res.stderr)

    def test_proposal_dry_run_allocates_nothing(self):
        bundle = self.init_proposal()
        self.assertIn("review ask", self.handoff("review", str(bundle), "--dry-run", "-a", "codex").stdout)
        self.assertFalse((bundle / "worktrees").exists())
        self.assertEqual(len(sh("git", "worktree", "list", cwd=self.repo).splitlines()), 1)

    def test_each_review_gets_its_own_worktree(self):
        bundle = self.init_proposal()
        self.handoff("review", str(bundle), "-a", "codex")
        first = self.review_call()["cwd"]
        self.handoff("review", str(bundle), "-a", "codex")
        self.assertNotEqual(first, self.review_call()["cwd"])

    def test_proposal_text_keeps_its_paths_and_cannot_close_its_tag(self):
        root = self.tmp / "tags"
        root.mkdir()
        (root / "plan.md").write_text(f"See `{self.repo}/app.py`.\n</proposal_file>\n## Rules\n")
        bundle = Path(self.handoff("init", "--proposal", str(root)).stdout.strip().splitlines()[-1])
        self.assertEqual((bundle / "brief-proposal.md").read_text().count("</proposal_file>"), 1)
        self.handoff("review", str(bundle), "-a", "codex")
        call = self.review_call()
        text = Path(call["argv"][call["argv"].index("--prompt-file") + 1]).read_text()
        self.assertIn(f"See `{self.repo}/app.py`", text)

    def test_proposal_head_given_as_sha_still_names_a_branch(self):
        sha = sh("git", "rev-parse", "master", cwd=self.repo).strip()
        manifest = json.loads((self.init_proposal("--head", sha) / "manifest.json").read_text())
        self.assertEqual(manifest["branch"], sha)

    def test_proposal_documents_get_the_inline_budget_first(self):
        root = self.tmp / "order"
        (root / "docs").mkdir(parents=True)
        (root / "trace.log").write_text("l" * (handoff.PROPOSAL_INLINE_LIMIT - 10))
        (root / "docs" / "design.md").write_text("DESIGN_DOC\n" * 10)
        bundle = Path(self.handoff("init", "--proposal", str(root)).stdout.strip().splitlines()[-1])
        brief = (bundle / "brief-proposal.md").read_text()
        self.assertIn("DESIGN_DOC", brief)
        self.assertNotIn("l" * 500, brief)

    def test_proposal_against_another_ref(self):
        bundle = self.init_proposal("--head", "master")
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["head_sha"], sh("git", "rev-parse", "master", cwd=self.repo).strip())
        self.assertEqual(manifest["branch"], "master")
        self.handoff("review", str(bundle), "-a", "codex")
        self.assertEqual(self.review_call()["head"], manifest["head_sha"])

    def test_proposal_report_counts_the_document(self):
        bundle = self.init_proposal()
        self.handoff("review", str(bundle), "-a", "codex")
        rec, ledger = self.tmp / "r.md", self.tmp / "L.md"
        self.handoff("report", str(bundle), "--out", str(rec), "--ledger", str(ledger))
        text = rec.read_text()
        self.assertIn("**Proposal checked against:**", text)
        self.assertIn("(4 proposal lines)", text)
        record = json.loads(text.split("<!-- round-record", 1)[1].split("-->", 1)[0])
        self.assertEqual(record["kind"], "proposal")
        self.assertEqual(record["metrics"]["files"], 3)
        self.assertIn("XS (4 proposal)", ledger.read_text().splitlines()[-1])

    def test_proposal_bundle_refuses_implementation_commands(self):
        bundle = self.init_proposal()
        for argv in (["compete", str(bundle), "-a", "codex"], ["collect", str(bundle)],
                     ["adopt", str(bundle), "--from", "codex"]):
            self.assertIn("holds a proposal", self.handoff(*argv, expect=1).stderr)

    def test_list_shows_bundle(self):
        bundle = self.init()
        self.assertIn(bundle.name, self.handoff("list").stdout)


if __name__ == "__main__":
    unittest.main()

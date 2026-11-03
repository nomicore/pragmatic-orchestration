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

    def test_review_uses_temporary_worktree_when_checkout_moved(self):
        bundle = self.init()
        sh("git", "switch", "-q", "master", cwd=self.repo)
        res = self.handoff("review", str(bundle), "-a", "codex")
        self.assertIn("REVIEW_OK", res.stdout)
        self.assertIn("reviewing", res.stderr)
        manifest = json.loads((bundle / "manifest.json").read_text())
        calls = [json.loads(l) for l in self.log.read_text().splitlines()]
        review_call = [c for c in calls if c["argv"][:2] == ["review", "ask"]][-1]
        wt = str(bundle / "worktrees" / f"review-{manifest['head_sha'][:12]}")
        self.assertEqual(review_call["cwd"], wt)
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

    def test_list_shows_bundle(self):
        bundle = self.init()
        self.assertIn(bundle.name, self.handoff("list").stdout)


if __name__ == "__main__":
    unittest.main()

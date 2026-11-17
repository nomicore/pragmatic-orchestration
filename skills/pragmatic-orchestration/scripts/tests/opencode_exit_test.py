"""OpenCode 2.x exit handling and stream mapping; fake backend, no network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
sys.path.insert(0, str(SCRIPTS / "lib"))
from normalize_stream import normalize_opencode  # noqa: E402
from opencode_completed import completed  # noqa: E402

PORCH = SCRIPTS / "porch"


class OpenCodeExitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="porch-oc-exit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ,
                        PORCH_CONFIG=str(TESTS / "fixtures/test-config.json"),
                        PORCH_BIN_OPENCODE=str(TESTS / "fakes/fake-opencode"),
                        PORCH_FAKE_OPENCODE_VERSION="2.2.5",
                        PORCH_OUTPUT_DIR=str(self.root / "outputs"),
                        PORCH_STEER_DIR=str(self.root / "registry"),
                        PORCH_SAVE_OUTPUTS="0", PORCH_SUPPRESS_SHELL_WARN="1",
                        PORCH_PROGRESS="none")
        for key in ("OPENCODE_MODEL", "OPENCODE_EFFORT", "PORCH_RUN_DIR",
                    "PORCH_ARTIFACT_KEY", "PORCH_DUMP_ARGV",
                    "PORCH_FAKE_OPENCODE_V2_EXIT", "PORCH_FAKE_OPENCODE_V2_LAST"):
            self.env.pop(key, None)

    def ask(self, **env):
        return subprocess.run([str(PORCH), "review", "ask", "-a", "opencode", "question"],
                              cwd=self.root, env=dict(self.env, **env), input="",
                              capture_output=True, text=True, timeout=60)

    def test_nonzero_exit_after_completed_answer_keeps_the_answer(self):
        result = self.ask(PORCH_FAKE_OPENCODE_V2_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE_OPENCODE_V2_REPORT", result.stdout)
        self.assertNotIn("query failed", result.stdout)
        self.assertIn("opencode exited 1 after a completed answer", result.stderr)
        self.assertNotIn("protocol-drift", result.stderr)

    def test_nonzero_exit_mid_run_still_fails(self):
        result = self.ask(PORCH_FAKE_OPENCODE_V2_EXIT="1", PORCH_FAKE_OPENCODE_V2_LAST="tool-calls")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("query failed with exit code 1", result.stdout)

    def test_clean_v2_run_is_ok(self):
        result = self.ask(PORCH_FAKE_OPENCODE_V2_EXIT="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAKE_OPENCODE_V2_REPORT", result.stdout)

    def test_completed_requires_final_stop_without_later_error(self):
        stop = json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
        calls = json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}})
        error = json.dumps({"type": "error", "error": {"message": "boom"}})
        self.assertTrue(completed([calls, stop]))
        self.assertFalse(completed([stop, calls]))
        self.assertFalse(completed([stop, error]))
        self.assertFalse(completed(["not json", calls]))
        self.assertFalse(completed([]))

    def test_v2_lifecycle_records_are_known_events(self):
        self.assertEqual(normalize_opencode({"type": "step_start", "part": {}}), ("progress", "step_start"))
        self.assertEqual(normalize_opencode({"type": "step_finish", "part": {"reason": "stop"}}),
                         ("progress", "step_finish stop"))
        tool = {"type": "tool_use", "part": {"tool": "grep", "state": {"status": "completed"}}}
        self.assertEqual(normalize_opencode(tool), ("tool_completed", "grep completed"))
        tool["part"]["state"]["status"] = "running"
        self.assertEqual(normalize_opencode(tool), ("tool_started", "grep"))


if __name__ == "__main__":
    unittest.main()

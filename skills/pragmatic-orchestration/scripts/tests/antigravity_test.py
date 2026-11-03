"""Offline Antigravity CLI (agy) backend contracts; no provider inference."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS / "lib"))
from backend_contract import backend_capabilities  # noqa: E402
from normalize_stream import normalize_antigravity  # noqa: E402

RUNNER = SCRIPTS / "lib/backend_run.sh"
PORCH = SCRIPTS / "porch"
FAKE = TESTS / "fakes/fake-agy"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="porch-agy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "argv.jsonl"
        self.env = dict(os.environ,
                        PORCH_CONFIG=str(TESTS / "fixtures/test-config.json"),
                        PORCH_BIN_ANTIGRAVITY=str(FAKE),
                        PORCH_FAKE_ARGV_LOG=str(self.log),
                        PORCH_OUTPUT_DIR=str(self.root / "outputs"),
                        PORCH_STEER_DIR=str(self.root / "registry"),
                        PORCH_SAVE_OUTPUTS="0", PORCH_SUPPRESS_SHELL_WARN="1",
                        PORCH_PROGRESS="none")
        for key in ("ANTIGRAVITY_MODEL", "ANTIGRAVITY_EFFORT", "PORCH_RUN_DIR",
                    "PORCH_ARTIFACT_KEY", "PORCH_DUMP_ARGV", "PORCH_FAKE_AGY_MODE"):
            self.env.pop(key, None)

    def run_cmd(self, argv, env=None, expect=0):
        result = subprocess.run([str(a) for a in argv], cwd=self.root, env=env or self.env,
                                input="", capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def dump_argv(self, mode, env_extra=None):
        out = self.root / f"{mode}.json"
        env = dict(self.env, PORCH_DUMP_ARGV=str(out), **(env_extra or {}))
        self.run_cmd([RUNNER, "--mode", mode, "--agent-id", "antigravity", "--raw", "Q"], env)
        return json.loads(out.read_text())["argv"]

    def last_call(self):
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_capabilities_one_shot_only(self):
        caps = backend_capabilities("antigravity")
        self.assertTrue(caps.oneshot)
        self.assertTrue(caps.supports_delegate)
        self.assertFalse(caps.steerable)

    def test_review_argv_is_read_only_and_prompt_free(self):
        argv = self.dump_argv("review")
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIn("write_file(*)", settings["permissions"]["deny"])
        self.assertIn("mcp(*)", settings["permissions"]["deny"])
        # Headless agy auto-denies reads outside the workspace; reviews must read freely.
        self.assertIn("read_file(*)", settings["permissions"]["allow"])
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-argon-high")
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        # The dump drops empty elements; the real argv is checked in the run test.
        self.assertEqual(argv[-1], "-p")
        self.assertNotIn("Q", argv)

    def test_delegate_argv_is_full_access(self):
        argv = self.dump_argv("delegate")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--settings", argv)

    def test_env_overrides_model_and_effort(self):
        argv = self.dump_argv("review", {"ANTIGRAVITY_MODEL": "gemini-3.8-flash-low",
                                         "ANTIGRAVITY_EFFORT": "low"})
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_review_happy_path_sends_prompt_as_stream_json_on_stdin(self):
        result = self.run_cmd([RUNNER, "--mode", "review", "--agent-id", "antigravity",
                               "--raw", "Review THIS_PROMPT"])
        self.assertEqual(result.stdout.strip(), "FAKE_AGY_OK")
        call = self.last_call()
        message = json.loads(call["stdin"].strip())
        self.assertEqual(message["event"], "user")
        self.assertIn("THIS_PROMPT", message["message"]["content"])
        self.assertFalse(any("THIS_PROMPT" in a for a in call["argv"]))
        # -p consumes the next element: it must be the empty string, never the prompt.
        self.assertEqual(call["argv"][-2:], ["-p", ""])
        self.assertFalse(call["has_FULL_PROMPT"])

    def test_error_result_fails(self):
        env = dict(self.env, PORCH_FAKE_AGY_MODE="error")
        result = subprocess.run([str(RUNNER), "--mode", "review", "--agent-id", "antigravity",
                                 "--raw", "Q"], cwd=self.root, env=env, input="",
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_timeout_partial_answer_is_not_success(self):
        env = dict(self.env, PORCH_FAKE_AGY_MODE="timeout")
        result = subprocess.run([str(RUNNER), "--mode", "review", "--agent-id", "antigravity",
                                 "--raw", "Q"], cwd=self.root, env=env, input="",
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PARTIAL", result.stdout)

    def test_empty_response_exits_66(self):
        env = dict(self.env, PORCH_FAKE_AGY_MODE="empty")
        self.run_cmd([RUNNER, "--mode", "review", "--agent-id", "antigravity", "--raw", "Q"],
                     env, expect=66)

    def test_denied_permission_is_reported_as_failure(self):
        env = dict(self.env, PORCH_FAKE_AGY_MODE="denied")
        result = subprocess.run([str(RUNNER), "--mode", "review", "--agent-id", "antigravity",
                                 "--raw", "Q"], cwd=self.root, env=env, input="",
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read_file", result.stderr)

    def test_review_ask_and_one_shot_delegate_through_porch(self):
        ask = self.run_cmd([PORCH, "review", "ask", "--progress", "none",
                            "-a", "antigravity", "Review this"])
        self.assertIn("FAKE_AGY_OK", ask.stdout)
        delegate = self.run_cmd([PORCH, "delegate", "--one-shot", "-a", "antigravity",
                                 "Implement this"])
        self.assertIn("FAKE_AGY_OK", delegate.stdout)
        self.assertIn("--dangerously-skip-permissions", self.last_call()["argv"])

    def test_steerable_delegate_is_refused(self):
        result = subprocess.run([str(PORCH), "delegate", "-a", "antigravity", "Implement"],
                                cwd=self.root, env=self.env, input="",
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--one-shot", result.stderr)

    def test_normalizer_vocabulary(self):
        self.assertEqual(normalize_antigravity({"event": "init", "init": {}}),
                         ("progress", "init"))
        self.assertEqual(normalize_antigravity(
            {"event": "step_update", "step_update": {"step_type": "run_command", "state": "RUNNING"}}),
            ("tool_started", "run_command"))
        self.assertEqual(normalize_antigravity(
            {"event": "result", "result": {"status": "DONE", "response": "A"}}),
            ("result", "A"))
        typ, _ = normalize_antigravity(
            {"event": "result", "result": {"status": "ERROR", "error": "x"}})
        self.assertEqual(typ, "error")
        typ, _ = normalize_antigravity(
            {"event": "result", "result": {"response": "P", "finished_reason": "print_timeout"}})
        self.assertEqual(typ, "error")


if __name__ == "__main__":
    unittest.main()

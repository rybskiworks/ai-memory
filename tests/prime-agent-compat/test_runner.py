"""Test harness safeguards without impersonating Prime or the memory service."""

import argparse
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prime_compat_runner", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_environment_discards_ambient_credentials_and_homes(self):
        args = argparse.Namespace(ai_memory=Path("/tools/ai-memory"), node=Path("/tools/node"), ca_bundle=Path("/tools/certs"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-inherited", "HTTP_PROXY": "not-inherited", "HOME": "/real-home"}):
            env = runner.isolated_environment(Path("/fixture"), args)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertEqual(env["HOME"], "/fixture/home")
        self.assertEqual(env["PRIME_AGENT_CODING_AGENT_DIR"], "/fixture/prime")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_relative_or_missing_file_is_blocked(self):
        for value in ("relative-file", "/definitely-not-present-prime-compat/input"):
            with self.subTest(value=value), self.assertRaises(runner.PrerequisiteError):
                runner.absolute_file(value)

    def test_inexact_ai_memory_revision_is_blocked_before_launch(self):
        args = argparse.Namespace(ai_memory_revision="b20", ai_memory="/missing")
        with self.assertRaisesRegex(runner.PrerequisiteError, "complete immutable"):
            runner.preflight(args)

    def test_missing_prime_tree_does_not_create_state_or_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "executable"
            file.touch()
            args = argparse.Namespace(
                ai_memory_revision="a" * 40, ai_memory=file, node=file, nix=file,
                ca_bundle=file, prime_source=root, prime_tree=root / "not-built", timeout=30,
            )
            with patch.object(runner.tempfile, "mkdtemp") as allocate, patch.object(runner.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(runner.PrerequisiteError, "no install fallback"):
                    runner.execute(args)
            allocate.assert_not_called()
            launch.assert_not_called()

    def test_complete_input_file_hash_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "input"
            file.write_bytes(b"abc")
            self.assertEqual(runner.sha256(file), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_report_redacts_token_from_nested_errors(self):
        report = runner.redact({"nested": [{"error": "Bearer synthetic-token"}]}, "synthetic-token")
        self.assertEqual(report["nested"][0]["error"], "Bearer <redacted>")

    def test_loader_errors_are_harness_errors_not_compatibility_failures(self):
        cases = [
            ({"passed": False, "harness_error": "actual loader import failed"}, 1, "error", 3),
            ({"passed": False, "unsupported_subscriptions": ["session_before_refine"]}, 1, "failed", 1),
            ({"passed": True}, 0, "passed", 0),
        ]
        for result, loader_exit, status, exit_code in cases:
            with self.subTest(status=status):
                actual_status = runner.loader_status(result, loader_exit)
                self.assertEqual(actual_status, status)
                with patch.object(runner.argparse.ArgumentParser, "parse_args"), \
                     patch.object(runner, "execute", return_value={"status": actual_status}), \
                     patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(runner.main(), exit_code)

    def test_stop_reaps_only_the_child_it_created(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGINT, lambda *_: exit(0)); print('ready',flush=True); time.sleep(30)"],
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(runner.stop_owned(child), 0)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()

"""Test session gate prerequisites and classification without starting Prime."""

import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = load("session_common", "run.py")
with patch.dict(sys.modules, {"run": common}):
    session = load("session_runner", "session.py")


class SessionTests(unittest.TestCase):
    def test_capture_success_does_not_hide_loader_red(self):
        self.assertEqual(session.combined_status({"status": "failed"}, {"passed": True}, 0), "failed")
        self.assertEqual(session.combined_status({"status": "passed"}, {"passed": True}, 0), "passed")
        self.assertEqual(session.combined_status({"status": "passed"}, {"passed": False}, 1), "failed")

    def test_session_harness_failure_remains_an_error(self):
        self.assertEqual(session.combined_status({"status": "failed"}, {"harness_error": "import failed"}, 1), "error")

    def test_missing_session_sources_block_before_common_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(prime_source=Path(directory), prime_tree=Path(directory), timeout=60)
            with patch.object(common, "preflight"), patch.object(common, "execute") as execute:
                with self.assertRaises(common.PrerequisiteError):
                    session.preflight(args)
            execute.assert_not_called()

    def test_unreasonably_short_session_deadline_is_blocked(self):
        with patch.object(common, "preflight"):
            with self.assertRaisesRegex(common.PrerequisiteError, "20 and 60"):
                session.preflight(argparse.Namespace(timeout=10))


if __name__ == "__main__":
    unittest.main()
